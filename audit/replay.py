"""按 report.json 中的最短前缀重放单个差异，逐步打印分支与三校验器结果。

用法:
  .venv/bin/python audit/replay.py                # 列出全部差异
  .venv/bin/python audit/replay.py 3              # 重放基线第 3 条
  .venv/bin/python audit/replay.py split 1        # 重放分裂变体第 1 条
  .venv/bin/python audit/replay.py hazard         # 重放 get_schema 就地改写
"""
import copy
import json
import os
import sys

# 允许以 `python audit/replay.py` 直接运行：把仓库根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jsonschema

from jsonmerge import Merger
from jsonmerge.exceptions import JSONMergeError

from audit.diff_audit import (
    SCHEMA, SCHEMA_SPLIT, EVENTS, DEFAULT_MERGE_OPTIONS,
    classify_branches, vin_errors, vout_errors,
)

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_BY_ID = {e["id"]: e for e in EVENTS}


def load_report():
    with open(os.path.join(HERE, "report.json"), encoding="utf-8") as fh:
        return json.load(fh)


def list_findings(report):
    for section_name, key in (("baseline", "baseline_run"),
                              ("split", "metadata_split_brain_run")):
        print("### %s" % section_name)
        for i, f in enumerate(report[key]["findings"], 1):
            print("  %s %2d  %-26s %-30s %s" % (
                section_name, i, f["class"], f["family"],
                " -> ".join(e["id"] for e in f["prefix"])))
    print("  hazard  get_schema_in_place_mutation")


def replay_prefix(schema, finding):
    merger = Merger(copy.deepcopy(schema))
    downstream = Merger(copy.deepcopy(schema)).get_schema()
    base = None
    for step, ev_desc in enumerate(finding["prefix"], 1):
        ev = EVENT_BY_ID[ev_desc["id"]]
        options = ev.get("merge_options", DEFAULT_MERGE_OPTIONS)
        print("\n--- 事件 %d: %s" % (step, ev["id"]))
        print("    V_in    :", vin_errors(schema, ev["doc"]) or "PASS")
        print("    分支标注:", json.dumps(classify_branches(schema, ev["doc"]),
                                          ensure_ascii=False))
        try:
            base = merger.merge(base, ev["doc"], merge_options=options)
        except JSONMergeError as exc:
            print("    merge   : RAISE %s @ %s（%s）%s" % (
                type(exc).__name__, getattr(exc.value, "ref", None),
                exc.strategy_name, exc.message))
            return
        print("    合并产物:", json.dumps(base, ensure_ascii=False))
    print("\n=== 最终 V_out（get_schema 结果）:",
          vout_errors(downstream, base) or "PASS")


def replay_hazard():
    victim = copy.deepcopy(SCHEMA)
    before = copy.deepcopy(victim)
    Merger(victim).get_schema()
    print("get_schema() 后输入 schema 是否被改写:", victim != before)
    out = Merger(victim).merge(None, EVENTS[0]["doc"])
    print("改写后再 merge 的产物（version 包装已消失）:")
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main(argv):
    report = load_report()
    if len(argv) == 1:
        list_findings(report)
        return
    if argv[1] == "hazard":
        replay_hazard()
        return
    if argv[1] == "split":
        index = int(argv[2]) - 1
        finding = report["metadata_split_brain_run"]["findings"][index]
        replay_prefix(SCHEMA_SPLIT, finding)
    else:
        index = int(argv[1]) - 1
        finding = report["baseline_run"]["findings"][index]
        replay_prefix(SCHEMA, finding)


if __name__ == "__main__":
    main(sys.argv)
