"""Exercise real VM task/rescue/always paths with fake ONE commands, offline.
Run using the project's Hatch environment; no cloud or system services touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLI = r'''#!/usr/bin/env python3
import json, pathlib, sys
base = pathlib.Path(__file__).parent
case = (base / 'case').read_text()
p = base / 'state.json'
s = json.loads(p.read_text()) if p.exists() else {'vms': [], 'calls': []}
a = sys.argv[1:]
s['calls'].append([pathlib.Path(sys.argv[0]).name] + a)
rc, out = 0, ''
if a[0] == 'instantiate':
    s['vms'] = [{'ID': '42', 'NAME': a[-1], 'STATE': '3'},
                {'ID': '99', 'NAME': 'bystander', 'STATE': '3'}]
    rc = 1 if case == 'partial_create' else 0
    out = 'unexpected output' if case == 'malformed_id' else 'VM ID: 42'
elif a[0] == 'show':
    out = json.dumps({'VM' if pathlib.Path(sys.argv[0]).name == 'onevm' else 'VMTEMPLATE': {'NAME': 'existing-resource'}})
elif a[0] == 'list':
    rc = 1 if case == 'list_failure' else 0
    out = json.dumps({'VM_POOL': {'VM': s['vms']}})
elif a[0] in ('terminate', 'recover'):
    if case == 'cleanup_failure':
        rc = 1
    elif case == 'recover' and a[0] == 'terminate':
        rc = 1
    else:
        s['vms'] = [v for v in s['vms'] if v['ID'] != a[-1]]
p.write_text(json.dumps(s))
print(out)
sys.exit(rc)
'''
PROBE = '''#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule
m = AnsibleModule(argument_spec=dict(phase=dict(), config=dict(type='dict'), auth=dict(type='dict', no_log=True)))
m.exit_json(changed=False, results=[dict(key='VM metrics present', value='ok' if m.params['config'].get('fixture_case') == 'success' else 'failed', comment='Fixture probe result')])
'''


def run(case):
    with tempfile.TemporaryDirectory(prefix='odv-vm-cleanup-') as tmp:
        work = Path(tmp)
        roles = work / 'roles'
        for role in ('observability_validation', 'validation_common'):
            shutil.copytree(ROOT / 'roles' / role, roles / role)
        tasks = roles / 'observability_validation/tasks/vm.yml'
        # Only replace transport and executables; retain production control flow.
        text = tasks.read_text().replace('become: true', 'become: false').replace('/tmp/odv_manifest.json', str(work / 'manifest.json'))
        text = text.replace('argv: [onevm,', 'argv: ["' + str(work / 'onevm') + '",')
        text = text.replace('argv: [onetemplate,', 'argv: ["' + str(work / 'onetemplate') + '",')
        text = text.replace('- "{{ \'onevm\' if', '- "' + str(work) + '/{{ \'onevm\' if')
        tasks.write_text(text)
        manifest = roles / 'validation_common/tasks/manifest_add.yml'
        manifest.write_text(manifest.read_text().replace('/tmp/odv_manifest.json', str(work / 'manifest.json')))
        (roles / 'observability_validation/library/observability_probe.py').write_text(PROBE)
        for cmd in ('onevm', 'onetemplate'):
            (work / cmd).write_text(CLI)
            (work / cmd).chmod(0o755)
        (work / 'case').write_text(case)
        variables = dict(observability_vm={'vm_id': 7} if case == 'existing_rejected' else {'template_id': 0},
                         odv_observability_config={'fixture_case': case}, observability_auth={},
                         observability_vm_defaults=dict(retries=1, delay=0, cleanup_retries=1, cleanup_delay=0))
        play = [dict(hosts='localhost', connection='local', gather_facts=False, vars=variables, tasks=[
            {'include_role': {'name': 'observability_validation', 'tasks_from': 'vm.yml'}},
            {'copy': {'dest': str(work / 'result.json'), 'content': '{{ {"manifest": odv_manifest | d([]), "results": verification_result | d({}), "leftovers": odv_leftovers | d([])} | to_json }}'}}])]
        (work / 'play.yml').write_text(yaml.safe_dump(play))
        env = dict(os.environ, ANSIBLE_ROLES_PATH=str(roles), ANSIBLE_LOCAL_TEMP=str(work / 'ansible-tmp'))
        proc = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', 'localhost,',
                               '-e', 'ansible_python_interpreter=' + sys.executable, str(work / 'play.yml')],
                              cwd=work, env=env, capture_output=True, text=True)
        if case == 'existing_rejected':
            assert proc.returncode != 0
            assert not (work / 'state.json').exists()
            assert not (work / 'manifest.json').exists()
            print('PASS', case)
            return
        if proc.returncode:
            raise AssertionError(proc.stdout + proc.stderr)
        result = json.loads((work / 'result.json').read_text())
        state = json.loads((work / 'state.json').read_text())
        assert any(v == ('ok' if case == 'success' else 'failed') for v in result['results'].values()), result
        assert json.loads((work / 'manifest.json').read_text()) == result['manifest']
        assert any(e['type'] == 'template' and e['disposition'] == 'reused' for e in result['manifest'])
        assert any(e['type'] == 'vm' and e['disposition'] == 'created' for e in result['manifest'])
        assert all(c[-1] != '99' for c in state['calls'] if c[1] in ('terminate', 'recover')), state
        if case in ('cleanup_failure', 'list_failure'):
            assert result['leftovers'] and any(k.endswith('VM cleanup') for k in result['results']), result
        else:
            assert [v['ID'] for v in state['vms']] == ['99'], state
            assert not result['leftovers'], result
        print('PASS', case)


if __name__ == '__main__':
    for case in ('success', 'probe_failure', 'partial_create', 'malformed_id', 'existing_rejected', 'cleanup_failure', 'list_failure', 'recover'):
        run(case)
