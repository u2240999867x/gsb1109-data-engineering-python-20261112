"""
Sorting of version registries and paged merging of sorted registries.

Version strings that are valid semantic versions are ordered by SemVer
precedence; their build metadata is retained verbatim and never takes
part in ordering. Values that do not parse as semantic versions are not
rejected - they form a separate natural-sort zone that always follows
the semantic versions, ordered with the unchanged natsort natural key.

When several records share the same precedence, the tie is broken by the
global sequence number assigned when the record first entered the
registry. The paged merge cursor records the full global position of the
last emitted record, so reading can continue after the registry is split
into a different set of partitions without skipping or repeating a row,
even when the page boundary lands between two builds of identical
precedence. Per-partition offsets are retained only as a fast forward
hint for an unchanged partition layout.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar, cast

from natsort.natsort import natsort_keygen
from natsort.ns_enum import NSType, ns
from natsort.semver import SemVer, parse_semver

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

T = TypeVar("T")

# A callable that extracts the candidate version text from a value.
VersionKeyType = Callable[[Any], str | None]

# Zone markers keep semantic versions and ordinary labels in separate,
# never directly compared, key spaces.
_SEMVER_ZONE = 0
_PLAIN_ZONE = 1


@dataclass(frozen=True)
class VersionRecord(Generic[T]):
    """
    A single row of a version registry.

    Attributes
    ----------
    value : object
        The original registry entry.
    semver : SemVer | None
        The parsed semantic version, or `None` when *value* does not
        carry a parseable semantic version. Such records form the
        natural-sort zone.
    build : str | None
        The build metadata preserved exactly as it appeared, including
        the case of a semantic version without build metadata (which is
        `None`). It is never used for ordering.
    order : int
        The global sequence number assigned at first ingestion. It is
        the sole tie breaker between records of equal precedence.

    See Also
    --------
    version_records

    """

    value: T
    semver: SemVer | None
    build: str | None
    order: int

    @property
    def is_semver(self) -> bool:
        """`True` when the record belongs to the semantic-version zone."""
        return self.semver is not None


@dataclass(frozen=True)
class PageCursor:
    """
    An immutable position inside a paged merge of sorted registries.

    Attributes
    ----------
    offsets : tuple[int, ...]
        One offset per partition, each pointing at the next row that had
        not been inspected under the layout that produced this cursor.
        These are only a fast-forward hint for an unchanged layout; when
        a later call sees a different partition count they are ignored.
    last_key : tuple | None
        The full global order key (zone key plus ingestion sequence
        number) of the last emitted record, or `None` before the first
        page. This is the correctness anchor: on resume every record with
        a key less than or equal to *last_key* is skipped, which keeps
        continuation exact even if the partitions were re-split.

    See Also
    --------
    merge_sorted_versions

    """

    offsets: tuple[int, ...] = ()
    last_key: tuple[Any, ...] | None = None

    @classmethod
    def start(cls, num_partitions: int) -> PageCursor:
        """Return a cursor positioned before the first row of every partition."""
        if num_partitions < 0:
            msg = "num_partitions must be non-negative"
            raise ValueError(msg)
        return cls(tuple(0 for _ in range(num_partitions)), None)


@dataclass(frozen=True)
class VersionPage(Generic[T]):
    """
    One page emitted by :func:`merge_sorted_versions`.

    Attributes
    ----------
    values : list
        The merged registry entries, in global order.
    records : list[VersionRecord]
        The records backing *values*, including preserved build metadata
        and ingestion sequence numbers.
    cursor : PageCursor
        The cursor to pass back to :func:`merge_sorted_versions` to read
        the next page.
    finished : bool
        `True` when every partition has been consumed.

    """

    values: list[T]
    records: list[VersionRecord[T]]
    cursor: PageCursor
    finished: bool


def _extract_version_text(
    value: Any,  # noqa: ANN401
    version: VersionKeyType | None,
) -> str | None:
    """Pull the candidate version text from *value* and coerce to str."""
    text = value if version is None else version(value)
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    return text


def _make_record(
    value: Any,  # noqa: ANN401
    version: VersionKeyType | None,
    counter: Iterator[int],
    *,
    strict: bool,
) -> VersionRecord[Any]:
    """Build a :class:`VersionRecord`, parsing the version when possible."""
    text = _extract_version_text(value, version)
    semver = parse_semver(text, strict=strict) if text is not None else None
    build = semver.build if semver is not None else None
    return VersionRecord[Any](value, semver, build, next(counter))


def version_records(
    seq: Iterable[T],
    version: VersionKeyType | None = None,
    *,
    strict: bool = False,
    start: int = 0,
) -> list[VersionRecord[T]]:
    """
    Materialize an iterable of registry entries into version records.

    A global sequence number is assigned in iteration order starting at
    *start*, matching the order of first ingestion.

    Parameters
    ----------
    seq : iterable
        The registry entries.
    version : callable, optional
        A callable returning the candidate version string for an entry.
        When omitted, the entry itself is used. A `None` result, or a
        string that is not a semantic version, places the entry in the
        natural-sort zone.
    strict : bool, optional
        When `True`, only strict SemVer 2.0.0 strings enter the
        semantic-version zone; shortened cores and a leading ``v`` fall
        back to the natural-sort zone. The default is `False`.
    start : int, optional
        First sequence number to hand out. The default is zero.

    Returns
    -------
    list[VersionRecord]
        The records in ingestion order.

    Examples
    --------
        >>> records = version_records(["1.0.0+build.2", "1.0.0+build.1"])
        >>> [record.order for record in records]
        [0, 1]
        >>> records[0].build
        'build.2'

    """
    counter = itertools.count(start)
    return [_make_record(value, version, counter, strict=strict) for value in seq]


def _zone_sort_key(
    record: VersionRecord[T],
    plain_natural_key: Callable[[Any], Any],
    version: VersionKeyType | None,
) -> tuple[Any, ...]:
    """
    Return the order-bearing part of a record's sort key.

    Build metadata is deliberately absent: equal precedence stays equal
    regardless of build, and the ingestion order resolves the tie. The
    natural zone keeps the historical natsort natural-key semantics. When
    a *version* callable yields no label for a plain record, the whole
    value is natural sorted instead of collapsing onto an empty key.
    """
    if record.semver is not None:
        return (_SEMVER_ZONE, record.semver.precedence_key())
    label = _extract_version_text(record.value, version)
    natural_target = record.value if label is None else label
    return (_PLAIN_ZONE, plain_natural_key(natural_target))


def _full_sort_key(
    record: VersionRecord[T],
    plain_natural_key: Callable[[Any], Any],
    version: VersionKeyType | None,
) -> tuple[tuple[Any, ...], int]:
    """Return the globally unique order key (zone key, ingestion order)."""
    return (_zone_sort_key(record, plain_natural_key, version), record.order)


def version_sort_keygen(
    version: VersionKeyType | None = None,
    *,
    alg: NSType = ns.DEFAULT,
    strict: bool = False,
    start: int = 0,
) -> Callable[[Any], tuple[Any, ...]]:
    """
    Generate a key function that orders entries as version registry rows.

    The returned key is suitable for the builtin :func:`sorted`. Each
    call consumes one global sequence number, in call order, which is
    used as the tie breaker for equal precedence.

    Parameters
    ----------
    version : callable, optional
        A callable returning the candidate version string for an entry.
        When omitted, the entry itself supplies the version.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone. The semantic-version
        zone is unaffected. The default is `ns.DEFAULT`, preserving the
        historical natural key semantics.
    strict : bool, optional
        Require strict SemVer 2.0.0 strings. The default is `False`.
    start : int, optional
        First ingestion sequence number. The default is zero.

    Returns
    -------
    callable
        A key function mapping an entry to its composite sort key.

    See Also
    --------
    versionsorted
    version_records

    Examples
    --------
        >>> key = version_sort_keygen()
        >>> sorted(["10.0.0", "2.0.0", "legacy-10", "legacy-2"], key=key)
        ['2.0.0', '10.0.0', 'legacy-2', 'legacy-10']

    """
    natural_key = natsort_keygen(alg=alg)
    counter = itertools.count(start)

    def key_func(value: Any) -> tuple[Any, ...]:  # noqa: ANN401
        record = _make_record(value, version, counter, strict=strict)
        return (*_zone_sort_key(record, natural_key, version), record.order)

    return key_func


def sorted_version_records(
    records: Iterable[VersionRecord[T]],
    version: VersionKeyType | None = None,
    alg: NSType = ns.DEFAULT,
) -> list[VersionRecord[T]]:
    """
    Sort ready made version records by precedence and ingestion order.

    This is the record-level counterpart of :func:`versionsorted`. It is
    intended for partitioned registries: records can be created once
    with global sequence numbers, split across partitions, sorted
    independently, and later merged with :func:`merge_sorted_versions`.

    Parameters
    ----------
    records : iterable of VersionRecord
        The records to sort.
    version : callable, optional
        Callable extracting version text from a record value; only used
        by the natural-sort zone.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone.

    Returns
    -------
    list[VersionRecord]
        A new list of records in sorted order.

    See Also
    --------
    version_records
    merge_sorted_versions

    """
    natural_key = natsort_keygen(alg=alg)
    return sorted(
        records,
        key=lambda record: (_zone_sort_key(record, natural_key, version), record.order),
    )


def versionsorted(  # noqa: PLR0913
    seq: Iterable[T],
    version: VersionKeyType | None = None,
    *,
    reverse: bool = False,
    alg: NSType = ns.DEFAULT,
    strict: bool = False,
    start: int = 0,
) -> list[T]:
    """
    Sort registry entries with SemVer precedence plus a natural-sort zone.

    Semantic versions are ordered per SemVer 2.0.0; build metadata is
    preserved on the records but ignored while ordering. Entries without
    a parseable semantic version follow as a separate zone sorted with
    the standard natsort natural key. Equal precedence is resolved by
    the global ingestion sequence number.

    Parameters
    ----------
    seq : iterable
        The registry entries to sort.
    version : callable, optional
        A callable returning the candidate version string for an entry.
        When omitted, the entry itself supplies the version.
    reverse : bool, optional
        Return the entries in reversed order. The default is `False`.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone. The default is
        `ns.DEFAULT`.
    strict : bool, optional
        Require strict SemVer 2.0.0 strings. The default is `False`.
    start : int, optional
        First ingestion sequence number. The default is zero.

    Returns
    -------
    list
        The sorted entries.

    See Also
    --------
    version_records
    version_sort_keygen
    index_versionsorted

    Examples
    --------
        >>> versionsorted(["1.0.0+build.2", "1.0.0-alpha", "1.0.0+build.1"])
        ['1.0.0-alpha', '1.0.0+build.2', '1.0.0+build.1']
        >>> versionsorted(["2.0.0", "node-10", "1.0.0", "node-2"])
        ['1.0.0', '2.0.0', 'node-2', 'node-10']

    """
    records = version_records(seq, version, strict=strict, start=start)
    records = sorted_version_records(records, version, alg)
    if reverse:
        records.reverse()
    return [record.value for record in records]


def index_versionsorted(  # noqa: PLR0913
    seq: Iterable[T],
    version: VersionKeyType | None = None,
    *,
    reverse: bool = False,
    alg: NSType = ns.DEFAULT,
    strict: bool = False,
    start: int = 0,
) -> list[int]:
    """
    Return the indexes that would sort *seq* as a version registry.

    Parameters mirror :func:`versionsorted`. The result can be fed to
    :func:`order_by_index` to reorder several parallel registries.

    Parameters
    ----------
    seq : iterable
        The registry entries.
    version : callable, optional
        A callable returning the candidate version string for an entry.
    reverse : bool, optional
        Return indexes for reversed order. The default is `False`.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone.
    strict : bool, optional
        Require strict SemVer 2.0.0 strings. The default is `False`.
    start : int, optional
        First ingestion sequence number. The default is zero.

    Returns
    -------
    list[int]
        The indexes of *seq* in version-sorted order.

    See Also
    --------
    versionsorted
    order_by_index

    Examples
    --------
        >>> index_versionsorted(["2.0.0", "1.0.0"])
        [1, 0]

    """
    indexed = list(enumerate(version_records(seq, version, strict=strict, start=start)))
    natural_key = natsort_keygen(alg=alg)
    indexed.sort(
        key=lambda pair: (_zone_sort_key(pair[1], natural_key, version), pair[1].order),
        reverse=reverse,
    )
    return [index for index, _ in indexed]


def _materialize_partitions(
    partitions: Iterable[Iterable[Any]],
    version: VersionKeyType | None,
    start: int,
    *,
    strict: bool,
) -> list[list[VersionRecord[Any]]]:
    """Turn each partition into a record list, preserving prebuilt records."""
    materialized: list[list[VersionRecord[Any]]] = []
    counter = itertools.count(start)
    for partition in partitions:
        rows = list(partition)
        if rows and isinstance(rows[0], VersionRecord):
            materialized.append(rows)
        else:
            materialized.append(
                [_make_record(value, version, counter, strict=strict) for value in rows]
            )
    return materialized


def _first_unread_offset(
    partition: list[VersionRecord[Any]],
    hint: int,
    last_key: tuple[Any, ...] | None,
    key_fn: Callable[[VersionRecord[Any]], tuple[tuple[Any, ...], int]],
) -> int:
    """Binary search the first record whose key is after *last_key*."""
    if last_key is None:
        return hint
    lo, hi = hint, len(partition)
    while lo < hi:
        mid = (lo + hi) // 2
        if key_fn(partition[mid]) <= last_key:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _resolve_offsets(
    cursor: PageCursor | tuple[int, ...] | None,
    record_partitions: list[list[VersionRecord[Any]]],
    key_fn: Callable[[VersionRecord[Any]], tuple[tuple[Any, ...], int]],
) -> tuple[list[int], tuple[Any, ...] | None]:
    """
    Resolve the first unread offset of every partition.

    The cursor's global *last_key* is the source of truth: every record
    whose full key is less than or equal to it is treated as already
    emitted. The per-partition offsets only provide a starting hint and
    are ignored (rather than rejected) when the partition layout changed.
    """
    if cursor is None:
        return [0 for _ in record_partitions], None
    if isinstance(cursor, PageCursor):
        hint = cursor.offsets
        last_key = cursor.last_key
    else:
        hint = cursor
        last_key = None

    layout_matches = len(hint) == len(record_partitions)
    offsets: list[int] = []
    for index, partition in enumerate(record_partitions):
        start = hint[index] if layout_matches else 0
        start = max(0, min(start, len(partition)))
        offsets.append(
            _first_unread_offset(partition, start, last_key, key_fn),
        )
    return offsets, last_key


def _paged_merge(
    record_partitions: list[list[VersionRecord[Any]]],
    page_size: int | None,
    cursor: PageCursor | tuple[int, ...] | None,
    natural_key: Callable[[Any], Any],
    version: VersionKeyType | None,
) -> VersionPage[Any]:
    """Run one k-way merge pass over already materialized record partitions."""
    lengths = [len(partition) for partition in record_partitions]

    def key_fn(
        record: VersionRecord[Any],
    ) -> tuple[tuple[Any, ...], int]:
        return _full_sort_key(record, natural_key, version)

    offsets, last_key = _resolve_offsets(cursor, record_partitions, key_fn)

    # Heap entries are (full order key, partition index). The ingestion
    # order inside the full key makes every entry globally unique, so
    # records themselves never need to be compared.
    heap: list[tuple[tuple[Any, ...], int]] = []
    for index, partition in enumerate(record_partitions):
        if offsets[index] < lengths[index]:
            record = partition[offsets[index]]
            heapq.heappush(heap, (key_fn(record), index))

    page_records: list[VersionRecord[Any]] = []
    while heap and (page_size is None or len(page_records) < page_size):
        _, index = heapq.heappop(heap)
        record = record_partitions[index][offsets[index]]
        page_records.append(record)
        last_key = key_fn(record)
        offsets[index] += 1
        next_offset = offsets[index]
        if next_offset < lengths[index]:
            next_record = record_partitions[index][next_offset]
            heapq.heappush(heap, (key_fn(next_record), index))

    next_cursor = PageCursor(tuple(offsets), last_key)
    finished = all(offset == length for offset, length in zip(offsets, lengths))
    return VersionPage[Any](
        values=[record.value for record in page_records],
        records=page_records,
        cursor=next_cursor,
        finished=finished,
    )


def merge_sorted_versions(  # noqa: PLR0913
    partitions: Iterable[Iterable[T | VersionRecord[T]]],
    page_size: int | None = None,
    cursor: PageCursor | tuple[int, ...] | None = None,
    *,
    version: VersionKeyType | None = None,
    alg: NSType = ns.DEFAULT,
    strict: bool = False,
    start: int = 0,
) -> VersionPage[T]:
    """
    Merge already sorted version registries, one page at a time.

    Every partition must already be sorted in ascending version order
    (for example with :func:`versionsorted`). The returned
    :class:`PageCursor` records the global position of the last emitted
    record, so a page boundary that falls between two builds of equal
    precedence resumes without dropping or repeating a row - even if the
    next call passes a different partition layout (for example a
    re-split of the same registry). Per-partition offsets are kept only
    as a fast-forward hint when the layout is unchanged.

    Parameters
    ----------
    partitions : iterable of iterables
        The sorted registries. Entries may be raw values (in which case
        *version*, *strict* and *start* govern record creation) or ready
        made :class:`VersionRecord` objects carrying global sequence
        numbers.
    page_size : int | None, optional
        Maximum number of entries per page. `None` emits every remaining
        entry in a single page. The default is `None`.
    cursor : PageCursor | tuple[int, ...] | None, optional
        A cursor returned by a previous call, or a tuple of per
        partition offsets. `None` starts before the first row. A cursor
        produced for a different partition count still resumes correctly
        via its global position anchor.
    version : callable, optional
        Callable extracting the version text from a raw entry.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone.
    strict : bool, optional
        Require strict SemVer 2.0.0 strings. The default is `False`.
    start : int, optional
        First ingestion sequence number when building records from raw
        entries. The default is zero.

    Returns
    -------
    VersionPage
        The page values, backing records, next cursor, and completion
        flag.

    Raises
    ------
    ValueError
        If *page_size* is not a positive integer or `None`.

    Notes
    -----
    Exact continuation across a changed partition layout relies on the
    global ingestion sequence number embedded in the anchor. To re-split
    a registry between pages, therefore, build the :class:`VersionRecord`
    objects once with :func:`version_records`, partition those records,
    and pass the same record objects on every call. Passing raw values
    re-assigns sequence numbers on each call, so it is only safe when the
    partition layout stays fixed (the common streaming case).

    See Also
    --------
    iter_merged_versions
    versionsorted
    PageCursor

    Examples
    --------
        >>> left = ["1.0.0", "3.0.0"]
        >>> right = ["2.0.0"]
        >>> page = merge_sorted_versions([left, right], page_size=2)
        >>> page.values
        ['1.0.0', '2.0.0']
        >>> page.finished
        False
        >>> next_page = merge_sorted_versions([left, right], 2, page.cursor)
        >>> next_page.values
        ['3.0.0']
        >>> next_page.finished
        True

    """
    if page_size is not None and (
        isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0
    ):
        msg = "page_size must be a positive integer or None"
        raise ValueError(msg)

    record_partitions = _materialize_partitions(
        partitions, version, start, strict=strict
    )
    natural_key = natsort_keygen(alg=alg)
    page = _paged_merge(record_partitions, page_size, cursor, natural_key, version)
    return cast("VersionPage[T]", page)


def iter_merged_versions(  # noqa: PLR0913
    partitions: Iterable[Iterable[T | VersionRecord[T]]],
    page_size: int,
    *,
    version: VersionKeyType | None = None,
    alg: NSType = ns.DEFAULT,
    strict: bool = False,
    start: int = 0,
) -> Iterator[VersionPage[T]]:
    """
    Yield successive pages from :func:`merge_sorted_versions` until done.

    The partitions are materialized once and paged internally, so the
    caller does not have to thread the cursor through each call.

    Parameters
    ----------
    partitions : iterable of iterables
        The sorted registries, as accepted by
        :func:`merge_sorted_versions`.
    page_size : int
        Maximum number of entries per page.
    version : callable, optional
        Callable extracting the version text from a raw entry.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone.
    strict : bool, optional
        Require strict SemVer 2.0.0 strings. The default is `False`.
    start : int, optional
        First ingestion sequence number for raw entries.

    Yields
    ------
    VersionPage
        Each page until all partitions are consumed.

    See Also
    --------
    merge_sorted_versions

    """
    if not isinstance(page_size, int) or page_size <= 0:
        msg = "page_size must be a positive integer"
        raise ValueError(msg)

    record_partitions = _materialize_partitions(
        partitions, version, start, strict=strict
    )
    natural_key = natsort_keygen(alg=alg)
    cursor: PageCursor | None = None
    while True:
        page = _paged_merge(record_partitions, page_size, cursor, natural_key, version)
        yield cast("VersionPage[T]", page)
        if page.finished:
            return
        cursor = page.cursor


def merge_versions(
    partitions: Iterable[Iterable[T | VersionRecord[T]]],
    *,
    version: VersionKeyType | None = None,
    alg: NSType = ns.DEFAULT,
    strict: bool = False,
    start: int = 0,
) -> list[T]:
    """
    Merge already sorted registries into a single ordered list.

    Equivalent to reading :func:`merge_sorted_versions` without a page
    size.

    Parameters
    ----------
    partitions : iterable of iterables
        The sorted registries.
    version : callable, optional
        Callable extracting the version text from a raw entry.
    alg : ns enum, optional
        Algorithm flags for the natural-sort zone.
    strict : bool, optional
        Require strict SemVer 2.0.0 strings. The default is `False`.
    start : int, optional
        First ingestion sequence number for raw entries.

    Returns
    -------
    list
        The merged entries in global version order.

    See Also
    --------
    merge_sorted_versions

    """
    return merge_sorted_versions(
        partitions,
        None,
        None,
        version=version,
        alg=alg,
        strict=strict,
        start=start,
    ).values
