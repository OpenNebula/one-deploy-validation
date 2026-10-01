# DPDK PCI Allowlist Validation

The `dpdk-validation` role validates that the DPDK PCI devices declared by
one-deploy match the effective Open vSwitch DPDK configuration on each
hypervisor.

## Enable the test

The test is disabled by default.

To enable it, modify inventory/reference/group_vars/all.yml

set the run_dpdk_pci_allowlist field to true

```yaml
validation:
  run_dpdk_pci_allowlist: true

The validation can be run independently with:
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags dpdk_pci_allowlist

# Source of truth
Expected DPDK PCI devices are derived directly from the deployment inventory.
An interface is considered a DPDK interface when its ovs.iface configuration
contains a type beginning with dpdk and an options:dpdk-devargs PCI
address.
For example:
ovs:
  iface:
    vmnic0:
      set:
        - type: dpdk
        - options:dpdk-devargs: '0000:19:00.0'

The expected PCI list is therefore not duplicated in the validation
configuration.

# Runtime validation
For every inventory-defined DPDK PCI device, the role validates:
- The PCI address exists under /sys/bus/pci/devices.
- The device is present in the Open vSwitch DPDK EAL allowlist configured
  through Open_vSwitch.other_config:dpdk-extra.
- The device is present in an Open vSwitch interface whose type is dpdk.
- No expected device is missing from the allowlist.
- No unexpected device is present in the allowlist.
- No expected device is missing from the runtime DPDK interfaces.
- No unexpected runtime DPDK device is present.
- No duplicate allowlist entries exist.
- No duplicate runtime DPDK devices exist.
The role records a per-host result using the key:
DPDK PCI allowlist on <hostname>

These node-host results are folded into the main validation report.

# Persistence validation
The same validation is reusable after a host reboot.
To validate persistence:
1. Run the validation and confirm that it passes.
2. Reboot the hypervisor.
3. Wait for Open vSwitch and the host networking configuration to recover.
4. Run the validation again.
A successful post-reboot run verifies that the inventory-defined DPDK devices,
the persisted OVS DPDK allowlist and the runtime DPDK interfaces remain
consistent.
Example DPDK allowlist
For two DPDK interfaces:
ovs:
  set:
    - other_config:dpdk-init: 'true'
    - other_config:dpdk-extra: '-a 0000:19:00.0 -a 0000:5e:00.0'

The corresponding expected validation result contains:
expected:
  - 0000:19:00.0
  - 0000:5e:00.0

effective_allowlist:
  - 0000:19:00.0
  - 0000:5e:00.0

runtime_dpdk_devices:
  - 0000:19:00.0
  - 0000:5e:00.0

with all missing, unexpected, duplicate and missing-hardware lists empty.

# Non-DPDK hosts
If the role is invoked on a host with no inventory-defined DPDK interfaces,
the result is recorded as skipped.
This allows the role to operate safely across a node group containing a mix
of DPDK and non-DPDK hypervisors.
