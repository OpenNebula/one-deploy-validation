# CPU Isolation and DPDK PMD CPU Assignment Validation

The `cpu-isolation-pmd-validation` role validates CPU reservation and runtime
placement for Open vSwitch DPDK Poll Mode Driver (PMD) threads.

The validation correlates the deployment inventory with the running kernel,
OVS configuration, Linux thread affinity and CPU/NUMA topology.

The role is read-only.

## Enable the test

```yaml
validation:
  run_cpu_isolation_pmd_validation: true
```

Run independently with:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags cpu_isolation_pmd_validation
```

## Source of truth

The expected PMD CPU set is derived from the deployment OVS configuration:

```yaml
ovs:
  set:
    - other_config:pmd-cpu-mask: "0x30000000000000003"
```

The hexadecimal mask is decoded into a human-readable CPU list.

For example:

```text
0x30000000000000003
```

decodes to:

```text
0,1,64,65
```

### CPU isolation expectation

When the deployment explicitly defines `isolcpus` in `kernel_params`, that
value is used as the expected isolated CPU set:

```yaml
kernel_params:
  - isolcpus: "0,1,64,65"
```

When no explicit `isolcpus` inventory value exists, the PMD CPU set derived
from `other_config:pmd-cpu-mask` is used as the expected isolation set.

This verifies that CPUs reserved for DPDK PMD processing are effectively
isolated from normal scheduler workloads.

## Runtime sources

The role uses:

```text
/proc/cmdline
/sys/devices/system/cpu/isolated
/sys/devices/system/cpu/possible
/sys/devices/system/cpu/present
/sys/devices/system/cpu/online
```

and:

```bash
ovs-vsctl get Open_vSwitch . other_config:pmd-cpu-mask
ovs-appctl dpif-netdev/pmd-rxq-show
lscpu -p=CPU,NODE,SOCKET,CORE
```

Linux PMD thread placement is also read from:

```text
/proc/<ovs-vswitchd-pid>/task/<tid>/status
/proc/<ovs-vswitchd-pid>/task/<tid>/stat
```

## Validation

The role validates:

- the deployment PMD CPU mask can be decoded;
- every expected PMD CPU exists in the host topology;
- every expected PMD CPU is online;
- the runtime OVS PMD CPU mask matches the deployment mask;
- OVS runtime PMD cores match the expected CPU set;
- Linux PMD thread affinity matches the expected CPU set;
- each PMD thread is pinned to exactly one CPU;
- PMD threads are not allowed to execute on non-designated CPUs;
- the CPU on which each PMD thread is currently executing is valid;
- the kernel command-line isolated CPU set matches the expected design;
- the effective kernel isolated CPU set matches the expected design;
- PMD CPUs are effectively isolated;
- PMD CPU NUMA placement is reported;
- optionally, PMD CPUs can be required to remain within one NUMA node;
- optionally, PMD CPUs can be prohibited from overlapping other reserved
  CPU roles.

## PMD runtime placement

The validator checks three independent representations of PMD placement.

### OVS PMD mask

Example:

```text
other_config:pmd-cpu-mask="0x30000000000000003"
```

### OVS runtime PMD cores

Example output from:

```bash
ovs-appctl dpif-netdev/pmd-rxq-show
```

may contain:

```text
pmd thread numa_id 0 core_id 0:
pmd thread numa_id 0 core_id 1:
pmd thread numa_id 0 core_id 64:
pmd thread numa_id 0 core_id 65:
```

### Linux thread affinity

For each `ovs-vswitchd` PMD thread the validator reads:

```text
Cpus_allowed_list
```

from:

```text
/proc/<pid>/task/<tid>/status
```

For example:

```text
pmd-c00 -> CPU 0
pmd-c01 -> CPU 1
pmd-c64 -> CPU 64
pmd-c65 -> CPU 65
```

Thread affinity is the authoritative scheduler placement check.

The validator also reports the processor on which the thread was executing
when sampled.

## Kernel isolation

The validator compares the expected isolation set with two runtime sources.

### Boot command line

```text
/proc/cmdline
```

For example:

```text
isolcpus=0,1,64,65
```

### Effective kernel isolation

```text
/sys/devices/system/cpu/isolated
```

For example:

```text
0-1,64-65
```

The role normalizes CPU-list formats before comparison.

## CPU topology

The validator verifies every expected CPU against:

```text
/sys/devices/system/cpu/possible
/sys/devices/system/cpu/present
/sys/devices/system/cpu/online
```

It also maps relevant CPUs to:

- NUMA node;
- physical socket;
- physical core.

Example report:

```yaml
cpu_topology:
  "0":
    node: 0
    socket: 0
    core: 0
  "64":
    node: 0
    socket: 0
    core: 64
```

## NUMA policy

By default, PMD CPUs are allowed to span multiple NUMA nodes because this can
be an intentional DPDK design.

To require all PMD CPUs to reside on one NUMA node:

```yaml
validation:
  run_cpu_isolation_pmd_validation: true

  cpu_isolation_pmd:
    require_single_numa: true
```

When enabled, a PMD CPU set spanning multiple NUMA nodes causes validation
failure.

## Reserved CPU role overlap

Additional CPU reservations can optionally be declared:

```yaml
validation:
  cpu_isolation_pmd:
    prohibited_cpu_sets:
      emulator: "2-3"
      storage: "8-9"
```

If a PMD CPU overlaps one of these sets, the validator fails and reports the
overlapping CPU IDs.

Example:

```text
PMD CPUs overlap prohibited role emulator: 2,3
```

The validator does not invent reserved-role policies when the deployment does
not define one.

## Failure examples

### OVS PMD mask mismatch

```text
OVS PMD CPU mask does not match deployment configuration
```

### Runtime PMD placement mismatch

```text
OVS PMD runtime core set does not match expected PMD CPUs
```

### Linux affinity mismatch

```text
Linux PMD thread affinity does not match expected PMD CPUs
```

### Invalid CPU

```text
PMD CPUs do not exist in host topology: 256
```

### Offline CPU

```text
PMD CPUs are not online: 256
```

### Kernel isolation mismatch

```text
kernel command line isolated CPUs do not match deployment configuration
```

```text
effective isolated CPU set does not match deployment configuration
```

```text
PMD CPUs are not effectively isolated: 0,1,64,65
```

### Unexpected PMD placement

```text
PMD thread 1234 can run on non-designated CPUs: 7
```

### NUMA policy violation

```text
PMD CPUs span multiple NUMA nodes: 0,1
```

## Report fields

The host result is recorded as:

```text
CPU isolation and DPDK PMD assignment on <hostname>
```

The report includes:

- expected PMD hexadecimal mask;
- decoded expected PMD CPU list;
- runtime OVS PMD mask;
- decoded runtime OVS PMD CPU list;
- expected isolated CPU list;
- kernel command-line isolated CPUs;
- effective isolated CPUs;
- CPU possible/present/online ranges;
- relevant CPU topology;
- PMD NUMA nodes;
- OVS runtime PMD cores;
- Linux PMD thread affinities;
- current PMD thread processors;
- individual PMD thread details;
- prohibited role overlaps;
- validation failures.

## Applicability

The test applies when at least one of the following indicates PMD use:

- a deployment `pmd-cpu-mask`;
- a runtime OVS `pmd-cpu-mask`;
- OVS runtime PMD threads.

A host without a PMD configuration or runtime PMD threads is reported as
skipped.

## Read-only behavior

The role does not:

- change the OVS PMD mask;
- change thread affinity;
- modify kernel parameters;
- isolate CPUs;
- reboot the host;
- restart OVS;
- rebalance PMD queues.

It only inspects and reports the running configuration.
