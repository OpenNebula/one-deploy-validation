# DPDK PCI Driver Binding Validation

The `dpdk-pci-driver-validation` role validates that physical PCI devices
configured for Open vSwitch DPDK exist and are bound to the expected driver.

The validation is read-only. It does not bind or unbind PCI devices, modify
driver overrides, reload kernel modules, restart Open vSwitch, or change host
configuration.

## Enable the test

The test is disabled by default.

Enable it with:

```yaml
validation:
  run_dpdk_pci_driver_validation: true
```

Run it independently with:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags dpdk_pci_driver_validation
```

## Source of truth

Expected physical DPDK PCI devices are derived directly from the deployment
inventory.

An OVS interface is considered a physical DPDK PCI device when:

- its interface type begins with `dpdk`;
- it defines `options:dpdk-devargs`;
- the first component of `dpdk-devargs` is a valid PCI BDF.

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
        - options:dpdk-devargs: '0000:01:00.1'
```

No separate PCI device list is required.

## Expected driver

The default expected driver is:

```yaml
validation:
  dpdk_pci_driver:
    default_driver: vfio-pci
```

Individual devices can override the default:

```yaml
validation:
  dpdk_pci_driver:
    default_driver: vfio-pci
    driver_overrides:
      "0000:81:00.0": mlx5_core
```

This is useful for devices using bifurcated PMDs where the physical device
intentionally remains bound to its native kernel driver.

The validator therefore does not maintain a hard-coded mapping between PCI
vendors and kernel drivers.

## Runtime driver validation

For every expected device, the validator checks:

- the PCI device exists under `/sys/bus/pci/devices`;
- a driver is currently attached;
- the current driver matches the configured expected driver;
- native kernel driver candidates reported by `lspci -k`;
- IOMMU group information where available;
- the corresponding `/dev/vfio/<group>` device for `vfio-pci`;
- DPDK device-binding utility output when available;
- driverctl override information when available.

The authoritative runtime driver is obtained from:

```text
/sys/bus/pci/devices/<PCI>/driver
```

For example:

```text
/sys/bus/pci/devices/0000:01:00.0/driver
```

The final component of the symlink target is treated as the currently bound
driver.

## Native kernel driver discovery

Native kernel driver candidates are discovered dynamically from:

```bash
lspci -Dnnk -s <PCI>
```

For example:

```text
Kernel driver in use: vfio-pci
Kernel modules: bnxt_en
```

is reported as:

```text
expected_driver: vfio-pci
actual_driver: vfio-pci
native_kernel_modules:
  - bnxt_en
```

This avoids maintaining vendor-specific mappings such as Intel to `i40e`,
Broadcom to `bnxt_en`, or Mellanox to `mlx5_core`.

If a device expected to use `vfio-pci` falls back to a driver listed in
`Kernel modules`, the failure explicitly identifies it as a native kernel
driver.

For example:

```text
expected driver vfio-pci, got native kernel driver bnxt_en
```

## VFIO validation

When the expected and active driver is `vfio-pci`, the validator reads the
device IOMMU group.

If the device has an IOMMU group, the corresponding VFIO group device is
expected to exist:

```text
/dev/vfio/<group>
```

A missing VFIO group device is reported as a failure.

## DPDK device-binding utilities

When available, the role inspects either:

```text
dpdk-devbind.py
```

or:

```text
dpdk-devbind
```

This output is retained as supporting runtime evidence.

The presence or absence of `dpdk-devbind` itself is not a validation
requirement.

The sysfs driver binding remains authoritative.

## driverctl

When available, `driverctl list-overrides` is inspected.

A driverctl override is diagnostic information only.

A correctly bound DPDK device does not need to have a driverctl override
because deployments may persist PCI binding using other mechanisms.

The absence of a driverctl override therefore does not cause validation
failure.

## Per-device result

Each inventory-defined PCI device receives an independent result:

```text
DPDK PCI driver <PCI> on <hostname>
```

The result is `ok` or `failed`.

Associated details include:

- inventory interface name;
- PCI address;
- complete `dpdk-devargs`;
- expected driver;
- actual bound driver;
- whether the driver matches;
- `lspci` driver in use;
- native kernel module candidates;
- whether the current driver is a native kernel driver;
- IOMMU group;
- VFIO group device;
- VFIO device presence;
- driverctl override when available;
- DPDK devbind detection and output;
- PCI description;
- failure reasons.

## Missing PCI devices

An inventory-defined PCI BDF that is not present under:

```text
/sys/bus/pci/devices/
```

is reported as failed.

Example:

```text
PCI device does not exist
```

## Unbound devices

A device that exists but has no driver symlink is reported as:

```text
PCI device has no driver attached; expected <driver>
```

This is a validation failure.

## Wrong driver

A device bound to a driver different from the configured expected driver
fails validation.

Examples:

```text
expected driver vfio-pci, got i40e
```

or:

```text
expected driver vfio-pci, got native kernel driver bnxt_en
```

## Host summary

A host-level result is recorded using:

```text
DPDK PCI driver binding on <hostname>
```

The host summary is `failed` if any expected PCI device fails.

Hosts with no inventory-defined physical DPDK PCI devices are recorded as:

```text
skipped — no inventory-defined physical DPDK PCI devices
```

## Validation after reboot

The role evaluates the current kernel PCI binding every time it runs.

It can therefore be executed after host reboot to verify that persistent PCI
binding restored the expected runtime state.

For example, a device that was correctly bound to `vfio-pci` before reboot
but returned to its native kernel driver afterward is detected as a failure.

The validation itself makes no changes to PCI devices or driver binding.

## Runtime commands

The role uses read-only host information equivalent to:

```bash
readlink /sys/bus/pci/devices/<PCI>/driver

lspci -Dnnk -s <PCI>

driverctl list-overrides

dpdk-devbind.py --status-dev net

readlink /sys/bus/pci/devices/<PCI>/iommu_group

stat /dev/vfio/<group>
```

Optional utilities are treated as diagnostic evidence and do not have to be
installed for the main driver-binding validation to function.

## Scope

This validation covers:

- inventory-derived physical DPDK PCI devices;
- PCI device existence;
- current PCI driver binding;
- expected versus actual driver;
- native kernel driver candidates;
- VFIO runtime availability;
- optional DPDK devbind evidence;
- optional driverctl evidence;
- per-device results;
- host-level summary;
- post-reboot binding verification.

It does not validate:

- DPDK PCI allowlist configuration;
- OVS bridge topology;
- OVS DPDK interface initialization;
- bond membership;
- PMD CPU placement;
- NUMA locality;
- SR-IOV VF assignment;
- network throughput or latency.

Those concerns are covered by separate validation tests.
