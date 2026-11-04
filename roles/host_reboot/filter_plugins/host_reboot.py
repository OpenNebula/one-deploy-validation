"""Pure helpers for the host reboot validation role."""

import json


def _host_object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict):
        return {}
    host = value.get("HOST", value)
    return host if isinstance(host, dict) else {}


def host_reboot_state(value):
    """Return the OpenNebula host state, or None for an invalid response."""
    host = _host_object(value)
    try:
        return int(host["STATE"])
    except (KeyError, TypeError, ValueError):
        return None


def host_reboot_candidates(cluster, host_pool, inventory_hostnames):
    """Return mapped monitored cluster hosts sorted by numeric OpenNebula ID."""
    if not isinstance(cluster, dict) or not isinstance(inventory_hostnames, dict):
        return []
    if isinstance(host_pool, str):
        try:
            host_pool = json.loads(host_pool)
        except (TypeError, ValueError):
            return []
    if not isinstance(host_pool, dict):
        return []
    pool = host_pool.get("HOST_POOL", {})
    if not isinstance(pool, dict):
        return []
    hosts = pool.get("HOST", [])
    if isinstance(hosts, dict):
        hosts = [hosts]
    if not isinstance(hosts, list):
        return []

    host_ids = cluster.get("host_ids") or []
    if not isinstance(host_ids, (list, tuple, set)):
        host_ids = [host_ids]
    cluster_ids = {str(host_id) for host_id in host_ids}
    candidates = []
    for host in hosts:
        if not isinstance(host, dict):
            continue
        try:
            host_id = int(host["ID"])
            state = int(host["STATE"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(host_id) not in cluster_ids or state != 2:
            continue
        template = host.get("TEMPLATE", {})
        hostname = template.get("HOSTNAME") if isinstance(template, dict) else None
        if not hostname:
            continue
        matches = [
            inventory_name
            for inventory_name, current_hostname in inventory_hostnames.items()
            if current_hostname == hostname
        ]
        if len(matches) != 1:
            continue
        candidates.append(
            {"id": host_id, "name": str(host.get("NAME", host_id)), "node": matches[0]}
        )
    return sorted(candidates, key=lambda candidate: candidate["id"])


class FilterModule:
    def filters(self):
        return {
            "host_reboot_state": host_reboot_state,
            "host_reboot_candidates": host_reboot_candidates,
        }
