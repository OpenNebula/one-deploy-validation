#!/usr/bin/env python3
"""Run the observability plays against local HTTP/command fixtures only.

Usage: python3 test/observability/integration.py /path/to/ansible-playbook
No SSH hosts, real systemctl operations or real cloud APIs are involved.
"""
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import urllib.parse

import yaml

REPO = Path(__file__).resolve().parents[2]
EXPORTERS = {
    'node': ('node_exporter', 'node_uname_info 1\nnode_memory_MemTotal_bytes 4096\n'),
    'opennebula': ('opennebula_exporter', 'opennebula_host_total 1\n'),
    'libvirt': ('libvirt_exporter', 'opennebula_libvirt_daemon_up 1\n'),
    'mysql': ('mysql_exporter', 'mysql_up 1\n'),
    'lvm': ('lvm_exporter', 'go_goroutines 2\n'),
    'smartctl': ('smartctl_exporter', 'go_goroutines 2\n'),
}


class Handler(BaseHTTPRequestHandler):
    broken = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        port = self.server.server_address[1]
        data = {}
        if path in ('/-/ready', '/-/healthy'):
            data = 'ready'
        elif path.startswith('/metrics/'):
            data = EXPORTERS[path.rsplit('/', 1)[1]][1]
        elif path == '/api/v2/status':
            data = {'config': {'original': 'route: {receiver: discard}'}}
        elif path == '/api/health':
            data = {'database': 'ok'}
        elif path == '/api/datasources':
            data = [{'type': 'prometheus', 'name': 'prometheus', 'uid': 'one'}]
        elif path == '/api/search':
            data = [{'title': name, 'uid': name} for name in ['OpenNebula', 'Hosts', 'Virtual Machine']]
        elif path.startswith('/api/dashboards/uid/'):
            data = {'dashboard': {'panels': [{'type': 'timeseries', 'targets': [{}], 'datasource': {'type': 'prometheus', 'uid': 'one'}}]}}
        elif path.endswith('/api/v1/query'):
            expression = urllib.parse.parse_qs(url.query).get('query', [''])[0]
            value = 0 if expression.startswith('time()') else 1
            labels = {'one_host_id': '0'} if expression == 'opennebula_host_state' else {}
            data = {'status': 'success', 'data': {'resultType': 'vector', 'result': [
                {'metric': labels, 'value': [0, str(value)]}]}}
        elif path == '/api/v1/targets':
            targets = [{'scrapeUrl': 'http://127.0.0.1:%s/metrics/%s' % (port, name),
                        'labels': {'job': spec[0], 'instance': '127.0.0.1:%s' % port},
                        'health': 'up', 'lastError': '',
                        'lastScrape': datetime.datetime.now(datetime.timezone.utc).isoformat()}
                       for name, spec in EXPORTERS.items() if not (self.broken and name == 'lvm')]
            data = {'status': 'success', 'data': {'activeTargets': targets}}
        elif path == '/api/v1/rules':
            data = {'status': 'success', 'data': {'groups': [{'rules': [{'type': 'alerting', 'name': 'FixtureAlert', 'health': 'ok', 'lastError': ''}]}]}}
        elif path == '/api/v1/alertmanagers':
            data = {'status': 'success', 'data': {'activeAlertmanagers': [{'url': 'http://127.0.0.1:9093/api/v2/alerts'}]}}
        else:
            self.send_error(404)
            return
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain' if isinstance(data, str) else 'application/json')
        self.end_headers()
        self.wfile.write(body)


def main():
    executable = sys.argv[1] if len(sys.argv) > 1 else 'ansible-playbook'
    with tempfile.TemporaryDirectory(prefix='odv-observability-test-') as tmp:
        work = Path(tmp)
        command_dir = work / 'bin'
        command_dir.mkdir()
        scripts = {
            'systemctl': '#!/bin/sh\nprintf "LoadState=loaded\\nActiveState=active\\nUnitFileState=enabled\\n"\n',
            'runuser': '#!/bin/sh\nprintf \'%s\\n\' \'{"HOST_POOL":{"HOST":[{"ID":"0"}]}}\'\n',
        }
        for name, body in scripts.items():
            file = command_dir / name
            file.write_text(body)
            file.chmod(0o755)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%s' % server.server_address[1]
        env = dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default', ANSIBLE_NOCOLOR='1',
                   PATH=str(command_dir) + os.pathsep + os.environ.get('PATH', ''))
        inventory = {
            'all': {'vars': {'ansible_connection': 'local', 'ansible_become': False,
                            'ansible_python_interpreter': sys.executable, 'one_version': '7.4.1',
                            'observability_auth': {'grafana': {'token': 'fixture-secret-do-not-print'}},
                            'observability': {
                                'attempts': 1, 'prometheus_url': base, 'grafana_url': base,
                                'alertmanager_url': base, 'require_grafana_auth': True,
                                'exporters': {name: {'url': base + '/metrics/' + name,
                                                      'port': server.server_address[1]} for name in EXPORTERS}}},
                    'children': {group: {'hosts': {'fixture-fe': {'ansible_host': '127.0.0.1'}}}
                                 for group in ['frontend', 'node', 'grafana']}}}
        inventory_path = work / 'inventory.yml'
        inventory_path.write_text(json.dumps(inventory))  # JSON is valid YAML inventory.
        try:
            for broken in (False, True):
                Handler.broken = broken
                prefix = str(work / ('broken' if broken else 'healthy'))
                report = Path(prefix + '.json')
                suite = yaml.safe_load((REPO / 'playbooks/validation.yml').read_text())
                play = [p for p in suite if 'observability' in p.get('tags', [])]
                play.append({'hosts': 'fixture-fe', 'gather_facts': False, 'tasks': [
                            {'copy': {'dest': str(report), 'content': '{{ verification_result | to_json }}'}}]})
                fixture_play = work / 'checks.yml'
                fixture_play.write_text(json.dumps(play))
                command = [executable, '-i', str(inventory_path), str(fixture_play),
                           '-e', '{"validation":{"run_observability":true}}']
                result = subprocess.run(command, cwd=REPO, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                assert result.returncode == 0, result.stdout
                if not report.exists():
                    raise AssertionError('No report generated:\n' + result.stdout)
                rows = [{'key': key, 'value': value} for key, value in json.loads(report.read_text()).items()]
                failures = [row for row in rows if row['value'] == 'failed']
                if broken:
                    assert any('lvm target' in row['key'] for row in failures), failures
                else:
                    assert not failures, failures
                assert 'fixture-secret-do-not-print' not in result.stdout
                assert 'fixture-secret-do-not-print' not in report.read_text()
                print('%s: %d findings, %d failures, rc=%d' % (
                    'missing-target' if broken else 'healthy', len(rows), len(failures), result.returncode))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
