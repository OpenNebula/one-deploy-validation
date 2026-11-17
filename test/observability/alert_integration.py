"""Run production alert tasks and receiver against fake Prometheus/AM HTTP APIs.
No live cloud, systemd services or deployment files are touched.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELOAD = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ['ODV_ALERT_FIXTURE'])
host, service = sys.argv[-1].split(':')
p = root / host
content = json.loads((p / (service + '.json')).read_text())
if os.environ.get('ODV_ALERT_CASE') == 'route_failure' and host == 'fe-1' and service == 'alertmanager' and 'ODVValidation_' in json.dumps(content):
    sys.exit(1)
if service == 'rules' and any(r.get('expr') == 'vector(1)' and r.get('alert', '').startswith('ODVValidation_') for g in content['groups'] for r in g['rules']):
    for peer in root.glob('fe-*'):
        assert 'ODVValidation_' in (peer / 'alertmanager.loaded').read_text(), 'Source fired before every route was loaded'
(p / (service + '.candidate')).write_text(json.dumps(content))
os.replace(p / (service + '.candidate'), p / (service + '.loaded'))
with (root / 'events.jsonl').open('a') as out:
    out.write(json.dumps([host, service, 'test' if 'ODVValidation_' in json.dumps(content) else 'original']) + '\\n')
'''


def run_case(case):
    work = Path(tempfile.mkdtemp(prefix='odv-alert-' + case + '-'))
    hosts = ['fe-0'] if case == 'sa' else ['fe-0', 'fe-1', 'fe-2']
    originals = {}
    edited = set()
    delivered = set()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def active():
        alerts = []
        for host in hosts:
            rules = json.loads((work / host / 'rules.loaded').read_text())
            for group in rules['groups']:
                for rule in group['rules']:
                    if rule.get('expr') == 'vector(1)' and 'alert' in rule:
                        alerts.append({'labels': dict(rule['labels'], alertname=rule['alert']), 'state': 'firing'})
        return alerts

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            host = 'fe-' + str(int(self.headers['Host'].split(':')[0].split('.')[-1]) - 2)
            service, path = self.path.split('/', 2)[1:]
            path = '/' + path
            alerts = active()
            if path == '/api/v1/alertmanagers':
                urls = ['http://127.0.0.%d:%d/am/api/v2/alerts' % (i+2, server.server_port) for i in range(len(hosts))]
                if case == 'unknown_target':
                    urls.append('http://192.0.2.99:9093/api/v2/alerts')
                payload = {'data': {'activeAlertmanagers': [{'url': url} for url in urls]}}
            elif path == '/api/v1/alerts':
                own = [a for a in alerts if a['labels']['odv_source'] == host]
                payload = {'data': {'alerts': own}}
                if case == 'external_edit' and host == 'fe-0' and own and host not in edited:
                    (work / host / 'rules.json').write_text('externally edited\n')
                    edited.add(host)
                for alert in own:
                    source = alert['labels']['odv_source']
                    if source in delivered or (case == 'missing_webhook' and source == 'fe-1'):
                        continue
                    # Only a peer receiver gets the notification in HA (deduplication).
                    peer = hosts[-1]
                    am = json.loads((work / peer / 'alertmanager.loaded').read_text())
                    name = 'ODVValidation_' + alert['labels']['odv_run_id']
                    receiver = next(r for r in am['receivers'] if r['name'] == name)
                    body = {'receiver': name, 'alerts': [dict(alert, status='firing')]}
                    with opener.open(urllib.request.Request(receiver['webhook_configs'][0]['url'], data=json.dumps(body).encode()), timeout=3):
                        delivered.add(source)
            elif path == '/api/v1/rules':
                rules = json.loads((work / host / 'rules.loaded').read_text())
                payload = {'data': {'groups': [{'rules': [{'name': r.get('alert', r.get('record'))} for r in g['rules']]} for g in rules['groups']]}}
            elif path.startswith('/api/v2/alerts?'):
                payload = [{'labels': a['labels']} for a in alerts]
            elif path == '/api/v2/status':
                payload = {'config': {'original': (work / host / 'alertmanager.loaded').read_text()},
                           'cluster': {'peers': [{'name': h} for h in hosts]}}
            else:
                raise AssertionError(self.path)
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        bindir = work / 'bin'
        bindir.mkdir()
        (bindir / 'systemctl').write_text(RELOAD)
        (bindir / 'systemctl').chmod(0o755)
        hostvars = {}
        for index, host in enumerate(hosts):
            directory = work / host
            directory.mkdir()
            configs = {
                'rules': {'groups': [{'name': 'ordinary', 'rules': [{'record': 'original', 'expr': 'vector(2)'}]}]},
                'alertmanager': {'route': {'receiver': 'ordinary'}, 'receivers': [{'name': 'ordinary'}]},
                'prometheus': {'rule_files': [str(directory / 'rules.json')]},
            }
            for name, config in configs.items():
                raw = json.dumps(config, indent=2) + '\n'
                (directory / (name + '.json')).write_text(raw)
                (directory / (name + '.loaded')).write_text(raw)
                originals[host, name] = raw
            base = 'http://127.0.0.%d:%d' % (index+2, server.server_port)
            hostvars[host] = {
                'ansible_host': '127.0.0.' + str(index+2),
                'odv_observability_config': {'prometheus_url': base + '/prom', 'alertmanager_url': base + '/am', 'validate_certs': True, 'timeout': 2},
                'observability_alert': {'enabled': True, 'wait_timeout': 10, 'poll_interval': 1,
                    'prometheus_config': str(directory / 'prometheus.json'), 'rules_file': str(directory / 'rules.json'),
                    'alertmanager_config': str(directory / 'alertmanager.json'), 'backup_dir': str(directory),
                    'promtool': '/bin/true', 'amtool': '/bin/true',
                    'prometheus_service': host + ':rules', 'alertmanager_service': host + ':alertmanager'},
            }
        if case == 'disabled_first':
            hostvars['fe-0']['observability_alert']['enabled'] = False
        if case == 'unreachable':
            plugins = work / 'connection_plugins'
            plugins.mkdir()
            (plugins / 'fixture_unreachable.py').write_text('''from ansible.plugins.connection import ConnectionBase
from ansible.errors import AnsibleConnectionFailure
class Connection(ConnectionBase):
    transport = 'fixture_unreachable'
    def _connect(self):
        raise AnsibleConnectionFailure('simulated connection loss')
    def exec_command(self, *args, **kwargs):
        self._connect()
    def put_file(self, *args, **kwargs):
        self._connect()
    def fetch_file(self, *args, **kwargs):
        self._connect()
    def close(self):
        pass
''')
            hostvars['fe-1']['ansible_connection'] = 'fixture_unreachable'
        if case == 'locked':
            (work / 'fe-1' / 'alertmanager.json.odv-lock').mkdir()
        inventory = {'all': {'vars': {'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable,
                                     'validation': {'run_observability': True}},
                             'children': {'frontend': {'hosts': hostvars}}}}
        source = yaml.safe_load((ROOT / 'playbooks/validation.yml').read_text())
        play = next(p for p in source if p.get('name') == 'Validate the complete alert delivery pipeline')
        play['environment'] = {'PATH': str(bindir) + ':' + os.environ['PATH'], 'ODV_ALERT_FIXTURE': str(work), 'ODV_ALERT_CASE': case}
        collect = {'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'tasks': [
            {'copy': {'dest': str(work / 'results.json'), 'content': '{{ hostvars[groups.frontend[0]].verification_result | to_json }}'}}]}
        (work / 'inventory.yml').write_text(yaml.safe_dump(inventory))
        (work / 'play.yml').write_text(yaml.safe_dump([play, collect]))
        env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / 'roles'), ANSIBLE_STDOUT_CALLBACK='default')
        result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', str(work/'inventory.yml'), str(work/'play.yml')],
                                cwd=work, env=env, capture_output=True, text=True, timeout=600)
        (work / 'ansible.log').write_text(result.stdout + result.stderr)
        assert result.returncode == 0, str(work / 'ansible.log') + '\n' + result.stdout[-4000:]
        results = json.loads((work / 'results.json').read_text())
        for host in hosts:
            if case == 'disabled_first' and host == 'fe-0':
                assert 'Observability / fe-0 / Alert delivery pipeline' not in results
                value = 'ok'
            else:
                value = results['Observability / ' + host + ' / Alert delivery pipeline']
            expected = 'ok'
            if case in ('unknown_target', 'route_failure', 'locked', 'unreachable') or (case == 'missing_webhook' and host == 'fe-1'):
                expected = 'failed'
            if case == 'external_edit' and host != 'fe-0':
                expected = 'skipped — previous source cleanup failed'
            assert value == expected, (case, host, value, results, str(work))
            if case != 'external_edit':
                for name in ['rules', 'alertmanager']:
                    assert (work/host/(name+'.json')).read_text() == originals[host, name], (host, name, str(work))
                assert not list((work/host).glob('odv-alert-*')), str(work)
            else:
                assert list((work/host).glob('odv-alert-*')), str(work)
                assert 'Alert pipeline cleanup' in str(results), results
            assert (work/host/'alertmanager.json.odv-lock').exists() == (case == 'external_edit' or case == 'locked' and host == 'fe-1'), str(work)
        if case == 'external_edit':
            assert (work/'fe-0/rules.json').read_text() == 'externally edited\n'
        print(case + ': PASS (' + str(work) + ')', flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    for case in sys.argv[1:] or ['sa', 'ha', 'missing_webhook', 'route_failure', 'unknown_target', 'external_edit', 'locked', 'unreachable', 'disabled_first']:
        run_case(case)
