# Host Monitoring Recovery After Reboot

The `host_reboot` test sets one hypervisor per validated OpenNebula cluster
offline, reboots it, enables it again, and checks that OpenNebula reports it
as MONITORED. It is opt-in because it interrupts running VMs on the selected
host. It does not evacuate them. OpenNebula stops monitoring while the host
is offline and resumes after it is enabled.

## Enable and run

```yaml
validation:
  run_host_reboot: true
  host_reboot:
    reboot_timeout: 600
    monitoring_timeout: 600
    poll_interval: 10
```

Run the full suite with `make I=inventory/<env>/hosts.yml validation`, or run
this test with `make I=inventory/<env>/hosts.yml T=host_reboot validation`.
When selected alone, the test only needs cluster membership and monitored
hosts; the discovery step does not require a datastore or vnet in that cluster.
The three time settings are in seconds. `reboot_timeout` is passed to the
Ansible reboot module; `monitoring_timeout` and `poll_interval` bound the
subsequent OpenNebula polling window. All three must be positive.

## Selection and validation

For each discovered cluster, the test lists OpenNebula hosts and considers
only hosts in MONITORED state (`STATE=2`) whose `TEMPLATE.HOSTNAME` maps
uniquely to a node inventory host. It probes their Ansible connections and
selects the reachable host with the lowest numeric OpenNebula ID. If none is
eligible, the cluster fails without rebooting a node.

The test runs `onehost offline` before `ansible.builtin.reboot`. After the
node reconnects, it runs `onehost enable` and polls `onehost show -j` until
OpenNebula reports `STATE=2` (MONITORED). If the reboot fails, the test still
attempts `onehost enable` so the host is not left administratively offline.
If setting the host offline fails, it skips the reboot and still attempts to
enable the host. This state check does not use a monitoring-history timestamp.

Results are recorded as `Host reboot: monitoring recovery` with a cluster
suffix when multiple clusters are validated. Comments identify the host and
show the offline, reboot, enable, and final state outcomes. On a reboot or recovery
failure, later clusters are marked skipped and VM HA is skipped for that run.
The report still renders and includes the failure. If the node remains down or
OpenNebula leaves it in ERROR or OFFLINE, restore it using the site's normal
operator procedure before another disruptive test.

This test checks that monitoring resumes after the host is explicitly enabled
following a normal reboot, the section 7 recovery scenario. It does not run
the broader post-reboot network, storage, DPDK, or VM checks in section 8.
