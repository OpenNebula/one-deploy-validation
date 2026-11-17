"""Read-only HA probe regressions, without SSH or service changes."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'roles/observability_validation/library/observability_probe.py'
spec = importlib.util.spec_from_file_location('ha_probe', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

filters = importlib.util.spec_from_file_location("topology", PATH.parents[1] / "filter_plugins/topology.py")
topology = importlib.util.module_from_spec(filters)
filters.loader.exec_module(topology)


def frontends():
    return [{'host': 'fe-%d' % i, 'aliases': ['fe-%d' % i, '192.0.2.%d' % (i + 1)]} for i in range(3)]


def probe(**options):
    config = {'host': 'fe-1', 'aliases': ['fe-1'], 'frontend_hosts': frontends(),
              'alertmanager_url': 'http://192.0.2.2:9093', 'attempts': 1}
    config.update(options)
    return module.Probe(config)


class HATests(unittest.TestCase):
    def identity(self, states, names=None, required_active=(), excluded_leader=''):
        servers = [{'NAME': name, 'STATE': str(state)} for name, state in
                   zip(names or ['fe-%d' % i for i in range(len(states))], states)]
        return topology.raft_leader({'ZONE': {'SERVER_POOL': {'SERVER': servers}}}, frontends(),
                                    required_active, excluded_leader)

    def test_raft_accepts_one_new_leader(self):
        result = self.identity([2, 3, 2], excluded_leader='fe-0', required_active=['fe-0','fe-1','fe-2'])
        self.assertEqual(result, 'fe-1')

    def test_raft_rejects_old_leader(self):
        self.assertFalse(self.identity([3, 2, 2], excluded_leader='fe-0'))

    def test_raft_rejects_missing_or_multiple_leaders(self):
        for states in ([2, 2, 2], [3, 3, 2]):
            self.assertFalse(self.identity(states))

    def test_raft_requires_recovered_members(self):
        self.assertFalse(self.identity([0, 3, 2], required_active=['fe-0']))

    def test_raft_rejects_membership_mismatch(self):
        self.assertFalse(self.identity([3, 2]))
        self.assertFalse(self.identity([3, 2, 2], names=['fe-0','fe-1','unknown']))
        self.assertFalse(self.identity([3, 2, 2, 2], names=['fe-0','fe-1','fe-2','fe-2']))

    def test_raft_maps_ip_aliases(self):
        self.assertTrue(self.identity([2, 3, 2], names=['192.0.2.1','192.0.2.2','192.0.2.3']))

    def monitor(self, stamp=100, previous=None, peers=True, targets=True):
        target = {'name': 'node', 'host': 'kvm-0', 'aliases': ['192.0.2.10'],
                  'job': 'node_exporter', 'port': 9100, 'metrics': ['node_uname_info']}
        p = probe(expected_targets=[target] if targets else [], previous_samples=previous or {},
                  require_progress=previous is not None)
        current = {'scrapeUrl': 'http://192.0.2.10:9100/metrics',
                   'labels': {'job': 'node_exporter', 'instance': '192.0.2.10:9100'}}
        status = {'cluster': {'status': 'ready', 'peers': [
            {'address': '192.0.2.1:9094'}, {'address': '192.0.2.3:9094'}] if peers else []}}
        sample = {'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': current['labels'], 'value': [9999, str(stamp)]}]}}
        with patch.object(p, 'prometheus'), patch.object(p, 'get', return_value=status), \
                patch.object(p, 'prom_get', return_value={'data': {'activeTargets': [current]}}), \
                patch.object(p, 'query', return_value=sample), \
                patch.object(p, 'addresses', side_effect=lambda aliases: set(aliases)):
            return p.execute('ha_monitoring')

    def test_frozen_samples_fail_despite_reachable_endpoints(self):
        baseline = self.monitor()['ha']['samples']
        self.assertEqual(self.monitor(previous=baseline)['results'][0]['value'], 'failed')
        self.assertEqual(self.monitor(stamp=101, previous=baseline)['results'][0]['value'], 'ok')

    def test_missing_peer_and_empty_targets_fail(self):
        self.assertEqual(self.monitor(peers=False)['results'][0]['value'], 'failed')
        self.assertEqual(self.monitor(targets=False)['results'][0]['value'], 'failed')

    def test_grafana_cannot_pass_without_auth(self):
        p = probe()
        with patch.object(p, 'grafana') as grafana:
            self.assertEqual(p.execute('ha_grafana')['results'][0]['value'], 'failed')
            grafana.assert_not_called()

    def test_grafana_uses_owners_credentials(self):
        p = probe()
        p.auth = {'grafana': {'token': 'owner-specific-token'}}
        def grafana():
            p.record('Grafana health', True)
            p.record('Grafana Prometheus datasource', True)
        with patch.object(p, 'grafana', side_effect=grafana):
            result = p.execute('ha_grafana')
            self.assertTrue(all(r['value'] == 'ok' for r in result['results']))


if __name__ == '__main__':
    unittest.main()
