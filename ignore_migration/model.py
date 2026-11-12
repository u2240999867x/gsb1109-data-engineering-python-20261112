"""
Input model: the ordered path alphabet, the partition path contracts, and
the two rule versions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import (
	dataclass,
	field,
)
from typing import (
	Dict,
	List,
	Optional,
	Tuple,
)

from .rules import (
	SEPARATOR,
)


@dataclass(frozen=True)
class ContractState:
	"""
	One state of a partition path contract.

	A state marks a *position* in the namespace. ``is_dir`` marks positions
	that are complete, legal directories (positions with outgoing transitions
	but ``is_dir == False`` are mere prefixes inside a name). ``leaf_types``
	lists the leaf types that may occur at exactly this position.
	``partition`` is an optional annotation naming the partition this
	directory position corresponds to.
	"""

	name: str
	is_dir: bool
	leaf_types: Tuple[str, ...]
	partition: Optional[str]
	transitions: Dict[str, str]


@dataclass(frozen=True)
class Contract:
	"""
	A finite-state contract describing legal directories, leaf types, and
	partition names of one partition namespace. Depth is unbounded because
	transitions may cycle.
	"""

	cid: str
	start: str
	states: Dict[str, ContractState]


@dataclass(frozen=True)
class Inputs:
	"""
	All inputs to one migration comparison.
	"""

	alphabet: Tuple[str, ...]
	"""The path alphabet in the caller's tie-breaking order. Must contain '/'."""

	contracts: Tuple[Contract, ...]
	old_lines: Tuple[str, ...]
	new_lines: Tuple[str, ...]


def _parse_contract(obj: dict) -> Contract:
	if not isinstance(obj, dict):
		raise TypeError(f"Contract must be an object: {obj!r}.")
	cid = obj['id']
	start = obj['start']
	states = {}
	for name, raw in obj['states'].items():
		leaf_types = tuple(raw.get('leaf_types', ()))
		if len(set(leaf_types)) != len(leaf_types):
			raise ValueError(f"Duplicate leaf types in state {name!r} of contract {cid!r}.")
		states[name] = ContractState(
			name=name,
			is_dir=bool(raw.get('dir', False)),
			leaf_types=leaf_types,
			partition=raw.get('partition'),
			transitions=dict(raw.get('transitions', {})),
		)
	contract = Contract(cid=cid, start=start, states=states)
	if start not in states:
		raise ValueError(f"Start state {start!r} is not defined in contract {cid!r}.")
	for state in states.values():
		for symbol, target in state.transitions.items():
			if target not in states:
				raise ValueError((
					f"Transition target {target!r} of state {state.name!r} is not "
					f"defined in contract {cid!r}."
				))
	return contract


def load_inputs(obj: dict) -> Inputs:
	"""
	Validate and load the input document::

		{
		  "alphabet": ["/", "a", "b", ...],   # caller's symbol order
		  "contracts": [
		    {"id": ..., "start": ...,
		     "states": {name: {"dir": bool, "leaf_types": [...],
		                       "partition": str|null,
		                       "transitions": {symbol: state}}}}
		  ],
		  "old_rules": [...],
		  "new_rules": [...]
		}
	"""
	if not isinstance(obj, dict):
		raise TypeError(f"Input document must be an object: {obj!r}.")

	alphabet = tuple(obj['alphabet'])
	if SEPARATOR not in alphabet:
		raise ValueError(f"The alphabet must contain the separator {SEPARATOR!r}.")
	if len(set(alphabet)) != len(alphabet):
		raise ValueError("The alphabet contains duplicate symbols.")
	for symbol in alphabet:
		if not isinstance(symbol, str) or len(symbol) != 1:
			raise ValueError(f"Alphabet symbols must be single characters: {symbol!r}.")

	contracts = tuple(_parse_contract(raw) for raw in obj.get('contracts', ()))
	ids = [contract.cid for contract in contracts]
	if len(set(ids)) != len(ids):
		raise ValueError(f"Duplicate contract ids: {ids!r}.")

	alphabet_set = set(alphabet)
	for contract in contracts:
		for state in contract.states.values():
			for symbol in state.transitions:
				if symbol not in alphabet_set:
					raise ValueError((
						f"Symbol {symbol!r} used by contract {contract.cid!r} "
						f"state {state.name!r} is not in the alphabet."
					))

	old_lines = tuple(obj['old_rules'])
	new_lines = tuple(obj['new_rules'])
	for lines, label in ((old_lines, 'old_rules'), (new_lines, 'new_rules')):
		for line in lines:
			if not isinstance(line, str):
				raise TypeError(f"{label} entries must be strings: {line!r}.")

	return Inputs(
		alphabet=alphabet,
		contracts=contracts,
		old_lines=old_lines,
		new_lines=new_lines,
	)


def canonical_json(obj) -> str:
	"""
	Serialize *obj* deterministically for hashing.
	"""
	return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def stable_hash(obj) -> str:
	"""
	A content hash over the canonical serialization of *obj*. Used to bind
	certificates and witness reports to the exact inputs they were produced
	from.
	"""
	return hashlib.sha256(canonical_json(obj).encode('utf-8')).hexdigest()


def contract_to_json(contract: Contract) -> dict:
	"""
	Serialize a contract back to its canonical JSON form (for hashing).
	"""
	return {
		'id': contract.cid,
		'start': contract.start,
		'states': {
			name: {
				'dir': state.is_dir,
				'leaf_types': list(state.leaf_types),
				'partition': state.partition,
				'transitions': dict(state.transitions),
			}
			for name, state in sorted(contract.states.items())
		},
	}
