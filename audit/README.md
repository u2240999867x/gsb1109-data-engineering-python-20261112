# jsonmerge 合并结果 vs 下游 schema 差分校验

对 `jsonmerge`（本仓库版本，jsonschema 4.26.0，Draft4）做一次可穷举的差分校验：
把连续合并的产物交给 `Merger.get_schema()` 推导出的下游 schema 验证，并与原始输入
schema 的契约做差分，同时用独立验证器标注每个输入命中的 `oneOf` 分支。

## 运行

```bash
.venv/bin/python audit/diff_audit.py     # 穷举重放，生成 audit/report.json
.venv/bin/python audit/replay.py         # 列出全部差异的最短前缀
.venv/bin/python audit/replay.py 3       # 逐步重放基线第 3 条
.venv/bin/python audit/replay.py split 1 # 重放分裂变体第 1 条
.venv/bin/python audit/replay.py hazard  # 复现 get_schema() 就地改写
.venv/bin/pytest tests/test_audit_diff.py -v
```

## 被测 schema 子集（`audit/diff_audit.py`）

单份嵌套 schema，覆盖题目要求的全部构造：

- 本地引用：6 个 `#/definitions/*`，`oneOf` 分支与数组项内部都通过 `$ref` 引用。
- 互斥 `oneOf`：顶层 `config`、与 `mergeStrategy` 同层的 `firmware`、
  按 ID 合并数组项内嵌的 `payload`。
- 带 metadata 的 `version`：`firmware`（schema 级 metadata + metadataSchema）
  与 `sensors[].reading`（metadata 走 per-event `merge_options`）。
- 按 ID 合并的数组：`arrayMergeById`，`idRef=sensorId`，内部再嵌 `oneOf`/`version`。
- `append` 数组 `tags`，输入契约带 `uniqueItems`/`maxItems`。

## 独立校验器

| 校验器 | 职责 | 是否经过 jsonmerge |
|---|---|---|
| `V_in` | 原始 schema 校验每个 head（独立 `Draft4Validator`，带 `$ref` 上下文） | 否 |
| `V_branch` | 逐 `oneOf` 站点标注命中分支下标（`evolve` 子校验器） | 否 |
| `V_out` | `get_schema()` 结果校验连续合并产物（独立深拷贝 Merger 推导） | 是 |
| `V_contract` | 非 version 站点的原始契约（tags 唯一/上限、sensors 必填、config 单分支） | 否 |
| `V_meta` | version 历史条目是否保留 metadata 模板键 | 否 |

合并与 `get_schema()` 使用两个独立深拷贝 Merger，因为后者会就地改写传入的 schema
（见 HAZARD）。

## 穷举空间与判定

- 事件字母表 9 个（E1–E9），枚举全部深度 1..3 的前缀：9 + 81 + 729 = **819** 个。
- 每个 head 都是完整文档；E5–E8 故意做成 `V_in` 非法，其余 5 个完全合法。
- 差异按 `(类别, 家族, 实例路径集合)` 归并，每族只保留**最短事件前缀**代表，
  并记录命中分支、实例 JSON Pointer、根因源码位置。
- 基线运行中 7 个前缀完全一致（含 `E2 -> E9`，version 的 `ignoreDups` 去重后无新条目）。

## 差异矩阵（基线 819 个前缀）

### 误拒 FALSE_REJECT（输入全合法，merge 直接抛异常）

| 家族 | 次数 | 最短前缀 | 实例路径 |
|---|---|---|---|
| `oneof_branch_switch` | 94 | `E1 -> E3`（laptop→sensorNode） | `#/config` |
| `oneof_branch_switch` | 72 | `E1 -> E4`（temp→geo） | `#/sensors/0/payload` |

根因：`jsonmerge/descenders.py:99` 的 OneOf descender 要求**同一分支同时验证 base 与
head**，合法的互斥分支切换被一律拒绝（`HeadInstanceError`）。

### 误放 FALSE_ACCEPT（输入全合法，产物违背契约但 V_out 放行）

| 家族 | 次数 | 最短前缀 | 实例路径 |
|---|---|---|---|
| `append_constraints_stripped` | 65 | `E1 -> E1` | `#/tags` |
| `version_metadata_split_brain`（reading 历史缺 `rev`） | 13/5/2 | `E9`、`E1 -> E9`、`E2 -> E1 -> E9` | `#/sensors/0/reading/{0,1,2}` |

根因：
- `jsonmerge/strategies.py:201` `Append.get_schema` 直接 pop 掉 `maxItems`/`uniqueItems`，
  跨事件累积出重复/超限标签后下游无约束。
- `version` 的运行期 metadata 与 `get_schema()` 的 metadataSchema 是两套独立配置，
  per-event 覆盖把 metadata 清空后，没有 metadataSchema 的站点无人拦截。

### 输入非法仍被接受 INPUT_INVALID_ACCEPTED（merge 从不校验 head）

| 家族 | 次数 | 最短前缀 | 实例路径 | V_out |
|---|---|---|---|---|
| `array_id_silent_skip`（缺 sensorId 项被静默丢弃） | 162 | `E5` | `sensors/0` | 放行 |
| `no_input_validation`（firmware 缺 build） | 162 | `E7` | `firmware` | 拦截 |
| `no_input_validation`（sensor 缺 loc） | 162 | `E6` | `sensors/0` | 拦截 |
| `no_input_validation`（tags 单 head 内重复） | 162 | `E8` | `tags` | 放行 |

根因：`jsonmerge/__init__.py:305` `Merger.merge` 不调用任何输入校验；
`jsonmerge/strategies.py:212` 对取不到 id 的数组项 `continue` 静默跳过。
其中 E5/E8 在下游也无人拦，属于"漏必填/违约束仍通过"的误放；E6/E7 会被 V_out 兜住。

## 分裂变体：下游误拒 DOWNSTREAM_FALSE_REJECT

`SCHEMA_SPLIT` 只把 firmware 的 `metadataSchema.required` 改成 `["rev","at"]`，
运行期 metadata 模板仍只给 `rev`（两者无一致性校验）。同样 819 个前缀，0 个通过
下游校验，最短前缀仅需 1 个事件：

| 次数 | 最短前缀 | V_out 报错路径 |
|---|---|---|
| 23 | `E1` | `firmware/0` 缺 `at` |
| 16 | `E1 -> E2` | `firmware/0`、`firmware/1` |
| 6 | `E1 -> E2 -> E1` | 三个历史条目全缺 `at` |

即：输入合法、merge 成功，但每个版本历史条目都过不了 `get_schema()` 的结果。

## HAZARD：get_schema() 就地改写调用方 schema

`WalkSchema.work` 只做浅拷贝，Ref descender 又就地 `clear()/update()` 定义体。
对同一 schema 对象先 `get_schema()` 再 `merge()`，version 包装会消失
（`firmware` 从历史数组退化为普通对象），输出形态已偏离原 schema 语义。
`audit/replay.py hazard` 可复现；审计代码本身因此对两个 Merger 分别深拷贝。

## 产物

- `audit/report.json`：自包含报告（schema、事件域、分支标注、枚举计数、
  每条差异的最短前缀与可重放事件、分支、路径、根因）。
- `tests/test_audit_diff.py`：11 个回归测试，锁定上述家族与 819/7 的枚举不变量。
