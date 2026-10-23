# VM lifecycle validation

Enable with `validation.run_vm_lifecycle: true` or run the `vm_lifecycle` tag
with that flag enabled. The default is off because this test deliberately
powers off, reboots, and migrates its own disposable VMs.

The primary frontend creates one VM for every monitored KVM host in each
discovered cluster. It exports or reuses the configured marketplace template,
registers the VM name before creation, instantiates it on hold, and forces its
deployment to the selected host. All eligible hosts in a cluster run their
lifecycle probes concurrently, so a destination host may briefly run its own
test VM and an incoming migrated VM. Clusters run sequentially. Each host gets
report rows for deployment, libvirt state, FireEdge VNC, guest readiness, power-off/power-on,
reboot, cold migration, and cleanup. Migration uses `onevm migrate --poff` so
the VM boots on the destination without restoring source-host CPU state. The
check confirms a new guest boot ID and SSH connectivity there. A single-host
cluster reports migration as skipped. A persistent libvirt `paused (starting
up)` state fails the libvirt row with that reason.

The FireEdge VNC row checks the running VM on its source host, before guest
SSH readiness. It authenticates as `oneadmin`, obtains a VM console token, and
opens FireEdge's Guacamole WebSocket through its SSH tunnel to the hypervisor.
The row passes only after a live
Guacamole `sync` instruction arrives. Guacamole connection errors are retried
within `vnc_timeout` while the VM's VNC socket starts. Failure is reported independently, so
the remaining lifecycle checks continue. The shared template must already
provide `GRAPHICS` with VNC type and port; this role does not change it.

The test image must support OpenNebula contextualization, guest root SSH, and
ACPI power actions. The frontend's `oneadmin` SSH key must reach the guest and
KVM hosts. The cluster network must assign an IP reachable from the frontend.

Configure under `validation.vm_lifecycle`:

| Setting | Default | Meaning |
|---|---|---|
| `market_name` | `Alpine Linux 3.21` | Marketplace app exported through the shared template service. |
| `vnet_name` | empty | Existing test vNet; empty uses the cluster-discovered vNet. |
| `deploy_timeout` | `300` | Seconds for OpenNebula deployment and libvirt startup. |
| `guest_timeout` | `180` | Seconds for guest SSH and boot ID after a start. |
| `vnc_timeout` | `30` | Seconds for FireEdge login, console token, and live WebSocket sync. |
| `fireedge_url` | empty | FireEdge HTTP(S) origin; empty uses `127.0.0.1` and the port from `/etc/one/fireedge-server.conf` (fallback `2616`). |
| `fireedge_verify_tls` | `true` | Verify the FireEdge HTTPS certificate. |
| `poweroff_timeout` | `180` | Seconds for graceful power-off. |
| `reboot_timeout` | `180` | Seconds for a new guest boot ID after reboot. |
| `migration_timeout` | `600` | Seconds for cold migration and destination startup. |
| `poll_interval` | `5` | Seconds between state checks. |

The role cleans each VM by its unique name even when a check fails. The final
suite sweep provides a second cleanup attempt and reports any survivor.
