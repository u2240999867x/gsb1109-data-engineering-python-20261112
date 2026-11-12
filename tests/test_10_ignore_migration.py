"""
Tests for the :mod:`ignore_migration` package: exact equivalence checking of
two ignore-rule versions over finite-state partition contracts.
"""
from __future__ import annotations

import copy
import random
import re
import unittest

from ignore_migration import (
	compare,
	load_inputs,
	verify_artifact,
	verify_proof,
	verify_report,
)
from ignore_migration.engine import (
	decide,
	explore_contract,
	leaf_outcome,
)
from ignore_migration.rules import (
	compile_rules,
	parse_rule,
)

ALPHABET = ['/', 'a', 'b', 'c', 'd', 'e', 'g', 'k', 'l', 'o', 'p', 't', 'x', 'y', '.']
"""
Alphabet used by the deterministic tests (in the caller's tie-breaking order).
"""


def dfa_accepts(rule_text: str, path: str, alphabet=ALPHABET) -> bool:
	"""
	Whether the single rule *rule_text* matches *path* (DFA acceptance).
	"""
	compiled = compile_rules([rule_text], alphabet)
	assert len(compiled) == 1
	dfa = compiled[0].dfa
	state = dfa.start
	for symbol in path:
		state = dfa.step(state, symbol)
	return dfa.accepts(state)


def make_inputs(contracts, old_rules, new_rules, alphabet=ALPHABET):
	return load_inputs({
		'alphabet': alphabet,
		'contracts': contracts,
		'old_rules': old_rules,
		'new_rules': new_rules,
	})


def simple_contract(cid='c1'):
	"""
	A small contract::

		q0 --a--> qa --/--> qas --a--> qaa (leaf f)
		                     |      --b--> qab (leaf f)
		q0 --b--> qb (leaf f)
		qa is a dir and a leaf (f).
	"""
	return {
		'id': cid,
		'start': 'q0',
		'states': {
			'q0': {'dir': True, 'leaf_types': [], 'partition': None,
				'transitions': {'a': 'qa', 'b': 'qb'}},
			'qa': {'dir': True, 'leaf_types': ['f'], 'partition': 'p=a',
				'transitions': {'/': 'qas'}},
			'qas': {'dir': True, 'leaf_types': [], 'partition': None,
				'transitions': {'a': 'qaa', 'b': 'qab'}},
			'qaa': {'dir': True, 'leaf_types': ['f'], 'partition': None, 'transitions': {}},
			'qab': {'dir': True, 'leaf_types': ['f'], 'partition': None, 'transitions': {}},
			'qb': {'dir': True, 'leaf_types': ['f'], 'partition': 'p=b', 'transitions': {}},
		},
	}


def loop_contract(cid='c1'):
	"""
	A contract with unbounded depth: ``q0 -(a/)*`` chains, leaves everywhere.
	"""
	return {
		'id': cid,
		'start': 'q0',
		'states': {
			'q0': {'dir': True, 'leaf_types': [], 'partition': None,
				'transitions': {'a': 'qa', 'b': 'qb'}},
			'qa': {'dir': True, 'leaf_types': ['f'], 'partition': None,
				'transitions': {'/': 'q0'}},
			'qb': {'dir': True, 'leaf_types': ['f'], 'partition': None,
				'transitions': {'/': 'q0'}},
		},
	}


class RuleMatchingTest(unittest.TestCase):
	"""
	Single-rule matching semantics of the supported subset.
	"""

	def test_literal_unanchored_matches_basename(self):
		self.assertTrue(dfa_accepts('keep.log', 'keep.log'))
		self.assertTrue(dfa_accepts('keep.log', 'x/keep.log'))
		self.assertFalse(dfa_accepts('keep.log', 'keep.log/x'))
		self.assertFalse(dfa_accepts('keep.log', 'xkeep.log'))

	def test_anchored(self):
		self.assertTrue(dfa_accepts('/a', 'a'))
		self.assertFalse(dfa_accepts('/a', 'x/a'))
		self.assertTrue(dfa_accepts('a/b', 'a/b'))
		self.assertFalse(dfa_accepts('a/b', 'x/a/b'))
		self.assertFalse(dfa_accepts('a/b', 'a/b/c'))

	def test_star(self):
		self.assertTrue(dfa_accepts('*.log', 'x.log'))
		self.assertTrue(dfa_accepts('*.log', 'd/y.log'))
		self.assertFalse(dfa_accepts('*.log', 'x.log/y'))
		self.assertTrue(dfa_accepts('a*.log', 'a.log'))
		self.assertTrue(dfa_accepts('a*c', 'abc'))
		self.assertFalse(dfa_accepts('a*c', 'a/c'))

	def test_double_star_placements(self):
		# '**' alone matches everything.
		self.assertTrue(dfa_accepts('**', 'a'))
		self.assertTrue(dfa_accepts('**', 'a/b/c'))
		# Leading '**/' matches at any depth.
		self.assertTrue(dfa_accepts('**/a', 'a'))
		self.assertTrue(dfa_accepts('**/a', 'x/a'))
		self.assertTrue(dfa_accepts('**/a', 'x/y/a'))
		# Trailing '/**' matches everything inside, not the directory itself.
		self.assertTrue(dfa_accepts('a/**', 'a/b'))
		self.assertTrue(dfa_accepts('a/**', 'a/b/c'))
		self.assertFalse(dfa_accepts('a/**', 'a'))
		self.assertFalse(dfa_accepts('a/**', 'xa/b'))
		# Inner '/**/' matches zero or more inner directories.
		self.assertTrue(dfa_accepts('a/**/b', 'a/b'))
		self.assertTrue(dfa_accepts('a/**/b', 'a/x/b'))
		self.assertTrue(dfa_accepts('a/**/b', 'a/x/y/b'))
		self.assertFalse(dfa_accepts('a/**/b', 'a/b/c'))
		self.assertFalse(dfa_accepts('a/**/b', 'x/a/b'))

	def test_double_star_inside_segment_is_star(self):
		self.assertTrue(dfa_accepts('a**b', 'ab'))
		self.assertTrue(dfa_accepts('a**b', 'axyb'))
		self.assertFalse(dfa_accepts('a**b', 'a/b'))

	def test_char_classes(self):
		self.assertTrue(dfa_accepts('[ab]x', 'ax'))
		self.assertTrue(dfa_accepts('[ab]x', 'bx'))
		self.assertFalse(dfa_accepts('[ab]x', 'cx'))
		self.assertTrue(dfa_accepts('[!ab]x', 'cx'))
		self.assertFalse(dfa_accepts('[!ab]x', 'ax'))
		self.assertTrue(dfa_accepts('[a-c]', 'b'))
		self.assertFalse(dfa_accepts('[a-c]', 'd'))
		# A class never matches the separator.
		self.assertFalse(dfa_accepts('a[ab]b', 'a/b'))
		self.assertFalse(dfa_accepts('a[!c]b', 'a/b'))

	def test_escaped_literal(self):
		self.assertTrue(dfa_accepts('a\\.log', 'a.log'))
		self.assertFalse(dfa_accepts('a\\.log', 'axlog'))

	def test_parse_flags(self):
		rule = parse_rule('!build/', 3)
		self.assertIsNotNone(rule)
		self.assertFalse(rule.ignore)
		self.assertTrue(rule.dir_only)
		self.assertFalse(rule.anchored)
		rule = parse_rule('/a/b', 1)
		self.assertTrue(rule.anchored)
		rule = parse_rule('a/b', 1)
		self.assertTrue(rule.anchored)
		rule = parse_rule('a', 1)
		self.assertFalse(rule.anchored)
		self.assertIsNone(parse_rule('', 1))
		self.assertIsNone(parse_rule('!', 1))
		self.assertIsNone(parse_rule('/', 1))


class DecisionTest(unittest.TestCase):
	"""
	Ordered decisions, negation, directory-only rules, ancestor blocking.
	"""

	def compile(self, lines):
		return compile_rules(lines, ALPHABET)

	def vec(self, rules, path):
		vec = []
		for rule in rules:
			state = rule.dfa.start
			for symbol in path:
				state = rule.dfa.step(state, symbol)
			vec.append(state)
		return tuple(vec)

	def test_last_match_wins(self):
		rules = self.compile(['*.log', '!keep.log'])
		decision = decide(rules, self.vec(rules, 'x.log'), is_dir=False)
		self.assertTrue(decision.ignored)
		self.assertEqual(decision.line, 1)
		decision = decide(rules, self.vec(rules, 'keep.log'), is_dir=False)
		self.assertFalse(decision.ignored)
		self.assertEqual(decision.line, 2)
		decision = decide(rules, self.vec(rules, 'x.logg'), is_dir=False)
		self.assertFalse(decision.ignored)
		self.assertIsNone(decision.line)

	def test_dir_only_skipped_for_leaves(self):
		rules = self.compile(['a/'])
		# The directory 'a' is excluded...
		decision = decide(rules, self.vec(rules, 'a'), is_dir=True)
		self.assertTrue(decision.ignored)
		# ...but a leaf named 'a' is not matched by the dir-only rule.
		decision = decide(rules, self.vec(rules, 'a'), is_dir=False)
		self.assertFalse(decision.ignored)

	def test_ancestor_blocking_cannot_be_revived(self):
		rules = self.compile(['b/', '!b/keep.log'])
		# The leaf is under an excluded directory: the negation cannot revive
		# it, and the deciding line is the blocker's line.
		outcome = leaf_outcome(rules, self.vec(rules, 'b/keep.log'), blocker=1)
		self.assertFalse(outcome.included)
		self.assertEqual(outcome.line, 1)


class EngineTest(unittest.TestCase):
	"""
	End-to-end comparisons over contracts.
	"""

	def combos_of(self, result, cid='c1'):
		self.assertEqual(result['verdict'], 'different')
		for section in result['contracts']:
			if section['contract_id'] == cid:
				return {
					(combo['direction'], combo['old_rule_line'], combo['new_rule_line'], combo['leaf_type']):
					combo['witness']
					for combo in section['combos']
				}
		raise AssertionError(f"no section for {cid!r}")

	def test_added_witness_is_shortest(self):
		inputs = make_inputs([simple_contract()], ['a'], [])
		result = compare(inputs)
		combos = self.combos_of(result)
		# 'a' excludes dir 'a' and leaf 'a'; leaves 'a', 'a/a', 'a/b' become
		# included in the new version. The shortest witness is 'a'.
		self.assertEqual(combos, {('added', 1, None, 'f'): 'a'})

	def test_removed_direction(self):
		inputs = make_inputs([simple_contract()], [], ['b'])
		result = compare(inputs)
		combos = self.combos_of(result)
		self.assertEqual(combos, {('removed', None, 1, 'f'): 'b'})

	def test_dir_only_rule_blocks_descendants_but_not_leaf(self):
		inputs = make_inputs([simple_contract()], ['a/'], [])
		result = compare(inputs)
		combos = self.combos_of(result)
		# The leaf 'a' itself is not matched by 'a/'; descendants are blocked
		# by the excluded directory. The deciding line is the blocker's line.
		self.assertEqual(combos, {('added', 1, None, 'f'): 'a/a'})

	def test_caller_order_breaks_ties(self):
		# '[ab]' excludes both 'a' and 'b'; the witness is the first leaf in
		# the caller's alphabet order.
		inputs = make_inputs([simple_contract()], ['[ab]'], [])
		combos = self.combos_of(compare(inputs))
		self.assertEqual(combos[('added', 1, None, 'f')], 'a')

		reordered = [sym for sym in ALPHABET if sym != 'a' and sym != 'b']
		reordered = ['/', 'b', 'a'] + reordered[1:]
		inputs = make_inputs([simple_contract()], ['[ab]'], [], alphabet=reordered)
		combos = self.combos_of(compare(inputs))
		self.assertEqual(combos[('added', 1, None, 'f')], 'b')

	def test_equivalent_rule_versions(self):
		inputs = make_inputs([loop_contract()], ['*.log'], ['**.log'])
		result = compare(inputs)
		self.assertEqual(result['verdict'], 'equivalent')
		self.assertEqual(verify_proof(inputs, result), [])

	def test_equivalent_with_negation(self):
		inputs = make_inputs(
			[loop_contract()],
			['*.log', '!keep.log'],
			['!keep.log', '*.log', '!keep.log'],
		)
		result = compare(inputs)
		self.assertEqual(result['verdict'], 'equivalent')
		self.assertEqual(verify_proof(inputs, result), [])

	def test_leaf_vs_dir_rule_difference(self):
		# 'a' excludes the leaf 'a' as well; 'a/' does not.
		inputs = make_inputs([simple_contract()], ['a'], ['a/'])
		result = compare(inputs)
		combos = self.combos_of(result)
		self.assertEqual(combos, {('added', 1, None, 'f'): 'a'})

	def test_deep_difference_witness(self):
		# Difference only reachable at depth >= 2 inside a looping contract.
		inputs = make_inputs([loop_contract()], [], ['/a/a'])
		result = compare(inputs)
		combos = self.combos_of(result)
		self.assertEqual(combos, {('removed', None, 1, 'f'): 'a/a'})

	def test_report_verifies(self):
		inputs = make_inputs([simple_contract()], ['a', '!a/b', 'b/'], ['b'])
		result = compare(inputs)
		self.assertEqual(result['verdict'], 'different')
		self.assertEqual(verify_report(inputs, result), [])

	def test_multiple_leaf_types(self):
		contract = simple_contract()
		contract['states']['qb']['leaf_types'] = ['f', 'g']
		inputs = make_inputs([contract], [], ['b'])
		result = compare(inputs)
		combos = self.combos_of(result)
		self.assertEqual(combos, {
			('removed', None, 1, 'f'): 'b',
			('removed', None, 1, 'g'): 'b',
		})


class ProofAndCheckerTest(unittest.TestCase):
	"""
	Equivalence certificates and the independent checker.
	"""

	def equivalent_artifact(self):
		contracts = [loop_contract('c1'), simple_contract('c2')]
		inputs = make_inputs(contracts, ['*.log', '!keep.log'], ['**.log', '!keep.log'])
		result = compare(inputs)
		self.assertEqual(result['verdict'], 'equivalent')
		return inputs, result

	def test_proof_verifies_across_contracts(self):
		inputs, proof = self.equivalent_artifact()
		self.assertEqual(set(proof['segments']), {'c1', 'c2'})
		self.assertEqual(verify_proof(inputs, proof), [])

	def test_changed_rule_detected_per_segment(self):
		inputs, proof = self.equivalent_artifact()
		# Replace one rule and reuse the old proof.
		changed = make_inputs(
			[loop_contract('c1'), simple_contract('c2')],
			['*.log', '!keep.log'],
			['**.log', '!keep.log', 'axe'],
		)
		issues = verify_proof(changed, proof)
		self.assertTrue(issues)
		# Both segments are reported stale.
		stale = [i for i in issues if 'stale' in i['message']]
		self.assertEqual({i['segment'] for i in stale}, {'c1', 'c2'})

	def test_changed_rule_localizes_failure(self):
		contracts = [loop_contract('c1')]
		inputs = make_inputs(contracts, ['a'], ['a'])
		proof = compare(inputs)
		self.assertEqual(verify_proof(inputs, proof), [])
		# One rule replaced: the checker must localize the failure to the
		# segment and the states where the proof breaks.
		changed = make_inputs(contracts, ['a'], ['b'])
		issues = verify_proof(changed, proof)
		self.assertTrue(issues)
		self.assertEqual({i.get('segment') for i in issues}, {'c1'})
		self.assertTrue(any('state' in i for i in issues))
		# A structurally different rule also diverges on the recorded DFA
		# states, which the checker attributes to the replaced rule line.
		changed = make_inputs(contracts, ['a'], ['[ab]'])
		issues = verify_proof(changed, proof)
		localized = [i for i in issues if i.get('rule_line') == 1]
		self.assertTrue(localized)
		self.assertEqual({i['segment'] for i in localized}, {'c1'})

	def test_changed_contract_detected(self):
		inputs, proof = self.equivalent_artifact()
		changed_contract = loop_contract('c1')
		changed_contract['states']['qb']['transitions'] = {'/': 'q0', 'a': 'qa'}
		changed = make_inputs(
			[changed_contract, simple_contract('c2')],
			['*.log', '!keep.log'],
			['**.log', '!keep.log'],
		)
		issues = verify_proof(changed, proof)
		self.assertTrue(issues)
		# All structural complaints must concern the changed contract only.
		self.assertEqual({i.get('segment') for i in issues}, {'c1'})

	def test_tampered_decision_detected(self):
		inputs, proof = self.equivalent_artifact()
		tampered = copy.deepcopy(proof)
		state = tampered['segments']['c1']['states'][1]
		leaf_type = next(iter(state['leaf_decisions']))
		state['leaf_decisions'][leaf_type]['new']['included'] = \
			not state['leaf_decisions'][leaf_type]['new']['included']
		issues = verify_proof(inputs, tampered)
		self.assertTrue(issues)

	def test_missing_transition_detected(self):
		inputs, proof = self.equivalent_artifact()
		tampered = copy.deepcopy(proof)
		state = tampered['segments']['c1']['states'][0]
		symbol = next(iter(state['transitions']))
		del state['transitions'][symbol]
		issues = verify_proof(inputs, tampered)
		self.assertTrue(any('Missing transition' in i['message'] for i in issues))

	def test_tampered_witness_detected(self):
		inputs = make_inputs([simple_contract()], ['a'], [])
		report = compare(inputs)
		self.assertEqual(verify_report(inputs, report), [])
		# 'b' shows no difference between the versions.
		tampered = copy.deepcopy(report)
		tampered['contracts'][0]['combos'][0]['witness'] = 'b'
		issues = verify_report(inputs, tampered)
		self.assertTrue(any('no difference' in i['message'] for i in issues))
		# A wrong deciding rule line is also caught.
		tampered = copy.deepcopy(report)
		tampered['contracts'][0]['combos'][0]['old_rule_line'] = None
		issues = verify_report(inputs, tampered)
		self.assertTrue(any('rule line' in i['message'] for i in issues))

	def test_witness_partitions_recorded(self):
		inputs = make_inputs([simple_contract()], ['a'], [])
		report = compare(inputs)
		combo = report['contracts'][0]['combos'][0]
		self.assertEqual(combo['witness'], 'a')
		self.assertEqual(combo['partitions'], ['p=a'])


# ---------------------------------------------------------------------------
# Independent naive implementation used for cross-validation.
# ---------------------------------------------------------------------------

def naive_rule(line: str, lineno: int):
	"""
	Independently translate a rule line to a Python regex. Returns
	``(lineno, ignore, dir_only, compiled_regex)`` or ``None`` for no-ops.
	"""
	if line == '':
		return None
	ignore = True
	body = line
	if body.startswith('!'):
		ignore = False
		body = body[1:]
	if body == '':
		return None
	dir_only = False
	if body.endswith('/') and not body.endswith('\\/'):
		dir_only = True
		body = body[:-1]
	if body == '':
		return None
	anchored = body.startswith('/') or '/' in body
	if body.startswith('/'):
		body = body[1:]
	segments = body.split('/')

	collapsed = []
	for seg in segments:
		is_ds = len(seg) >= 2 and set(seg) == {'*'}
		if collapsed and is_ds and len(collapsed[-1]) >= 2 and set(collapsed[-1]) == {'*'}:
			continue
		collapsed.append(seg)
	segments = collapsed

	def seg_regex(seg):
		out = []
		i = 0
		while i < len(seg):
			char = seg[i]
			if char == '\\' and i + 1 < len(seg):
				out.append(re.escape(seg[i + 1]))
				i += 2
			elif char == '*':
				while i < len(seg) and seg[i] == '*':
					i += 1
				out.append('[^/]*')
			elif char == '[':
				end = seg.index(']', i + 1)
				body_ = seg[i + 1:end]
				if body_.startswith('!'):
					body_ = '^/' + body_[1:]
				out.append('[' + body_ + ']')
				i = end + 1
			else:
				out.append(re.escape(char))
				i += 1
		return ''.join(out)

	def is_ds(seg):
		return len(seg) >= 2 and set(seg) == {'*'}

	if len(segments) == 1 and is_ds(segments[0]):
		regex = '.*'
	elif not anchored:
		regex = '(?:.*/)?' + seg_regex(segments[0])
	else:
		parts = []
		need_slash = False
		last = len(segments) - 1
		for i, seg in enumerate(segments):
			if is_ds(seg):
				if i == 0:
					parts.append('(?:[^/]+/)*')
				elif i == last:
					parts.append('(?:/[^/]+)+')
				else:
					parts.append('(?:/[^/]+)*/')
				need_slash = False
			else:
				if need_slash:
					parts.append('/')
				parts.append(seg_regex(seg))
				need_slash = True
		regex = ''.join(parts)

	return (lineno, ignore, dir_only, re.compile(regex))


def naive_decide(naive_rules, path: str, is_dir: bool):
	last = None
	for rule in naive_rules:
		_, _, dir_only, regex = rule
		if dir_only and not is_dir:
			continue
		if regex.fullmatch(path):
			last = rule
	if last is None:
		return False, None
	return last[1], last[0]


def naive_leaf_outcome(contract, naive_rules, path: str):
	"""
	Simulate the documented semantics directly on *path*.
	"""
	current = contract.start
	blocker = None
	depth = 0
	for i, symbol in enumerate(path):
		state = contract.states[current]
		if state.is_dir and depth > 0 and blocker is None:
			ignore, line = naive_decide(naive_rules, path[:i], True)
			if ignore:
				blocker = line
		current = state.transitions[symbol]
		depth += 1
	if blocker is not None:
		return False, blocker
	ignore, line = naive_decide(naive_rules, path, False)
	return (not ignore), line


class CrossValidationTest(unittest.TestCase):
	"""
	Cross-validate the DFA machinery and the engine against the independent
	naive implementation above.
	"""

	LETTERS = ['a', 'b']
	FUZZ_ALPHABET = ['/', 'a', 'b']

	def random_rule_line(self, rng):
		segments = []
		for _ in range(rng.randint(1, 3)):
			roll = rng.random()
			if roll < 0.35:
				segments.append(rng.choice(['a', 'b', 'ab', 'ba', 'aa']))
			elif roll < 0.5:
				segments.append('*')
			elif roll < 0.65:
				segments.append('**')
			elif roll < 0.8:
				segments.append(rng.choice(['[ab]', '[!a]', '[a-b]']))
			else:
				segments.append(rng.choice(['a*', '*b', 'a*b', '[ab]a', 'a[ab]']))
		line = '/'.join(segments)
		if rng.random() < 0.3:
			line = '/' + line
		if rng.random() < 0.25:
			line = '!' + line
		if rng.random() < 0.25:
			line += '/'
		return line

	def random_contract(self, rng, cid):
		count = rng.randint(2, 4)
		names = [f'q{i}' for i in range(count)]
		states = {}
		for name in names:
			transitions = {}
			for symbol in self.FUZZ_ALPHABET:
				if rng.random() < 0.5:
					transitions[symbol] = rng.choice(names)
			states[name] = {
				'dir': rng.random() < 0.7,
				'leaf_types': [lt for lt in ('f1', 'f2') if rng.random() < 0.4],
				'partition': None,
				'transitions': transitions,
			}
		return {'id': cid, 'start': 'q0', 'states': states}

	def test_dfa_matches_naive_regex(self):
		rng = random.Random(20261112)
		for _ in range(300):
			line = self.random_rule_line(rng)
			naive = naive_rule(line.lstrip('!'), 1)
			compiled = compile_rules([line], self.FUZZ_ALPHABET)
			if naive is None:
				continue
			_, _, dir_only, regex = naive
			self.assertEqual(len(compiled), 1)
			self.assertEqual(compiled[0].dir_only, dir_only)
			self.assertEqual(compiled[0].ignore, not line.startswith('!'))
			dfa = compiled[0].dfa
			for _ in range(30):
				length = rng.randint(0, 6)
				path = ''.join(rng.choice(self.FUZZ_ALPHABET) for _ in range(length))
				state = dfa.start
				for symbol in path:
					state = dfa.step(state, symbol)
				self.assertEqual(
					dfa.accepts(state), bool(regex.fullmatch(path)),
					f"rule {line!r} path {path!r}",
				)

	def enumerate_paths(self, contract, max_depth):
		"""
		All legal paths of the contract up to *max_depth*, in (length, caller
		order) order, paired with their contract state.
		"""
		result = []
		queue = [('', contract.start)]
		while queue:
			path, state_name = queue.pop(0)
			result.append((path, state_name))
			if len(path) >= max_depth:
				continue
			for symbol in self.FUZZ_ALPHABET:
				target = contract.states[state_name].transitions.get(symbol)
				if target is not None:
					queue.append((path + symbol, target))
		return result

	def test_engine_matches_brute_force(self):
		max_depth = 6
		for seed in range(40):
			rng = random.Random(1000 + seed)
			contract_json = self.random_contract(rng, 'c1')
			old_lines = [self.random_rule_line(rng) for _ in range(rng.randint(0, 3))]
			new_lines = [self.random_rule_line(rng) for _ in range(rng.randint(0, 3))]
			inputs = make_inputs(
				[contract_json], old_lines, new_lines, alphabet=self.FUZZ_ALPHABET,
			)
			contract = inputs.contracts[0]

			# Preserve negation in the naive tuples.
			old_naive = []
			for i, line in enumerate(old_lines):
				ignore = not line.startswith('!')
				stripped = line[1:] if line.startswith('!') else line
				naive = naive_rule(stripped, i + 1)
				if naive is not None:
					old_naive.append((i + 1, ignore, naive[2], naive[3]))
			new_naive = []
			for i, line in enumerate(new_lines):
				ignore = not line.startswith('!')
				stripped = line[1:] if line.startswith('!') else line
				naive = naive_rule(stripped, i + 1)
				if naive is not None:
					new_naive.append((i + 1, ignore, naive[2], naive[3]))

			# Brute-force combos over all paths up to max_depth.
			brute = {}
			for path, state_name in self.enumerate_paths(contract, max_depth):
				state = contract.states[state_name]
				for leaf_type in state.leaf_types:
					old_inc, old_line = naive_leaf_outcome(contract, old_naive, path)
					new_inc, new_line = naive_leaf_outcome(contract, new_naive, path)
					if old_inc != new_inc:
						direction = 'added' if new_inc else 'removed'
						combo = (direction, old_line, new_line, leaf_type)
						if combo not in brute:
							brute[combo] = path

			result = compare(inputs)
			if not brute:
				# No difference up to max_depth; the engine may still find a
				# deeper one. Verify whatever it produced.
				self.assertEqual(verify_artifact(inputs, result), [], f"seed {seed}")
				continue

			self.assertEqual(result['verdict'], 'different', f"seed {seed}")
			self.assertEqual(verify_artifact(inputs, result), [], f"seed {seed}")
			engine_combos = {
				(combo['direction'], combo['old_rule_line'], combo['new_rule_line'], combo['leaf_type']):
				combo['witness']
				for combo in result['contracts'][0]['combos']
			}
			for combo, witness in brute.items():
				self.assertIn(combo, engine_combos, f"seed {seed} combo {combo}")
				self.assertEqual(engine_combos[combo], witness, f"seed {seed} combo {combo}")
			for combo, witness in engine_combos.items():
				if len(witness) <= max_depth:
					self.assertEqual(brute.get(combo), witness, f"seed {seed} combo {combo}")


if __name__ == '__main__':
	unittest.main()
