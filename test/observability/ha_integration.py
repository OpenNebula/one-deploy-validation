#!/usr/bin/env python3
"""Run real HA tasks against local service/API fixtures, never against a lab.
Requires ansible-core and PyYAML. Logs are retained under /tmp.
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
SYSTEMCTL = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['ODV_HA_STATE']); s=json.loads(p.read_text()); h=os.environ['ODV_HA_HOST']
a=sys.argv[1:]; op=a[0]; u=a[1] if len(a)>1 else ''
state=s['services'][h].get(u,'active')
if op=='show':
 print(state if '--value' in a else 'LoadState=loaded\\nActiveState='+state+'\\nUnitFileState=enabled\\nSubState=running')
elif op in ('start','stop'):
 s['services'][h][u]='active' if op=='start' else 'inactive'
 s['events'].append([op,h,u])
 if op=='stop' and u=='opennebula.service': s['leader']='fe-1'
 p.write_text(json.dumps(s))
else: sys.exit('Unexpected fake systemctl call: '+repr(a))
'''
PROBE = '''#!/usr/bin/python
from ansible.module_utils.basic import AnsibleModule
import json, os
from pathlib import Path
m=AnsibleModule(argument_spec={'phase':{'type':'str'},'config':{'type':'dict'},'auth':{'type':'dict','no_log':True}},supports_check_mode=True)
p=Path(os.environ['ODV_HA_STATE']); s=json.loads(p.read_text()); c=m.params['config']; phase=m.params['phase']; h=c['host']; ha={}; ok=True
s['events'].append(['probe',phase,h])
if phase=='ha_grafana':
 ok=s['services'][h].get('grafana-server.service')=='active'
 if s.get('fail_grafana_recovery')==h and ['start',h,'grafana-server.service'] in s['events']: ok=False
elif phase=='ha_monitoring':
 s['counter']+=1; ha={'samples':{'sample':s['counter']}}
 ok=all(s['services'][h].get(u)=='active' for u in ['opennebula-prometheus.service','opennebula-alertmanager.service'])
 if c.get('require_progress'): ok=ok and s['counter']>c['previous_samples']['sample']
 if s.get('fail_outage') and s['services']['fe-0']['opennebula.service']=='inactive': ok=False
p.write_text(json.dumps(s))
m.exit_json(changed=False,ha=ha,results=[{'key':'test','value':'ok' if ok else 'failed','comment':'fixture'}])
'''


def run_case(base, fail_outage=False, fail_grafana=None, grafana_hosts=None, leader_enabled=True):
    grafana_hosts = grafana_hosts or ['fe-0', 'fe-1', 'fe-2', 'grafana-extra']
    hosts = sorted(set(['fe-0', 'fe-1', 'fe-2'] + grafana_hosts))
    units = ['opennebula.service', 'opennebula-prometheus.service', 'opennebula-alertmanager.service', 'grafana-server.service']
    state = base / 'state.json'
    state.write_text(json.dumps({'leader': 'fe-0', 'counter': 0, 'events': [],
        'fail_outage': fail_outage, 'fail_grafana_recovery': fail_grafana,
        'services': {h: {u: 'active' for u in units} for h in hosts}}))
    bindir = base / 'bin'; bindir.mkdir()
    (bindir/'systemctl').write_text(SYSTEMCTL); (bindir/'systemctl').chmod(0o755)
    command_fixture = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
s=json.loads(Path(os.environ['ODV_HA_STATE']).read_text()); h=os.environ['ODV_HA_HOST']
if Path(sys.argv[0]).name=='onezone':
 print(json.dumps({'ZONE': {'SERVER_POOL': {'SERVER': [{'NAME': n, 'STATE': '3' if n==s['leader'] else '2' if s['services'][n]['opennebula.service']=='active' else '0'} for n in ['fe-0','fe-1','fe-2']]}}}))
else:
 print(json.dumps([{'addr_info': [{'local':'192.0.2.100'}]}] if h==s['leader'] else []))
'''
    for name in ('onezone', 'ip'):
        (bindir/name).write_text(command_fixture); (bindir/name).chmod(0o755)
    roles = base/'roles'; roles.mkdir()
    shutil.copytree(ROOT/'roles/observability_validation', roles/'observability_validation')
    (roles/'observability_validation/library/observability_probe.py').write_text(PROBE)
    identity=roles/'observability_validation/tasks/ha/identity.yml'
    identity.write_text(identity.read_text().replace('become: true', 'become: false'))
    # Mock HTTP transport only; endpoint assertions/retries remain production YAML.
    endpoint = roles/'observability_validation/tasks/endpoints/check.yml'
    endpoint.write_text(endpoint.read_text().replace('ansible.builtin.uri:', 'fixture_endpoint:'))
    (roles/'observability_validation/library/fixture_endpoint.py').write_text('''#!/usr/bin/python
import json, os
from pathlib import Path
from ansible.module_utils.basic import AnsibleModule
keys='url method status_code return_content follow_redirects use_netrc use_proxy validate_certs timeout headers url_username url_password force_basic_auth'.split()
m=AnsibleModule(argument_spec={k: {'type':'raw', 'no_log': k in ('headers','url_password')} for k in keys})
s=json.loads(Path(os.environ['ODV_HA_STATE']).read_text()); host=os.environ['ODV_HA_HOST']
url=m.params['url']; unit='grafana-server.service' if '/api/health' in url else 'opennebula-alertmanager.service' if ':9093' in url else 'opennebula-prometheus.service'
m.exit_json(changed=False, status=200 if s['services'][host][unit]=='active' else 503, json={'database':'ok','config':{'original':'fixture'}})
''')
    common=roles/'validation_common/tasks'; common.mkdir(parents=True)
    shutil.copy(ROOT/'roles/validation_common/tasks/record_result.yml', common/'record_result.yml')
    inventory={'all': {'vars': {'ansible_connection': 'local', 'ansible_become': False, 'fixture_leader_enabled': leader_enabled,
        'observability_ha': {'enabled':True,'vip':'192.0.2.100','retries':1,'recovery_retries':1,'delay':0},
        'observability_grafana_restart': {'enabled':True,'retries':1,'delay':0},
        'odv_observability_targets':[{'fixture':True}],
        'odv_observability_frontends':[{'host':h,'aliases':[h]} for h in ['fe-0','fe-1','fe-2']]},
        'children': {'frontend':{'hosts':{h:{} for h in ['fe-0','fe-1','fe-2']}},
                     'grafana': {'hosts':{h:{} for h in grafana_hosts}}}, 'hosts':{}}}
    for h in hosts:
        wrapper=base/('python-'+h)
        wrapper.write_text('#!/bin/sh\nexport ODV_HA_HOST='+h+'\nexport ODV_HA_STATE='+str(state)+'\nexport PATH='+str(bindir)+':"$PATH"\nexec /usr/bin/python3 "$@"\n')
        wrapper.chmod(0o755)
        inventory['all']['hosts'][h]={'ansible_python_interpreter':str(wrapper),
            'odv_observability_config':{'host':h,'grafana_url':'http://127.0.0.1:1','prometheus_url':'http://127.0.0.1:9090','alertmanager_url':'http://127.0.0.1:9093','validate_certs':True,'timeout':1,'attempts':1,'delay':0},
            'odv_observability_local':{'expected_targets':[], 'results':[{'value':'ok'}]},
            'observability_auth':{'grafana':{'username':'fixture','password':'fixture'}}}
    (base/'inventory.yml').write_text(yaml.safe_dump(inventory))
    (base/'play.yml').write_text('''- hosts: frontend
  gather_facts: false
  tasks:
    - ansible.builtin.include_role:
        name: observability_validation
        tasks_from: ha_leader.yml
      run_once: true
      when: fixture_leader_enabled
    - ansible.builtin.include_role:
        name: observability_validation
        tasks_from: ha_grafana.yml
      run_once: true
    - ansible.builtin.copy:
        content: "{{ verification_result | to_json }}"
        dest: "'''+str(base/'results.json')+'''"
      run_once: true
''')
    env=dict(os.environ, ANSIBLE_ROLES_PATH=str(roles), ANSIBLE_STDOUT_CALLBACK='default',
             ANSIBLE_CALLBACKS_ENABLED='', ANSIBLE_HOST_KEY_CHECKING='False')
    process=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(base/'inventory.yml'),str(base/'play.yml')],
                           env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=240)
    (base/'output.log').write_text(process.stdout)
    if process.returncode or not (base/'results.json').exists():
        raise AssertionError(process.stdout[-12000:])
    results=json.loads((base/'results.json').read_text()); final=json.loads(state.read_text())
    assert all(v=='active' for services in final['services'].values() for v in services.values()), final
    if fail_outage:
        assert any('failover' in k and v=='failed' for k,v in results.items()), results
        assert any('Alertmanager recovery' in k and v=='ok' for k,v in results.items()), results
        assert not any(e[0]=='stop' and e[2]=='grafana-server.service' for e in final['events']), final
    elif fail_grafana:
        assert results['Observability / '+fail_grafana+' / Grafana service recovery']=='failed', results
        later=grafana_hosts[grafana_hosts.index(fail_grafana)+1:]
        assert not any(e[0]=='stop' and e[1] in later and e[2]=='grafana-server.service' for e in final['events']), final
    else:
        assert all(v=='ok' for v in results.values()), results
        for h in grafana_hosts:
            assert ['stop',h,'grafana-server.service'] in final['events']
            assert ['start',h,'grafana-server.service'] in final['events']
    return results


if __name__=='__main__':
    directory=Path(tempfile.mkdtemp(prefix='odv-ha-integration-'))
    print('Fixture logs: '+str(directory),flush=True)
    for name, kwargs in [('success',{}), ('outage_failure',{'fail_outage':True}),
                         ('grafana_failure',{'fail_grafana':'fe-0'}),
                         ('single_grafana',{'grafana_hosts':['grafana-extra']}),
                         ('grafana_only',{'grafana_hosts':['grafana-extra'], 'leader_enabled':False})]:
        case=directory/name; case.mkdir()
        run_case(case,**kwargs)
        print(name+': PASS',flush=True)
