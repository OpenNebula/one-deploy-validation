#!/usr/bin/python
"""Read-only observability probes. Only GET requests and read-only commands.

The pure helpers are intentionally independent of Ansible for fixture tests.
Secrets are accepted separately and never included in findings.
"""
import base64
import datetime
import json
import math
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def samples(body):
    """Read sample names/values, ignoring comments and optional timestamps."""
    result = {}
    for line in body.splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{.*\})?\s+([^\s]+)(?:\s|$)', line)
        if match:
            try:
                result.setdefault(match[1], []).append(float(match[2]))
            except ValueError:
                pass
    return result


def vector_values(response):
    if response.get('status') != 'success' or response.get('data', {}).get('resultType') != 'vector':
        raise ValueError('Expected a successful Prometheus vector response')
    result = response['data'].get('result', [])
    return [float(item['value'][1]) for item in result]


def fresh_target(target, max_age, now=None):
    try:
        # Prometheus emits nanoseconds; Python < 3.11 accepts microseconds only.
        timestamp = re.sub(r'(\.\d{6})\d+', r'\1', target['lastScrape'])
        stamp = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
        age = (time.time() if now is None else now) - stamp
        return target.get('health') == 'up' and not target.get('lastError') and -30 <= age <= max_age
    except (KeyError, ValueError, TypeError):
        return False


def run_command(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ''


def resolve_datasource(reference, dashboard, sources, seen=()):
    """Resolve explicit/default references and datasource variables conservatively."""
    if isinstance(reference, dict):
        reference = reference.get('uid')
    if reference is None or reference == '':
        matches = [s for s in sources if s.get('isDefault')]
    elif isinstance(reference, str):
        variable = re.fullmatch(r'\$\{(\w+)\}|\$(\w+)', reference)
        if variable:
            name = variable.group(1) or variable.group(2)
            if name in seen:
                raise ValueError('Cyclic datasource variable: ' + name)
            variables = [v for v in dashboard.get('templating', {}).get('list', [])
                         if v.get('name') == name and v.get('type') == 'datasource']
            if len(variables) != 1:
                raise ValueError('Datasource variable undefined: ' + name)
            var = variables[0]
            candidates = [s for s in sources if s.get('type') == var.get('query')]
            pattern = var.get('regex', '')
            if pattern:
                if pattern.startswith('/') and pattern.endswith('/'):
                    pattern = pattern[1:-1]
                try:
                    candidates = [s for s in candidates if re.search(pattern, s.get('name', ''))]
                except re.error:
                    raise ValueError('Unsupported datasource filter: ' + name) from None
            selected = (var.get('current') or {}).get('value')
            if selected not in (None, ''):
                if isinstance(selected, list):
                    if len(selected) != 1:
                        raise ValueError('Multiple datasource selections: ' + name)
                    selected = selected[0]
                return resolve_datasource(selected, dashboard, candidates, seen + (name,))
            if len(candidates) != 1:
                raise ValueError('Datasource variable %s has no saved selection and %d candidates' % (name, len(candidates)))
            return candidates[0]['uid']
        matches = [s for s in sources if reference in (s.get('uid'), s.get('name'))]
    else:
        raise ValueError('Invalid datasource reference')
    if len(matches) != 1:
        raise ValueError('Datasource reference does not resolve uniquely: %s' % reference)
    return matches[0]['uid']


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials to another host or accept a login page.
        return None


class Probe:
    def __init__(self, config, auth=None):
        self.c = config
        self.auth = auth or {}
        self.results = []
        self.expected = []
        self.ha_data = {}
        self.aliases = list(set(config.get('aliases', []) + [socket.gethostname(), socket.getfqdn()]))
        self.context = ssl.create_default_context()
        if not config.get('validate_certs', True):
            self.context = ssl._create_unverified_context()

    def record(self, check, ok, detail='', skipped=False):
        self.results.append({'key': 'Observability / %s / %s' % (self.c['host'], check),
                             'value': ('skipped — ' + detail) if skipped else ('ok' if ok else 'failed'),
                             'comment': '' if skipped else detail[:800]})

    def get(self, url, auth=None, as_json=True, attempts=None):
        if urllib.parse.urlsplit(url).username is not None:
            raise ValueError('Use observability_auth, not credentials in URLs')
        headers = {'Accept': 'application/json' if as_json else 'text/plain'}
        auth = auth or {}
        if auth.get('token'):
            headers['Authorization'] = 'Bearer ' + auth['token']
        elif auth.get('username'):
            credentials = '%s:%s' % (auth['username'], auth.get('password', ''))
            headers['Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=self.context))
        attempts = max(1, min(int(self.c.get('attempts', 3) if attempts is None else attempts), 10))
        for attempt in range(attempts):
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=self.c.get('timeout', 5)) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                    if len(body) > 8 * 1024 * 1024:
                        raise ValueError('Response exceeds 8 MiB')
                    body = body.decode('utf-8')
                    return json.loads(body) if as_json else body
            except (OSError, ValueError, urllib.error.URLError) as exc:
                if attempt + 1 == attempts:
                    # Never expose remote bodies, URLs with secrets, or auth headers.
                    raise ValueError('HTTP request failed (%s%s)' % (
                        type(exc).__name__, ': %s' % exc.code if isinstance(exc, urllib.error.HTTPError) else '')) from None
                time.sleep(self.c.get('delay', 2))

    def check(self, name, function):
        try:
            detail = function()
            if isinstance(detail, str):
                self.record(name, True, detail)
        except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
            self.record(name, False, str(exc) if isinstance(exc, ValueError) else 'Malformed response or unavailable resource')

    def exporter_config(self, name):
        conf = dict(self.c.get('exporters', {}).get(name, {}), name=name)
        if not all(key in conf for key in ('port', 'job', 'metrics')):
            raise ValueError('Missing exporter defaults: ' + name)
        conf.setdefault('url', 'http://127.0.0.1:%s/metrics' % conf['port'])
        return conf

    def exporter(self, conf):
        # Retry the complete scrape, not just HTTP errors: a successful response
        # can temporarily lack required metrics or report a collector failure.
        attempts = max(1, min(int(self.c.get('attempts', 3)), 10))
        for attempt in range(attempts):
            try:
                body = self.get(conf['url'], self.auth.get(conf['name']),
                                as_json=False, attempts=1)
                return self.validate_exporter_metrics(conf, body)
            except ValueError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(self.c.get('delay', 2))

    def validate_exporter_metrics(self, conf, body):
        name = conf['name']
        values = samples(body)
        if not values:
            raise ValueError('No valid Prometheus samples in /metrics')
        missing = [metric for metric in conf['metrics'] if metric not in values]
        problems = []
        if missing:
            problems.append('missing metrics=' + ','.join(missing))
        health_metric = {'mysql': 'mysql_up', 'libvirt': 'opennebula_libvirt_daemon_up'}.get(name)
        if health_metric:
            readings = values.get(health_metric, [])
            if not readings or not all(v == 1 for v in readings):
                problems.append('%s=%s' % (health_metric, readings or 'missing'))
        errors = [k for k, readings in values.items()
                  if ('last_scrape_error' in k or k.endswith('_scrape_error')) and any(v != 0 for v in readings)]
        if errors:
            problems.append('collector errors=' + ','.join(errors))
        if problems:
            raise ValueError('; '.join(problems))
        return '/metrics valid; required metrics and collector health OK'

    def local(self):
        roles = self.c['roles']
        role_defaults = self.c.get('expected_exporters', {})
        names = list(dict.fromkeys(name for role in roles for name in role_defaults.get(role, [])))
        if any(role in roles for role in ('frontend', 'node')):
            names = list(dict.fromkeys(names + self.c.get('additional_exporters', [])))
        unknown = set(names) - set(self.c.get('exporters', {}))
        if unknown:
            raise ValueError('Unknown exporter(s): ' + ', '.join(sorted(unknown)))
        if any(role in roles for role in ('frontend', 'node')) and not names:
            raise ValueError('No expected exporters configured')
        for name in names:
            conf = self.exporter_config(name)
            # Construct expectations BEFORE probing; failed exporters must not disappear.
            scrape = name not in ('smartctl', 'ovs') or 'node' in roles
            scrape = conf.get('scrape', scrape)
            if scrape:
                self.expected.append(dict(conf, host=self.c['host'], aliases=self.aliases))
                self.check(name + ' exporter', lambda conf=conf: self.exporter(conf))
        if 'grafana' in roles:
            self.check('Grafana', self.grafana)

    def addresses(self, aliases):
        result = set(aliases)
        for alias in aliases:
            try:
                result.update(item[4][0] for item in socket.getaddrinfo(alias, None))
            except OSError:
                pass
        return result

    def match_target(self, target, expected):
        url = urllib.parse.urlsplit(target.get('scrapeUrl', ''))
        if target.get('labels', {}).get('job') != expected['job'] or url.port != expected['port']:
            return False
        addresses = self.addresses(expected['aliases'])
        actual = self.addresses([url.hostname]) if url.hostname else set()
        if url.hostname in ('localhost', '127.0.0.1', '::1'):
            return expected['host'] == self.c['host']
        return bool(addresses & actual)

    def prom_get(self, path):
        return self.get(self.c.get('prometheus_url', 'http://127.0.0.1:9090').rstrip('/') + path, self.auth.get('prometheus'))

    def query(self, expression):
        return self.prom_get('/api/v1/query?' + urllib.parse.urlencode({'query': expression}))

    def target(self, expected, targets):
        name = '%s / %s target' % (expected['host'], expected['name'])
        matches = [t for t in targets if self.match_target(t, expected)]
        if len(matches) != 1:
            self.record(name, False, 'Expected exactly one target, found %d' % len(matches))
            return
        target = matches[0]
        problems = []
        if not fresh_target(target, self.c.get('max_sample_age', 120)):
            problems.append('scrape health=%s; age=%s; error=%s' % (
                target.get('health'), target.get('lastScrape'), bool(target.get('lastError'))))
        labels = target.get('labels', {})
        selector = '{' + ','.join('%s=%s' % (key, json.dumps(labels[key])) for key in ('job', 'instance')) + '}'
        checked = []
        for metric in ['up'] + expected['metrics']:
            readings = vector_values(self.query(metric + selector))
            health = metric in ('up', 'mysql_up', 'opennebula_libvirt_daemon_up')
            if not readings or not all(math.isfinite(v) and (not health or v == 1) for v in readings):
                problems.append('%s missing/invalid' % metric)
            ages = vector_values(self.query('time() - timestamp(' + metric + selector + ')'))
            if not ages or not all(-30 <= v <= self.c.get('max_sample_age', 120) for v in ages):
                problems.append('%s stale' % metric)
            checked.append(metric)
        self.record(name, not problems,
                    ('; '.join(problems) if problems else 'UP; fresh metrics=' + ','.join(checked)))

    def prometheus(self):
        self.check('Prometheus loaded configuration', self.reload_status)
        response = self.prom_get('/api/v1/targets')
        if response.get('status') != 'success':
            raise ValueError('Prometheus targets API did not return success')
        targets = response['data']['activeTargets']
        expected_targets = [expected for expected in self.c.get('expected_targets', [])
                            if expected['name'] != 'opennebula' or expected['host'] == self.c['host']]
        for expected in expected_targets:
            self.check('%s / %s target' % (expected['host'], expected['name']),
                       lambda expected=expected: self.target(expected, targets))
        exporter_jobs = {item['job'] for item in expected_targets}
        unexpected = []
        for target in targets:
            labels = target.get('labels', {})
            url = urllib.parse.urlsplit(target.get('scrapeUrl', ''))
            job = labels.get('job')
            if job not in exporter_jobs:
                continue
            if not any(self.match_target(target, dict(item, job=item['job'], port=item['port']))
                       for item in expected_targets if item['job'] == job):
                unexpected.append('%s %s' % (job, url.netloc))
        if unexpected:
            self.record('Prometheus unexpected exporter targets', False, ', '.join(unexpected))
        self.check('Prometheus rules', self.rules)
        self.check('Prometheus Alertmanager discovery', self.alertmanager_targets)
        self.check('OpenNebula host coverage', self.host_coverage)

    def reload_status(self):
        values = vector_values(self.query('prometheus_config_last_reload_successful'))
        self.record('Prometheus loaded configuration', bool(values) and all(v == 1 for v in values))

    def rules(self):
        response = self.prom_get('/api/v1/rules')
        if response.get('status') != 'success':
            raise ValueError('Rules API did not return success')
        groups = response['data']['groups']
        rules = [rule for group in groups for rule in group.get('rules', [])]
        alert_rules = [rule for rule in rules if rule.get('type') == 'alerting']
        recording_rules = [rule for rule in rules if rule.get('type') == 'recording']
        alert_problems = []
        if not alert_rules:
            alert_problems.append('no alerting rules loaded')
        alert_problems.extend('unhealthy ' + str(rule.get('name')) for rule in alert_rules
                              if rule.get('health') != 'ok' or rule.get('lastError'))
        self.record('Prometheus alerting rules', not alert_problems,
                    '; '.join(alert_problems) if alert_problems else '%d loaded and healthy' % len(alert_rules))
        recording_problems = []
        for rule in recording_rules:
            if rule.get('health') != 'ok' or rule.get('lastError'):
                recording_problems.append('unhealthy ' + str(rule.get('name')))
                continue
            try:
                if not vector_values(self.query(rule['name'])):
                    recording_problems.append('no output ' + rule['name'])
            except (ValueError, KeyError, TypeError):
                recording_problems.append('query failed ' + rule.get('name', '<unnamed>'))
        if not recording_rules:
            self.record('Prometheus recording rules', True,
                        'No recording rules configured; N/A', skipped=True)
        else:
            self.record('Prometheus recording rules', not recording_problems,
                        '; '.join(recording_problems) if recording_problems else
                        '%d loaded with output' % len(recording_rules))

    def alertmanager_targets(self):
        response = self.prom_get('/api/v1/alertmanagers')
        if response.get('status') != 'success':
            raise ValueError('Alertmanager discovery API did not return success')
        targets = response['data'].get('activeAlertmanagers', [])
        for host in self.c.get('frontend_hosts', []):
            present = any(self.match_target({'scrapeUrl': target['url'], 'labels': {'job': 'alertmanager'}},
                                           dict(host, job='alertmanager', port=9093)) for target in targets)
            self.record('Alertmanager discovery / ' + host['host'], present)

    def host_coverage(self):
        rc, output = run_command(['runuser', '-u', 'oneadmin', '--', 'onehost', 'list', '--json'])
        if rc:
            raise ValueError('Cannot read onehost list as oneadmin')
        pool = json.loads(output).get('HOST_POOL') or {}
        hosts = pool.get('HOST') or []
        hosts = [hosts] if isinstance(hosts, dict) else hosts
        ids = {str(host['ID']) for host in hosts}
        response = self.query('opennebula_host_state')
        vector_values(response)
        actual = {str(item['metric']['one_host_id']) for item in response['data']['result']}
        self.record('OpenNebula host coverage', ids == actual, 'Missing IDs=%s; unexpected IDs=%s' % (sorted(ids - actual), sorted(actual - ids)))

    def grafana(self):
        base = self.c.get('grafana_url', 'http://127.0.0.1:3000').rstrip('/')
        auth = self.auth.get('grafana', {})
        if not auth:
            self.record('Grafana authenticated checks', False, 'Configure observability_auth.grafana to check datasource and dashboards',
                        skipped=not self.c.get('require_grafana_auth', False))
            return
        all_sources = self.get(base + '/api/datasources', auth)
        sources = [source for source in all_sources if source.get('type') == 'prometheus'
                   and source.get('name') == self.c.get('grafana_datasource', 'prometheus')]
        datasource_problems = []
        if len(sources) != 1:
            datasource_problems.append('expected exactly one matching Prometheus datasource; found %d' % len(sources))
        if len(sources) == 1:
            source = sources[0]
            expected_url = self.c.get('grafana_prometheus_url')
            if expected_url and self.c.get('grafana_require_datasource_url', False):
                actual_url = source.get('url', '').rstrip('/')
                expected_url = expected_url.rstrip('/')
                if actual_url != expected_url:
                    datasource_problems.append('URL=%s; expected=%s' % (actual_url, expected_url))
            uid = urllib.parse.quote(source['uid'], safe='')
            try:
                response = self.get(base + '/api/datasources/proxy/uid/' + uid + '/api/v1/query?query=up', auth)
                readings = vector_values(response)
                if not readings or not all(math.isfinite(v) and v in (0, 1) for v in readings):
                    datasource_problems.append('query up returned no valid series')
            except (ValueError, KeyError, TypeError):
                datasource_problems.append('query through datasource failed')
        self.record('Grafana Prometheus datasource', not datasource_problems,
                    '; '.join(datasource_problems) if datasource_problems else
                    'query up succeeded; datasource UID=%s; URL=%s' % (source['uid'], source.get('url', '')))
        dashboards = self.get(base + '/api/search?type=dash-db&limit=1000', auth)
        by_title = {item.get('title'): item for item in dashboards}
        for title in self.c.get('dashboards', ['OpenNebula', 'Hosts', 'Virtual Machine']):
            item = by_title.get(title)
            if item is None:
                self.record('Grafana dashboard / ' + title, False, 'Dashboard is missing')
                continue
            dashboard = self.get(base + '/api/dashboards/uid/' + urllib.parse.quote(item['uid'], safe=''), auth)
            tree = dashboard.get('dashboard', {})
            panel_sources = []
            def collect_panels(value):
                if isinstance(value, dict):
                    if 'type' in value and value.get('targets'):
                        for target in value['targets']:
                            panel_sources.append(target.get('datasource', value.get('datasource')))
                    for child in value.values():
                        collect_panels(child)
                elif isinstance(value, list):
                    for child in value:
                        collect_panels(child)
            collect_panels(tree.get('panels', []))
            source_uid = sources[0].get('uid') if len(sources) == 1 else None
            problems = []
            if not panel_sources:
                problems.append('No panel queries found')
            for src in panel_sources:
                try:
                    resolved = resolve_datasource(src, tree, all_sources)
                    if resolved != source_uid:
                        problems.append('Datasource UID=%s; expected=%s' % (resolved, source_uid))
                except ValueError as exc:
                    problems.append(str(exc))
            valid = not problems
            self.record('Grafana dashboard / ' + title, valid,
                        ('Panel queries: %d; datasource UID: %s' % (len(panel_sources), source_uid)
                         if valid else '; '.join(dict.fromkeys(problems))))

    def vm(self):
        vm_id = str(self.c['vm_id'])
        if not re.fullmatch(r'[0-9]+', vm_id):
            raise ValueError('vm_id must be a numeric OpenNebula VM ID')
        rc, body = run_command(['runuser', '-u', 'oneadmin', '--', 'onevm', 'show', vm_id, '--json'])
        if rc:
            raise ValueError('VM %s: cannot read OpenNebula VM information' % vm_id)
        vm = json.loads(body)['VM']
        if str(vm['ID']) != vm_id or str(vm['STATE']) != '3' or str(vm['LCM_STATE']) != '3':
            raise ValueError('VM %s: waiting for RUNNING' % vm_id)
        history = vm['HISTORY_RECORDS']['HISTORY']
        history = history if isinstance(history, list) else [history]
        host_id = str(max(history, key=lambda h: int(h['SEQ']))['HID'])
        # KVM DEPLOY_ID is a UUID; the exporter labels the libvirt domain NAME.
        domain = 'one-' + vm_id
        metrics = {
            'opennebula_vm_state': ('opennebula', {'one_vm_id': vm_id}),
            'opennebula_vm_cpu_ratio': ('opennebula', {'one_vm_id': vm_id}),
            'opennebula_libvirt_cpu_seconds_total': ('libvirt', {
                'one_vm_id': vm_id, 'one_host_id': host_id, 'domain': domain}),
            'opennebula_libvirt_memory_total_bytes': ('libvirt', {
                'one_vm_id': vm_id, 'one_host_id': host_id, 'domain': domain}),
        }
        for metric, (exporter, required) in metrics.items():
            response = self.query('%s{one_vm_id=%s}' % (metric, json.dumps(vm_id)))
            values = vector_values(response)
            if not values or not all(math.isfinite(value) for value in values):
                raise ValueError('VM %s: missing or invalid %s' % (vm_id, metric))
            expected_job = self.exporter_config(exporter)['job']
            for sample in response['data']['result']:
                labels = sample.get('metric', {})
                for label, expected in dict(required, job=expected_job).items():
                    if labels.get(label) != expected:
                        raise ValueError('VM %s: %s label %s=%r; expected %r' % (
                            vm_id, metric, label, labels.get(label), expected))
                if not labels.get('instance'):
                    raise ValueError('VM %s: %s missing instance label' % (vm_id, metric))
        return 'VM %s: VM and libvirt metrics present; identity, host, domain and job labels match' % vm_id

    def ha_grafana(self):
        auth = self.auth.get('grafana', {})
        if not auth.get('token') and not (auth.get('username') and auth.get('password')):
            raise ValueError('HA Grafana checks require token or username/password')
        self.grafana()

    def ha_monitoring(self):
        if not self.c.get('expected_targets'):
            raise ValueError('No expected exporter targets available for HA checks')
        self.prometheus()
        self.ha_data['samples'] = {}
        previous = self.c.get('previous_samples', {})
        targets = self.prom_get('/api/v1/targets')['data']['activeTargets']
        for target in self.c['expected_targets']:
            if target['name'] == 'opennebula' and target['host'] != self.c['host']:
                continue
            matches = [t for t in targets if self.match_target(t, target)]
            if len(matches) != 1:
                raise ValueError('Cannot identify HA metric target ' + target['host'] + '/' + target['name'])
            labels = matches[0]['labels']
            selector = '{' + ','.join('%s=%s' % (k, json.dumps(labels[k])) for k in ('job', 'instance')) + '}'
            for metric in ['up'] + target['metrics']:
                response = self.query('timestamp(' + metric + selector + ')')
                values = vector_values(response)
                if not values or not all(math.isfinite(v) for v in values):
                    raise ValueError('Missing sample timestamps: ' + metric + selector)
                for sample in response['data']['result']:
                    key = metric + ':' + json.dumps(sample['metric'], sort_keys=True)
                    stamp = float(sample['value'][1])
                    self.ha_data['samples'][key] = stamp
                    if self.c.get('require_progress') and (key not in previous or stamp <= previous[key]):
                        raise ValueError('Metric samples have not advanced: ' + key)
        if self.c.get('require_progress') and set(previous) != set(self.ha_data['samples']):
            raise ValueError('Metric series changed during HA test; cannot confirm continuity')
        status = self.get(self.c['alertmanager_url'].rstrip('/') + '/api/v2/status', self.auth.get('alertmanager'))
        cluster = status.get('cluster', {})
        if cluster.get('status') != 'ready':
            raise ValueError('Alertmanager cluster is not ready')
        peers = [str(p.get('address', '')).rsplit(':', 1)[0].strip('[]') for p in cluster.get('peers', [])]
        for host in self.c['frontend_hosts']:
            if host['host'] == self.c['host']:
                continue
            addresses = self.addresses(host['aliases'])
            if not any(self.addresses([peer]) & addresses for peer in peers if peer):
                raise ValueError('Alertmanager peer missing: ' + host['host'])
        return 'Endpoints, targets, metric samples and Alertmanager peers verified'

    def execute(self, phase):
        self.check('VM metrics present' if phase == 'vm' else phase + ' checks',
                   {'local': self.local, 'prometheus': self.prometheus, 'vm': self.vm,
                    'ha_monitoring': self.ha_monitoring, 'ha_grafana': self.ha_grafana}[phase])
        return {'changed': False, 'results': self.results, 'expected_targets': self.expected,
                'aliases': self.aliases, 'ha': self.ha_data}


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec={
        'phase': {'type': 'str', 'choices': ['local', 'prometheus', 'vm', 'ha_monitoring', 'ha_grafana'], 'required': True},
        'config': {'type': 'dict', 'required': True},
        'auth': {'type': 'dict', 'default': {}, 'no_log': True},
    }, supports_check_mode=True)
    module.exit_json(**Probe(module.params['config'], module.params['auth']).execute(module.params['phase']))


if __name__ == '__main__':
    main()
