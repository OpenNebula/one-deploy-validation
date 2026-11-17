"""Group monitoring results for presentation; keep raw host facts intact."""


SECTIONS = [
    ('services', '1. Endpoints readiness'),
    ('frontend', '2. Frontend metrics collection'),
    ('kvm', '3. KVM metrics collection'),
    ('integration', '4. Observability functionality and recovery'),
]

CHECK_LABELS = {
    'Alertmanager discovery': 'Prometheus discovers the configured Alertmanager endpoint',
    'Alertmanager loaded configuration': 'Alertmanager configuration is loaded and readable',
    'OpenNebula host coverage': 'All OpenNebula hosts have monitoring metrics',
    'Prometheus loaded configuration': 'Prometheus configuration was successfully loaded',
    'Prometheus alerting rules': 'Prometheus alerting rules are loaded successfully',
    'Prometheus recording rules': 'Prometheus recording rules are loaded successfully',
    'Grafana Prometheus datasource': 'Grafana successfully queries Prometheus datasource',
    'Grafana dashboard / Hosts': 'Grafana Hosts dashboard is available and configured',
    'Grafana dashboard / OpenNebula': 'Grafana OpenNebula dashboard is available and configured',
    'Grafana dashboard / Virtual Machine': 'Grafana Virtual Machine dashboard is available and configured',
    'VM metrics present': 'VM is instantiated and correct metrics collected',
    'Alert delivery pipeline': 'Test alert reaches the receiver',
    'Prometheus and Alertmanager leader failover': 'Monitoring remains available through VIP after leader failover',
    'Prometheus and Alertmanager recovery': 'Monitoring recovers after leader services restart',
    'Grafana restart': 'Grafana restarts and integration checks pass',
}


# Presentation order is independent of labels and task execution order.
CHECK_ORDER = {name: position for position, name in enumerate([
    'Validation prerequisites',
    'Prometheus readiness',
    'Alertmanager readiness',
    'Grafana readiness',
    'Prometheus loaded configuration',
    'Prometheus alerting rules',
    'Prometheus recording rules',
    'Alertmanager discovery',
    'Alertmanager loaded configuration',
    'OpenNebula host coverage',
    'VM metrics present',
    'Alert delivery pipeline',
    'Prometheus and Alertmanager leader failover',
    'Prometheus and Alertmanager recovery',
    'Grafana Prometheus datasource',
    'Grafana dashboard / Hosts',
    'Grafana dashboard / OpenNebula',
    'Grafana dashboard / Virtual Machine',
    'Grafana restart',
])}

EXPORTER_NAMES = {'node': 'opennebula-node-exporter', 'opennebula': 'opennebula-exporter',
                  'mysql': 'opennebula-mysql-exporter', 'libvirt': 'opennebula-libvirt-exporter',
                  'lvm': 'opennebula-lvm-exporter', 'smartctl': 'opennebula-smartctl-exporter',
                  'ovs': 'opennebula-ovs-exporter'}


def observability_report(results, comments=None, frontends=None, nodes=None):
    comments = comments or {}
    output = dict(results)
    notes = dict(comments)
    groups = {}
    labels = {}
    categories = {}
    frontends, nodes = set(frontends or []), set(nodes or [])
    prefix = 'Observability / '
    for key, value in results.items():
        if not key.startswith(prefix):
            continue
        parts = key.split(' / ')
        host = parts[1]
        subject = host
        name = ' / '.join(parts[2:])
        output.pop(key)
        notes.pop(key, None)
        if name in ('opennebula-prometheus service', 'opennebula-alertmanager service', 'grafana-server service'):
            continue
        if len(parts) == 4 and parts[3].endswith(' target'):
            subject = parts[2]
            name = parts[3]
            host = '%s → %s' % (host, parts[2])
        elif name.startswith('Alertmanager discovery / '):
            host = '%s → %s' % (host, parts[-1])
            name = 'Alertmanager discovery'
        elif name.endswith(' probe coverage'):
            host = '%s → %s' % (host, name[:-len(' probe coverage')])
            name = 'Probe coverage'
        if name in ('inventory topology', 'Probe coverage', 'probe execution', 'local checks'):
            if value == 'ok':
                continue
            category, name = 'services', 'Validation prerequisites'
        elif name in ('Prometheus readiness', 'Alertmanager readiness', 'Grafana health',
                      'opennebula-prometheus service', 'opennebula-alertmanager service', 'grafana-server service'):
            category = 'services'
            name = 'Grafana readiness' if name == 'Grafana health' else name
        elif name.endswith(' exporter') or name.endswith(' target'):
            category = 'kvm' if subject in nodes else 'frontend' if subject in frontends else 'integration'
            exporter = name.rsplit(' ', 1)[0]
            name = EXPORTER_NAMES.get(exporter, exporter) + ' metrics collection'
        elif name.startswith('Grafana'):
            category = 'integration'
        elif name.startswith(('Prometheus', 'Alertmanager', 'OpenNebula host coverage', 'prometheus checks')):
            category = 'integration'
        else:
            category = 'integration'
        if name in ('Grafana service stop-start', 'Grafana service recovery'):
            name = 'Grafana restart'
        group_key = prefix + category + ' / ' + name
        groups.setdefault(group_key, []).append((host, str(value), comments.get(key, '')))
        labels[group_key] = CHECK_LABELS.get(name, name)
        categories[group_key] = category
    for key, members in groups.items():
        failed = [host for host, value, _ in members
                  if any(token in value.lower() for token in ('failed', 'error', 'unreachable'))
                  and not value.lower().startswith('skipped')]
        skipped = [host for host, value, _ in members if value.lower().startswith('skipped')]
        if failed:
            output[key] = 'failed'
            notes[key] = ', '.join(dict.fromkeys(failed))
        elif skipped:
            output[key] = ('skipped — not evaluated' if len(skipped) == len(members)
                           else 'warning — partially evaluated')
        elif all(value == 'ok' for _, value, _ in members):
            output[key] = 'ok'
        else:
            output[key] = 'warning — incomplete results'
        if not failed and output[key] != 'ok':
            notes[key] = '; '.join(dict.fromkeys(
                '%s: %s' % (host, detail or value)
                for host, value, detail in members if value != 'ok'))
    sections = [{'id': category, 'title': title,
                 'checks': [(key, output[key]) for key in sorted(groups, key=lambda k: (
                     CHECK_ORDER.get(k.split(' / ', 2)[2], len(CHECK_ORDER)), labels[k]))
                            if categories[key] == category]}
                for category, title in SECTIONS]
    return {'results': output, 'comments': notes, 'labels': labels, 'sections': sections}


class FilterModule:
    def filters(self):
        return {'observability_report': observability_report}
