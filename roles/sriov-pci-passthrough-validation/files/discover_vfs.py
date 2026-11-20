#!/usr/bin/env python3
"""Read-only SR-IOV VF eligibility discovery on a KVM host."""

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


PCI_RE = re.compile(
    r"^(?:(?P<domain>[0-9a-fA-F]{4}):)?"
    r"(?P<bus>[0-9a-fA-F]{2}):"
    r"(?P<slot>[0-9a-fA-F]{2})\."
    r"(?P<function>[0-7])$"
)


def canonical(value):
    match = PCI_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"Invalid PCI address: {value!r}")
    return (
        f"{match.group('domain') or '0000'}:"
        f"{match.group('bus')}:{match.group('slot')}."
        f"{match.group('function')}"
    ).lower()


def as_list(value):
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def read_int(path):
    return int(Path(path).read_text().strip())


def run(*args):
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def libvirt_attachments():
    """Return PCI addresses used by running libvirt domains."""
    attached = set()
    domains = run(
        "virsh", "-c", "qemu:///system",
        "list", "--name", "--state-running",
    ).splitlines()

    for domain in domains:
        if not domain.strip():
            continue

        root = ET.fromstring(
            run("virsh", "-c", "qemu:///system",
                "dumpxml", domain.strip())
        )

        for address in root.findall(
            "./devices/hostdev[@type='pci']/source/address"
        ):
            attached.add(
                canonical(
                    f"{int(address.get('domain'), 0):04x}:"
                    f"{int(address.get('bus'), 0):02x}:"
                    f"{int(address.get('slot'), 0):02x}."
                    f"{int(address.get('function'), 0):x}"
                )
            )

    return attached


def process_fds():
    """Inspect VFIO and IOMMU descriptors without changing them."""
    owners = defaultdict(list)
    incomplete = []

    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue

        try:
            fds = list((proc / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError:
            incomplete.append(proc.name)
            continue

        for fd in fds:
            try:
                target = os.readlink(fd)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                incomplete.append(proc.name)
                continue

            if target.startswith("/dev/vfio/") or (
                target.startswith("/dev/iommu")
            ):
                owners[target].append(proc.name)

    return owners, sorted(set(incomplete))


def check_vf(device, attachments, owners, incomplete):
    vf = canonical(device["SHORT_ADDRESS"])
    path = Path("/sys/bus/pci/devices") / vf
    reasons = []

    result = {
        "vf": vf,
        "pf": "",
        "iommu_group": "",
        "driver": "",
        "status": "skipped",
        "reasons": reasons,
    }

    if str(device.get("VMID", "")) != "-1":
        reasons.append(f"OpenNebula VMID={device.get('VMID')}")

    if not path.exists():
        reasons.append("VF absent from sysfs")
        return result

    physfn = path / "physfn"
    if not physfn.exists():
        reasons.append("No SR-IOV parent PF")
        return result

    pf = physfn.resolve().name
    result["pf"] = pf

    try:
        if read_int(
            Path("/sys/bus/pci/devices") / pf / "sriov_numvfs"
        ) <= 0:
            reasons.append("Parent PF has no enabled VFs")
    except (OSError, ValueError):
        reasons.append("Cannot read parent PF SR-IOV count")

    driver = path / "driver"
    if driver.exists():
        result["driver"] = driver.resolve().name

    if result["driver"] != "vfio-pci":
        reasons.append(
            f"Driver is {result['driver'] or 'unbound'}, "
            "not vfio-pci"
        )

    group_link = path / "iommu_group"
    if not group_link.exists():
        reasons.append("No IOMMU group")
    else:
        group_path = group_link.resolve()
        group = group_path.name
        result["iommu_group"] = group

        members = sorted(
            p.name for p in (group_path / "devices").iterdir()
        )

        if members != [vf]:
            reasons.append(
                "IOMMU group contains other devices: "
                + ",".join(members)
            )

        group_fd = f"/dev/vfio/{group}"

        if owners.get(group_fd):
            reasons.append(
                "VFIO group held by PIDs: "
                + ",".join(sorted(set(owners[group_fd])))
            )

    vfio_dev_dir = path / "vfio-dev"
    cdevs = []

    if vfio_dev_dir.exists():
        cdevs = [
            f"/dev/vfio/devices/{entry.name}"
            for entry in vfio_dev_dir.iterdir()
            if entry.name.startswith("vfio")
        ]

    for cdev in cdevs:
        if owners.get(cdev):
            reasons.append(
                f"{cdev} held by PIDs: "
                + ",".join(sorted(set(owners[cdev])))
            )

    if vf in attachments:
        reasons.append("VF attached to a running libvirt domain")

    # A generic iommufd descriptor cannot by itself identify the
    # particular PCI device. Be conservative when no per-device cdev
    # mapping is exposed.
    if not cdevs and owners.get("/dev/iommu"):
        reasons.append(
            "Active iommufd users cannot be attributed "
            "to individual VFs"
        )

    if incomplete:
        reasons.append(
            "Process descriptor inspection incomplete"
        )

    if not reasons:
        result["status"] = "eligible"

    return result


def main():
    data = json.load(sys.stdin)

    host = data["host"]
    host_id = str(data["host_id"])
    pci = as_list(data.get("pci"))

    output = {
        "host": host,
        "host_id": host_id,
        "selected": [],
        "vf_checks": [],
        "errors": [],
    }

    try:
        attachments = libvirt_attachments()
        owners, incomplete = process_fds()
    except Exception as exc:
        output["errors"].append(
            f"Ownership discovery failed: {exc}"
        )
        print(json.dumps(output))
        return 1

    for device in pci:
        if str(device.get("SRIOV", "")).lower() != "vf":
            continue

        try:
            output["vf_checks"].append(
                check_vf(device, attachments, owners, incomplete)
            )
        except Exception as exc:
            output["vf_checks"].append({
                "vf": str(device.get("SHORT_ADDRESS", "?")),
                "pf": "",
                "status": "skipped",
                "reasons": [f"Inspection error: {exc}"],
            })

    selected_pfs = set()

    for result in sorted(
        output["vf_checks"],
        key=lambda item: (
            item.get("pf", ""),
            item.get("vf", ""),
        ),
    ):
        if result["status"] != "eligible":
            continue

        pf = result["pf"]
        if pf in selected_pfs:
            continue

        selected_pfs.add(pf)
        result["status"] = "selected"

        output["selected"].append({
            "host": host,
            "host_id": host_id,
            "pf": pf,
            "vf": result["vf"],
        })

    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
