"""\
Row-level reconciliation tests for ``index_natsorted``.

These tests treat the output of ``index_natsorted`` as a *permutation* that must
reorder several columns (here an inventory fact table and its price column) in
lock-step, always identifying a row by its original source index.  They cover:

* lock-step reordering of two columns, with identical-looking rows that only
  differ in how the columns are paired;
* partition-then-merge giving the same permutation as sorting the whole table,
  with ties broken by the original source row number, both forward and reversed;
* a single-use, call-counting key that must be evaluated exactly once per row;
* composition of the pre- and post-filter permutations back onto original rows;
* the shortest counter-examples that separately distinguish a tie flip, a
  positional inversion, and evaluating the key a second time.
"""

from __future__ import annotations

from itertools import groupby
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis.strategies import booleans, integers, lists, sampled_from

from natsort import index_natsorted, natsort_key, natsort_keygen, order_by_index

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# A small alphabet with deliberate natural-sort collisions ("b" twice) and
# multi-digit numbers so the ordering is not plain lexicographic.
_ALPHABET = ["a", "a1", "a2", "a10", "b", "b", "x"]


def _stable_reverse_permutation(seq: Sequence[str]) -> list[int]:
    """Reference reversed permutation that keeps Python's stable tie order.

    A plain ``sorted(..., key=..., reverse=True)`` does *not* reverse the whole
    index sequence: equal keys keep their relative (ascending source index)
    order.  Reversing only the runs of equal keys reproduces that contract,
    independently of ``index_natsorted``.
    """
    forward = sorted(range(len(seq)), key=lambda i: natsort_key(seq[i]))
    runs = [list(run) for _, run in groupby(forward, key=lambda i: natsort_key(seq[i]))]
    return [i for run in reversed(runs) for i in run]


def _reference_permutation(seq: Sequence[str], reverse: bool) -> list[int]:
    forward = sorted(range(len(seq)), key=lambda i: natsort_key(seq[i]))
    return _stable_reverse_permutation(seq) if reverse else forward


class _OneShotKey:
    """Callable key that records evaluations and fails if asked a second time.

    Each instance wraps one row value.  It is a true single-use reader: the
    first call returns the value, any further call raises so a regression that
    evaluates the key twice per element is detected immediately.
    """

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, _value: object) -> str:
        self.calls += 1
        if self.calls > 1:
            msg = "single-use key evaluated more than once"
            raise AssertionError(msg)
        return self.value


# ---------------------------------------------------------------------------
# 1. Two columns move in lock-step; identical rows, different pairings.
# ---------------------------------------------------------------------------


def test_index_permutation_keeps_price_glued_to_inventory_row() -> None:
    # Rows 0, 1 and 3 carry the identical SKU "sku-7" but hold different
    # prices, so the only thing keeping each price attached to its row is the
    # source-index tie break.
    skus = ["sku-7", "sku-7", "sku-1", "sku-7", "sku-1"]
    prices = [10.0, 20.0, 30.0, 40.0, 50.0]

    forward = index_natsorted(skus)
    reverse = index_natsorted(skus, reverse=True)

    # Natural order groups "sku-1" before "sku-7"; within each SKU the original
    # row order is preserved (stable sort).
    assert forward == [2, 4, 0, 1, 3]
    assert order_by_index(skus, forward) == [
        "sku-1",
        "sku-1",
        "sku-7",
        "sku-7",
        "sku-7",
    ]
    assert order_by_index(prices, forward) == [30.0, 50.0, 10.0, 20.0, 40.0]

    # Reversing flips the distinct-key groups but never the equal-key rows:
    # the three "sku-7" rows still read 0, 1, 3 and each keeps its own price.
    assert reverse == [0, 1, 3, 2, 4]
    assert order_by_index(prices, reverse) == [10.0, 20.0, 40.0, 30.0, 50.0]


def test_indistinguishable_rows_are_separated_by_their_paired_column() -> None:
    # Every SKU is the same string; the price column is the only discriminator.
    # Reordering by the shared permutation must pair each price with the exact
    # original row rather than arbitrarily permuting equal-looking rows.
    skus = ["same", "same", "same", "same"]
    prices = [1.0, 2.0, 3.0, 4.0]

    forward = index_natsorted(skus)
    reverse = index_natsorted(skus, reverse=True)

    # All keys compare equal, so stability pins both permutations to identity.
    assert forward == [0, 1, 2, 3]
    assert reverse == [0, 1, 2, 3]
    assert order_by_index(prices, forward) == prices
    assert order_by_index(prices, reverse) == prices


# ---------------------------------------------------------------------------
# 2. Partition-then-merge equals a whole-table sort (forward and reverse).
# ---------------------------------------------------------------------------


def _partition_then_merge(
    seq: Sequence[str],
    partitions: Sequence[Sequence[int]],
    reverse: bool,
) -> list[int]:
    """Sort every partition locally, then merge on the global source rank."""
    local_orders: list[int] = []
    for rows in partitions:
        local = index_natsorted([seq[i] for i in rows], reverse=reverse)
        local_orders.extend(rows[k] for k in local)
    global_rank = {
        original: position
        for position, original in enumerate(_reference_permutation(seq, reverse))
    }
    return sorted(local_orders, key=lambda original: global_rank[original])


def test_partition_then_merge_matches_whole_table_on_cross_partition_ties() -> None:
    # Equal "x" keys are deliberately spread across both partitions so the
    # merge itself has to re-tie them by source row number.
    skus = ["x", "x", "y", "x"]
    partitions = [[0, 2], [1, 3]]

    whole_forward = index_natsorted(skus)
    whole_reverse = index_natsorted(skus, reverse=True)

    assert whole_forward == [0, 1, 3, 2]
    assert whole_reverse == [2, 0, 1, 3]
    assert _partition_then_merge(skus, partitions, reverse=False) == whole_forward
    assert _partition_then_merge(skus, partitions, reverse=True) == whole_reverse


@given(
    lists(sampled_from(_ALPHABET), min_size=1, max_size=14),
    booleans(),
    integers(min_value=2, max_value=4),
)
def test_partition_merge_agrees_with_whole_table_for_any_partitioning(
    seq: list[str],
    reverse: bool,
    partition_count: int,
) -> None:
    whole = index_natsorted(seq, reverse=reverse)
    # Round-robin source rows into partitions; every partition stays in
    # ascending source-row order, as produced by a real partitioned scan.
    partitions: list[list[int]] = [[] for _ in range(partition_count)]
    for row in range(len(seq)):
        partitions[row % partition_count].append(row)

    merged = _partition_then_merge(seq, partitions, reverse)

    assert merged == whole
    assert merged == _reference_permutation(seq, reverse)


# ---------------------------------------------------------------------------
# 3. Single-use, call-counting key is evaluated exactly once per row.
# ---------------------------------------------------------------------------


def test_key_is_evaluated_exactly_once_per_row() -> None:
    rows = ["row2", "row10", "row1", "row1", "row20"]
    one_shots = [_OneShotKey(value) for value in rows]

    index = index_natsorted(list(range(len(rows))), key=lambda i: one_shots[i](i))

    # Natural order, with the equal "row1" pair left in source order.
    assert index == [2, 3, 0, 1, 4]
    assert [shot.calls for shot in one_shots] == [1, 1, 1, 1, 1]


@given(lists(sampled_from(_ALPHABET), min_size=1, max_size=12), booleans())
def test_single_use_key_never_re_evaluated_under_property(
    seq: list[str],
    reverse: bool,
) -> None:
    one_shots = [_OneShotKey(value) for value in seq]

    index_natsorted(
        list(range(len(seq))),
        key=lambda i: one_shots[i](i),
        reverse=reverse,
    )

    assert all(shot.calls == 1 for shot in one_shots)


# ---------------------------------------------------------------------------
# 4. Pre-/post-filter permutations compose back onto original source rows.
# ---------------------------------------------------------------------------


def test_filter_permutation_composes_by_original_row_number() -> None:
    skus = ["x", "y", "x", "z", "x", "y"]
    prices = [10, 20, 30, 40, 50, 60]
    # An order-preserving mask filter: selected rows stay in ascending source
    # order, and equal "x"/"y" keys straddle the kept/dropped boundary.
    keep = [True, True, False, False, True, True]
    selected = [row for row, kept in enumerate(keep) if kept]
    assert selected == [0, 1, 4, 5]

    for reverse in (False, True):
        full_permutation = index_natsorted(skus, reverse=reverse)
        subset_permutation = index_natsorted(
            [skus[row] for row in selected],
            reverse=reverse,
        )

        # Compose the subset permutation through the selected source rows.
        composite = [selected[position] for position in subset_permutation]
        expected = [row for row in full_permutation if row in set(selected)]

        assert composite == expected
        # The price column must follow the same composed permutation.
        assert order_by_index(prices, composite) == [prices[row] for row in expected]


@given(
    lists(sampled_from(_ALPHABET), min_size=1, max_size=12),
    booleans(),
    booleans(),
)
def test_filter_composition_restricts_the_full_permutation(
    seq: list[str],
    reverse: bool,
    keep_flag: bool,
) -> None:
    # A deterministic, order-preserving mask driven by the strategy booleans.
    mask = [bool((row + int(keep_flag)) % 2) for row in range(len(seq))]
    selected = [row for row, kept in enumerate(mask) if kept]
    if not selected:
        return

    full_permutation = index_natsorted(seq, reverse=reverse)
    subset_permutation = index_natsorted(
        [seq[row] for row in selected],
        reverse=reverse,
    )
    composite = [selected[position] for position in subset_permutation]
    selected_set = set(selected)

    assert composite == [row for row in full_permutation if row in selected_set]
    assert composite == [
        row for row in _reference_permutation(seq, reverse) if row in selected_set
    ]


# ---------------------------------------------------------------------------
# 5. Shortest counter-examples that separate the three failure modes.
# ---------------------------------------------------------------------------


def _correct_permutation(
    seq: Sequence[str],
    key: Callable[[str], str] | None = None,
    reverse: bool = False,
) -> list[int]:
    generated = natsort_keygen(key=key)
    forward = sorted(range(len(seq)), key=lambda i: generated(seq[i]))
    if not reverse:
        return forward
    runs = [list(run) for _, run in groupby(forward, key=lambda i: generated(seq[i]))]
    return [i for run in reversed(runs) for i in run]


def _tie_flipped_permutation(seq: Sequence[str]) -> list[int]:
    # Bug: break ties with the *negated* source index instead of the index.
    generated = natsort_key
    return sorted(range(len(seq)), key=lambda i: (generated(seq[i]), -i))


def _inverse_permutation(seq: Sequence[str]) -> list[int]:
    # Bug: return the inverse (rank) permutation rather than the ordering.
    ordering = _correct_permutation(seq)
    inverse = [0] * len(ordering)
    for output_position, original in enumerate(ordering):
        inverse[original] = output_position
    return inverse


def _double_evaluated_permutation(
    seq: Sequence[str],
    key: Callable[[str], str],
) -> list[int]:
    # Bug: invoke the key twice per element before sorting.
    def doubled(value: str) -> str:
        key(value)
        return key(value)

    return _correct_permutation(seq, key=doubled)


def test_shortest_counterexamples_isolate_each_failure_mode() -> None:
    # Two genuinely equal rows: only a tie flip changes the answer.
    tie_seq = ["x", "x"]
    assert _correct_permutation(tie_seq) == [0, 1]
    assert _tie_flipped_permutation(tie_seq) == [1, 0]
    # A positional inversion is harmless when no element actually moves...
    assert _inverse_permutation(tie_seq) == [0, 1]
    # ...and a double evaluation is invisible without a single-use key.
    assert _correct_permutation(tie_seq, key=lambda value: value) == [0, 1]

    # A three-element non-trivial cycle is the smallest input whose inverse
    # differs from the ordering itself, while a tie flip leaves it untouched.
    cycle_seq = ["b", "c", "a"]
    expected_order = [2, 0, 1]
    assert _correct_permutation(cycle_seq) == expected_order
    assert _inverse_permutation(cycle_seq) == [1, 2, 0]
    assert _tie_flipped_permutation(cycle_seq) == expected_order

    # A single row is the smallest case that can expose a second key
    # evaluation, and it cannot involve a tie or an inversion at all.
    one_shot = _OneShotKey("a")
    assert _correct_permutation(["a"], key=one_shot) == [0]
    assert one_shot.calls == 1
    fresh_shot = _OneShotKey("a")
    with pytest.raises(AssertionError, match="more than once"):
        _double_evaluated_permutation(["a"], fresh_shot)
    assert fresh_shot.calls == 2


def test_index_natsorted_passes_all_three_discriminating_cases() -> None:
    assert index_natsorted(["x", "x"]) == [0, 1]
    assert index_natsorted(["b", "c", "a"]) == [2, 0, 1]

    one_shot = _OneShotKey("a")
    assert index_natsorted(["a"], key=one_shot) == [0]
    assert one_shot.calls == 1
