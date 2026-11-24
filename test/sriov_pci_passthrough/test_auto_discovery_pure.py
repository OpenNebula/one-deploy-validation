#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest

from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "roles/sriov-pci-passthrough-validation/files/discover_vfs.py"
)

spec = importlib.util.spec_from_file_location(
    "odv_discover_vfs", SOURCE
)

discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


class TestPCIParsing(unittest.TestCase):

    def test_short_address(self):
        self.assertEqual(
            discovery.canonical("86:02.0"),
            "0000:86:02.0",
        )

    def test_full_address(self):
        self.assertEqual(
            discovery.canonical("0000:8A:06.1"),
            "0000:8a:06.1",
        )

    def test_invalid_address(self):
        with self.assertRaises(ValueError):
            discovery.canonical("86:02.8")

    def test_singleton_pci_object(self):
        self.assertEqual(
            discovery.as_list({"SRIOV": "vf"}),
            [{"SRIOV": "vf"}],
        )

    def test_empty_pci_inventory(self):
        self.assertEqual(discovery.as_list(None), [])


class TestVFEligibility(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.root = Path(self.tmp.name)
        self.pf = "0000:86:00.0"
        self.vf = "0000:86:02.0"

        pf_path = (
            self.root / "sys/bus/pci/devices" / self.pf
        )
        pf_path.mkdir(parents=True)

        (pf_path / "sriov_numvfs").write_text("2")

        self.pf_path = pf_path

        self.driver = (
            self.root / "sys/bus/pci/drivers/vfio-pci"
        )
        self.driver.mkdir(parents=True)

        self.add_vf(self.vf, "39")

        self.device = {
            "SHORT_ADDRESS": "86:02.0",
            "SRIOV": "vf",
            "VMID": "-1",
        }

    def add_vf(self, address, group_number):
        vf_path = (
            self.root / "sys/bus/pci/devices" / address
        )
        vf_path.mkdir(parents=True)

        (vf_path / "physfn").symlink_to(self.pf_path)
        (vf_path / "driver").symlink_to(self.driver)

        group = (
            self.root
            / "sys/kernel/iommu_groups"
            / group_number
        )

        (group / "devices").mkdir(parents=True)

        (vf_path / "iommu_group").symlink_to(group)
        (group / "devices" / address).symlink_to(vf_path)

        return vf_path, group

    def virtual_path(self, value):
        value = str(value)

        if value.startswith("/sys/"):
            return self.root / value.lstrip("/")

        return Path(value)

    def inspect(self, owners=None, attached=None, incomplete=None):
        with mock.patch.object(
            discovery,
            "Path",
            side_effect=self.virtual_path,
        ):
            return discovery.check_vf(
                self.device,
                attached or set(),
                owners or {},
                incomplete or [],
            )

    def test_free_vf_is_eligible(self):
        self.assertEqual(
            self.inspect()["status"],
            "eligible",
        )

    def test_opennebula_allocated_vf_is_skipped(self):
        self.device["VMID"] = "271"

        result = self.inspect()

        self.assertEqual(result["status"], "skipped")
        self.assertIn("271", str(result["reasons"]))

    def test_busy_vfio_group_is_skipped(self):
        result = self.inspect(
            owners={"/dev/vfio/39": ["42424"]}
        )

        self.assertEqual(result["status"], "skipped")
        self.assertIn("42424", str(result["reasons"]))

    def test_shared_iommu_group_is_skipped(self):
        group_devices = (
            self.root
            / "sys/kernel/iommu_groups/39/devices"
        )

        (group_devices / "0000:86:02.1").touch()

        result = self.inspect()

        self.assertEqual(result["status"], "skipped")
        self.assertIn(
            "other devices",
            str(result["reasons"]),
        )

    def test_libvirt_assigned_vf_is_skipped(self):
        result = self.inspect(attached={self.vf})

        self.assertEqual(result["status"], "skipped")
        self.assertIn("libvirt", str(result["reasons"]))

    def test_incomplete_process_scan_is_skipped(self):
        result = self.inspect(incomplete=["999"])

        self.assertEqual(result["status"], "skipped")
        self.assertIn(
            "incomplete",
            str(result["reasons"]),
        )

    def test_wrong_driver_is_skipped(self):
        wrong_driver = (
            self.root
            / "sys/bus/pci/drivers/ixgbevf"
        )
        wrong_driver.mkdir(parents=True)

        driver_link = (
            self.root
            / "sys/bus/pci/devices"
            / self.vf
            / "driver"
        )

        driver_link.unlink()
        driver_link.symlink_to(wrong_driver)

        result = self.inspect()

        self.assertEqual(result["status"], "skipped")
        self.assertIn("ixgbevf", str(result["reasons"]))

    def test_disabled_vfs_are_skipped(self):
        (self.pf_path / "sriov_numvfs").write_text("0")

        result = self.inspect()

        self.assertEqual(result["status"], "skipped")

    def test_only_one_vf_selected_per_pf(self):
        self.add_vf("0000:86:02.1", "18")

        payload = {
            "host": "kvm-test-01.example",
            "host_id": 42,
            "pci": [
                {
                    "SHORT_ADDRESS": "86:02.0",
                    "SRIOV": "vf",
                    "VMID": "-1",
                },
                {
                    "SHORT_ADDRESS": "86:02.1",
                    "SRIOV": "vf",
                    "VMID": "-1",
                },
            ],
        }

        output = io.StringIO()

        with (
            mock.patch.object(
                discovery,
                "Path",
                side_effect=self.virtual_path,
            ),
            mock.patch.object(
                discovery,
                "libvirt_attachments",
                return_value=set(),
            ),
            mock.patch.object(
                discovery,
                "process_fds",
                return_value=({}, []),
            ),
            mock.patch(
                "sys.stdin",
                io.StringIO(json.dumps(payload)),
            ),
            contextlib.redirect_stdout(output),
        ):
            rc = discovery.main()

        self.assertEqual(rc, 0)

        result = json.loads(output.getvalue())

        self.assertEqual(len(result["selected"]), 1)
        self.assertEqual(
            result["selected"][0]["pf"],
            self.pf,
        )

    def test_failed_ownership_discovery_is_not_silent(self):
        payload = {
            "host": "kvm-test-01.example",
            "host_id": 42,
            "pci": [self.device],
        }

        output = io.StringIO()

        with (
            mock.patch.object(
                discovery,
                "libvirt_attachments",
                side_effect=RuntimeError("Permission denied"),
            ),
            mock.patch(
                "sys.stdin",
                io.StringIO(json.dumps(payload)),
            ),
            contextlib.redirect_stdout(output),
        ):
            rc = discovery.main()

        result = json.loads(output.getvalue())

        self.assertNotEqual(rc, 0)
        self.assertEqual(result["selected"], [])
        self.assertTrue(result["errors"])


class TestIntegrationSafetyContracts(unittest.TestCase):

    def test_cleanup_failure_guards_exist(self):
        source = (
            ROOT
            / "playbooks/tasks/sriov-pci-auto-per-vf.yml"
        ).read_text()

        self.assertIn(
            "(_tvbn_survivors | default([]) | length) == 0",
            source,
        )
        self.assertIn(
            "(odv_leftovers | default([]) | length) == 0",
            source,
        )
        self.assertIn("  rescue:", source)
        self.assertIn("  always:", source)
        self.assertIn("result_value: failed", source)

    def test_no_premature_manifest_deletion(self):
        source = (
            ROOT / "playbooks/sriov-pci-auto.yml"
        ).read_text()

        self.assertNotIn(
            "Remove completed SR-IOV entries from persisted manifest",
            source,
        )


if __name__ == "__main__":
    unittest.main()
