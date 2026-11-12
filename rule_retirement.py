"""
数据清单 pathspec 规则退役影响分析器.

给定一份 gitignore 语义的采集排除规则(每行保留原始行号与类型)和一份数据清单
(目录节点 + 叶子对象, 每个对象附业务行数), 对"删除任意单行规则"分别计算:

- 进仓净增减行数(多进仓 / 少进仓);
- 受影响分区及对应行数;
- 能还原变化叶子的解释(每个状态翻转的叶子: 行数、前后状态、前后决定规则)。

匹配语义直接复用本仓库的 :class:`pathspec.gitignore.GitIgnoreSpec`(simple 后端),
因此"删除祖先排除规则导致一整支下级否定重新生效"、"等价规则互相遮住"等行为
都与 Git 实际一致; 单删影响一律按实际可达文件重算。

每轮分析(基线或某次单删)中, 每个 (编译规则, 清单节点) 对最多做一次实际正则匹配:
由 :class:`CountedPattern` 上的记忆化与调用计数保证, 运行结束会校验并打印计数。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import (
	dataclass,
	field)
from typing import (
	Optional)

from pathspec.gitignore import (
	GitIgnoreSpec)
from pathspec.patterns.gitignore.spec import (
	GitIgnoreSpecPattern)


# ---------------------------------------------------------------------------
# 规则与清单的加载
# ---------------------------------------------------------------------------

@dataclass
class Rule:
	"""一条规则行, 保留原行号与类型。"""

	line_no: int
	text: str
	include: Optional[bool]  # True=排除, False=否定(重新纳入), None=无操作

	@property
	def kind(self) -> str:
		if self.include is True:
			return '排除'
		elif self.include is False:
			return '否定'
		else:
			return '注释/空行(无操作)'


@dataclass
class Node:
	"""清单节点: 目录(分区路径)或叶子对象。"""

	path: str  # 目录以 '/' 结尾
	is_dir: bool
	rows: int = 0  # 仅叶子有效

	@property
	def partition(self) -> str:
		return self.path.split('/', 1)[0]


def load_rules(path: str) -> list[Rule]:
	"""读取规则文件, 逐行编译, 保留原行号与类型。"""
	with open(path, encoding='utf-8') as fh:
		lines = fh.read().splitlines()
	rules = []
	for line_no, text in enumerate(lines, start=1):
		compiled = GitIgnoreSpecPattern(text)
		rules.append(Rule(line_no=line_no, text=text, include=compiled.include))
	return rules


def load_manifest(path: str) -> list[Node]:
	"""
	读取清单文件。每行一个条目: ``路径`` 或 ``路径<TAB>行数``。
	以 '/' 结尾的是目录节点; 其余是叶子对象, 必须带业务行数。
	缺失的祖先目录会自动补齐, 保证每个被查询的路径都是清单节点。
	"""
	entries: dict[str, Node] = {}
	order: list[str] = []

	def ensure_dir(dir_path: str) -> None:
		if dir_path and dir_path not in entries:
			entries[dir_path] = Node(path=dir_path, is_dir=True)
			order.append(dir_path)

	with open(path, encoding='utf-8') as fh:
		for line_no, raw in enumerate(fh, start=1):
			raw = raw.rstrip('\n')
			if not raw.strip():
				continue
			parts = raw.split('\t')
			node_path = parts[0].strip()
			if node_path.endswith('/'):
				ensure_dir(node_path)
				continue
			if len(parts) < 2 or not parts[1].strip():
				raise ValueError(f"清单第 {line_no} 行叶子对象缺少行数: {raw!r}")
			rows = int(parts[1])
			if node_path in entries:
				raise ValueError(f"清单第 {line_no} 行路径重复: {node_path!r}")
			entries[node_path] = Node(path=node_path, is_dir=False, rows=rows)
			order.append(node_path)

	# 补齐缺失的祖先目录(保持树上前序)。
	for node_path in list(order):
		if not entries[node_path].is_dir:
			idx = node_path.find('/')
			while idx != -1:
				ensure_dir(node_path[:idx + 1])
				idx = node_path.find('/', idx + 1)

	return [entries[p] for p in sorted(entries)]


# ---------------------------------------------------------------------------
# 带调用计数的编译规则代理
# ---------------------------------------------------------------------------

class MatchCounter:
	"""记录一轮分析中每个 (编译规则, 清单节点) 对的实际匹配次数。"""

	def __init__(self) -> None:
		self.calls: dict[tuple[int, str], int] = {}

	def record(self, line_no: int, node_path: str) -> None:
		key = (line_no, node_path)
		self.calls[key] = self.calls.get(key, 0) + 1

	@property
	def total(self) -> int:
		return sum(self.calls.values())

	@property
	def pairs(self) -> int:
		return len(self.calls)

	@property
	def max_per_pair(self) -> int:
		return max(self.calls.values(), default=0)


class CountedPattern:
	"""
	编译规则的代理: 委托 :class:`GitIgnoreSpecPattern`, 但对同一轮内同一
	清单节点的匹配结果做记忆化, 保证每个 (规则, 节点) 对最多一次实际匹配。
	"""

	__slots__ = ('inner', 'line_no', 'counter', '_memo')

	def __init__(self, inner: GitIgnoreSpecPattern, line_no: int, counter: MatchCounter) -> None:
		self.inner = inner
		self.line_no = line_no
		self.counter = counter
		self._memo: dict[str, object] = {}

	@property
	def include(self) -> Optional[bool]:
		return self.inner.include

	@property
	def regex(self):
		return self.inner.regex

	@property
	def pattern(self):
		return self.inner.pattern

	def match_file(self, file: str):
		if file not in self._memo:
			self.counter.record(self.line_no, file)
			self._memo[file] = self.inner.match_file(file)
		return self._memo[file]


# ---------------------------------------------------------------------------
# 一轮求值
# ---------------------------------------------------------------------------

@dataclass
class Decision:
	ingested: bool  # True=进仓(未被排除)
	rule_line: Optional[int]  # 决定该节点状态的规则行号


@dataclass
class RoundResult:
	decisions: dict[str, Decision] = field(default_factory=dict)
	match_calls: int = 0
	match_pairs: int = 0
	max_per_pair: int = 0


def evaluate(rules: list[Rule], nodes: list[Node], drop_line: Optional[int] = None) -> RoundResult:
	"""
	对规则集合(可选地删除 *drop_line* 行)在整份清单上求值一轮。

	返回每个清单节点的进仓状态与决定规则, 以及本轮 (规则, 节点) 匹配计数。
	"""
	counter = MatchCounter()
	kept = [r for r in rules if r.line_no != drop_line]
	proxies = [CountedPattern(GitIgnoreSpecPattern(r.text), r.line_no, counter) for r in kept]
	spec = GitIgnoreSpec(proxies, backend='simple')

	node_paths = {n.path for n in nodes}
	result = RoundResult()
	for node in nodes:
		include, index = spec._backend.match_file(node.path)
		rule_line = kept[index].line_no if index is not None else None
		result.decisions[node.path] = Decision(
			ingested=include is not True,  # 被排除才不进仓
			rule_line=rule_line,
		)

	# 校验: 每个 (编译规则, 清单节点) 对最多一次实际匹配, 且被匹配的路径都是清单节点。
	for (_line, node_path), count in counter.calls.items():
		assert count == 1, ((node_path, _line), count)
		assert node_path in node_paths, node_path
	result.match_calls = counter.total
	result.match_pairs = counter.pairs
	result.max_per_pair = counter.max_per_pair
	return result


# ---------------------------------------------------------------------------
# 单删影响分析
# ---------------------------------------------------------------------------

@dataclass
class LeafChange:
	path: str
	rows: int
	before: bool  # 删除前进仓状态
	after: bool  # 删除后进仓状态
	before_rule: Optional[int]
	after_rule: Optional[int]


@dataclass
class DeletionReport:
	line_no: int
	kind: str
	text: str
	gained_rows: int  # 多进仓行数
	lost_rows: int  # 少进仓行数
	changes: list[LeafChange]
	partitions: dict[str, tuple[int, int]]  # 分区 -> (多进仓, 少进仓)
	match_calls: int
	match_pairs: int

	@property
	def net_rows(self) -> int:
		return self.gained_rows - self.lost_rows


def analyze(rules: list[Rule], nodes: list[Node]) -> tuple[RoundResult, list[DeletionReport]]:
	"""基线 + 每个单行删除的影响, 逐轮重算(按实际可达文件)。"""
	leaves = {n.path: n for n in nodes if not n.is_dir}
	baseline = evaluate(rules, nodes)

	reports = []
	for rule in rules:
		if rule.include is None:
			# 注释/空行/无效规则: 删除不可能改变任何匹配, 不必跑一轮。
			reports.append(DeletionReport(
				line_no=rule.line_no, kind=rule.kind, text=rule.text,
				gained_rows=0, lost_rows=0, changes=[], partitions={},
				match_calls=0, match_pairs=0,
			))
			continue

		round_result = evaluate(rules, nodes, drop_line=rule.line_no)
		changes: list[LeafChange] = []
		partitions: dict[str, list[int]] = {}
		gained = lost = 0
		for path, leaf in leaves.items():
			before = baseline.decisions[path]
			after = round_result.decisions[path]
			if before.ingested == after.ingested:
				continue
			changes.append(LeafChange(
				path=path, rows=leaf.rows,
				before=before.ingested, after=after.ingested,
				before_rule=before.rule_line, after_rule=after.rule_line,
			))
			part = leaf.partition
			slot = partitions.setdefault(part, [0, 0])
			if after.ingested:
				gained += leaf.rows
				slot[0] += leaf.rows
			else:
				lost += leaf.rows
				slot[1] += leaf.rows

		reports.append(DeletionReport(
			line_no=rule.line_no, kind=rule.kind, text=rule.text,
			gained_rows=gained, lost_rows=lost, changes=changes,
			partitions={p: (g, l) for p, (g, l) in partitions.items()},
			match_calls=round_result.match_calls,
			match_pairs=round_result.match_pairs,
		))
	return baseline, reports


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def _fmt_rule(rule_line: Optional[int]) -> str:
	return f"L{rule_line}" if rule_line is not None else "无规则命中"


def render_text(rules: list[Rule], nodes: list[Node], baseline: RoundResult, reports: list[DeletionReport]) -> str:
	leaves = [n for n in nodes if not n.is_dir]
	base_rows = sum(n.rows for n in leaves if baseline.decisions[n.path].ingested)
	total_rows = sum(n.rows for n in leaves)

	out = []
	out.append("=" * 72)
	out.append("基线(规则全集)")
	out.append("=" * 72)
	out.append(f"清单: {len(nodes)} 个节点({len(leaves)} 个叶子对象), 总行数 {total_rows}")
	out.append(f"基线进仓行数: {base_rows} (排除 {total_rows - base_rows})")
	out.append(
		f"基线轮匹配计数: 实际匹配 {baseline.match_calls} 次, "
		f"覆盖 {baseline.match_pairs} 个(规则,节点)对, 每对最多 {baseline.max_per_pair} 次"
	)
	out.append("")

	for rep in reports:
		out.append("-" * 72)
		out.append(f"删除 L{rep.line_no} [{rep.kind}] {rep.text!r}")
		if rep.kind.startswith('注释'):
			out.append("  无操作行, 删除不影响任何匹配。")
			continue
		net = rep.net_rows
		sign = '+' if net >= 0 else ''
		out.append(
			f"  净增减: {sign}{net} 行 (多进仓 {rep.gained_rows}, 少进仓 {rep.lost_rows})"
		)
		if rep.partitions:
			out.append("  分区影响:")
			for part in sorted(rep.partitions):
				g, l = rep.partitions[part]
				out.append(f"    {part}/: 多进仓 {g} 行, 少进仓 {l} 行")
		else:
			out.append("  分区影响: 无(按实际可达文件计算, 无叶子状态变化)")
		if rep.changes:
			out.append("  变化叶子(可据此还原):")
			for ch in rep.changes:
				direction = '排除→进仓' if ch.after else '进仓→排除'
				out.append(
					f"    {ch.path}  rows={ch.rows}  {direction}  "
					f"决定规则: {_fmt_rule(ch.before_rule)} → {_fmt_rule(ch.after_rule)}"
				)
		out.append(
			f"  本轮匹配计数: 实际匹配 {rep.match_calls} 次, "
			f"覆盖 {rep.match_pairs} 个(规则,节点)对, 每对≤1 次"
		)
	out.append("")
	return '\n'.join(out)


def render_json(rules: list[Rule], nodes: list[Node], baseline: RoundResult, reports: list[DeletionReport]) -> str:
	leaves = [n for n in nodes if not n.is_dir]
	payload = {
		'baseline': {
			'ingested_rows': sum(n.rows for n in leaves if baseline.decisions[n.path].ingested),
			'total_rows': sum(n.rows for n in leaves),
			'match_calls': baseline.match_calls,
			'match_pairs': baseline.match_pairs,
			'max_per_pair': baseline.max_per_pair,
		},
		'deletions': [
			{
				'line_no': rep.line_no,
				'kind': rep.kind,
				'text': rep.text,
				'net_rows': rep.net_rows,
				'gained_rows': rep.gained_rows,
				'lost_rows': rep.lost_rows,
				'partitions': {
					p: {'gained': g, 'lost': l} for p, (g, l) in rep.partitions.items()
				},
				'changed_leaves': [
					{
						'path': ch.path,
						'rows': ch.rows,
						'before_ingested': ch.before,
						'after_ingested': ch.after,
						'before_rule_line': ch.before_rule,
						'after_rule_line': ch.after_rule,
					}
					for ch in rep.changes
				],
				'match_calls': rep.match_calls,
				'match_pairs': rep.match_pairs,
			}
			for rep in reports
		],
	}
	return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('rules', help="pathspec/gitignore 规则文件")
	parser.add_argument('manifest', help="数据清单(路径<TAB>行数, 目录以 / 结尾)")
	parser.add_argument('--json', action='store_true', help="输出 JSON 报告")
	args = parser.parse_args(argv)

	rules = load_rules(args.rules)
	nodes = load_manifest(args.manifest)
	baseline, reports = analyze(rules, nodes)
	if args.json:
		print(render_json(rules, nodes, baseline, reports))
	else:
		print(render_text(rules, nodes, baseline, reports))
	return 0


if __name__ == '__main__':
	sys.exit(main())
