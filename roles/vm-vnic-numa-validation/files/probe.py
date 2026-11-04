#!/usr/bin/env python3
"""Runtime VM vNIC NUMA-locality probe for OpenNebula/KVM/OVS-DPDK.

Read-only. It correlates libvirt vCPU/memory placement with vhost-user PMD
placement, OVS physical DPDK uplinks/bonds, and SR-IOV VF/PF PCI topology.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SEVERITY = {"ok": 0, "unknown": 1, "degraded": 2, "failed": 3}


def worst(*values: str) -> str:
    vals = [v for v in values if v in SEVERITY]
    return max(vals, key=lambda x: SEVERITY[x]) if vals else "unknown"


def run(args: list[str]) -> dict:
    try:
        p = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return {"rc": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}
    except Exception as exc:  # pragma: no cover - defensive runtime path
        return {"rc": 127, "stdout": "", "stderr": str(exc)}


def clean_ovs(value: str) -> str:
    value = (value or "").strip()
    if value == "[]":
        return ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_range_list(value: str | None) -> set[int]:
    out: set[int] = set()
    if value is None:
        return out
    for token in str(value).strip().split(","):
        token = token.strip()
        if not token or token in {"-", "N/A"}:
            continue
        if re.fullmatch(r"\d+", token):
            out.add(int(token))
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            first, last = int(m.group(1)), int(m.group(2))
            if last >= first:
                out.update(range(first, last + 1))
    return out


def normalize_bdf(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"(?:(?P<dom>[0-9a-fA-F]{4}):)?(?P<bus>[0-9a-fA-F]{2}):(?P<slot>[0-9a-fA-F]{2})\.(?P<fn>[0-7])", value)
    if not m:
        return None
    dom = m.group("dom") or "0000"
    return f"{dom.lower()}:{m.group('bus').lower()}:{m.group('slot').lower()}.{m.group('fn')}"


def pci_numa(bdf: str | None) -> int | None:
    if not bdf:
        return None
    p = Path("/sys/bus/pci/devices") / bdf / "numa_node"
    try:
        value = int(p.read_text().strip())
        return value if value >= 0 else None
    except Exception:
        return None


def pci_physfn(bdf: str | None) -> str | None:
    if not bdf:
        return None
    p = Path("/sys/bus/pci/devices") / bdf / "physfn"
    try:
        if not p.exists():
            return None
        return Path(os.path.realpath(p)).name
    except Exception:
        return None


def cpu_to_numa_map() -> dict[int, int]:
    mapping: dict[int, int] = {}
    for node_path in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
        m = re.fullmatch(r"node(\d+)", node_path.name)
        if not m:
            continue
        node = int(m.group(1))
        try:
            cpus = parse_range_list((node_path / "cpulist").read_text())
        except Exception:
            cpus = set()
        for cpu in cpus:
            mapping[cpu] = node
    return mapping


def placement_from_runtime(domain: str) -> dict:
    findings: list[str] = []
    cpu_map = cpu_to_numa_map()

    vcpupin = run(["virsh", "vcpupin", domain])
    cpu_affinity: dict[str, list[int]] = {}
    cpu_nodes: set[int] = set()
    if vcpupin["rc"] == 0:
        for line in vcpupin["stdout"].splitlines():
            m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
            if not m:
                continue
            vcpu = m.group(1)
            cpus = parse_range_list(m.group(2))
            if not cpus:
                continue
            cpu_affinity[vcpu] = sorted(cpus)
            cpu_nodes.update(cpu_map[cpu] for cpu in cpus if cpu in cpu_map)
    else:
        findings.append("virsh vcpupin failed: " + (vcpupin["stderr"] or "unknown error"))

    numatune = run(["virsh", "numatune", domain])
    memory_mode = ""
    memory_nodes: set[int] = set()
    if numatune["rc"] == 0:
        for line in numatune["stdout"].splitlines():
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            if key == "numa_mode":
                memory_mode = value
            elif key == "numa_nodeset":
                memory_nodes = parse_range_list(value)
    else:
        findings.append("virsh numatune failed: " + (numatune["stderr"] or "unknown error"))

    target_node: int | None = None
    if len(cpu_nodes) == 1 and len(memory_nodes) == 1 and cpu_nodes == memory_nodes:
        result = "ok"
        target_node = next(iter(cpu_nodes))
    elif cpu_nodes and memory_nodes:
        if cpu_nodes.isdisjoint(memory_nodes):
            result = "failed"
            findings.append(
                f"VM CPU NUMA nodes {sorted(cpu_nodes)} are disjoint from memory NUMA nodes {sorted(memory_nodes)}"
            )
        else:
            result = "degraded"
            findings.append(
                f"VM placement spans NUMA nodes: CPU={sorted(cpu_nodes)} memory={sorted(memory_nodes)}"
            )
            if len(memory_nodes) == 1 and next(iter(memory_nodes)) in cpu_nodes:
                target_node = next(iter(memory_nodes))
    else:
        result = "unknown"
        findings.append(
            "VM NUMA locality could not be resolved from both vCPU affinity and memory policy"
        )
        if len(memory_nodes) == 1:
            target_node = next(iter(memory_nodes))
        elif len(cpu_nodes) == 1:
            target_node = next(iter(cpu_nodes))

    return {
        "result": result,
        "cpu_nodes": sorted(cpu_nodes),
        "memory_nodes": sorted(memory_nodes),
        "memory_mode": memory_mode,
        "target_node": target_node,
        "vcpu_affinity": cpu_affinity,
        "findings": findings,
    }


def parse_pmd_rxq(text: str) -> list[dict]:
    rows: list[dict] = []
    current_numa = None
    current_core = None
    for line in (text or "").splitlines():
        m = re.search(r"pmd thread numa_id\s+(-?\d+)\s+core_id\s+(\d+):", line)
        if m:
            current_numa = int(m.group(1))
            current_core = int(m.group(2))
            continue
        m = re.search(r"port:\s+(\S+)\s+queue-id:\s*(\d+)\s*(?:\((enabled|disabled)\))?", line)
        if m and current_core is not None and m.group(3) != "disabled":
            rows.append(
                {
                    "port": m.group(1),
                    "queue": int(m.group(2)),
                    "core": current_core,
                    "numa": current_numa if current_numa is not None and current_numa >= 0 else None,
                }
            )
    return rows


def evaluate_nodes(target_node: int | None, nodes: list[int | None]) -> tuple[str, list[str]]:
    if target_node is None or not nodes:
        return "unknown", ["NUMA locality cannot be evaluated because the VM or device NUMA node is unknown"]
    known = [n for n in nodes if n is not None]
    local = any(n == target_node for n in known)
    remote = any(n != target_node for n in known)
    if local and remote:
        return "degraded", [f"local and remote NUMA paths are both eligible relative to VM NUMA node {target_node}"]
    # Missing data must not turn a partly observed set into an all-local or
    # all-remote result. A known mix is degraded regardless of unknown paths.
    if len(known) != len(nodes):
        return "unknown", ["one or more eligible device NUMA nodes are unknown"]
    if remote:
        return "failed", [f"all eligible NUMA paths are remote from VM NUMA node {target_node}"]
    return "ok", []


def evaluate_balanced_bond(target_node: int | None, members: list[dict]) -> tuple[str, list[str]]:
    eligible = [m for m in members if m.get("eligible", True)]
    if not eligible:
        return "unknown", ["balanced bond has no eligible members"]
    result, findings = evaluate_nodes(target_node, [m.get("numa_node") for m in eligible])
    if result == "degraded":
        findings = ["balanced bond has both local and remote eligible members"] + findings
    elif result == "failed":
        findings = ["all eligible balanced-bond members are remote"] + findings
    return result, findings


def evaluate_active_backup(target_node: int | None, members: list[dict], active_member: str | None) -> tuple[str, list[str]]:
    if target_node is None:
        return "unknown", ["VM NUMA node is unknown"]
    active = next((m for m in members if m.get("name") == active_member), None)
    if active is None:
        return "unknown", ["active-backup active member could not be identified"]
    active_node = active.get("numa_node")
    if active_node is None:
        return "unknown", ["active-backup active member NUMA node is unknown"]
    if active_node != target_node:
        return "failed", [f"active-backup active member {active_member} is remote from VM NUMA node {target_node}"]
    standbys = [m for m in members if m.get("name") != active_member and m.get("eligible", True)]
    remote_standby = [m["name"] for m in standbys if m.get("numa_node") is not None and m["numa_node"] != target_node]
    if remote_standby:
        return "degraded", ["active member is local but remote standby member(s) exist: " + ",".join(remote_standby)]
    if any(m.get("numa_node") is None for m in standbys):
        return "unknown", ["active member is local but one or more eligible standby NUMA nodes are unknown"]
    return "ok", []


def parse_bond_show(text: str) -> dict:
    out = {"mode": "", "active_member": None, "members": {}}
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("bond_mode:"):
            out["mode"] = line.split(":", 1)[1].strip()
            continue
        m = re.match(r"member\s+(\S+):\s*(\S+)", line)
        if m:
            current = m.group(1)
            out["members"].setdefault(current, {})["state"] = m.group(2)
            continue
        if current and line == "active member":
            out["active_member"] = current
            out["members"].setdefault(current, {})["active"] = True
            continue
        if current and line.startswith("may_enable:"):
            out["members"].setdefault(current, {})["may_enable"] = (
                line.split(":", 1)[1].strip().lower() == "true"
            )
    return out


def port_interface_names(port: str) -> list[str]:
    result = run(["ovs-vsctl", "--if-exists", "get", "Port", port, "interfaces"])
    if result["rc"] != 0 or not result["stdout"]:
        return []
    identifiers = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", result["stdout"])
    names: list[str] = []
    for ident in identifiers:
        name = run(["ovs-vsctl", "--if-exists", "get", "Interface", ident, "name"])
        if name["rc"] == 0 and clean_ovs(name["stdout"]):
            names.append(clean_ovs(name["stdout"]))
    if names:
        return names
    # A one-interface Port can be addressed by the same name in most OVS setups.
    name = run(["ovs-vsctl", "--if-exists", "get", "Interface", port, "name"])
    if name["rc"] == 0 and clean_ovs(name["stdout"]):
        return [clean_ovs(name["stdout"])]
    return []


def dpdk_interface(name: str) -> dict | None:
    itype = run(["ovs-vsctl", "--if-exists", "get", "Interface", name, "type"])
    if itype["rc"] != 0 or clean_ovs(itype["stdout"]) != "dpdk":
        return None
    devargs = run(["ovs-vsctl", "--if-exists", "get", "Interface", name, "options:dpdk-devargs"])
    raw = clean_ovs(devargs["stdout"]) if devargs["rc"] == 0 else ""
    bdf = normalize_bdf(raw)
    return {
        "name": name,
        "pci": bdf,
        "devargs": raw,
        "numa_node": pci_numa(bdf),
    }


def evaluate_uplink_pmds(
    target_node: int | None, members: list[dict], pmd_rows: list[dict],
    mode: str = "direct", active_member: str | None = None,
) -> tuple[str, list[str]]:
    """Check physical DPDK RX PMDs versus both VM and PCI locality.

    Enrich each member with its observed PMD queues; only eligible members
    contribute to the resulting datapath verdict.
    """
    eligible_paths: list[tuple[str, str]] = []
    findings: list[str] = []
    for member in members:
        queues = [row for row in pmd_rows if row["port"] == member["name"]]
        nodes = [row.get("numa") for row in queues]
        vm_result, vm_findings = evaluate_nodes(target_node, nodes)
        pci_result, pci_findings = evaluate_nodes(member.get("numa_node"), nodes)
        nic_result, _ = evaluate_nodes(target_node, [member.get("numa_node")])
        path_result = worst(vm_result, pci_result, nic_result)
        member["pmd"] = {
            "queues": queues,
            "cores": sorted({row["core"] for row in queues}),
            "numa_nodes": sorted({node for node in nodes if node is not None}),
            "vm_result": vm_result,
            "pci_result": pci_result,
            "result": path_result,
            "findings": vm_findings + pci_findings,
        }
        if not member.get("eligible", True):
            continue
        eligible_paths.append((member["name"], path_result))
        if path_result != "ok":
            findings.append(f"physical uplink {member['name']} path locality: {path_result}")
        if pci_result != "ok":
            findings.extend(f"physical uplink {member['name']} PCI-to-PMD: {item}" for item in pci_findings)
        if vm_result != "ok":
            findings.extend(f"physical uplink {member['name']} RX PMD: {item}" for item in vm_findings)
    if not eligible_paths:
        return "unknown", ["physical uplink PMD locality has no eligible members"]
    statuses = [status for _, status in eligible_paths]
    if mode == "active-backup":
        active = next((status for name, status in eligible_paths if name == active_member), None)
        if active is None:
            return "unknown", findings + ["active-backup PMD locality: active member is unknown"]
        if active in {"failed", "degraded", "unknown"}:
            return active, findings
        standby = [status for name, status in eligible_paths if name != active_member]
        if any(status in {"failed", "degraded"} for status in standby):
            return "degraded", findings
        if "unknown" in standby:
            return "unknown", findings
        return "ok", findings
    # For balance-slb/tcp, a single bad member does not force FAILED when
    # another has a fully local path. Two cross-wired paths can both fail even
    # though PCI and PMD NUMA sets each contain a local node.
    if "degraded" in statuses or ("ok" in statuses and "failed" in statuses):
        return "degraded", findings
    if "unknown" in statuses:
        return "unknown", findings
    if "failed" in statuses:
        return "failed", findings
    return "ok", findings


def uplinks_for_bridge(bridge: str, target: str, target_node: int | None, pmd_rows: list[dict]) -> list[dict]:
    ports_result = run(["ovs-vsctl", "list-ports", bridge])
    if ports_result["rc"] != 0:
        return []
    uplinks: list[dict] = []
    for port in ports_result["stdout"].splitlines():
        port = port.strip()
        if not port or port == target:
            continue
        ifaces = port_interface_names(port)
        members = [x for x in (dpdk_interface(name) for name in ifaces) if x]
        if not members:
            continue
        if len(members) == 1:
            member = members[0]
            pci_result, pci_findings = evaluate_nodes(target_node, [member.get("numa_node")])
            pmd_result, pmd_findings = evaluate_uplink_pmds(target_node, [member], pmd_rows)
            result = worst(pci_result, pmd_result)
            findings = pci_findings + pmd_findings
            uplinks.append(
                {
                    "type": "direct",
                    "port": port,
                    "members": [{**member, "eligible": True}],
                    "result": result,
                    "findings": findings,
                }
            )
            continue

        bond_raw = run(["ovs-appctl", "bond/show", port])
        parsed = parse_bond_show(bond_raw["stdout"] if bond_raw["rc"] == 0 else "")
        for member in members:
            runtime = parsed["members"].get(member["name"], {})
            member["state"] = runtime.get("state", "unknown")
            member["active"] = bool(runtime.get("active", False))
            member["eligible"] = bool(runtime.get("may_enable", True)) and runtime.get("state", "enabled") == "enabled"

        mode = parsed.get("mode") or clean_ovs(
            run(["ovs-vsctl", "--if-exists", "get", "Port", port, "bond_mode"])["stdout"]
        )
        if mode in {"balance-slb", "balance-tcp"}:
            result, findings = evaluate_balanced_bond(target_node, members)
        elif mode == "active-backup":
            result, findings = evaluate_active_backup(target_node, members, parsed.get("active_member"))
        else:
            result, findings = "unknown", [f"unsupported or unknown OVS bond mode: {mode or 'unknown'}"]
        pmd_result, pmd_findings = evaluate_uplink_pmds(
            target_node, members, pmd_rows, mode=mode, active_member=parsed.get("active_member")
        )
        result = pmd_result if mode in {"balance-slb", "balance-tcp", "active-backup"} else "unknown"
        findings += pmd_findings
        uplinks.append(
            {
                "type": "bond",
                "port": port,
                "mode": mode or "unknown",
                "active_member": parsed.get("active_member"),
                "members": members,
                "result": result,
                "findings": findings,
            }
        )
    return uplinks


def vhost_interfaces(root: ET.Element, domain: str, target_node: int | None, expected_targets: set[str]) -> list[dict]:
    pmd_result = run(["ovs-appctl", "dpif-netdev/pmd-rxq-show"])
    pmd_rows = parse_pmd_rxq(pmd_result["stdout"] if pmd_result["rc"] == 0 else "")
    out: list[dict] = []
    for iface in root.findall("./devices/interface[@type='vhostuser']"):
        target_el = iface.find("target")
        source_el = iface.find("source")
        target = target_el.get("dev") if target_el is not None else ""
        if expected_targets and target not in expected_targets:
            continue
        socket = source_el.get("path") if source_el is not None else ""
        bridge_result = run(["ovs-vsctl", "iface-to-br", target])
        bridge = bridge_result["stdout"].strip() if bridge_result["rc"] == 0 else ""
        itype = clean_ovs(run(["ovs-vsctl", "--if-exists", "get", "Interface", target, "type"])["stdout"])
        options = clean_ovs(run(["ovs-vsctl", "--if-exists", "get", "Interface", target, "options"])["stdout"])

        target_pmd = [row for row in pmd_rows if row["port"] == target]
        pmd_nodes = [row.get("numa") for row in target_pmd]
        pmd_result_name, pmd_findings = evaluate_nodes(target_node, pmd_nodes)
        uplinks = uplinks_for_bridge(bridge, target, target_node, pmd_rows) if bridge else []
        uplink_result = worst(*(u.get("result", "unknown") for u in uplinks)) if uplinks else "unknown"
        findings = list(pmd_findings)
        for uplink in uplinks:
            findings.extend(uplink.get("findings", []))
        result = worst(pmd_result_name, uplink_result)
        out.append(
            {
                "name": target or socket or "vhost-user",
                "kind": "vhost-user",
                "socket": socket,
                "ovs_type": itype,
                "ovs_options": options,
                "bridge": bridge,
                "pmd": {
                    "queues": target_pmd,
                    "cores": sorted({row["core"] for row in target_pmd}),
                    "numa_nodes": sorted({row["numa"] for row in target_pmd if row.get("numa") is not None}),
                    "result": pmd_result_name,
                    "findings": pmd_findings,
                },
                "uplinks": uplinks,
                "result": result,
                "findings": findings,
            }
        )
    return out


def address_to_bdf(addr: ET.Element | None) -> str | None:
    if addr is None:
        return None
    try:
        dom = int(addr.get("domain", "0"), 16)
        bus = int(addr.get("bus", "0"), 16)
        slot = int(addr.get("slot", "0"), 16)
        fn = int(addr.get("function", "0"), 16)
        return f"{dom:04x}:{bus:02x}:{slot:02x}.{fn:x}"
    except Exception:
        return None


def sriov_interfaces(root: ET.Element, target_node: int | None, expected_pci: set[str]) -> list[dict]:
    out: list[dict] = []
    for hostdev in root.findall("./devices/hostdev[@type='pci']"):
        source = hostdev.find("./source/address")
        vf = address_to_bdf(source)
        if not vf:
            continue
        if expected_pci and vf not in expected_pci:
            continue
        pf = pci_physfn(vf)
        vf_node = pci_numa(vf)
        pf_node = pci_numa(pf)
        findings: list[str] = []
        if pf is None:
            result = "unknown"
            findings.append(f"PCI device {vf} has no physfn link; SR-IOV PF could not be established")
        elif target_node is None or vf_node is None or pf_node is None:
            result = "unknown"
            findings.append("VF/PF NUMA locality cannot be fully evaluated")
        elif vf_node != pf_node:
            result = "failed"
            findings.append(f"VF {vf} is on NUMA {vf_node} but PF {pf} is on NUMA {pf_node}")
        elif vf_node != target_node:
            result = "failed"
            findings.append(f"VF/PF are on NUMA {vf_node}, remote from VM NUMA node {target_node}")
        else:
            result = "ok"
        out.append(
            {
                "name": f"pci:{vf}",
                "kind": "sriov",
                "vf_pci": vf,
                "vf_numa_node": vf_node,
                "pf_pci": pf,
                "pf_numa_node": pf_node,
                "result": result,
                "findings": findings,
            }
        )
    return out


def probe(vm_id: int, expected_targets: set[str], expected_pci: set[str]) -> dict:
    domain = f"one-{vm_id}"
    xml_result = run(["virsh", "dumpxml", domain])
    if xml_result["rc"] != 0:
        return {
            "vm_id": vm_id,
            "domain": domain,
            "result": "failed",
            "placement": {"result": "unknown", "findings": []},
            "interfaces": [],
            "findings": ["virsh dumpxml failed: " + (xml_result["stderr"] or "unknown error")],
        }
    try:
        root = ET.fromstring(xml_result["stdout"])
    except Exception as exc:
        return {
            "vm_id": vm_id,
            "domain": domain,
            "result": "failed",
            "placement": {"result": "unknown", "findings": []},
            "interfaces": [],
            "findings": [f"libvirt XML parse failed: {exc}"],
        }

    placement = placement_from_runtime(domain)
    target_node = placement.get("target_node")
    interfaces = vhost_interfaces(root, domain, target_node, expected_targets)
    interfaces += sriov_interfaces(root, target_node, expected_pci)

    findings = list(placement.get("findings", []))
    if not interfaces:
        findings.append("no matching vhost-user or SR-IOV interfaces were found in libvirt")
        interface_result = "unknown"
    else:
        interface_result = worst(*(i.get("result", "unknown") for i in interfaces))
        for iface in interfaces:
            findings.extend(iface.get("findings", []))

    return {
        "vm_id": vm_id,
        "domain": domain,
        "result": worst(placement.get("result", "unknown"), interface_result),
        "placement": placement,
        "interfaces": interfaces,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-id", required=True, type=int)
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--expected-pci", action="append", default=[])
    args = parser.parse_args()
    expected_targets = {x for x in args.target if x}
    expected_pci = {normalize_bdf(x) for x in args.expected_pci if normalize_bdf(x)}
    result = probe(args.vm_id, expected_targets, expected_pci)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
