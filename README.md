[//]: # ( vim: set wrap : )

# one-deploy-validation

This repository provides a toolset for automating the validation of an OpenNebula deployment.

## Requirements

1. Install `hatch`, which is used by the [Makefile](./Makefile) to manage virtual environments.

   ```shell
   pip install hatch
   ```

   or

   ```shell
   pipx install hatch
   ```

   or use any other method you see fit.

## Playbooks/Roles

The development of the tests should be structured in [playbooks](./playbooks/) and [roles](./roles/) directories, following Ansible.

## Inventory/Execution

1. Inventories are kept in the [inventory](./inventory/) directory, please take a look at [example.yml](./inventory/example.yml)

1. To execute `ansible-playbook` you can run

   ```shell
   make I=inventory/reference/example.yml validation
   ```

## Validation Results

The execution of the all the tests produces the following output files on the Ansible controller (e.g. laptop):

- `/tmp/cloud_verification_report.html`
- `/tmp/conn-matrix-report.html`
- `/tmp/conn-matrix-raw-data.json`
