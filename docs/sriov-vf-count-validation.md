# SR-IOV VF Count Validation

The `sriov-vf-count-validation` role validates that every SR-IOV Physical
Function defined in the deployment inventory exposes the expected number of
Virtual Functions.

The validation is read-only. It does not create, remove, enable, disable, bind,
or reconfigure VFs.

## Enable the test

```yaml
validation:
  run_sriov_vf_count_validation: true
```

Run independently with:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags sriov_vf_count_validation
```

## Source of truth

Expected SR-IOV PFs and VF counts are derived from the deployment
`pci_devices` inventory.

For example:

```yaml
pci_devices:
  - address: "0000:81:00.0"
    set_name: pf0
    set_numvfs: 8
```

Only PCI entries containing both `address` and `set_numvfs` are treated as
SR-IOV Physical Functions.

No separate validation-side VF count configuration is required.

## Validation

For each configured SR-IOV PF the role checks:

- the PF PCI device exists;
- `sriov_numvfs` can be read;
- `sriov_totalvfs` can be read;
- the active VF count equals the inventory `set_numvfs`;
- the expected count does not exceed the hardware maximum;
- the number of `virtfn*` entries matches `sriov_numvfs`;
- every generated VF PCI function is visible under `/sys/bus/pci/devices`;
- the PF network interface name is reported when available;
- PCI and `ip link` information is collected for diagnostics.

## Runtime sources

The primary runtime sources are:

```text
/sys/bus/pci/devices/<PF>/sriov_numvfs
/sys/bus/pci/devices/<PF>/sriov_totalvfs
/sys/bus/pci/devices/<PF>/virtfn*
```

Equivalent network paths are:

```text
/sys/class/net/<PF>/device/sriov_numvfs
/sys/class/net/<PF>/device/sriov_totalvfs
```

PCI-address-based paths are used by the validator because the PCI BDF is the
stable inventory identifier.

## VF PCI enumeration

Each `virtfn*` link is resolved to its VF PCI BDF.

For example:

```text
virtfn0 -> 0000:81:00.2
virtfn1 -> 0000:81:00.3
```

Each resulting VF PCI address must also exist under:

```text
/sys/bus/pci/devices/
```

This ensures that a numerically correct `sriov_numvfs` value does not pass
validation if the corresponding VF PCI functions are not visible to the
operating system.

## Per-PF result

Each Physical Function receives an independent result:

```text
SR-IOV VF count <PF> (<PCI>) on <hostname>
```

The result includes:

- PF name;
- PF PCI address;
- expected VF count;
- active VF count;
- maximum supported VF count;
- VF count match;
- PF network devices;
- enumerated VF PCI devices;
- missing VF PCI devices;
- VF enumeration consistency;
- PCI description;
- `ip link` diagnostic output;
- failure reasons.

## Failure conditions

Validation fails when:

- the PF PCI device does not exist;
- `sriov_numvfs` cannot be read;
- `sriov_totalvfs` cannot be read;
- the active VF count differs from the expected inventory value;
- the expected VF count exceeds the reported hardware maximum;
- the number of `virtfn*` entries differs from `sriov_numvfs`;
- an enumerated VF PCI function is missing from the operating system.

Examples:

```text
expected 6 VFs, actual 0
```

```text
expected 6 VFs, actual 32
```

```text
SR-IOV PF PCI device does not exist
```

```text
sriov_numvfs cannot be read
```

## Host summary

A host-level result is also recorded:

```text
SR-IOV VF count on <hostname>
```

The host summary fails if any configured Physical Function fails.

Hosts with no inventory entries containing `set_numvfs` are skipped.

## Validation after reboot

The role reads the live SR-IOV state every time it executes.

It can therefore be rerun after a host reboot to verify that VF creation
persisted correctly.

For example, if the inventory expects 8 VFs but the PF returns to:

```text
sriov_numvfs = 0
```

after reboot, the validator reports the PF as failed.

## Runtime commands

The role performs read-only operations equivalent to:

```bash
cat /sys/bus/pci/devices/<PF>/sriov_numvfs
cat /sys/bus/pci/devices/<PF>/sriov_totalvfs
readlink /sys/bus/pci/devices/<PF>/virtfn*
lspci -Dnn -s <PF>
ip -details link show dev <PF>
```

## Scope

This validation covers:

- inventory-derived SR-IOV Physical Functions;
- expected VF counts;
- active VF counts;
- maximum VF counts;
- VF PCI enumeration;
- VF PCI visibility;
- missing PF detection;
- post-reboot VF persistence.

It does not validate:

- VF driver binding;
- VF assignment to virtual machines;
- OpenNebula PCI scheduling;
- VF MAC or VLAN configuration;
- VF trust or spoof-check settings;
- SR-IOV network throughput;
- NUMA locality.

Those concerns are outside the scope of this validation.
