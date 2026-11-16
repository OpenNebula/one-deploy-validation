# Host Reboot validation

Enable flag: `validation.run_host_reboot` (default `false`) · Play tag: `-t host_reboot`

## What it validates

Validates the resilience and recovery of OpenNebula KVM hypervisor nodes following a system reboot. The test ensures that a node can be cleanly isolated, rebooted, and automatically reintegrated into the cluster.

It executes post-reboot health checks by:
1. Cryptographically proving the OS kernel rebooted (via `/proc/sys/kernel/random/boot_id` transition).
2. Verifying that critical virtualization daemons (like `libvirtd`, OVS, and Prometheus exporters) start automatically on boot.
3. Inspecting sthe post-reboot system journal (`journalctl`) to flag underlying hardware, ACPI, or driver errors that occurred during the boot sequence.
4. Confirming that the OpenNebula frontend successfully resumes monitoring the node.

Because this is a destructive test that briefly removes compute capacity, it is disabled by default.

## How it works

1. **Target Selection:** The test runs from the frontend group, iterating over the hypervisors defined in `validation.host_reboot.target_nodes`. Targets map exactly to hostnames in the Ansible `node` inventory group.
2. **Safety Guard:** For each target node, the role queries Libvirt (`virsh list --name`) to see if it is currently hosting the active OpenNebula Frontend VM. If the Frontend is running on the target node, the role safely skips the reboot to prevent a control-plane blackout and records `skipped - hypervisor hosts an OpenNebula Frontend`.
3. **Phase 1 (Pre-Reboot & Isolation):** The pre-reboot `boot_id` is captured. The node is registered in the test manifest (with the list of monitored services attached as details), and it is isolated from OpenNebula scheduling using `onehost offline`.
4. **Phase 2 (Reboot):** The `reboot` module is triggered on the hypervisor. Ansible waits for SSH to return based on the configured timeouts.
5. **Phase 3 (Post-Reboot Verification):**
    * A new `boot_id` is captured to prove a hardware reset occurred.
    * The role uses systemd to verify that all daemons listed in `validation.host_reboot.required_services` are `active`.
    * `journalctl -b 0` is queried for errors, filtering out expected/harmless warnings defined in `journal_exclude_patterns`.
6. **Phase 4 (OpenNebula Reconciliation):** The node is brought back online (`onehost enable` + `onehost forceupdate`). The role polls the frontend until the node's state returns to `MONITORED` (`on`).
7. **Phase 5 (Reporting):** The results of the assertions are evaluated, and an aggregate pass/fail verdict is recorded for the node.

## Requirements

* OpenNebula CLI (`onehost`) accessible on the frontend for `oneadmin`.
* SSH connectivity and `sudo` privileges from the controller to the hypervisor nodes to trigger the reboot and query systemd/journalctl.
* `jq` installed on the frontend (used to parse `onehost list -j`).
* Timeouts calibrated to your hardware: bare-metal servers may take 10+ minutes to clear POST/BIOS memory checks, whereas nested VMs reboot in seconds.

## Configuration

| Option | Default | Description |
|---|---|---|
| `validation.run_host_reboot` | `false` | Enable/disable the entire reboot validation test. |
| `validation.host_reboot.target_nodes` | `"all"` | Which nodes to test. Can be `"all"`, a comma-separated string, or a YAML list of inventory hostnames (e.g., `["host2", "host3"]`). |
| `validation.host_reboot.reboot_timeout` | `600` | Maximum time in seconds to wait for the host to complete the reboot cycle and accept SSH. |
| `validation.host_reboot.connect_timeout` | `10` | Maximum time in seconds for individual SSH polling attempts during the recovery phase. |
| `validation.host_reboot.monitored_wait_retries` | `30` | Number of times to poll OpenNebula to check if the host has reached the `MONITORED` state. |
| `validation.host_reboot.monitored_wait_delay` | `10` | Delay in seconds between OpenNebula monitoring polls. |
| `validation.host_reboot.required_services` | `[{name: 'libvirtd.service', desc: 'Libvirt daemon'}]` | A list of systemd units that must be `active` post-reboot. Each requires a `name` and a `desc` (used for report keys). |
| `validation.host_reboot.check_journal_errors` | `true` | Enables post-reboot inspection of `journalctl` for errors. |
| `validation.host_reboot.journal_priority` | `"err"` | The syslog priority level to query. `err` (3) is standard. |
| `validation.host_reboot.journal_exclude_patterns` | `["ACPI BIOS Error", "pci.*aer"]` | Regex patterns for log entries that should be ignored (e.g., known harmless hardware/firmware warnings). |

## Example

The names supplied to `target_nodes` must match the host definitions under the `node` block in your deployment's inventory YAML:

```yaml
# inventory/<env>/hosts.yaml
node:
  hosts:
    host1: { ansible_host: 192.168.1.31 }
    host2: { ansible_host: 192.168.1.32 }
    host3: { ansible_host: 192.168.1.33 }
```

In your variables file (`group_vars/all.yml`), map those exact names to test specific hosts:

```yaml
validation:
  run_host_reboot: true
  host_reboot:
    target_nodes:
      - "host2"
      - "host3"
    reboot_timeout: 900
    required_services:
      - name: libvirtd.service
        desc: Libvirt daemon
      - name: opennebula-node-exporter.service
        desc: OpenNebula Node Exporter
    check_journal_errors: true
    journal_exclude_patterns:
      - "ACPI BIOS Error"
      - "smartd.*Currently unreadable" # Ignore known bad sectors
```

Run just this test (the cluster discovery, cleanup sweep, and report generation plays are tagged `always` and will still run):

```sh
make I=inventory/<env>/hosts.yaml validation TAGS="host_reboot" ANSIBLE_ARGS="-e @playbooks/report_input_vars.yml"
```

## Report output

Report keys recorded via `verification_result` (comments carry the error detail on failure):

* `[<node>] Node reboot boot_id transition` - `ok`/`failed`. `failed` means the OS kernel did not actually reset (e.g., the reboot command was ignored by a systemd inhibitor).
* `[<node>] <desc> (<name>) active post-reboot` - `ok`/`failed`. One row is generated per service in `required_services`. Failure includes the `ActiveState` and `LoadState`.
* `[<node>] System journal post-reboot error check` - `ok`/`failed`. `failed` lists the number of errors and itemizes the log lines inside the report's `NOTE` block.
* `[<node>] OpenNebula node re-monitoring` - `ok`/`failed`. Proves the `oned` daemon successfully transitioned the host back to `on`.
* `Host reboot validation` - Aggregate summary row: `ok` if all checks across all tested nodes passed. `failed` if any underlying check failed. `skipped` if cluster discovery failed or no valid target nodes were found.
* `Host reboot on <node>` - Usually absent. Appears as `skipped` if the node was safely bypassed because it hosts the OpenNebula Frontend, or `failed` if the Ansible harness itself crashed (e.g., unreachable SSH before the test began).

Badges in the HTML/PDF report follow the shared mapping: values containing `failed`/`error` render **FAIL**, `skipped` values render **WARN** (or **INFO** if safely disabled), and `ok` renders **PASS**.

## Cleanup & failure behavior

* **Manifest:** Each targeted host is registered in the run manifest with type `host_reboot` and disposition `kept`. The final cleanup sweep will *not* attempt to delete the host from OpenNebula, but the cleanup summary table will display it as `kept (reused)`. The `DETAIL` column will list exactly which systemd services were verified during the run.
* **always block (unconditional restore):** If a reboot times out or a service check fails, the playbook guarantees the node is not left isolated. It unconditionally executes `onehost enable` and `onehost forceupdate` for the target host ID.
* **Leftovers:** If the `always` block fails to re-enable the host, it records the failure via `record_leftover` (type `host_state`), which surfaces as a red `LEFTOVER` in the Execution & Cleanup Summary.
* **rescue:** Catches harness bugs (e.g., Jinja parsing errors or CLI command crashes). It records a harness failure for the specific node and proceeds to the next node or role without aborting the play.
