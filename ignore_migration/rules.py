"""
Parsing and compilation of the ignore-rule subset.

Supported rule features (a subset of gitignore syntax):

-	literal characters (``\\x`` escapes the following character),
-	character classes: ``[abc]``, ``[a-z]``, ``[!abc]`` (also ``[^abc]``);
	a class never matches the path separator,
-	``*`` -- any run of non-separator characters (empty run included),
-	``**`` as a whole path segment -- any run of characters including
	separators, with the usual gitignore placement rules,
-	anchoring: a leading ``/`` or any other separator in the pattern anchors
	it to the namespace root; otherwise the pattern matches the basename at
	any depth,
-	negation: a leading ``!``,
-	directory-only rules: a trailing ``/``.

Matching semantics per path (git's documented semantics):

-	an unanchored rule matches a path iff its single segment glob matches the
	path's basename,
-	an anchored rule matches a path iff its segment translation matches the
	whole path,
-	a directory-only rule applies only to directory decisions,
-	rules are ordered: the last matching rule decides; a negated rule decides
	"include", any other rule decides "exclude"; with no matching rule the
	default is "include",
-	a leaf is reachable (included) iff no ancestor directory is excluded and
	the leaf's own decision is "include"; rules cannot revive a leaf under an
	excluded directory.
"""
from __future__ import annotations

from dataclasses import (
	dataclass,
)
from typing import (
	FrozenSet,
	List,
	Optional,
	Sequence,
	Tuple,
)

from .automata import (
	Ast,
	DFA,
	compile_ast,
)

SEPARATOR = '/'
"""
The path separator.
"""


@dataclass(frozen=True)
class Rule:
	"""
	A parsed ignore rule.
	"""

	lineno: int
	"""1-based line number in the original rule file."""

	text: str
	"""The original rule line."""

	ignore: bool
	"""Whether a match excludes (:data:`True`) or includes (negated rule)."""

	dir_only: bool
	"""Whether the rule applies only to directories (trailing ``/``)."""

	anchored: bool
	"""Whether the rule is anchored to the namespace root."""

	segments: Tuple[str, ...]
	"""The pattern split into path segments (escapes preserved)."""


@dataclass(frozen=True)
class CompiledRule:
	"""
	A rule compiled to a DFA over the path alphabet.
	"""

	lineno: int
	text: str
	ignore: bool
	dir_only: bool
	dfa: DFA


def _is_double_star(segment: str) -> bool:
	"""
	Whether *segment* consists only of two or more asterisks (git collapses
	longer runs to ``**``).
	"""
	return len(segment) >= 2 and set(segment) == {'*'}


def _split_segments(body: str) -> List[str]:
	"""
	Split a pattern on unescaped separators. Escapes are preserved for the
	segment translator.
	"""
	segments = []
	current: List[str] = []
	i = 0
	end = len(body)
	while i < end:
		char = body[i]
		if char == '\\' and i + 1 < end:
			current.append(body[i:i + 2])
			i += 2
			continue
		if char == SEPARATOR:
			segments.append(''.join(current))
			current = []
		else:
			current.append(char)
		i += 1
	segments.append(''.join(current))
	return segments


def parse_rule(line: str, lineno: int) -> Optional[Rule]:
	"""
	Parse one rule line. Returns :data:`None` for no-op lines (blank lines,
	bare ``!``, bare ``/``). Raises :exc:`ValueError` on invalid input.
	"""
	if not isinstance(line, str):
		raise TypeError(f"Rule line {lineno} is not a string: {line!r}.")
	if line == '':
		return None

	ignore = True
	body = line
	if body.startswith('!'):
		ignore = False
		body = body[1:]
	if body == '':
		# A bare '!' is a no-op.
		return None

	dir_only = False
	if body.endswith(SEPARATOR) and not body.endswith('\\' + SEPARATOR):
		dir_only = True
		body = body[:-1]
	if body == '':
		# A bare '/' is a no-op (matches git).
		return None

	anchored = body.startswith(SEPARATOR) or SEPARATOR in body
	if body.startswith(SEPARATOR):
		body = body[1:]

	segments = _split_segments(body)
	if any(seg == '' for seg in segments):
		raise ValueError(f"Empty path segment in rule line {lineno}: {line!r}.")

	# Collapse runs of '**' segments (git treats '**/**' as '**').
	collapsed: List[str] = []
	for seg in segments:
		if collapsed and _is_double_star(seg) and _is_double_star(collapsed[-1]):
			continue
		collapsed.append(seg)

	return Rule(
		lineno=lineno,
		text=line,
		ignore=ignore,
		dir_only=dir_only,
		anchored=anchored,
		segments=tuple(collapsed),
	)


def _parse_class(
	segment: str,
	start: int,
	non_separator: FrozenSet[str],
) -> Tuple[FrozenSet[str], int]:
	"""
	Parse a character class starting at *start* (``segment[start] == '['``).
	Returns the matched symbol set (restricted to *non_separator*) and the
	index one past the closing bracket.
	"""
	end = len(segment)
	j = start + 1
	negated = False
	if j < end and segment[j] in ('!', '^'):
		negated = True
		j += 1

	chars = set()
	if j < end and segment[j] == ']':
		chars.add(']')
		j += 1

	while j < end and segment[j] != ']':
		char = segment[j]
		if char == '\\' and j + 1 < end:
			chars.add(segment[j + 1])
			j += 2
			continue
		if j + 2 < end and segment[j + 1] == '-' and segment[j + 2] != ']':
			lo, hi = segment[j], segment[j + 2]
			if lo > hi:
				raise ValueError(f"Invalid range {lo}-{hi!r} in class {segment!r}.")
			chars.update(chr(point) for point in range(ord(lo), ord(hi) + 1))
			j += 3
			continue
		chars.add(char)
		j += 1

	if j >= end:
		raise ValueError(f"Unterminated character class in {segment!r}.")

	# A class never matches the path separator.
	chars.discard(SEPARATOR)
	if negated:
		chars = set(non_separator) - chars
	else:
		chars &= set(non_separator)
	return frozenset(chars), j + 1


def _segment_to_ast(
	segment: str,
	non_separator: FrozenSet[str],
	alphabet: FrozenSet[str],
) -> Ast:
	"""
	Translate one path-segment glob to a regular-expression AST.
	"""
	parts: List[Ast] = []
	i = 0
	end = len(segment)
	while i < end:
		char = segment[i]
		if char == '\\' and i + 1 < end:
			literal = segment[i + 1]
			if literal not in alphabet:
				raise ValueError(f"Symbol {literal!r} in {segment!r} is not in the alphabet.")
			parts.append(('lit', literal))
			i += 2
		elif char == '*':
			while i < end and segment[i] == '*':
				i += 1
			# A star run inside a segment (including '**' not spanning a whole
			# segment) behaves as a single '*'.
			parts.append(('star', ('class', non_separator)))
		elif char == '[':
			charset, i = _parse_class(segment, i, non_separator)
			parts.append(('class', charset))
		else:
			if char not in alphabet:
				raise ValueError(f"Symbol {char!r} in {segment!r} is not in the alphabet.")
			parts.append(('lit', char))
			i += 1

	if not parts:
		return ('eps',)
	if len(parts) == 1:
		return parts[0]
	return ('concat', tuple(parts))


def rule_to_ast(
	rule: Rule,
	alphabet: FrozenSet[str],
	non_separator: FrozenSet[str],
) -> Ast:
	"""
	Translate a parsed rule to a regular-expression AST matched against the
	whole path string.
	"""
	any_symbol: Ast = ('class', alphabet)
	non_slash: Ast = ('class', non_separator)
	segments = rule.segments

	if len(segments) == 1 and _is_double_star(segments[0]):
		# '**' alone matches every path.
		return ('star', any_symbol)

	if not rule.anchored:
		# Unanchored rules have no separator: match the basename.
		prefix: Ast = ('quest', ('concat', (('star', any_symbol), ('lit', SEPARATOR))))
		return ('concat', (prefix, _segment_to_ast(segments[0], non_separator, alphabet)))

	parts: List[Ast] = []
	need_slash = False
	last = len(segments) - 1
	for i, seg in enumerate(segments):
		if _is_double_star(seg):
			if i == 0:
				# Leading '**/' matches any leading directories (or none). The
				# trailing separator is consumed here.
				parts.append(('star', ('concat', (('plus', non_slash), ('lit', SEPARATOR)))))
			elif i == last:
				# Trailing '/**' matches everything inside (at least one
				# segment), but not the directory itself.
				parts.append(('plus', ('concat', (('lit', SEPARATOR), ('plus', non_slash)))))
			else:
				# Inner '/**/' matches zero or more inner directories.
				parts.append(('star', ('concat', (('lit', SEPARATOR), ('plus', non_slash)))))
				parts.append(('lit', SEPARATOR))
			need_slash = False
		else:
			if need_slash:
				parts.append(('lit', SEPARATOR))
			parts.append(_segment_to_ast(seg, non_separator, alphabet))
			need_slash = True

	return ('concat', tuple(parts))


def compile_rules(lines: Sequence[str], alphabet: Sequence[str]) -> List[CompiledRule]:
	"""
	Parse and compile every rule line. Blank/no-op lines are skipped but line
	numbers of the original file are preserved.
	"""
	alphabet_set = frozenset(alphabet)
	non_separator = frozenset(sym for sym in alphabet if sym != SEPARATOR)
	compiled = []
	for lineno, line in enumerate(lines, start=1):
		rule = parse_rule(line, lineno)
		if rule is None:
			continue
		ast = rule_to_ast(rule, alphabet_set, non_separator)
		dfa = compile_ast(ast, alphabet)
		compiled.append(CompiledRule(
			lineno=rule.lineno,
			text=rule.text,
			ignore=rule.ignore,
			dir_only=rule.dir_only,
			dfa=dfa,
		))
	return compiled
