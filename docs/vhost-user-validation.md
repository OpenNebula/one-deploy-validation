# Vhost-User Runtime Path Validation

The `vhost-user-validation` role validates the host filesystem and security configuration required for Open vSwitch DPDK vhost-user networking.

The validation is read-only. It does not create directories, change ownership or permissions, modify ACLs, relabel files, install SELinux policy, or modify vhost-user sockets.

## Enable the test

The test is disabled by default.

Enable it in the validation configuration:

```yaml
validation:
  run_vhost_user_validation: true
```

The validation can be run independently with:

```bash
ansible-playbook \
  -i <inventory> \
  playbooks/validation.yml \
  --tags vhost_user_validation
```

## Objective

Vhost-user networking requires both QEMU/libvirt and Open vSwitch to access a shared UNIX socket path.

A host may have healthy DPDK and Open vSwitch runtime state while VM deployment still fails because the vhost-user socket directory has incorrect ownership, permissions, ACLs, or security labels.

Typical failures include:

```text
vhost-user directory missing after reboot

Open vSwitch cannot traverse a parent directory

Open vSwitch cannot access the socket directory

QEMU cannot create the server socket

required ACL entry is missing

an ACL exists but its effective permissions are restricted by the ACL mask

socket ownership or mode is incorrect

socket has an incorrect SELinux type

required SELinux policy is missing
```

This role validates these requirements independently.

## Default OneDeploy layout

The default validation policy mirrors the vhost-user layout created by OneDeploy.

The parent directory is expected to use:

```text
path  : /var/run/one
owner : 9869
group : 9869
mode  : 0770
ACL   : user:openvswitch:r-x
```

The vhost-user socket directory is expected to use:

```text
path         : /var/run/one/vhost-socks
owner        : 9869
group        : 9869
mode         : 0770
ACL          : user:openvswitch:rwx
default ACL  : default:user:openvswitch:rwx
SELinux type : virt_var_run_t
```

UID and GID `9869` are the standard OneDeploy/OpenNebula `oneadmin` identity.

These values are defaults only. They can be replaced through the validation inventory.

## Inventory configuration

The complete default configuration is equivalent to:

```yaml
validation:
  vhost_user:
    directories:
      - path: /var/run/one
        owner: 9869
        group: 9869
        mode: "0770"
        acls:
          - "user:openvswitch:r-x"
        access:
          - user: openvswitch
            permissions: "rx"
        selinux_type: null

      - path: /var/run/one/vhost-socks
        owner: 9869
        group: 9869
        mode: "0770"
        acls:
          - "user:openvswitch:rwx"
          - "default:user:openvswitch:rwx"
        access:
          - user: openvswitch
            permissions: "rwx"
        selinux_type: virt_var_run_t

    qemu_access:
      discover_running_users: true
      fallback_users:
        - oneadmin
      paths:
        - path: /var/run/one
          permissions: "x"
        - path: /var/run/one/vhost-socks
          permissions: "wx"

    sockets:
      directory: /var/run/one/vhost-socks
      inspect_existing: true
      expected_paths: []
      owner: 9869
      group: 9869
      mode: "0770"
      acls:
        - "user:openvswitch:rwx"
      access:
        - user: openvswitch
          permissions: "rw"
      selinux_type: virt_var_run_t

    selinux:
      validate: true
      policy_module: dpdk_virt
```

Directory and socket expectations can therefore be adapted for deployments using different runtime paths or identities.

The `directories` list replaces the default list when explicitly supplied, so an inventory overriding it should contain every directory that needs to be validated.

## Directory validation

Each configured directory is checked independently for existence, ownership, mode, ACLs, effective access and, when configured, SELinux type.

Separate report results are generated for:

```text
Vhost-user directory existence <path> on <hostname>

Vhost-user directory ownership <path> on <hostname>

Vhost-user directory mode <path> on <hostname>

Vhost-user directory ACLs <path> on <hostname>

Vhost-user directory access <path> on <hostname>

Vhost-user SELinux context <path> on <hostname>
```

A failure in one property does not hide the status of the others.

For example, an existing directory with the correct owner but an incorrect ACL reports existence and ownership as successful while the ACL validation fails.

## ACL validation

ACLs are read using:

```bash
getfacl -cp <path>
```

Configured entries are compared with the ACL entries returned by the host.

Both access and default ACLs can be validated.

For example:

```text
user:openvswitch:rwx
default:user:openvswitch:rwx
```

The validator also performs effective access tests instead of relying exclusively on the presence of an ACL entry.

This is important because an ACL entry can exist while the ACL mask reduces its effective permissions.

## Effective Open vSwitch access

Configured users are tested using their real UNIX credentials.

Equivalent read-only checks include:

```bash
runuser -u openvswitch -- test -r <path>
runuser -u openvswitch -- test -w <path>
runuser -u openvswitch -- test -x <path>
```

The default policy requires Open vSwitch to have traversal access to `/var/run/one` and full directory access to `/var/run/one/vhost-socks`.

## QEMU/libvirt access

The validator does not assume that QEMU always runs as the distribution's `qemu` account.

When enabled, the role discovers the owners of currently running QEMU processes and validates filesystem access using those identities.

Processes matching QEMU executables such as:

```text
qemu-kvm
qemu-kvm-one
qemu-system-*
```

are inspected.

If no running QEMU process is available, the configured fallback users are used.

The OneDeploy default fallback is:

```yaml
fallback_users:
  - oneadmin
```

The default required QEMU access is:

```text
/var/run/one             execute
/var/run/one/vhost-socks write + execute
```

Write and execute permission on the socket directory allows the QEMU process to create and remove UNIX socket directory entries.

## Existing socket validation

Existing UNIX sockets can be discovered automatically:

```yaml
sockets:
  directory: /var/run/one/vhost-socks
  inspect_existing: true
```

Each discovered socket is independently checked for:

```text
existence

UNIX socket file type

owner

group

mode

required ACL entries

effective configured-user access

SELinux type
```

Results use:

```text
Vhost-user socket <socket-path> on <hostname>
```

## Required socket paths

Static or otherwise required socket paths can be explicitly configured:

```yaml
validation:
  vhost_user:
    sockets:
      expected_paths:
        - /var/run/one/vhost-socks/example.sock
```

An expected socket that does not exist is reported as failed.

This check is independent of automatic inspection of existing sockets.

## Socket ownership and permissions

The default socket expectation is:

```text
owner        : 9869
group        : 9869
mode         : 0770
ACL          : user:openvswitch:rwx
SELinux type : virt_var_run_t
```

These values reflect sockets created by QEMU under the default OneDeploy directory and ACL configuration.

Deployments with a different QEMU identity or socket policy can override these settings in inventory.

## SELinux validation

SELinux handling is enabled by default when the host reports either:

```text
Enforcing
```

or:

```text
Permissive
```

For paths with a configured SELinux expectation, the current object type is inspected.

The default socket tree expectation is:

```text
virt_var_run_t
```

The default configuration also checks for the OneDeploy SELinux policy module:

```text
dpdk_virt
```

The policy module and filesystem labels are validated independently.

A host running SELinux in permissive mode can therefore demonstrate that the expected labels and policy are installed, but the validation does not claim that access denials are actively enforced in that mode.

When SELinux is disabled, SELinux-specific results are recorded as skipped rather than failed.

## Parent path traversal

Effective user access tests implicitly validate traversal through the path hierarchy.

If a required user cannot traverse a parent component, an access test on the configured target fails even when the target directory itself has permissive ACLs.

Tools such as:

```bash
namei -l <path>
```

remain useful for manual diagnosis but are not required to determine the validator result.

## Missing directories

A configured directory that does not exist is reported as an existence failure.

Ownership, mode, ACL, access and SELinux checks for that directory are then reported as skipped where they cannot be evaluated meaningfully.

This keeps structural failures separate from secondary checks.

## Hosts without existing sockets

An empty vhost-user socket directory is not automatically a validation failure.

When:

```yaml
inspect_existing: true
```

and no sockets currently exist, there may simply be no running VM using vhost-user networking.

If no `expected_paths` are configured either, socket inspection is recorded as:

```text
skipped — no existing or expected sockets
```

Directory, ACL, access and security checks still run.

## Host summary

A host-level result is recorded using:

```text
Vhost-user validation on <hostname>
```

The host summary is `failed` when any required directory, effective-access, SELinux-policy, socket or socket-property validation fails.

The summary includes the detected SELinux mode, discovered QEMU users, inspected socket paths and aggregated failure reasons.

## Validation after reboot

The validation is entirely based on current host state.

It can therefore be rerun after reboot to confirm that tmpfiles configuration, ACL application and security labeling have resulted in the required runtime state again.

A post-reboot execution validates the actual filesystem and socket environment rather than merely checking whether persistent configuration files exist.

This is particularly important for `/run` and `/var/run`, which are runtime filesystems recreated during boot.

## Runtime commands

The role uses read-only commands equivalent to:

```bash
stat
getfacl
getenforce
semodule -l
ps
id
runuser
find
```

No validation command changes host ownership, permissions, ACLs, SELinux configuration, sockets or Open vSwitch state.

## Scope

This validation covers vhost-user filesystem prerequisites: runtime directories, ownership, group, modes, ACL entries, effective UNIX access, QEMU process-user access, existing UNIX sockets, required socket paths, SELinux object types and the configured SELinux policy module.

It does not validate DPDK PCI binding, DPDK allowlist persistence, OVS bridge topology, DPDK interface initialization, PMD placement, NUMA locality, network throughput, or packet latency.

Those concerns are covered by separate validation tests.
