"""
This script tests :class:`.GitIgnoreTreeSpec` and the manifest selection and
diff helpers.
"""

from unittest import (
	TestCase)

from pathspec.gitignore_tree import (
	GitIgnoreTreeSpec,
	TreeSelection,
	diff_selections)


class GitIgnoreTreeSpecTest(TestCase):
	"""
	The :class:`GitIgnoreTreeSpecTest` class tests combining rule files
	scattered across directory levels.
	"""

	def test_01_deeper_level_takes_precedence(self):
		"""
		Test that a rule from a deeper directory wins over a shallower one.
		"""
		spec = GitIgnoreTreeSpec.from_lines({
			'': ['*.log'],
			'sub/': ['!keep.log'],
		})

		result = spec.check_file('sub/keep.log')
		self.assertIs(result.include, False)
		self.assertEqual((result.origin.prefix, result.origin.index), ('sub/', 0))

		result = spec.check_file('sub/drop.log')
		self.assertIs(result.include, True)
		self.assertEqual((result.origin.prefix, result.origin.index), ('', 0))

		result = spec.check_file('top.log')
		self.assertIs(result.include, True)
		self.assertEqual((result.origin.prefix, result.origin.index), ('', 0))

	def test_02_exclusion_reports_ancestor_rule(self):
		"""
		Test that an exclusion caused by an ancestor directory reports the
		ancestor's rule file and line.
		"""
		spec = GitIgnoreTreeSpec.from_lines({
			'': ['build/'],
			'build/': ['!keep.txt'],
		})

		# The deep negation cannot apply because "build/" itself is excluded by
		# the root rule file.
		result = spec.check_file('build/keep.txt')
		self.assertIs(result.include, True)
		self.assertEqual(result.origin.prefix, '')
		self.assertEqual(result.origin.index, 0)
		self.assertEqual(result.origin.line, 1)
		self.assertEqual(result.origin.pattern.pattern, 'build/')

	def test_03_deep_negation_with_reachable_parent(self):
		"""
		Test that a deep negation applies while the parent directory is still
		reachable.
		"""
		spec = GitIgnoreTreeSpec.from_lines({
			'': ['build/', '!build/'],
			'build/': ['*.tmp', '!keep.tmp'],
		})

		result = spec.check_file('build/drop.tmp')
		self.assertIs(result.include, True)
		self.assertEqual((result.origin.prefix, result.origin.index), ('build/', 0))

		result = spec.check_file('build/keep.tmp')
		self.assertIs(result.include, False)
		self.assertEqual((result.origin.prefix, result.origin.index), ('build/', 1))

	def test_04_prefix_normalization(self):
		"""
		Test that level prefixes may be given with or without a trailing slash.
		"""
		spec = GitIgnoreTreeSpec.from_lines([
			('a/b', ['*.tmp']),
		])
		self.assertEqual([level.prefix for level in spec.levels], ['a/b/'])
		self.assertTrue(spec.match_file('a/b/c.tmp'))
		self.assertFalse(spec.match_file('a/b/c.txt'))

	def test_05_match_files_negate(self):
		"""
		Test matching files with negated results.
		"""
		spec = GitIgnoreTreeSpec.from_lines({'': ['*.log']})
		files = ['a.log', 'b.txt']
		self.assertEqual(list(spec.match_files(files)), ['a.log'])
		self.assertEqual(list(spec.match_files(files, negate=True)), ['b.txt'])


class ManifestSelectionTest(TestCase):
	"""
	The :class:`ManifestSelectionTest` class tests selecting partitions from a
	manifest.
	"""

	def test_01_dir_and_leaf_manifests_agree(self):
		"""
		Test that a directory listing and a leaf-only listing select the same
		leaf partitions.
		"""
		spec = GitIgnoreTreeSpec.from_lines({
			'': ['data/tmp/', '!data/keep/'],
		})

		dir_selection = spec.select(['data/', 'data/tmp/', 'data/keep/'])
		self.assertEqual(dir_selection.selected, ['data/', 'data/keep/'])
		self.assertEqual(
			[(r.file, r.origin.prefix, r.origin.index) for r in dir_selection.excluded],
			[('data/tmp/', '', 0)],
		)

		leaves = ['data/x.parquet', 'data/tmp/y.parquet', 'data/keep/z.parquet']
		leaf_selection = spec.select(leaves)
		self.assertEqual(leaf_selection.selected, [
			'data/x.parquet',
			'data/keep/z.parquet',
		])

		# The leaf below the excluded directory is excluded by the same rule
		# which excludes the directory listing.
		excluded, = leaf_selection.excluded
		self.assertEqual(excluded.file, 'data/tmp/y.parquet')
		self.assertEqual((excluded.origin.prefix, excluded.origin.index), ('', 0))

		# Every leaf takes the decision of its innermost listed directory.
		dirs = ['data/', 'data/tmp/', 'data/keep/']
		for leaf in leaves:
			innermost = max(d for d in dirs if leaf.startswith(d))
			with self.subTest(leaf=leaf, innermost=innermost):
				if innermost in dir_selection.selected:
					self.assertIn(leaf, leaf_selection.selected)
				else:
					self.assertNotIn(leaf, leaf_selection.selected)

	def test_02_dir_and_object_with_same_name(self):
		"""
		Test that a directory and a same-named data object are told apart by
		the manifest entry, not by probing the file-system.
		"""
		spec = GitIgnoreTreeSpec.from_lines({'': ['data/']})

		selection = spec.select(['data', 'data/'])
		self.assertEqual(selection.selected, ['data'])
		self.assertEqual([r.file for r in selection.excluded], ['data/'])

	def test_03_selection_protocol(self):
		"""
		Test the selection container protocol.
		"""
		spec = GitIgnoreTreeSpec.from_lines({'': ['*.log']})
		selection = spec.select(['a.log', 'b.txt'])
		self.assertIsInstance(selection, TreeSelection)
		self.assertEqual(len(selection), 1)
		self.assertIn('b.txt', selection)
		self.assertNotIn('a.log', selection)
		self.assertEqual(list(selection), ['b.txt'])


class SelectionDiffTest(TestCase):
	"""
	The :class:`SelectionDiffTest` class tests diffing two selections.
	"""

	def test_01_added_and_withdrawn_keep_causes(self):
		"""
		Test that publishing the next rule version yields the added and
		withdrawn partitions with the rule responsible for each change.
		"""
		manifest = ['a.parquet', 'keep.parquet', 'readme.md']
		old_spec = GitIgnoreTreeSpec.from_lines({'': [
			'*.parquet',
		]})
		new_spec = GitIgnoreTreeSpec.from_lines({'': [
			'*.parquet',
			'!keep.parquet',
			'readme.md',
		]})

		old_selection = old_spec.select(manifest)
		new_selection = new_spec.select(manifest)
		self.assertEqual(old_selection.selected, ['readme.md'])
		self.assertEqual(new_selection.selected, ['keep.parquet'])

		diff = diff_selections(old_selection, new_selection)

		self.assertEqual([r.file for r in diff.added], ['keep.parquet'])
		self.assertIs(diff.added[0].include, False)
		self.assertEqual(
			(diff.added[0].origin.prefix, diff.added[0].origin.index),
			('', 1),
		)

		self.assertEqual([r.file for r in diff.withdrawn], ['readme.md'])
		self.assertIs(diff.withdrawn[0].include, True)
		self.assertEqual(
			(diff.withdrawn[0].origin.prefix, diff.withdrawn[0].origin.index),
			('', 2),
		)

	def test_02_manifest_entry_removed(self):
		"""
		Test that a partition dropped from the manifest is reported as
		withdrawn with its previous cause.
		"""
		old_spec = GitIgnoreTreeSpec.from_lines({'': ['*.tmp']})
		new_spec = GitIgnoreTreeSpec.from_lines({'': ['*.tmp']})

		old_selection = old_spec.select(['a.txt', 'b.txt'])
		new_selection = new_spec.select(['a.txt'])

		diff = diff_selections(old_selection, new_selection)
		self.assertEqual([r.file for r in diff.added], [])
		self.assertEqual([r.file for r in diff.withdrawn], ['b.txt'])
		self.assertIsNone(diff.withdrawn[0].origin)
