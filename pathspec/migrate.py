"""
This module provides the migration entry point for reconciling historical
selection manifests against a new path-spec.

Historical manifests were produced by scanning a tree with an old path-spec.
The scanner was barrier-aware: a directory matched by the spec (a directory
barrier) was recorded, but its subtree was never enumerated. Negations in the
old rules decided whether a directory matched, and therefore whether its
subtree was listed. Two kinds of manifests exist:

-	"Full" manifests recorded every candidate object the scanner saw along with
	whether each object was a directory. The old selection decisions are
	reconstructed by replaying the candidates with the current compatible
	semantics (*old_spec*).

-	"Included-only" manifests recorded just the selected objects (the admitted
	list). Nothing is known about the objects that were not listed.

A recorded barrier directory can hide an unenumerated subtree. An unseen leaf
must not be treated as existing, and a pruned directory must not be treated as
empty. Such subtrees are reported as rescan ranges (insufficient evidence)
instead of being guessed at, unless the target semantics still matches the
directory, in which case the barrier entry keeps standing for its whole
subtree.

For each partition, :func:`plan_partition` (or :func:`plan_migration`) reports:

-	*additions*: recorded objects not selected under the old semantics which
	are selected under the target semantics.

-	*removals*: recorded objects selected under the old semantics which are no
	longer selected under the target semantics.

-	*rescan*: directory ranges with insufficient evidence. Ranges already
	covered by an ancestor range are removed. Every known object outside these
	ranges has a final decision, so it does not need to be re-read.

:func:`run_partition` (or :func:`run_migration`) applies a plan: known objects
outside the rescan ranges are decided directly, and only the rescan ranges are
re-enumerated through a caller supplied scan. Running a plan against a
complete tree yields the same selected set as a barrier-aware full scan with
the target semantics.

.. note::
	The barrier reasoning assumes gitignore semantics: a matched directory
	stands for its whole subtree, as implemented by
	:class:`~pathspec.gitignore.GitIgnoreSpec`.
"""
from __future__ import annotations

from collections.abc import (
	Callable,
	Iterable)
from dataclasses import (
	dataclass)
from typing import (
	Optional,  # Replaced by `X | None` in 3.10.
	Union)  # Replaced by `X | Y` in 3.10.

from .pathspec import (
	PathSpec)
from .util import (
	StrPath,
	iter_tree_entries,
	normalize_file)

__all__ = [
	'ManifestEntry',
	'MigrationScanFn',
	'PartitionManifest',
	'PartitionPlan',
	'ScanFn',
	'make_tree_scan',
	'minimize_rescan_ranges',
	'plan_migration',
	'plan_partition',
	'run_migration',
	'run_partition',
]

ScanFn = Callable[[str], Iterable['ManifestEntry']]
"""
Type hint for the rescan callback. It is called with a rescan range (:class:`str`;
``''`` for the partition root, otherwise a directory path ending with ``'/'``)
and yields each entry (:class:`ManifestEntry`) strictly below that directory,
with paths relative to the partition root.
"""

MigrationScanFn = Callable[[str, str], Iterable['ManifestEntry']]
"""
Type hint for the rescan callback used by :func:`run_migration`. It is called
with a partition name and a rescan range (:class:`str`), and yields each entry
(:class:`ManifestEntry`) strictly below that directory, with paths relative to
the partition root.
"""


def _match_entry(
	spec: PathSpec,
	entry: 'ManifestEntry',
) -> bool:
	"""
	Match the entry against the path-spec. Directories are matched with a
	trailing path separator.

	*spec* (:class:`.PathSpec`) is the path-spec to match with.

	*entry* (:class:`ManifestEntry`) is the entry to match.

	Returns whether the entry is selected (:class:`bool`).
	"""
	path = entry.path
	if entry.is_dir:
		path += '/'

	return spec.match_file(path)


def _iter_parent_dirs(path: str) -> Iterable[str]:
	"""
	Yield each strict ancestor directory of the path with a trailing slash.

	*path* (:class:`str`) is the normalized relative path.

	Yields each ancestor directory prefix (:class:`str`).
	"""
	index = path.find('/')
	while index != -1:
		yield path[:index + 1]
		index = path.find('/', index + 1)


def _is_covered(
	path: str,
	ranges: Iterable[str],
) -> bool:
	"""
	Check whether the path is strictly below any of the rescan ranges. The
	root range (``''``) covers every path. A range covers the strict
	descendants of its directory, not the directory itself.

	*path* (:class:`str`) is the normalized relative path (no trailing slash).

	*ranges* (:class:`~collections.abc.Iterable` of :class:`str`) contains the
	rescan ranges.

	Returns whether the path is covered (:class:`bool`).
	"""
	return any(not prefix or path.startswith(prefix) for prefix in ranges)


def _select_entries(
	entries: Iterable['ManifestEntry'],
	spec: PathSpec,
) -> tuple[set[str], set[str]]:
	"""
	Replay the entries against the path-spec with barrier semantics: a matched
	directory is selected and stands for its whole subtree, so its descendants
	are covered rather than individually selected.

	*entries* (:class:`~collections.abc.Iterable` of :class:`ManifestEntry`)
	contains the entries to replay. They do not need to be sorted.

	*spec* (:class:`.PathSpec`) is the path-spec to match with.

	Returns a :class:`tuple` containing:

	-	The explicitly selected paths (:class:`set` of :class:`str`): matched
		entries outside of any barrier.

	-	The effectively selected paths (:class:`set` of :class:`str`): the
		explicitly selected paths plus the entries covered by a barrier.
	"""
	explicit = set()
	effective = set()
	barriers = []
	for entry in sorted(entries, key=lambda __ent: __ent.path):
		if any(entry.path.startswith(__prefix) for __prefix in barriers):
			# Covered by a barrier directory.
			effective.add(entry.path)
		elif _match_entry(spec, entry):
			explicit.add(entry.path)
			effective.add(entry.path)
			if entry.is_dir:
				barriers.append(f"{entry.path}/")

	return explicit, effective


@dataclass(frozen=True)
class ManifestEntry:
	"""
	The :class:`ManifestEntry` class is a single object recorded in a
	historical manifest.
	"""

	# Make the class dict-less.
	__slots__ = (
		'is_dir',
		'path',
	)

	path: str
	"""
	*path* (:class:`str`) is the normalized path relative to the partition
	root, without a trailing slash.
	"""

	is_dir: bool
	"""
	*is_dir* (:class:`bool`) is whether the object is a directory.
	"""

	@staticmethod
	def make(
		path: StrPath,
		is_dir: Optional[bool] = None,
	) -> 'ManifestEntry':
		"""
		Create an entry from a path, normalizing it. A trailing path separator
		marks a directory when *is_dir* is not given.

		*path* (:class:`str` or :class:`os.PathLike`) is the path.

		*is_dir* (:class:`bool` or :data:`None`) is whether the object is a
		directory. Default is :data:`None` to detect a trailing separator, and
		otherwise assume a file.

		Returns the entry (:class:`ManifestEntry`).
		"""
		norm_path = normalize_file(path)
		if is_dir is None:
			is_dir = norm_path.endswith('/')

		return ManifestEntry(norm_path.rstrip('/'), is_dir)


@dataclass(frozen=True)
class PartitionManifest:
	"""
	The :class:`PartitionManifest` class is the historical manifest of one
	partition.
	"""

	# Make the class dict-less.
	__slots__ = (
		'entries',
		'full',
		'partition',
	)

	partition: str
	"""
	*partition* (:class:`str`) is the name of the partition.
	"""

	full: bool
	"""
	*full* (:class:`bool`) is whether the manifest recorded every candidate
	object with its directory type (:data:`True`), or only the selected
	objects (:data:`False`).
	"""

	entries: tuple[ManifestEntry, ...]
	"""
	*entries* (:class:`tuple` of :class:`ManifestEntry`) contains the recorded
	objects: every candidate for a full manifest, or just the selected objects
	for an included-only manifest.
	"""

	@staticmethod
	def from_candidates(
		partition: str,
		entries: Iterable[Union[ManifestEntry, StrPath]],
	) -> 'PartitionManifest':
		"""
		Create a full manifest from every candidate object the old scanner
		enumerated.

		*partition* (:class:`str`) is the name of the partition.

		*entries* (:class:`~collections.abc.Iterable`) yields each candidate
		(:class:`ManifestEntry`; or a path which is assumed to be a file unless
		it ends with a separator).

		Returns the manifest (:class:`PartitionManifest`).
		"""
		return PartitionManifest(partition, True, tuple(
			__ent if isinstance(__ent, ManifestEntry) else ManifestEntry.make(__ent)
			for __ent in entries
		))

	@staticmethod
	def from_included(
		partition: str,
		paths: Iterable[Union[ManifestEntry, StrPath]],
	) -> 'PartitionManifest':
		"""
		Create an included-only manifest from the selected objects.

		*partition* (:class:`str`) is the name of the partition.

		*paths* (:class:`~collections.abc.Iterable`) yields each selected object
		(:class:`ManifestEntry`; or a path which is assumed to be a file unless
		it ends with a separator).

		Returns the manifest (:class:`PartitionManifest`).
		"""
		return PartitionManifest(partition, False, tuple(
			__ent if isinstance(__ent, ManifestEntry) else ManifestEntry.make(__ent)
			for __ent in paths
		))


@dataclass(frozen=True)
class PartitionPlan:
	"""
	The :class:`PartitionPlan` class is the migration plan for one partition:
	what to add, what to withdraw, and where the evidence is insufficient.
	"""

	# Make the class dict-less.
	__slots__ = (
		'additions',
		'entries',
		'partition',
		'removals',
		'rescan',
		'selected',
	)

	partition: str
	"""
	*partition* (:class:`str`) is the name of the partition.
	"""

	selected: tuple[str, ...]
	"""
	*selected* (:class:`tuple` of :class:`str`) contains the sorted paths of
	the old selected set (the admitted list), reconstructed for full
	manifests.
	"""

	additions: tuple[str, ...]
	"""
	*additions* (:class:`tuple` of :class:`str`) contains the sorted paths of
	known objects to collect under the target semantics.
	"""

	removals: tuple[str, ...]
	"""
	*removals* (:class:`tuple` of :class:`str`) contains the sorted paths of
	known objects to withdraw under the target semantics.
	"""

	rescan: tuple[str, ...]
	"""
	*rescan* (:class:`tuple` of :class:`str`) contains the sorted directory
	ranges with insufficient evidence (``''`` for the whole partition,
	otherwise ending with ``'/'``). Ranges covered by an ancestor range are
	removed. Known objects outside these ranges have final decisions and do
	not need to be re-read.
	"""

	entries: tuple[ManifestEntry, ...]
	"""
	*entries* (:class:`tuple` of :class:`ManifestEntry`) contains the known
	objects the plan was computed from.
	"""


def minimize_rescan_ranges(
	ranges: Iterable[StrPath],
) -> tuple[str, ...]:
	"""
	Normalize the rescan ranges and remove each range already covered by an
	ancestor range.

	*ranges* (:class:`~collections.abc.Iterable`) contains the requested
	ranges. The root is ``''`` (or ``'.'``) and covers everything; other
	ranges are directory paths with or without a trailing slash.

	Returns the minimal ranges (:class:`tuple` of :class:`str`): ``''`` for
	the root, otherwise directory paths ending with ``'/'``, sorted.
	"""
	norm_ranges = set()
	for range_ in ranges:
		norm_range = normalize_file(range_).strip('/')
		if norm_range == '.':
			# The current directory is the root range.
			norm_range = ''

		norm_ranges.add(norm_range)

	if '' in norm_ranges:
		# The root range covers every other range.
		return ('',)

	out_ranges = []
	for path in sorted(norm_ranges):
		if not any(path.startswith(__anc + '/') for __anc in out_ranges):
			out_ranges.append(path)

	return tuple(f"{__path}/" for __path in out_ranges)


def plan_partition(
	manifest: PartitionManifest,
	old_spec: PathSpec,
	new_spec: PathSpec,
) -> PartitionPlan:
	"""
	Compute the migration plan for one partition by comparing the current
	compatible semantics against the target semantics.

	*manifest* (:class:`PartitionManifest`) is the historical manifest of the
	partition.

	*old_spec* (:class:`.PathSpec`) is the current compatible semantics the
	manifest was produced with.

	*new_spec* (:class:`.PathSpec`) is the target semantics to migrate to.

	Returns the plan (:class:`PartitionPlan`).
	"""
	entries = manifest.entries

	if manifest.full:
		# Reconstruct the old selected set from the recorded candidates.
		old_explicit, old_effective = _select_entries(entries, old_spec)
		new_explicit, new_effective = _select_entries(entries, new_spec)

		# Find the directories whose subtrees were never enumerated. The old
		# scanner pruned directories matched by a barrier, so a barrier
		# directory without recorded children may hide an unseen subtree. It
		# cannot be treated as empty either.
		parent_dirs = set()
		for entry in entries:
			parent_dirs.update(_iter_parent_dirs(entry.path))

		unresolved = []
		for entry in entries:
			if (
				entry.is_dir
				and entry.path in old_explicit
				and f"{entry.path}/" not in parent_dirs
				and not _match_entry(new_spec, entry)
			):
				# The subtree was pruned by the old barrier, and the target
				# semantics no longer matches the directory, so anything
				# beneath it is unknown. When the target semantics still
				# matches the directory, the barrier entry keeps standing for
				# its whole subtree and no rescan is needed.
				unresolved.append(entry.path)

	else:
		# Only the selected objects were recorded. Unlisted objects cannot be
		# treated as existing, so additions stay empty regardless of the target
		# semantics, and every unlisted object is unknown: the whole partition
		# is suspect.
		old_explicit = {__ent.path for __ent in entries}
		old_effective = old_explicit
		new_explicit = set()
		new_effective = _select_entries(entries, new_spec)[1]
		unresolved = ['']

	additions = new_explicit - old_effective
	removals = old_explicit - new_effective

	return PartitionPlan(
		partition=manifest.partition,
		selected=tuple(sorted(old_explicit)),
		additions=tuple(sorted(additions)),
		removals=tuple(sorted(removals)),
		rescan=minimize_rescan_ranges(unresolved),
		entries=entries,
	)


def plan_migration(
	manifests: Iterable[PartitionManifest],
	old_spec: PathSpec,
	new_spec: PathSpec,
) -> list[PartitionPlan]:
	"""
	Compute the migration plan for each partition.

	*manifests* (:class:`~collections.abc.Iterable` of :class:`PartitionManifest`)
	contains the historical manifest of each partition.

	*old_spec* (:class:`.PathSpec`) is the current compatible semantics the
	manifests were produced with.

	*new_spec* (:class:`.PathSpec`) is the target semantics to migrate to.

	Returns the plan for each partition (:class:`list` of
	:class:`PartitionPlan`). Partitions with an empty
	:attr:`~PartitionPlan.rescan` do not need to be re-read.
	"""
	return [
		plan_partition(__manifest, old_spec, new_spec)
		for __manifest in manifests
	]


def run_partition(
	plan: PartitionPlan,
	new_spec: PathSpec,
	scan: ScanFn,
) -> set[str]:
	"""
	Run the migration plan for one partition. Known objects outside the rescan
	ranges are decided directly from the plan evidence; only the rescan ranges
	are re-enumerated through *scan*. Decisions are barrier-aware: a directory
	matched by *new_spec* is selected and stands for its whole subtree.

	*plan* (:class:`PartitionPlan`) is the migration plan.

	*new_spec* (:class:`.PathSpec`) is the target semantics to migrate to.

	*scan* (:data:`ScanFn`) is called with each rescan range and yields each
	entry (:class:`ManifestEntry`) strictly below that directory.

	Returns the selected paths under the target semantics (:class:`set` of
	:class:`str`).
	"""
	selected = set()
	barriers = []

	def select_entries(entries: Iterable[ManifestEntry]) -> None:
		for entry in sorted(entries, key=lambda __ent: __ent.path):
			if any(entry.path.startswith(__prefix) for __prefix in barriers):
				# Covered by a barrier directory.
				continue

			if _match_entry(new_spec, entry):
				selected.add(entry.path)
				if entry.is_dir:
					barriers.append(f"{entry.path}/")

	# Known objects outside the rescan ranges have final decisions.
	known_entries = [
		__ent for __ent in plan.entries
		if not _is_covered(__ent.path, plan.rescan)
	]
	select_entries(known_entries)

	# Re-enumerate only the ranges with insufficient evidence.
	for prefix in plan.rescan:
		select_entries(scan(prefix))

	return selected


def run_migration(
	plans: Iterable[PartitionPlan],
	new_spec: PathSpec,
	scan: MigrationScanFn,
) -> dict[str, set[str]]:
	"""
	Run the migration plan for each partition.

	*plans* (:class:`~collections.abc.Iterable` of :class:`PartitionPlan`)
	contains the migration plan of each partition.

	*new_spec* (:class:`.PathSpec`) is the target semantics to migrate to.

	*scan* (:data:`MigrationScanFn`) is called with each partition name and
	rescan range, and yields each entry (:class:`ManifestEntry`) strictly
	below that directory. It is not called for partitions with an empty
	:attr:`~PartitionPlan.rescan`; those partitions do not need to be
	re-read.

	Returns the selected paths of each partition (:class:`dict` mapping
	:class:`str` to :class:`set` of :class:`str`).
	"""
	out_selected = {}
	for plan in plans:
		out_selected[plan.partition] = run_partition(
			plan,
			new_spec,
			lambda __prefix, _partition=plan.partition: scan(_partition, __prefix),
		)

	return out_selected


def make_tree_scan(
	root: StrPath,
	on_error: Optional[Callable[[OSError], None]] = None,
	follow_links: Optional[bool] = None,
) -> ScanFn:
	"""
	Create a rescan callback which walks the file-system tree at *root*.

	*root* (:class:`str` or :class:`os.PathLike`) is the root directory of the
	partition.

	*on_error* (:class:`~collections.abc.Callable` or :data:`None`) optionally
	is the error handler for file-system exceptions. See
	:func:`.util.iter_tree_entries`.

	*follow_links* (:class:`bool` or :data:`None`) optionally is whether to
	walk symbolic links that resolve to directories. Default is :data:`None`
	for :data:`True`.

	Returns the scan callback (:data:`ScanFn`).
	"""
	def scan(dir_prefix: str) -> Iterable[ManifestEntry]:
		subdir = dir_prefix.rstrip('/') or None
		entries = iter_tree_entries(
			root, on_error=on_error, follow_links=follow_links, subdir=subdir,
		)
		for entry in entries:
			yield ManifestEntry.make(entry.path, entry.is_dir())

	return scan
