# IBAN 历史布尔值回填实验

模拟两版只存 `is_valid` 布尔值的离线字典，评估历史记录的迁移动作。
校验分层与 `stdnum/iban.py` 的 `validate()` 完全对应：

| 层 | 内容 | 是否随版本变化 |
|----|------|----------------|
| L1 | ISO 7064 Mod 97-10 整体校验位 | 否（标准不变） |
| L2 | 国家 BBAN 结构（本地注册表） | 随注册表版本 |
| L3 | 国家规则 + 银行目录，仅 `check_country=True` 时执行 | 随规则版本与数据版本 |

## 运行

```
.venv/bin/python backfill_demo/run_demo.py
.venv/bin/python -m pytest backfill_demo/test_backfill.py -o addopts=""
```

## 文件

- `engine.py` — 三层校验引擎、可替换快照（带行级内容指纹）、分层裁决。
- `national_rules.py` — BE 银行目录规则；XK 可版本化国家校验（v1 mod97 → v2 mod89）。
- `data/` — 两版注册表（有效行一致）与两版 BE 银行目录。
- `run_demo.py` — 三种变化、两段同值历史、迁移分类、最少证据清单。
- `test_backfill.py` — 钉住全部结论，并与真实 `stdnum` 的通用层交叉校验。

## 迁移分类

- **INHERIT（直接继承）**：失败落在不可变的 L1（或两版一致的 L2），重算确认即可。
- **RECHECK-NATIONAL（必须重查国家规则）**：L1/L2 可继承，L3 必须按当前规则与数据重跑。
- **FORMAT-ONLY CEILING（重查也只能说格式有效）**：该国无国家模块，离线只能证明格式，不能给国家级结论。
- 账户是否存在不参与任何离线裁决，单列字段，默认 `UNKNOWN`。
