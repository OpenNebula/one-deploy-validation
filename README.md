[//]: # ( vim: set wrap : )

# one-deploy-validation

This repository provides a toolset for automating the validation of an OpenNebula deployment.

## Requirements

1. Install `hatch`, which is used by the [Makefile](./Makefile) to manage virtual environments.

   ```shell
   pip install hatch uv
   ```

   or

   ```shell
   pipx install hatch
   pipx install uv
   ```

   or use any other method you see fit.

## Playbooks/Roles

The development of the tests should be structured in [playbooks](./playbooks/) and [roles](./roles/) directories, following Ansible.

## Inventory/Execution

1. Inventories are kept in the [inventory](./inventory/) directory, please take a look at [example.yml](./inventory/reference/example.yml)

1. Customize the configuration of the validation testcases by setting the enabler flags (`validation.run_*` booleans) in [all.yml](./inventory/reference/group_vars/all.yml). This file contains the recommended configuration that should be tested on most deployments. Always intend to enable all testcases that are relevant for the deployment (for example, if VM HA or FE HA are configured, enable those testcases as well). 
If any testcase is disabled compared to the current [all.yml](./inventory/reference/group_vars/all.yml), please provide justification in addition to the generated HTML reports.

1. To execute `ansible-playbook` you can run

   ```shell
   make I=inventory/reference/example.yml validation
   ```

## Validation Results

The execution of the all the tests produces the following output files on the Ansible controller (e.g. laptop):

- `/tmp/cloud_verification_report.html`
- `/tmp/conn-matrix-report.html`
- `/tmp/conn-matrix-raw-data.json`
- `/tmp/fe_ha_report.html`
