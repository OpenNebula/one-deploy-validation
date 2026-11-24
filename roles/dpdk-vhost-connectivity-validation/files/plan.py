#!/usr/bin/env python3
"""Read-only discovery for two-host OVS-DPDK connectivity validation."""
import json
import subprocess
import sys
from collections import defaultdict


def seq(raw):
    if raw is None or raw == "":
        return []
    return raw if isinstance(raw, list) else [raw]


def ids(raw):
    return {str(x) for x in seq(raw)}


def choose_hosts(hosts, inventory_hosts, requested, excluded):
    allowed = set(inventory_hosts) - set(excluded)
    online = [
        h for h in hosts
        if str(h.get("STATE")) == "2"
        and str(h.get("VM_MAD", "")).lower() == "kvm"
        and h.get("NAME") in allowed
    ]
    names = {h["NAME"]: h for h in online}

    if requested:
        if len(requested) != 2 or len(set(requested)) != 2:
            raise ValueError("Exactly two distinct hosts are required")

        absent = [h for h in requested if h not in names]
        if absent:
            raise ValueError(
                "Requested hosts are not online KVM inventory hosts: "
                + ", ".join(absent)
            )

        selected = [names[h] for h in requested]

        if len({str(h.get("CLUSTER_ID")) for h in selected}) != 1:
            raise ValueError("Hosts must belong to the same cluster")
    else:
        by_cluster = defaultdict(list)

        for h in sorted(
            online,
            key=lambda x: (str(x.get("CLUSTER_ID")), x["NAME"]),
        ):
            by_cluster[str(h.get("CLUSTER_ID"))].append(h)

        selected = next(
            (
                group[:2]
                for _, group in sorted(by_cluster.items())
                if len(group) >= 2
            ),
            [],
        )

    return [
        {
            "name": h["NAME"],
            "id": str(h["ID"]),
            "cluster_id": str(h["CLUSTER_ID"]),
        }
        for h in selected
    ]


def vnet_candidates(vnets, cluster_id, requested="", min_free=2):
    candidates = []

    for v in vnets:
        template = v.get("TEMPLATE") or {}

        if requested and v.get("NAME") != requested:
            continue

        if str(cluster_id) not in ids(
            (v.get("CLUSTERS") or {}).get("ID")
        ):
            continue

        if str(template.get("BRIDGE_TYPE", "")).lower() != "openvswitch_dpdk":
            continue

        if str(template.get("VN_MAD", "")).lower() != "ovswitch":
            continue

        # Trunk vNETs require guest-side VLAN configuration.
        # This validation tests ordinary access vNETs only.
        if str(template.get("VLAN_TAGGED_ID") or "").strip():
            continue

        bridge = str(template.get("BRIDGE", "")).strip()
        if not bridge:
            continue

        free = 0

        for ar in seq((v.get("AR_POOL") or {}).get("AR")):
            if "IP4" not in str(ar.get("TYPE", "")):
                continue

            free += max(
                0,
                int(ar.get("SIZE") or 0)
                - int(ar.get("USED_LEASES") or 0),
            )

        if free < min_free:
            continue

        candidates.append({
            "id": str(v.get("ID")),
            "name": v["NAME"],
            "bridge": bridge,
            "vlan_id": str(template.get("VLAN_ID") or ""),
            "vlan_tagged_id": str(
                template.get("VLAN_TAGGED_ID") or ""
            ),
            "free_ipv4": free,
        })

    candidates.sort(
        key=lambda x: int(x["id"]) if x["id"].isdigit() else 2**31
    )

    return candidates


def run_cli(*args):
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

    return json.loads(proc.stdout)


def main():
    try:
        config = json.load(sys.stdin)

        host_data = run_cli("onehost", "list", "-j")["HOST_POOL"]

        selected = choose_hosts(
            seq(host_data.get("HOST")),
            config["inventory_hosts"],
            config.get("hosts") or [],
            config.get("excluded_hosts") or [],
        )

        if len(selected) != 2:
            print(json.dumps({
                "status": "skipped",
                "reason": "fewer than two online KVM hosts in one cluster",
            }))
            return

        vnet_data = run_cli("onevnet", "list", "-j")["VNET_POOL"]
        detailed = []

        for v in seq(vnet_data.get("VNET")):
            try:
                detailed.append(
                    run_cli(
                        "onevnet", "show", str(v["ID"]), "-j"
                    )["VNET"]
                )
            except (KeyError, RuntimeError, ValueError):
                if (
                    config.get("vnet_name")
                    and config["vnet_name"] == v.get("NAME")
                ):
                    raise

        vnets = vnet_candidates(
            detailed,
            selected[0]["cluster_id"],
            config.get("vnet_name") or "",
        )

        if not vnets:
            print(json.dumps({
                "status": "skipped",
                "reason": "no eligible OVS-DPDK vNET with two free IPv4 leases",
                "hosts": selected,
            }))
            return

        ds_data = run_cli(
            "onedatastore", "list", "-j"
        )["DATASTORE_POOL"]

        datastores = []

        for datastore in seq(ds_data.get("DATASTORE")):
            if str(datastore.get("TYPE")) != "0":
                continue

            cluster_ids = ids(
                (datastore.get("CLUSTERS") or {}).get("ID")
            )

            if selected[0]["cluster_id"] in cluster_ids:
                datastores.append(str(datastore["ID"]))

        if not datastores:
            raise ValueError(
                "No image datastore assigned to selected cluster"
            )

        datastores.sort(
            key=lambda x: int(x) if x.isdigit() else 2**31
        )

        print(json.dumps({
            "status": "ok",
            "hosts": selected,
            "vnets": vnets,
            "cluster_id": selected[0]["cluster_id"],
            "image_ds_id": datastores[0],
        }))

    except (KeyError, ValueError, RuntimeError) as exc:
        print(json.dumps({
            "status": "failed",
            "reason": str(exc),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
