#!/usr/bin/env python3
"""Host-side OpenNebula SR-IOV PCI passthrough validation.

Creates a disposable PCI-only VM, verifies exact VF allocation against
OpenNebula and libvirt, then terminates the VM and verifies VF release.

No guest access or network connectivity is required.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET


CHECKS = (
    "preflight",
    "VM request",
    "deployment",
    "PCI allocation",
    "libvirt hostdev",
    "cleanup",
)


class ProbeError(Exception):
    pass


def run(argv, timeout=60, check=True):
    """Run a command without inheriting stdin or invoking a shell."""
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"{argv[0]}: {exc}") from exc

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProbeError(
            f"{argv[0]} exited {result.returncode}: {detail[:500]}"
        )

    return result


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_vf(address):
    """Return canonical PCI address: 0000:bb:ss.f."""
    address = str(address).strip().lower()

    pattern = r"(?:(?:[0-9a-f]{4}):)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]"

    if not re.fullmatch(pattern, address):
        raise ProbeError(f"Invalid PCI address: {address}")

    if len(address.split(":")) == 2:
        address = "0000:" + address

    return address


def short_vf(address):
    return normalize_vf(address).split(":", 1)[1]


def one_json(argv):
    result = run(argv)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ProbeError(f"Invalid JSON from {argv[0]}") from exc


def get_host(host_id):
    return one_json(["onehost", "show", str(host_id), "-j"])["HOST"]


def get_vm(vm_id):
    return one_json(["onevm", "show", str(vm_id), "-j"])["VM"]


def get_template(template_id):
    return one_json(
        ["onetemplate", "show", str(template_id), "-j"]
    )["VMTEMPLATE"]


def get_pci(host_id, address):
    host = get_host(host_id)

    devices = (
        host.get("HOST_SHARE", {})
        .get("PCI_DEVICES", {})
        .get("PCI")
    )

    matches = [
        device
        for device in as_list(devices)
        if device.get("SHORT_ADDRESS")
        and normalize_vf(device["SHORT_ADDRESS"]) == address
    ]

    if len(matches) != 1:
        raise ProbeError(
            f"Expected one OpenNebula PCI entry for {address}; "
            f"found {len(matches)}"
        )

    return matches[0]


def vm_pci_address(vm):
    devices = as_list(vm.get("TEMPLATE", {}).get("PCI"))

    if len(devices) != 1:
        raise ProbeError(
            f"Expected exactly one VM PCI device; found {len(devices)}"
        )

    address = devices[0].get("SHORT_ADDRESS")

    if not address:
        raise ProbeError("VM PCI device has no SHORT_ADDRESS")

    return normalize_vf(address)


def wait_for_vm(vm_id, expected_host, timeout, interval):
    """Wait for an ACTIVE/RUNNING VM on the requested host."""
    deadline = time.monotonic() + timeout
    last_state = "unknown"

    while time.monotonic() < deadline:
        vm = get_vm(vm_id)

        state = str(vm.get("STATE"))
        lcm = str(vm.get("LCM_STATE"))
        last_state = f"{state}/{lcm}"

        if state == "3" and lcm == "3":
            history = vm.get("HISTORY_RECORDS", {}).get("HISTORY")

            if history:
                records = as_list(history)
                actual_host = str(records[-1].get("HID", ""))

                if actual_host != str(expected_host):
                    raise ProbeError(
                        f"VM deployed on host {actual_host}, "
                        f"expected {expected_host}"
                    )

            return vm

        if lcm in ("36", "37", "38", "39"):
            raise ProbeError(
                f"VM entered failure state {last_state}"
            )

        time.sleep(interval)

    raise ProbeError(
        f"Timed out waiting for VM RUNNING; last state={last_state}"
    )


def wait_for_hold(vm_id, timeout, interval):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        vm = get_vm(vm_id)
        state = str(vm.get("STATE"))

        if state == "2":
            return vm

        if state not in ("0", "10"):
            raise ProbeError(
                f"VM entered unexpected state {state} before deployment"
            )

        time.sleep(interval)

    raise ProbeError("Timed out waiting for VM HOLD state")


def ssh_virsh(host, *arguments):
    """Run virsh on a hypervisor over the existing SSH connection."""
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        host,
        "virsh",
        "-c", "qemu:///system",
        *arguments,
    ]

    return run(command, timeout=30).stdout.strip()


def libvirt_pci_addresses(xml):
    """Extract physical PCI source addresses, not guest PCI addresses."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ProbeError(f"Invalid libvirt XML: {exc}") from exc

    addresses = []

    for device in root.findall("./devices/hostdev"):
        if device.get("type") != "pci":
            continue

        source = device.find("./source/address")

        if source is None:
            continue

        try:
            address = "%04x:%02x:%02x.%x" % (
                int(source.get("domain"), 16),
                int(source.get("bus"), 16),
                int(source.get("slot"), 16),
                int(source.get("function"), 16),
            )
        except (TypeError, ValueError) as exc:
            raise ProbeError(
                "Invalid PCI source address in libvirt XML"
            ) from exc

        addresses.append(address)

    return addresses


def cleanup_vm(vm_id, vm_name, host_id, vf, timeout, interval):
    """Delete only the VM created by this probe and verify VF release."""
    vm = get_vm(vm_id)

    if vm.get("NAME") != vm_name:
        raise ProbeError(
            f"Refusing cleanup: VM {vm_id} has an unexpected name"
        )

    result = run(
        ["onevm", "terminate", "--hard", str(vm_id)],
        check=False,
    )

    if result.returncode != 0:
        run(["onevm", "recover", str(vm_id), "--delete"])

    deadline = time.monotonic() + timeout
    deleted = False

    while time.monotonic() < deadline:
        result = run(
            ["onevm", "show", str(vm_id), "-j"],
            check=False,
        )

        if result.returncode != 0:
            deleted = True
            break

        try:
            current = json.loads(result.stdout)["VM"]
        except (ValueError, KeyError):
            raise ProbeError("Cannot determine VM state during cleanup")

        if current.get("NAME") != vm_name:
            raise ProbeError(
                "VM identity changed during cleanup"
            )

        if str(current.get("STATE")) == "6":
            deleted = True
            break

        time.sleep(interval)

    if not deleted:
        raise ProbeError(f"VM {vm_id} was not deleted")

    # OpenNebula may release the PCI resource asynchronously.
    while time.monotonic() < deadline:
        pci = get_pci(host_id, vf)

        if str(pci.get("VMID")) == "-1":
            return f"VM {vm_id} deleted; VF {vf} released"

        time.sleep(interval)

    raise ProbeError(
        f"VM {vm_id} deleted but VF {vf} was not released"
    )


def execute(config):
    host_id = str(config.host_id)
    host = config.host
    vf = normalize_vf(config.vf)
    vf_short = short_vf(vf)
    vm_name = config.vm_name

    vm_id = None
    override_path = None

    checks = {
        name: {
            "name": name,
            "status": "skipped - prerequisite not completed",
            "detail": "",
        }
        for name in CHECKS
    }

    def record(name, status, detail=""):
        checks[name] = {
            "name": name,
            "status": status,
            "detail": str(detail)[:600],
        }

    stage = "preflight"

    try:
        # ----------------------------------------------------
        # 1. PREFLIGHT
        # ----------------------------------------------------
        target_host = get_host(host_id)

        if target_host.get("NAME") != host:
            raise ProbeError(
                f"Host ID {host_id} is {target_host.get('NAME')}, "
                f"not {host}"
            )

        pci = get_pci(host_id, vf)

        if str(pci.get("VMID")) != "-1":
            raise ProbeError(
                f"VF {vf} is already allocated to "
                f"VMID={pci.get('VMID')}"
            )

        template = get_template(config.template_id)
        template_body = template.get("TEMPLATE", {})

        if as_list(template_body.get("PCI")):
            raise ProbeError(
                "Base template already contains PCI devices"
            )

        if as_list(template_body.get("NIC")):
            raise ProbeError(
                "Base template contains NICs; "
                "use a PCI-only test template"
            )

        record(
            stage,
            "ok",
            f"Host={host_id}; VF={vf}; "
            f"VMID=-1; template={config.template_id}",
        )

        # ----------------------------------------------------
        # 2. CREATE VM WITH EXACT PCI ADDRESS
        # ----------------------------------------------------
        stage = "VM request"

        override = f'''CPU = "1"
VCPU = "{config.vcpu}"
MEMORY = "{config.memory}"
SCHED_REQUIREMENTS = "ID = {host_id}"

PCI = [
  SHORT_ADDRESS = "{vf_short}"
]
'''

        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="odv-sriov-",
            suffix=".one",
            dir="/tmp",
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(override)
            override_path = handle.name

        output = run([
            "onetemplate", "instantiate",
            str(config.template_id),
            override_path,
            "--name", vm_name,
            "--hold",
        ], timeout=90).stdout

        match = re.search(r"(?:VM )?ID:\s*(\d+)", output)

        if not match:
            raise ProbeError(
                "Instantiation succeeded but returned no VM ID"
            )

        vm_id = match.group(1)

        held_vm = wait_for_hold(
            vm_id,
            config.deploy_timeout,
            config.poll_interval,
        )

        requested = vm_pci_address(held_vm)

        if requested != vf:
            raise ProbeError(
                f"VM request changed from {vf} to {requested}"
            )

        if as_list(held_vm.get("TEMPLATE", {}).get("NIC")):
            raise ProbeError("Test VM unexpectedly contains a NIC")

        record(
            stage,
            "ok",
            f"VM {vm_id} on HOLD requests exact VF {requested}",
        )

        # ----------------------------------------------------
        # 3. DEPLOY VM
        # ----------------------------------------------------
        stage = "deployment"

        run([
            "onevm", "deploy",
            str(vm_id),
            host_id,
        ], timeout=90)

        running_vm = wait_for_vm(
            vm_id,
            host_id,
            config.deploy_timeout,
            config.poll_interval,
        )

        record(
            stage,
            "ok",
            f"VM {vm_id} ACTIVE/RUNNING on host {host}",
        )

        # ----------------------------------------------------
        # 4. VERIFY OPENNEBULA PCI ALLOCATION
        # ----------------------------------------------------
        stage = "PCI allocation"

        allocated = vm_pci_address(running_vm)

        if allocated != vf:
            raise ProbeError(
                f"VM requested {vf} but OpenNebula "
                f"allocated {allocated}"
            )

        pci = get_pci(host_id, vf)
        owner = str(pci.get("VMID"))

        if owner != str(vm_id):
            raise ProbeError(
                f"VF {vf} allocated to VMID={owner}, "
                f"expected {vm_id}"
            )

        record(
            stage,
            "ok",
            f"VM template VF={allocated}; "
            f"host PCI allocation VMID={owner}",
        )

        # ----------------------------------------------------
        # 5. VERIFY LIBVIRT HOSTDEV
        # ----------------------------------------------------
        stage = "libvirt hostdev"

        domain = f"one-{vm_id}"

        state = ssh_virsh(host, "domstate", domain)

        if state.lower() != "running":
            raise ProbeError(
                f"Libvirt domain {domain} state={state}"
            )

        xml = ssh_virsh(host, "dumpxml", domain)
        addresses = libvirt_pci_addresses(xml)

        if addresses != [vf]:
            raise ProbeError(
                f"Libvirt PCI sources={addresses}; "
                f"expected [{vf}]"
            )

        record(
            stage,
            "ok",
            f"Libvirt domain {domain} running; "
            f"physical hostdev={vf}",
        )

    except Exception as exc:
        record(stage, "failed", str(exc))

    finally:
        # ----------------------------------------------------
        # 6. CLEANUP AND VF RELEASE
        # ----------------------------------------------------
        if override_path:
            try:
                os.unlink(override_path)
            except OSError:
                pass

        if vm_id is not None:
            try:
                detail = cleanup_vm(
                    vm_id,
                    vm_name,
                    host_id,
                    vf,
                    config.cleanup_timeout,
                    config.poll_interval,
                )
                record("cleanup", "ok", detail)
            except Exception as exc:
                record("cleanup", "failed", str(exc))
        else:
            record(
                "cleanup",
                "skipped - no VM ID was created",
                "No known VM ID to terminate",
            )

    results = list(checks.values())

    status = (
        "ok"
        if all(item["status"] == "ok" for item in results)
        else "failed"
    )

    return {
        "status": status,
        "vm_id": vm_id,
        "host_id": host_id,
        "requested_vf": vf,
        "checks": results,
    }


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--template-id", required=True, type=int)
    parser.add_argument("--host-id", required=True, type=int)
    parser.add_argument("--host", required=True)
    parser.add_argument("--vf", required=True)
    parser.add_argument("--vm-name", required=True)

    parser.add_argument("--memory", type=int, default=1024)
    parser.add_argument("--vcpu", type=int, default=1)
    parser.add_argument("--deploy-timeout", type=int, default=300)
    parser.add_argument("--cleanup-timeout", type=int, default=180)
    parser.add_argument("--poll-interval", type=int, default=5)

    args = parser.parse_args()

    try:
        result = execute(args)
    except Exception as exc:
        result = {
            "status": "failed",
            "checks": [{
                "name": "probe",
                "status": "failed",
                "detail": f"Unexpected probe error: {exc}",
            }],
        }

    # stdout must contain only JSON for the Ansible role.
    print(json.dumps(result))


if __name__ == "__main__":
    main()
