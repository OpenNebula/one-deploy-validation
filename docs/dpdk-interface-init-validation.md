# DPDK Interface Initialization Validation

The `dpdk-interface-init-validation` role validates that DPDK interfaces
declared in the deployment inventory have successfully initialized and are
attached to the running Open vSwitch userspace datapath.

The validation is read-only. It does not modify Open vSwitch, restart
services, bind PCI devices, or change interface configuration.

## Enable the test

The test is disabled by default.

Enable it in the validation configuration:

```yaml
validation:
  run_dpdk_interface_init_validation: true
```

The validation can be run independently with:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags dpdk_interface_init_validation
```

## Objective

A DPDK interface can exist in OVSDB with the expected configuration while
the underlying DPDK device failed to initialize.

Examples include:

- PCI device attachment failure.
- Invalid DPDK device arguments.
- Device already in use.
- Hugepage or memory allocation failure.
- DPDK initialization failure.
- Interface configuration failure.
- Interface remaining in OVSDB with an invalid OpenFlow port.
- Interface existing in OVSDB but missing from the runtime datapath.

A configuration-only validation could therefore report a false positive.

This role correlates configured state with actual runtime state.

## Source of truth

The expected DPDK interfaces are derived directly from the deployment
inventory.

An interface is considered an expected DPDK interface when:

- Its OVS interface `type` begins with `dpdk`.
- It defines `options:dpdk-devargs`.

For example:

```yaml
ovs:
  iface:
    dpdk0:
      set:
        - type: dpdk
        - options:dpdk-devargs: '0000:01:00.0'

    dpdk1:
      set:
        - type: dpdk
        - options:dpdk-devargs: '0000:02:00.0'
```

No separate list of interfaces or PCI addresses is required by the
validation role.

## Runtime validation

For every inventory-defined DPDK interface, the role validates the current
runtime state.

The checks include:

- The global OVS DPDK subsystem reports `dpdk_initialized=true`.
- The OVS Interface object exists.
- The runtime interface type matches the inventory.
- The runtime `dpdk-devargs` matches the inventory.
- `Interface.error` is empty.
- The OpenFlow port number is greater than zero.
- The interface is present in `ovs-appctl dpif/show`.
- The datapath reports the interface as a DPDK device.
- The datapath attachment uses the expected `dpdk-devargs`.

A healthy runtime interface typically has state equivalent to:

```text
name        : dpdk0
type        : dpdk
options     : {dpdk-devargs="0000:01:00.0"}
error       : []
admin_state : up
link_state  : up
ofport      : 2
status      : {...}
```

and is also present in the userspace datapath:

```text
dpdk0 2/3: (dpdk: dpdk-devargs=0000:01:00.0, ...)
```

The `status` field is included in the validation details as runtime evidence.
It may contain information such as the DPDK driver, queue counts, link speed,
NUMA node and DPDK port number.

These informational status values are not independently used as failure
criteria.

## Convergence window

A validation can run while Open vSwitch is still initializing DPDK devices,
particularly immediately after deployment or boot.

To avoid false failures, an unhealthy interface is observed multiple times
before being classified as persistently failed.

The default behavior is:

```yaml
dpdk_interface_init_retries: 6
dpdk_interface_init_delay: 5
```

A healthy interface returns immediately and does not wait for the remaining
retries.

With the defaults, a still-unhealthy interface is observed for approximately
25 seconds between the first and final check.

These values are role defaults and can be overridden through normal Ansible
variable precedence if a deployment requires a longer initialization window.

## Datapath validation

OVSDB represents configured state.

The runtime userspace datapath is inspected independently with:

```bash
ovs-appctl dpif/show
```

This distinction is important.

For example, OVSDB may contain:

```text
name    : dpdk0
type    : dpdk
options : {dpdk-devargs="0000:01:00.0"}
```

while runtime initialization has failed:

```text
error  : "Error attaching device '0000:01:00.0' to DPDK"
ofport : -1
```

and `dpdk0` is absent from:

```bash
ovs-appctl dpif/show
```

This condition is reported as a failure even though the expected OVSDB
configuration is present.

## Log correlation

The role inspects OVS-related logs from the current boot.

Sources include:

- The platform Open vSwitch systemd service.
- `ovs-vswitchd.service`.
- `opennebula-ovs.service`.
- `/var/log/openvswitch/ovs-vswitchd.log`, when available.

Relevant initialization and attachment messages are correlated with each
configured DPDK interface using the interface name and expected
`dpdk-devargs`.

Examples of relevant errors include:

```text
Driver cannot attach the device
Failed to attach device
Error attaching device
could not set configuration
Failed to bind PCI device
Failed to add port
already in use
invalid devargs
hugepage allocation errors
memory allocation errors
```

## Transient versus persistent errors

Historical log errors do not automatically cause validation failure.

DPDK initialization during startup can temporarily generate attachment errors
before device binding and network configuration have completed.

The current runtime state is authoritative.

The role uses the following classification model:

```text
current runtime unhealthy
        |
        v
persistent_runtime_failure
```

```text
current runtime healthy
        +
earlier initialization errors
        +
later successful attachment
        |
        v
recovered_transient
```

```text
current runtime healthy
        +
historical errors
        +
no explicit later attachment message
        |
        v
historical_errors_runtime_healthy
```

```text
current runtime healthy
        +
no matching errors
        |
        v
clean
```

A `recovered_transient` classification is not a validation failure.

This prevents known startup convergence behavior from producing false
positives.

## Persistent failure

An interface is classified as a persistent runtime failure when it remains
unhealthy after the configured convergence window.

Possible failure reasons include:

```text
DPDK subsystem is not initialized

OVS Interface object does not exist

type expected dpdk, got <actual-type>

dpdk-devargs expected <expected>, got <actual>

OVS Interface.error=<error text>

invalid OpenFlow port -1

interface is not attached to the OVS datapath as DPDK

datapath attachment does not use expected dpdk-devargs

recent initialization error: <log text>
```

Relevant current-boot log text is included when available.

## Per-interface results

Each configured DPDK interface receives an independent validation result.

Result keys use:

```text
DPDK interface initialization <interface> on <hostname>
```

For example:

```text
DPDK interface initialization dpdk0 on hypervisor01
```

The result is:

```text
ok
```

or:

```text
failed
```

The associated report details include:

- Expected interface type.
- Expected `dpdk-devargs`.
- Number of runtime observations.
- Global DPDK initialization state.
- OVSDB interface existence.
- Actual interface type.
- Actual `dpdk-devargs`.
- `Interface.error`.
- Administrative state.
- Link state.
- OpenFlow port.
- OVS `status`.
- Datapath attachment state.
- Datapath `dpdk-devargs` match.
- Matching datapath line.
- Log classification.
- Matching journal error count.
- Matching successful attachment count.
- Last relevant error.
- Last successful attachment.
- Recent relevant errors.
- Validation failure reasons.

## Host summary

In addition to the individual interface results, one host-level summary is
recorded using:

```text
DPDK interface initialization on <hostname>
```

The host summary is `failed` if any configured DPDK interface fails.

The summary also contains:

- OVS service state.
- Global DPDK startup error context.
- Per-interface validation details.
- Aggregated interface failures.

## Runtime commands

The role uses read-only commands equivalent to:

```bash
ovs-vsctl get Open_vSwitch . dpdk_initialized

ovs-vsctl get Interface <interface> name
ovs-vsctl get Interface <interface> type
ovs-vsctl get Interface <interface> options:dpdk-devargs
ovs-vsctl get Interface <interface> error
ovs-vsctl get Interface <interface> admin_state
ovs-vsctl get Interface <interface> link_state
ovs-vsctl get Interface <interface> ofport
ovs-vsctl get Interface <interface> status

ovs-appctl dpif/show

systemctl is-active <ovs-service>
systemctl show <ovs-service>

journalctl -b -u <ovs-service>
journalctl -b -u ovs-vswitchd.service
journalctl -b -u opennebula-ovs.service
```

When available, the role also reads:

```text
/var/log/openvswitch/ovs-vswitchd.log
```

No command used by the validation role changes host state.

## Non-DPDK hosts

If no inventory-defined DPDK interfaces are present, the validation is
recorded as skipped.

This allows the role to execute safely across a node group containing both
DPDK and non-DPDK hypervisors.

## Validation after reboot

The validation uses current runtime state and logs from the current boot.

It can therefore be run after a host reboot to verify that:

- OVS DPDK initialized successfully.
- Configured DPDK interfaces initialized again.
- Interfaces attached to the userspace datapath.
- Startup errors recovered successfully or remained persistent.

The validator does not modify the host during this check.

## Scope

This validation covers:

- Global OVS DPDK initialization state.
- Inventory-derived DPDK interfaces.
- OVSDB interface existence.
- DPDK interface type.
- DPDK device arguments.
- OVS interface initialization errors.
- OpenFlow port initialization.
- Runtime userspace datapath attachment.
- Runtime datapath device arguments.
- Current-boot initialization errors.
- Successful attachment messages.
- Persistent versus recovered startup failures.
- Per-interface validation results.
- Host-level summary result.

This validation does not cover:

- PCI driver binding correctness.
- DPDK PCI allowlist persistence.
- OVS bridge topology.
- Bond membership.
- PMD CPU placement.
- NUMA locality.
- Throughput or latency benchmarking.

Those concerns are validated by separate validation tasks.
