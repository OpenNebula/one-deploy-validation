"""Match service discovery responses to the observability inventory."""
from urllib.parse import urlsplit

from ansible.errors import AnsibleFilterError


def raft_leader(zone, frontends, required_active, excluded_leader=""):
    """Return the elected inventory host, or an empty string while RAFT converges."""
    servers = zone.get("ZONE", {}).get("SERVER_POOL", {}).get("SERVER", [])
    if isinstance(servers, dict):
        servers = [servers]

    states = {}
    for server in servers:
        matches = [
            fe["host"] for fe in frontends
            if server.get("NAME") in [fe["host"], *fe.get("aliases", [])]
        ]
        if len(matches) != 1 or matches[0] in states:
            return ""
        states[matches[0]] = str(server.get("STATE", ""))

    if set(states) != {fe["host"] for fe in frontends}:
        return ""
    # OpenNebula RAFT states: 2 = FOLLOWER, 3 = LEADER.
    if any(states.get(host) not in ("2", "3") for host in required_active):
        return ""
    leaders = [host for host, state in states.items() if state == "3"]
    if len(leaders) == 1 and leaders[0] != excluded_leader:
        return leaders[0]
    return ""


def endpoint_port(url):
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else 80


def alertmanager_hosts(endpoints, peers, source):
    """Resolve each discovered endpoint to exactly one prepared receiver."""
    destinations = []
    for endpoint in endpoints:
        url = urlsplit(endpoint["url"])
        matches = []
        for host, peer in peers.items():
            if url.hostname in ("localhost", "127.0.0.1", "::1") and host != source:
                continue
            configured_url = urlsplit(peer["cfg"]["alertmanager_url"])
            if url.hostname in peer["aliases"] and endpoint_port(url) == endpoint_port(configured_url):
                matches.append(host)
        if len(matches) != 1:
            raise AnsibleFilterError("An Alertmanager endpoint did not match exactly one prepared frontend")
        if matches[0] not in destinations:
            destinations.append(matches[0])
    return destinations


class FilterModule:
    def filters(self):
        return {
            "odv_raft_leader": raft_leader,
            "odv_alertmanager_hosts": alertmanager_hosts,
        }
