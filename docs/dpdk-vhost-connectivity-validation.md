# DPDK / vhost-user Cross-Host Connectivity Validation

## Purpose

Validate that two OpenNebula KVM hypervisors can deploy virtual machines
using OVS-DPDK/vhost-user interfaces and exchange traffic over an existing
OpenNebula virtual network.

The test verifies both runtime configuration and, when possible, actual
bidirectional VM-to-VM ICMP connectivity.

The test is disabled by default.

## Validation workflow

1. Resolve the active OpenNebula RAFT leader.
2. Discover two healthy KVM hosts belonging to the same cluster.
3. Select an existing OVS-DPDK virtual network.
4. Verify the selected OVS bridge uses the `netdev` datapath on both hosts.
5. Ensure a reusable validation VM template is available.
6. Deploy one temporary VM on each selected hypervisor.
7. Verify VM placement using OpenNebula host IDs.
8. Inspect libvirt and Open vSwitch runtime configuration.
9. Check whether the RAFT leader can reach both guests on TCP/22.
10. If reachable, execute bidirectional ICMP between the VMs.
11. Remove temporary VMs and SSH keys.
12. Record validation and cleanup results.

## Virtual network selection

The validation uses an existing OpenNebula virtual network.

A suitable network must have:

- `VN_MAD=ovswitch`
- `BRIDGE_TYPE=openvswitch_dpdk`
- An existing bridge on both selected hypervisors
- OVS `datapath_type=netdev`
- An IPv4 Address Range with at least two free leases
- Assignment to the selected OpenNebula cluster

Networks requiring guest-side VLAN tagging are not eligible.

When `vnet_name` is empty, the role automatically selects an eligible
network. An explicit network name restricts selection to that network.

The validation does not create or modify virtual networks, bridges,
VLAN tags, address ranges, or physical interfaces.

## VM deployment

Two disposable VMs are created, one on each selected hypervisor.

VM placement uses OpenNebula scheduling requirements and is verified
using the host ID recorded in the deployment history.

The VM configuration includes:

- Virtio network adapter
- Vhost-user network attachment
- Shared memory
- HugePages
- Guest network contextualization
- Temporary SSH public key

A compatible existing template is reused when available. Otherwise,
the configured Marketplace application can be exported.

## Runtime DPDK verification

Each VM's runtime configuration is inspected on its hypervisor.

Checks include:

- Libvirt vhost-user NIC definition
- Virtio network model
- QEMU vhost-user UNIX socket existence
- Matching libvirt and OVS socket paths
- OVS interface type `dpdkvhostuserclient`
- Valid OVS `ofport`
- No OVS interface errors
- Correct OVS bridge attachment
- OVS bridge datapath type `netdev`

When enabled, VLAN configuration is checked read-only:

- OVS `Port.tag` matches the virtual network's VLAN ID
- Access-port VLAN mode is consistent
- No unexpected trunk VLANs are configured

No VLAN isolation or negative VLAN testing is performed.

## Frontend reachability prerequisite

The elected RAFT leader initiates SSH connections to both test VMs.

Hypervisors do not initiate guest SSH connections.

Before running ICMP, the role checks TCP/22 reachability from the
RAFT leader to both guests.

If either guest is unreachable, VM-to-VM connectivity testing is
reported as SKIPPED.

This accommodates multitenant environments in which tenant networks
are intentionally isolated from management frontends.

The validation does not add IP addresses or routes to frontend or
hypervisor interfaces to bypass tenant network isolation.

## ICMP connectivity

When both guests are reachable from the frontend, the role connects
to each VM using its temporary SSH key and executes ICMP to the
other VM's OpenNebula-allocated IPv4 address.

Both directions are tested independently:

- VM A to VM B
- VM B to VM A

The default is 10 ICMP packets per direction.

Results include source and destination addresses, packet count,
packet loss, execution status, and command output.

Frontend SSH failures and actual ICMP failures are distinguished
in the validation results.

This is a functional connectivity test, not a throughput benchmark.

## Configuration

The test is disabled by default.

Example inventory configuration using generic hostnames:

```yaml
validation:
  run_dpdk_vhost_connectivity_validation: true

  dpdk_vhost_connectivity:
    hosts:
      - kvm-a.example.test
      - kvm-b.example.test

    excluded_hosts:
      - kvm-maintenance.example.test

    excluded_frontends:
      - fe-maintenance.example.test

    vnet_name: tenant-dpdk
    market_name: 'Alpine Linux 3.21'

    vcpu: 1
    memory_mb: 2048

    ping_count: 10
    check_vlan_config: true

    image_ready_retries: 60
    image_ready_delay: 10
    vm_running_retries: 30
    vm_running_delay: 5
    ssh_retries: 12
    ssh_delay: 5
```

### Configuration options

| Option | Default | Description |
|---|---|---|
| `run_dpdk_vhost_connectivity_validation` | `false` | Enable active validation |
| `hosts` | `[]` | Two explicit KVM hosts or automatic selection |
| `excluded_hosts` | `[]` | Hypervisors excluded from selection |
| `excluded_frontends` | `[]` | Frontends excluded from RAFT discovery |
| `vnet_name` | `''` | Existing DPDK vNET; empty enables discovery |
| `market_name` | `Alpine Linux 3.21` | Marketplace app or reusable template |
| `vcpu` | `1` | vCPU count per test VM |
| `memory_mb` | `2048` | Memory per test VM |
| `ping_count` | `10` | ICMP packets per direction |
| `check_vlan_config` | `true` | Read-only VLAN consistency verification |
| `image_ready_retries` | `60` | Image readiness polling attempts |
| `image_ready_delay` | `10` | Seconds between image polls |
| `vm_running_retries` | `30` | VM deployment polling attempts |
| `vm_running_delay` | `5` | Seconds between deployment polls |
| `ssh_retries` | `12` | SSH connectivity polling attempts |
| `ssh_delay` | `5` | Seconds between SSH attempts |

`hosts: []` automatically selects two eligible online KVM hosts in
the same OpenNebula cluster.

An explicit list must contain two distinct eligible hypervisors.
Hostnames must match OpenNebula and Ansible inventory entries.

## Execution

Run the standalone playbook:

```bash
hatch env run -e validation-default -- ansible-playbook \
  -i inventory/site.yml \
  playbooks/dpdk-vhost-connectivity.yml \
  --tags dpdk_vhost_connectivity
```

Enable the validation using the inventory configuration.

Alternatively, explicitly enable standalone execution:

```bash
hatch env run -e validation-default -- ansible-playbook \
  -i inventory/site.yml \
  playbooks/dpdk-vhost-connectivity.yml \
  -e odv_dpdk_enable=true
```

The role is also integrated with `playbooks/validation.yml`.

## Validation results

| Condition | Result |
|---|---|
| Valid OVS-DPDK runtime and successful bidirectional ICMP | PASS |
| No eligible OVS-DPDK vNET | SKIPPED |
| Fewer than two eligible online KVM hosts | SKIPPED |
| Frontend cannot reach test guests on TCP/22 | SKIPPED |
| Invalid libvirt/OVS/vhost-user runtime configuration | FAIL |
| SSH authentication fails after TCP/22 succeeds | FAIL |
| ICMP execution fails or reports packet loss | FAIL |
| Temporary resource cleanup fails | FAIL |

Runtime verification is recorded separately for each hypervisor.

A SKIPPED connectivity result does not establish whether the tenant
network is functioning correctly.

## Cleanup

The role tracks temporary resources using the existing validation
manifest and shared cleanup mechanisms.

Temporary resources include:

- Two disposable virtual machines
- Temporary SSH keys on the RAFT leader
- Marketplace image and template, when newly exported

Existing virtual networks, templates, and images are preserved.

Cleanup runs even if deployment, runtime verification, SSH, or
ICMP execution fails.

## Limitations

- Only two hypervisors are exercised per run.
- Only one selected virtual network is tested.
- Tenant networks unreachable from the frontend cannot undergo ICMP testing.
- The guest image must support SSH and provide the `ping` utility.
- TCP/22 reachability alone does not guarantee SSH authentication.
- Zero packet loss in a short test does not prove sustained performance.
- No physical NIC throughput benchmarking is performed.
- No VLAN modification or VLAN isolation tests are performed.
