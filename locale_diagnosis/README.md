# 分区归并重复键断裂：locale 排查工具与证据

## 背景现象

每个分区（shard）本地用 natsort 排好序，汇总时做多路归并，重复客户名
却在归并结果里分成两段（split duplicate）。根因在于三个时刻的 locale
规则可能不是同一套：

1. **创建 key 函数时**：`natsort_keygen(alg=ns.LOCALE)` 被调用的时刻；
2. **真正取 key 时**：排序/归并中对每条记录调用 key 函数的时刻；
3. **分区实际保存的键**：写分区时落盘的到底是排序后的行，还是 key 本身，
   以及写分区时用的是哪套规则。

## 代码层面的两个捕获点

- `natsort/utils.py:700`：`string_component_transform_factory` 在 keygen
  阶段执行一次 `get_strxfrm()`，把返回值闭包进 key 函数，之后不再重新获取。
- `natsort/natsort.py:184`：`dumb_sort()` 只在 keygen 时求值一次，结果以
  `NS_DUMB` 位烤进 `alg`，取 key 时不会重新判断。
- `natsort/natsort.py:769`：模块导入时即执行 `os_sort_key = os_sort_keygen()`，
  ICU 后端会在导入瞬间创建 Collator（见探针审计日志里的 `#1 loc=C.UTF-8`）。

## 两条后端路径的本质区别

### 系统回退（本机真实路径，未安装 PyICU）

`natsort/compat/locale.py:79` 的 `get_strxfrm()` 直接返回内置
`locale.strxfrm`。它是一个全局函数，**每次调用都读取进程当前的全局
locale**，闭包捕获的只是函数对象，不是规则。

- `fallback-drift` 探针：同一个 key 函数，`setlocale` 前后对同一字符串
  产出不同 key；且旧函数在新 locale 下的输出与新 keygen 的输出完全一致，
  证明规则是在“比较/取 key 时刻”读取的，即比较时用了新规则。
- `dumb-baked` 探针：C locale 下 `dumb_sort()` 为 True，keygen 时
  `NS_DUMB`（含 groupletters/swapcase 结构）被固化；之后切到 de_DE，
  旧 key 函数仍保留旧结构，而新 keygen 产出不同结构——这部分是“创建时
  捕获旧规则”。
- 因此回退路径是**混合语义**：结构性开关在 keygen 时冻结，排序权重在
  取 key 时实时读取全局 locale。

### ICU（PyICU，用 `fake_icu.py` 适配器隔离演示）

`natsort/compat/locale.py:52` 的 `get_strxfrm()` 返回
`Collator.createInstance(...).getSortKey` 这个**绑定方法**。Collator 在
创建实例时快照 locale 与排序规则，之后全局 locale 再怎么变，已存在的
Collator 都不受影响；`dumb_sort()` 在 ICU 下恒为 False。

- `icu-snapshot` 探针：规则变更后，旧 key 函数输出保持不变（冻结在创建
  时刻），新 keygen 才跟随新规则；审计日志列出每次 Collator 创建及其
  规则快照，可直接区分“旧函数捕获旧规则”与“新函数使用新规则”。
- 说明：本机为离线沙箱、无法安装 PyICU，ICU 证据由受控适配器给出。
  适配器只实现 natsort 实际使用的接口，Collator 的快照语义与真实
  PyICU 一致；系统回退证据则全部来自真实解释器。

## 复现归并断裂

`merge-fallback` 与 `merge-icu` 分别在两条后端上复现同一现象：

- 两个分区各自在本地 key 下完全有序；
- 用汇总端的新 key 做 `heapq.merge` 后，重复键（`Борис` 或 `Andrei`）
  出现在不相邻的两个位置；
- 用归并 key 回测各分区，构建规则不同的那个分区出现逆序对（inversions=1）。
  这说明“本地有序”只对构建时的规则成立，对归并时的规则不成立。

## 运行方式

```bash
.venv/bin/python locale_diagnosis/run_probes.py   # 五个隔离子进程探针
.venv/bin/python locale_diagnosis/rework_scope.py # 修复范围判定
```

每个探针都是独立解释器进程，避免导入期后端选择和全局 locale 互相污染；
输出同时落盘到 `evidence/`。

## 两组现象相同、修复范围不同的分区

判定器 `rework_scope.py` 用分区构建清单（manifest）+ 固定探针语料的
key 指纹做决策，依据的都是**已经落盘的信息**，不需要重算业务数据：

### 分区 P1：只需重排一片

- 现象：归并后重复键断裂。
- 已存信息：两个 shard 的 locale、后端、引擎版本、natsort 版本、alg、
  `dumb_at_build`、key 指纹全部一致；仅 shard-B 的物理顺序标志为乱序
  （例如写入时并发/分页拼接错误）。
- 结论：键本身仍有效且同源，只需对 shard-B 用现有键重新排序
  （REORDER-ONLY），无需重新生成任何 key。

### 分区 P2：必须重新生成全部键

- 现象：与 P1 完全相同。
- 已存信息：shard-B 的引擎版本（ICU/CLDR 升级）或构建 locale 与
  shard-A 不同，且固定语料的 key 指纹不同。指纹不同意味着两套键不具备
  可比性，无法判断哪些行“碰巧”顺序正确。
- 结论：在统一规则下重新生成**所有**分区的键（REGENERATE-ALL-KEYS），
  再重排、重归并。回退后端下即使 manifest 一致，也要警惕全局 locale
  漂移：只要任一 shard 构建时的进程 locale 无法从清单证明一致，就按
  全量重建处理。

### 判定依据速查

- 可只重排一片：key 定义字段（locale/backend/engine/natsort/alg/
  dumb/指纹）全等，且故障仅限于某片的物理顺序。
- 必须全量重建：任一 key 定义字段或指纹不一致；或缺少构建清单、无法
  证明各 shard 同源（回退后端的全局 locale 不可从代码推断，只能靠
  落盘清单举证）。
