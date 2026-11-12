"""
This module provides :class:`.GitIgnoreTreeSpec` which combines *gitignore*
rule files scattered across directory levels (e.g., nested *.gitignore* files)
into a single spec. Every decision is reported with the rule responsible for
it: which ancestor directory's rule file and which line. This makes it suited
for selecting data-lake partitions from a manifest without probing the
file-system: manifest entries ending with a slash are treated as directories,
anything else as a leaf object.
"""
from __future__ import annotations

from collections.abc import (
	Collection,
	Iterable,
	Iterator,
	Mapping,
	Sequence)
from dataclasses import (
	dataclass)
from typing import (
	Generic,
	Optional,
	TypeVar,
	Union)

from pathspec.backend import (
	BackendNamesHint,
	_TestBackendFactoryHint)
from pathspec.gitignore import (
	GitIgnoreSpec)
from pathspec.patterns.gitignore.spec import (
	GitIgnoreSpecPattern)
from pathspec._typing import (
	AnyStr)  # Removed in 3.18.
from pathspec.util import (
	TStrPath,
	_is_iterable,
	normalize_file)

Self = TypeVar("Self", bound='GitIgnoreTreeSpec')
"""
:class:`.GitIgnoreTreeSpec` self type hint to support Python v<3.11 using PEP
673 recommendation.
"""


@dataclass(frozen=True)
class GitIgnoreLevel:
	"""
	The :class:`GitIgnoreLevel` class pairs a directory prefix with the
	:class:`.GitIgnoreSpec` compiled from the rule file found at that directory.
	"""

	# Make the class dict-less.
	__slots__ = (
		'prefix',
		'spec',
	)

	prefix: str
	"""
	*prefix* (:class:`str`) is the directory the rule file applies to, relative
	to the tree root. It is empty (``""``) for the root, and otherwise ends with
	a slash (e.g., ``"a/b/"``).
	"""

	spec: GitIgnoreSpec
	"""
	*spec* (:class:`.GitIgnoreSpec`) is the spec compiled from the rule file at
	:attr:`prefix <GitIgnoreLevel.prefix>`. Its patterns are relative to the
	prefix directory.
	"""


@dataclass(frozen=True)
class RuleOrigin:
	"""
	The :class:`RuleOrigin` class identifies the rule responsible for a
	decision: the rule file (by its ancestor directory) and the line within
	that file.
	"""

	# Make the class dict-less.
	__slots__ = (
		'prefix',
		'index',
		'pattern',
	)

	prefix: str
	"""
	*prefix* (:class:`str`) is the directory of the rule file containing the
	rule, relative to the tree root. It is empty (``""``) for the root rule
	file, and otherwise ends with a slash.
	"""

	index: int
	"""
	*index* (:class:`int`) is the 0-based index of the rule within the rule
	file at :attr:`prefix <RuleOrigin.prefix>`.
	"""

	pattern: GitIgnoreSpecPattern
	"""
	*pattern* (:class:`.GitIgnoreSpecPattern`) is the compiled rule.
	"""

	@property
	def line(self) -> int:
		"""
		*line* (:class:`int`) is the 1-based line number of the rule within the
		rule file at :attr:`prefix <RuleOrigin.prefix>`.
		"""
		return self.index + 1


@dataclass(frozen=True)
class TreeCheckResult(Generic[TStrPath]):
	"""
	The :class:`TreeCheckResult` class contains information about the checked
	entry and the rule which decided it.
	"""

	# Make the class dict-less.
	__slots__ = (
		'file',
		'include',
		'origin',
	)

	file: TStrPath
	"""
	*file* (:class:`str` or :class:`os.PathLike`) is the entry path as passed
	in.
	"""

	include: Optional[bool]
	"""
	*include* (:class:`bool` or :data:`None`) is whether the entry matched the
	rules. If :data:`None`, no rule matched. Following the *gitignore*
	convention, :data:`True` means the entry is ignored (excluded), and
	:data:`False` means a negation re-included it.
	"""

	origin: Optional[RuleOrigin]
	"""
	*origin* (:class:`RuleOrigin` or :data:`None`) is the rule responsible for
	the decision. If :data:`None`, no rule matched.
	"""


class GitIgnoreTreeSpec(object):
	"""
	The :class:`GitIgnoreTreeSpec` class combines the *gitignore* rule files
	found at each directory level of a tree into a single spec. Rules from a
	deeper directory take precedence over rules from a shallower one, and a
	negation can only re-include a path while every ancestor directory is still
	reachable (not excluded), matching how Git stops descending at the first
	excluded directory.
	"""

	def __init__(
		self,
		levels: Iterable[GitIgnoreLevel],
	) -> None:
		"""
		Initializes the :class:`GitIgnoreTreeSpec` instance.

		*levels* (:class:`~collections.abc.Iterable` of :class:`GitIgnoreLevel`)
		contains the rule file of each directory level.
		"""
		# Store the levels deepest first so the first decisive rule wins.
		self.levels: Sequence[GitIgnoreLevel] = sorted(
			levels,
			key=lambda level: level.prefix.count('/'),
			reverse=True,
		)
		"""
		*levels* (:class:`~collections.abc.Sequence` of :class:`GitIgnoreLevel`)
		contains the rule file of each directory level, ordered deepest first.
		"""

	def __repr__(self) -> str:
		"""
		Returns a debug representation of this tree-spec.
		"""
		return f"{self.__class__.__name__}(levels={self.levels!r})"

	def __len__(self) -> int:
		"""
		Returns the number of directory levels (:class:`int`).
		"""
		return len(self.levels)

	@classmethod
	def from_lines(
		cls: type[Self],
		levels: Union[Mapping[str, Iterable[AnyStr]], Iterable[tuple[str, Iterable[AnyStr]]]],
		*,
		backend: Union[BackendNamesHint, str, None] = None,
		_test_backend_factory: _TestBackendFactoryHint = None,
	) -> Self:
		"""
		Compiles the rule files scattered across directory levels.

		*levels* maps each directory prefix (:class:`str`) to the lines
		(:class:`~collections.abc.Iterable` of :class:`str`) of the rule file at
		that directory. It can be a :class:`~collections.abc.Mapping` (e.g., a
		:class:`dict`), or an iterable of ``(prefix, lines)`` pairs. The prefix
		is relative to the tree root: use ``""`` for the root rule file; a
		trailing slash on the prefix is optional. The lines of each level are
		relative to that level's directory, as with nested *.gitignore* files.

		*backend* (:class:`str` or :data:`None`) is the pattern (regular
		expression) matching backend to use. Default is :data:`None` for "best"
		to use the best available backend. Priority of backends is: "re2",
		"hyperscan", "simple". The "simple" backend is always available.

		Returns the :class:`GitIgnoreTreeSpec` instance.
		"""
		if isinstance(levels, Mapping):
			level_items = levels.items()
		elif _is_iterable(levels):
			level_items = levels
		else:
			raise TypeError(f"levels:{levels!r} is not a mapping or iterable.")

		gi_levels = []
		for prefix, lines in level_items:
			norm_prefix = prefix.strip('/')
			if norm_prefix:
				norm_prefix += '/'

			spec = GitIgnoreSpec.from_lines(
				lines,
				backend=backend,
				_test_backend_factory=_test_backend_factory,
			)
			gi_levels.append(GitIgnoreLevel(norm_prefix, spec))

		return cls(gi_levels)

	def check_file(
		self,
		file: TStrPath,
		separators: Optional[Collection[str]] = None,
	) -> TreeCheckResult[TStrPath]:
		"""
		Check the entry against this tree-spec.

		*file* (:class:`str` or :class:`os.PathLike`) is the entry path to be
		checked. A trailing slash marks a directory; whether the entry exists
		on the file-system is never probed.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		Returns the entry check result (:class:`TreeCheckResult`).
		"""
		norm_file = normalize_file(file, separators)
		include, origin = self._match(norm_file)
		return TreeCheckResult(file, include, origin)

	def check_files(
		self,
		files: Iterable[TStrPath],
		separators: Optional[Collection[str]] = None,
	) -> Iterator[TreeCheckResult[TStrPath]]:
		"""
		Check the entries against this tree-spec.

		*files* (:class:`~collections.abc.Iterable` of :class:`str` or
		:class:`os.PathLike`) contains the entry paths to be checked.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		Returns an :class:`~collections.abc.Iterator` yielding each entry check
		result (:class:`TreeCheckResult`).
		"""
		if not _is_iterable(files):
			raise TypeError(f"files:{files!r} is not an iterable.")

		for orig_file in files:
			yield self.check_file(orig_file, separators)

	def match_file(
		self,
		file: TStrPath,
		separators: Optional[Collection[str]] = None,
	) -> bool:
		"""
		Matches the entry to this tree-spec.

		*file* (:class:`str` or :class:`os.PathLike`) is the entry path to be
		matched. A trailing slash marks a directory.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		Returns :data:`True` if *file* matched the rules (i.e., is excluded);
		otherwise, :data:`False`.
		"""
		return bool(self.check_file(file, separators).include)

	def match_files(
		self,
		files: Iterable[TStrPath],
		separators: Optional[Collection[str]] = None,
		*,
		negate: Optional[bool] = None,
	) -> Iterator[TStrPath]:
		"""
		Matches the entries to this tree-spec.

		*files* (:class:`~collections.abc.Iterable` of :class:`str` or
		:class:`os.PathLike`) contains the entry paths to be matched.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		*negate* (:class:`bool` or :data:`None`) is whether to negate the match
		results of the rules. If :data:`True`, a rule matching an entry will
		exclude the entry rather than include it. Default is :data:`None` for
		:data:`False`.

		Returns the matched entries (:class:`~collections.abc.Iterator` of
		:class:`str` or :class:`os.PathLike`).
		"""
		for result in self.check_files(files, separators):
			include = result.include
			if negate:
				include = not include

			if include:
				yield result.file

	def select(
		self,
		entries: Iterable[TStrPath],
		separators: Optional[Collection[str]] = None,
	) -> 'TreeSelection[TStrPath]':
		"""
		Select the ingestible entries from a partition manifest.

		*entries* (:class:`~collections.abc.Iterable` of :class:`str` or
		:class:`os.PathLike`) is the partition manifest. An entry ending with a
		slash is a directory (selecting everything beneath it); any other entry
		is a leaf object. The two forms are told apart by the entry itself, never
		by probing the file-system, so a directory listing and a leaf-only
		listing of the same partitions select the same leaves.

		*separators* (:class:`~collections.abc.Collection` of :class:`str`; or
		:data:`None`) optionally contains the path separators to normalize. See
		:func:`.normalize_file` for more information.

		Returns the selection (:class:`TreeSelection`).
		"""
		return TreeSelection(self.check_files(entries, separators))

	def _match(
		self,
		norm_file: str,
	) -> tuple[Optional[bool], Optional[RuleOrigin]]:
		"""
		Match the normalized entry path against the levels.

		*norm_file* (:class:`str`) is the normalized entry path.

		Returns a :class:`tuple` containing whether the entry matched
		(:class:`bool` or :data:`None`), and the responsible rule
		(:class:`RuleOrigin` or :data:`None`).
		"""
		# Git stops descending at the first excluded directory, so ask the
		# ancestors outermost first. A negation below an excluded ancestor can
		# never re-include the entry.
		index = norm_file.find('/')
		while index != -1 and index + 1 < len(norm_file):
			include, origin = self._match_self(norm_file[:index + 1])
			if include:
				return (True, origin)

			index = norm_file.find('/', index + 1)

		return self._match_self(norm_file)

	def _match_self(
		self,
		norm_file: str,
	) -> tuple[Optional[bool], Optional[RuleOrigin]]:
		"""
		Match the normalized entry path against the levels applicable to the
		path itself, deepest first.

		*norm_file* (:class:`str`) is the normalized entry path.

		Returns a :class:`tuple` containing whether the entry matched
		(:class:`bool` or :data:`None`), and the responsible rule
		(:class:`RuleOrigin` or :data:`None`).
		"""
		# The rule file in a directory applies to everything below the
		# directory, but not to the directory itself.
		for level in self.levels:
			prefix = level.prefix
			if prefix != norm_file and norm_file.startswith(prefix):
				rel_file = norm_file[len(prefix):]
				result = level.spec.check_file(rel_file)
				if result.include is not None:
					assert result.index is not None, result
					origin = RuleOrigin(prefix, result.index, level.spec.patterns[result.index])
					return (result.include, origin)

		return (None, None)


class TreeSelection(Generic[TStrPath]):
	"""
	The :class:`TreeSelection` class contains the result of selecting the
	ingestible entries from a partition manifest with
	:meth:`.GitIgnoreTreeSpec.select`.
	"""

	def __init__(
		self,
		results: Iterable[TreeCheckResult[TStrPath]],
	) -> None:
		"""
		Initializes the :class:`TreeSelection` instance.

		*results* (:class:`~collections.abc.Iterable` of :class:`TreeCheckResult`)
		contains the check result of every manifest entry.
		"""
		self.results: list[TreeCheckResult[TStrPath]] = list(results)
		"""
		*results* (:class:`list` of :class:`TreeCheckResult`) contains the check
		result of every manifest entry, in manifest order.
		"""

	def __contains__(self, file: TStrPath) -> bool:
		"""
		Returns whether *file* is a selected entry (:class:`bool`).
		"""
		return file in self.selected

	def __iter__(self) -> Iterator[TStrPath]:
		"""
		Iterates over the selected entries.
		"""
		return iter(self.selected)

	def __len__(self) -> int:
		"""
		Returns the number of selected entries (:class:`int`).
		"""
		return len(self.selected)

	def __repr__(self) -> str:
		"""
		Returns a debug representation of this selection.
		"""
		return f"{self.__class__.__name__}(selected={self.selected!r})"

	@property
	def selected(self) -> list[TStrPath]:
		"""
		*selected* (:class:`list` of :class:`str` or :class:`os.PathLike`) is
		the ingestible entries: every manifest entry no rule excluded. Directory
		entries keep their trailing slash and stand for every leaf beneath them.
		"""
		return [result.file for result in self.results if not result.include]

	@property
	def excluded(self) -> list[TreeCheckResult[TStrPath]]:
		"""
		*excluded* (:class:`list` of :class:`TreeCheckResult`) is the check
		result of every excluded manifest entry, each carrying the rule
		responsible for the exclusion.
		"""
		return [result for result in self.results if result.include]


@dataclass(frozen=True)
class SelectionDiff(Generic[TStrPath]):
	"""
	The :class:`SelectionDiff` class contains the partitions added and
	withdrawn between two selections, each with the rule responsible for its
	current status.
	"""

	# Make the class dict-less.
	__slots__ = (
		'added',
		'withdrawn',
	)

	added: list[TreeCheckResult[TStrPath]]
	"""
	*added* (:class:`list` of :class:`TreeCheckResult`) is the check result of
	every entry selected by the new selection but not by the old one. The
	:attr:`~TreeCheckResult.origin` is the rule which now keeps the entry
	(e.g., a negation), or :data:`None` when no rule touches it.
	"""

	withdrawn: list[TreeCheckResult[TStrPath]]
	"""
	*withdrawn* (:class:`list` of :class:`TreeCheckResult`) is the check result
	of every entry selected by the old selection but not by the new one. The
	:attr:`~TreeCheckResult.origin` is the rule which now excludes the entry,
	or the old result when the entry no longer appears in the new manifest.
	"""


def diff_selections(
	old: TreeSelection[TStrPath],
	new: TreeSelection[TStrPath],
) -> SelectionDiff[TStrPath]:
	"""
	Diff two selections (e.g., the same manifest checked against the previous
	and the next version of the rules) to find the added and withdrawn
	partitions, keeping the rule responsible for each change.

	*old* (:class:`TreeSelection`) is the previous selection.

	*new* (:class:`TreeSelection`) is the next selection.

	Returns the diff (:class:`SelectionDiff`).
	"""
	old_selected = {result.file for result in old.results if not result.include}
	new_selected = {result.file for result in new.results if not result.include}
	new_by_file = {result.file: result for result in new.results}

	added = [
		result
		for result in new.results
		if not result.include and result.file not in old_selected
	]
	withdrawn = [
		new_by_file.get(result.file, result)
		for result in old.results
		if not result.include and result.file not in new_selected
	]
	return SelectionDiff(added, withdrawn)
