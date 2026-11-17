# Observability Validation

Set `validation.run_observability: true` to run the checks in [the main validation playbook](../playbooks/validation.yml). The default is `false`.
Play tag: `observability`. The reference configuration is
[`inventory/reference/group_vars/all.yml`](../inventory/reference/group_vars/all.yml).
It lists the supported inventory settings, including commented technical overrides.
Implementation defaults are in
[`roles/observability_validation/defaults/main.yml`](../roles/observability_validation/defaults/main.yml).

## What it validates

The baseline observes an existing deployment: Prometheus, Alertmanager and Grafana
endpoints, exporter metrics, the expected Prometheus target set, sample freshness,
alerting/recording rules, Grafana datasource queries and dashboard datasource references.
It does not restart services or create resources. Optional scenarios independently
create a test VM, deliver a test alert, stop leader services or restart Grafana.

## Requirements

- A deployed observability stack and SSH access with privileges to read its configuration.
- Correct frontend, node and Grafana groups and reachable per-host API endpoints.
- Grafana credentials for datasource and dashboard checks.
- For the VM scenario: an existing ready-to-use template and capacity to instantiate it.
- For alert delivery: all frontend peers reachable, `promtool` / `amtool` installed,
  and permission to update and reload monitoring configuration temporarily.
- For HA failover: at least three frontends, a configured VIP and permission to stop
  leader services. Grafana restart is independently enabled.

## Enable and run

Declare the `frontend`, `node` and `grafana` inventory groups. Grafana may run on
one, several or all frontends, or on dedicated hosts. Host counts need not match.
Each Grafana instance is checked with its own endpoint and credentials.

```yaml
validation:
  run_observability: true

observability:
  prometheus_url: "http://{{ ansible_host }}:9090"
  alertmanager_url: "http://{{ ansible_host }}:9093"
  grafana_url: "http://{{ ansible_host }}:3000"
```

Run from the repository root using your environment's inventory:

```bash
make validation I=inventory/<env>/hosts.yml
```

Monitoring runs after common discovery/preclean and before the other tests restart
services or fence hosts. All enabled suite roles still run. To select only monitoring
plays, use `T=observability`; tasks tagged `always`, including shared cleanup, may
still run. Selecting the tag alone does not enable `validation.run_observability`.

## Configuration

User settings belong at inventory top level, except the master switch
`validation.run_observability`. Do not place credentials under `validation`:
the standard report includes that dictionary. Host-specific settings can be placed
in `host_vars`.

| Variable | Default / purpose |
|---|---|
| `frontend_group`, `node_group`, `grafana_group` | `frontend`, `node`, `grafana`; inventory group names |
| `observability_aliases` | `[]`; extra names/IPs identifying the current host |
| `observability.require_grafana` | `true`; require a Grafana group |
| `observability.require_grafana_auth` | `true`; fail authenticated checks when credentials are missing |
| `observability.prometheus_url` | `http://127.0.0.1:9090`; reference uses `ansible_host` |
| `observability.alertmanager_url` | `http://127.0.0.1:9093`; reference uses `ansible_host` |
| `observability.grafana_url` | `http://127.0.0.1:3000`; reference uses `ansible_host` |
| `observability.timeout` | `5` seconds per HTTP request |
| `observability.attempts` | `3` total attempts, including the initial request; Python requests cap at 10 |
| `observability.delay` | `2` seconds between attempts |
| `observability.max_sample_age` | `120` seconds; allow for your scrape interval |
| `observability.validate_certs` | `true`; verify HTTPS certificates |
| `observability.grafana_datasource` | `prometheus`; expected datasource name |
| `observability.dashboards` | `[OpenNebula, Hosts, Virtual Machine]`; expected dashboard titles |
| `observability.grafana_require_datasource_url` | `false`; compare datasource URL when enabled together with `grafana_prometheus_url` |
| `observability.grafana_prometheus_url` | No default; expected datasource URL for the strict check |
| `observability.expected_exporters` | Exporter sets per `frontend` / `node` role |
| `observability.additional_exporters` | `[]`; names added to the default sets, e.g. `[ovs]` |
| `observability.exporters` | Exporter definitions, described below |

### Exporters, targets and metrics

Default sets target OpenNebula 7.4. For other releases, override the sets explicitly;
the role does not automatically select exporters by version or detect OVS.

| Exporter key | Port | Prometheus job | Required metrics |
|---|---|---|---|
| `node` | 9100 | `node_exporter` | `node_uname_info`, `node_memory_MemTotal_bytes` |
| `opennebula` | 9925 | `opennebula_exporter` | `opennebula_host_total` |
| `libvirt` | 9926 | `libvirt_exporter` | `opennebula_libvirt_daemon_up` |
| `mysql` | 9104 | `mysql_exporter` | `mysql_up` |
| `lvm` | 9845 | `lvm_exporter` | No resource metrics required by default |
| `smartctl` | 9633 | `smartctl_exporter` | No resource metrics required by default |
| `ovs` | 9475 | `ovs_exporter` | Optional; no resource metrics required by default |

Frontend set: `[node, opennebula, mysql, smartctl]`.
Node set: `[node, libvirt, lvm, smartctl]`.
SMART/OVS are excluded from direct checks and expected targets on frontend-only
hosts by default; on KVM nodes they are included when selected.
OpenNebula exporter targets are checked locally on each frontend.

Each exporter definition supports `port`, `job`, `metrics`, an optional `url`
(default `http://127.0.0.1:<port>/metrics`) and `scrape` to override participation.
`scrape: false` disables both the direct exporter check and its expected target.
The `unit` field documents the corresponding service name; baseline checks do not
validate its systemd enabled/running state. The full reference lists all unit names.
Overrides under `observability.exporters` merge recursively with defaults.

`additional_exporters` deduplicates names and applies to frontend/node hosts in
its inventory scope; place it in node group vars to add exporters only on KVM hosts. A new custom name also needs a definition
with `port`, `job` and `metrics`. Unknown or incomplete definitions fail validation.
Empty `metrics` still checks valid exposition, collector errors where exposed,
Prometheus target presence, UP state and freshness of `up`; it does not prove every
physical disk, volume group or OVS port has metrics.

Targets are matched by job, endpoint port and inventory aliases. Missing, duplicate,
unexpected or stale exporter targets fail. Unrelated Prometheus jobs are ignored.
Required samples must be finite and recent. MySQL/libvirt health metrics must be 1.
Baseline checks do not exhaustively verify all labels or generate load.

Alerting rules must exist and all loaded alerting rules must be healthy. All loaded
recording rules and their outputs are checked; no recording rules is reported as
N/A/WARN. No inventory list of required rule names is needed.

### Authentication

Grafana credentials are configured under `observability_auth.grafana` using
`token` or `username` / `password`. Use Vault or environment lookups for secrets.
Prometheus, Alertmanager and exporter endpoints need no credentials in the default
one-deploy setup. Their optional `observability_auth.<service>` mappings configure
HTTP client credentials only when the endpoint or its reverse proxy requires them;
they do not enable server-side authentication. Use the service name (`prometheus`,
`alertmanager`) or exporter key. Supplying an explicit auth mapping replaces the
role's default mapping, so include Grafana credentials if its checks are enabled.

Grafana defaults read `GRAFANA_TOKEN`; a nonempty token selects Bearer auth.
Otherwise `GRAFANA_USER` / `GRAFANA_PASSWORD` select Basic Auth, with `admin` / `admin`
fallback for labs. An invalid token does not fall back to a password. Separate
Grafana installations generally require separate tokens; use per-host auth or a
common username/password configured on those instances. No accounts are created.

Credentials are hidden in Ansible module arguments. HTTP redirects are rejected.
Grafana datasource checks query Prometheus through Grafana and validate dashboard
references, including saved datasource variables. They do not test browser rendering.

## Optional VM metrics scenario

```yaml
observability_vm:
  enabled: true
  template_id: 0
```

Specify a numeric `template_id`. Each run creates a new VM so that the check
verifies discovery of a newly created monitoring object. Existing `vm_id` input
is not supported. The template must already be ready to instantiate;
no Marketplace download or template creation is performed. The guest needs no
Internet access or workload generator. The scenario waits for RUNNING and checks
`opennebula_vm_state`, `opennebula_vm_cpu_ratio`, libvirt CPU and memory metrics,
including `one_vm_id`, `job`, `instance`, libvirt `domain=one-<VM ID>` and the current
`one_host_id`. It checks presence and identity, not updates under generated load.

`observability_vm.retries` / `delay` override waiting (defaults 30 / 10 seconds).
`observability_vm_defaults` contains all technical timings: `retries: 30`, `delay: 10`,
`cleanup_retries: 12`, `cleanup_delay: 5`. Override that complete mapping if changing
cleanup timing; `observability_vm.cleanup_retries` is not a supported override.

The unique VM name is registered before creation. Cleanup runs in `always`, including
partial creation or an unparseable returned ID, and verifies that the VM is absent
or DONE. The source template is registered as reused and kept.
The full suite's final sweep retries cleanup. Interruption or lost connectivity can delay cleanup
until a later run; the persisted manifest retains resource ownership.

## Optional alert delivery scenario (each frontend)

```yaml
observability_alert:
  enabled: true
```

One Ansible coordinator prepares a loopback HTTP receiver and an isolated test route
on every Alertmanager in the frontend inventory. It then tests each enabled,
selected Prometheus source sequentially: one in SA, multiple in HA. Each source
uses the transaction's unique alert ID and its own `odv_source` label.

Prometheus keeps its deployed alert destinations, including delivery to all HA
Alertmanagers. The scenario verifies firing in the source Prometheus, arrival at
every discovered destination and an actual webhook received by at least one
cluster receiver. It does not inject alerts directly into Alertmanager or require
one notification per peer: HA deduplication may choose any receiver.

API URLs must address the individual frontend, not the VIP. Every discovered
Alertmanager must map uniquely to a prepared frontend; unknown destinations fail
before a test rule is installed. Include the complete frontend inventory and make
all peers reachable, even when only some sources have the scenario enabled.
The configured rule file must already be included by Prometheus. Configuration
is validated before reload; service restarts are not required.

Original files are backed up on each frontend and restored byte for byte. The
source rule is cleared before the next source starts; test routes are removed only
after no test alert remains on any Alertmanager. Receivers, temporary files and
owned locks are removed after successful cleanup. If cleanup cannot be confirmed,
subsequent sources are skipped and recovery files/isolated routes are retained as
needed. Externally edited configuration is not overwritten. The log records backup
paths and manual recovery instructions; the report identifies failed hosts.

The lock is `<alertmanager_config>.odv-lock`. After an interrupted run, inspect the
retained `odv-alert-*` directory under `backup_dir`: restore source rules and reload
Prometheus, confirm test alerts are resolved, then restore Alertmanager configuration
and reload it before removing the owned lock and backup directory. Receivers have
a bounded lifetime; creating `stop` in their transaction directory requests shutdown.
These resources are managed by the scenario's `always` tasks, not the generic sweep.

| `observability_alert` option | Default |
|---|---|
| `enabled` | `false` |
| `wait_timeout`, `poll_interval` | `180`, `2` seconds |
| `prometheus_config` | `/etc/one/prometheus/prometheus.yml` |
| `rules_file` | `/etc/one/prometheus/rules.yml` |
| `alertmanager_config` | `/etc/one/alertmanager/alertmanager.yml` |
| `prometheus_service`, `alertmanager_service` | `opennebula-prometheus`, `opennebula-alertmanager` |
| `promtool`, `amtool` | `/usr/bin/promtool`, `/usr/bin/amtool` |
| `backup_dir` | `/var/tmp` |

`prometheus_url`, `alertmanager_url`, `timeout`, `validate_certs` inherit the effective
`observability` values and can also be overridden for the alert scenario.

## Optional monitoring HA and Grafana restart

```yaml
observability_ha:
  enabled: true
  vip: 192.0.2.100  # Replace with the real deployment VIP.
observability_grafana_restart:
  enabled: true
```

Leader failover requires at least three frontends and the complete inventory.
It checks the baseline, discovers the leader, stops Prometheus/Alertmanager/oned,
requires a different leader and moved VIP, and checks targets, progressing metric
samples and Alertmanager peers. It then restores services and checks recovery.
It neither reboots a host nor forces the original leader back into leadership.
A service stop ending in `failed` instead of `inactive` fails the scenario and
triggers recovery; a later successful recovery does not erase that failure.

| `observability_ha` option | Default / purpose |
|---|---|
| `enabled` | `false` |
| `vip` | Required when enabled; deployment VIP |
| `zone` | `OpenNebula`; zone name/ID accepted by `onezone show` |
| `retries`, `delay`, `recovery_retries` | `36`, `5` seconds, `60`; retries after initial attempt |
| `grafana_vip` | `auto`; check Grafana through VIP when every frontend belongs to Grafana; also accepts `true` / `false` |
| `vip_scheme` | `http` |
| `prometheus_port`, `alertmanager_port`, `grafana_port` | `9090`, `9093`, `3000`; VIP endpoint ports |
| `oned_service` | `opennebula.service` |
| `prometheus_service` | `opennebula-prometheus.service` |
| `alertmanager_service` | `opennebula-alertmanager.service` |

Grafana stop/start is independently enabled and runs sequentially on every host
in the Grafana group, including dedicated instances. It checks service stop and
endpoint unavailability, starts the service, then checks health, datasource and
dashboards. Options under `observability_grafana_restart`: `enabled: false`,
`service: grafana-server.service`, `retries: 36`, `delay: 5`. If leader failover or
recovery failed in the same run, Grafana fault injection is skipped. No further
Grafana hosts are stopped after a failure. Recovery is attempted for the affected host.

## Results and cleanup

The shared HTML/PDF report has one Observability block: Endpoints readiness, Frontend
metrics collection, KVM metrics collection and Observability functionality and recovery.
Results are combined across hosts; PASS shows `ok`, failure notes identify failed hosts,
and warnings include reasons. Detailed findings remain in Ansible output.

Execution & Cleanup Summary includes Observability. Temporary VMs appear in
the normal manifest/sweep tables; alert recovery is handled separately as described
above. Successfully removed resources are excluded from
the final LEFTOVER list even if an earlier cleanup attempt failed; the original
failed check remains as history. The full suite's exit behavior is unchanged:
`failed=0` in PLAY RECAP does not mean every validation passed when rescue handled
failures. Inspect the report.

Developer tests and implementation notes are in
[`test/observability/README.md`](../test/observability/README.md).
