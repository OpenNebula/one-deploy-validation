#!/usr/bin/python
"""Run one storage validation step on the OpenNebula frontend as oneadmin."""

import hashlib
import json
import re
import secrets
import shlex

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.odv_vm import (
    FatalProbeError, ProbeError, guest_command, guest_ip, run_command, vm_info,
    vm_running, wait_for, wait_guest, wait_held, wait_vm,
)


# virtio-blk exports its serial in sysfs even on a minimal guest without udev.
INVENTORY = """set -eu
test -d /sys/block
for disk in /sys/block/*; do
    test -b "/dev/${disk##*/}" || continue
    serial=''
    if test -r "$disk/serial"; then serial=$(cat "$disk/serial"); fi
    printf '%s %s\\n' "${disk##*/}" "$serial"
done"""


def rows(value):
    return [value] if isinstance(value, dict) else (value or [])


def resource_id(output):
    match = re.search(r'\bID:\s*(\d+)\s*$', output)
    if not match:
        raise ProbeError("OpenNebula did not return a resource ID: " + output)
    return match.group(1)


def inventory(ip):
    disks = {}
    for line in guest_command(ip, INVENTORY).splitlines():
        name, _, serial = line.partition(" ")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ProbeError("Unexpected guest block device name: " + name)
        disks[name] = serial
    if not disks:
        raise ProbeError("Guest returned an empty block device inventory")
    return disks


def serial_device(disks, serial):
    matches = [name for name, value in disks.items() if value == serial]
    if len(matches) > 1:
        raise FatalProbeError("Multiple guest devices have serial " + serial)
    return matches[0] if matches else None


def attached_disk(vm, image_id, serial):
    disks = rows(vm["TEMPLATE"].get("DISK"))
    matches = [d for d in disks if str(d.get("IMAGE_ID")) == image_id or d.get("SERIAL") == serial]
    if not matches:
        return None
    if (len(matches) != 1 or str(matches[0].get("IMAGE_ID")) != image_id
            or matches[0].get("SERIAL") != serial):
        raise FatalProbeError("Ambiguous disk identity in the OpenNebula VM template")
    return matches[0]


def owned_resource(kind, name):
    key = kind.upper()
    pool = json.loads(run_command(["one" + kind, "list", "-j"]))[key + "_POOL"]
    if pool in (None, ""):
        return None
    if not isinstance(pool, dict):
        raise ProbeError("Unexpected OpenNebula %s pool response" % kind)
    matches = [r for r in rows(pool.get(key)) if r["NAME"] == name
               and not (kind == "vm" and str(r["STATE"]) == "6")]
    if len(matches) > 1:
        raise FatalProbeError("Multiple OpenNebula resources named " + name)
    return matches[0] if matches else None


class DiskProbe:
    def __init__(self, params):
        self.params = params
        self.state = params["probe_state"]
        self.timeout = params["timeout"]
        self.interval = params["poll_interval"]
        self.name = params["name"]
        self.host = params["host_id"]

    def wait(self, predicate, description):
        return wait_for(predicate, self.timeout, self.interval, description)

    def vm_action(self, action, *args):
        run_command(["onevm", action, self.state["vm_id"], *args])

    def running(self):
        return wait_vm(self.state["vm_id"], self.timeout, self.interval, "running", self.host)

    def image_ready(self):
        def inspect():
            image = json.loads(run_command(["oneimage", "show", self.state["image_id"], "-j"]))["IMAGE"]
            if str(image["STATE"]) == "5":
                raise FatalProbeError("Test image entered ERROR")
            return str(image["STATE"]) == "1"
        self.wait(inspect, "test image to reach READY")

    def disk(self, vm):
        return attached_disk(vm, self.state["image_id"], self.state["serial"])

    def checksum(self, ip, device):
        # Read exactly the seeded sector. Hash on the frontend so a guest
        # pipeline cannot conceal a read error or supply its own expectation.
        data = bytes.fromhex(guest_command(ip, "od -An -v -N512 -tx1 /dev/%s" % device))
        if hashlib.sha256(data).hexdigest() != self.state["checksum"]:
            raise ProbeError("Data disk checksum differs from the seeded payload")

    def attach(self):
        p = self.params
        vm_id = resource_id(run_command(["onetemplate", "instantiate", p["template_id"], "--hold",
                                         "--nic", p["vnet"], "--name", self.name, "--ssh", "--net_context"]))
        self.state["vm_id"] = vm_id
        wait_held(vm_id, self.timeout, self.interval)
        self.vm_action("deploy", self.host, "--enforce")
        ip = guest_ip(self.running())
        self.state["boot_id"] = wait_guest(ip, self.timeout, self.interval)
        before = inventory(ip)
        image = resource_id(run_command(["oneimage", "create", "-d", p["datastore_id"], "--name",
                                         self.name + "-data", "--type", "DATABLOCK", "--size", "64",
                                         "--persistent"]))
        self.state.update(image_id=image, serial="odv" + secrets.token_hex(8))
        self.image_ready()
        serial = self.state["serial"]
        if serial in before.values():
            raise ProbeError("Guest already has the proposed test disk serial")
        run_command(["onevm", "disk-attach", vm_id],
                    stdin='DISK = [IMAGE_ID="%s", DEV_PREFIX="vd", SERIAL="%s"]\n' % (image, serial))

        def attached():
            vm = vm_info(vm_id)
            return vm_running(vm, self.host) and self.disk(vm)

        disk = self.wait(attached, "hot-attached image in VM template")
        self.state["disk_id"] = str(disk["DISK_ID"])
        device = self.wait(lambda: serial_device(inventory(ip), serial), "guest disk serial " + serial)
        if device in before:
            raise ProbeError("Refusing to seed pre-existing guest device " + device)
        payload = secrets.token_hex(256)
        # Check the serial again immediately before opening the device.
        guest_command(ip, 'set -eu; test "$(cat /sys/block/%s/serial)" = %s; '
                      'dd of=/dev/%s bs=512 2>/dev/null; sync' % (device, shlex.quote(serial), device),
                      stdin=payload)
        self.state["checksum"] = hashlib.sha256(payload.encode()).hexdigest()
        self.checksum(ip, device)

    def verify(self):
        vm = self.running()
        ip = guest_ip(vm)
        self.state["boot_id"] = wait_guest(ip, self.timeout, self.interval, self.state["boot_id"])
        disk = self.disk(vm)
        if not disk or str(disk["DISK_ID"]) != self.state["disk_id"]:
            raise ProbeError("Expected disk attachment is missing after recovery")
        serial = self.state["serial"]
        device = self.wait(lambda: serial_device(inventory(ip), serial), "recovered guest disk " + serial)
        self.checksum(ip, device)

    def reboot(self):
        self.vm_action("reboot")
        self.verify()

    def redeploy(self):
        self.vm_action("undeploy")
        self.wait(lambda: str(vm_info(self.state["vm_id"])["STATE"]) == "9", "VM to reach UNDEPLOYED")
        self.vm_action("deploy", self.host, "--enforce")
        self.verify()

    def poweroff(self):
        self.vm_action("poweroff")
        wait_vm(self.state["vm_id"], self.timeout, self.interval, "poweroff")

    def resume(self):
        self.vm_action("resume")
        self.verify()

    def detach(self):
        vm = self.running()
        disk = self.disk(vm)
        disk_id = self.state["disk_id"]
        if not disk or str(disk["DISK_ID"]) != disk_id:
            raise ProbeError("Cannot identify the disk to detach")
        previous_error = vm.get("USER_TEMPLATE", {}).get("ERROR", "")
        self.vm_action("disk-detach", disk_id)

        def detached():
            current = vm_info(self.state["vm_id"])
            if not vm_running(current, self.host):
                return False
            # OpenNebula returns to RUNNING after either result. A failed
            # driver action leaves a new DETACHDISK message on the VM.
            error = current.get("USER_TEMPLATE", {}).get("ERROR", "")
            if error != previous_error and "DETACHDISK:" in error:
                raise FatalProbeError(
                    "VM %s disk %s: %s" % (self.state["vm_id"], disk_id, error)
                )
            disk_ids = [str(d["DISK_ID"]) for d in rows(current["TEMPLATE"].get("DISK"))]
            return disk_id not in disk_ids and not self.disk(current)

        self.wait(detached, "hot-detached disk to disappear from the VM template")
        self.wait(lambda: not serial_device(inventory(guest_ip(vm)), self.state["serial"]),
                  "hot-detached disk to disappear from the guest")
        self.image_ready()

    def cleanup_vm(self):
        vm = owned_resource("vm", self.name)
        if vm:
            try:
                run_command(["onevm", "terminate", "--hard", str(vm["ID"])])
            except ProbeError:
                run_command(["onevm", "recover", "--delete", str(vm["ID"])])
            self.wait(lambda: not owned_resource("vm", self.name), "VM termination")

    def cleanup_image(self):
        name = self.name + "-data"

        def released():
            image = owned_resource("image", name)
            return image is None or str(image["STATE"]) in {"1", "3", "5"}  # READY, DISABLED, ERROR

        self.wait(released, "image release after VM termination")
        image = owned_resource("image", name)
        if image:
            run_command(["oneimage", "delete", str(image["ID"])])
            self.wait(lambda: not owned_resource("image", name), "image deletion")


def main():
    module = AnsibleModule(argument_spec=dict(
        operation=dict(required=True, choices=["attach", "reboot", "redeploy", "poweroff", "resume",
                                               "detach", "cleanup_vm", "cleanup_image"]),
        name=dict(required=True), host_id=dict(type="str"), template_id=dict(type="str"),
        datastore_id=dict(type="str"), vnet=dict(type="str"), probe_state=dict(type="dict", default={}),
        timeout=dict(type="int", default=300), poll_interval=dict(type="int", default=5),
    ))
    probe = DiskProbe(module.params)
    try:
        if probe.timeout <= 0 or probe.interval <= 0:
            raise ProbeError("timeout and poll_interval must be positive seconds")
        getattr(probe, module.params["operation"])()
    except (ProbeError, ValueError, KeyError, TypeError) as exc:
        module.fail_json(msg=str(exc))
    module.exit_json(changed=True, probe_state=probe.state)


if __name__ == "__main__":
    main()
