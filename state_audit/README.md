# FlashText 词典编辑状态对照（state_audit）

维护商品词典时别名会被连续增删，只抽样看最后一条命中很难判断数据是从哪次
操作开始坏掉的。这个包提供一套小规模、独立的状态对照：

1. `glossary_data.py`：独立词条清单与 17 步编辑序列，逐条解释别名的
   规范名（canonical）、词界（boundary）、重叠（overlap）、大小写（case）；
2. `reference_matcher.py`：**不导入 flashtext** 的参考匹配器，自己按词界
   切 token、做最长前缀匹配和切片替换，作为对照规范；
3. `auditor.py`：逐步回放编辑序列，对每个标题核对每个实体的原文半开区间
   `[start, end)`、区间原文和替换串，差异分成
   `missing_entity` / `extra_entity` / `clean_mismatch` / `span_mismatch`，
   并提供贪心最小化，缩到仍会选错同一个实体的最短编辑子序列；
4. `demo.py`：端到端演示。

## 运行

```bash
python -m state_audit.demo
pytest test/test_state_audit.py -v
```

## 编辑序列覆盖的两种关键变化

- 长别名删掉后短别名重现：`red apple x1 pro` -> `red apple x1` -> `red`
  逐级删除，同一起点 0 的命中区间逐级缩短，前缀 trie 路径始终保留；
- 覆盖后多个别名汇到同一实体：`iphone`、`iphone 8` 先映射旧规范名，
  再次 `add_keyword` 覆盖映射后与 `apple phone` 一起汇到 `APPLE-PHONE`，
  词条数不增加（覆盖只改 `_keyword_` 终止标记上的替换串）。

## 两类可控故障证明对照能区分故障形态

- 删坏前缀（`buggy_remove_break_prefix`）：删除时砍掉别名首字符的整棵
  trie 子树，同前缀短别名一起丢失，报 `missing_entity`/`extra_entity`，
  最小化结果是一条 add + 一条 remove 的组合；
- 偏移失真（`OffsetDistortingProcessor`）：继承真实 FlashText，只把 span
  末尾整体 +1，规范名和替换串仍正确，只报 `span_mismatch`，最小化结果
  缩成单条 add——说明它与编辑历史无关，两类故障的差异签名互不相交。
