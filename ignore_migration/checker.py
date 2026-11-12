"""
The independent checker.

The checker validates an equivalence proof or a difference report against
the inputs using only local computations:

-	it recomputes the content hashes and the rule DFAs from the inputs,
-	it replays the recorded transitions state by state (no search, no
	fixpoint iteration of its own),
-	it recomputes every recorded decision from the recorded components and
	checks the old/new outcomes agree on every leaf,
-	it checks the recorded states form a closed cover: every state lists a
	successor for every alphabet symbol the contract allows, and every
	recorded state is reachable from the initial state.

When an input changed (e.g. one rule was replaced) but an old proof is
reused, the checker reports exactly which segment (contract) and which
recorded state / transition / rule line the proof first fails at.
"""
from __future__ import annotations

from collections import (
	deque,
)
from typing import (
	Dict,
	List,
	Optional,
	Sequence,
	Tuple,
)

from .engine import (
	ADDED,
	REMOVED,
	PROOF_FORMAT,
	REPORT_FORMAT,
	Key,
	_decision_json,
	_outcome_json,
	decide,
	leaf_outcome,
	partition_chain,
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


def _issue(message: str, **location) -> dict:
	issue = {'message': message}
	issue.update({key: value for key, value in location.items() if value is not None})
	return issue


def _check_hashes(inputs: Inputs, artifact: dict, segment_ids: Sequence) -> List[dict]:
	issues = []
	expected_alphabet = stable_hash(list(inputs.alphabet))
	if artifact.get('alphabet_hash') != expected_alphabet:
		issues.append(_issue(
			"Alphabet hash mismatch: the artifact was produced for a different "
			"alphabet (or symbol order).",
			expected=expected_alphabet, found=artifact.get('alphabet_hash'),
		))
	expected_rules = {
		'old': stable_hash(list(inputs.old_lines)),
		'new': stable_hash(list(inputs.new_lines)),
	}
	found_rules = artifact.get('rules_hash') or {}
	for version in ('old', 'new'):
		if found_rules.get(version) != expected_rules[version]:
			for cid in segment_ids:
				issues.append(_issue((
					f"Rules hash mismatch ({version} version): the artifact is stale "
					f"for this segment; the rules it was proved against changed."
				), segment=cid, version=version,
				expected=expected_rules[version], found=found_rules.get(version)))
	return issues


def _initial_key(contract: Contract, old_rules, new_rules) -> Key:
	return (
		contract.start,
		tuple(rule.dfa.start for rule in old_rules),
		tuple(rule.dfa.start for rule in new_rules),
		None,
		None,
	)


def _verify_segment(
	alphabet: Sequence[str],
	contract: Contract,
	old_rules: Sequence[CompiledRule],
	new_rules: Sequence[CompiledRule],
	segment: dict,
) -> List[dict]:
	cid = contract.cid
	issues: List[dict] = []
	states = segment.get('states')
	if not isinstance(states, list) or not states:
		return [_issue("Segment has no states.", segment=cid)]

	entries: Dict[int, dict] = {}
	for entry in states:
		sid = entry.get('id')
		if not isinstance(sid, int) or sid in entries:
			issues.append(_issue(f"Duplicate or invalid state id {sid!r}.", segment=cid, state=sid))
			continue
		entries[sid] = entry
	if 0 not in entries:
		issues.append(_issue("Segment has no initial state (id 0).", segment=cid))
		return issues

	def local_successor(key: Key, depth: int, symbol: str) -> Optional[Key]:
		cs_name, old_vec, new_vec, old_blocker, new_blocker = key
		cstate = contract.states.get(cs_name)
		if cstate is None:
			return None
		next_cs = cstate.transitions.get(symbol)
		if next_cs is None:
			return None
		next_old_vec = tuple(
			rule.dfa.step(state, symbol) for rule, state in zip(old_rules, old_vec)
		)
		next_new_vec = tuple(
			rule.dfa.step(state, symbol) for rule, state in zip(new_rules, new_vec)
		)
		next_old_blocker = old_blocker
		next_new_blocker = new_blocker
		if cstate.is_dir and depth > 0:
			if old_blocker is None:
				old_dir = decide(old_rules, old_vec, is_dir=True)
				if old_dir.ignored:
					next_old_blocker = old_dir.line
			if new_blocker is None:
				new_dir = decide(new_rules, new_vec, is_dir=True)
				if new_dir.ignored:
					next_new_blocker = new_dir.line
		return (next_cs, next_old_vec, next_new_vec, next_old_blocker, next_new_blocker)

	# Replay the recorded transition graph from the initial state,
	# recomputing every component from the current inputs.
	recomputed: Dict[int, Key] = {0: _initial_key(contract, old_rules, new_rules)}
	depths: Dict[int, int] = {0: 0}
	queue = deque([0])
	while queue:
		sid = queue.popleft()
		entry = entries[sid]
		key = recomputed[sid]
		depth = depths[sid]
		recorded_trans = entry.get('transitions') or {}
		for symbol in alphabet:
			expected = local_successor(key, depth, symbol)
			if expected is None:
				if symbol in recorded_trans:
					issues.append(_issue((
						f"Recorded transition on {symbol!r} but the contract allows "
						f"none from state {entry.get('contract_state')!r}."
					), segment=cid, state=sid, symbol=symbol))
				continue
			if symbol not in recorded_trans:
				issues.append(_issue((
					f"Missing transition on {symbol!r}: the certificate does not "
					f"cover the whole alphabet at this state."
				), segment=cid, state=sid, symbol=symbol))
				continue
			tid = recorded_trans[symbol]
			if not isinstance(tid, int) or tid not in entries:
				issues.append(_issue((
					f"Transition on {symbol!r} points to invalid state id {tid!r}."
				), segment=cid, state=sid, symbol=symbol))
				continue
			if tid in recomputed:
				if recomputed[tid] != expected:
					issues.append(_issue((
						f"Inconsistent successor on {symbol!r}: state {tid} was "
						f"reached before with different components."
					), segment=cid, state=sid, symbol=symbol, target=tid))
			else:
				recomputed[tid] = expected
				depths[tid] = depth + 1
				queue.append(tid)

	# Compare recorded components against the recomputed ones, and validate
	# the recorded decisions locally.
	for sid, entry in entries.items():
		if sid not in recomputed:
			issues.append(_issue((
				"State is not reachable from the initial state via the recorded "
				"transitions."
			), segment=cid, state=sid, path=entry.get('path')))
			continue
		key = recomputed[sid]
		cs_name, old_vec, new_vec, old_blocker, new_blocker = key
		depth = depths[sid]
		path = entry.get('path')

		if entry.get('contract_state') != cs_name:
			issues.append(_issue((
				f"Contract state mismatch: recorded {entry.get('contract_state')!r}, "
				f"recomputed {cs_name!r}."
			), segment=cid, state=sid, path=path))

		for label, rules, recorded_vec, expected_vec in (
			('old', old_rules, entry.get('old_dfa'), old_vec),
			('new', new_rules, entry.get('new_dfa'), new_vec),
		):
			recorded_vec = tuple(recorded_vec or ())
			if len(recorded_vec) != len(rules):
				issues.append(_issue((
					f"{label} rule count changed: the certificate records "
					f"{len(recorded_vec)} rule DFAs, the input has {len(rules)}."
				), segment=cid, state=sid, path=path, version=label))
				continue
			for index, (recorded_state, expected_state) in enumerate(zip(recorded_vec, expected_vec)):
				if recorded_state != expected_state:
					issues.append(_issue((
						f"{label} rule line {rules[index].lineno} DFA diverges here: "
						f"recorded state {recorded_state}, recomputed {expected_state}. "
						f"The proof is invalid from this point on."
					), segment=cid, state=sid, path=path, version=label,
					rule_line=rules[index].lineno))

		for label, recorded_blocker, expected_blocker in (
			('old', entry.get('old_blocker'), old_blocker),
			('new', entry.get('new_blocker'), new_blocker),
		):
			if recorded_blocker != expected_blocker:
				issues.append(_issue((
					f"{label} blocker mismatch: recorded {recorded_blocker!r}, "
					f"recomputed {expected_blocker!r}."
				), segment=cid, state=sid, path=path, version=label))

		cstate = contract.states.get(entry.get('contract_state'))
		if cstate is None:
			issues.append(_issue((
				f"Recorded contract state {entry.get('contract_state')!r} does not "
				f"exist in the contract."
			), segment=cid, state=sid, path=path))
			continue

		# Recompute decisions from the *recorded* components and compare with
		# the recorded decisions.
		recorded_old_vec = tuple(entry.get('old_dfa') or ())
		recorded_new_vec = tuple(entry.get('new_dfa') or ())
		vecs_ok = (
			len(recorded_old_vec) == len(old_rules)
			and len(recorded_new_vec) == len(new_rules)
		)
		if not vecs_ok:
			continue

		is_dir_position = cstate.is_dir and depth > 0
		recorded_dir = entry.get('dir_decision') or {}
		for label, rules, vec in (
			('old', old_rules, recorded_old_vec),
			('new', new_rules, recorded_new_vec),
		):
			expected_dir = decide(rules, vec, is_dir=True) if is_dir_position else None
			if recorded_dir.get(label) != _decision_json(expected_dir):
				issues.append(_issue((
					f"Recorded {label} directory decision does not match the "
					f"decision recomputed from the recorded components."
				), segment=cid, state=sid, path=path, version=label,
				recorded=recorded_dir.get(label),
				recomputed=_decision_json(expected_dir)))

		recorded_leaves = entry.get('leaf_decisions') or {}
		for leaf_type in cstate.leaf_types:
			recorded_leaf = recorded_leaves.get(leaf_type)
			if recorded_leaf is None:
				issues.append(_issue((
					f"Missing leaf decision for leaf type {leaf_type!r}."
				), segment=cid, state=sid, path=path, leaf_type=leaf_type))
				continue
			old_out = leaf_outcome(old_rules, recorded_old_vec, entry.get('old_blocker'))
			new_out = leaf_outcome(new_rules, recorded_new_vec, entry.get('new_blocker'))
			if recorded_leaf.get('old') != _outcome_json(old_out):
				issues.append(_issue((
					"Recorded old leaf outcome does not match recomputation."
				), segment=cid, state=sid, path=path, leaf_type=leaf_type, version='old',
				recorded=recorded_leaf.get('old'), recomputed=_outcome_json(old_out)))
			if recorded_leaf.get('new') != _outcome_json(new_out):
				issues.append(_issue((
					"Recorded new leaf outcome does not match recomputation."
				), segment=cid, state=sid, path=path, leaf_type=leaf_type, version='new',
				recorded=recorded_leaf.get('new'), recomputed=_outcome_json(new_out)))
			if old_out.included != new_out.included:
				issues.append(_issue((
					f"Leaf outcomes differ between versions at this state "
					f"(old included={old_out.included}, new included={new_out.included}): "
					f"this is not a valid equivalence proof."
				), segment=cid, state=sid, path=path, leaf_type=leaf_type))
		for leaf_type in recorded_leaves:
			if leaf_type not in cstate.leaf_types:
				issues.append(_issue((
					f"Recorded leaf decision for unknown leaf type {leaf_type!r}."
				), segment=cid, state=sid, path=path, leaf_type=leaf_type))

	return issues


def verify_proof(inputs: Inputs, proof: dict) -> List[dict]:
	"""
	Verify an equivalence proof against the inputs. Returns a list of issues;
	an empty list means the proof is accepted.
	"""
	issues: List[dict] = []
	if proof.get('format') != PROOF_FORMAT or proof.get('verdict') != 'equivalent':
		return [_issue(f"Not an equivalence proof of format {PROOF_FORMAT!r}.")]

	issues.extend(_check_hashes(inputs, proof, list((proof.get('segments') or {}).keys())))

	old_rules = compile_rules(inputs.old_lines, inputs.alphabet)
	new_rules = compile_rules(inputs.new_lines, inputs.alphabet)

	segments = proof.get('segments') or {}
	contracts_by_id = {contract.cid: contract for contract in inputs.contracts}
	for cid, contract in contracts_by_id.items():
		segment = segments.get(cid)
		if segment is None:
			issues.append(_issue("Missing proof segment for this contract.", segment=cid))
			continue
		expected_contract_hash = stable_hash(contract_to_json(contract))
		if segment.get('contract_hash') != expected_contract_hash:
			issues.append(_issue((
				"Contract hash mismatch: the contract changed since the proof was "
				"produced."
			), segment=cid, expected=expected_contract_hash,
			found=segment.get('contract_hash')))
		issues.extend(_verify_segment(
			inputs.alphabet, contract, old_rules, new_rules, segment,
		))
	for cid in segments:
		if cid not in contracts_by_id:
			issues.append(_issue((
				"Proof segment refers to a contract that is not in the input."
			), segment=cid))
	return issues


def _verify_witness(
	inputs: Inputs,
	contract: Contract,
	old_rules: Sequence[CompiledRule],
	new_rules: Sequence[CompiledRule],
	combo: dict,
) -> List[dict]:
	cid = contract.cid
	issues: List[dict] = []
	path = combo.get('witness')
	direction = combo.get('direction')
	location = {'segment': cid, 'witness': path, 'leaf_type': combo.get('leaf_type')}

	if not isinstance(path, str):
		return [_issue("Witness path is not a string.", **location)]
	if direction not in (ADDED, REMOVED):
		return [_issue(f"Invalid direction {direction!r}.", **location)]
	for symbol in path:
		if symbol not in inputs.alphabet:
			issues.append(_issue((
				f"Witness symbol {symbol!r} is not in the alphabet."
			), **location))
	if issues:
		return issues

	# Replay the path through the contract and both rule-DFA vectors.
	cs = contract.start
	old_vec = tuple(rule.dfa.start for rule in old_rules)
	new_vec = tuple(rule.dfa.start for rule in new_rules)
	old_blocker: Optional[int] = None
	new_blocker: Optional[int] = None
	depth = 0
	for symbol in path:
		cstate = contract.states[cs]
		if cstate.is_dir and depth > 0:
			if old_blocker is None:
				old_dir = decide(old_rules, old_vec, is_dir=True)
				if old_dir.ignored:
					old_blocker = old_dir.line
			if new_blocker is None:
				new_dir = decide(new_rules, new_vec, is_dir=True)
				if new_dir.ignored:
					new_blocker = new_dir.line
		next_cs = cstate.transitions.get(symbol)
		if next_cs is None:
			issues.append(_issue((
				f"Witness path is not legal: no transition on {symbol!r} from "
				f"contract state {cs!r}."
			), **location))
			return issues
		cs = next_cs
		old_vec = tuple(
			rule.dfa.step(state, symbol) for rule, state in zip(old_rules, old_vec)
		)
		new_vec = tuple(
			rule.dfa.step(state, symbol) for rule, state in zip(new_rules, new_vec)
		)
		depth += 1

	final_state = contract.states[cs]
	leaf_type = combo.get('leaf_type')
	if leaf_type not in final_state.leaf_types:
		issues.append(_issue((
			f"Leaf type {leaf_type!r} is not allowed at the witness's final "
			f"contract state {cs!r}."
		), **location))

	old_out = leaf_outcome(old_rules, old_vec, old_blocker)
	new_out = leaf_outcome(new_rules, new_vec, new_blocker)
	if old_out.included == new_out.included:
		issues.append(_issue((
			f"Witness shows no difference: both versions "
			f"{'include' if old_out.included else 'exclude'} the leaf."
		), **location))
	else:
		expected_direction = ADDED if new_out.included else REMOVED
		if direction != expected_direction:
			issues.append(_issue((
				f"Direction mismatch: recorded {direction!r}, recomputed "
				f"{expected_direction!r}."
			), **location))
	if combo.get('old_rule_line') != old_out.line:
		issues.append(_issue((
			f"Old deciding rule line mismatch: recorded {combo.get('old_rule_line')!r}, "
			f"recomputed {old_out.line!r}."
		), **location))
	if combo.get('new_rule_line') != new_out.line:
		issues.append(_issue((
			f"New deciding rule line mismatch: recorded {combo.get('new_rule_line')!r}, "
			f"recomputed {new_out.line!r}."
		), **location))
	if 'partitions' in combo and combo['partitions'] != partition_chain(contract, path):
		issues.append(_issue("Recorded partition chain does not match the witness path.", **location))
	return issues


def verify_report(inputs: Inputs, report: dict) -> List[dict]:
	"""
	Verify a difference report against the inputs: every witness must be a
	legal path exhibiting exactly the recorded difference. (Minimality of the
	witnesses is guaranteed by the generator's BFS order and is not locally
	rechecked.)
	"""
	issues: List[dict] = []
	if report.get('format') != REPORT_FORMAT or report.get('verdict') != 'different':
		return [_issue(f"Not a difference report of format {REPORT_FORMAT!r}.")]

	issues.extend(_check_hashes(
		inputs, report,
		[section.get('contract_id') for section in report.get('contracts') or []],
	))

	old_rules = compile_rules(inputs.old_lines, inputs.alphabet)
	new_rules = compile_rules(inputs.new_lines, inputs.alphabet)

	contracts_by_id = {contract.cid: contract for contract in inputs.contracts}
	sections = report.get('contracts') or []
	seen = set()
	for section in sections:
		cid = section.get('contract_id')
		contract = contracts_by_id.get(cid)
		if contract is None:
			issues.append(_issue((
				"Report section refers to a contract that is not in the input."
			), segment=cid))
			continue
		seen.add(cid)
		for combo in section.get('combos') or []:
			issues.extend(_verify_witness(inputs, contract, old_rules, new_rules, combo))
	for cid in contracts_by_id:
		if cid not in seen:
			issues.append(_issue("Missing report section for this contract.", segment=cid))
	return issues


def verify_artifact(inputs: Inputs, artifact: dict) -> List[dict]:
	"""
	Verify either an equivalence proof or a difference report, dispatched on
	the artifact's verdict.
	"""
	verdict = artifact.get('verdict')
	if verdict == 'equivalent':
		return verify_proof(inputs, artifact)
	if verdict == 'different':
		return verify_report(inputs, artifact)
	return [_issue(f"Unknown verdict {verdict!r}.")]
