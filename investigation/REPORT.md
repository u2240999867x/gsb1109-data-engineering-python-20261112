# Mimesis 样表重跑对不上：差异来源排查报告

复现与取证脚本：`investigation/rng_lineage_probe.py`（用 `.venv/bin/python` 运行，
逐次尝试的完整轨迹落盘到 `investigation/out/traces.json`）。

## 结论

同种子重跑"前几行相同、之后对不上"在导出行层面至少对应两种完全不同的机制，
只看导出 CSV/JSON 无法区分：

- **轨迹 A（共享 Field）**：两个表的 schema 闭包捕获同一个 `Field` 实例。顺序生成改成
  交错生成后，两表在同一条随机流上互相抢抽取，先行的表只有第一行能对上。
- **轨迹 B（回调跳行/额外抽取）**：每表有独立 `Field`，但 schema 回调中途引入了质量门
  （抽到门槛值后返回 `None` 跳行）和会额外抽取的 key 函数（如 `maybe`）。行数不变
  （`Schema.create` 按导出行数循环），但从跳行点开始整体平移。

用两类额外观测即可分开（见下文"最小区分观测"）。

## 机制定位（代码依据）

- `Field(seed)` 只种子化它内部那个 `Generic`；`Generic` 第一次访问某 provider 时才
  实例化，**每个 provider 各持有一个 `Random` 实例**（`mimesis/providers/generic.py:50`，
  `mimesis/providers/base.py:99`）。所以"随机源"的粒度是
  `(Field 实例, provider)`，而不是 Field 本身：共享 Field 的两表若恰好只用不相交的
  provider，其实互不干扰。
- `Schema(seed=...)` **不会**给任何 Field 重新播种，种子只被存下并放进
  `SchemaContext`（`mimesis/schema.py:467`、`mimesis/schema.py:548`）。真正的随机状态
  在闭包捕获的 `Field` 里——"用同一个 Schema seed 重跑"不是重播机制（探针已验证：
  不 reseed Field 时两次 `create()` 结果不同）。
- `Schema.create()` 按**导出行数**循环（`mimesis/schema.py:574`）：回调返回 `None`
  的尝试不产出行，但该次尝试已经发生的抽取全部消耗掉了，且导出里不留任何痕迹。
- key 函数拿到的 random 是 `generic.random`（`mimesis/schema.py:200`），例如
  `maybe` 会额外 `choices` 一次（`mimesis/keys.py:88`）。这次抽取落在 generic 自己的
  随机源上，与 provider 的流都不是同一条。

## 排查方法：给每次尝试打三类标识

探针在 `Random.random` / `Random.getrandbits`（所有 provider 方法最终都汇入这两个
原语）上挂了计数器，并按当前上下文归账。每次**尝试**（不是每个导出行）记录：

| 字段 | 含义 |
| --- | --- |
| `table` / `pk` | 表名 / 业务主键（按导出行从上游序列分配，如 `ORD-1001`） |
| `attempt` | 尝试号：schema 回调的第几次调用，**含被跳过的尝试** |
| `sources` | 该次尝试触碰的随机源标识，如 `F-shared/person`、`F-orders/generic` |
| `draws` / `outcome` | 原语抽取次数 / emitted 或 skipped |

## 两条轨迹与最小区分观测

两条轨迹的导出视图同构：同种子、行数相同、前缀若干行一致、之后值整体平移——
只看导出行必然误判成同一种问题。

**轨迹 A**：`Field(seed=42)` 同时喂 orders 和 customers。顺序跑（A1）vs 交错跑（A2）：

- 导出：orders 仅第 0 行一致，customers 全部对不上。
- 轨迹：两表的 `sources` 都是 `F-shared/person` + `F-shared/numeric`（**跨表同源**）；
  无 skipped；两表抽取总数在两种调度间**守恒**（29+36 == 30+35），只是归属变了。

**轨迹 B**：orders/customers 各自 `Field(seed=42/43)`，orders 回调从第 2 次尝试起加
质量门（约 20% 跳行）并给 amount 挂 `maybe`：

- 导出：orders 前 2 行一致、第 2 行起对不上；customers 6/6 完全一致。
- 轨迹：两表 `sources` **互不相交**；orders 出现 skipped 尝试 `[2, 5]`，抽取总量
  29 → 44 膨胀，还多了 `F-orders/generic` 这个源（key 函数抽取）；未受波及的
  customers 表逐比特稳定（38 == 38）。

**最小区分观测（两条即够）**：

1. **每行随机源标识**：跨表出现同一源 → 共享 Field（A）；逐表互不相交 → 各表独立（B）。
2. **尝试号序列 + skipped 标记**：出现空洞/skipped、且未受影响表的抽取总量逐比特
   稳定 → 回调跳行或额外抽取（B）；尝试号稠密、跨表总量守恒但归属重排 → 调度交错（A）。

## 按表独立回填：可复现的创建与重播边界

**创建边界**（新批次要满足）：

- 每表一个专用 `Field`/`Generic`，绝不跨表共享；记录
  `(table, seed, locale, mimesis 版本, Python 版本)`。
- 批次 = 若干互不耦合的 `(table, seed, attempt 序列)` 单元；每次尝试（含 skipped）
  留痕，至少记录尝试号、跳过标记、抽取数。
- 更稳的做法：每表批首快照 `field.get_random_instance().getstate()`，重播边界就变成
  "状态快照 + 尝试区间"，不再依赖回调代码版本。

**重播边界**（回填 table T 的第 a..b 行）：

- 坐标是**尝试号**而不是导出行号。用 T 的种子新建 Field，按同一代码版本重放
  尝试 0..b-1（含被跳过的），只导出 a..b-1。探针验证：干净批次按此回填第 2..4 行
  逐字节一致。
- 对轨迹 B 这种带跳行的批次，**按导出行号 naive 重放必然失败**（探针验证为 False），
  按尝试号 + 同版本回调重放才能还原（探针验证为 True）。
- `Schema(seed=...)` 不在重播边界内——它不回填 Field 的随机状态。

## 缺少哪些历史信息就无法还原旧批次

- **种子**：用了 `MissingSeed`（系统熵）的批次原则上不可还原（探针验证两次运行即不同）。
- **Field ↔ 表的共享拓扑**：不知道哪几个表共用同一个 Field，就不知道哪些表会互相污染。
- **原始运行的交错顺序**：共享流上各表的调用交错序列决定每次抽取的归属，事后无法从
  导出行推出。
- **跳行/额外抽取模式**：被跳过尝试的位置与次数、key 函数/自定义 handler 里的额外抽取，
  等价于要求保留回调代码的确切版本。
- **locale 与 mimesis 版本**：数据集随版本变化，同种子不同版本产出不同。
- **Python 版本**：`random()` 的梅森序列稳定，但 `choices`/`sample` 等高层方法的实现
  跨版本有漂移，跨 Python 版本重播不保证一致。
