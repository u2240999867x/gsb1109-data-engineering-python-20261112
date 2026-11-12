"""
This module provides a literal-prefix/suffix index used to narrow down the
patterns the simple backends have to run a regular expression against.

WARNING: The *pathspec._backends.simple* package is not part of the public API.
Its contents and structure are likely to change.

Most rules in a real ignore list are "cheap": they are either a literal
directory prefix (e.g. ``build/``, ``src/foo/``), a literal file name or suffix
(e.g. ``*.pyc``, ``*.log``), or one of a small number of wildcard rules. Running
every compiled regular expression for every file makes a single-file check grow
linearly with the number of rules even when almost all of them target an
unrelated part of the tree.

The index only ever *removes* work: every rule it cannot prove irrelevant is
still handed to the regular expression engine, which remains the single source
of truth for the include/exclude decision and the matched rule index. A rule is
therefore indexed only when the cheap string test is a necessary condition of a
regular-expression match, so the index can never suppress a rule that would have
matched.
"""
from __future__ import annotations

from collections.abc import (
	Iterable)
from typing import (
	Optional)  # Replaced by `X | None` in 3.10.

from pathspec.pattern import (
	Pattern)
from pathspec.patterns.gitignore.base import (
	_GitIgnoreBasePattern,
	_split_pattern,
	_strip_trailing_ws)

_GLOB_META = frozenset('*?[')
"""
The glob metacharacters that keep a path segment from being a plain literal. A
backslash escapes the next character and is handled separately.
"""

# Index kinds.
_KIND_PREFIX = 0
"""
A rooted rule made only of literal segments (e.g. ``src/foo/``). The key is the
literal directory path without a trailing separator.
"""

_KIND_BASENAME = 1
"""
An unrooted rule whose single segment is a plain literal (e.g. ``foo``,
``keep/``). The key is the unescaped literal segment.
"""

_KIND_SUFFIX = 2
"""
An unrooted rule whose single segment is a leading run of asterisks followed by
a literal (e.g. ``*.pyc``). The key is the unescaped literal suffix.
"""

_KIND_WILDCARD = 3
"""
A rule that cannot be narrowed with a cheap string test. It stays in the
"always consider" bucket.
"""

Entry = tuple[int, Pattern]
"""
An enumerated pattern: its original spec index and the compiled pattern.
"""


def _literal_segment(segment: str) -> Optional[str]:
	"""
	Unescape a single path *segment* and return its literal text, or :data:`None`
	if it contains a glob metacharacter.

	*segment* (:class:`str`) is one slash-delimited piece of a raw pattern, with
	any backslash escapes still in place.

	Returns the literal text (:class:`str`) or :data:`None`.
	"""
	out: list[str] = []
	i = 0
	end = len(segment)
	while i < end:
		char = segment[i]
		if char in _GLOB_META:
			return None
		if char == '\\':
			i += 1
			if i >= end:
				# A trailing escape is an invalid pattern; do not index it.
				return None
			char = segment[i]
		out.append(char)
		i += 1
	return ''.join(out)


def _classify_pattern(pattern: Pattern) -> tuple[int, Optional[str]]:
	"""
	Classify a compiled *pattern* for the literal index.

	The raw pattern text is re-parsed rather than the compiled regular expression
	so that glob syntax (which distinguishes a rooted ``a/b`` rule from an
	unrooted ``b`` rule) stays available. Only patterns built from a :class:`str`
	line are considered; a custom or precompiled pattern falls back to
	:data:`_KIND_WILDCARD` so it is always evaluated.

	*pattern* (:class:`.Pattern`) is the compiled pattern.

	Returns a :class:`tuple` of the index kind (:class:`int`) and the lookup key
	(:class:`str` or :data:`None` for :data:`_KIND_WILDCARD`).
	"""
	raw = getattr(pattern, 'pattern', None)
	if not isinstance(raw, str) or not raw:
		return (_KIND_WILDCARD, None)

	if not isinstance(pattern, _GitIgnoreBasePattern):
		# Only the gitignore pattern families carry the rooted/glob segment
		# semantics the index relies on. A generic or custom RegexPattern is
		# typically unanchored, so it stays in the always-evaluate bucket.
		return (_KIND_WILDCARD, None)

	# Apply the same line-level normalization the compilers do: a trailing line
	# ending and unescaped spaces are removed before the pattern is interpreted.
	line = _strip_trailing_ws(raw)

	# Strip the negation marker; an include and an exclude of the same shape are
	# indexed identically because both still have to be evaluated.
	if line.startswith('!'):
		line = line[1:]
		if not line:
			return (_KIND_WILDCARD, None)

	# A comment or a lone slash cannot match anything (null patterns are filtered
	# out before this point, but stay defensive).
	if line.startswith('#') or line == '/':
		return (_KIND_WILDCARD, None)

	# Split on slashes while honoring bracket expressions and escaped slashes.
	segments = _split_pattern(line)

	if segments[-1] == '':
		# A directory-only rule (trailing slash). The distinction is re-checked by
		# the regular expression, so it does not affect indexing.
		segments = segments[:-1]

	if not segments:
		return (_KIND_WILDCARD, None)

	# A leading slash roots the pattern; drop the empty first segment it leaves.
	if segments[0] == '':
		segments = segments[1:]
		if not segments:
			return (_KIND_WILDCARD, None)
		rooted = True
	else:
		# A single-segment rule is unrooted and may match any path component; a
		# multi-segment rule is rooted relative to the spec root.
		rooted = len(segments) > 1

	if not rooted:
		segment = segments[0]

		# A leading run of asterisks followed by a literal (e.g. "*.pyc",
		# "**.log") is indexed by its literal suffix. Consecutive stars inside a
		# segment are ordinary non-separator wildcards; the globstar spelling only
		# applies to a whole "**" segment, which leaves an empty remainder here and
		# falls through to wildcard handling below.
		if segment.startswith('*'):
			star_end = 0
			while star_end < len(segment) and segment[star_end] == '*':
				star_end += 1
			suffix = _literal_segment(segment[star_end:])
			if suffix:
				return (_KIND_SUFFIX, suffix)

		literal = _literal_segment(segment)
		if literal is not None:
			return (_KIND_BASENAME, literal)

		return (_KIND_WILDCARD, None)

	# Rooted: every segment must be a plain literal for the prefix test to be a
	# necessary condition of a match.
	prefix_parts: list[str] = []
	for segment in segments:
		literal = _literal_segment(segment)
		if literal is None or literal == '':
			return (_KIND_WILDCARD, None)
		prefix_parts.append(literal)

	return (_KIND_PREFIX, '/'.join(prefix_parts))


class _ReverseTrie(object):
	"""
	The :class:`_ReverseTrie` class stores literal suffix keys so that every
	suffix matching a given component can be found in time proportional to the
	component length rather than the number of indexed rules.

	Keys are inserted reversed: the suffix ``".log"`` is stored as the path
	``"gol."``. Matching a component walks its reversed text down the trie and
	collects the entries attached to every terminal node reached, which yields
	exactly the suffix keys that terminate the component.
	"""

	__slots__ = (
		'children',
		'entries',
	)

	def __init__(self) -> None:
		self.children: dict[str, _ReverseTrie] = {}
		self.entries: list[Entry] = []

	def insert(self, reversed_key: str, entry: Entry) -> None:
		"""
		Insert a *reversed_key* with its enumerated *entry*.
		"""
		node = self
		for char in reversed_key:
			child = node.children.get(char)
			if child is None:
				child = _ReverseTrie()
				node.children[char] = child
			node = child
		node.entries.append(entry)


class LiteralIndex(object):
	"""
	The :class:`LiteralIndex` class maps cheap literal keys to the enumerated
	patterns that might match a path.

	Indexed rules are partitioned into four buckets. :meth:`candidates`
	reproduces the exact set of rules the unindexed linear scan would evaluate:
	the wildcard bucket plus every literal bucket whose cheap test is a necessary
	condition of a match. Because each bucket is keyed on literal text, the
	candidate count is independent of how many unrelated rules the spec contains.
	"""

	__slots__ = (
		'_prefixes',
		'_basenames',
		'_suffix_trie',
		'_wildcards',
		'candidate_calls',
		'candidate_count',
		'regex_calls',
		'string_checks',
	)

	def __init__(self, patterns: Iterable[Entry]) -> None:
		"""
		Initialize the :class:`LiteralIndex`.

		*patterns* (:class:`~collections.abc.Iterable` of :class:`tuple`) yields
		each enumerated pattern as ``(index, pattern)``.
		"""
		self._prefixes: dict[str, list[Entry]] = {}
		self._basenames: dict[str, list[Entry]] = {}
		self._suffix_trie: _ReverseTrie = _ReverseTrie()
		self._wildcards: list[Entry] = []

		# Instrumentation. The counters are deterministic for a fixed spec and
		# input because they depend only on the literal object keys, so a test can
		# recompute them by hand and tell a genuine skip (a rule absent from the
		# candidate set) apart from merely making fewer regular-expression calls.
		self.candidate_calls: int = 0
		"""
		*candidate_calls* (:class:`int`) is the number of times
		:meth:`candidates` was invoked (one per evaluated path).
		"""

		self.candidate_count: int = 0
		"""
		*candidate_count* (:class:`int`) is the number of candidate rules offered
		across all calls. A rule not offered here is genuinely skipped; the value
		is recomputable from the literal object keys and the queried paths.
		"""

		self.regex_calls: int = 0
		"""
		*regex_calls* (:class:`int`) is the number of candidate rules the backend
		actually evaluated with a regular expression. It can be lower than
		:attr:`candidate_count` when an ordering optimization stops early, which is
		distinct from a rule being skipped by the index.
		"""

		self.string_checks: int = 0
		"""
		*string_checks* (:class:`int`) is the number of cheap literal lookups
		(dictionary and trie lookups on :class:`str` keys) performed while
		gathering candidates.
		"""

		for entry in patterns:
			kind, key = _classify_pattern(entry[1])
			if kind == _KIND_PREFIX:
				self._prefixes.setdefault(key, []).append(entry)  # type: ignore[arg-type]
			elif kind == _KIND_BASENAME:
				self._basenames.setdefault(key, []).append(entry)  # type: ignore[arg-type]
			elif kind == _KIND_SUFFIX:
				self._suffix_trie.insert(key[::-1], entry)  # type: ignore[union-attr]
			else:
				self._wildcards.append(entry)

	def candidates(self, file: str) -> tuple[list[Entry], int]:
		"""
		Return the enumerated patterns that can match *file* and the number of
		cheap string lookups used to decide.

		*file* (:class:`str`) is the normalized file path (it may end in ``/`` for
		a directory query).

		Returns a :class:`tuple` of the candidate rules
		(:class:`list` of :class:`tuple`, ordered by original spec index) and the
		string-check count (:class:`int`).
		"""
		self.candidate_calls += 1
		checks = 0

		# A trailing separator marks a directory query; it is not a path component
		# and must not participate in basename/suffix lookups.
		trimmed = file[:-1] if file.endswith('/') else file
		components = trimmed.split('/') if trimmed else []

		candidate_ids: set[int] = set()
		candidates: list[Entry] = []

		def add(entries: list[Entry]) -> None:
			for entry in entries:
				index = entry[0]
				if index not in candidate_ids:
					candidate_ids.add(index)
					candidates.append(entry)

		# Wildcard rules can always match and are evaluated in full.
		add(self._wildcards)

		# Rooted literal-prefix rules. A rule "a/b" matches "a/b" itself and
		# anything beneath "a/b/"; a directory-only rule only matches beneath it.
		# The regular expression re-checks that distinction, so both shapes use
		# the same necessary-condition lookup here.
		path = trimmed
		while path:
			checks += 1
			entries = self._prefixes.get(path)
			if entries is not None:
				add(entries)
			slash = path.rfind('/')
			if slash == -1:
				break
			path = path[:slash]

		# Unrooted literal rules may match any path component.
		basenames = self._basenames
		trie = self._suffix_trie
		for component in components:
			if not component:
				continue

			if basenames:
				# Exact component name lookup.
				checks += 1
				entries = basenames.get(component)
				if entries is not None:
					add(entries)

			# Walk the reversed component through the suffix trie. Each terminal
			# node reached is a suffix key that terminates this component.
			node = trie
			for char in reversed(component):
				checks += 1
				child = node.children.get(char)
				if child is None:
					break
				node = child
				if node.entries:
					add(node.entries)

		candidates.sort(key=lambda entry: entry[0])

		self.candidate_count += len(candidates)
		self.string_checks += checks
		return candidates, checks

	def note_regex_calls(self, count: int) -> None:
		"""
		Record that *count* candidate rules were actually evaluated with a regular
		expression. Backends call this so :attr:`regex_calls` reflects real work
		(including early termination) rather than the number of candidates offered.
		"""
		self.regex_calls += count

	def reset_counters(self) -> None:
		"""
		Reset the instrumentation counters to zero.
		"""
		self.candidate_calls = 0
		self.candidate_count = 0
		self.regex_calls = 0
		self.string_checks = 0
