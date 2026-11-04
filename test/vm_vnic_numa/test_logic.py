#!/usr/bin/env python3
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROBE = ROOT / "roles" / "vm-vnic-numa-validation" / "files" / "probe.py"
spec = importlib.util.spec_from_file_location("vvn_probe", PROBE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class VerdictTests(unittest.TestCase):
    def test_balanced_all_local_ok(self):
        result, _ = mod.evaluate_balanced_bond(0, [
            {"name": "a", "numa_node": 0, "eligible": True},
            {"name": "b", "numa_node": 0, "eligible": True},
        ])
        self.assertEqual(result, "ok")

    def test_balanced_mixed_is_degraded(self):
        result, findings = mod.evaluate_balanced_bond(0, [
            {"name": "a", "numa_node": 0, "eligible": True},
            {"name": "b", "numa_node": 1, "eligible": True},
        ])
        self.assertEqual(result, "degraded")
        self.assertTrue(any("local and remote" in x for x in findings))

    def test_balanced_all_remote_failed(self):
        result, _ = mod.evaluate_balanced_bond(0, [
            {"name": "a", "numa_node": 1, "eligible": True},
            {"name": "b", "numa_node": 1, "eligible": True},
        ])
        self.assertEqual(result, "failed")

    def test_active_backup_local_with_remote_standby_degraded(self):
        result, _ = mod.evaluate_active_backup(0, [
            {"name": "a", "numa_node": 0, "eligible": True},
            {"name": "b", "numa_node": 1, "eligible": True},
        ], "a")
        self.assertEqual(result, "degraded")

    def test_active_backup_remote_active_failed(self):
        result, _ = mod.evaluate_active_backup(0, [
            {"name": "a", "numa_node": 1, "eligible": True},
            {"name": "b", "numa_node": 0, "eligible": True},
        ], "a")
        self.assertEqual(result, "failed")

    def test_pmd_mixed_degraded(self):
        result, _ = mod.evaluate_nodes(1, [1, 0])
        self.assertEqual(result, "degraded")

    def test_severity_order(self):
        self.assertEqual(mod.worst("ok", "unknown"), "unknown")
        self.assertEqual(mod.worst("unknown", "degraded"), "degraded")
        self.assertEqual(mod.worst("degraded", "failed"), "failed")


    def test_report_classifies_degraded_as_warning(self):
        report = (ROOT / "playbooks" / "templates" / "appendix-cloud-report.j2").read_text()
        self.assertIn("'degraded' in text", report)
        self.assertIn("vt.startswith('degraded')", report)
        self.assertIn("run_vm_vnic_numa_validation", report)
        self.assertIn("VM vNIC NUMA locality", report)

    def test_report_explicit_degraded_overrides_diagnostic_text(self):
        import re
        from jinja2 import Environment

        source = (
            ROOT / "playbooks" / "templates" /
            "appendix-cloud-report.j2"
        ).read_text()

        begin = source.index("{% macro status_from(")
        end = source.index("{%- endmacro %}", begin) + len("{%- endmacro %}")

        env = Environment()
        env.filters["regex_search"] = (
            lambda value, pattern: re.search(pattern, value)
        )

        template = env.from_string(
            source[begin:end] +
            "{{ status_from(value, comment) | trim }}"
        )

        cases = [
            (
                "degraded",
                '{"findings":["physical uplink vmnic0 path locality: failed"]}',
                "warn",
            ),
            (
                "degraded",
                '{"error":"remote physical RX PMD"}',
                "warn",
            ),
            (
                "failed",
                '{"findings":["balanced bond degraded"]}',
                "fail",
            ),
            ("ok", "", "pass"),
            ("unknown", "", "warn"),
        ]

        for value, comment, expected in cases:
            with self.subTest(value=value, comment=comment):
                actual = template.render(
                    value=value, comment=comment
                ).strip()
                self.assertEqual(actual, expected)


    def test_pmd_parser_fixture(self):
        text = """
pmd thread numa_id 0 core_id 40:
  port: one-60-0          queue-id:  0 (enabled)   pmd usage: NOT AVAIL
  port: one-60-0          queue-id:  1 (enabled)   pmd usage: NOT AVAIL
pmd thread numa_id 1 core_id 41:
  port: vmnic0            queue-id:  0 (enabled)   pmd usage: NOT AVAIL
"""
        rows = mod.parse_pmd_rxq(text)
        self.assertEqual(rows[0], {"port": "one-60-0", "queue": 0, "core": 40, "numa": 0})
        self.assertEqual(rows[1]["queue"], 1)
        self.assertEqual(rows[2]["numa"], 1)

    def test_partial_unknown_does_not_claim_all_remote_or_local(self):
        for nodes in ([1, None], [0, None], [None]):
            with self.subTest(nodes=nodes):
                self.assertEqual(mod.evaluate_nodes(0, nodes)[0], "unknown")
        self.assertEqual(mod.evaluate_nodes(0, [0, 1, None])[0], "degraded")

    def test_active_backup_local_with_unknown_standby(self):
        members = [
            {"name": "a", "numa_node": 0, "eligible": True},
            {"name": "b", "numa_node": None, "eligible": True},
        ]
        self.assertEqual(mod.evaluate_active_backup(0, members, "a")[0], "unknown")
        members.append({"name": "c", "numa_node": 1, "eligible": True})
        self.assertEqual(mod.evaluate_active_backup(0, members, "a")[0], "degraded")

    def test_disabled_rx_queues_are_excluded(self):
        rows = mod.parse_pmd_rxq("""
pmd thread numa_id 0 core_id 0:
  port: vmnic0 queue-id: 0 (disabled)  pmd usage: 0 %
pmd thread numa_id 1 core_id 1:
  port: vmnic0 queue-id: 1 (enabled)   pmd usage: 0 %
""")
        self.assertEqual(rows, [{"port": "vmnic0", "queue": 1, "core": 1, "numa": 1}])

    def test_physical_pmd_local_and_remote_to_nic(self):
        for pmd_nodes, expected in [([1], "ok"), ([0], "failed"), ([0, 1], "degraded"), ([], "unknown")]:
            with self.subTest(pmd_nodes=pmd_nodes):
                member = {"name": "vmnic0", "numa_node": 1, "eligible": True}
                rows = [{"port": "vmnic0", "queue": i, "core": 40 + i, "numa": n}
                        for i, n in enumerate(pmd_nodes)]
                result, _ = mod.evaluate_uplink_pmds(1, [member], rows)
                self.assertEqual(result, expected)
                self.assertEqual(member["pmd"]["result"], expected)

    def test_unknown_nic_pci_numa_cannot_pass(self):
        member = {"name": "vmnic0", "numa_node": None, "eligible": True}
        rows = [{"port": "vmnic0", "queue": 0, "core": 41, "numa": 1}]
        self.assertEqual(mod.evaluate_uplink_pmds(1, [member], rows)[0], "unknown")

    def test_ineligible_physical_member_excluded_from_pmd_verdict(self):
        members = [
            {"name": "vmnic0", "numa_node": 1, "eligible": True},
            {"name": "vmnic6", "numa_node": 0, "eligible": False},
        ]
        rows = [{"port": "vmnic0", "queue": 0, "core": 41, "numa": 1},
                {"port": "vmnic6", "queue": 0, "core": 40, "numa": 0}]
        self.assertEqual(mod.evaluate_uplink_pmds(1, members, rows)[0], "ok")

    def test_direct_uplink_uses_physical_pmd_verdict(self):
        from unittest.mock import patch
        def fake_run(argv):
            if argv == ["ovs-vsctl", "list-ports", "ovsbr0"]:
                return {"rc": 0, "stdout": "vmnic0", "stderr": ""}
            raise AssertionError(argv)
        member = {"name": "vmnic0", "pci": "0000:83:00.0", "numa_node": 1}
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "port_interface_names", return_value=["vmnic0"]), \
             patch.object(mod, "dpdk_interface", return_value=member):
            uplinks = mod.uplinks_for_bridge("ovsbr0", "one-99-0", 1,
                [{"port": "vmnic0", "queue": 0, "core": 0, "numa": 0}])
        self.assertEqual(uplinks[0]["result"], "failed")
        self.assertEqual(uplinks[0]["members"][0]["pmd"]["pci_result"], "failed")

    def test_balanced_bond_classifies_mixed_and_misplaced_physical_pmds(self):
        from unittest.mock import patch
        def fake_run(argv):
            if argv == ["ovs-vsctl", "list-ports", "ovsbr0"]:
                return {"rc": 0, "stdout": "bond0", "stderr": ""}
            if argv == ["ovs-appctl", "bond/show", "bond0"]:
                return {"rc": 0, "stdout": """bond_mode: balance-slb
member vmnic0: enabled
  active member
  may_enable: true
member vmnic6: enabled
  may_enable: true""", "stderr": ""}
            raise AssertionError(argv)
        members = {"vmnic0": {"name": "vmnic0", "numa_node": 1},
                   "vmnic6": {"name": "vmnic6", "numa_node": 0}}
        def fake_interface(name):
            return members[name].copy()
        with patch.object(mod, "run", side_effect=fake_run), \
             patch.object(mod, "port_interface_names", return_value=["vmnic0", "vmnic6"]), \
             patch.object(mod, "dpdk_interface", side_effect=fake_interface):
            local_per_nic = [{"port": "vmnic0", "queue": 0, "core": 41, "numa": 1},
                             {"port": "vmnic6", "queue": 0, "core": 40, "numa": 0}]
            misplaced = [{**local_per_nic[0], "numa": 0}, local_per_nic[1]]
            self.assertEqual(mod.uplinks_for_bridge("ovsbr0", "one-99-0", 1, local_per_nic)[0]["result"], "degraded")
            self.assertEqual(mod.uplinks_for_bridge("ovsbr0", "one-99-0", 1, misplaced)[0]["result"], "failed")
            # A remote NIC with a VM-local PMD does not make the entire bond
            # FAILED when the other NIC and its PMD are both local.
            remote_nic_remote_pmd = [local_per_nic[0], {**local_per_nic[1], "numa": 1}]
            self.assertEqual(mod.uplinks_for_bridge("ovsbr0", "one-99-0", 1, remote_nic_remote_pmd)[0]["result"], "degraded")
            # Cross-wired PMDs mean neither bond member offers a local path.
            cross_wired = [{**local_per_nic[0], "numa": 0},
                           {**local_per_nic[1], "numa": 1}]
            self.assertEqual(mod.uplinks_for_bridge("ovsbr0", "one-99-0", 1, cross_wired)[0]["result"], "failed")



if __name__ == "__main__":
    unittest.main()
