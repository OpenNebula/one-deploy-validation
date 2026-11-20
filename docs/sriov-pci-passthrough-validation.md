# SR-IOV PCI Passthrough Validation

## Purpose

Validate that an OpenNebula KVM host can deploy a disposable VM
with an explicitly selected SR-IOV Virtual Function (VF).

The test verifies PCI allocation and attachment from the host side.
It does not require access to the guest operating system.

## Configuration

Enable the test under the inventory's `validation:` dictionary.

Example:

    run_sriov_pci_passthrough_validation: true

    sriov_pci_passthrough:
      template_id: "<TEMPLATE_ID>"

      targets:
        - host_id: 1
          host: kvm-test-01.example
          vf: '86:06.0'

      memory_mb: 1024
      vcpu: 1

      deploy_timeout: 300
      cleanup_timeout: 180
      poll_interval: 5

The template must be bootable and must not contain preconfigured
NIC or PCI devices. Use a nonpersistent disk image for disposable
test VMs.

## Validation procedure

For each configured target:

1. Verify the OpenNebula host identity.
2. Verify the requested PCI VF exists.
3. Verify that the VF is free (VMID=-1).
4. Instantiate a disposable VM with PCI.SHORT_ADDRESS.
5. Verify the requested address before deployment.
6. Deploy the VM directly to the specified KVM host.
7. Wait for ACTIVE/RUNNING.
8. Verify the VM's allocated PCI address.
9. Verify the host PCI inventory assigns the VF to the test VM.
10. Verify the libvirt domain is running.
11. Verify libvirt's physical PCI hostdev source.
12. Terminate the VM and verify VF release.

## Reported checks

The role records these checks:

- preflight
- VM request
- deployment
- PCI allocation
- libvirt hostdev
- cleanup

Each target also receives an overall result.

## Scope

Included:

- OpenNebula PCI discovery and availability.
- Explicit VF selection.
- VM deployment to the intended KVM host.
- OpenNebula PCI resource accounting.
- Libvirt PCI hostdev assignment.
- VF release after cleanup.

Excluded:

- Guest driver initialization.
- Guest SSH access.
- SR-IOV vNet configuration.
- VLAN functionality.
- Packet delivery and throughput.
- PCI passthrough migration.
- Recovery and VM recreation testing.

## Cleanup

Each VM receives a unique name prefixed with `odv-sriov-pci-`.

The VM is registered in the validation manifest before creation.

The Python probe attempts VM termination and verifies VF release.
The Ansible role invokes the shared VM cleanup helper as a
second cleanup mechanism.

The final validation sweep provides an additional safeguard
against leftover resources.

Cleanup failures are recorded in the verification report.

## Execution

Run with the configured inventory and the test tag:

    hatch env run -e validation-default -- \
      ansible-playbook \
      -i inventory/reference/example.yml \
      playbooks/validation.yml \
      -t sriov_pci_passthrough

Replace the reference inventory with the target deployment inventory.

The validation is disabled by default because it creates
and terminates virtual machines.
