# Storage validation

Enable `validation.run_storage_validation` to verify a persistent data disk through guest reboot, VM redeployment, and hot-detach. The primary frontend runs the test on the lowest-ID monitored KVM host in each selected cluster. The role defaults to off.

The frontend needs working OpenNebula CLI access as `oneadmin`, a cluster image datastore with room for the test VM and a 64 MiB data image, and a vNet on which `oneadmin` can SSH to the guest as root. The Linux guest must support contextualization, ACPI shutdown, virtio-blk serials in sysfs, and the standard `dd`, `od`, and `sync` commands. The default marketplace app comes from `validation.test_vm.vm.market_name`, falling back to Alpine Linux 3.21. Use the usual `validation.clusters` and `validation.cluster_overrides` settings to select clusters, datastores, and networks.

## Running the checks

Merge the following settings into the inventory's existing `validation` mapping.

```yaml
validation:
  run_storage_validation: true
  storage_validation:
    timeout: 300
    poll_interval: 5
```

Both times are in seconds, per wait. Set `storage_validation.market_name` to use another compatible app. Run standalone storage checks with the following command.

```sh
make validation I=inventory/<env>/hosts.yml TAGS=storage_validation
```

For physical reboot coverage, also set `validation.run_host_reboot: true` and set `validation.host_reboot.target_nodes` to the intended hypervisors. Select both tags, or use a full validation run with both flags enabled.

```sh
make validation I=inventory/<env>/hosts.yml TAGS=storage_validation,host_reboot
```

## Reading the results

- **Hot-attach** creates a fresh persistent data image, attaches it to a running VM, and matches the OpenNebula image ID and disk serial to a newly visible guest block device. The probe writes and flushes one sector and checks its SHA-256 against the frontend's expected value.
- **Guest reboot and VM redeploy** each require a new guest boot ID and the original disk attachment, serial, and checksum. Redeploy waits for the same VM to reach `UNDEPLOYED` and deploys it back to the selected host.
- **Physical host reboot** uses a separate disposable VM on each eligible reboot target. It powers the VM off with the disk attached before the existing reboot sequence, then resumes it on the original host after the hypervisor boot ID changes and OpenNebula monitoring recovers. Host selection, frontend placement guards, and recovery follow the [host reboot workflow](host_reboot.md).
- **Hot-detach** requires the disk to disappear from the VM template and the guest while the guest is running, and the image to return to `READY`.
- **Cleanup** terminates the owned VM and deletes its data image. The report records a failed check independently of cleanup, with a reason for each dependent check skipped. The suite manifest lets the sweep retry interrupted cleanup. The data image is disposable even when exported marketplace images are kept.

Disk IOPS, bandwidth, and p99 latency are measured by the [fio storage benchmark](storage-benchmark.md). NFS availability is tracked separately.
