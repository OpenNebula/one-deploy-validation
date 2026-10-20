# Hugepage Configuration and Availability Validation

The `hugepage-validation` role validates hugepage configuration and runtime
availability for DPDK and vhost-user workloads.

The validation correlates deployment inventory with:

- kernel boot parameters;
- global hugepage pools;
- NUMA-local hugepage pools;
- hugetlbfs mounts;
- OVS DPDK socket-memory configuration;
- OVS DPDK hugepage directory configuration.

The role is read-only.

## Enable

```yaml
validation:
  run_hugepage_validation: true
```

Run independently:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags hugepage_validation
```

## Deployment sources

Expected hugepage size and count come from `kernel_params`.

Example:

```yaml
kernel_params:
  - default_hugepagesz: "1G"
  - hugepagesz: "1G"
  - hugepages: 100
```

Multiple hugepage pools are supported. Repeated `hugepagesz` / `hugepages`
entries are interpreted in inventory order.

DPDK requirements come from the OVS inventory:

```yaml
ovs:
  set:
    - other_config:dpdk-socket-mem: "6144"
    - other_config:dpdk-hugepage-dir: "/dev/hugepages"
```

## Runtime sources

The role reads:

```text
/proc/cmdline
/proc/meminfo
/sys/kernel/mm/hugepages/
/sys/devices/system/node/node*/hugepages/
/proc/mounts
```

It also queries OVS runtime configuration.

## Global hugepage validation

For every inventory-defined hugepage pool the role reports:

- page size;
- expected page count;
- runtime total pages;
- free pages;
- reserved pages;
- surplus pages.

The runtime total must match the deployment requirement.

For example:

```text
hugepage total mismatch for 1G pages:
expected 100, actual 333
```

## Kernel command line

The validator checks the hugepage configuration persisted in the running
kernel command line.

Conflicting values are reported.

Example:

```text
hugepages=333
...
hugepages=100
```

produces:

```text
kernel command line contains conflicting hugepage counts for 1G pages: 333,100
```

This makes the validation suitable for checking persistence after reboot.

## NUMA validation

Hugepage state is reported separately for every NUMA node and page size.

Example:

```yaml
numa_nodes:
  "0":
    cpulist: "0-255"
    pools:
      1G:
        total_pages: 333
        free_pages: 330
```

## DPDK NUMA requirements

`other_config:dpdk-socket-mem` is interpreted by NUMA-node index.

For example:

```text
6144
```

means NUMA node 0 requires 6144 MB of DPDK hugepage capacity.

With 1 GB pages this requires at least:

```text
6 pages
```

For a multi-NUMA host:

```text
4096,8192
```

means:

```text
NUMA 0 -> 4 GB
NUMA 1 -> 8 GB
```

The validator fails if a NUMA node required by DPDK:

- does not exist;
- has no pool of the required hugepage size;
- has fewer total hugepages than required by `dpdk-socket-mem`.

Free hugepages are always reported.

Free pages are not compared directly with `dpdk-socket-mem` because pages may
already be consumed by the running OVS-DPDK datapath.

## hugetlbfs validation

When the inventory defines:

```yaml
other_config:dpdk-hugepage-dir: "/dev/hugepages"
```

the role verifies:

- the path exists;
- it is backed by `hugetlbfs`;
- it is an explicit mount point;
- its effective page size matches the expected deployment page size;
- runtime OVS uses the same hugepage directory.

## OVS validation

The role compares inventory and runtime values for:

```text
other_config:dpdk-socket-mem
other_config:dpdk-hugepage-dir
```

It also reports the runtime `dpdk_initialized` state.

## Result fields

The report includes:

- expected default hugepage size;
- runtime default hugepage size;
- inventory hugepage pools;
- global runtime hugepage pools;
- total/free/reserved/surplus counts;
- per-NUMA hugepage pools;
- DPDK NUMA memory requirements;
- minimum pages required by DPDK per NUMA node;
- expected and actual DPDK socket-memory configuration;
- expected and actual DPDK hugepage directory;
- hugetlbfs mount information;
- kernel hugepage command-line values;
- `/proc/meminfo` hugepage state;
- validation failures.

The result key is:

```text
Hugepage configuration and availability on <hostname>
```

## Failure examples

```text
default hugepage size mismatch: expected 2M, actual 1G
```

```text
hugepage total mismatch for 1G pages: expected 100, actual 333
```

```text
expected hugepage directory does not exist: /dev/hugepages
```

```text
expected hugepage directory is not backed by hugetlbfs: /dev/hugepages
```

```text
insufficient hugepages on NUMA node 1:
DPDK requires at least 6 1G pages, actual 0
```

```text
DPDK requires hugepages on NUMA node 1,
but the NUMA node does not exist
```

## Availability semantics

`free_hugepages` is reported for capacity and operational diagnostics.

The validator does not assume that the complete `dpdk-socket-mem` allocation
must remain free after OVS has initialized, because a running DPDK process may
already consume part of that hugepage pool.

The invariant checked for DPDK NUMA capacity is therefore:

```text
NUMA-local total pages >= pages required by dpdk-socket-mem
```

An explicit VM free-hugepage reserve is not enforced because the deployment
inventory currently does not define such a requirement.

## Read-only behavior

The role does not:

- allocate or release hugepages;
- mount or unmount hugetlbfs;
- change kernel parameters;
- restart OVS;
- restart libvirt;
- reboot the host.

It only inspects and reports runtime state.
