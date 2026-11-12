"""
Decide whether two versions of ordered ignore rules behave identically over
a partition namespace described by finite-state path contracts.

See ``IGNORE_MIGRATION.md`` for the method, the exactness argument, and the
certificate / witness formats.
"""
from .checker import (
	verify_artifact,
	verify_proof,
	verify_report,
)
from .engine import (
	compare,
)
from .model import (
	Inputs,
	load_inputs,
	stable_hash,
)
from .rules import (
	CompiledRule,
	Rule,
	compile_rules,
	parse_rule,
)

__all__ = [
	'CompiledRule',
	'Inputs',
	'Rule',
	'compare',
	'compile_rules',
	'load_inputs',
	'parse_rule',
	'stable_hash',
	'verify_artifact',
	'verify_proof',
	'verify_report',
]
