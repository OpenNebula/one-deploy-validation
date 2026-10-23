#!/usr/bin/env python3
"""Exercise one disposable OpenNebula VM from the primary frontend.

The argument is base64 JSON so Ansible can pass the configuration without
shell quoting.
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CHECKS = (
    "deployment",
    "libvirt state",
    "FireEdge VNC",
    "guest readiness",
    "power-off and power-on",
    "reboot",
    "cold migration",
)


class ProbeError(Exception):
    pass


class FatalProbeError(ProbeError):
    pass


def run_command(argv, timeout=45):
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("%s: %s" % (argv[0], exc)) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise ProbeError("%s exited %s: %s" % (argv[0], result.returncode, detail))
    return result.stdout.strip()


def vm_info(vm_id):
    try:
        return json.loads(run_command(["onevm", "show", str(vm_id), "-j"]))["VM"]
    except (ValueError, KeyError) as exc:
        raise ProbeError("onevm show returned invalid VM JSON") from exc


def last_history(vm):
    history = vm.get("HISTORY_RECORDS", {}).get("HISTORY", [])
    if isinstance(history, dict):
        return history
    return history[-1] if history else {}


def vm_running(vm, host_id=None):
    if str(vm.get("STATE")) != "3" or str(vm.get("LCM_STATE")) != "3":
        return False
    return host_id is None or str(last_history(vm).get("HID")) == str(host_id)


def select_target(source_id, eligible_ids):
    ids = [str(item) for item in eligible_ids]
    source = str(source_id)
    if source not in ids or len(ids) < 2:
        return None
    return ids[(ids.index(source) + 1) % len(ids)]


def wait_for(predicate, timeout, interval, description):
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            value = predicate()
            if value:
                return value
        except FatalProbeError:
            raise
        except ProbeError as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            raise ProbeError("timeout waiting for %s%s" % (description, "; " + last if last else ""))
        time.sleep(interval)


def wait_vm(vm_id, timeout, interval, state, host_id=None):
    def inspect():
        vm = vm_info(vm_id)
        if state == "running" and vm_running(vm, host_id):
            return vm
        if state == "poweroff" and str(vm.get("STATE")) == "8":
            return vm
        if str(vm.get("LCM_STATE")) in {"36", "37", "38", "39"}:
            raise FatalProbeError("VM entered failure state %s/%s" % (vm.get("STATE"), vm.get("LCM_STATE")))
        return None

    return wait_for(inspect, timeout, interval, "%s (VM %s)" % (state, vm_id))


def wait_held(vm_id, timeout, interval):
    def inspect():
        vm = vm_info(vm_id)
        state = str(vm.get("STATE"))
        if state == "2":  # HOLD: cloning finished; manual deploy is allowed.
            return vm
        if state not in {"0", "10"}:  # INIT and CLONING are transient.
            raise FatalProbeError("VM entered state %s before deployment" % state)
        return None

    return wait_for(inspect, timeout, interval, "held VM after image cloning (VM %s)" % vm_id)


def classify_libvirt(output):
    state = output.strip().lower()
    if state.startswith("running"):
        return "running"
    if "paused" in state and "starting up" in state:
        return "paused (starting up)"
    return state or "unknown"


def libvirt_reason(vm_id, vm, host_address):
    domain = str(vm.get("DEPLOY_ID") or "one-%s" % vm_id)
    output = run_command(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host_address,
         "virsh", "-c", "qemu:///system", "domstate", "--reason", domain],
        timeout=15,
    )
    return classify_libvirt(output)


def wait_deployment(vm_id, host_id, host_address, timeout, interval):
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            vm = vm_info(vm_id)
            if vm_running(vm, host_id):
                return vm
            if str(vm.get("LCM_STATE")) in {"36", "37", "38", "39"}:
                raise FatalProbeError("VM entered failure state %s/%s" % (vm.get("STATE"), vm.get("LCM_STATE")))
            last = "OpenNebula state %s/%s" % (vm.get("STATE"), vm.get("LCM_STATE"))
            if vm.get("DEPLOY_ID"):
                try:
                    last = libvirt_reason(vm_id, vm, host_address)
                except ProbeError:
                    pass
        except FatalProbeError:
            raise
        except ProbeError as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            if last == "paused (starting up)":
                raise ProbeError("libvirt domain remained paused (starting up) during deployment")
            raise ProbeError("timeout waiting for VM %s on host %s: %s" % (vm_id, host_id, last))
        time.sleep(interval)


def wait_libvirt(vm_id, vm, host_address, timeout, interval):
    domain = str(vm.get("DEPLOY_ID") or "one-%s" % vm_id)
    deadline = time.monotonic() + timeout
    last = "not found"
    while True:
        try:
            last = libvirt_reason(vm_id, vm, host_address)
            if last == "running":
                return last
        except ProbeError as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            if last == "paused (starting up)":
                raise ProbeError("libvirt domain %s remained paused (starting up) on %s" % (domain, host_address))
            raise ProbeError("libvirt domain %s did not run on %s: %s" % (domain, host_address, last))
        time.sleep(interval)


def guest_ip(vm):
    nics = vm.get("TEMPLATE", {}).get("NIC", [])
    if isinstance(nics, dict):
        nics = [nics]
    for nic in nics:
        ip = nic.get("IP") or nic.get("IP6_GLOBAL")
        if ip:
            return str(ip)
    raise ProbeError("VM has no assigned guest IP")


def guest_boot_id(ip):
    return run_command(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
         "root@%s" % ip, "cat", "/proc/sys/kernel/random/boot_id"],
        timeout=12,
    )


def wait_guest(ip, timeout, interval, previous=None):
    def inspect():
        boot_id = guest_boot_id(ip)
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", boot_id):
            raise ProbeError("guest returned an invalid boot ID")
        return boot_id if previous is None or boot_id != previous else None

    return wait_for(inspect, timeout, interval, "guest SSH and a new boot ID at %s" % ip)


def fireedge_url(config):
    url = str(config.get("fireedge_url") or "").strip()
    if not url:
        port = 2616
        try:
            with open("/etc/one/fireedge-server.conf", encoding="utf-8") as conf:
                for line in conf:
                    match = re.match(r"^port:\s*['\"]?(\d+)", line)
                    if match:
                        port = int(match.group(1))
                        break
        except OSError:
            pass
        url = "http://127.0.0.1:%s" % port
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ProbeError("fireedge_url must be an HTTP(S) origin without a path or credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProbeError("fireedge_url has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ProbeError("fireedge_url has an invalid port")
    return url.rstrip("/")


def fireedge_credentials():
    auth_file = os.environ.get("ONE_AUTH") or "/var/lib/one/.one/one_auth"
    try:
        with open(auth_file, encoding="utf-8") as credentials:
            user, password = credentials.read().strip().split(":", 1)
    except (OSError, ValueError) as exc:
        raise ProbeError("could not read oneadmin credentials from one_auth") from exc
    if not user or not password:
        raise ProbeError("one_auth has empty credentials")
    return user, password


def fireedge_session(vm_id, url, verify_tls, deadline):
    user, password = fireedge_credentials()
    cookies = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(cookies)]
    if not verify_tls:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)

    def request(path, data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("timeout establishing FireEdge VNC session")
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url + path, data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            with opener.open(req, timeout=remaining) as response:
                return json.loads(response.read(65536))
        except urllib.error.HTTPError as exc:
            raise ProbeError("FireEdge %s returned HTTP %s" % (path.split("/")[-1] or "login", exc.code)) from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ProbeError("FireEdge request failed (%s)" % type(exc).__name__) from exc

    request("/fireedge/api/auth/", {"user": user, "token": password, "remember": False})
    if not any(cookie.name == "FireedgeToken" for cookie in cookies):
        raise ProbeError("FireEdge login did not establish a session")
    response = request("/fireedge/api/vm/%s/guacamole/vnc" % vm_id)
    token = response.get("data") if isinstance(response, dict) else response
    if not isinstance(token, str) or not token:
        raise ProbeError("FireEdge returned no VNC session token")
    return token


def recv_exact(sock, length, deadline):
    parts = []
    while length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError("timeout waiting for FireEdge VNC console")
        sock.settimeout(remaining)
        chunk = sock.recv(length)
        if not chunk:
            raise ProbeError("FireEdge VNC console closed before sync")
        parts.append(chunk)
        length -= len(chunk)
    return b"".join(parts)


def send_ws_frame(sock, opcode, payload=b""):
    mask = secrets.token_bytes(4)
    length = len(payload)
    header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else bytes([0x80 | opcode, 0xfe]) + length.to_bytes(2, "big")
    sock.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))


def wait_guacamole_sync(url, token, verify_tls, deadline):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # FireEdge's browser client sends rangeports for VNC. That makes FireEdge
    # create its SSH tunnel to the hypervisor, including VNC sockets bound to
    # loopback on the host. Without it guacd tries a direct connection.
    query = urllib.parse.urlencode({"token": token, "width": "1024", "height": "768", "dpi": "96", "enable-audio": "true", "rangeports": "5900:65535"})
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    host_header = host if ":" not in host else "[%s]" % host
    request = (
        "GET /fireedge/guacamole?%s HTTP/1.1\r\n"
        "Host: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ) % (query, host_header, port, key)
    try:
        with socket.create_connection((host, port), timeout=max(0.01, deadline - time.monotonic())) as connection:
            if parsed.scheme == "https":
                context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
                sock = context.wrap_socket(connection, server_hostname=host)
            else:
                sock = connection
            with sock:
                sock.sendall(request.encode("ascii"))
                headers = bytearray()
                while not headers.endswith(b"\r\n\r\n"):
                    if len(headers) >= 16384:
                        raise ProbeError("invalid FireEdge WebSocket response")
                    headers.extend(recv_exact(sock, 1, deadline))
                lines = headers.decode("iso-8859-1").split("\r\n")
                if not re.match(r"^HTTP/1\.[01] 101(?:\s|$)", lines[0]):
                    raise ProbeError("FireEdge WebSocket upgrade failed: %s" % lines[0][:80])
                fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
                fields = {name.lower(): value.strip() for name, value in fields.items()}
                expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
                if fields.get("sec-websocket-accept") != expected or fields.get("upgrade", "").lower() != "websocket":
                    raise ProbeError("invalid FireEdge WebSocket upgrade")
                stream = ""
                fragment = bytearray()
                fragmented = False
                while True:
                    first, second = recv_exact(sock, 2, deadline)
                    opcode, fin = first & 0x0f, first & 0x80
                    if first & 0x70:
                        raise ProbeError("invalid FireEdge WebSocket frame flags")
                    if second & 0x80:
                        raise ProbeError("invalid masked FireEdge WebSocket frame")
                    length = second & 0x7f
                    if length == 126:
                        length = int.from_bytes(recv_exact(sock, 2, deadline), "big")
                    elif length == 127:
                        length = int.from_bytes(recv_exact(sock, 8, deadline), "big")
                    if length > 1048576:
                        raise ProbeError("oversized FireEdge WebSocket frame")
                    payload = recv_exact(sock, length, deadline)
                    if opcode == 9:
                        send_ws_frame(sock, 10, payload)
                        continue
                    if opcode == 8:
                        raise ProbeError("FireEdge VNC console closed before sync")
                    if opcode not in (0, 1):
                        raise ProbeError("unexpected FireEdge WebSocket frame")
                    if (opcode == 1 and fragmented) or (opcode == 0 and not fragmented):
                        raise ProbeError("invalid fragmented FireEdge WebSocket message")
                    fragment.extend(payload)
                    if len(fragment) > 1048576:
                        raise ProbeError("oversized FireEdge VNC message")
                    if fin:
                        try:
                            stream += fragment.decode("utf-8")
                        except UnicodeError as exc:
                            raise ProbeError("invalid FireEdge VNC text message") from exc
                        fragment.clear()
                        fragmented = False
                        if re.search(r"(?:^|;)5\.error,", stream):
                            raise ProbeError("FireEdge VNC console reported an error")
                        if re.search(r"(?:^|;)4\.sync,\d+\.[^;]*;", stream):
                            return
                        stream = stream[-1048576:]
                    else:
                        fragmented = True
    except socket.timeout as exc:
        raise ProbeError("timeout waiting for FireEdge VNC console") from exc
    except OSError as exc:
        raise ProbeError("FireEdge VNC console connection failed (%s)" % type(exc).__name__) from exc


def check_fireedge_vnc(vm_id, vm, config):
    graphics = vm.get("TEMPLATE", {}).get("GRAPHICS") or {}
    if isinstance(graphics, list):
        graphics = graphics[0] if graphics else {}
    if str(graphics.get("TYPE", "")).upper() != "VNC" or not graphics.get("PORT"):
        raise ProbeError("VM template has no VNC graphics type or port")
    deadline = time.monotonic() + int(config["vnc_timeout"])
    url = fireedge_url(config)
    verify_tls = config["fireedge_verify_tls"]
    token = fireedge_session(vm_id, url, verify_tls, deadline)
    while True:
        try:
            wait_guacamole_sync(url, token, verify_tls, deadline)
            return
        except ProbeError as exc:
            # The VM can be RUNNING before its VNC socket accepts connections.
            # Retry guacd connection errors during the configured readiness window.
            if str(exc) != "FireEdge VNC console reported an error":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(int(config["poll_interval"]), remaining))
            if time.monotonic() >= deadline:
                raise


def execute(config):
    results = {name: {"name": name, "status": "skipped - earlier check did not complete", "detail": ""} for name in CHECKS}

    def record(name, status, detail=""):
        results[name] = {"name": name, "status": status, "detail": detail[:500]}

    source = str(config["source_id"])
    target = select_target(source, config["eligible_ids"])
    vm_id = None
    try:
        output = run_command(
            ["onetemplate", "instantiate", str(config["template_id"]), "--hold",
             "--nic", config["vnet_name"], "--name", config["vm_name"],
             "--ssh", "--net_context"],
            timeout=90,
        )
        match = re.search(r"(?:VM )?ID:\s*(\d+)", output)
        if not match:
            raise ProbeError("onetemplate instantiate returned no VM ID")
        vm_id = match.group(1)
        wait_held(vm_id, int(config["deploy_timeout"]), int(config["poll_interval"]))
        run_command(["onevm", "deploy", vm_id, source, "--enforce"], timeout=90)
        vm = wait_deployment(vm_id, source, config["hosts"][source], int(config["deploy_timeout"]), int(config["poll_interval"]))
        record("deployment", "ok", "VM %s deployed on host %s" % (vm_id, source))
    except ProbeError as exc:
        record("deployment", "failed", str(exc))
        return list(results.values())

    try:
        wait_libvirt(vm_id, vm, config["hosts"][source], int(config["deploy_timeout"]), int(config["poll_interval"]))
        record("libvirt state", "ok")
    except ProbeError as exc:
        record("libvirt state", "failed", str(exc))
        return list(results.values())

    try:
        check_fireedge_vnc(vm_id, vm, config)
        record("FireEdge VNC", "ok", "VM %s console delivered a Guacamole sync instruction" % vm_id)
    except ProbeError as exc:
        record("FireEdge VNC", "failed", str(exc))

    try:
        ip = guest_ip(vm)
        boot_id = wait_guest(ip, int(config["guest_timeout"]), int(config["poll_interval"]))
        record("guest readiness", "ok", "guest SSH responded at %s" % ip)
    except ProbeError as exc:
        record("guest readiness", "failed", str(exc))
        return list(results.values())

    try:
        run_command(["onevm", "poweroff", vm_id], timeout=60)
        wait_vm(vm_id, int(config["poweroff_timeout"]), int(config["poll_interval"]), "poweroff")
        run_command(["onevm", "resume", vm_id], timeout=60)
        vm = wait_deployment(vm_id, source, config["hosts"][source], int(config["deploy_timeout"]), int(config["poll_interval"]))
        wait_libvirt(vm_id, vm, config["hosts"][source], int(config["deploy_timeout"]), int(config["poll_interval"]))
        new_boot_id = wait_guest(ip, int(config["guest_timeout"]), int(config["poll_interval"]), boot_id)
        record("power-off and power-on", "ok", "poweroff, resume and new guest boot ID confirmed")
        boot_id = new_boot_id
    except ProbeError as exc:
        record("power-off and power-on", "failed", str(exc))
        return list(results.values())

    try:
        run_command(["onevm", "reboot", vm_id], timeout=60)
        boot_id = wait_guest(ip, int(config["reboot_timeout"]), int(config["poll_interval"]), boot_id)
        vm = wait_deployment(vm_id, source, config["hosts"][source], int(config["deploy_timeout"]), int(config["poll_interval"]))
        wait_libvirt(vm_id, vm, config["hosts"][source], int(config["deploy_timeout"]), int(config["poll_interval"]))
        record("reboot", "ok", "new guest boot ID confirmed")
    except ProbeError as exc:
        record("reboot", "failed", str(exc))
        return list(results.values())

    if target is None:
        record("cold migration", "skipped - no second eligible KVM host in cluster")
        return list(results.values())
    try:
        # Save/restore carries the source CPU state to the destination. Booting
        # afresh also exercises migration between hosts with different CPUs.
        run_command(["onevm", "migrate", vm_id, target, "--poff", "--enforce"], timeout=90)
        vm = wait_deployment(vm_id, target, config["hosts"][target], int(config["migration_timeout"]), int(config["poll_interval"]))
        wait_libvirt(vm_id, vm, config["hosts"][target], int(config["migration_timeout"]), int(config["poll_interval"]))
        wait_guest(guest_ip(vm), int(config["guest_timeout"]), int(config["poll_interval"]), boot_id)
        record("cold migration", "ok", "VM %s moved from host %s to %s; guest rebooted and reachable" % (vm_id, source, target))
    except ProbeError as exc:
        record("cold migration", "failed", str(exc))
    return list(results.values())


def main():
    try:
        config = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
        checks = execute(config)
    except Exception as exc:  # Preserve a report row even for an unexpected probe error.
        checks = [{"name": name, "status": "failed" if name == "deployment" else "skipped - probe failed",
                   "detail": ("probe error: %s" % exc)[:500] if name == "deployment" else ""} for name in CHECKS]
    print(json.dumps({"checks": checks}))


if __name__ == "__main__":
    main()
