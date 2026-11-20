"""Exercise production endpoint tasks against local HTTP fixtures, without SSH."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import yaml

ROOT = Path(__file__).resolve().parents[2]


class Handler(BaseHTTPRequestHandler):
    mode = 'healthy'
    calls = {}
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.calls[self.path] = self.calls.get(self.path, 0) + 1
        authorized = self.headers.get('Authorization') in ('Bearer fixture-secret', 'Basic Zml4dHVyZTpmaXh0dXJl')
        status = 200 if authorized else 401
        payload = {'database': 'ok', 'config': {'original': 'fixture'}}
        if self.path == '/api/health' and (self.mode in ('bad_database', 'single_failure') or self.mode == 'transient' and self.calls[self.path] == 1):
            payload['database'] = 'failed'
        if self.mode == 'bad_config' and self.path == '/api/v2/status':
            payload = {}
        if self.mode == 'redirect':
            status = 302
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        if status == 302:
            self.send_header('Location', '/login')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='odv-endpoints-') as tmp:
            work = Path(tmp)
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            for mode in ('healthy', 'transient', 'bad_database', 'bad_config', 'redirect', 'basic', 'single_failure'):
                Handler.mode, Handler.calls = mode, {}
                auth = {'username': 'fixture', 'password': 'fixture'} if mode == 'basic' else {'token': 'fixture-secret', 'username': 'ignored', 'password': 'ignored'}
                variables = {'_obs_endpoint_config': {**{s+'_url': base for s in ('prometheus', 'alertmanager', 'grafana')},
                    'attempts': 1 if mode == 'single_failure' else 2, 'delay': 0, 'timeout': 2, 'validate_certs': True},
                    '_obs_endpoint_auth': {s: auth for s in ('prometheus', 'alertmanager', 'grafana')},
                    '_obs_endpoint_host': 'localhost', '_obs_endpoint_subject': 'fixture',
                    '_obs_endpoint_roles': ['frontend', 'grafana'], 'ansible_python_interpreter': sys.executable}
                play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'vars': variables, 'tasks': [
                    {'include_role': {'name': 'observability_validation', 'tasks_from': 'endpoints/main.yml'}},
                    {'copy': {'dest': str(work/'results.json'), 'content': '{{ _obs_endpoint_results | to_json }}'}}]}]
                (work/'play.yml').write_text(yaml.safe_dump(play))
                env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT/'roles'))
                result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', 'localhost,', str(work/'play.yml')],
                    cwd=work, env=env, text=True, capture_output=True, timeout=120)
                assert result.returncode == 0, result.stdout + result.stderr
                rows = json.loads((work/'results.json').read_text())
                assert len(rows) == 4, rows
                failures = [row['key'].split(' / ')[-1] for row in rows if row['value'] == 'failed']
                expected = {'bad_database': ['Grafana health'], 'single_failure': ['Grafana health'], 'bad_config': ['Alertmanager loaded configuration'],
                            'redirect': ['Prometheus readiness', 'Alertmanager readiness', 'Alertmanager loaded configuration', 'Grafana health']}.get(mode, [])
                assert failures == expected, rows
                assert 'fixture-secret' not in result.stdout + result.stderr
                assert '/login' not in Handler.calls
                if mode == 'single_failure':
                    assert Handler.calls['/api/health'] == 1, Handler.calls
                if mode == 'bad_database':
                    assert Handler.calls['/api/health'] == 2, Handler.calls
                if mode == 'transient':
                    assert Handler.calls['/api/health'] == 2, Handler.calls
                print(mode + ': PASS', flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
