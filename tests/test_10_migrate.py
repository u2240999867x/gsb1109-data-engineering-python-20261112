"""Test the migration planning and running utilities."""

import os.path
import pathlib
import tempfile
import unittest
from collections.abc import (
	Iterable)

from pathspec.gitignore import (
	GitIgnoreSpec)
from pathspec.migrate import (
	ManifestEntry,
	PartitionManifest,
	make_tree_scan,
	minimize_rescan_ranges,
	plan_migration,
	plan_partition,
	run_migration,
	run_partition)

from .util import (
	make_dirs,
	make_files)


def make_spec(lines: Iterable[str]) -> GitIgnoreSpec:
	"""
	Compile the gitignore pattern lines into a spec.
	"""
	return GitIgnoreSpec.from_lines(lines)


def simulate_historical_scan(
	tree: list[tuple[str, bool]],
	spec: GitIgnoreSpec,
) -> list[tuple[str, bool]]:
	"""
	Replay the historical scan: walk the tree, record every candidate, and
	prune directories excluded by a barrier (the old scanner never enumerated
	their subtrees).

	*tree* (:class:`list` of :class:`tuple`) contains each ``(path, is_dir)``
	with parents before children.

	*spec* (:class:`.GitIgnoreSpec`) is the old semantics.

	Returns the recorded candidates (:class:`list` of :class:`tuple`).
	"""
	seen = []
	pruned = []
	for path, is_dir in tree:
		if any(path.startswith(__prefix) for __prefix in pruned):
			continue

		seen.append((path, is_dir))
		if is_dir and spec.match_file(f"{path}/"):
			pruned.append(f"{path}/")

	return seen


def full_scan_selected(
	tree: list[tuple[str, bool]],
	spec: GitIgnoreSpec,
) -> set[str]:
	"""
	Match every object in the tree against the spec (a barrier-aware full
	scan): a matched directory is selected and stands for its whole subtree,
	so its descendants are not individually selected.
	"""
	selected = set()
	barriers = []
	for path, is_dir in sorted(tree):
		if any(path.startswith(__prefix) for __prefix in barriers):
			continue

		if spec.match_file(f"{path}/" if is_dir else path):
			selected.add(path)
			if is_dir:
				barriers.append(f"{path}/")

	return selected


def make_scan(
	tree: list[tuple[str, bool]],
	calls: list[str],
):
	"""
	Create a rescan callback over the in-memory tree which records the ranges
	it was called with.
	"""
	def scan(dir_prefix: str) -> Iterable[ManifestEntry]:
		calls.append(dir_prefix)
		for path, is_dir in tree:
			if not dir_prefix or path.startswith(dir_prefix):
				yield ManifestEntry(path, is_dir)

	return scan


TREE = [
	('a', True),
	('a/b', True),
	('a/b/keep.txt', False),
	('a/b/drop.log', False),
	('a/x.txt', False),
	('build', True),
	('build/out.txt', False),
	('build/nested', True),
	('build/nested/a.log', False),
	('src', True),
	('src/main.py', False),
	('src/debug.log', False),
	('top.txt', False),
]


class ManifestEntryTest(unittest.TestCase):

	def test_make(self):
		self.assertEqual(ManifestEntry.make('a/b'), ManifestEntry('a/b', False))
		self.assertEqual(ManifestEntry.make('a/b/'), ManifestEntry('a/b', True))
		self.assertEqual(ManifestEntry.make('a/b', is_dir=True), ManifestEntry('a/b', True))
		self.assertEqual(ManifestEntry.make('./a//b/'), ManifestEntry('a/b', True))


class MinimizeRescanRangesTest(unittest.TestCase):

	def test_ancestor_coverage(self):
		# Requests covered by an ancestor are removed.
		self.assertEqual(
			minimize_rescan_ranges(['a/b/', 'a/', 'c/d/', 'c/d/e/', 'x']),
			('a/', 'c/d/', 'x/'),
		)

	def test_root_covers_all(self):
		self.assertEqual(minimize_rescan_ranges(['a/', '']), ('',))
		self.assertEqual(minimize_rescan_ranges(['a/', '.']), ('',))

	def test_normalization(self):
		self.assertEqual(minimize_rescan_ranges(['./a/b', 'a/']), ('a/',))
		self.assertEqual(minimize_rescan_ranges([]), ())

	def test_sibling_not_covered(self):
		# "a/bc" is not below "a/b".
		self.assertEqual(minimize_rescan_ranges(['a/b', 'a/bc']), ('a/b/', 'a/bc/'))


class PlanPartitionTest(unittest.TestCase):

	def test_full_manifest_barrier_removed(self):
		# The old barrier "build/" pruned the subtree. The target semantics
		# drops the barrier, so the subtree is insufficient evidence.
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['*.log'])

		seen = simulate_historical_scan(TREE, old_spec)
		self.assertNotIn(('build/out.txt', False), seen)

		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)

		self.assertEqual(plan.partition, 'p1')
		self.assertEqual(plan.selected, ('a/b/drop.log', 'build', 'src/debug.log'))
		# The "build" directory object itself is withdrawn.
		self.assertEqual(plan.removals, ('build',))
		# Unseen leaves cannot be treated as existing: no additions.
		self.assertEqual(plan.additions, ())
		# The pruned subtree cannot be treated as empty: rescan it.
		self.assertEqual(plan.rescan, ('build/',))

	def test_full_manifest_barrier_retained(self):
		# The target semantics keeps the barrier, so the pruned subtree stays
		# excluded and no rescan is needed.
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['build/'])

		seen = simulate_historical_scan(TREE, old_spec)
		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)

		self.assertEqual(plan.removals, ('a/b/drop.log', 'src/debug.log'))
		self.assertEqual(plan.additions, ())
		self.assertEqual(plan.rescan, ())

	def test_full_manifest_negation(self):
		# File-level negations are decided from evidence without a rescan.
		old_spec = make_spec(['*.txt'])
		new_spec = make_spec(['*.txt', '!keep.txt'])

		tree = [
			('keep.txt', False),
			('a', True),
			('a/keep.txt', False),
			('a/drop.txt', False),
		]
		seen = simulate_historical_scan(tree, old_spec)
		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)

		self.assertEqual(plan.removals, ('a/keep.txt', 'keep.txt'))
		self.assertEqual(plan.additions, ())
		self.assertEqual(plan.rescan, ())

	def test_full_manifest_addition(self):
		# A recorded candidate excluded by the old semantics but selected by
		# the target semantics is an addition from evidence.
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['*.log', '*.txt'])

		seen = simulate_historical_scan(TREE, old_spec)
		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)

		self.assertEqual(plan.additions, ('a/b/keep.txt', 'a/x.txt', 'top.txt'))
		self.assertEqual(plan.removals, ('build',))
		# "build/" is still pruned evidence: "*.txt" could select beneath it.
		self.assertEqual(plan.rescan, ('build/',))

	def test_full_manifest_new_barrier(self):
		# The target semantics adds a barrier on a fully enumerated directory.
		# The barrier entry stands for its subtree, so the objects beneath it
		# are covered rather than individually added or withdrawn.
		old_spec = make_spec(['*.log'])
		new_spec = make_spec(['build/'])

		seen = simulate_historical_scan(TREE, old_spec)
		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)

		self.assertEqual(plan.additions, ('build',))
		# "build/nested/a.log" stays selected through the new barrier.
		self.assertEqual(plan.removals, ('a/b/drop.log', 'src/debug.log'))
		self.assertEqual(plan.rescan, ())

	def test_included_only_manifest(self):
		# Only the admitted list was recorded: every unlisted object is
		# unknown, so the whole partition is insufficient evidence.
		old_spec = make_spec(['*.log'])
		new_spec = make_spec(['*.txt'])

		seen = simulate_historical_scan(TREE, old_spec)
		listed = [
			path for path, is_dir in seen
			if old_spec.match_file(f"{path}/" if is_dir else path)
		]
		manifest = PartitionManifest.from_included('p1', listed)
		plan = plan_partition(manifest, old_spec, new_spec)

		self.assertEqual(
			plan.selected,
			('a/b/drop.log', 'build/nested/a.log', 'src/debug.log'),
		)
		self.assertEqual(
			plan.removals,
			('a/b/drop.log', 'build/nested/a.log', 'src/debug.log'),
		)
		# Unseen leaves cannot be treated as existing: no additions.
		self.assertEqual(plan.additions, ())
		self.assertEqual(plan.rescan, ('',))


class RunPartitionTest(unittest.TestCase):

	def test_run_aligns_with_full_scan(self):
		# Running the migration on the complete tree aligns with a full scan
		# under the target semantics.
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['*.log', '!debug.log'])

		seen = simulate_historical_scan(TREE, old_spec)
		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)
		self.assertEqual(plan.rescan, ('build/',))

		calls = []
		selected = run_partition(plan, new_spec, make_scan(TREE, calls))

		self.assertEqual(selected, full_scan_selected(TREE, new_spec))
		self.assertEqual(selected, {'a/b/drop.log', 'build/nested/a.log'})
		# Only the insufficient-evidence range was re-read.
		self.assertEqual(calls, ['build/'])

	def test_run_without_rescan(self):
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['build/'])

		seen = simulate_historical_scan(TREE, old_spec)
		manifest = PartitionManifest.from_candidates(
			'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)
		plan = plan_partition(manifest, old_spec, new_spec)
		self.assertEqual(plan.rescan, ())

		def fail_scan(dir_prefix: str) -> Iterable[ManifestEntry]:
			raise AssertionError(f"unexpected rescan of {dir_prefix!r}")

		selected = run_partition(plan, new_spec, fail_scan)
		self.assertEqual(selected, full_scan_selected(TREE, new_spec))
		self.assertEqual(selected, {'build'})

	def test_run_included_only_manifest(self):
		old_spec = make_spec(['*.log'])
		new_spec = make_spec(['*.txt'])

		seen = simulate_historical_scan(TREE, old_spec)
		listed = [
			path for path, is_dir in seen
			if old_spec.match_file(f"{path}/" if is_dir else path)
		]
		manifest = PartitionManifest.from_included('p1', listed)
		plan = plan_partition(manifest, old_spec, new_spec)

		calls = []
		selected = run_partition(plan, new_spec, make_scan(TREE, calls))

		self.assertEqual(selected, full_scan_selected(TREE, new_spec))
		self.assertEqual(selected, {'a/b/keep.txt', 'a/x.txt', 'build/out.txt', 'top.txt'})
		self.assertEqual(calls, [''])


class MigrationTest(unittest.TestCase):

	def test_plan_and_run_migration(self):
		# Partitions are planned independently; partitions outside the rescan
		# ranges are not re-read.
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['*.log'])

		seen = simulate_historical_scan(TREE, old_spec)
		full_manifest = PartitionManifest.from_candidates(
			'full', (ManifestEntry(path, is_dir) for path, is_dir in seen),
		)

		clean_tree = [
			('keep.txt', False),
			('drop.log', False),
		]
		clean_manifest = PartitionManifest.from_candidates(
			'clean', (ManifestEntry(path, is_dir) for path, is_dir in clean_tree),
		)

		plans = plan_migration([full_manifest, clean_manifest], old_spec, new_spec)
		self.assertEqual([__plan.partition for __plan in plans], ['full', 'clean'])
		self.assertEqual(plans[0].rescan, ('build/',))
		# The clean partition has final decisions everywhere: no re-read.
		self.assertEqual(plans[1].rescan, ())

		calls = []

		def scan(partition: str, dir_prefix: str) -> Iterable[ManifestEntry]:
			calls.append((partition, dir_prefix))
			tree = {'full': TREE, 'clean': clean_tree}[partition]
			for path, is_dir in tree:
				if not dir_prefix or path.startswith(dir_prefix):
					yield ManifestEntry(path, is_dir)

		results = run_migration(plans, new_spec, scan)

		self.assertEqual(results['full'], full_scan_selected(TREE, new_spec))
		self.assertEqual(results['clean'], full_scan_selected(clean_tree, new_spec))
		# Only the "full" partition's suspect range was re-read.
		self.assertEqual(calls, [('full', 'build/')])


class TreeScanTest(unittest.TestCase):

	def test_make_tree_scan(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			temp_path = pathlib.Path(temp_dir)
			make_dirs(temp_path, ['sub', 'sub/deep'])
			make_files(temp_path, ['top.txt', 'sub/a.log', 'sub/deep/b.log'])

			scan = make_tree_scan(temp_path)

			root_entries = set(scan(''))
			self.assertEqual(root_entries, {
				ManifestEntry('sub', True),
				ManifestEntry('sub/deep', True),
				ManifestEntry('top.txt', False),
				ManifestEntry('sub/a.log', False),
				ManifestEntry('sub/deep/b.log', False),
			})

			sub_entries = set(scan('sub/'))
			self.assertEqual(sub_entries, {
				ManifestEntry('sub/deep', True),
				ManifestEntry('sub/a.log', False),
				ManifestEntry('sub/deep/b.log', False),
			})

	def test_make_tree_scan_with_migration(self):
		# End-to-end on a real file-system tree: the migration result aligns
		# with a full scan under the target semantics.
		old_spec = make_spec(['build/', '*.log'])
		new_spec = make_spec(['*.log'])

		with tempfile.TemporaryDirectory() as temp_dir:
			temp_path = pathlib.Path(temp_dir)
			make_dirs(temp_path, ['build', 'src'])
			make_files(temp_path, [
				'build/out.txt',
				'build/a.log',
				'src/main.py',
				'src/debug.log',
				'top.txt',
			])

			tree = [
				(os.path.relpath(os.path.join(dirpath, name), temp_dir).replace(os.sep, '/'), is_dir)
				for dirpath, dirnames, filenames in os.walk(temp_dir)
				for name, is_dir in (
					[(d, True) for d in dirnames] + [(f, False) for f in filenames]
				)
			]
			tree.sort()

			seen = simulate_historical_scan(tree, old_spec)
			manifest = PartitionManifest.from_candidates(
				'p1', (ManifestEntry(path, is_dir) for path, is_dir in seen),
			)
			plan = plan_partition(manifest, old_spec, new_spec)
			self.assertEqual(plan.rescan, ('build/',))

			selected = run_partition(plan, new_spec, make_tree_scan(temp_path))
			self.assertEqual(selected, full_scan_selected(tree, new_spec))
			self.assertEqual(selected, {'build/a.log', 'src/debug.log'})
