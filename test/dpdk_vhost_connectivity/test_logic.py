#!/usr/bin/env python3
"""Regression tests for DPDK cross-host connectivity validation."""

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles/dpdk-vhost-connectivity-validation/files"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load_module("dpdk_plan", ROLE / "plan.py")
probe = load_module("dpdk_probe", ROLE / "probe.py")


class HostSelectionTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [
            {
                "ID": "1",
                "NAME": "kvm-a.example.test",
                "STATE": "2",
                "VM_MAD": "kvm",
                "CLUSTER_ID": "0",
            },
            {
                "ID": "2",
                "NAME": "kvm-b.example.test",
                "STATE": "2",
                "VM_MAD": "kvm",
                "CLUSTER_ID": "0",
            },
            {
                "ID": "3",
                "NAME": "kvm-offline.example.test",
                "STATE": "3",
                "VM_MAD": "kvm",
                "CLUSTER_ID": "0",
            },
        ]
        self.inventory = [h["NAME"] for h in self.hosts]

    def test_selects_two_online_hosts(self):
        selected = plan.choose_hosts(
            self.hosts, self.inventory, [], []
        )
        self.assertEqual(
            [h["id"] for h in selected], ["1", "2"]
        )

    def test_offline_host_cannot_be_selected(self):
        with self.assertRaises(ValueError):
            plan.choose_hosts(
                self.hosts,
                self.inventory,
                [
                    "kvm-a.example.test",
                    "kvm-offline.example.test",
                ],
                [],
            )

    def test_excluded_host_is_not_selected(self):
        selected = plan.choose_hosts(
            self.hosts,
            self.inventory,
            [],
            ["kvm-b.example.test"],
        )
        self.assertEqual(selected, [])

    def test_explicit_hosts_require_same_cluster(self):
        hosts = [dict(h) for h in self.hosts]
        hosts[1]["CLUSTER_ID"] = "1"

        with self.assertRaises(ValueError):
            plan.choose_hosts(
                hosts,
                self.inventory,
                [
                    "kvm-a.example.test",
                    "kvm-b.example.test",
                ],
                [],
            )


class NetworkSelectionTests(unittest.TestCase):
    def setUp(self):
        self.vnet = {
            "ID": "5",
            "NAME": "tenant-dpdk",
            "CLUSTERS": {"ID": ["0"]},
            "TEMPLATE": {
                "VN_MAD": "ovswitch",
                "BRIDGE_TYPE": "openvswitch_dpdk",
                "BRIDGE": "dpdk-br0",
                "VLAN_ID": "120",
            },
            "AR_POOL": {
                "AR": {
                    "AR_ID": "0",
                    "TYPE": "IP4",
                    "IP": "198.51.100.10",
                    "SIZE": "8",
                    "USED_LEASES": "3",
                }
            },
        }

    def test_valid_dpdk_network_selected(self):
        selected = plan.vnet_candidates(
            [self.vnet], "0"
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["name"], "tenant-dpdk")
        self.assertEqual(selected[0]["free_ipv4"], 5)

    def test_insufficient_free_leases_rejected(self):
        self.vnet["AR_POOL"]["AR"]["USED_LEASES"] = "7"
        self.assertEqual(
            plan.vnet_candidates([self.vnet], "0"),
            [],
        )

    def test_non_dpdk_network_rejected(self):
        self.vnet["TEMPLATE"]["BRIDGE_TYPE"] = "linux"
        self.assertEqual(
            plan.vnet_candidates([self.vnet], "0"),
            [],
        )

    def test_wrong_cluster_rejected(self):
        self.assertEqual(
            plan.vnet_candidates([self.vnet], "1"),
            [],
        )


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.xml = """
<domain>
  <devices>
    <interface type="vhostuser">
      <source type="unix" path="/run/one/vhost/one-42-0" mode="server"/>
      <target dev="one-42-0"/>
      <model type="virtio"/>
    </interface>
  </devices>
</domain>
"""
        self.ovs = {
            "type": "dpdkvhostuserclient",
            "path": "/run/one/vhost/one-42-0",
            "bridge": "dpdk-br0",
            "datapath_type": "netdev",
            "error": "[]",
            "ofport": "5",
            "tag": "120",
            "trunks": "[]",
            "vlan_mode": "access",
        }

    def verify(self):
        return probe.verify(
            self.xml,
            "one-42-0",
            "dpdk-br0",
            "120",
            self.ovs,
            True,
        )

    def test_valid_vhost_user_interface(self):
        self.assertEqual(self.verify(), [])

    def test_wrong_vlan_detected(self):
        self.ovs["tag"] = "121"
        self.assertTrue(
            any("VLAN" in message for message in self.verify())
        )

    def test_wrong_socket_detected(self):
        self.ovs["path"] = "/run/one/vhost/other.sock"
        self.assertTrue(
            any("socket" in message for message in self.verify())
        )

    def test_invalid_ofport_detected(self):
        self.ovs["ofport"] = "-1"
        self.assertTrue(
            any("ofport" in message for message in self.verify())
        )


if __name__ == "__main__":
    unittest.main()
