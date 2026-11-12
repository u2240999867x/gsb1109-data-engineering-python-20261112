"""Tests for the natsort.explain public comparison explainer."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis.strategies import text

from natsort import (
    MISSING,
    Span,
    explain,
    natsort_keygen,
    natsorted,
    ns,
)


def test_explain_reports_policy_and_result() -> None:
    explanation = explain("a2", "a10")
    assert explanation.alg == ns.DEFAULT
    assert explanation.result == -1
    assert explain("a10", "a2").result == 1
    assert explain("a2", "a2").result == 0


def test_explain_rejects_invalid_alg() -> None:
    with pytest.raises(ValueError, match="alg"):
        explain("a", "b", alg="not-an-alg")


def test_explain_decisive_component_has_original_spans() -> None:
    explanation = explain("a2", "a10")
    decisive = explanation.decisive
    assert decisive is not None
    assert decisive.decisive
    assert decisive.path == (1,)
    assert (decisive.left, decisive.right) == (2, 10)
    assert decisive.kind == "number"
    # The spans point at the original labels on both ends.
    assert decisive.left_span == Span(1, 2)
    assert decisive.right_span == Span(1, 3)
    assert decisive.left_text == "2"
    assert decisive.right_text == "10"


def test_explain_skipped_equal_numbers_when_suffix_decides() -> None:
    explanation = explain("a01x", "a1y")
    # The suffix decides because the numbers are numerically equivalent.
    assert explanation.decisive is not None
    assert (explanation.decisive.left, explanation.decisive.right) == ("x", "y")
    # The equivalent-but-skipped numbers are pointed out.
    assert len(explanation.skipped_equal_numbers) == 1
    skipped = explanation.skipped_equal_numbers[0]
    assert skipped.value == 1
    assert skipped.left_text == "01"
    assert skipped.right_text == "1"
    assert skipped.left_span == Span(1, 3)
    assert skipped.right_span == Span(1, 2)


def test_explain_no_skipped_numbers_when_text_identical() -> None:
    explanation = explain("a1x", "a1y")
    assert explanation.skipped_equal_numbers == ()


def test_explain_preserves_hierarchy_with_path() -> None:
    explanation = explain("a/b2", "a/b10", alg=ns.PATH)
    assert explanation.left_key == (("a",), ("b", 2))
    assert explanation.decisive is not None
    assert explanation.decisive.path == (1, 1)
    # Spans are relative to the whole original string, not the path part.
    assert explanation.decisive.left_span == Span(3, 4)
    assert explanation.decisive.right_span == Span(3, 5)
    assert explanation.decisive.left_text == "2"
    assert explanation.decisive.right_text == "10"


def test_explain_preserves_hierarchy_with_ungroupletters() -> None:
    alg = ns.LOCALE | ns.UNGROUPLETTERS
    explanation = explain("Apple2", "apple10", alg=alg)
    assert explanation.decisive is not None
    assert explanation.decisive.path == (0, 0)
    assert explanation.decisive.kind == "first-letter"
    assert explanation.decisive.left_span == Span(0, 1)
    assert explanation.decisive.left_text == "A"


def test_explain_normalization_expansion_maps_to_original_span() -> None:
    # "é" (U+00E9) is decomposed into "e" + combining accent by NFD, but
    # the span must still cover the single original character.
    explanation = explain("é2", "e1z")
    first = explanation.components[0]
    assert first.left_span == Span(0, 1)
    assert first.left_text == "é"


def test_explain_casefold_expansion_maps_to_original_span() -> None:
    # "ß" casefolds to "ss"; the doubled text maps back to one character.
    explanation = explain("ß1", "ss2", alg=ns.IGNORECASE)
    first = explanation.components[0]
    assert first.left == first.right == "ss"
    assert first.left_span == Span(0, 1)
    assert first.left_text == "ß"
    assert first.right_span == Span(0, 2)
    assert first.right_text == "ss"


def test_explain_normalization_regroup_maps_to_original_span() -> None:
    # NFC (used with LOCALEALPHA) regroups "e" + combining accent into
    # "é"; the span must cover both original characters.
    explanation = explain("é", "é", alg=ns.LOCALEALPHA)
    assert explanation.result == 0
    first = explanation.components[0]
    assert first.left_span == Span(0, 2)
    assert first.left_text == "é"
    assert first.right_span == Span(0, 1)
    assert first.right_text == "é"


def test_explain_regroup_does_not_blur_later_spans() -> None:
    # Even though "é" is regrouped from two characters by NFC, components
    # after it must still map to their own original intervals.
    alg = ns.LOCALE | ns.IGNORECASE
    explanation = explain("café 2号", "café 10号", alg=alg)
    decisive = explanation.decisive
    assert decisive is not None
    assert decisive.kind == "number"
    assert decisive.left_span == Span(5, 6)
    assert decisive.left_text == "2"
    assert decisive.right_span == Span(5, 7)
    assert decisive.right_text == "10"


def test_explain_locale_marks_whole_segment() -> None:
    explanation = explain("apple", "apple2", alg=ns.LOCALEALPHA)
    first = explanation.components[0]
    assert first.kind == "string"
    assert first.note is not None
    assert "whole segment" in first.note
    # The locale transform is opaque, so the span covers the whole segment.
    assert first.left_span == Span(0, 5)
    assert first.right_span == Span(0, 5)


def test_explain_inserted_separator_has_no_span() -> None:
    explanation = explain("2a", "10a")
    separator = explanation.components[0]
    assert separator.kind == "separator"
    assert separator.left_span is None
    assert separator.right_span is None
    assert separator.note is not None


def test_explain_key_applied_exactly_once_and_reused() -> None:
    calls = []

    def key(d: dict[str, str]) -> str:
        calls.append(d)
        return d["label"]

    left, right = {"label": "x2"}, {"label": "x10"}
    explanation = explain(left, right, key=key)
    # One evaluation per input, reused by the explanation and the ordering.
    assert calls == [left, right]
    assert explanation.left_value == "x2"
    assert explanation.right_value == "x10"
    assert explanation.result == -1
    assert explanation.decisive is not None
    assert explanation.decisive.left_span == Span(1, 2)
    # The explanation agrees with an actual sort using the same key.
    assert natsorted([right, left], key=key) == [left, right]


def test_explain_matches_real_sort_keys_with_key_function() -> None:
    key = str.swapcase
    alg = ns.FLOAT | ns.IGNORECASE
    key_func = natsort_keygen(key, alg)
    explanation = explain("A1.5", "a2", key=key, alg=alg)
    assert explanation.left_key == key_func("A1.5")
    assert explanation.right_key == key_func("a2")


def test_explain_non_string_inputs_have_no_spans() -> None:
    assert explain(2, 10).result == -1
    assert explain(2, 10).decisive.left_span is None  # type: ignore[union-attr]
    assert explain(None, 5).result == -1
    assert explain(float("nan"), 5).result == -1
    # bytes are compared as-is by natsort (no number parsing)
    bytes_explanation = explain(b"a2", b"a10")
    assert bytes_explanation.result == 1  # b"a2" > b"a10" lexicographically
    assert bytes_explanation.decisive is not None
    assert bytes_explanation.decisive.left_span is None


def test_explain_iterable_input_preserves_hierarchy() -> None:
    explanation = explain(("a", 2), ("a", 10))
    assert explanation.result == -1
    assert explanation.decisive is not None
    assert explanation.decisive.path == (1, 1)


def test_explain_prefix_keys_note_exhaustion() -> None:
    explanation = explain("a1", "a1b")
    assert explanation.result == -1
    decisive = explanation.decisive
    assert decisive is not None
    assert decisive.right == "b"
    assert decisive.left is MISSING
    assert decisive.note is not None
    assert "exhausted" in decisive.note


def test_explain_str_output_is_reviewable() -> None:
    rendered = str(explain("a01x", "a1y"))
    assert "ns.DEFAULT" in rendered
    assert "decisive component" in rendered
    assert "skipped equal number" in rendered
    assert "'01'" in rendered


@given(text(), text())
def test_explain_result_always_matches_real_keys(left: str, right: str) -> None:
    for alg in (ns.DEFAULT, ns.FLOAT, ns.IGNORECASE, ns.PATH, ns.NUMAFTER):
        key_func = natsort_keygen(alg=alg)
        left_key, right_key = key_func(left), key_func(right)
        expected = (left_key > right_key) - (left_key < right_key)
        explanation = explain(left, right, alg=alg)
        assert explanation.result == expected
        assert explanation.left_key == left_key
        assert explanation.right_key == right_key
        # Any reported span must slice the original input.
        for component in explanation.components:
            if component.left_span is not None:
                assert left[component.left_span.start : component.left_span.end]
            if component.right_span is not None:
                assert right[component.right_span.start : component.right_span.end]
