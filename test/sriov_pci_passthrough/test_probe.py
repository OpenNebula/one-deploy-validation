#!/usr/bin/env python3
"""Offline regression tests for the SR-IOV PCI passthrough probe."""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = (
    ROOT
    / "roles/sriov-pci-passthrough-validation/files/pci_probe.py"
)

spec = importlib.util.spec_from_file_location("pci_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


LIBVIRT_XML = """
<domain type="kvm">
  <devices>
    <hostdev mode="subsystem" type="pci" managed="yes">
      <driver name="vfio"/>
      <source>
        <address domain="0x0000" bus="0x84"
                 slot="0x06" function="0x0"/>
      </source>
      <address type="pci" domain="0x0000" bus="0x01"
               slot="0x00" function="0x0"/>
    </hostdev>
  </devices>
</domain>
"""


def configuration():
    return SimpleNamespace(
        template_id=0,
        host_id=1,
        host="nfhhvmadlb06",
        vf="84:06.0",
        vm_name="odv-sriov-pci-unit-test",
        memory=1024,
        vcpu=1,
        deploy_timeout=10,
        cleanup_timeout=10,
        poll_interval=1,
    )


def make_vm(vf="84:06.0"):
    return {
        "ID": "15",
        "NAME": "odv-sriov-pci-unit-test",
        "STATE": "3",
        "LCM_STATE": "3",
        "TEMPLATE": {
            "PCI": [{"SHORT_ADDRESS": vf}],
        },
    }


class TestPCIAddressParsing(unittest.TestCase):

    def test_short_address(self):
        self.assertEqual(
            probe.normalize_vf("84:06.0"),
            "0000:84:06.0",
        )

    def test_full_address(self):
        self.assertEqual(
            probe.normalize_vf("0000:84:06.0"),
            "0000:84:06.0",
        )

    def test_invalid_address(self):
        with self.assertRaises(probe.ProbeError):
            probe.normalize_vf("84:GG.0")

    def test_vm_pci_dict_and_list(self):
        self.assertEqual(
            probe.vm_pci_address(make_vm()),
            "0000:84:06.0",
        )
        vm = make_vm()
        vm["TEMPLATE"]["PCI"] = {"SHORT_ADDRESS": "84:06.0"}
        self.assertEqual(
            probe.vm_pci_address(vm),
            "0000:84:06.0",
        )

    def test_vm_multiple_pci_devices_rejected(self):
        vm = make_vm()
        vm["TEMPLATE"]["PCI"].append(
            {"SHORT_ADDRESS": "84:02.0"}
        )
        with self.assertRaises(probe.ProbeError):
            probe.vm_pci_address(vm)


class TestHostPCIInventory(unittest.TestCase):

    @mock.patch.object(probe, "get_host")
    def test_requested_vf_discovery(self, get_host):
        get_host.return_value = {
            "HOST_SHARE": {
                "PCI_DEVICES": {
                    "PCI": [
                        {"SHORT_ADDRESS": "84:02.0", "VMID": "-1"},
                        {"SHORT_ADDRESS": "84:06.0", "VMID": "-1"},
                    ]
                }
            }
        }

        result = probe.get_pci(1, "0000:84:06.0")

        self.assertEqual(result["SHORT_ADDRESS"], "84:06.0")
        self.assertEqual(result["VMID"], "-1")

    @mock.patch.object(probe, "get_host")
    def test_single_device_json_object(self, get_host):
        get_host.return_value = {
            "HOST_SHARE": {
                "PCI_DEVICES": {
                    "PCI": {
                        "SHORT_ADDRESS": "84:06.0",
                        "VMID": "-1",
                    }
                }
            }
        }

        result = probe.get_pci(1, "0000:84:06.0")
        self.assertEqual(result["VMID"], "-1")

    @mock.patch.object(probe, "get_host")
    def test_missing_vf_rejected(self, get_host):
        get_host.return_value = {
            "HOST_SHARE": {
                "PCI_DEVICES": {
                    "PCI": [
                        {"SHORT_ADDRESS": "84:02.0", "VMID": "-1"}
                    ]
                }
            }
        }

        with self.assertRaises(probe.ProbeError):
            probe.get_pci(1, "0000:84:06.0")


class TestLibvirt(unittest.TestCase):

    def test_physical_source_not_guest_address(self):
        result = probe.libvirt_pci_addresses(LIBVIRT_XML)

        self.assertEqual(result, ["0000:84:06.0"])

    def test_invalid_xml_rejected(self):
        with self.assertRaises(probe.ProbeError):
            probe.libvirt_pci_addresses("<domain><broken>")


class TestEndToEndLogic(unittest.TestCase):

    def prepare_mocks(self, patches):
        patches["get_host"].return_value = {
            "NAME": "nfhhvmadlb06"
        }
        patches["get_template"].return_value = {
            "TEMPLATE": {
                "DISK": {"IMAGE_ID": "0"}
            }
        }
        patches["get_pci"].side_effect = [
            {"SHORT_ADDRESS": "84:06.0", "VMID": "-1"},
            {"SHORT_ADDRESS": "84:06.0", "VMID": "15"},
        ]
        patches["run"].return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="VM ID: 15\n", stderr=""
        )
        patches["wait_for_hold"].return_value = make_vm()
        patches["wait_for_vm"].return_value = make_vm()
        patches["ssh_virsh"].side_effect = [
            "running",
            LIBVIRT_XML,
        ]
        patches["cleanup_vm"].return_value = (
            "VM 15 deleted; VF 0000:84:06.0 released"
        )

    def execute_with_mocks(self, changed_vf=None,
                           cleanup_error=None):
        functions = (
            "get_host",
            "get_template",
            "get_pci",
            "run",
            "wait_for_hold",
            "wait_for_vm",
            "ssh_virsh",
            "cleanup_vm",
        )

        with mock.patch.multiple(
            probe,
            **{name: mock.DEFAULT for name in functions}
        ) as patches:
            self.prepare_mocks(patches)

            if changed_vf:
                patches["wait_for_vm"].return_value = make_vm(
                    changed_vf
                )

            if cleanup_error:
                patches["cleanup_vm"].side_effect = (
                    probe.ProbeError(cleanup_error)
                )

            result = probe.execute(configuration())
            ssh_calls = patches["ssh_virsh"].call_count
            cleanup_calls = patches["cleanup_vm"].call_count

        return result, ssh_calls, cleanup_calls

    def test_vm15_exact_vf_assignment_passes(self):
        result, ssh_calls, cleanup_calls = (
            self.execute_with_mocks()
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["vm_id"], "15")
        self.assertEqual(ssh_calls, 2)
        self.assertEqual(cleanup_calls, 1)
        self.assertTrue(
            all(
                check["status"] == "ok"
                for check in result["checks"]
            )
        )

    def test_vm14_wrong_vf_assignment_fails(self):
        result, ssh_calls, cleanup_calls = (
            self.execute_with_mocks(changed_vf="84:02.0")
        )

        checks = {
            item["name"]: item for item in result["checks"]
        }

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            checks["PCI allocation"]["status"],
            "failed",
        )
        self.assertIn(
            "0000:84:02.0",
            checks["PCI allocation"]["detail"],
        )
        self.assertEqual(ssh_calls, 0)
        self.assertEqual(cleanup_calls, 1)

    def test_cleanup_failure_causes_overall_failure(self):
        result, _, _ = self.execute_with_mocks(
            cleanup_error="VF remains allocated"
        )

        checks = {
            item["name"]: item for item in result["checks"]
        }

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            checks["cleanup"]["status"],
            "failed",
        )

    @mock.patch.object(probe, "get_vm")
    @mock.patch.object(probe, "run")
    def test_cleanup_refuses_wrong_vm_identity(self, run, get_vm):
        get_vm.return_value = {
            "ID": "15",
            "NAME": "production-vm",
        }

        with self.assertRaises(probe.ProbeError):
            probe.cleanup_vm(
                "15",
                "odv-sriov-pci-unit-test",
                "1",
                "0000:84:06.0",
                10,
                1,
            )

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
