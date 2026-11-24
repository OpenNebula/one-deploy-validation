#!/usr/bin/env python3
"""Read-only libvirt, OVS and VLAN inspection."""
import argparse
import json
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET


def command(*args):
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode:
        raise RuntimeError(
            "%s: %s" % (" ".join(args), proc.stderr.strip())
        )

    return proc.stdout.strip()


def ovs_value(table, name, column):
    return command(
        "ovs-vsctl", "get", table, name, column
    ).strip().strip('"')


def verify(xml, expected_name, expected_bridge,
           expected_vlan, ovs, socket_ok):
    failures = []
    root = ET.fromstring(xml)

    iface = next(
        (
            item
            for item in root.findall("./devices/interface")
            if item.find("target") is not None
            and item.find("target").get("dev") == expected_name
        ),
        None,
    )

    if iface is None:
        return [
            "libvirt target interface %s not found"
            % expected_name
        ]

    source = iface.find("source")
    model = iface.find("model")

    path = (
        source.get("path", "")
        if source is not None
        else ""
    )

    if iface.get("type") != "vhostuser":
        failures.append("libvirt NIC is not vhostuser")

    if not path:
        failures.append("libvirt vhost-user socket path missing")

    if model is None or model.get("type") != "virtio":
        failures.append("libvirt NIC is not virtio")

    if source is None or source.get("mode") != "server":
        failures.append("libvirt NIC is not vhost-user server mode")

    if not socket_ok:
        failures.append(
            "QEMU vhost-user socket absent or not a UNIX socket"
        )

    if ovs.get("type") != "dpdkvhostuserclient":
        failures.append(
            "OVS Interface.type is not dpdkvhostuserclient"
        )

    if ovs.get("path") != path:
        failures.append("libvirt/OVS socket paths differ")

    if ovs.get("bridge") != expected_bridge:
        failures.append(
            "OVS port attached to unexpected bridge"
        )

    if ovs.get("datapath_type") != "netdev":
        failures.append("OVS bridge datapath is not netdev")

    if ovs.get("error") not in ("[]", ""):
        failures.append("OVS interface has an error")

    ofport = str(ovs.get("ofport", ""))

    if not ofport.isdigit() or int(ofport) <= 0:
        failures.append("OVS interface has invalid ofport")

    if expected_vlan:
        if ovs.get("tag") != expected_vlan:
            failures.append(
                "OVS Port.tag does not match vNET VLAN_ID"
            )

        if ovs.get("trunks") != "[]":
            failures.append(
                "OVS access port has unexpected trunks"
            )

        if ovs.get("vlan_mode") not in ("access", "[]", ""):
            failures.append(
                "OVS access port has unexpected vlan_mode"
            )

    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-id", type=int, required=True)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--vlan", default="")
    parser.add_argument("--check-vlan", action="store_true")
    args = parser.parse_args()

    name = "one-%d-0" % args.vm_id

    try:
        xml = command(
            "virsh", "-c", "qemu:///system",
            "dumpxml", "one-%d" % args.vm_id,
        )

        root = ET.fromstring(xml)

        target = next(
            (
                iface
                for iface in root.findall("./devices/interface")
                if iface.find("target") is not None
                and iface.find("target").get("dev") == name
            ),
            None,
        )

        path = ""

        if target is not None and target.find("source") is not None:
            path = target.find("source").get("path", "")

        socket_ok = bool(
            path
            and os.path.exists(path)
            and stat.S_ISSOCK(os.stat(path).st_mode)
        )

        data = {
            "type": ovs_value(
                "Interface", name, "type"
            ),
            "path": ovs_value(
                "Interface", name,
                "options:vhost-server-path"
            ),
            "error": ovs_value(
                "Interface", name, "error"
            ),
            "ofport": ovs_value(
                "Interface", name, "ofport"
            ),
            "bridge": command(
                "ovs-vsctl", "port-to-br", name
            ),
            "datapath_type": ovs_value(
                "Bridge", args.bridge, "datapath_type"
            ),
            "tag": ovs_value(
                "Port", name, "tag"
            ),
            "trunks": ovs_value(
                "Port", name, "trunks"
            ),
            "vlan_mode": ovs_value(
                "Port", name, "vlan_mode"
            ),
        }

        failures = verify(
            xml,
            name,
            args.bridge,
            args.vlan if args.check_vlan else "",
            data,
            socket_ok,
        )

        print(json.dumps({
            "status": "failed" if failures else "ok",
            "vm_id": args.vm_id,
            "interface": name,
            "socket": path,
            "socket_present": socket_ok,
            "ovs": data,
            "failures": failures,
        }))

        return 1 if failures else 0

    except (
        ET.ParseError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({
            "status": "failed",
            "vm_id": args.vm_id,
            "failures": [str(exc)],
        }))
        return 1


if __name__ == "__main__":
    sys.exit(main())
