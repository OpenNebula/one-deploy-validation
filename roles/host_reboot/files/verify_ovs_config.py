import ipaddress
import json
import os
import subprocess
import sys


def run_cmd(args):
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr

def normalize_ovs_val(raw: str) -> str:
    s = raw.strip()
    if s == "[]":
        return ""
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s

def normalize_expected_val(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, list):
        return "[" + ", ".join(normalize_expected_val(v) for v in val) + "]"
    return str(val)

def parse_ovs_set_items(raw: str):
    s = raw.strip()
    if s == "[]":
        return []
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [normalize_ovs_val(part) for part in inner.split(",")]
    return [normalize_ovs_val(s)]

def values_match(actual_raw: str, expected_val) -> bool:
    actual = normalize_ovs_val(actual_raw)
    if isinstance(expected_val, list):
        exp_list = [normalize_expected_val(v) for v in expected_val]
        act_list = parse_ovs_set_items(actual_raw)
        return sorted(act_list) == sorted(exp_list)
    expected = normalize_expected_val(expected_val)
    if actual == expected:
        return True
    if actual.startswith("[") and actual.endswith("]") and "," in expected:
        exp_list = [p.strip() for p in expected.split(",") if p.strip()]
        act_list = parse_ovs_set_items(actual_raw)
        return sorted(act_list) == sorted(exp_list)
    return False

def iter_set_pairs(set_spec):
    if isinstance(set_spec, dict):
        for k, v in set_spec.items():
            yield k, v
    elif isinstance(set_spec, list):
        for item in set_spec:
            if isinstance(item, dict):
                for k, v in item.items():
                    yield k, v

def parse_resolv_conf(text: str):
    nameservers = []
    search_domains = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            nameservers.append(parts[1])
        elif len(parts) >= 2 and parts[0] in ("search", "domain"):
            search_domains.extend(d.rstrip(".") for d in parts[1:])
    return nameservers, search_domains

def get_active_dns_and_search(br_name: str):
    nameservers = []
    search_domains = []

    rc, out, _ = run_cmd(["resolvectl", "status", br_name])
    if rc == 0 and out:
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("DNS Servers:") or stripped.startswith("Current DNS Server:"):
                vals = stripped.split(":", 1)[1].strip().split()
                nameservers.extend(vals)
            elif stripped.startswith("DNS Domain:"):
                vals = stripped.split(":", 1)[1].strip().split()
                search_domains.extend(d.lstrip("~").rstrip(".") for d in vals)

    for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    ns, sd = parse_resolv_conf(fh.read())
                for item in ns:
                    if item != "127.0.0.53" and item not in nameservers:
                        nameservers.append(item)
                for item in sd:
                    if item not in search_domains:
                        search_domains.append(item)
            except OSError:
                pass
            if nameservers or search_domains:
                break

    return nameservers, search_domains

def verify_ovs(expected_ovs: dict) -> dict:
    mismatches = []
    checked = 0

    def check_table_sets(table: str, record: str, set_spec, label: str):
        nonlocal checked
        for key, val in iter_set_pairs(set_spec):
            checked += 1
            rc, out, err = run_cmd(
                ["ovs-vsctl", "--if-exists", "get", table, record, str(key)]
            )
            if rc != 0:
                mismatches.append(f"{label} {key}: query failed ({err.strip()})")
                continue
            if not values_match(out, val):
                actual = normalize_ovs_val(out)
                actual_disp = actual if actual != "" else "<unset>"
                expected_disp = normalize_expected_val(val)
                mismatches.append(
                    f"{label} {key}: expected '{expected_disp}', got '{actual_disp}'"
                )

    def check_iface_ofport(iface_name: str, label: str):
        nonlocal checked
        checked += 1
        rc, out, _ = run_cmd(
            ["ovs-vsctl", "--if-exists", "get", "Interface", iface_name, "ofport"]
        )
        if rc == 0 and out.strip():
            try:
                ofport = int(normalize_ovs_val(out))
            except ValueError:
                ofport = None
            if ofport is not None and ofport < 0:
                _, err_out, _ = run_cmd(
                    ["ovs-vsctl", "--if-exists", "get", "Interface", iface_name, "error"]
                )
                err_msg = normalize_ovs_val(err_out)
                detail = f" ({err_msg})" if err_msg else ""
                mismatches.append(
                    f"{label}: interface failed to initialize in OVS datapath (ofport={ofport}){detail}"
                )

    check_table_sets("Open_vSwitch", ".", expected_ovs.get("set"), "Open_vSwitch")

    for br_name, br_cfg in (expected_ovs.get("br") or {}).items():
        br_cfg = br_cfg or {}
        checked += 1
        rc, _, _ = run_cmd(["ovs-vsctl", "br-exists", br_name])
        if rc != 0:
            mismatches.append(f"Bridge '{br_name}': does not exist")
            continue

        check_table_sets("Bridge", br_name, br_cfg.get("set"), f"Bridge '{br_name}'")

        expected_ports = br_cfg.get("ports") or []
        if expected_ports:
            rc, out, err = run_cmd(["ovs-vsctl", "list-ports", br_name])
            if rc != 0:
                mismatches.append(
                    f"Bridge '{br_name}': failed to list ports ({err.strip()})"
                )
            else:
                actual_ports = set(out.splitlines())
                for port in expected_ports:
                    checked += 1
                    if port not in actual_ports:
                        mismatches.append(
                            f"Bridge '{br_name}': missing port '{port}' (attached: {sorted(actual_ports)})"
                        )

        raw_addrs = br_cfg.get("addrs") or []
        addr_specs = []
        for entry in raw_addrs:
            if isinstance(entry, str) and entry.strip():
                addr_specs.append({"cidr": entry.strip()})
            elif isinstance(entry, dict) and entry.get("cidr"):
                addr_specs.append(entry)

        if (
            addr_specs
            or br_cfg.get("gw")
            or br_cfg.get("gw6")
            or br_cfg.get("dhcp4")
            or br_cfg.get("dhcp6")
        ):
            checked += 1
            rc_l, out_l, err_l = run_cmd(["ip", "-j", "link", "show", "dev", br_name])
            if rc_l != 0:
                mismatches.append(
                    f"Bridge '{br_name}': network device missing ({err_l.strip()})"
                )
            else:
                link_data = json.loads(out_l)
                flags = link_data[0].get("flags", []) if link_data else []
                if "UP" not in flags:
                    mismatches.append(
                        f"Bridge '{br_name}': interface is not UP (flags: {flags})"
                    )

        if addr_specs:
            checked += 1
            rc, out, err = run_cmd(["ip", "-j", "addr", "show", "dev", br_name])
            if rc != 0:
                mismatches.append(
                    f"Bridge '{br_name}': failed to inspect addresses ({err.strip()})"
                )
            else:
                addr_data = json.loads(out)
                actual_cidrs = {
                    f"{ainfo['local']}/{ainfo['prefixlen']}"
                    for iface_obj in addr_data
                    for ainfo in iface_obj.get("addr_info", [])
                    if "local" in ainfo and "prefixlen" in ainfo
                }

                metric_specs = [
                    spec for spec in addr_specs if spec.get("metric") is not None
                ]
                dev_routes = []
                if metric_specs:
                    rc_r, out_r, err_r = run_cmd(
                        ["ip", "-j", "route", "show", "dev", br_name]
                    )
                    if rc_r != 0:
                        mismatches.append(
                            f"Bridge '{br_name}': failed to inspect interface routes ({err_r.strip()})"
                        )
                    else:
                        dev_routes = json.loads(out_r)

                for addr_spec in addr_specs:
                    exp_cidr = str(addr_spec["cidr"]).strip()
                    if exp_cidr not in actual_cidrs:
                        mismatches.append(
                            f"Bridge '{br_name}': expected IP '{exp_cidr}', found {sorted(actual_cidrs)}"
                        )
                        continue

                    if addr_spec.get("metric") is not None:
                        checked += 1
                        exp_m = int(addr_spec["metric"])
                        iface_obj = ipaddress.ip_interface(exp_cidr)
                        ip_str = str(iface_obj.ip)
                        net_str = str(iface_obj.network)
                        subnet_routes = [
                            r
                            for r in dev_routes
                            if r.get("dst") != "default"
                            and (
                                r.get("prefsrc") == ip_str
                                or r.get("dst") in (net_str, ip_str)
                            )
                        ]
                        if not subnet_routes:
                            mismatches.append(
                                f"Bridge '{br_name}': no connected subnet route found for '{exp_cidr}'"
                            )
                        elif not any(
                            int(r.get("metric", 0)) == exp_m for r in subnet_routes
                        ):
                            actual_m = [r.get("metric", 0) for r in subnet_routes]
                            mismatches.append(
                                f"Bridge '{br_name}': expected subnet route metric {exp_m} for '{exp_cidr}', got {actual_m}"
                            )

        for gw_key, ip_ver_flag in (("gw", "-4"), ("gw6", "-6")):
            exp_gw = br_cfg.get(gw_key)
            if exp_gw:
                checked += 1
                gw_str = str(exp_gw).strip()
                rc, out, err = run_cmd(
                    ["ip", "-j", ip_ver_flag, "route", "show", "default"]
                )
                if rc != 0:
                    mismatches.append(
                        f"Bridge '{br_name}': failed to inspect default route ({err.strip()})"
                    )
                else:
                    routes = json.loads(out)
                    gw_ok = any(
                        r.get("gateway") == gw_str and r.get("dev") == br_name
                        for r in routes
                    )
                    if not gw_ok:
                        mismatches.append(
                            f"Bridge '{br_name}': expected default gateway '{gw_str}' via '{br_name}'"
                        )

        for route_spec in br_cfg.get("routes") or []:
            if not isinstance(route_spec, dict) or not route_spec.get("to"):
                continue
            checked += 1
            to_dst = str(route_spec["to"]).strip()
            via_gw = str(route_spec.get("via", "")).strip()
            ip_ver_flag = "-6" if ":" in to_dst or ":" in via_gw else "-4"
            rc, out, err = run_cmd(
                ["ip", "-j", ip_ver_flag, "route", "show", "dev", br_name]
            )
            if rc != 0:
                mismatches.append(
                    f"Bridge '{br_name}': failed to inspect routes ({err.strip()})"
                )
            else:
                routes = json.loads(out)
                matched = [
                    r
                    for r in routes
                    if r.get("dst") == to_dst
                    and (not via_gw or r.get("gateway") == via_gw)
                ]
                if not matched:
                    mismatches.append(
                        f"Bridge '{br_name}': missing route to '{to_dst}'"
                        + (f" via '{via_gw}'" if via_gw else "")
                    )
                elif route_spec.get("metric") is not None:
                    exp_rm = int(route_spec["metric"])
                    if not any(int(r.get("metric", 0)) == exp_rm for r in matched):
                        actual_rm = [r.get("metric", 0) for r in matched]
                        mismatches.append(
                            f"Bridge '{br_name}': route '{to_dst}' expected metric {exp_rm}, got {actual_rm}"
                        )

        raw_dns = br_cfg.get("dns")
        expected_dns = (
            [d.strip() for d in raw_dns.split() if d.strip()]
            if isinstance(raw_dns, str)
            else [str(d).strip() for d in (raw_dns or []) if str(d).strip()]
        )
        raw_search = br_cfg.get("search")
        expected_search = (
            [s.strip().rstrip(".") for s in raw_search.split() if s.strip()]
            if isinstance(raw_search, str)
            else [
                str(s).strip().rstrip(".") for s in (raw_search or []) if str(s).strip()
            ]
        )
        if expected_dns or expected_search:
            actual_dns, actual_search = get_active_dns_and_search(br_name)
            for dns_ip in expected_dns:
                checked += 1
                if dns_ip not in actual_dns:
                    mismatches.append(
                        f"Bridge '{br_name}': expected DNS '{dns_ip}', found {actual_dns}"
                    )
            for domain in expected_search:
                checked += 1
                if domain not in actual_search:
                    mismatches.append(
                        f"Bridge '{br_name}': expected search domain '{domain}', found {actual_search}"
                    )

    for bond_name, bond_cfg in (expected_ovs.get("bond") or {}).items():
        bond_cfg = bond_cfg or {}
        checked += 1
        rc, out, _ = run_cmd(
            ["ovs-vsctl", "--if-exists", "get", "Port", bond_name, "name"]
        )
        if rc != 0 or normalize_ovs_val(out) != bond_name:
            mismatches.append(f"Bond '{bond_name}': port does not exist")
            continue

        if bond_cfg.get("br"):
            checked += 1
            rc_b, out_b, _ = run_cmd(["ovs-vsctl", "port-to-br", bond_name])
            if rc_b != 0 or out_b.strip() != str(bond_cfg["br"]).strip():
                mismatches.append(
                    f"Bond '{bond_name}': expected bridge '{bond_cfg['br']}', got '{out_b.strip() or '<none>'}'"
                )

        check_table_sets("Port", bond_name, bond_cfg.get("set"), f"Bond '{bond_name}'")

        expected_ifaces = bond_cfg.get("ifaces") or []
        if expected_ifaces:
            checked += 1
            rc_p, out_p, _ = run_cmd(
                [
                    "ovs-vsctl",
                    "--format=json",
                    "--columns=name,interfaces",
                    "list",
                    "Port",
                    bond_name,
                ]
            )
            rc_i, out_i, _ = run_cmd(
                [
                    "ovs-vsctl",
                    "--format=json",
                    "--columns=_uuid,name",
                    "list",
                    "Interface",
                ]
            )
            if rc_p != 0 or rc_i != 0:
                mismatches.append(
                    f"Bond '{bond_name}': failed to query member interfaces"
                )
            else:
                uuid_to_name = {}
                for row in json.loads(out_i).get("data", []):
                    u = row[0][1] if isinstance(row[0], list) else row[0]
                    uuid_to_name[u] = row[1]
                port_rows = json.loads(out_p).get("data", [])
                actual_ifaces = []
                if port_rows:
                    raw_ifaces = port_rows[0][1]
                    if isinstance(raw_ifaces, list) and raw_ifaces[0] == "uuid":
                        actual_ifaces = [uuid_to_name.get(raw_ifaces[1], raw_ifaces[1])]
                    elif isinstance(raw_ifaces, list) and raw_ifaces[0] == "set":
                        actual_ifaces = [
                            uuid_to_name.get(entry[1], entry[1])
                            for entry in raw_ifaces[1]
                        ]
                if sorted(actual_ifaces) != sorted(expected_ifaces):
                    mismatches.append(
                        f"Bond '{bond_name}' interfaces: expected {expected_ifaces}, got {actual_ifaces}"
                    )

    for port_name, port_cfg in (expected_ovs.get("port") or {}).items():
        port_cfg = port_cfg or {}
        checked += 1
        rc, out, _ = run_cmd(
            ["ovs-vsctl", "--if-exists", "get", "Port", port_name, "name"]
        )
        if rc != 0 or normalize_ovs_val(out) != port_name:
            mismatches.append(f"Port '{port_name}': does not exist")
            continue
        if port_cfg.get("br"):
            checked += 1
            rc_b, out_b, _ = run_cmd(["ovs-vsctl", "port-to-br", port_name])
            if rc_b != 0 or out_b.strip() != str(port_cfg["br"]).strip():
                mismatches.append(
                    f"Port '{port_name}': expected bridge '{port_cfg['br']}', got '{out_b.strip() or '<none>'}'"
                )
        check_table_sets("Port", port_name, port_cfg.get("set"), f"Port '{port_name}'")

    for iface_name, iface_cfg in (expected_ovs.get("iface") or {}).items():
        iface_cfg = iface_cfg or {}
        checked += 1
        rc, out, _ = run_cmd(
            ["ovs-vsctl", "--if-exists", "get", "Interface", iface_name, "name"]
        )
        if rc != 0 or normalize_ovs_val(out) != iface_name:
            mismatches.append(f"Interface '{iface_name}': does not exist")
            continue
        check_iface_ofport(iface_name, f"Interface '{iface_name}'")
        check_table_sets(
            "Interface", iface_name, iface_cfg.get("set"), f"Interface '{iface_name}'"
        )

    return {
        "ok": len(mismatches) == 0,
        "checked": checked,
        "mismatches": mismatches,
    }


def main():
    raw = sys.stdin.read().strip()
    expected_ovs = json.loads(raw) if raw else {}
    result = verify_ovs(expected_ovs)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
