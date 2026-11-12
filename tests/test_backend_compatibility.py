"""\
Executable compatibility criteria for the optional ``fastnumbers`` backend.

The same batch of natural keys can be produced on two workers, one with the
``fastnumbers`` accelerator installed and one without it (the pure-python
:mod:`natsort.compat.fake_fastnumbers` fallback).  Merging the two outputs is
only safe for equality grouping when both backends agree on:

1. key equality  - distinct spellings of the same number must share a key,
2. strict order  - every pair is ordered the same way and the relation stays
   transitive,
3. stable rows   - equal keys keep their input order, so a two-worker merge
   never splits an equality group.

This module makes those three criteria executable.  The controlled corpus is
built around triples whose comparison relation changes, covering oversized
integers (the CPython 4300 digit conversion cap), unicode numeric runs, and
the boundary where those runs touch ordinary text.  When a divergence is
injected it is reduced to the smallest fragment that still splits a group (or
breaks transitivity).  When the accelerator is not installed locally, every
test states explicitly which path is exercised.
"""

from __future__ import annotations

import heapq
import itertools
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from natsort import natsort_keygen, ns
from natsort.compat import fastnumbers as fastnumbers_compat
from natsort.compat.fastnumbers import try_float, try_int

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from typing import Any, Callable

    from natsort.ns_enum import NSType

    Row = tuple[int, str]
    Key = tuple[Any, ...]
    KeyFunc = Callable[[str], Key]

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def _active_backend_name() -> str:
    """Return ``"fastnumbers"`` or ``"fake_fastnumbers"`` for this process."""
    if hasattr(fastnumbers_compat, "fn_ver"):
        return "fastnumbers"
    return "fake_fastnumbers"


ACTIVE_BACKEND = _active_backend_name()
FASTNUMBER_VERSION: str | None = getattr(
    fastnumbers_compat,
    "fn_ver",
    None,
)
INT_LIMIT: int | None = getattr(sys, "get_int_max_str_digits", lambda: None)()

if ACTIVE_BACKEND == "fastnumbers":
    BACKEND_COVERAGE_NOTE = (
        f"accelerated path covered: fastnumbers {FASTNUMBER_VERSION} provides "
        "try_int/try_float"
    )
else:
    BACKEND_COVERAGE_NOTE = (
        "fallback path covered: fastnumbers is not installed, so "
        "natsort.compat.fake_fastnumbers.fast_int/fast_float supply "
        "try_int/try_float; cross-backend claims use the injected divergence "
        "harness, and the real dual-backend test is skipped with an explicit "
        "reason"
    )


class BackendDivergenceError(AssertionError):
    """A pair of backends failed one of the compatibility criteria."""


# ---------------------------------------------------------------------------
# Controlled corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Corpus:
    """A controlled set of strings and the equality groups they must form."""

    strings: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    alg: NSType


OVERSIZED_OK = "b" + "9" * 4300
OVERSIZED_BAD = "b" + "9" * 4301

# The oversized strings are kept out of the baseline corpora: on the live
# fallback they intentionally parse as text and therefore are not comparable
# to integer-bearing keys, which is exactly the divergence under test.
OVERSIZED_CORPUS = Corpus(
    strings=("b5", OVERSIZED_OK, OVERSIZED_BAD),
    groups=(),
    alg=ns.INT,
)

# Integer parsing: heterogeneous digit spellings of the same numeric value,
# natural-order triples, and text immediately adjacent to the numeric runs.
INT_CORPUS = Corpus(
    strings=(
        "a1",
        "a①",
        "a١",
        "a01",
        "x12",
        "x۱۲",
        "z2",
        "z²",
        "①a",
        "1a",
        "a1a",
        "a1b",
        "a2",
        "a10",
        "item5",
        "item6",
        "item50",
        "a-5",
        "a-2",
        "a5",
        "b5",
    ),
    groups=(
        ("a1", "a①", "a١", "a01"),
        ("x12", "x۱۲"),
        ("z2", "z²"),
        ("①a", "1a"),
        ("a1a",),
        ("a1b",),
    ),
    alg=ns.INT,
)

SIGNED_CORPUS = Corpus(
    strings=("a-5", "a-2", "a5"),
    groups=(("a-5",), ("a-2",), ("a5",)),
    alg=ns.INT | ns.SIGNED,
)

# Float parsing: vulgar fractions and unicode decimals become numbers, and
# 2 vs 2.0 must collapse onto the same key.
FLOAT_CORPUS = Corpus(
    strings=(
        "item0.25",
        "item0.5",
        "item½",
        "item1",
        "v2",
        "v2.0",
        "w12.12",
        "w۱۲.۱۲",
        "nan",
        "inf",
        "c" + "9" * 5000,
    ),
    groups=(
        ("item0.5", "item½"),
        ("v2", "v2.0"),
        ("w12.12", "w۱۲.۱۲"),
    ),
    alg=ns.FLOAT,
)

ALL_CORPORA = (INT_CORPUS, SIGNED_CORPUS, FLOAT_CORPUS)


# ---------------------------------------------------------------------------
# The three compatibility criteria
# ---------------------------------------------------------------------------


@dataclass
class CompatReport:
    """Outcome of comparing two key functions over one corpus."""

    backend_a: str
    backend_b: str
    mismatched_equality: list[tuple[str, str]] = field(default_factory=list)
    flipped_orderings: list[tuple[str, str]] = field(default_factory=list)
    incomparable_pairs: list[tuple[str, ...]] = field(default_factory=list)
    split_groups: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not (
            self.mismatched_equality
            or self.flipped_orderings
            or self.incomparable_pairs
            or self.split_groups
        )


def _key_cmp(key_left: Key, key_right: Key) -> int | None:
    """Compare two keys, returning ``None`` when they cannot be compared."""
    try:
        less = key_left < key_right
        greater = key_right < key_left
    except TypeError:
        return None
    else:
        if less:
            return -1
        if greater:
            return 1
        return 0


def _pairwise(
    items: Iterable[tuple[str, Key]],
) -> Iterator[tuple[str, Key, str, Key]]:
    for (left, key_left), (right, key_right) in itertools.combinations(items, 2):
        yield left, key_left, right, key_right


def _record_equality_divergences(
    report: CompatReport,
    corpus: Corpus,
    keys_a: dict[str, Key],
    keys_b: dict[str, Key],
) -> None:
    """Record pairs whose key equality differs, plus internally split groups."""
    for left, key_left, right, key_right in _pairwise(keys_a.items()):
        equal_a = key_left == key_right
        equal_b = keys_b[left] == keys_b[right]
        if equal_a != equal_b:
            report.mismatched_equality.append((left, right))
    for group in corpus.groups:
        anchor = keys_a[group[0]]
        if any(keys_a[member] != anchor for member in group[1:]):
            report.split_groups.append(group)


def _record_order_divergences(
    report: CompatReport,
    keys_a: dict[str, Key],
    keys_b: dict[str, Key],
) -> None:
    """Record flipped orderings and pairs comparable on only one backend."""
    for left, key_left, right, key_right in _pairwise(keys_a.items()):
        cmp_a = _key_cmp(key_left, key_right)
        cmp_b = _key_cmp(keys_b[left], keys_b[right])
        if cmp_a is None or cmp_b is None:
            if cmp_a != cmp_b:
                report.incomparable_pairs.append((left, right))
        elif cmp_a != cmp_b:
            report.flipped_orderings.append((left, right))


def _record_merge_divergences(
    report: CompatReport,
    corpus: Corpus,
    key_a: KeyFunc,
    key_b: KeyFunc,
) -> None:
    """Record groups split or made incomparable by a two-worker merge."""
    rows: list[Row] = list(enumerate(corpus.strings))
    try:
        merged = _two_worker_merge(rows, key_a, key_b)
    except TypeError:
        report.incomparable_pairs.append(tuple(sorted(corpus.strings)))
    else:
        if not _groups_are_contiguous(merged, key_a):
            report.split_groups.append(tuple(sorted(corpus.strings)))


def check_backend_compatibility(
    key_a: KeyFunc,
    key_b: KeyFunc,
    corpus: Corpus,
    backend_a: str,
    backend_b: str,
) -> CompatReport:
    """Apply the equality, ordering and grouping criteria to two backends."""
    report = CompatReport(backend_a, backend_b)
    keys_a = {string: key_a(string) for string in corpus.strings}
    keys_b = {string: key_b(string) for string in corpus.strings}
    _record_equality_divergences(report, corpus, keys_a, keys_b)
    _record_order_divergences(report, keys_a, keys_b)
    _record_merge_divergences(report, corpus, key_a, key_b)
    return report


def assert_compatible(report: CompatReport) -> None:
    """Raise a descriptive error when a report shows a divergence."""
    if report.compatible:
        return
    msg = [
        f"backends {report.backend_a!r} and {report.backend_b!r} diverged:",
        f"  mismatched equality: {report.mismatched_equality}",
        f"  flipped orderings:   {report.flipped_orderings}",
        f"  incomparable pairs:  {report.incomparable_pairs}",
        f"  split groups:        {report.split_groups}",
    ]
    raise BackendDivergenceError("\n".join(msg))


def _two_worker_merge(
    rows: Sequence[Row],
    key_a: KeyFunc,
    key_b: KeyFunc,
) -> list[Row]:
    """Sort half the rows on each worker and merge the precomputed keys."""
    middle = len(rows) // 2
    left = sorted(
        ((key_a(string), idx, string) for idx, string in rows[:middle]),
        key=lambda row: row[0],
    )
    right = sorted(
        ((key_b(string), idx, string) for idx, string in rows[middle:]),
        key=lambda row: row[0],
    )
    return [
        (idx, string)
        for _, idx, string in heapq.merge(
            left,
            right,
            key=lambda row: row[0],
        )
    ]


def _groups_are_contiguous(merged: Sequence[Row], canonical: KeyFunc) -> bool:
    """True if no canonical equality group is interrupted by another group."""
    seen: set[Key] = set()
    previous: Key | None = None
    for _, string in merged:
        group = canonical(string)
        if group != previous:
            if group in seen:
                return False
            seen.add(group)
            previous = group
    return True


def _is_total_order(key: KeyFunc, strings: Sequence[str]) -> None:
    """Assert totality, antisymmetry and transitivity of a key relation."""
    items = [(string, key(string)) for string in strings]
    for _, key_left, _, key_right in _pairwise(items):
        # A total order: every pair is comparable, and the reverse relation
        # is the exact opposite (-1/0/1 -> its negation).
        relation = _key_cmp(key_left, key_right)
        reverse = _key_cmp(key_right, key_left)
        assert relation is not None
        assert reverse is not None
        assert relation == -reverse
    for (_, ka), (_, kb), (_, kc) in itertools.permutations(items, 3):
        if ka < kb < kc:
            assert ka < kc


def merged_relation_has_cycle(
    key_a: KeyFunc,
    key_b: KeyFunc,
    strings: Sequence[str],
) -> bool:
    """True if the union of two backends' strict orders is not transitive.

    Each backend contributes directed edges ``smaller -> larger``.  A single
    ordering is always acyclic, but merging two disagreeing backends can close
    a loop, which means no transitive total order can satisfy both workers.
    """
    edges: dict[str, set[str]] = {string: set() for string in strings}
    for key in (key_a, key_b):
        keys = {string: key(string) for string in strings}
        for left, right in itertools.combinations(strings, 2):
            relation = _key_cmp(keys[left], keys[right])
            if relation == -1:
                edges[left].add(right)
            elif relation == 1:
                edges[right].add(left)
    # Kahn's algorithm: a directed graph admits a topological ordering iff it
    # has no cycle.
    indegree: dict[str, int] = dict.fromkeys(strings, 0)
    for sources in edges.values():
        for target in sources:
            indegree[target] += 1
    queue = [string for string, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for target in edges[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return visited != len(strings)


def _stable_sort_preserves_input_order(key: KeyFunc, strings: Sequence[str]) -> None:
    rows = list(enumerate(strings))
    first = sorted(rows, key=lambda row: key(row[1]))
    second = sorted(rows, key=lambda row: key(row[1]))
    assert first == second
    keys_in_order = [key(string) for _, string in first]
    for group_key in set(keys_in_order):
        positions = [
            index for index, value in enumerate(keys_in_order) if value == group_key
        ]
        original_indices = [first[index][0] for index in positions]
        assert original_indices == sorted(original_indices)


# ---------------------------------------------------------------------------
# Divergence injection: a deliberately incompatible alternate component
# transform, standing in for a backend that parses a token differently.
# ---------------------------------------------------------------------------


def _patch_int_token(
    monkeypatch: pytest.MonkeyPatch,
    tokens: frozenset[str],
    *,
    coerce: Callable[[str], Any],
) -> None:
    """Force one regex token through ``coerce`` on the integer code path."""

    def alternate(
        components: Iterable[str],
        *,
        map: bool,
        on_fail: Callable[[str], str] = lambda value: value,
    ) -> Iterator[Any]:
        for component in components:
            if component in tokens:
                yield coerce(component)
            else:
                yield next(try_int([component], map=map, on_fail=on_fail))

    monkeypatch.setattr("natsort.utils.try_int", alternate)


def _patch_float_token(
    monkeypatch: pytest.MonkeyPatch,
    tokens: frozenset[str],
    *,
    coerce: Callable[[str], Any],
) -> None:
    """Force one regex token through ``coerce`` on the float code path."""

    def alternate(
        components: Iterable[str],
        *,
        map: bool,
        nan: float = float("inf"),
        on_fail: Callable[[str], str] = lambda value: value,
    ) -> Iterator[Any]:
        for component in components:
            if component in tokens:
                yield coerce(component)
            else:
                yield next(
                    try_float([component], map=map, nan=nan, on_fail=on_fail),
                )

    monkeypatch.setattr("natsort.utils.try_float", alternate)


def _unbounded_int(token: str) -> int:
    """Parse a token even when it crosses the CPython digit conversion cap."""
    set_limit = getattr(sys, "set_int_max_str_digits", None)
    if INT_LIMIT is None or set_limit is None:
        return int(token)
    set_limit(0)
    try:
        return int(token)
    finally:
        set_limit(INT_LIMIT)


# ---------------------------------------------------------------------------
# Minimization: shrink a diverging string to the smallest responsible fragment
# ---------------------------------------------------------------------------


def minimize_oversized_token(prefix: str = "b") -> tuple[int, str]:
    """Find the shortest all-9 token that the live integer path cannot parse."""
    key = natsort_keygen()
    low, high = 1, 5000
    while low < high:
        middle = (low + high) // 2
        token = "9" * middle
        if isinstance(key(prefix + token)[-1], str):
            high = middle
        else:
            low = middle + 1
    return low, "9" * low


# ---------------------------------------------------------------------------
# Tests: baseline behavior of whichever backend is installed locally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("corpus", ALL_CORPORA)
def test_live_backend_satisfies_all_compatibility_criteria(corpus: Corpus) -> None:
    key = natsort_keygen(alg=corpus.alg)
    for group in corpus.groups:
        anchor = key(group[0])
        assert all(key(member) == anchor for member in group)
    _is_total_order(key, corpus.strings)
    _stable_sort_preserves_input_order(key, corpus.strings)
    rows = list(enumerate(corpus.strings))
    merged = _two_worker_merge(rows, key, key)
    assert _groups_are_contiguous(merged, key)
    assert merged == sorted(rows, key=lambda row: key(row[1]))


@pytest.mark.parametrize("corpus", ALL_CORPORA)
def test_live_backend_equality_groups_are_key_identical(corpus: Corpus) -> None:
    key = natsort_keygen(alg=corpus.alg)
    for group in corpus.groups:
        signatures = {key(member) for member in group}
        assert len(signatures) == 1


def test_reports_which_backend_path_is_covered() -> None:
    if ACTIVE_BACKEND == "fastnumbers":
        assert FASTNUMBER_VERSION is not None
        assert hasattr(fastnumbers_compat, "fn_ver")
        assert "accelerated path" in BACKEND_COVERAGE_NOTE
    else:
        assert FASTNUMBER_VERSION is None
        assert not hasattr(fastnumbers_compat, "fn_ver")
        assert "fallback path" in BACKEND_COVERAGE_NOTE
    assert ACTIVE_BACKEND in {"fastnumbers", "fake_fastnumbers"}


# ---------------------------------------------------------------------------
# Tests: oversized integers split an equality group across backends
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    INT_LIMIT is None,
    reason="CPython digit conversion cap is absent",
)
def test_oversized_integer_diverges_on_live_fallback() -> None:
    key = natsort_keygen()
    assert isinstance(key(OVERSIZED_OK)[-1], int)
    # The fallback's int() hits the conversion cap and leaves the token as
    # text, while an accelerator would return a real int.
    assert isinstance(key(OVERSIZED_BAD)[-1], str)


@pytest.mark.skipif(
    INT_LIMIT is None,
    reason="CPython digit conversion cap is absent",
)
def test_oversized_integer_minimizes_to_cap_plus_one() -> None:
    assert INT_LIMIT is not None
    length, token = minimize_oversized_token()
    assert length == INT_LIMIT + 1
    key = natsort_keygen()
    assert isinstance(key("b" + "9" * (length - 1))[-1], int)
    assert isinstance(key("b" + token)[-1], str)


@pytest.mark.skipif(
    INT_LIMIT is None,
    reason="CPython digit conversion cap is absent",
)
def test_oversized_integer_divergence_splits_merge_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_key = natsort_keygen()
    bad_token = OVERSIZED_BAD[1:]
    _patch_int_token(monkeypatch, frozenset({bad_token}), coerce=_unbounded_int)
    accelerated_key = natsort_keygen()

    # One worker keeps the text fallback for the oversized token while the
    # other parses a normal integer; the mixed keys cannot be compared.
    rows: list[Row] = [(0, OVERSIZED_BAD), (1, "b5")]
    with pytest.raises(TypeError):
        _two_worker_merge(rows, live_key, accelerated_key)

    report = check_backend_compatibility(
        live_key,
        accelerated_key,
        OVERSIZED_CORPUS,
        "fake_fastnumbers",
        "fastnumbers(simulated)",
    )
    assert not report.compatible
    assert report.incomparable_pairs


# ---------------------------------------------------------------------------
# Tests: a single unicode numeral fragment breaks grouping / transitivity
# ---------------------------------------------------------------------------


def test_unicode_numeral_failure_splits_equality_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_key = natsort_keygen()
    assert live_key("a1") == live_key("a①")
    _patch_int_token(monkeypatch, frozenset({"①"}), coerce=lambda token: token)
    broken_key = natsort_keygen()

    report = check_backend_compatibility(
        live_key,
        broken_key,
        Corpus(("a1", "a①", "a2"), (("a1", "a①"),), ns.INT),
        "fake_fastnumbers",
        "fastnumbers(simulated)",
    )
    assert ("a1", "a①") in report.mismatched_equality
    assert not report.compatible

    # Minimization: the prefix "a" is innocent; the single numeral "①" is the
    # smallest fragment whose changed coercion still splits the group.
    assert live_key("a") == broken_key("a")
    assert live_key("①") != broken_key("①")


def test_unicode_numeral_wrong_value_breaks_strict_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_key = natsort_keygen()
    _patch_int_token(monkeypatch, frozenset({"①"}), coerce=lambda _token: 3)
    broken_key = natsort_keygen()

    report = check_backend_compatibility(
        live_key,
        broken_key,
        Corpus(("a1", "a①", "a2"), (("a1", "a①"),), ns.INT),
        "fake_fastnumbers",
        "fastnumbers(simulated)",
    )
    assert ("a1", "a①") in report.mismatched_equality
    assert ("a①", "a2") in report.flipped_orderings
    # Each backend alone stays a valid total order; only the mix is unsafe.
    _is_total_order(live_key, ("a1", "a①", "a2"))
    _is_total_order(broken_key, ("a1", "a①", "a2"))
    # The union closes a directed cycle: live orders a① before a2 while the
    # broken backend orders a2 before a①, so no transitive total order can
    # satisfy both workers at once.
    triple = ("a1", "a①", "a2")
    assert merged_relation_has_cycle(live_key, broken_key, triple)
    assert not merged_relation_has_cycle(live_key, live_key, triple)
    # Minimality: the contradicted pair alone already forms the cycle, and
    # removing either member of that pair dissolves it.
    contradicting_pair = ("a①", "a2")
    assert merged_relation_has_cycle(live_key, broken_key, contradicting_pair)
    for member in contradicting_pair:
        assert not merged_relation_has_cycle(
            live_key,
            broken_key,
            tuple(string for string in triple if string != member),
        )
    with pytest.raises(BackendDivergenceError):
        assert_compatible(report)


def test_unicode_run_adjacent_to_text_stays_one_group_on_live_backend() -> None:
    key = natsort_keygen()
    # Numeric runs directly next to ordinary text must keep their value.
    assert key("①a") == key("1a")
    assert key("x۱۲") == key("x12")
    assert key("z²") == key("z2")


# ---------------------------------------------------------------------------
# Tests: vulgar-fraction float parsing at the text boundary
# ---------------------------------------------------------------------------


def test_vulgar_fraction_failure_misorders_float_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_key = natsort_keygen(alg=ns.FLOAT)
    assert live_key("item½") == live_key("item0.5")
    _patch_float_token(monkeypatch, frozenset({"½"}), coerce=lambda token: token)
    broken_key = natsort_keygen(alg=ns.FLOAT)

    report = check_backend_compatibility(
        live_key,
        broken_key,
        Corpus(
            ("item0.25", "item0.5", "item½", "item1"),
            (("item0.5", "item½"),),
            ns.FLOAT,
        ),
        "fake_fastnumbers",
        "fastnumbers(simulated)",
    )
    assert ("item0.5", "item½") in report.mismatched_equality
    assert not report.compatible
    # Minimal fragment again: the fraction character alone carries the break.
    assert live_key("½") != broken_key("½")
    assert live_key("item") == broken_key("item")


def test_huge_float_overflow_remains_comparable_on_live_backend() -> None:
    key = natsort_keygen(alg=ns.FLOAT)
    overflow = key("c" + "9" * 5000)
    assert isinstance(overflow[-1], float)
    _is_total_order(key, ("c5", "c" + "9" * 5000))


# ---------------------------------------------------------------------------
# Tests: real dual-backend comparison in subprocesses (only with the extra)
# ---------------------------------------------------------------------------


_BLOCKER_SCRIPT = """
import json, sys

class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fastnumbers" or fullname.startswith("fastnumbers."):
            raise ImportError("fastnumbers blocked by the compatibility test")

sys.meta_path.insert(0, Blocker())
from natsort import natsort_keygen  # noqa: E402
from natsort.ns_enum import ns  # noqa: E402

alg = {alg!r}
strings = json.loads({payload!r})
key = natsort_keygen(alg=alg)
print(json.dumps({{s: list(key(s)) for s in strings}}))
"""


def _subprocess_keys(
    alg: NSType,
    strings: tuple[str, ...],
    *,
    block: bool,
) -> dict[str, list[Any]]:
    payload = json.dumps(list(strings))
    if block:
        script = _BLOCKER_SCRIPT.format(alg=int(alg), payload=payload)
    else:
        script = (
            "import json\n"
            "from natsort import natsort_keygen\n"
            f"key = natsort_keygen(alg={int(alg)!r})\n"
            f"strings = json.loads({payload!r})\n"
            "print(json.dumps({s: list(key(s)) for s in strings}))\n"
        )
    completed = subprocess.run(  # noqa: S603 - fixed local interpreter, test code
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return cast("dict[str, list[Any]]", json.loads(completed.stdout))


@pytest.mark.skipif(
    ACTIVE_BACKEND != "fastnumbers",
    reason="needs the fastnumbers extra installed; locally only the fallback "
    "is importable, so real dual-backend comparison cannot execute here",
)
@pytest.mark.parametrize("corpus", ALL_CORPORA)
def test_real_fastnumbers_and_fallback_agree_on_corpus(corpus: Corpus) -> None:
    accelerated = _subprocess_keys(corpus.alg, corpus.strings, block=False)
    fallback = _subprocess_keys(corpus.alg, corpus.strings, block=True)
    assert set(accelerated) == set(corpus.strings)
    for string in corpus.strings:
        assert tuple(accelerated[string]) == tuple(fallback[string]), string
