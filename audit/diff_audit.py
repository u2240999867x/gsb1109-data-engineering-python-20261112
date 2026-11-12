"""
jsonmerge 合并结果 vs 下游 get_schema() 差分校验（小而可穷举的 schema 子集）

Schema 覆盖（可嵌套）:
  * 本地引用 $ref（oneOf 分支与数组项内部都引用 #/definitions/...）
  * oneOf 互斥分支（顶层 config、version 同层 firmware、数组项内嵌 payload）
  * 带 metadata 的 version（firmware 与 sensors[].reading 两处）
  * 按 ID 合并的数组 arrayMergeById（idRef=sensorId），内部再嵌 oneOf/version
  * append 数组，输入契约带 uniqueItems/maxItems

独立校验器:
  V_in     : 原始 schema 校验每个 head（jsonschema Draft4，独立实例，带 $ref 上下文）
  V_branch : 独立 oneOf 分支标注器（不走 jsonmerge descender，直接 jsonschema evolve）
  V_out    : Merger.get_schema() 结果校验连续合并产物（独立深拷贝 Merger 推导）
  V_contract: 非 version 站点的原始契约（tags 唯一/上限、sensors 必填、config 单分支）
  V_meta   : version 历史条目是否保留 mergeOptions.metadata 模板声明的键

差异分类:
  FALSE_REJECT            所有输入合法，merge() 直接抛异常（合并期误拒）
  DOWNSTREAM_FALSE_REJECT 输入合法、merge 成功，V_out 拒绝产物（下游 schema 过严）
  FALSE_ACCEPT            产物违背契约/历史字段缺失，且 V_out 在该路径放行（误放）
  INPUT_INVALID_ACCEPTED  head 被 V_in 判非法，merge 不做输入校验照样处理

每个差异只留可重放的最短事件前缀、命中分支与实例 JSON Pointer。
"""
import copy
import itertools
import json
import os

import jsonschema

from jsonmerge import Merger
from jsonmerge.exceptions import JSONMergeError

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# 1. 小而可穷举的嵌套 schema 子集（基线版：metadata 与 metadataSchema 一致）
# --------------------------------------------------------------------------
def build_schema(firmware_metadata_required):
    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "definitions": {
            "laptop": {
                "type": "object",
                "properties": {"class": {"enum": ["laptop"]},
                               "owner": {"type": "string"}},
                "required": ["class", "owner"],
                "additionalProperties": False,
            },
            "sensorNode": {
                "type": "object",
                "properties": {"class": {"enum": ["sensor"]},
                               "site": {"type": "string"}},
                "required": ["class", "site"],
                "additionalProperties": False,
            },
            "rollout": {
                "type": "object",
                "properties": {"phase": {"enum": ["rollout"]},
                               "build": {"type": "string"}},
                "required": ["phase", "build"],
                "additionalProperties": False,
            },
            "rollback": {
                "type": "object",
                "properties": {"phase": {"enum": ["rollback"]},
                               "reason": {"type": "string"}},
                "required": ["phase", "reason"],
                "additionalProperties": False,
            },
            "tempPayload": {
                "type": "object",
                "properties": {"p": {"enum": ["temp"]},
                               "celsius": {"type": "number"}},
                "required": ["p", "celsius"],
                "additionalProperties": False,
            },
            "geoPayload": {
                "type": "object",
                "properties": {"p": {"enum": ["geo"]},
                               "grid": {"type": "string"}},
                "required": ["p", "grid"],
                "additionalProperties": False,
            },
        },
        "properties": {
            "deviceId": {"type": "string"},
            # 互斥 oneOf，分支本体走本地 $ref
            "config": {
                "oneOf": [
                    {"$ref": "#/definitions/laptop"},
                    {"$ref": "#/definitions/sensorNode"},
                ]
            },
            # version + metadata；oneOf 与 mergeStrategy 同层（合并期 oneOf 被跳过）
            "firmware": {
                "mergeStrategy": "version",
                "mergeOptions": {
                    "metadata": {"rev": 1},
                    "metadataSchema": {
                        "type": "object",
                        "properties": {"rev": {"type": "integer"},
                                       "at": {"type": "string"}},
                        "required": firmware_metadata_required,
                    },
                },
                "oneOf": [
                    {"$ref": "#/definitions/rollout"},
                    {"$ref": "#/definitions/rollback"},
                ],
            },
            # 按 ID 合并的数组，内部再嵌套 oneOf 与 version
            "sensors": {
                "type": "array",
                "mergeStrategy": "arrayMergeById",
                "mergeOptions": {"idRef": "sensorId"},
                "items": {
                    "type": "object",
                    "properties": {
                        "sensorId": {"type": "string"},
                        "loc": {"type": "string"},
                        "payload": {
                            "oneOf": [
                                {"$ref": "#/definitions/tempPayload"},
                                {"$ref": "#/definitions/geoPayload"},
                            ]
                        },
                        "reading": {
                            "mergeStrategy": "version",
                            "type": "object",
                            "properties": {"v": {"type": "number"}},
                            "required": ["v"],
                        },
                    },
                    "required": ["sensorId", "loc", "payload"],
                },
            },
            # append 数组，输入契约带 uniqueItems / maxItems
            "tags": {
                "type": "array",
                "mergeStrategy": "append",
                "uniqueItems": True,
                "maxItems": 2,
                "items": {"type": "string"},
            },
        },
        "required": ["deviceId", "config", "firmware", "sensors", "tags"],
    }


SCHEMA = build_schema([])                 # 基线：metadataSchema 无必填，与运行模板自洽
SCHEMA_SPLIT = build_schema(["rev", "at"])  # 分裂变体：metadataSchema 多要 at，运行模板只给 rev

ONEOF_SITES = {
    "#/config": ["laptop", "sensorNode"],
    "#/firmware": ["rollout", "rollback"],
    "#/sensors/*/payload": ["tempPayload", "geoPayload"],
}


# --------------------------------------------------------------------------
# 2. 独立校验器与分支标注器
# --------------------------------------------------------------------------
ROOT_VALIDATOR = jsonschema.Draft4Validator(SCHEMA)


def vin_errors(schema, doc):
    return sorted(
        ("/".join(map(str, e.path)) or "#", e.message)
        for e in jsonschema.Draft4Validator(schema).iter_errors(doc)
    )


def submatch(validator, subschema, fragment):
    # evolve 保留根 schema 的 $ref 解析上下文，独立于 jsonmerge 内部 descender
    return not list(validator.evolve(schema=subschema).iter_errors(fragment))


def classify_branches(schema, doc):
    """对每个 oneOf 站点独立计算实例满足的分支下标；0 或 >1 都如实标出。"""
    validator = jsonschema.Draft4Validator(schema)
    result = {}
    result["#/config"] = [
        i for i, b in enumerate(schema["properties"]["config"]["oneOf"])
        if submatch(validator, b, doc["config"])]
    result["#/firmware"] = [
        i for i, b in enumerate(schema["properties"]["firmware"]["oneOf"])
        if submatch(validator, b, doc["firmware"])]
    per_sensor = []
    payload_branches = (schema["properties"]["sensors"]["items"]
                        ["properties"]["payload"]["oneOf"])
    for i, item in enumerate(doc.get("sensors", [])):
        if "payload" not in item:
            per_sensor.append({"path": "#/sensors/%d/payload" % i, "branches": []})
            continue
        per_sensor.append({
            "path": "#/sensors/%d/payload" % i,
            "branches": [j for j, b in enumerate(payload_branches)
                         if submatch(validator, b, item["payload"])],
        })
    result["#/sensors/*/payload"] = per_sensor
    return result


def vout_errors(downstream_schema, doc):
    return sorted(
        ("/".join(map(str, e.path)) or "#", e.message)
        for e in jsonschema.Draft4Validator(downstream_schema).iter_errors(doc)
    )


def path_covered(path, reported):
    """reported 中是否已有同路径或祖先路径的报错。"""
    p = path.lstrip("#/")
    return any(p == r.lstrip("#/") or p.startswith(r.lstrip("#/") + "/")
               or r.lstrip("#/").startswith(p + "/") for r in reported)


def contract_violations(schema, output):
    """非 version 站点的原始输入契约在产物上的违背点。"""
    violations = []
    tags = output.get("tags", [])
    tag_cap = schema["properties"]["tags"]["maxItems"]
    if len(tags) > tag_cap:
        violations.append(("#/tags", "append_constraints_stripped",
                           "maxItems=%d 被突破，实际 %d 项" % (tag_cap, len(tags))))
    if len(tags) != len(set(tags)):
        dupes = sorted({t for t in tags if tags.count(t) > 1})
        violations.append(("#/tags", "append_constraints_stripped",
                           "uniqueItems 被破坏，重复值 %s" % dupes))
    for i, item in enumerate(output.get("sensors", [])):
        for req in schema["properties"]["sensors"]["items"]["required"]:
            if req not in item:
                violations.append(("#/sensors/%d" % i, "no_input_validation",
                                   "输出缺必填字段 %r" % req))
    validator = jsonschema.Draft4Validator(schema)
    cfg_hits = [i for i, b in enumerate(schema["properties"]["config"]["oneOf"])
                if submatch(validator, b, output["config"])]
    if len(cfg_hits) != 1:
        violations.append(("#/config", "no_input_validation",
                           "config 命中 %d 个 oneOf 分支，要求恰好 1 个" % len(cfg_hits)))
    return violations


# reading 站点的 metadata 模板由审计侧契约声明（schema 不写 mergeOptions.metadata，
# 否则会覆盖 per-event 的 merge_options；见 Walk.descend 的 opts 合并顺序）
READING_METADATA_TEMPLATE = {"rev"}
DEFAULT_MERGE_OPTIONS = {"version": {"metadata": {"rev": 1}}}


def metadata_drift(schema, output):
    """version 历史条目缺 metadata 模板声明的键（无 metadataSchema 必填时无人拦）。"""
    drift = []
    firmware_template = set(
        schema["properties"]["firmware"]["mergeOptions"]["metadata"])
    for j, entry in enumerate(output.get("firmware", [])):
        missing = sorted(firmware_template - set(entry))
        if missing:
            drift.append(("#/firmware/%d" % j,
                          "version_metadata_split_brain",
                          "firmware 历史条目 metadata 缺模板键 %s" % missing))
    for i, item in enumerate(output.get("sensors", [])):
        for j, entry in enumerate(item.get("reading", [])):
            missing = sorted(READING_METADATA_TEMPLATE - set(entry))
            if missing:
                drift.append(("#/sensors/%d/reading/%d" % (i, j),
                              "version_metadata_split_brain",
                              "reading 历史条目 metadata 缺模板键 %s" % missing))
    return drift


# --------------------------------------------------------------------------
# 3. 可穷举事件域（每个 head 都是完整文档；描述符在重放时展开）
# --------------------------------------------------------------------------
def head(config, firmware, sensors, tags):
    return {"deviceId": "dev-1", "config": config,
            "firmware": firmware, "sensors": sensors, "tags": tags}


LAPTOP = {"class": "laptop", "owner": "ann"}
SENSOR = {"class": "sensor", "site": "lab-1"}
ROLLOUT1 = {"phase": "rollout", "build": "b100"}
ROLLBACK1 = {"phase": "rollback", "reason": "defect"}
S1_TEMP = {"sensorId": "s1", "loc": "room-1",
           "payload": {"p": "temp", "celsius": 21.5},
           "reading": {"v": 1}}
S1_TEMP_V2 = dict(S1_TEMP, reading={"v": 2})
S1_GEO = {"sensorId": "s1", "loc": "room-1",
          "payload": {"p": "geo", "grid": "G7"},
          "reading": {"v": 2}}


EVENTS = [
    {"id": "E1_laptop_rollout_s1temp",
     "desc": "基线：laptop 分支 + rollout 分支 + s1 温度分支 + tag t1",
     "doc": head(LAPTOP, ROLLOUT1, [S1_TEMP], ["t1"])},
    {"id": "E2_laptop_rollback_s1v2",
     "desc": "config 同分支；firmware 切 rollback 分支；s1 同温度分支读数 v2；tag t2",
     "doc": head(LAPTOP, ROLLBACK1, [S1_TEMP_V2], ["t2"])},
    {"id": "E3_config_switch_sensor",
     "desc": "config 从 laptop 切 sensorNode（顶层 oneOf 互斥分支切换）",
     "doc": head(SENSOR, {"phase": "rollout", "build": "b101"}, [S1_TEMP_V2], ["t3"])},
    {"id": "E4_payload_switch_geo",
     "desc": "s1 的 payload 从 temp 切 geo（数组项内嵌 oneOf 互斥分支切换）；tag t1",
     "doc": head(LAPTOP, {"phase": "rollout", "build": "b102"}, [S1_GEO], ["t1"])},
    {"id": "E5_sensor_item_without_id",
     "desc": "含一个缺 sensorId 的数组项（V_in 非法；arrayMergeById 静默跳过该项）",
     "doc": head(LAPTOP, ROLLOUT1,
                 [{"loc": "room-2", "payload": {"p": "temp", "celsius": 9.9}},
                  S1_TEMP_V2], ["t2"])},
    {"id": "E6_s1_missing_loc",
     "desc": "s1 项缺必填 loc（V_in 非法；objectMerge 无输入校验）",
     "doc": head(LAPTOP, ROLLOUT1,
                 [{"sensorId": "s1", "payload": {"p": "temp", "celsius": 23.0},
                   "reading": {"v": 3}}], ["t2"])},
    {"id": "E7_firmware_missing_build",
     "desc": "firmware 值缺 rollout 必填 build（V_in 非法；version 照样入史）",
     "doc": head(LAPTOP, {"phase": "rollout"}, [S1_TEMP_V2], ["t2"])},
    {"id": "E8_duplicate_tags",
     "desc": "单个 head 内 tags 重复（V_in 违反 uniqueItems）",
     "doc": head(LAPTOP, ROLLOUT1, [S1_TEMP_V2], ["t1", "t1"])},
    {"id": "E9_metadata_blank_override",
     "desc": "合法事件；本次 merge 把 version metadata 覆盖为空（reading 历史字段漂移）；tag t1 制造跨事件重复",
     "doc": head(LAPTOP, ROLLBACK1, [S1_TEMP_V2], ["t1"]),
     "merge_options": {"version": {"metadata": {}}}},
]

MAX_DEPTH = 3

ROOT_CAUSE = {
    "oneof_branch_switch":
        "jsonmerge/descenders.py:99-106 OneOf 要求同一分支同时验证 base 与 head，"
        "分支切换即 HeadInstanceError",
    "version_metadata_split_brain":
        "jsonmerge/strategies.py:88-96 merge 只消费 mergeOptions.metadata，"
        "strategies.py:128-148 get_schema 只消费独立的 metadataSchema，两者无一致性校验",
    "append_constraints_stripped":
        "jsonmerge/strategies.py:201-205 Append.get_schema 直接 pop 掉 maxItems/uniqueItems",
    "array_id_silent_skip":
        "jsonmerge/strategies.py:212-219 iter_index_key_item 吞掉 RefResolutionError 后 continue",
    "no_input_validation":
        "jsonmerge/__init__.py:305-341 Merger.merge 从不调用 validator 校验 head",
    "get_schema_in_place_mutation":
        "jsonmerge/__init__.py:215-219 WalkSchema.work 仅浅拷贝，"
        "descenders.py:53-54 就地 clear/update 被引用的定义体",
}


# --------------------------------------------------------------------------
# 4. 重放、归类与最短前缀归并
# --------------------------------------------------------------------------
def replay(schema, prefix):
    """从头重放事件前缀。合并与 get_schema 用两个独立深拷贝 Merger（后者会就地改写 schema）。"""
    merge_merger = Merger(copy.deepcopy(schema))
    downstream = Merger(copy.deepcopy(schema)).get_schema()

    obs = {"input_valid": True, "vin": [], "branch_trace": [],
           "merge_error": None, "output": None,
           "vout": None, "contract": None, "metadata_drift": None}
    base = None
    for ev in prefix:
        vin = vin_errors(schema, ev["doc"])
        obs["vin"].append({"event": ev["id"], "errors": vin})
        if vin:
            obs["input_valid"] = False
        obs["branch_trace"].append(
            {"event": ev["id"], "branches": classify_branches(schema, ev["doc"])})
        try:
            options = ev.get("merge_options", DEFAULT_MERGE_OPTIONS)
            base = merge_merger.merge(base, ev["doc"], merge_options=options)
        except JSONMergeError as exc:
            obs["merge_error"] = {
                "type": type(exc).__name__, "message": exc.message,
                "path": getattr(exc.value, "ref", None),
                "strategy": exc.strategy_name}
            return obs

    obs["output"] = base
    obs["vout"] = vout_errors(downstream, base)
    obs["contract"] = contract_violations(schema, base)
    obs["metadata_drift"] = metadata_drift(schema, base)
    return obs


class Findings:
    """按 (类别, 家族, 路径集合) 归并，只保留最短前缀代表。"""

    def __init__(self):
        self.items = {}

    def record(self, cls, family, prefix, branches, paths, detail):
        key = (cls, family, tuple(sorted(paths)))
        if key not in self.items or len(prefix) < len(self.items[key]["prefix"]):
            self.items[key] = {"class": cls, "family": family,
                               "root_cause": ROOT_CAUSE[family],
                               "prefix": list(prefix), "branches": branches,
                               "paths": sorted(paths), "detail": detail,
                               "occurrences": 1}
        else:
            self.items[key]["occurrences"] += 1

    def all(self):
        return [self.items[k] for k in sorted(self.items, key=lambda k: (
            {"FALSE_REJECT": 0, "DOWNSTREAM_FALSE_REJECT": 1,
             "FALSE_ACCEPT": 2, "INPUT_INVALID_ACCEPTED": 3, "HAZARD": 4}[k[0]],
            k[1], k[2]))]


def enumerate_schema(schema, label):
    findings = Findings()
    replayed = consistent = 0

    for depth in range(1, MAX_DEPTH + 1):
        for combo in itertools.product(EVENTS, repeat=depth):
            replayed += 1
            prefix = list(combo)
            obs = replay(schema, prefix)
            vout_paths = [p for p, _ in obs["vout"] or []]

            if obs["merge_error"] is not None:
                if obs["input_valid"]:
                    err = obs["merge_error"]
                    findings.record("FALSE_REJECT", "oneof_branch_switch", prefix,
                                    obs["branch_trace"], [err["path"] or "#"],
                                    "merge 抛 %s: %s（策略 %s）" % (
                                        err["type"], err["message"], err["strategy"]))
                continue

            clean = (obs["input_valid"] and not obs["vout"]
                     and not obs["contract"] and not obs["metadata_drift"])
            if clean:
                consistent += 1
                continue

            if obs["input_valid"] and obs["vout"]:
                findings.record("DOWNSTREAM_FALSE_REJECT",
                                "version_metadata_split_brain", prefix,
                                obs["branch_trace"], vout_paths,
                                "V_out 拒绝: %s" % [m for _, m in obs["vout"]])

            # 误放只统计输入全部合法的前缀：契约/历史字段被合并过程破坏，V_out 却放行
            if obs["input_valid"]:
                for path, family, msg in obs["contract"]:
                    if not path_covered(path, vout_paths):
                        findings.record("FALSE_ACCEPT", family, prefix,
                                        obs["branch_trace"], [path],
                                        "%s；该路径 V_out 放行（V_out 全文=%s）"
                                        % (msg, obs["vout"]))

                for path, family, msg in obs["metadata_drift"]:
                    if not path_covered(path, vout_paths):
                        findings.record("FALSE_ACCEPT", family, prefix,
                                        obs["branch_trace"], [path],
                                        "%s；V_out 无 metadataSchema 约束，全文=%s"
                                        % (msg, obs["vout"]))

            if not obs["input_valid"]:
                # 按单条 V_in 报错路径拆分，各自归家族、各自留最短前缀
                for ev_errors in obs["vin"]:
                    for path, message in ev_errors["errors"]:
                        family = ("array_id_silent_skip" if "sensorId" in message
                                  else "no_input_validation")
                        caught = path_covered(path, vout_paths)
                        findings.record(
                            "INPUT_INVALID_ACCEPTED", family, prefix,
                            obs["branch_trace"], [path],
                            "V_in 报错 %r；merge 不校验输入；V_out 在该路径%s"
                            % (message, "拦截" if caught else "放行（误放）"))

    return {"label": label,
            "enumeration": {"alphabet_size": len(EVENTS),
                            "max_prefix_depth": MAX_DEPTH,
                            "prefixes_replayed": replayed,
                            "consistent_prefixes": consistent},
            "findings": findings.all()}


def hazard_mutation():
    """独立复现 get_schema() 就地改写传入 schema 的 hazard。"""
    original = copy.deepcopy(SCHEMA)
    victim = copy.deepcopy(SCHEMA)
    Merger(victim).get_schema()
    mutated = victim != original
    after = Merger(victim).merge(None, EVENTS[0]["doc"]) if mutated else None
    return {"mutated": mutated,
            "example_output_after_mutation": after,
            "note": "改写后同一 Merger 再 merge，version 包装消失，输出形态已不符合原 schema 语义"}


def main():
    domain_report = []
    for ev in EVENTS:
        vin = vin_errors(SCHEMA, ev["doc"])
        domain_report.append({
            "id": ev["id"], "desc": ev["desc"],
            "input_valid": not vin, "vin_errors": vin,
            "branches": classify_branches(SCHEMA, ev["doc"]),
            "merge_options": ev.get("merge_options"),
        })

    baseline = enumerate_schema(SCHEMA, "consistent_baseline")
    split_run = enumerate_schema(SCHEMA_SPLIT, "metadata_split_brain")
    # 分裂变体只保留下游误拒家族，避免与基线重复噪声
    split_run["findings"] = [f for f in split_run["findings"]
                             if f["class"] == "DOWNSTREAM_FALSE_REJECT"]

    report = {
        "oneof_sites": ONEOF_SITES,
        "event_domain": domain_report,
        "baseline_run": baseline,
        "metadata_split_brain_run": split_run,
        "get_schema_mutation_hazard": hazard_mutation(),
    }
    out_path = os.path.join(HERE, "report.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    for section in (baseline, split_run):
        en = section["enumeration"]
        print("## %s：重放 %d 个前缀（深度 1..%d），一致通过 %d，差异代表 %d 个\n"
              % (section["label"], en["prefixes_replayed"], en["max_prefix_depth"],
                 en["consistent_prefixes"], len(section["findings"])))
        for i, f in enumerate(section["findings"], 1):
            print("[%s] %s / %s（出现 %d 次）"
                  % (i, f["class"], f["family"], f["occurrences"]))
            print("  最短前缀: %s" % " -> ".join(e["id"] for e in f["prefix"]))
            print("  实例路径: %s" % f["paths"])
            print("  分支轨迹: %s" % json.dumps(f["branches"], ensure_ascii=False))
            print("  详情    : %s" % f["detail"])
            print("  根因    : %s\n" % f["root_cause"])

    h = report["get_schema_mutation_hazard"]
    print("## get_schema 就地改写 hazard: %s" % h["mutated"])
    print("  改写后 merge 示例: %s\n" % json.dumps(h["example_output_after_mutation"],
                                                   ensure_ascii=False))
    print("完整报告: %s" % out_path)


if __name__ == "__main__":
    main()
