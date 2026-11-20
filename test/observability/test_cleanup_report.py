"""Verify monitoring summary aggregation and ownership filtering on recovery."""
from pathlib import Path
import unittest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]


class CleanupReportTests(unittest.TestCase):
    def test_preclean_never_deletes_reused_vm_or_template(self):
        tasks = yaml.safe_load((ROOT / 'roles/validation_sweep/tasks/preclean.yml').read_text())
        task = next(t for t in tasks if t['name'] == 'Exclude reused resources from previous-run cleanup')
        expression = task['ansible.builtin.set_fact']['_preclean_entries']
        owned = {'type': 'vm', 'name': 'odv-monitoring-run', 'disposition': 'created'}
        legacy = {'type': 'vm', 'name': 'legacy-run'}
        reused = [{'type': t, 'name': 'user-resource', 'disposition': 'reused'} for t in ('vm', 'template')]
        actual = Templar(loader=DataLoader(), variables={'_preclean_entries': [owned, legacy] + reused}).template(expression)
        self.assertEqual(actual, [owned, legacy])

    def test_final_sweep_removes_only_confirmed_leftovers(self):
        plays = yaml.safe_load((ROOT / 'playbooks/validation.yml').read_text())
        task = next(t for play in plays for t in play.get('tasks', [])
                    if t.get('name') == 'Reconcile observability VM cleanup with the final sweep')
        old = [{'type': 'vm', 'name': 'removed-vm', 'role': 'observability-vm', 'host': 'fe-1'},
               {'type': 'vm', 'name': 'removed-vm', 'role': 'test-vm', 'host': 'fe-1'},
               {'type': 'vm', 'name': 'stuck-vm', 'role': 'observability-vm', 'host': 'fe-1'},
               {'type': 'file', 'name': '/tmp/shared-name', 'host': 'fe-1'}]
        sweep = [{'type': 'vm', 'name': 'removed-vm', 'host': 'fe-0', 'action': 'removed'},
                 {'type': 'vm', 'name': 'stuck-vm', 'host': 'fe-0', 'action': 'leftover'},
                 {'type': 'file', 'name': '/tmp/shared-name', 'host': 'fe-0', 'action': 'removed'}]
        render = Templar(loader=DataLoader(), variables={'report_leftovers': old, 'report_sweep_results': sweep, **task['vars']})
        self.assertEqual(render.template(task['ansible.builtin.set_fact']['report_leftovers']), old[1:])

    def test_monitoring_failures_are_visible_in_execution_summary(self):
        text = (ROOT / 'playbooks/templates/appendix-cloud-report.j2').read_text()
        fragment = text[text.index('  {% macro exec_worst_of'):text.index('    <div class="section-title" style="margin-top:18px">Cleanup')]
        env = Environment()
        env.filters['bool'] = bool
        for value, expected in [('ok', 'OK'), ('failed', 'FAILED'), ('warning — partially evaluated', 'WARNING')]:
            rendered = env.from_string(fragment).render(
                validation_vars={'run_observability': True},
                verification_result={'Observability / integration / VM metrics present': value})
            row = rendered.split('<tr><td>Observability</td>', 1)[1].split('</tr>', 1)[0]
            self.assertIn('YES', row)
            self.assertIn('>' + expected + '</span>', row)


if __name__ == '__main__':
    unittest.main()
