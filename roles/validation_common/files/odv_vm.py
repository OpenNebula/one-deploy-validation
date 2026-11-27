"""OpenNebula VM and guest SSH helpers used by frontend validation probes."""

import json
import re
import subprocess
import time


class ProbeError(Exception):
    pass


class FatalProbeError(ProbeError):
    pass


def run_command(argv, timeout=45, stdin=None):
    try:
        result = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=timeout, check=False)
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


def guest_ip(vm):
    nics = vm.get("TEMPLATE", {}).get("NIC", [])
    if isinstance(nics, dict):
        nics = [nics]
    for nic in nics:
        ip = nic.get("IP") or nic.get("IP6_GLOBAL")
        if ip:
            return str(ip)
    raise ProbeError("VM has no assigned guest IP")


def guest_command(ip, command, stdin=None):
    return run_command(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
         "root@%s" % ip, command],
        timeout=12, stdin=stdin,
    )


def guest_boot_id(ip):
    return guest_command(ip, "cat /proc/sys/kernel/random/boot_id")


def wait_guest(ip, timeout, interval, previous=None):
    def inspect():
        boot_id = guest_boot_id(ip)
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", boot_id):
            raise ProbeError("guest returned an invalid boot ID")
        return boot_id if previous is None or boot_id != previous else None

    return wait_for(inspect, timeout, interval, "guest SSH and a new boot ID at %s" % ip)
