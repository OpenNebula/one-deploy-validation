# Observability development and local tests

Ansible owns HTTP readiness/config checks, leader discovery, VIP checks, service
operations, VM cleanup and the complete alert transaction/recovery. Python handles
metric parsing, matching RAFT members and discovered Alertmanagers to inventory hosts,
target/label matching, dashboard references, report grouping and
the small HTTP webhook receiver.

```bash
make test-observability
make test-observability-endpoints
make test-observability-vm-cleanup
make test-observability-ha
hatch env run -e validation-default -- python3 test/observability/alert_integration.py
```

These use local fixtures, not live cloud service changes. Also run the full baseline
fixture in the same environment:

```bash
hatch env run -e validation-default -- python3 test/observability/integration.py ansible-playbook
```

Use the project's supported Python/Ansible versions from `pyproject.toml`.
Run these commands from the repository root; the fixture runs do not require a lab.

A real SA run covers VM/alert delivery; a real
HA run also covers per-frontend alert delivery, service failover/recovery and
Grafana. Neither substitutes for the other.

Role-private facts and task inputs use `_obs_*`. Shared cross-play findings and
configuration use `odv_observability_*`; public inventory settings remain
`observability*`. Exporter selection has one source of truth: role defaults merged
with inventory overrides.

Operator configuration and recovery instructions: [docs/observability.md](../../docs/observability.md).
