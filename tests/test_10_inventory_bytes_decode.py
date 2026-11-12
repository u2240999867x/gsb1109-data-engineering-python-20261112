"""
Regression tests for selecting inventory objects whose names exist both as text
keys (``str``) and as legacy byte keys (``bytes``).

An inventory object can carry two representations of the same name. The public
matching API only accepts text paths (see :func:`pathspec.util.normalize_file`),
so a caller has to decode byte keys before they reach a matcher. These tests pin
down the interaction of:

- rule escaping (a literal ``!`` vs. a negation, and escaped trailing spaces),
- directory markers (the trailing ``/`` used by directory-only rules),
- path separators (native separators normalized to POSIX ``/``),
- caller-declared decoding policies (UTF-8 strict, Latin-1, UTF-8 replace).

For a pair of representations that round-trips losslessly and denotes the same
name, leaf sets, winning rules and parent-directory reachability are checked
independently against each representation's own object identity. A byte key that
cannot be decoded, or two distinct objects that collapse onto one decoded name,
are kept as reproducible ambiguity records instead of being silently matched.
"""
from __future__ import annotations

import os
import stat
import unittest
from dataclasses import (
	dataclass)
from typing import (
	Callable,
	Optional)

from pathspec import (
	GitIgnoreSpec,
	PathSpec)
from pathspec.util import (
	TreeEntry,
	normalize_file)

RULE_ENCODING = 'utf-8'
"""
Encoding used to compile the byte representation of the rule lines. It matches
the on-disk encoding of the UTF-8 byte keys so the two specs describe the same
rules.
"""

# Rule lines exercising escaping, negation, directory markers and trailing
# whitespace. Indices are referenced explicitly in the assertions below.
RULE_LINES: list[str] = [
	r'\!keep.txt',            # 0: literal '!', not a negation.
	'!build/excluded.txt',    # 1: negation of one file.
	'build/',                 # 2: excluded directory (directory marker).
	'!build/keep/',           # 3: re-included descendant directory.
	'spaces/name ',           # 4: unescaped trailing space is swallowed.
	r'spaces/name\ ',         # 5: escaped trailing space is part of the name.
	'café/*.py',              # 6: non-ASCII text rule.
]


@dataclass(frozen=True)
class InvObject:
	"""
	One inventory object identified independently of its name representations.

	*obj_id* is the stable object identity.

	*text_key* is the text name (:class:`str`) or :data:`None` when the object is
	only known by its byte name.

	*byte_key* is the legacy byte name (:class:`bytes`) or :data:`None` when the
	object is only known by a text name.

	*is_dir* is whether the object is a directory and therefore needs a trailing
	path separator when matched.
	"""

	obj_id: str
	text_key: Optional[str]
	byte_key: Optional[bytes]
	is_dir: bool = False


@dataclass(frozen=True)
class DecodePolicy:
	"""
	A caller-declared policy for turning a byte key into a text path.

	*name* is the reproducible policy label.

	*decode* performs the decoding.

	*encoding* is the codec name used to verify whether a particular key
	round-trips losslessly. A lossy error handler (``errors='replace'``) only
	rewrites the keys it cannot decode; a key that is valid for the codec still
	round-trips, so losslessness is decided per key rather than for the whole
	policy.
	"""

	name: str
	decode: Callable[[bytes], str]
	encoding: Optional[str]


POLICIES: tuple[DecodePolicy, ...] = (
	DecodePolicy('utf-8-strict', lambda __b: __b.decode('utf-8'), 'utf-8'),
	# Latin-1 is a byte bijection so it never fails and never collides, but its
	# decoding of a UTF-8 key is not the same name as the text key.
	DecodePolicy('latin-1', lambda __b: __b.decode('latin-1'), 'latin-1'),
	# The replacement handler rewrites undecodable bytes, which can make two
	# distinct objects collide on the replacement character.
	DecodePolicy(
		'utf-8-replace',
		lambda __b: __b.decode('utf-8', errors='replace'),
		'utf-8',
	),
)


@dataclass(frozen=True)
class Ambiguity:
	"""
	A reproducible record explaining why an object cannot be selected by identity.

	*kind* is one of ``'undecodable'``, ``'lossy_rewrite'``,
	``'name_divergence'`` or ``'name_collision'``.

	*policy* is the policy label under which the ambiguity was observed.

	*object_ids* are the stable identities involved (one object, except for a
	collision).

	*name* is the decoded normalized name when available, otherwise :data:`None`.

	*detail* is deterministic diagnostic text (including the raw byte hex).
	"""

	kind: str
	policy: str
	object_ids: tuple[str, ...]
	name: Optional[str]
	detail: str


# Paired objects whose UTF-8 byte key and text key denote the same name.
PAIRED_OBJECTS: tuple[InvObject, ...] = (
	InvObject('O1', 'café/app.py', b'caf\xc3\xa9/app.py'),
	InvObject('O2', '!keep.txt', b'!keep.txt'),
	InvObject('O3', 'build/excluded.txt', b'build/excluded.txt'),
	InvObject('O4', 'build/keep/leaf.txt', b'build/keep/leaf.txt'),
	InvObject('O5', 'spaces/name', b'spaces/name'),
	InvObject('O6', 'spaces/name ', b'spaces/name '),
	InvObject('O7', 'build/', b'build/', is_dir=True),
	InvObject('O8', 'build/keep/', b'build/keep/', is_dir=True),
)

# Byte-only objects, including keys that are not valid UTF-8.
BYTE_ONLY_OBJECTS: tuple[InvObject, ...] = (
	InvObject('B1', None, b'tmp/\xfffe/bad.txt'),
	InvObject('C1', None, b'a\xffb'),
	InvObject('C2', None, b'a\xfeb'),
)

# A genuine text object whose name contains a replacement character. Under the
# lossy UTF-8 policy both C1 and C2 are rewritten onto this same name.
TEXT_ONLY_OBJECTS: tuple[InvObject, ...] = (
	InvObject('T1', 'a�b', None),
)

INVENTORY: tuple[InvObject, ...] = (
	PAIRED_OBJECTS + BYTE_ONLY_OBJECTS + TEXT_ONLY_OBJECTS
)


def _with_dir_marker(path: str, is_dir: bool) -> str:
	"""Append the directory marker exactly like ``PathSpec.match_entries()``."""
	if is_dir and not path.endswith('/'):
		return path + '/'
	return path


def _with_dir_marker_bytes(path: bytes, is_dir: bool) -> bytes:
	"""Byte counterpart of :func:`_with_dir_marker`."""
	if is_dir and not path.endswith(b'/'):
		return path + b'/'
	return path


def _ancestor_dirs(path: str) -> list[str]:
	"""
	Return the strict ancestor directory queries (each with a trailing marker)
	outermost first.
	"""
	parts = path.rstrip('/').split('/')
	return ['/'.join(parts[:index]) + '/' for index in range(1, len(parts))]


def classify_inventory(
	inventory: tuple[InvObject, ...],
	policy: DecodePolicy,
) -> tuple[dict[str, str], list[Ambiguity]]:
	"""
	Classify *inventory* under *policy*.

	Returns the clean decoded paths (mapped by stable object id) and the
	reproducible ambiguity records. An object is clean only when its byte key
	decodes, round-trips losslessly, denotes the same name as its text key, and
	does not share a decoded name with another object identity.
	"""
	decoded_paths: dict[str, str] = {}
	ambiguities: list[Ambiguity] = []
	name_owners: dict[str, set[str]] = {}

	def claim(name: str, obj_id: str) -> None:
		name_owners.setdefault(name, set()).add(obj_id)

	for obj in inventory:
		if obj.text_key is not None:
			claim(obj.text_key, obj.obj_id)

		if obj.byte_key is None:
			continue

		try:
			decoded = policy.decode(obj.byte_key)
		except UnicodeDecodeError as exc:
			ambiguities.append(Ambiguity(
				'undecodable', policy.name, (obj.obj_id,), None,
				f"{exc.reason} at byte {exc.start}; hex={obj.byte_key.hex()}",
			))
			continue

		roundtrip_ok = (
			policy.encoding is not None
			and decoded.encode(policy.encoding) == obj.byte_key
		)
		identity_ok = (
			obj.text_key is None or decoded == obj.text_key
		)

		if not roundtrip_ok:
			ambiguities.append(Ambiguity(
				'lossy_rewrite', policy.name, (obj.obj_id,), decoded,
				f"hex={obj.byte_key.hex()} decoded={decoded!r}",
			))
		elif not identity_ok:
			ambiguities.append(Ambiguity(
				'name_divergence', policy.name, (obj.obj_id,), decoded,
				f"text_key={obj.text_key!r} decoded={decoded!r}",
			))

		decoded_paths[obj.obj_id] = decoded
		claim(decoded, obj.obj_id)

	for name, owners in name_owners.items():
		if len(owners) > 1:
			ambiguities.append(Ambiguity(
				'name_collision', policy.name, tuple(sorted(owners)), name,
				f"decoded name {name!r} claimed by {sorted(owners)}",
			))

	ambiguities.sort(key=lambda __a: (__a.kind, __a.object_ids, __a.name or ''))

	tainted = {
		__obj_id
		for __ambiguity in ambiguities
		for __obj_id in __ambiguity.object_ids
	}
	clean_paths = {
		obj_id: path
		for obj_id, path in decoded_paths.items()
		if obj_id not in tainted
	}
	return clean_paths, ambiguities


def _make_specs() -> tuple[PathSpec, PathSpec]:
	"""
	Build the text spec and its byte twin. The plain ``PathSpec`` with the
	``'gitignore'`` factory and the simple backend evaluates ``str`` and ``bytes``
	paths symmetrically, which is what makes an identity-based cross-check
	possible.
	"""
	text_spec = PathSpec.from_lines(
		'gitignore', RULE_LINES, backend='simple',
	)
	byte_spec = PathSpec.from_lines(
		'gitignore',
		[line.encode(RULE_ENCODING) for line in RULE_LINES],
		backend='simple',
	)
	return text_spec, byte_spec


def _entry(path: str, is_dir: bool) -> TreeEntry:
	"""Build a lightweight :class:`TreeEntry` without touching the file-system."""
	mode = (stat.S_IFDIR if is_dir else stat.S_IFREG) | 0o755
	stat_result = os.stat_result((mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))
	name = path.rstrip('/').rsplit('/', 1)[-1]
	return TreeEntry(name, path, stat_result, stat_result)


class InventoryClassificationTest(unittest.TestCase):
	"""Tests for policy-driven classification and ambiguity records."""

	def test_utf8_strict_clean_pairs_and_undecodable_records(self):
		policy = POLICIES[0]
		clean, ambiguities = classify_inventory(INVENTORY, policy)

		# Every paired object survives; byte-only and text-only objects do not.
		self.assertEqual(set(clean), {obj.obj_id for obj in PAIRED_OBJECTS})

		undecodable = [a for a in ambiguities if a.kind == 'undecodable']
		self.assertEqual(
			[(a.object_ids, a.policy) for a in undecodable],
			[(('B1',), policy.name), (('C1',), policy.name), (('C2',), policy.name)],
		)
		# Records are reproducible: the raw byte hex is captured.
		self.assertTrue(all(a.detail.startswith('invalid start byte') for a in undecodable))
		self.assertIn(BYTE_ONLY_OBJECTS[0].byte_key.hex(), undecodable[0].detail)

		self.assertFalse(any(a.kind == 'name_collision' for a in ambiguities))

	def test_latin1_is_bijective_but_diverges_from_text_identity(self):
		policy = POLICIES[1]
		clean, ambiguities = classify_inventory(INVENTORY, policy)

		# Latin-1 never fails to decode and never merges two byte objects.
		self.assertFalse(any(a.kind == 'undecodable' for a in ambiguities))
		self.assertFalse(any(a.kind == 'name_collision' for a in ambiguities))

		# The UTF-8 key round-trips as bytes but decodes to mojibake, so the pair
		# does not denote the same name and is excluded from identity parity.
		divergence = [a for a in ambiguities if a.kind == 'name_divergence']
		self.assertEqual([a.object_ids for a in divergence], [('O1',)])
		self.assertNotIn('O1', clean)

		# ASCII-only paired objects keep an identical text identity.
		self.assertIn('O2', clean)
		self.assertEqual(clean['O2'], '!keep.txt')

	def test_replace_policy_rewrites_and_collides_distinct_objects(self):
		policy = POLICIES[2]
		clean, ambiguities = classify_inventory(INVENTORY, policy)

		rewrites = [a for a in ambiguities if a.kind == 'lossy_rewrite']
		self.assertEqual(
			sorted(a.object_ids[0] for a in rewrites),
			['B1', 'C1', 'C2'],
		)

		collisions = [a for a in ambiguities if a.kind == 'name_collision']
		self.assertEqual(len(collisions), 1)
		# C1 and C2 are rewritten onto the genuine text object T1's name.
		self.assertEqual(collisions[0].object_ids, ('C1', 'C2', 'T1'))
		self.assertEqual(collisions[0].name, 'a�b')

		# Every rewritten or colliding identity is withheld from clean selection.
		self.assertFalse({'C1', 'C2', 'B1', 'T1'} & set(clean))

	def test_ambiguity_records_are_deterministic(self):
		policy = POLICIES[2]
		first = classify_inventory(INVENTORY, policy)[1]
		second = classify_inventory(tuple(reversed(INVENTORY)), policy)[1]
		self.assertEqual(first, second)


class LosslessRoundTripParityTest(unittest.TestCase):
	"""
	Identity-based cross-checks for the lossless UTF-8 representation pair.
	"""

	def setUp(self):
		self.text_spec, self.byte_spec = _make_specs()

	def test_specs_encode_the_same_rules(self):
		self.assertEqual(len(self.text_spec.patterns), len(self.byte_spec.patterns))
		for text_pattern, byte_pattern in zip(
			self.text_spec.patterns, self.byte_spec.patterns,
		):
			self.assertEqual(text_pattern.include, byte_pattern.include)
			# The byte pattern remembers its source line, which decodes back to the
			# exact text rule under the rule encoding.
			self.assertEqual(
				text_pattern.pattern,
				byte_pattern.pattern.decode(RULE_ENCODING),
			)

	def test_leaf_set_parity_by_object_identity(self):
		policy = POLICIES[0]
		clean, _ = classify_inventory(INVENTORY, policy)
		objects_by_id = {obj.obj_id: obj for obj in INVENTORY}

		text_included: set[str] = set()
		byte_included: set[str] = set()

		for obj_id in clean:
			obj = objects_by_id[obj_id]
			text_path = _with_dir_marker(
				normalize_file(obj.text_key), obj.is_dir,
			)
			byte_path = _with_dir_marker_bytes(obj.byte_key, obj.is_dir)

			text_decision = self.text_spec._backend.match_file(text_path)
			byte_decision = self.byte_spec._backend.match_file(byte_path)

			# The two representations are queried independently but must reach the
			# same include decision and winning rule index.
			self.assertEqual(
				text_decision, byte_decision,
				(f"{obj_id}: {text_path!r} vs {byte_path!r}"),
			)
			if text_decision[0]:
				text_included.add(obj_id)
			if byte_decision[0]:
				byte_included.add(obj_id)

		# The included leaf sets are identical as object identities.
		self.assertEqual(text_included, byte_included)
		self.assertEqual(
			text_included,
			{'O1', 'O2', 'O3', 'O5', 'O6', 'O7'},
		)

	def test_winning_rule_identity(self):
		# O3 is claimed by the negation (1) and then by the excluded directory (2):
		# the last matching rule wins on both representations.
		text_decision = self.text_spec._backend.match_file('build/excluded.txt')
		byte_decision = self.byte_spec._backend.match_file(b'build/excluded.txt')
		self.assertEqual(text_decision, (True, 2))
		self.assertEqual(byte_decision, (True, 2))

		# O4 sits in the re-included subtree; the directory negation (3) wins.
		text_decision = self.text_spec._backend.match_file('build/keep/leaf.txt')
		byte_decision = self.byte_spec._backend.match_file(b'build/keep/leaf.txt')
		self.assertEqual(text_decision, (False, 3))
		self.assertEqual(byte_decision, (False, 3))

		for index in (2, 3):
			self.assertEqual(
				self.text_spec.patterns[index].include,
				self.byte_spec.patterns[index].include,
			)

	def test_parent_directory_reachability_parity(self):
		text_path = 'build/keep/leaf.txt'
		ancestors = _ancestor_dirs(text_path)

		# The strict ancestor chain walked outermost first.
		self.assertEqual(ancestors, ['build/', 'build/keep/'])

		for ancestor in ancestors:
			text_decision = self.text_spec._backend.match_file(ancestor)
			byte_decision = self.byte_spec._backend.match_file(
				ancestor.encode(RULE_ENCODING),
			)
			self.assertEqual(text_decision, byte_decision, ancestor)

		# The excluded ancestor 'build/' is reachable as an excluding directory.
		self.assertEqual(
			self.text_spec._backend.match_file('build/'), (True, 2),
		)
		self.assertEqual(
			self.byte_spec._backend.match_file(b'build/'), (True, 2),
		)
		# The re-included descendant directory is reachable as a negation.
		self.assertEqual(
			self.text_spec._backend.match_file('build/keep/'), (False, 3),
		)
		self.assertEqual(
			self.byte_spec._backend.match_file(b'build/keep/'), (False, 3),
		)


class RuleEscapingTest(unittest.TestCase):
	"""The sample must distinguish the three documented failure causes."""

	def setUp(self):
		self.spec = PathSpec.from_lines('gitignore', RULE_LINES, backend='simple')
		self.patterns = self.spec.patterns

	def test_literal_exclamation_is_not_a_negation(self):
		literal = self.patterns[0]
		negation = self.patterns[1]

		# The escaped rule includes; the unescaped rule excludes.
		self.assertIs(literal.include, True)
		self.assertIs(negation.include, False)

		# The escaped rule matches the file whose name literally starts with '!'.
		self.assertEqual(
			self.spec._backend.match_file('!keep.txt'), (True, 0),
		)
		# A bare (unescaped) exclamation is a negation whose target is the name
		# without the leading '!'; it must not match a literal '!keep.txt'.
		bare = PathSpec.from_lines(
			'gitignore', ['!keep.txt'], backend='simple',
		)
		self.assertEqual(
			bare._backend.match_file('!keep.txt'), (None, None),
		)
		# Instead it negates an ordinary 'keep.txt' when that file is included.
		bare_with_include = PathSpec.from_lines(
			'gitignore', ['keep.txt', '!keep.txt'], backend='simple',
		)
		self.assertEqual(
			bare_with_include._backend.match_file('keep.txt'), (False, 1),
		)

	def test_trailing_space_swallowed_vs_escaped(self):
		swallowed = self.patterns[4]
		escaped = self.patterns[5]

		# The swallowed rule's regex has no trailing space; the escaped one does.
		self.assertNotIn(' ', swallowed.regex.pattern)
		self.assertIn(r'\ ', escaped.regex.pattern)

		# 'spaces/name' is selected by the swallowed rule only.
		self.assertEqual(
			self.spec._backend.match_file('spaces/name'), (True, 4),
		)
		# The swallowed rule must not match a name that genuinely ends with a space.
		self.assertNotEqual(
			self.spec._backend.match_file('spaces/name ')[1], 4,
		)
		# That name is instead selected by the escaped-trailing-space rule.
		self.assertEqual(
			self.spec._backend.match_file('spaces/name '), (True, 5),
		)

	def test_rewritten_key_is_matched_only_after_decode(self):
		# The native byte key never matches a rule written for the replacement
		# character; only the lossy decoding produces a name that collides.
		native_key = b'a\xffb'
		rewritten = native_key.decode('utf-8', errors='replace')
		self.assertEqual(rewritten, 'a�b')

		rule_spec = PathSpec.from_lines(
			'gitignore', ['a�b'], backend='simple',
		)
		byte_rule_spec = PathSpec.from_lines(
			'gitignore', ['a�b'.encode('utf-8')], backend='simple',
		)

		# Native bytes are untouched by the text rule (no rewrite, no match).
		self.assertIsNone(byte_rule_spec._backend.match_file(native_key)[0])
		# After the caller rewrites the key, it matches - the collision is purely
		# an artifact of the decoding policy.
		self.assertEqual(
			rule_spec._backend.match_file(rewritten), (True, 0),
		)


class DirectoryMarkerAndSeparatorTest(unittest.TestCase):
	"""Directory flags and path separators on both representations."""

	def setUp(self):
		self.text_spec, self.byte_spec = _make_specs()

	def test_directory_marker_matches_directory_not_bare_name(self):
		# Directory-only rule 'build/' matches the marker and descendants, but not
		# the bare file name.
		self.assertEqual(
			self.text_spec._backend.match_file('build/'), (True, 2),
		)
		self.assertEqual(
			self.text_spec._backend.match_file('build/leaf.txt'), (True, 2),
		)
		self.assertIsNone(
			self.text_spec._backend.match_file('build')[0],
		)
		self.assertEqual(
			self.byte_spec._backend.match_file(b'build/'), (True, 2),
		)
		self.assertIsNone(
			self.byte_spec._backend.match_file(b'build')[0],
		)

	def test_match_entries_appends_marker_without_changing_path(self):
		spec = GitIgnoreSpec.from_lines(['build/'], backend='simple')
		dir_entry = _entry('build', is_dir=True)
		file_entry = _entry('build/file.txt', is_dir=False)

		matched = list(spec.match_entries([dir_entry, file_entry]))
		self.assertEqual(matched, [dir_entry, file_entry])
		# The entry's stored path is left untouched.
		self.assertEqual(dir_entry.path, 'build')

	def test_native_separator_normalization(self):
		spec = GitIgnoreSpec.from_lines(['build/'], backend='simple')

		# A native separator is normalized to POSIX '/' when declared.
		self.assertTrue(
			spec.match_file('build\\leaf.txt', separators=('\\',)),
		)
		# Passing an empty separator container leaves the backslash literal.
		self.assertFalse(
			spec.match_file('build\\leaf.txt', separators=()),
		)

		# Byte twin: an explicit POSIX byte path matches; a raw backslash does not.
		self.assertEqual(
			self.byte_spec._backend.match_file(b'build/leaf.txt'), (True, 2),
		)
		self.assertIsNone(
			self.byte_spec._backend.match_file(b'build\\leaf.txt')[0],
		)


class MatcherBoundaryTest(unittest.TestCase):
	"""Byte keys cannot reach the text-only public matcher unchanged."""

	def test_public_api_rejects_raw_bytes(self):
		spec = GitIgnoreSpec.from_lines(['build/'])

		for method_name in ('check_file', 'match_file'):
			with self.subTest(method=method_name):
				with self.assertRaises(TypeError):
					getattr(spec, method_name)(b'build/leaf.txt')

		with self.assertRaises(TypeError):
			normalize_file(b'build/leaf.txt')

	def test_match_entries_rejects_byte_paths(self):
		spec = GitIgnoreSpec.from_lines(['build/'])
		mode = stat.S_IFDIR | 0o755
		stat_result = os.stat_result((mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))
		# A byte-keyed entry cannot be normalized by the text-only matcher.
		byte_entry = TreeEntry(b'build', b'build', stat_result, stat_result)
		with self.assertRaises(TypeError):
			list(spec.match_entries([byte_entry]))

	def test_plain_pathspec_is_the_symmetric_bytes_evaluation_path(self):
		# The harness relies on PathSpec + 'gitignore' + simple being symmetric.
		text_spec, byte_spec = _make_specs()
		self.assertEqual(
			text_spec._backend.match_file('build/keep/leaf.txt'),
			byte_spec._backend.match_file(b'build/keep/leaf.txt'),
		)
