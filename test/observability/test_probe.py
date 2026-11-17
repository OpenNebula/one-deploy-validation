"""Offline regression tests: no SSH, systemd changes or external API calls."""
import importlib.util
from pathlib import Path
import unittest
import yaml
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'roles/observability_validation/library/observability_probe.py'
spec = importlib.util.spec_from_file_location('observability_probe', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def vector(values):
    return {'status': 'success', 'data': {'resultType': 'vector',
            'result': [{'metric': {}, 'value': [1000, str(v)]} for v in values]}}


class ProbeTests(unittest.TestCase):
    def make(self, **kwargs):
        config = dict(host='fe-0', roles=['frontend'], version='7.4.1', attempts=1,
                      aliases=['192.0.2.1'])
        defaults = yaml.safe_load((PATH.parents[1] / 'defaults/main.yml').read_text())['observability_defaults']
        config = dict(defaults, **config)
        config.update(kwargs)
        return probe.Probe(config)

    def vm_fixture(self):
        return {'VM': {'ID': '42', 'STATE': '3', 'LCM_STATE': '3',
                       'DEPLOY_ID': '4c3cef04-1577-48d8-a332-f04f3e48880d', 'HISTORY_RECORDS': {'HISTORY': [
                           {'SEQ': '0', 'HID': '1'}, {'SEQ': '1', 'HID': '7'}]}}}

    def vm_vector(self, expression):
        metric = expression.split('{')[0]
        libvirt = metric.startswith('opennebula_libvirt')
        response = vector([0])  # Idle VM counters may legitimately be zero.
        response['data']['result'][0]['metric'] = {
            'one_vm_id': '42', 'instance': 'kvm:9926' if libvirt else 'fe:9925',
            'job': 'libvirt_exporter' if libvirt else 'opennebula_exporter'}
        if libvirt:
            response['data']['result'][0]['metric'].update(one_host_id='7', domain='one-42')
        return response

    def run_vm(self, query=None, vm=None):
        import json
        p = self.make(vm_id='42')
        with patch.object(probe, 'run_command', return_value=(0, json.dumps(vm or self.vm_fixture()))), \
                patch.object(p, 'query', side_effect=query or self.vm_vector):
            return p.execute('vm')['results'][0]

    def test_vm_metrics_and_identity_labels(self):
        self.assertEqual(self.run_vm()['value'], 'ok')

    def test_vm_id_zero_with_uuid_deploy_id(self):
        import json
        vm = self.vm_fixture()
        vm['VM']['ID'] = '0'
        p = self.make(vm_id=0)
        def query(expression):
            response = self.vm_vector(expression)
            labels = response['data']['result'][0]['metric']
            labels['one_vm_id'] = '0'
            if 'domain' in labels:
                labels['domain'] = 'one-0'
            return response
        with patch.object(probe, 'run_command', return_value=(0, json.dumps(vm))), \
                patch.object(p, 'query', side_effect=query):
            self.assertEqual(p.execute('vm')['results'][0]['value'], 'ok')

    def test_vm_missing_metrics_fails(self):
        self.assertEqual(self.run_vm(lambda q: vector([]))['value'], 'failed')

    def test_vm_wrong_labels_fail(self):
        for label, wrong in [('one_vm_id', '99'), ('one_host_id', '1'),
                             ('domain', 'one-99'), ('job', 'other'), ('instance', '')]:
            def query(q):
                response = self.vm_vector(q)
                if q.startswith('opennebula_libvirt'):
                    response['data']['result'][0]['metric'][label] = wrong
                return response
            with self.subTest(label=label):
                self.assertEqual(self.run_vm(query)['value'], 'failed')

    def test_vm_waits_for_running(self):
        vm = self.vm_fixture()
        vm['VM']['LCM_STATE'] = '1'
        self.assertEqual(self.run_vm(vm=vm)['value'], 'failed')

    def test_additional_ovs_preserves_default_exporters(self):
        p = self.make(roles=['node'], additional_exporters=['ovs', 'ovs'])
        with patch.object(p, 'exporter', return_value='ok'):
            result = p.execute('local')
        self.assertEqual({t['name'] for t in result['expected_targets']},
                         {'node', 'libvirt', 'lvm', 'ovs'})
        self.assertEqual(len(result['expected_targets']), 4)

    def test_smartctl_opt_in_adds_only_kvm_targets(self):
        for roles in (['frontend'], ['node']):
            p = self.make(roles=roles, additional_exporters=['smartctl'])
            with self.subTest(roles=roles), patch.object(p, 'exporter', return_value='ok'):
                targets = p.execute('local')['expected_targets']
                self.assertEqual('smartctl' in {t['name'] for t in targets}, roles == ['node'])

    def test_missing_exporter_still_expected(self):
        p = self.make()
        with patch.object(p, 'get', side_effect=ValueError('offline')):
            result = p.execute('local')
        self.assertEqual({e['name'] for e in result['expected_targets']}, {'node', 'opennebula', 'mysql'})
        self.assertTrue(any(r['value'] == 'failed' for r in result['results']))
        # FE SMART service exists but default patch_datasources only discovers node SMART.
        self.assertNotIn('smartctl', {e['name'] for e in result['expected_targets']})

    def test_local_exporter_checks_do_not_require_systemd(self):
        p = self.make(roles=['node'])
        metrics = 'node_uname_info 1\nnode_memory_MemTotal_bytes 1024\nopennebula_libvirt_daemon_up 1\n'
        with patch.object(probe, 'run_command', side_effect=AssertionError('No service checks expected')), \
                patch.object(p, 'get', return_value=metrics):
            result = p.execute('local')
        self.assertEqual({target['name'] for target in result['expected_targets']}, {'node', 'libvirt', 'lvm'})
        self.assertTrue(all(r['value'] == 'ok' for r in result['results']))

    def test_missing_target_is_failure_not_vacuous_success(self):
        p = self.make()
        p.target(dict(p.exporter_config('node'), host='fe-0', aliases=['192.0.2.1']), [])
        self.assertEqual(p.results[0]['value'], 'failed')

    def test_targets_match_identity_and_job(self):
        p = self.make()
        expected = dict(p.exporter_config('node'), host='fe-0', aliases=['192.0.2.1'])
        with patch.object(p, 'addresses', side_effect=lambda aliases: set(aliases)):
            self.assertTrue(p.match_target({'scrapeUrl': 'http://192.0.2.1:9100/metrics', 'labels': {'job': 'node_exporter'}}, expected))
            self.assertFalse(p.match_target({'scrapeUrl': 'http://192.0.2.2:9100/metrics', 'labels': {'job': 'node_exporter'}}, expected))
            self.assertFalse(p.match_target({'scrapeUrl': 'http://192.0.2.1:9100/metrics', 'labels': {'job': 'wrong'}}, expected))
            expected['host'] = 'other-host'
            self.assertFalse(p.match_target({'scrapeUrl': 'http://localhost:9100/metrics', 'labels': {'job': 'node_exporter'}}, expected))

    def test_target_staleness_and_errors(self):
        target = {'lastScrape': '2026-01-01T00:00:00Z', 'health': 'up', 'lastError': ''}
        now = 1767225600
        self.assertTrue(probe.fresh_target(target, 120, now + 30))
        self.assertTrue(probe.fresh_target(dict(target, lastScrape='2026-01-01T00:00:00.123456789Z'), 120, now + 30))
        self.assertFalse(probe.fresh_target(target, 120, now + 121))
        self.assertFalse(probe.fresh_target(dict(target, health='down'), 120, now))
        self.assertFalse(probe.fresh_target(dict(target, lastError='broken'), 120, now))
        self.assertFalse(probe.fresh_target(dict(target, lastScrape='garbage'), 120, now))

    def test_exporter_retries_missing_metric_and_records_one_success(self):
        p = self.make(attempts=3, delay=2)
        with patch.object(p, 'get', side_effect=[
                'go_goroutines 4\n', 'opennebula_host_total 3.0\n']) as get, \
                patch.object(probe.time, 'sleep') as sleep:
            p.check('opennebula exporter', lambda: p.exporter(p.exporter_config('opennebula')))
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(call.kwargs['attempts'] == 1 for call in get.call_args_list))
        sleep.assert_called_once_with(2)
        self.assertEqual([r['value'] for r in p.results], ['ok'])

    def test_exporter_persistent_missing_metric_fails_after_budget(self):
        p = self.make(attempts=3, delay=2)
        with patch.object(p, 'get', return_value='go_goroutines 4\n') as get, \
                patch.object(probe.time, 'sleep') as sleep:
            p.check('opennebula exporter', lambda: p.exporter(p.exporter_config('opennebula')))
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([r['value'] for r in p.results], ['failed'])
        self.assertIn('missing metrics=opennebula_host_total', p.results[0]['comment'])

    def test_exporter_transport_and_content_errors_share_retry_budget(self):
        p = self.make(attempts=3, delay=0)
        with patch.object(p, 'get', side_effect=[
                ValueError('HTTP request failed'), '', 'mysql_up 1\n']) as get, \
                patch.object(probe.time, 'sleep'):
            p.exporter(p.exporter_config('mysql'))
        self.assertEqual(get.call_count, 3)

    def test_exporter_retries_collector_failure(self):
        p = self.make(attempts=2, delay=0)
        with patch.object(p, 'get', side_effect=[
                'mysql_up 0\n', 'mysql_up 1\n']) as get, \
                patch.object(probe.time, 'sleep'):
            p.exporter(p.exporter_config('mysql'))
        self.assertEqual(get.call_count, 2)

    def test_database_http_ok_but_collector_down(self):
        p = self.make()
        with patch.object(p, 'get', return_value='mysql_up 0\nmysql_exporter_last_scrape_error 1\n'):
            with self.assertRaisesRegex(ValueError, 'mysql_up=\[0.0\]'):
                p.exporter(p.exporter_config('mysql'))

    def test_empty_libvirt_host_is_healthy(self):
        p = self.make()
        with patch.object(p, 'get', return_value='opennebula_libvirt_daemon_up 1\n'):
            self.assertIn('required metrics and collector health OK', p.exporter(p.exporter_config('libvirt')))

    def test_healthy_exporter_records_one_result(self):
        p = self.make()
        with patch.object(p, 'get', return_value='opennebula_libvirt_daemon_up 1\n'):
            p.check('libvirt exporter', lambda: p.exporter(p.exporter_config('libvirt')))
        self.assertEqual(len(p.results), 1)
        self.assertEqual(p.results[0]['value'], 'ok')

    def test_lvm_without_resources_is_healthy(self):
        p = self.make()
        with patch.object(p, 'get', return_value='go_goroutines 4\n'):
            self.assertIn('required metrics and collector health OK', p.exporter(p.exporter_config('lvm')))

    def test_no_grafana_auth_is_explicit_skip_or_failure(self):
        for required in [False, True]:
            p = self.make(require_grafana_auth=required)
            with patch.object(p, 'get', return_value={'database': 'ok'}):
                p.grafana()
            self.assertEqual(p.results[-1]['value'] == 'failed', required)
            self.assertEqual(p.results[-1]['value'].startswith('skipped'), not required)

    def test_grafana_datasource_and_dashboards(self):
        p = self.make()
        p.auth = {'grafana': {'token': 'do-not-leak'}}
        responses = [[{'name': 'prometheus', 'type': 'prometheus', 'uid': 'one', 'isDefault': True}],
                     vector([1, 1]), [{'title': t, 'uid': t} for t in ['OpenNebula', 'Hosts', 'Virtual Machine']]]
        for title in ['OpenNebula', 'Hosts', 'Virtual Machine']:
            responses.append({'dashboard': {'panels': [{'type': 'timeseries', 'targets': [{}],
                                                        'datasource': {'type': 'prometheus', 'uid': 'one'}}]}})
        with patch.object(p, 'get', side_effect=responses):
            p.grafana()
        self.assertTrue(all(r['value'] == 'ok' for r in p.results))
        self.assertNotIn('do-not-leak', str(p.results))

    def test_recording_rules_require_query_output(self):
        p = self.make()
        response = {'status': 'success', 'data': {'groups': [{'rules': [
            {'type': 'recording', 'name': 'host:cpu:usage:rate5m', 'health': 'ok', 'lastError': ''}]}]}}
        with patch.object(p, 'prom_get', return_value=response), patch.object(p, 'query', return_value=vector([0.2])):
            p.rules()
        self.assertTrue(any(row['key'].endswith('Prometheus recording rules') and row['value'] == 'ok'
                            for row in p.results))

    def test_missing_alerting_rules_fail_and_missing_recordings_are_na(self):
        p = self.make()
        response = {'status': 'success', 'data': {'groups': []}}
        with patch.object(p, 'prom_get', return_value=response):
            p.rules()
        self.assertEqual(p.results[0]['value'], 'failed')
        self.assertTrue(p.results[1]['value'].startswith('skipped'))

    def test_all_loaded_rules_are_checked_without_name_configuration(self):
        for bad in ('alert_health', 'recording_health', 'recording_output'):
            with self.subTest(bad=bad):
                p = self.make()
                rules = [
                    {'type': 'alerting', 'name': 'ArbitraryAlert', 'health': 'err' if bad == 'alert_health' else 'ok'},
                    {'type': 'recording', 'name': 'arbitrary:first', 'health': 'ok'},
                    {'type': 'recording', 'name': 'arbitrary:second', 'health': 'err' if bad == 'recording_health' else 'ok'},
                ]
                response = {'status': 'success', 'data': {'groups': [{'rules': rules}]}}
                def query(name):
                    return vector([] if bad == 'recording_output' and name == 'arbitrary:second' else [1])
                with patch.object(p, 'prom_get', return_value=response), patch.object(p, 'query', side_effect=query):
                    p.rules()
                self.assertEqual(p.results[0]['value'], 'failed' if bad == 'alert_health' else 'ok')
                self.assertEqual(p.results[1]['value'], 'ok' if bad == 'alert_health' else 'failed')

    def test_dashboard_datasource_uid_must_match(self):
        p = self.make(dashboards=['OpenNebula'])
        p.auth = {'grafana': {'token': 'token'}}
        responses = [[{'name': 'prometheus', 'type': 'prometheus', 'uid': 'expected'}],
                     vector([1]), [{'title': 'OpenNebula', 'uid': 'dash'}],
                     {'dashboard': {'panels': [{'type': 'timeseries', 'targets': [{}],
                                                'datasource': {'type': 'prometheus', 'uid': 'wrong'}}]}}]
        with patch.object(p, 'get', side_effect=responses):
            p.grafana()
        result = next(row for row in p.results if row['key'].endswith('Grafana dashboard / OpenNebula'))
        self.assertEqual(result['value'], 'failed')

    def test_bad_prometheus_vector_and_html(self):
        with self.assertRaises(ValueError):
            probe.vector_values({'status': 'error'})
        self.assertEqual(probe.samples('<html>Sign in</html>'), {})
        self.assertEqual(probe.samples('sample{label="hello world"} 1.2e3\n# HELP x y'), {'sample': [1200.0]})

    def test_datasource_variable_resolution(self):
        sources = [{'uid': 'one', 'name': 'prometheus', 'type': 'prometheus', 'isDefault': True}]
        var = {'name': 'DS_PROMETHEUS', 'type': 'datasource', 'query': 'prometheus'}
        dashboard = {'templating': {'list': [var]}}
        for ref in ['${DS_PROMETHEUS}', '$DS_PROMETHEUS', {'uid': '${DS_PROMETHEUS}'}]:
            self.assertEqual(probe.resolve_datasource(ref, dashboard, sources), 'one')
        sources.append({'uid': 'two', 'name': 'other', 'type': 'prometheus'})
        with self.assertRaisesRegex(ValueError, 'no saved selection'):
            probe.resolve_datasource('${DS_PROMETHEUS}', dashboard, sources)
        var['current'] = {'value': 'two'}
        self.assertEqual(probe.resolve_datasource('${DS_PROMETHEUS}', dashboard, sources), 'two')
        var['current'] = {'value': 'missing'}
        with self.assertRaises(ValueError):
            probe.resolve_datasource('${DS_PROMETHEUS}', dashboard, sources)

    def test_grafana_url_policy_and_variable_dashboard(self):
        for strict, expected in [(False, 'ok'), (True, 'failed')]:
            p = self.make(dashboards=['OpenNebula'], grafana_prometheus_url='http://172.20.0.26:9090',
                          grafana_require_datasource_url=strict)
            p.auth = {'grafana': {'token': 'token'}}
            responses = [[{'name': 'prometheus', 'type': 'prometheus',
                          'uid': 'one', 'url': 'http://fe-0:9090'}], vector([1, 0]),
                         [{'title': 'OpenNebula', 'uid': 'dash'}],
                         {'dashboard': {'templating': {'list': [{'name': 'DS_PROMETHEUS',
                           'type': 'datasource', 'query': 'prometheus'}]},
                           'panels': [{'type': 'timeseries', 'targets': [{}],
                                       'datasource': {'uid': '${DS_PROMETHEUS}'}}]}}]
            with patch.object(p, 'get', side_effect=responses):
                p.grafana()
            self.assertEqual(next(r['value'] for r in p.results if r['key'].endswith('Grafana Prometheus datasource')), expected)
            self.assertEqual(p.results[-1]['value'], 'ok')

    def test_query_empty_and_stale_are_failures(self):
        p = self.make()
        target = {'scrapeUrl': 'http://192.0.2.1:9100/metrics', 'health': 'up',
                  'lastScrape': '2026-01-01T00:00:00Z', 'lastError': '',
                  'labels': {'job': 'node_exporter', 'instance': '192.0.2.1:9100'}}
        expected = dict(p.exporter_config('node'), host='fe-0', aliases=['192.0.2.1'])
        with patch.object(p, 'match_target', return_value=True), patch.object(p, 'query', return_value=vector([])):
            p.target(expected, [target])
        self.assertEqual(p.results[-1]['value'], 'failed')

    def test_rules_errors_and_empty(self):
        for rules in [[], [{'health': 'err', 'lastError': 'failed evaluation'}]]:
            p = self.make()
            with patch.object(p, 'prom_get', return_value={'status': 'success', 'data': {'groups': [{'rules': rules}]}}):
                p.rules()
            self.assertEqual(next(r['value'] for r in p.results
                                  if r['key'].endswith('Prometheus alerting rules')), 'failed')


if __name__ == '__main__':
    unittest.main()
