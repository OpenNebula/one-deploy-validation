"""Presentation aggregation must retain failures across hosts."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[2] / 'playbooks/filter_plugins/observability_report.py'
spec = importlib.util.spec_from_file_location('observability_report', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReportTests(unittest.TestCase):
    def test_prometheus_and_alertmanager_precede_grafana(self):
        names = [
            'Grafana health', 'Alertmanager readiness', 'Prometheus readiness',
            'Grafana service recovery', 'Grafana dashboard / Hosts',
            'Grafana Prometheus datasource', 'Prometheus and Alertmanager recovery',
            'Alertmanager loaded configuration', 'Prometheus recording rules',
        ]
        raw = {'Observability / fe-0 / ' + name: 'ok' for name in names}
        grouped = module.observability_report(raw)
        sections = {section['id']: section['checks'] for section in grouped['sections']}
        self.assertEqual([key.split(' / ', 2)[2] for key, _ in sections['services']],
                         ['Prometheus readiness', 'Alertmanager readiness', 'Grafana readiness'])
        integration = [key.split(' / ', 2)[2] for key, _ in sections['integration']]
        self.assertEqual(integration, [
            'Prometheus recording rules', 'Alertmanager loaded configuration',
            'Prometheus and Alertmanager recovery', 'Grafana Prometheus datasource',
            'Grafana dashboard / Hosts', 'Grafana restart',
        ])
        self.assertEqual(len(raw), len(names))

    def test_grafana_restart_combines_stop_and_recovery(self):
        for recovery, expected in [('ok', 'ok'), ('failed', 'failed')]:
            raw = {'Observability / fe-0 / Grafana service stop-start': 'ok',
                   'Observability / fe-0 / Grafana service recovery': recovery}
            grouped = module.observability_report(raw)
            key = 'Observability / integration / Grafana restart'
            self.assertEqual(grouped['results'], {key: expected})
            self.assertEqual(grouped['labels'][key], 'Grafana restarts and integration checks pass')
            self.assertEqual(len(raw), 2)
            if recovery == 'failed':
                self.assertEqual(grouped['comments'][key], 'fe-0')

    def test_vm_scenario_label_describes_new_vm(self):
        key = 'Observability / integration / VM metrics present'
        grouped = module.observability_report({'Observability / fe-0 / VM metrics present': 'ok'})
        self.assertEqual(grouped['labels'][key], 'VM is instantiated and correct metrics collected')

    def test_host_failures_win_and_only_failed_hosts_are_noted(self):
        results = {'Observability / fe-0 / Grafana health': 'ok',
                   'Observability / fe-1 / Grafana health': 'failed'}
        grouped = module.observability_report(results, {k: 'details' for k in results})
        self.assertEqual(grouped['results'], {'Observability / services / Grafana readiness': 'failed'})
        self.assertEqual(grouped['comments'], {'Observability / services / Grafana readiness': 'fe-1'})
        self.assertEqual(len(results), 2)

    def test_success_has_no_notes_and_other_checks_are_unchanged(self):
        results = {'Observability / a / node exporter': 'ok',
                   'Observability / b / node exporter': 'ok', 'Core service': 'ok'}
        grouped = module.observability_report(results, {k: 'detail' for k in results})
        self.assertEqual(grouped['results'], {'Core service': 'ok', 'Observability / integration / opennebula-node-exporter metrics collection': 'ok'})
        self.assertEqual(grouped['comments'], {'Core service': 'detail'})

    def test_target_failures_retain_both_ends(self):
        results = {'Observability / fe-0 / kvm-0 / node target': 'ok',
                   'Observability / fe-1 / kvm-0 / node target': 'failed'}
        grouped = module.observability_report(results)
        self.assertEqual(grouped['comments']['Observability / integration / opennebula-node-exporter metrics collection'], 'fe-1 → kvm-0')

    def test_skipped_host_is_not_pass(self):
        results = {'Observability / a / node exporter': 'ok',
                   'Observability / b / node exporter': 'skipped — not available'}
        grouped = module.observability_report(results)
        self.assertTrue(grouped['results']['Observability / integration / opennebula-node-exporter metrics collection'].startswith('warning'))
        self.assertEqual(grouped['comments'], {'Observability / integration / opennebula-node-exporter metrics collection': 'b: skipped — not available'})

    def test_warning_includes_original_reason(self):
        key = 'Observability / fe-0 / Prometheus recording rules'
        grouped = module.observability_report({key: 'skipped — N/A'},
                                             {key: 'No recording rules configured'})
        self.assertEqual(grouped['comments']['Observability / integration / Prometheus recording rules'],
                         'fe-0: No recording rules configured')

    def test_explicit_warning_preserves_evidence(self):
        key = 'Observability / fe-0 / inventory topology'
        grouped = module.observability_report({key: 'warning'}, {key: 'Partial inventory'})
        self.assertEqual(grouped['comments']['Observability / services / Validation prerequisites'],
                         'fe-0: Partial inventory')

    def test_frontend_and_kvm_exporters_stay_separate(self):
        results = {'Observability / controller / node exporter': 'ok',
                   'Observability / worker / node exporter': 'failed',
                   'Observability / controller / worker / node target': 'failed',
                   'Observability / controller / controller / node target': 'ok'}
        grouped = module.observability_report(results, frontends=['controller'], nodes=['worker'])
        self.assertEqual(grouped['results']['Observability / frontend / opennebula-node-exporter metrics collection'], 'ok')
        self.assertEqual(grouped['results']['Observability / kvm / opennebula-node-exporter metrics collection'], 'failed')
        self.assertEqual(grouped['comments']['Observability / kvm / opennebula-node-exporter metrics collection'], 'worker, controller → worker')
        self.assertEqual(grouped['labels']['Observability / kvm / opennebula-node-exporter metrics collection'], 'opennebula-node-exporter metrics collection')
        emitted = [key for section in grouped['sections'] for key, _ in section['checks']]
        self.assertEqual(set(emitted), set(grouped['results']))
        self.assertEqual(len(emitted), len(grouped['results']))

    def test_prerequisites_are_combined_without_losing_failures(self):
        results = {'Observability / controller / inventory topology': 'ok',
                   'Observability / worker / probe execution': 'failed',
                   'Observability / controller / worker probe coverage': 'failed'}
        grouped = module.observability_report(results)
        key = 'Observability / services / Validation prerequisites'
        self.assertEqual(grouped['results'], {key: 'failed'})
        self.assertEqual(grouped['comments'][key], 'worker, controller → worker')

    def test_sections_keep_grafana_health_separate_from_integration(self):
        results = {'Observability / controller / Grafana health': 'ok',
                   'Observability / controller / Grafana dashboard / Hosts': 'ok',
                   'Observability / controller / Prometheus alerting rules': 'ok'}
        grouped = module.observability_report(results)
        populated = [s['id'] for s in grouped['sections'] if s['checks']]
        self.assertEqual(populated, ['services', 'integration'])

    def test_successful_prerequisites_are_not_displayed(self):
        results = {'Observability / fe-0 / inventory topology': 'ok'}
        grouped = module.observability_report(results)
        self.assertEqual(grouped['results'], {})
        self.assertTrue(all(not s['checks'] for s in grouped['sections']))

    def test_collector_failure_survives_successful_scrape(self):
        results = {'Observability / worker / mysql exporter': 'failed',
                   'Observability / controller / worker / mysql target': 'ok'}
        grouped = module.observability_report(results, nodes=['worker'])
        key = 'Observability / kvm / opennebula-mysql-exporter metrics collection'
        self.assertEqual(grouped['results'], {key: 'failed'})
        self.assertEqual(grouped['comments'][key], 'worker')
