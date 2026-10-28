# VM vNIC NUMA Locality Validation

The `vm-vnic-numa-validation` role correlates a VM's runtime CPU and memory
placement with the NUMA locality of its network datapath.

It supports:

- OVS-DPDK vhost-user interfaces (`dpdkvhostuserclient`)
- direct physical DPDK uplinks
- OVS `balance-slb` / `balance-tcp` bonds
- OVS `active-backup` bonds
- OVS PMD RX queue placement
- SR-IOV VF to PF PCI/NUMA correlation

The canonical vhost-user join key is the OpenNebula/libvirt/OVS target name
`one-<VMID>-<NIC_ID>`.

## Enable

```yaml
validation:
  run_vm_vnic_numa_validation: true
```

The role is **read-only by default**. It inspects applicable running VMs and
does not create or modify cloud resources.

```yaml
validation:
  run_vm_vnic_numa_validation: true
  vm_vnic_numa:
    create_test_vms: false
```

## Active validation

Active mode creates disposable VMs pinned to each requested NUMA node on each
host in every discovered cluster. This mode is opt-in:

```yaml
validation:
  run_vm_vnic_numa_validation: true
  vm_vnic_numa:
    create_test_vms: true
    vnet_name: ""               # empty: auto-select cluster-local OVS-DPDK VNet
    market_name: "Alpine Linux 3.21"
    test_numa_nodes: all        # or e.g. [0, 1]
    vcpu: 2
    memory_mb: 2048
    image_ready_retries: 60
    image_ready_delay: 10
    vm_running_retries: 30
    vm_running_delay: 5
```

The role never creates an OVS-DPDK VNet. If no suitable existing VNet is
available for a cluster, that cluster is reported as skipped.

The shared marketplace template/image service is reused. Before a disposable
VM is instantiated, the role waits until the image reaches `rdy` or `used`.
Every VM is registered in the run manifest immediately before instantiation
and is terminated from an `always` cleanup block.

## Verdicts

The evaluator uses the following severity order:

`failed > degraded > unknown > ok`

### Direct physical DPDK interface

- local physical PCI device and locally assigned physical RX PMD queues: `ok`
- remote physical PCI device **or** all physical RX PMDs remote: `failed`
- mixed local/remote physical RX PMD queues: `degraded`
- missing PCI NUMA or missing physical PMD queue placement: `unknown` (unless another observed condition already has higher severity)

### Balanced OVS bond (`balance-slb`, `balance-tcp`)

Only enabled/eligible members are considered. Both PCI NUMA and physical
RX PMD queue NUMA placement are inspected for each member.

- every eligible member local with locally assigned RX PMDs: `ok`
- local and remote eligible members: `degraded`, unless RX PMD placement establishes a higher-severity failure
- every eligible member remote: `failed`
- missing locality information for eligible paths prevents a clean `ok` or an unsupported all-remote conclusion

A mixed balanced bond is deliberately **not** a failure: some traffic can use
a local member while other flows can cross the NUMA boundary.

### Active-backup bond

- active member local, all eligible standbys local, RX PMDs local: `ok`
- active member local with a remote eligible standby: `degraded`
- active member remote: `failed`
- active member local but standby NUMA unknown: `unknown`, absent independently observed degradation/failure
- physical RX PMD locality can raise the severity regardless of PCI placement

### vhost-user PMDs

- all VM queues on local PMDs: `ok`
- VM queues split between local and remote PMDs: `degraded`
- all VM queues on remote PMDs: `failed`

Physical uplink RX queues are checked separately against both VM NUMA and
physical PCI NUMA. Only enabled RX queues contribute to the PMD calculation.
If a required queue assignment is absent, the result is `unknown` rather than
assumed local. This can change a previously clean `ok` into `unknown` on
hosts where OVS does not expose the physical port's PMD assignment.

### SR-IOV

The assigned VF PCI device is followed through `/sys/bus/pci/devices/<VF>/physfn`
to its PF.

- VF/PF and VM placement local: `ok`
- VF/PF on a remote NUMA node or VF/PF disagree: `failed`
- incomplete NUMA/PF information: `unknown`

### VM placement

The probe uses `virsh vcpupin` plus `virsh numatune` and maps the vCPU affinity
set through the host's sysfs NUMA topology. A single matching CPU/memory NUMA
node is `ok`; a spanning placement is `degraded`; disjoint CPU and memory
nodes are `failed`; incomplete placement data is `unknown`.

`PIN_POLICY=NONE` is not itself considered an error. The role evaluates the
actual runtime affinity and memory policy.

## Report

Result keys start with `VM vNIC NUMA locality`. `degraded` is rendered as a
WARN badge in the main report.

Active mode records one row per `(host, NUMA node)`. Read-only mode records one
row per applicable running VM; the JSON comment contains each interface,
physical member, PMD queue/core, PCI BDF, NUMA node and finding.

## Testing

`make test` runs the pure-Python evaluator tests in
`test/vm_vnic_numa/test_logic.py`, including balanced-bond cases:
all-local `ok`, mixed local/remote `degraded`, and all-remote `failed`,
as well as missing NUMA information, physical NIC/PMD mismatches,
and disabled PMD RX queues.
