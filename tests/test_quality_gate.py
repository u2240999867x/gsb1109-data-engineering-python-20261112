"""
Tests for the mixed-corpus quality gate in ``ftfy.quality_gate``.

The fixture corpus in ``tests/quality-cases/corpus.json`` contains paired,
hand-labeled records. The same string appears once as a product title that
ftfy should fix and once as documentation that displays the mojibake on
purpose, and several records are repeated to simulate ten re-exports of the
same document.
"""

import json
from pathlib import Path

import pytest

from ftfy import fix_text
from ftfy.quality_gate import (
    ChangeInterval,
    QualityRecord,
    diff_intervals,
    evaluate_corpus,
    evaluate_record,
    load_records,
    minimal_switch_combos,
)

CORPUS_PATH = Path(__file__).parent / "quality-cases" / "corpus.json"


@pytest.fixture(scope="module")
def records():
    return load_records(CORPUS_PATH)


@pytest.fixture(scope="module")
def result(records):
    return evaluate_corpus(records)


def test_corpus_loads_and_contains_repeated_exports(records):
    assert len(records) == 23
    assert sum(record.record_id == "R-101" for record in records) == 10
    assert sum(record.record_id == "R-001" for record in records) == 2


def test_records_are_deduplicated_before_counting(result):
    assert len(result.outcomes) == 13
    assert result.duplicate_record_ids == ["R-001", "R-101"]
    assert len({outcome.record.record_id for outcome in result.outcomes}) == 13


def test_metrics_are_counted_per_distinct_business_record(result):
    fix_metrics = result.metrics_by_purpose["should_fix"]
    keep_metrics = result.metrics_by_purpose["should_keep"]

    assert fix_metrics.total == 7
    assert fix_metrics.false_retain_count == 1
    assert fix_metrics.false_change_count == 0
    assert fix_metrics.false_retain_rate == pytest.approx(1 / 7)

    # Five of six distinct should_keep records are wrongly rewritten. The ten
    # exports of R-101 count as one record, so the rate is 5/6 rather than the
    # 14/15 that a raw row count would produce.
    assert keep_metrics.total == 6
    assert keep_metrics.false_change_count == 5
    assert keep_metrics.false_change_rate == pytest.approx(5 / 6)
    assert keep_metrics.introduced_change_count == 5


def test_repeated_exports_cannot_move_the_proportion(records):
    baseline = evaluate_corpus(records)
    keep_rate = baseline.metrics_by_purpose["should_keep"].false_change_rate

    inflated = records + [
        record for record in records if record.record_id == "R-101"
    ] * 9
    repeated = evaluate_corpus(inflated)
    assert (
        repeated.metrics_by_purpose["should_keep"].false_change_rate == keep_rate
    )
    assert repeated.metrics_by_purpose["should_keep"].total == 6


def test_same_string_is_judged_by_field_purpose(result):
    by_id = {outcome.record.record_id: outcome for outcome in result.outcomes}

    title = by_id["R-002"]
    assert title.record.original == by_id["R-101"].record.original
    assert title.changed is True
    assert title.false_change is False

    documented = by_id["R-101"]
    assert documented.false_change is True
    assert documented.introduced_intervals == documented.changed_intervals


def test_changed_intervals_locate_every_rewrite(result):
    by_id = {outcome.record.record_id: outcome for outcome in result.outcomes}
    outcome = by_id["R-001"]

    assert outcome.changed_intervals == [ChangeInterval(3, 5, 3, 4)]
    source_span = outcome.record.original[3:5]
    target_span = outcome.actual[3:4]
    assert source_span == "Ã¶"
    assert target_span == "ö"


def test_diff_intervals_handle_insertions_and_deletions():
    assert diff_intervals("abc", "abc") == []
    # Pure insertion gets an empty source interval at the change point.
    assert diff_intervals("ac", "abc") == [ChangeInterval(1, 1, 1, 2)]
    # Pure deletion gets an empty target interval.
    assert diff_intervals("abc", "ac") == [ChangeInterval(1, 2, 1, 1)]


def test_false_retain_records_the_unchanged_intended_span(result):
    by_id = {outcome.record.record_id: outcome for outcome in result.outcomes}
    outcome = by_id["R-007"]

    assert outcome.changed is False
    assert outcome.false_retain is True
    assert outcome.retained_intervals == [ChangeInterval(43, 45, 43, 44)]
    assert outcome.record.original[43:45] == "√π"
    assert outcome.minimal_switch_combos == []


def test_wrong_fix_splits_into_retained_and_introduced_spans(result):
    by_id = {outcome.record.record_id: outcome for outcome in result.outcomes}
    outcome = by_id["R-006"]

    assert outcome.changed is True
    assert outcome.false_retain is False
    # The first region is fixed correctly, while the other two intended fixes
    # are never applied and therefore show up as retained spans.
    retained_text = [
        outcome.record.original[span.source_start:span.source_end]
        for span in outcome.retained_intervals
    ]
    assert retained_text == ["Ă ", "√π"]
    # The actual output consequently diverges from the expected output at the
    # same regions, which is recorded as introduced-change intervals.
    assert outcome.introduced_intervals
    # No switch combination can produce the hand-labeled expected output.
    assert outcome.minimal_switch_combos == []


def test_minimal_switch_combos_for_real_rewrites(result):
    by_id = {outcome.record.record_id: outcome for outcome in result.outcomes}

    assert by_id["R-102"].minimal_switch_combos == [("fix_encoding",)]
    assert by_id["R-103"].minimal_switch_combos == [("uncurl_quotes",)]
    assert by_id["R-105"].minimal_switch_combos == [
        ("uncurl_quotes", "unescape_html")
    ]
    # R-104 and R-101 reroute through another fixer when only one switch is
    # off, so neither switch alone explains the rewrite.
    assert by_id["R-104"].minimal_switch_combos == [
        ("fix_c1_controls", "fix_encoding")
    ]
    assert by_id["R-101"].minimal_switch_combos == [
        ("fix_encoding", "uncurl_quotes")
    ]


def test_combos_actually_suppress_the_rewrite(result):
    for outcome in result.outcomes:
        for combo in outcome.minimal_switch_combos:
            assert (
                fix_text(outcome.record.original, **{name: False for name in combo})
                == outcome.record.expected
            )


def test_all_minimal_combos_are_returned_for_an_or_gated_fixer():
    switches = ("switch_a", "switch_b", "switch_c")

    def toggle_fixer(text, disabled):
        # Two independent causes: disabling either switch suppresses the
        # rewrite, so there are two incomparable minimal combinations.
        if "switch_a" in disabled or "switch_b" in disabled:
            return text
        return text + " [rewritten]"

    combos = minimal_switch_combos(
        "original", "original", toggle_fixer=toggle_fixer, switches=switches
    )
    assert combos == [("switch_a",), ("switch_b",)]


def test_supersets_are_not_reported_as_minimal():
    switches = ("switch_a", "switch_b")

    def toggle_fixer(text, disabled):
        # An AND gate: both switches must be off together.
        if {"switch_a", "switch_b"} <= disabled:
            return text
        return text + " [rewritten]"

    combos = minimal_switch_combos(
        "original", "original", toggle_fixer=toggle_fixer, switches=switches
    )
    assert combos == [("switch_a", "switch_b")]


def test_no_combination_explains_an_unreachable_expectation():
    def toggle_fixer(text, disabled):
        return text + "X"

    assert (
        minimal_switch_combos(
            "original", "original", toggle_fixer=toggle_fixer, switches=("a", "b")
        )
        == []
    )


def test_default_toggle_fixer_rejects_unknown_switch():
    from ftfy.quality_gate import default_toggle_fixer

    with pytest.raises(ValueError, match="unknown ftfy switches"):
        default_toggle_fixer("She said “hi”", frozenset({"not_a_switch"}))


def test_toggle_search_can_be_skipped():
    record = QualityRecord(
        record_id="X-2",
        purpose="should_keep",
        original="MISUTÂ AJIKKO",
        expected="MISUTÂ AJIKKO",
    )
    outcome = evaluate_record(record, toggle_fixer=None)
    assert outcome.false_change is True
    assert outcome.minimal_switch_combos == []


def test_report_is_json_serializable(result):
    encoded = json.dumps(result.as_dict(), ensure_ascii=False)
    decoded = json.loads(encoded)
    assert set(decoded["metrics"]) == {"should_fix", "should_keep"}
    assert decoded["duplicate_record_ids"] == ["R-001", "R-101"]
    assert len(decoded["records"]) == 13


def test_record_validation():
    with pytest.raises(ValueError, match="unknown purpose"):
        QualityRecord(
            record_id="X-3",
            purpose="mystery",
            original="a",
            expected="a",
        )

    with pytest.raises(ValueError, match="should_keep"):
        QualityRecord(
            record_id="X-4",
            purpose="should_keep",
            original="a",
            expected="b",
        )

    with pytest.raises(ValueError, match="record_id"):
        QualityRecord(
            record_id="",
            purpose="should_fix",
            original="a",
            expected="b",
        )

    with pytest.raises(ValueError, match="missing required field"):
        QualityRecord.from_dict({"record_id": "X-5", "purpose": "should_fix"})
