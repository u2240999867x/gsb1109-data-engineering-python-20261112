"""
This module provides the simple backend for :class:`~pathspec.pathspec.PathSpec`.

WARNING: The *pathspec._backends.simple* package is not part of the public API.
Its contents and structure are likely to change.
"""

from collections.abc import (
	Sequence)
from typing import (
	Optional)  # Replaced by `X | None` in 3.10.

from pathspec.backend import (
	_Backend)
from pathspec.pattern import (
	Pattern)
from pathspec._typing import (
	override)  # Added in 3.12.
from pathspec.util import (
	check_match_file)

from .._utils import (
	enumerate_patterns)
from ._index import (
	LiteralIndex)


class SimplePsBackend(_Backend):
	"""
	The :class:`SimplePsBackend` class is the default (or simple) implementation
	used by :class:`~pathspec.pathspec.PathSpec` for matching files.
	"""

	def __init__(
		self,
		patterns: Sequence[Pattern],
		*,
		no_filter: Optional[bool] = None,
		no_reverse: Optional[bool] = None,
	) -> None:
		"""
		Initialize the :class:`SimplePsBackend` instance.

		*patterns* (:class:`Sequence` of :class:`.Pattern`) contains the compiled
		patterns.

		*no_filter* (:class:`bool`) is whether to keep no-op patterns (:data:`True`),
		or remove them (:data:`False`).

		*no_reverse* (:class:`bool`) is whether to keep the pattern order
		(:data:`True`), or reverse the order (:data:`True`).
		"""

		self._is_reversed: bool = not no_reverse
		"""
		*_is_reversed* (:class:`bool`) is whether to the pattern order was reversed.
		"""

		self._patterns: list[tuple[int, Pattern]] = enumerate_patterns(
			patterns, filter=not no_filter, reverse=not no_reverse,
		)
		"""
		*_patterns* (:class:`list` of :class:`tuple`) contains the enumerated
		patterns.
		"""

		# The literal index is an exact, narrowing optimization over the linear
		# scan. It is disabled on the explicit "no optimization" configuration so
		# that forward/unfiltered evaluation stays available as a reference
		# implementation (the test suite cross-checks the two against each other).
		self._index_enabled: bool = not (no_filter or no_reverse)
		"""
		*_index_enabled* (:class:`bool`) is whether the literal index narrows the
		patterns evaluated for each file.
		"""

		if self._index_enabled:
			self._index: Optional[LiteralIndex] = LiteralIndex(self._patterns)
		else:
			self._index = None

	@override
	def match_file(self, file: str) -> tuple[Optional[bool], Optional[int]]:
		"""
		Check the file against the patterns.

		*file* (:class:`str`) is the normalized file path to check.

		Returns a :class:`tuple` containing whether to include *file* (:class:`bool`
		or :data:`None`), and the index of the last matched pattern (:class:`int` or
		:data:`None`).
		"""
		index = self._index
		if index is None or not self._is_reversed:
			return check_match_file(self._patterns, file, self._is_reversed)

		# The index returns candidates in ascending spec order; this backend
		# evaluates highest-index-first so the first match wins.
		candidates, _checks = index.candidates(file)
		regex_calls = 0
		for pattern_index, pattern in reversed(candidates):
			regex_calls += 1
			if pattern.include is not None and pattern.match_file(file) is not None:
				index.note_regex_calls(regex_calls)
				return pattern.include, pattern_index

		index.note_regex_calls(regex_calls)
		return None, None
