"""
Finite automata over an explicit finite alphabet.

Regular expressions are represented as AST tuples:

-	``('eps',)`` -- the empty string.
-	``('lit', ch)`` -- a single literal symbol.
-	``('class', frozenset(...))`` -- one symbol from the given set.
-	``('concat', (a, b, ...))`` -- concatenation.
-	``('alt', (a, b, ...))`` -- alternation.
-	``('star', a)``, ``('plus', a)``, ``('quest', a)`` -- repetition.

An AST is compiled to an NFA (Thompson construction), determinized (subset
construction), and minimized. DFA state numbers are assigned canonically:
states are numbered in breadth-first order following the caller-supplied
alphabet order. The construction is therefore a pure, deterministic function
of the AST and the ordered alphabet, which the independent certificate
checker relies on to reproduce the exact same automata from the inputs.
"""
from __future__ import annotations

from collections import (
	deque,
)
from typing import (
	Dict,
	FrozenSet,
	Iterable,
	List,
	Sequence,
	Set,
	Tuple,
)

#: AST node type.
Ast = Tuple


class DFA:
	"""
	A complete deterministic finite automaton.
	"""

	__slots__ = ('alphabet', 'start', 'trans', 'accept')

	def __init__(
		self,
		alphabet: Sequence[str],
		start: int,
		trans: List[Dict[str, int]],
		accept: Set[int],
	) -> None:
		self.alphabet = tuple(alphabet)
		self.start = start
		self.trans = trans
		self.accept = frozenset(accept)

	def step(self, state: int, symbol: str) -> int:
		return self.trans[state][symbol]

	def accepts(self, state: int) -> bool:
		return state in self.accept

	def __len__(self) -> int:
		return len(self.trans)


class _NFA:
	"""
	An NFA with epsilon transitions used by the Thompson construction.
	"""

	def __init__(self) -> None:
		self.trans: List[Dict[str, Set[int]]] = []
		self.eps: List[Set[int]] = []
		self.start = 0
		self.accepts: Set[int] = set()

	def new_state(self) -> int:
		self.trans.append({})
		self.eps.append(set())
		return len(self.trans) - 1

	def add(self, src: int, symbol: str, dst: int) -> None:
		self.trans[src].setdefault(symbol, set()).add(dst)

	def add_eps(self, src: int, dst: int) -> None:
		self.eps[src].add(dst)


def _build(ast: Ast, nfa: _NFA, src: int, dst: int) -> None:
	"""
	Connect *src* to *dst* in *nfa* with a fragment recognizing *ast*.
	"""
	kind = ast[0]
	if kind == 'eps':
		nfa.add_eps(src, dst)

	elif kind == 'lit':
		nfa.add(src, ast[1], dst)

	elif kind == 'class':
		for ch in sorted(ast[1]):
			nfa.add(src, ch, dst)

	elif kind == 'concat':
		parts = ast[1]
		if not parts:
			nfa.add_eps(src, dst)
		prev = src
		for i, part in enumerate(parts):
			nxt = dst if i == len(parts) - 1 else nfa.new_state()
			_build(part, nfa, prev, nxt)
			prev = nxt

	elif kind == 'alt':
		for part in ast[1]:
			sub_src = nfa.new_state()
			sub_dst = nfa.new_state()
			nfa.add_eps(src, sub_src)
			_build(part, nfa, sub_src, sub_dst)
			nfa.add_eps(sub_dst, dst)

	elif kind == 'star':
		sub_src = nfa.new_state()
		sub_dst = nfa.new_state()
		nfa.add_eps(src, sub_src)
		nfa.add_eps(src, dst)
		nfa.add_eps(sub_dst, sub_src)
		nfa.add_eps(sub_dst, dst)
		_build(ast[1], nfa, sub_src, sub_dst)

	elif kind == 'plus':
		sub_src = nfa.new_state()
		sub_dst = nfa.new_state()
		nfa.add_eps(src, sub_src)
		nfa.add_eps(sub_dst, sub_src)
		nfa.add_eps(sub_dst, dst)
		_build(ast[1], nfa, sub_src, sub_dst)

	elif kind == 'quest':
		nfa.add_eps(src, dst)
		_build(ast[1], nfa, src, dst)

	else:
		raise ValueError(f"Unknown AST node: {kind!r}.")


def _determinize(nfa: _NFA, alphabet: Sequence[str]) -> DFA:
	"""
	Subset construction. States are numbered in BFS order following
	*alphabet* order so the result is canonical.
	"""
	def eps_closure(states: Iterable[int]) -> FrozenSet[int]:
		seen = set(states)
		stack = list(seen)
		while stack:
			state = stack.pop()
			for nxt in nfa.eps[state]:
				if nxt not in seen:
					seen.add(nxt)
					stack.append(nxt)
		return frozenset(seen)

	start = eps_closure([nfa.start])
	ids = {start: 0}
	trans: List[Dict[str, int]] = []
	accept: Set[int] = set()
	queue = deque([start])
	while queue:
		subset = queue.popleft()
		sid = ids[subset]
		assert sid == len(trans)
		row: Dict[str, int] = {}
		if nfa.accepts & subset:
			accept.add(sid)
		for symbol in alphabet:
			reached: Set[int] = set()
			for state in subset:
				reached.update(nfa.trans[state].get(symbol, ()))
			target = eps_closure(reached)
			if target not in ids:
				ids[target] = len(ids)
				queue.append(target)
			row[symbol] = ids[target]
		trans.append(row)

	return DFA(alphabet, 0, trans, accept)


def _minimize(dfa: DFA) -> DFA:
	"""
	Hopcroft-style partition refinement (iterative refinement is sufficient
	for the small automata produced by ignore rules). The result is
	renumbered canonically (BFS in alphabet order).
	"""
	alphabet = dfa.alphabet
	n = len(dfa.trans)
	if not dfa.accept or len(dfa.accept) == n:
		block = [0] * n
	else:
		block = [0 if s in dfa.accept else 1 for s in range(n)]

	while True:
		sig_to_id: Dict[Tuple, int] = {}
		new_block = [0] * n
		for state in range(n):
			sig = (
				block[state],
				tuple(block[dfa.trans[state][sym]] for sym in alphabet),
			)
			if sig not in sig_to_id:
				sig_to_id[sig] = len(sig_to_id)
			new_block[state] = sig_to_id[sig]
		if len(sig_to_id) == len(set(block)):
			break
		block = new_block

	block_count = len(set(block))
	# Map from block id to a representative state.
	rep = [0] * block_count
	for state in range(n):
		rep[block[state]] = state
	block_trans: List[Dict[str, int]] = [
		{sym: block[dfa.trans[rep[b]][sym]] for sym in alphabet}
		for b in range(block_count)
	]
	block_accept = {b for b in range(block_count) if rep[b] in dfa.accept}
	block_start = block[dfa.start]

	# Canonical renumbering: BFS from the start block in alphabet order.
	order = {block_start: 0}
	queue = deque([block_start])
	while queue:
		b = queue.popleft()
		for sym in alphabet:
			t = block_trans[b][sym]
			if t not in order:
				order[t] = len(order)
				queue.append(t)

	trans = [
		{sym: order[block_trans[b][sym]] for sym in alphabet}
		for b in range(block_count)
	]
	# Reorder rows by canonical id.
	canonical_trans = [None] * block_count  # type: ignore[list-item]
	for b in range(block_count):
		canonical_trans[order[b]] = trans[b]
	accept = {order[b] for b in block_accept}
	return DFA(alphabet, 0, canonical_trans, accept)  # type: ignore[arg-type]


def compile_ast(ast: Ast, alphabet: Sequence[str]) -> DFA:
	"""
	Compile a regular-expression AST to a complete, minimal, canonically
	numbered DFA over *alphabet* (given in the caller's order).
	"""
	nfa = _NFA()
	start = nfa.new_state()
	end = nfa.new_state()
	nfa.start = start
	nfa.accepts = {end}
	_build(ast, nfa, start, end)
	return _minimize(_determinize(nfa, alphabet))
