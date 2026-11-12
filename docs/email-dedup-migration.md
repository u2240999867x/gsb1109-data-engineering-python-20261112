# 邮箱去重迁移判据（双客户库合并）

一边的客户库保存规范化（normalized）地址，另一边保留供应商自己的点号、
加号别名形态。本文档给出合并时的迁移判据、与输入顺序无关的分群算法，以及
必须留档的原值与政策版本。参考实现见 `email_validator/dedup.py`，可运行
推演见 `examples/migration_dedup_demo.py`。

## 1. 两层等价：表示等价 vs 业务账户等价

### 1.1 表示等价（representation equivalence）

只依赖线路格式规范，与任何供应商政策无关，命中即可无条件合并。对每条原值
用本库实算并保存三元事实：

| 事实 | 来源 | 实算示例 |
| --- | --- | --- |
| NFC | 本地部分 Unicode NFC，域 UTS-46/IDNA i18n | `é.user`（NFD 与 NFC 收敛为同一串） |
| IDNA | 域的 A-label 与 Unicode 形态 | `xn--r8jz45g.xn--zckzah` ≡ `例え.テスト` |
| SMTPUTF8 | 本地部分是否存在 ASCII 线路形态 | 非 ASCII 本地部分 `ascii=None, smtputf8=True` |

表示等价键 `rep_key = (nfc, ascii_form or "", smtputf8)`。SMTPUTF8 位入键，
避免仅凭域相同就把一个非 ASCII 本地部分记录并到 ASCII 记录上。表示等价
**只证明"线路上是同一个邮箱"**，不证明归属同一个业务账户。

### 1.2 业务账户等价（business-account equivalence）

供应商别名（Gmail 式去点、去 `+tag` 等）只在声明的**域名范围**和**半开生效
区间** `[valid_from, valid_to)` 内成立，并且必须按每条记录的观测时刻逐条
判定：

* 域不匹配 → 规则对该记录无效（例如点号在 `example.net` 是真实字符，不能
  套用 gmail 规则）；
* 观测时刻不在区间内 → 规则过期/未生效，该声明只能作为弱证据留档；
* 同一地址在不相交区间可以换主人：归属判断必须带时间维度，邮箱身份相同与
  账户主人相同是两件事。

## 2. 与输入顺序无关的分群办法

分两层图处理，所有分组都以排序后的键/集合构造，结论对记录顺序不敏感
（`tests/test_dedup.py::test_clustering_is_order_independent` 用 10 次随机
洗牌验证）。

**第一层：硬并查集（只吃硬同边）**

1. `rep_key` 相同 → 硬同边（表示等价）；
2. 同一规则、同一规范化域、同一供应商规范 token，且规则在观测时刻生效 →
   硬同边；
3. 共享稳定 `customer_ref` → 硬同边。

硬并查集产出的每个连通块是一个"原子群"。原子群内出现多个不同
`customer_ref`，即为直接矛盾（direct conflict）。

**第二层：候选图（弱边 + 冲突判定）**

* 过期/越域的别名声明不产生硬边，只在原子群之间建立**弱边**，并写入
  拒绝日志（含规则 id、政策版本、观测时刻）；
* 用第二个并查集把原子群按弱边连成候选簇，再按簇给三值结论。

**三值判定**

| 结论 | 条件 |
| --- | --- |
| `same`（必然相同） | 单个原子群，无弱边，且主人 id 至多一个；所有连接均为硬同边且无矛盾 |
| `conflict`（冲突） | 原子群内出现多个主人 id；或候选簇借弱边才连通且含多个主人 id |
| `insufficient`（证据不足） | 仅由过期/越域弱边连通，且簇内至多一个主人 id，转人工复核 |

关键性质：弱边永远不会把簇提升为 `same`。一条过期别名即使经过第三条记录
中转，也只能形成弱边候选簇；当两端主人 id 已被硬证据证实时，簇直接判
`conflict`，已证实不同的账户不会被串并（测试
`test_stale_alias_cannot_transit_through_third_record`）。

## 3. 旧数据推演（同时含错并、漏并、间接冲突）

完整数据与规则在 `tests/test_dedup.py` 与演示脚本中，判定结果：

| 记录 | 旧系统问题 | 迁移判定 |
| --- | --- | --- |
| R1/R2 `Jane.Doe+news@` vs `janedoe@example.com` | — | `same`：2024 年起生效的 example.com 规则，token 均为 `janedoe`，且共享 C100 |
| R3/R4 NFD vs NFC 的 `é.user@` | 漏并（按原值比较） | `same`：NFC 收敛，rep_key 相同（SMTPUTF8=True 一并留档） |
| R10/R11 A-label vs Unicode 域 | 漏并 | `same`：IDNA 双向等价，rep_key 相同 |
| R5（2018，C300）/R6/R7（2026，C400）gmail | 错并：把 2020 年才生效的规则回溯套用 | `conflict`：R6/R7 硬同；R5 的过期声明只是弱边，C300≠C400，禁止经 R7 串并 |
| R8/R9 `team.lead` vs `teamlead@example.net` | 跨域错并 | 两个独立 `same`：无规则覆盖 example.net，点号是真实字符 |
| R12/R13 同地址不同主人、不同区间 | 静默合并风险 | `conflict`：邮箱身份相同（硬表示等价）但主人 id 不同，换主人不自动并 |
| R14/R15 过期 gmail 别名、无主人 id | — | `insufficient`：仅弱边连通，转人工 |
| R16 不可解析 | — | 隔离（quarantine），不丢弃、不参与分群 |

## 4. 目标库必须留档的原值与政策版本

1. 每行的**原始字符串**（如 R1 保留 `Jane.Doe+news@Example.COM`），规范化
   形态只作附加列，永不覆盖原值；
2. 实算的 `nfc` / `ascii_form` / `smtputf8` 三元事实，以及本次重算的政策
   版本串 `email-validator/<ver>::idna/<ver>::unicodedata/UCD-<ver>`——Unicode
   与 IDNA 表会升级，旧结果必须可按版本复现；
3. 记录采集时自带的 `policy_version`（如 `legacy-policy-2018`、
   `capture-policy-2025`），迁移不得改写；
4. 每条别名声明的 `rule_id`、`policy_version`、域名范围、半开区间
   `[valid_from, valid_to)`、转换器身份；
5. 每条弱边/拒绝声明（规则 id + 政策版本 + 观测时刻 + 原值），以及每个
   `same`/`conflict`/`insufficient` 簇背后的完整证据链（表示等价、生效别名、
   主人 id、过期弱边分别可追溯）。
