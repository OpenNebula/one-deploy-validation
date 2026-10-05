# OVS Topology Validation

The `ovs-topology-validation` role validates that the Open vSwitch topology
running on each hypervisor matches the topology declared in the deployment
inventory.

The validation is read-only. It does not modify Open vSwitch configuration.

## Enable the test

The test is disabled by default.

Enable it in the validation configuration:

```yaml
validation:
  run_ovs_topology_validation: true
```

The validation can be run independently with:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags ovs_topology_validation
```

## Source of truth

Expected topology is derived directly from the host's deployment inventory.

For example:

```yaml
ovs:
  br:
    ovsbr0:
      set:
        - datapath_type: netdev
      ports:
        - bond0

  bond:
    bond0:
      ifaces:
        - eth0
        - eth2
      set:
        - bond_mode: balance-slb

  iface:
    eth0:
      set:
        - type: dpdk
        - options:dpdk-devargs: '0000:19:00.0'

    eth2:
      set:
        - type: dpdk
        - options:dpdk-devargs: '0000:5e:00.0'
```

No duplicate topology configuration is required by the validation role.

## Bridge validation

For each inventory-defined OVS bridge, the role verifies:

- The bridge exists.
- Its configured `datapath_type` matches the inventory.
- Every inventory-defined port exists on the bridge.

Additional runtime ports are allowed.

This is required because OpenNebula dynamically creates ports for virtual
machines. A runtime bridge can therefore legitimately contain ports that are
not part of the static deployment inventory.

For example, with:

```yaml
ovs:
  br:
    ovsbr0:
      ports:
        - bond0
```

a runtime bridge containing both:

```text
bond0
nfhonfedv6-nic0
```

is valid. The VM interface is not treated as unexpected topology.

## Port validation

For every port referenced through `ovs.br.<bridge>.ports`, the role verifies:

- The port exists.
- The port is attached to the expected bridge.

Bridge membership is derived from:

```text
ovs.br.<bridge>.ports
```

The `ovs.port` configuration dictionary is not interpreted as additional
bridge membership because it may contain configuration for an internal bridge
port.

## Bond validation

For every inventory-defined OVS bond, the role verifies:

- The bond port exists.
- OVS recognizes it as a runtime bond.
- The bond is attached to the expected bridge.
- The configured bond mode matches the inventory.
- All expected members exist.
- No unexpected members are present.
- Every expected bond member is reported as `enabled`.

The validator does not use the `all members active` field as a generic health
requirement.

For example, a healthy `balance-slb` bond may report:

```text
all members active: false
```

while its individual members report:

```text
member eth0: enabled
  may_enable: true

member eth2: enabled
  may_enable: true
```

This is considered healthy.

## DPDK interface validation

An inventory interface is included in DPDK topology validation when:

- Its OVS interface `type` begins with `dpdk`.
- It defines `options:dpdk-devargs`.

For each such interface, the role verifies:

- The OVS Interface object exists.
- The runtime interface type matches the inventory.
- `options:dpdk-devargs` matches the expected PCI address.
- The interface is attached to the expected bridge.
- `Interface.error` is empty.
- `admin_state` is `up`.
- `link_state` is `up`.
- `ofport` is greater than zero.

A healthy DPDK interface typically looks like:

```text
name        : eth0
type        : dpdk
options     : {dpdk-devargs="0000:19:00.0"}
error       : []
admin_state : up
link_state  : up
ofport      : 2
```

These checks detect cases where an interface exists in OVSDB but OVS failed
to initialize it correctly.

For example, an initialization failure may result in a non-empty:

```text
error
```

or an invalid:

```text
ofport: -1
```

Both conditions cause validation failure.

PCI driver binding is intentionally outside the scope of this role. PCI
binding and persistence are validated separately.

## Runtime commands

The role obtains runtime state using read-only Open vSwitch commands
equivalent to:

```bash
ovs-vsctl br-exists <bridge>
ovs-vsctl list-ports <bridge>
ovs-vsctl port-to-br <port>
ovs-vsctl iface-to-br <interface>

ovs-vsctl \
  --columns=name,type,options,error,admin_state,link_state,ofport \
  list Interface <interface>

ovs-appctl bond/show <bond>
```

The validator does not make changes through `ovs-vsctl`, `ovs-appctl`, or any
other command.

## Validation behavior

The role compares:

```text
deployment inventory
        |
        v
expected OVS topology
        |
        v
running OVS topology
        |
        v
differences / operational state
```

It validates inventory-managed objects but does not require the entire runtime
OVS database to exactly equal the deployment inventory.

This is important because OpenNebula can dynamically add VM interfaces to an
existing bridge.

For inventory-managed bonds, however, bond membership is expected to match
exactly. Unexpected bond members are reported as failures.

## Failure reporting

The role collects topology discrepancies instead of stopping on the first
one.

This allows a single validation run to report multiple problems such as:

```text
bridge ovsbr0: expected bridge does not exist

port bond0: expected bridge ovsbr0, got ovsbr1

bond bond0: expected bridge ovsbr0, got ovsbr1

bond bond0: mode expected balance-slb, got active-backup

bond bond0: missing members eth2

bond bond0: unexpected members eth3

DPDK interface eth0: type expected dpdk, got system

DPDK interface eth0: dpdk-devargs expected 0000:19:00.0, got 0000:19:00.1

DPDK interface eth0: expected bridge ovsbr0, got ovsbr1

DPDK interface eth0: OVS error <error text>

DPDK interface eth0: admin_state expected up, got down

DPDK interface eth0: link_state expected up, got down

DPDK interface eth0: invalid OpenFlow port -1
```

## Result reporting

The role records one validation result per hypervisor using the key:

```text
OVS topology on <hostname>
```

For example:

```text
OVS topology on nfhhvmadlb11
```

A successful validation records:

```text
ok
```

A host with no applicable inventory-defined OVS topology is recorded as
skipped.

Node-host results are folded into the main cloud verification report.

The test is also represented in the report's:

```text
Execution & Cleanup Summary
```

using the `OVS topology` result prefix.

## Running only this validation

With the validation enabled:

```yaml
validation:
  run_ovs_topology_validation: true
```

run:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags ovs_topology_validation
```

The OVS topology play gathers the host facts required to resolve deployment
inventory values such as:

```yaml
addrs:
  - cidr: "{{ ansible_default_ipv4.address ~ '/' ~ ansible_default_ipv4.prefix }}"

gw: "{{ ansible_default_ipv4.gateway }}"
```

## Non-OVS hosts

If no applicable inventory-defined OVS topology is present, the validation is
recorded as skipped.

This allows the role to run safely across a node group containing a mix of
OVS/DPDK and non-OVS hypervisors.

## Scope

This validation covers:

- OVS bridge existence and datapath type.
- Inventory-defined bridge ports.
- Port-to-bridge placement.
- OVS bond existence.
- Bond mode.
- Bond member membership.
- Bond member operational state.
- DPDK interface type.
- DPDK `dpdk-devargs`.
- DPDK interface-to-bridge placement.
- OVS interface errors.
- DPDK administrative state.
- DPDK link state.
- Valid OpenFlow port initialization.

This validation does not cover:

- PCI driver binding.
- DPDK PCI allowlist persistence.
- PMD CPU placement.
- NUMA locality.
- Throughput or latency benchmarking.

Those concerns are validated by separate validation tasks.
