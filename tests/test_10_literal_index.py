"""
This script tests the literal prefix/suffix index used by the simple backends
to narrow down which patterns are evaluated for each file.
"""

import re
from functools import (
	partial)
from unittest import (
	TestCase)

from pathspec import (
	GitIgnoreSpec,
	PathSpec,
	RegexPattern)
from pathspec._backends.simple._index import (
	_KIND_BASENAME,
	_KIND_PREFIX,
	_KIND_SUFFIX,
	_KIND_WILDCARD,
	_classify_pattern)
from pathspec._backends.simple.gitignore import (
	SimpleGiBackend)
from pathspec._backends.simple.pathspec import (
	SimplePsBackend)
from pathspec.patterns.gitignore.basic import (
	GitIgnoreBasicPattern)
from pathspec.patterns.gitignore.spec import (
	GitIgnoreSpecPattern)


def _spec(lines, spec_cls=GitIgnoreSpec, optimized=True):
	"""
	Build a simple-backend spec. The unoptimized variant keeps no-op patterns and
	forward order, serving as the reference linear-scan implementation.
	"""
	if spec_cls is GitIgnoreSpec:
		if optimized:
			factory = None
		else:
			factory = partial(SimpleGiBackend, no_filter=True, no_reverse=True)
		return GitIgnoreSpec.from_lines(
			lines, backend='simple', _test_backend_factory=factory,
		)
	else:
		factory = (
			None if optimized
			else partial(SimplePsBackend, no_filter=True, no_reverse=True)
		)
		return PathSpec.from_lines(
			GitIgnoreBasicPattern, lines, backend='simple',
			_test_backend_factory=factory,
		)


class ClassifyPatternTest(TestCase):
	"""
	The :class:`ClassifyPatternTest` class tests raw-pattern classification.
	"""

	def test_literal_directory_prefixes(self):
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('src/foo/')),
			(_KIND_PREFIX, 'src/foo'),
		)
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('/build/out')),
			(_KIND_PREFIX, 'build/out'),
		)
		# Negation does not change the lookup key; it is still evaluated.
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('!src/foo/')),
			(_KIND_PREFIX, 'src/foo'),
		)
		# Backslashes escape literal glob characters.
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern(r'a/b\*.txt')),
			(0, 'a/b*.txt'),
		)

	def test_literal_basenames(self):
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('keep')),
			(_KIND_BASENAME, 'keep'),
		)
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('keep/')),
			(_KIND_BASENAME, 'keep'),
		)

	def test_literal_suffixes(self):
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('*.pyc')),
			(_KIND_SUFFIX, '.pyc'),
		)
		self.assertEqual(
			_classify_pattern(GitIgnoreSpecPattern('***.log')),
			(_KIND_SUFFIX, '.log'),
		)

	def test_wildcards_are_not_indexed(self):
		for line in (
			'**', '**/', '*', 'a/**/b', 'docs/**/draft/*', 'data-*.json',
			'foo?', 'file[0-9].dat', 'a/*/b', '**/temp/**',
		):
			kind, key = _classify_pattern(GitIgnoreSpecPattern(line))
			self.assertEqual(kind, _KIND_WILDCARD, line)
			self.assertIsNone(key, line)

	def test_generic_regex_pattern_is_wildcard(self):
		# A precompiled regular expression is unanchored and carries no glob
		# segment semantics, so it must always be evaluated.
		pattern = RegexPattern(re.compile(r'keep'), include=True)
		self.assertEqual(_classify_pattern(pattern), (_KIND_WILDCARD, None))

	def test_bytes_pattern_is_wildcard(self):
		pattern = GitIgnoreSpecPattern(b'build/')
		self.assertEqual(_classify_pattern(pattern), (_KIND_WILDCARD, None))


class CandidateCompletenessTest(TestCase):
	"""
	The :class:`CandidateCompletenessTest` class verifies the index never drops a
	rule whose regular expression would have matched.
	"""

	LINE_SET = [
		'build/', 'dist/', 'node_modules/', 'src/generated/',
		'*.pyc', '*.log', '*.tar.gz', 'keep', 'README*',
		'!keep.log', '!node_modules/keep/',
		'docs/**/draft/*', '**/temp/**', 'data-*.json', 'a/**/b/',
		'file[0-9].dat', 'foo?',
	]

	FILE_SET = [
		'build/out/x.pyc', 'node_modules/keep/log.js', 'src/generated/a.py',
		'keep.log', 'keep', 'docs/guide/draft/note.md', 'a/temp/b/file',
		'data-1.json', 'file5.dat', 'foox', 'src/app/main.log',
		'deep/nested/build/artifact', '',
	]

	def _candidate_indices(self, spec, file):
		index = spec._backend._index
		candidates, _checks = index.candidates(file)
		return {entry[0] for entry in candidates}

	def test_no_false_negatives(self):
		spec = _spec(self.LINE_SET)
		index = spec._backend._index
		for file in self.FILE_SET:
			candidate_ids = self._candidate_indices(spec, file)
			for pattern_index, pattern in enumerate(spec.patterns):
				if pattern.include is None or pattern.regex is None:
					continue
				if pattern.regex.search(file) is not None:
					self.assertIn(
						pattern_index, candidate_ids,
						(file, str(pattern.pattern)),
					)

	def test_ancestor_directory_rules_remain_candidates(self):
		# A directory-only rule is a candidate even when the query is a regular
		# file beneath that directory, because it excludes through an ancestor.
		spec = _spec(['build/'])
		self.assertIn(0, self._candidate_indices(spec, 'build/keep.txt'))


class ConstantScalingTest(TestCase):
	"""
	The :class:`ConstantScalingTest` class verifies that rules disjoint from the
	queried partition do not add per-file candidates.
	"""

	BASE = [
		'build/', 'src/generated/', '*.pyc', '*.log',
		'!keep.log', '**/temp/**', 'data-*.json',
	]

	PROBE_PATHS = [
		'src/app/main.pyc', 'build/out/lib.log', 'keep.log',
		'a/b/c.tmp', 'src/generated/x.py',
	]

	@staticmethod
	def _disjoint_rules(count):
		rules = []
		for i in range(count):
			rules.append(f'zz_part{i}/sub/deep/')
			rules.append(f'*.zz{i:04d}.ext')
		return rules

	def _candidate_total(self, spec, paths):
		index = spec._backend._index
		index.reset_counters()
		for path in paths:
			spec.match_file(path)
		return index.candidate_count

	def test_disjoint_rules_add_no_candidates(self):
		small = _spec(self.BASE)
		big = _spec(self.BASE + self._disjoint_rules(1000))

		self.assertEqual(
			self._candidate_total(small, self.PROBE_PATHS),
			self._candidate_total(big, self.PROBE_PATHS),
		)

		# The disjoint rules themselves remain reachable for their own paths.
		index = big._backend._index
		index.reset_counters()
		candidates, _ = index.candidates('zz_part7/sub/deep/x.txt')
		indices = {entry[0] for entry in candidates}
		self.assertIn(len(self.BASE) + 2 * 7, indices)

	def test_string_checks_bounded_by_path_size(self):
		# Cheap comparisons scale with the path's component/length, not rule count.
		small = _spec(self.BASE)
		big = _spec(self.BASE + self._disjoint_rules(1000))
		for path in self.PROBE_PATHS:
			small._backend._index.reset_counters()
			big._backend._index.reset_counters()
			small.match_file(path)
			big.match_file(path)
			self.assertLessEqual(
				big._backend._index.string_checks,
				small._backend._index.string_checks + len(path),
				path,
			)


class CounterSemanticsTest(TestCase):
	"""
	The :class:`CounterSemanticsTest` class distinguishes genuine index skips
	from merely making fewer regular-expression calls.
	"""

	def test_regex_calls_can_be_fewer_than_candidates(self):
		# The generic simple backend is reversed and stops at the first match.
		spec = _spec(
			['*.pyc', 'build/', '!build/keep.pyc', '*.log'],
			spec_cls=PathSpec,
		)
		index = spec._backend._index
		index.reset_counters()
		result = spec.check_file('build/keep.pyc')
		self.assertFalse(result.include)
		self.assertEqual(result.index, 2)
		self.assertGreater(index.candidate_count, index.regex_calls)

	def test_candidate_count_is_recomputable(self):
		spec = _spec([
			'build/', 'src/generated/', '*.pyc', '*.log', 'keep',
			'!keep.log', '**/temp/**',
		])
		index = spec._backend._index

		def recompute(file):
			trimmed = file[:-1] if file.endswith('/') else file
			components = trimmed.split('/') if trimmed else []
			total = len(index._wildcards)
			path = trimmed
			while path:
				total += len(index._prefixes.get(path, ()))
				slash = path.rfind('/')
				if slash == -1:
					break
				path = path[:slash]
			for component in components:
				if not component:
					continue
				total += len(index._basenames.get(component, ()))
				node = index._suffix_trie
				seen = set()
				for char in reversed(component):
					node = node.children.get(char)
					if node is None:
						break
					seen.update(entry[0] for entry in node.entries)
				total += len(seen)
			return total

		for file in ('build/keep.pyc', 'a/b/c.log', 'keep', 'x/keep.log/'):
			index.reset_counters()
			index.candidates(file)
			self.assertEqual(
				index.candidate_count, recompute(file), file,
			)

	def test_reset_counters(self):
		spec = _spec(['*.pyc'])
		index = spec._backend._index
		spec.match_file('a.pyc')
		self.assertEqual(index.candidate_calls, 1)
		index.reset_counters()
		self.assertEqual(
			(index.candidate_calls, index.candidate_count,
				index.regex_calls, index.string_checks),
			(0, 0, 0, 0),
		)


class DifferentialMatchTest(TestCase):
	"""
	The :class:`DifferentialMatchTest` class cross-checks decisions and rule
	indices between the indexed and unindexed simple backends, including wildcard
	negations and directory-only rules that change the final result.
	"""

	PATHS = [
		'a.txt', 'a.pyc', 'build/x.pyc', 'build/keep.pyc', 'keep.log',
		'src/generated/a.py', 'sub/b.py', 'd/child/grand/deep.txt',
		'a/b/file.txt', 'x/top.txt', 'nope.md', 'build/', 'sub/',
	]

	CASES = [
		['*.pyc', '!keep.log', 'build/'],
		['*', '!*/', '!*.py'],
		['a/', '!a/b/', '!*.txt'],
		['d/**/', '!d/child/'],
		['a/**', '!a/b/**'],
		['*', '!*.txt'],
		['build/', '!build/'],
		['**/temp/**', '!**/temp/keep'],
	]

	def test_gitignore_spec_matches_reference(self):
		for lines in self.CASES:
			optimized = _spec(lines, GitIgnoreSpec, optimized=True)
			reference = _spec(lines, GitIgnoreSpec, optimized=False)
			got = [
				(r.include, r.index)
				for r in optimized.check_files(iter(self.PATHS))
			]
			expected = [
				(r.include, r.index)
				for r in reference.check_files(iter(self.PATHS))
			]
			self.assertEqual(got, expected, lines)

	def test_pathspec_matches_reference(self):
		for lines in self.CASES:
			optimized = _spec(lines, PathSpec, optimized=True)
			reference = _spec(lines, PathSpec, optimized=False)
			got = [
				(r.include, r.index)
				for r in optimized.check_files(iter(self.PATHS))
			]
			expected = [
				(r.include, r.index)
				for r in reference.check_files(iter(self.PATHS))
			]
			self.assertEqual(got, expected, lines)

	def test_disjoint_rules_preserve_decisions(self):
		# A large set of unrelated prefix/suffix rules must not change any
		# include decision or the matched rule index.
		disjoint = [
			f'zz_dir{i}/deep/' for i in range(250)
		] + [
			f'*.zz{i:03d}.bin' for i in range(250)
		]
		for lines in self.CASES:
			plain = _spec(lines)
			expanded = _spec(lines + disjoint)
			got = [
				(r.include, r.index)
				for r in expanded.check_files(iter(self.PATHS))
			]
			expected = [
				(r.include, r.index)
				for r in plain.check_files(iter(self.PATHS))
			]
			self.assertEqual(got, expected, lines)
