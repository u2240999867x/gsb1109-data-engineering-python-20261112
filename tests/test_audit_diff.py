# vim:ts=4 sw=4 expandtab softtabstop=4
"""差分校验的回归测试：锁定 jsonmerge 合并结果与 get_schema() 的差异家族。

运行: .venv/bin/pytest tests/test_audit_diff.py -v
"""
import copy
import unittest

import jsonschema

from jsonmerge import Merger
from jsonmerge.exceptions import HeadInstanceError

from audit.diff_audit import (
    SCHEMA, SCHEMA_SPLIT, EVENTS, DEFAULT_MERGE_OPTIONS,
    READING_METADATA_TEMPLATE, classify_branches, vin_errors,
    vout_errors, contract_violations, metadata_drift, replay,
    enumerate_schema, hazard_mutation,
)

EVENT = {e["id"]: e for e in EVENTS}


def merged(schema, ids):
    merger = Merger(copy.deepcopy(schema))
    base = None
    for ev_id in ids:
        ev = EVENT[ev_id]
        base = merger.merge(base, ev["doc"],
                            merge_options=ev.get("merge_options",
                                                 DEFAULT_MERGE_OPTIONS))
    return base


class TestIndependentValidators(unittest.TestCase):

    def test_every_well_formed_event_validates(self):
        for ev in EVENTS:
            if ev["id"].startswith(("E5", "E6", "E7", "E8")):
                self.assertTrue(vin_errors(SCHEMA, ev["doc"]), ev["id"])
            else:
                self.assertEqual([], vin_errors(SCHEMA, ev["doc"]), ev["id"])

    def test_branch_labeler_is_exclusive(self):
        labels = classify_branches(SCHEMA, EVENT["E1_laptop_rollout_s1temp"]["doc"])
        self.assertEqual([0], labels["#/config"])
        self.assertEqual([0], labels["#/firmware"])
        self.assertEqual([0], labels["#/sensors/*/payload"][0]["branches"])
        switched = classify_branches(
            SCHEMA, EVENT["E3_config_switch_sensor"]["doc"])
        self.assertEqual([1], switched["#/config"])


class TestFalseReject(unittest.TestCase):

    def test_top_level_oneof_branch_switch_rejected(self):
        # 两个 head 各自完全合法，但分支 laptop(0) -> sensorNode(1)
        self.assertEqual([], vin_errors(SCHEMA, EVENT["E1_laptop_rollout_s1temp"]["doc"]))
        self.assertEqual([], vin_errors(SCHEMA, EVENT["E3_config_switch_sensor"]["doc"]))
        merger = Merger(copy.deepcopy(SCHEMA))
        base = merger.merge(None, EVENT["E1_laptop_rollout_s1temp"]["doc"],
                            merge_options=DEFAULT_MERGE_OPTIONS)
        with self.assertRaises(HeadInstanceError) as cm:
            merger.merge(base, EVENT["E3_config_switch_sensor"]["doc"],
                         merge_options=DEFAULT_MERGE_OPTIONS)
        self.assertEqual("#/config", cm.exception.value.ref)

    def test_nested_oneof_branch_switch_inside_array_rejected(self):
        merger = Merger(copy.deepcopy(SCHEMA))
        base = merger.merge(None, EVENT["E1_laptop_rollout_s1temp"]["doc"],
                            merge_options=DEFAULT_MERGE_OPTIONS)
        with self.assertRaises(HeadInstanceError) as cm:
            merger.merge(base, EVENT["E4_payload_switch_geo"]["doc"],
                         merge_options=DEFAULT_MERGE_OPTIONS)
        self.assertEqual("#/sensors/0/payload", cm.exception.value.ref)


class TestFalseAccept(unittest.TestCase):

    def test_append_strips_unique_and_max_items(self):
        # 两个完全合法的 head 合并后破坏 uniqueItems，但 get_schema 结果放行
        out = merged(SCHEMA, ["E1_laptop_rollout_s1temp",
                              "E1_laptop_rollout_s1temp"])
        downstream = Merger(copy.deepcopy(SCHEMA)).get_schema()
        self.assertEqual([], vout_errors(downstream, out))
        paths = [p for p, _, _ in contract_violations(SCHEMA, out)]
        self.assertIn("#/tags", paths)

    def test_version_metadata_override_drift_silently_accepted(self):
        out = merged(SCHEMA, ["E9_metadata_blank_override"])
        entry = out["sensors"][0]["reading"][0]
        self.assertNotIn("rev", entry)
        # 审计契约仍要求模板键 rev
        drift_paths = [p for p, _, _ in metadata_drift(SCHEMA, out)]
        self.assertIn("#/sensors/0/reading/0", drift_paths)
        # 原始 schema、下游 schema 都放行
        self.assertEqual([], vin_errors(SCHEMA, EVENT["E9_metadata_blank_override"]["doc"]))
        downstream = Merger(copy.deepcopy(SCHEMA)).get_schema()
        self.assertEqual([], vout_errors(downstream, out))

    def test_array_item_without_id_silently_skipped(self):
        out = merged(SCHEMA, ["E5_sensor_item_without_id"])
        # 无 id 的数组项被静默丢弃，下游 schema 在该路径没有任何报错
        self.assertEqual(1, len(out["sensors"]))
        downstream = Merger(copy.deepcopy(SCHEMA)).get_schema()
        self.assertEqual([], vout_errors(downstream, out))
        bad = [p for v in vin_errors(SCHEMA, EVENT["E5_sensor_item_without_id"]["doc"])
               for p in [v[0]]]
        self.assertIn("sensors/0", bad)


class TestDownstreamFalseReject(unittest.TestCase):

    def test_split_brain_metadata_schema_rejects_valid_merge(self):
        # 运行模板只写 rev，metadataSchema 却必填 rev+at
        self.assertEqual([], vin_errors(
            SCHEMA_SPLIT, EVENT["E1_laptop_rollout_s1temp"]["doc"]))
        out = merged(SCHEMA_SPLIT, ["E1_laptop_rollout_s1temp"])
        downstream = Merger(copy.deepcopy(SCHEMA_SPLIT)).get_schema()
        errors = vout_errors(downstream, out)
        self.assertEqual(["firmware/0"], [p for p, _ in errors])
        self.assertIn("'at' is a required property", errors[0][1])


class TestGetSchemaHazard(unittest.TestCase):

    def test_get_schema_mutates_caller_schema(self):
        hazard = hazard_mutation()
        self.assertTrue(hazard["mutated"])
        # 改写后 version 策略丢失，输出不再是历史数组
        self.assertNotIsInstance(
            hazard["example_output_after_mutation"]["firmware"], list)


class TestEnumerationInvariants(unittest.TestCase):

    def test_baseline_enumeration_counts(self):
        run = enumerate_schema(SCHEMA, "consistent_baseline")
        self.assertEqual(819, run["enumeration"]["prefixes_replayed"])
        self.assertEqual(7, run["enumeration"]["consistent_prefixes"])
        classes = {f["class"] for f in run["findings"]}
        self.assertEqual(
            {"FALSE_REJECT", "FALSE_ACCEPT", "INPUT_INVALID_ACCEPTED"},
            classes)

    def test_every_finding_has_replayable_prefix_and_paths(self):
        run = enumerate_schema(SCHEMA, "consistent_baseline")
        for finding in run["findings"]:
            self.assertTrue(finding["prefix"])
            self.assertTrue(finding["paths"])
            self.assertTrue(finding["branches"])
            # 前缀中的每个事件都能在事件域中找到
            for step in finding["prefix"]:
                self.assertIn(step["id"], EVENT)


if __name__ == "__main__":
    unittest.main()
