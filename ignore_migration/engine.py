"""
The equivalence engine.

For each contract the engine explores the product of

-	the contract automaton (legal namespace positions),
-	one DFA per rule of the old version,
-	one DFA per rule of the new version,
-	the old and new *blockers* (the rule line that excluded the shallowest
	excluded ancestor directory, or ``None``).

The product is finite, so the breadth-first exploration terminates. Because
the inclusion decision of any legal path is a function of the product state
it reaches, comparing decisions on every reachable product state is
*exactly* the comparison of the two rule versions on the whole (unbounded)
namespace.

The exploration visits states in (path length, caller's alphabet order)
order, so the first witness recorded for each difference combination is a
shortest one, with ties broken by the caller's symbol order.
"""
from __future__ import annotations

from collections import (
	deque,
)
from dataclasses import (
	dataclass,
)
from typing import (
	Dict,
	List,
	Optional,
	Sequence,
	Tuple,
)

from .model import (
	Contract,
	Inputs,
	contract_to_json,
	stable_hash,
)
from .rules import (
	CompiledRule,
	compile_rules,
)

PROOF_FORMAT = 'ignore-migration-proof/1'
REPORT_FORMAT = 'ignore-migration-report/1'

#: Difference directions.
ADDED = 'added'
"""New version includes the leaf, old version excludes it (入仓新增)."""

REMOVED = 'removed'
"""Old version includes the leaf, new version excludes it (入仓撤回)."""


@dataclass(frozen=True)
class Decision:
	"""
	The decision of one rule version for one path.
	"""

	ignored: bool
	"""Whether the decision is "exclude"."""

	line: Optional[int]
	"""The deciding rule line (1-based), or ``None`` for the default include."""


@dataclass(frozen=True)
class LeafOutcome:
	"""
	The effective outcome for a leaf, taking ancestor blocking into account.
	"""

	included: bool
	line: Optional[int]
	"""The deciding rule line: the blocker line if an ancestor directory is
	excluded, otherwise the leaf's own deciding line (``None`` = default
	include)."""


def decide(rules: Sequence[CompiledRule], vec: Tuple[int, ...], is_dir: bool) -> Decision:
	"""
	The decision of a rule version for a path, given the vector of rule DFA
	states the path reaches. The last matching rule decides.
	"""
	last: Optional[int] = None
	for index, rule in enumerate(rules):
		if rule.dir_only and not is_dir:
			continue
		if rule.dfa.accepts(vec[index]):
			last = index
	if last is None:
		return Decision(ignored=False, line=None)
	rule = rules[last]
	return Decision(ignored=rule.ignore, line=rule.lineno)


def leaf_outcome(
	rules: Sequence[CompiledRule],
	vec: Tuple[int, ...],
	blocker: Optional[int],
) -> LeafOutcome:
	"""
	The effective outcome for a leaf at a product state.
	"""
	if blocker is not None:
		return LeafOutcome(included=False, line=blocker)
	decision = decide(rules, vec, is_dir=False)
	return LeafOutcome(included=not decision.ignored, line=decision.line)


#: A product state key: (contract state, old DFA vector, new DFA vector,
#: old blocker, new blocker).
Key = Tuple[str, Tuple[int, ...], Tuple[int, ...], Optional[int], Optional[int]]

#: A difference combination: (direction, old deciding line, new deciding
#: line, leaf type).
Combo = Tuple[str, Optional[int], Optional[int], str]


@dataclass
class ContractResult:
	"""
	The exploration result for one contract.
	"""

	contract_id: str
	combos: Dict[Combo, str]
	"""Maps each reached difference combination to its witness path."""

	states: List[dict]
	"""The reachable product states (the certificate segment when the
	contract shows no difference)."""


def _decision_json(decision: Optional[Decision]) -> Optional[dict]:
	if decision is None:
		return None
	return {'ignored': decision.ignored, 'line': decision.line}


def _outcome_json(outcome: LeafOutcome) -> dict:
	return {'included': outcome.included, 'line': outcome.line}


def explore_contract(
	alphabet: Sequence[str],
	contract: Contract,
	old_rules: Sequence[CompiledRule],
	new_rules: Sequence[CompiledRule],
) -> ContractResult:
	"""
	Explore the reachable product graph of one contract in (length, caller
	order) BFS order.
	"""
	start_key: Key = (
		contract.start,
		tuple(rule.dfa.start for rule in old_rules),
		tuple(rule.dfa.start for rule in new_rules),
		None,
		None,
	)
	ids: Dict[Key, int] = {start_key: 0}
	paths: List[str] = ['']
	queue = deque([start_key])
	states: List[dict] = []
	combos: Dict[Combo, str] = {}

	while queue:
		key = queue.popleft()
		sid = ids[key]
		assert sid == len(states)
		cs_name, old_vec, new_vec, old_blocker, new_blocker = key
		path = paths[sid]
		cstate = contract.states[cs_name]

		# Directory decision at this position (the root is never excluded).
		is_dir_position = cstate.is_dir and path != ''
		old_dir = decide(old_rules, old_vec, is_dir=True) if is_dir_position else None
		new_dir = decide(new_rules, new_vec, is_dir=True) if is_dir_position else None

		# Leaf outcomes at this position.
		leaf_json = {}
		for leaf_type in cstate.leaf_types:
			old_out = leaf_outcome(old_rules, old_vec, old_blocker)
			new_out = leaf_outcome(new_rules, new_vec, new_blocker)
			leaf_json[leaf_type] = {'old': _outcome_json(old_out), 'new': _outcome_json(new_out)}
			if old_out.included != new_out.included:
				direction = ADDED if new_out.included else REMOVED
				combo = (direction, old_out.line, new_out.line, leaf_type)
				if combo not in combos:
					combos[combo] = path

		# Successors in the caller's alphabet order.
		transitions = {}
		for symbol in alphabet:
			next_cs = cstate.transitions.get(symbol)
			if next_cs is None:
				continue
			next_old_vec = tuple(
				rule.dfa.step(state, symbol)
				for rule, state in zip(old_rules, old_vec)
			)
			next_new_vec = tuple(
				rule.dfa.step(state, symbol)
				for rule, state in zip(new_rules, new_vec)
			)
			if old_blocker is not None:
				next_old_blocker = old_blocker
			elif old_dir is not None and old_dir.ignored:
				next_old_blocker = old_dir.line
			else:
				next_old_blocker = None
			if new_blocker is not None:
				next_new_blocker = new_blocker
			elif new_dir is not None and new_dir.ignored:
				next_new_blocker = new_dir.line
			else:
				next_new_blocker = None

			next_key = (next_cs, next_old_vec, next_new_vec, next_old_blocker, next_new_blocker)
			if next_key not in ids:
				ids[next_key] = len(ids)
				paths.append(path + symbol)
				queue.append(next_key)
			transitions[symbol] = ids[next_key]

		states.append({
			'id': sid,
			'path': path,
			'contract_state': cs_name,
			'old_dfa': list(old_vec),
			'new_dfa': list(new_vec),
			'old_blocker': old_blocker,
			'new_blocker': new_blocker,
			'dir_decision': {'old': _decision_json(old_dir), 'new': _decision_json(new_dir)},
			'leaf_decisions': leaf_json,
			'transitions': transitions,
		})

	return ContractResult(contract_id=contract.cid, combos=combos, states=states)


def partition_chain(contract: Contract, path: str) -> List[str]:
	"""
	The partition names of the directory positions along *path*
	(informational, for witnesses).
	"""
	names = []
	current = contract.start
	for symbol in path:
		current = contract.states[current].transitions[symbol]
		state = contract.states[current]
		if state.is_dir and state.partition is not None:
			names.append(state.partition)
	return names


def compare(inputs: Inputs) -> dict:
	"""
	Compare the two rule versions over every contract.

	Returns a difference report (``verdict == 'different'``) with a shortest
	legal witness for every reached combination of (direction, old deciding
	rule line, new deciding rule line, leaf type), or an equivalence proof
	(``verdict == 'equivalent'``) verifiable by an independent checker using
	only the inputs and local transitions.
	"""
	old_rules = compile_rules(inputs.old_lines, inputs.alphabet)
	new_rules = compile_rules(inputs.new_lines, inputs.alphabet)

	results = [
		explore_contract(inputs.alphabet, contract, old_rules, new_rules)
		for contract in inputs.contracts
	]

	contracts_by_id = {contract.cid: contract for contract in inputs.contracts}

	if any(result.combos for result in results):
		sections = []
		for result in results:
			contract = contracts_by_id[result.contract_id]
			combo_list = []
			for combo, path in sorted(
				result.combos.items(),
				key=lambda item: (
					item[0][0],
					-1 if item[0][1] is None else item[0][1],
					-1 if item[0][2] is None else item[0][2],
					item[0][3],
				),
			):
				direction, old_line, new_line, leaf_type = combo
				combo_list.append({
					'direction': direction,
					'old_rule_line': old_line,
					'new_rule_line': new_line,
					'leaf_type': leaf_type,
					'witness': path,
					'partitions': partition_chain(contract, path),
				})
			sections.append({'contract_id': result.contract_id, 'combos': combo_list})
		return {
			'format': REPORT_FORMAT,
			'verdict': 'different',
			'alphabet_hash': stable_hash(list(inputs.alphabet)),
			'rules_hash': {
				'old': stable_hash(list(inputs.old_lines)),
				'new': stable_hash(list(inputs.new_lines)),
			},
			'contracts': sections,
		}

	segments = {
		result.contract_id: {
			'contract_hash': stable_hash(contract_to_json(contracts_by_id[result.contract_id])),
			'states': result.states,
		}
		for result in results
	}
	return {
		'format': PROOF_FORMAT,
		'verdict': 'equivalent',
		'alphabet_hash': stable_hash(list(inputs.alphabet)),
		'rules_hash': {
			'old': stable_hash(list(inputs.old_lines)),
			'new': stable_hash(list(inputs.new_lines)),
		},
		'segments': segments,
	}
