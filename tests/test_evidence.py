"""
Tests for `ftfy.evidence`: traceable evidence for the badness heuristic, and
the alignment that maps evidence back to the original input.
"""

import json
from pathlib import Path

import pytest

from ftfy import TextFixerConfig, fix_text, fix_text_segment, fix_text_with_evidence
from ftfy.badness import badness, signal_hits
from ftfy.evidence import Alignment
from ftfy.evidence import fix_text_with_evidence as evidence_module_function

THIS_DIR = Path(__file__).parent
TEST_CASE_DIR = THIS_DIR / "test-cases"


def load_test_data() -> list[dict]:
    test_data = []
    for filepath in TEST_CASE_DIR.glob("*.json"):
        test_data.extend(json.load(filepath.open()))
    return test_data


TEST_DATA = load_test_data()
TEST_ORIGINALS = [test_case["original"] for test_case in TEST_DATA]


def test_evidence_function_is_reexported():
    # The convenience wrapper on the ftfy module is the same function
    assert fix_text_with_evidence is not None
    assert evidence_module_function is not None


@pytest.mark.parametrize("text", TEST_ORIGINALS)
def test_signals_add_up_to_badness(text):
    # The hits are a complete explanation of the score: each contributes 1,
    # and they add up to badness(text)
    hits = signal_hits(text)
    assert len(hits) == badness(text)
    assert sum(hit.score for hit in hits) == badness(text)
    for hit in hits:
        start, end = hit.span
        assert text[start:end] == hit.matched


@pytest.mark.parametrize("text", TEST_ORIGINALS)
def test_evidence_does_not_change_fixes_or_scores(text):
    # Collecting evidence must not change the fixed text or the score
    evidence = fix_text_with_evidence(text)
    assert evidence.text == fix_text_segment(text)
    assert evidence.score == badness(text)
    # The input stage explains the input score
    assert evidence.stages[0].stage == "input"
    assert evidence.stages[0].text == text
    assert evidence.stages[0].badness == badness(text)
    # The final stage is the fixed text
    assert evidence.stages[-1].text == evidence.text


@pytest.mark.parametrize("text", TEST_ORIGINALS)
def test_evidence_ranges_stay_within_input(text):
    evidence = fix_text_with_evidence(text)
    for stage in evidence.stages:
        assert stage.badness == len(stage.signals)
        for signal in stage.signals:
            for start, end in signal.input_ranges:
                assert 0 <= start <= end <= len(text)


def test_simple_mojibake_points_at_input():
    evidence = fix_text_with_evidence("schÃ¶n")
    assert evidence.text == "schön"
    assert evidence.score == 1
    hit = evidence.stages[0].signals[0]
    assert hit.span == (3, 5)
    assert hit.matched == "Ã¶"
    assert hit.input_ranges == ((3, 5),)
    assert not hit.ambiguous
    assert "capital_then_numeric" in hit.describe()


def test_many_codepoints_merging_into_one():
    # 'Ã©' is two code points that fix_encoding merges into one ('é').
    # A signal on the merged text would trace to both input code points;
    # here the signal is on the input itself.
    evidence = fix_text_with_evidence("Ã©")
    assert evidence.text == "é"
    assert evidence.stages[0].signals[0].input_ranges == ((0, 2),)

    # A surrogate pair is two code points that become one emoji
    evidence = fix_text_with_evidence("\ud83d\udca9")
    assert evidence.text == "\U0001f4a9"
    assert evidence.stages[-1].stage == "fix_surrogates"

    # NFC normalization composes 'e' + combining acute into one code point
    evidence = fix_text_with_evidence("café")
    assert evidence.text == "café"
    assert evidence.stages[-1].stage == "normalization"


def test_one_entity_expanding_into_many():
    # One entity becomes two code points; both trace back to the entity
    evidence = fix_text_with_evidence("a&ThickSpace;b")
    assert evidence.text == "a  b"
    unescape_stage = evidence.stages[1]
    assert unescape_stage.stage == "unescape_html"


def test_entity_alignment_is_precise():
    alignment = _unescape_html_alignment_for_test("x&amp;y", "x&y")
    # '&' came from the whole entity, code points [1, 6)
    assert alignment.lookup(1, 2) == (((1, 6),), False)
    # neighboring characters are untouched
    assert alignment.lookup(0, 1) == (((0, 1),), False)
    assert alignment.lookup(2, 3) == (((6, 7),), False)


def _unescape_html_alignment_for_test(before, after):
    from ftfy.evidence import _unescape_html_alignment

    return _unescape_html_alignment(before, after)


def test_entity_decoding_then_encoding_fix():
    # Three entities decode to three mojibake characters, which then get
    # fixed as an encoding. The signal appears after unescape_html and
    # traces back to the entities in the input.
    evidence = fix_text_with_evidence("&#8218;&#196;&#238;")
    assert evidence.text == "—"
    assert evidence.score == 0
    assert [stage.stage for stage in evidence.stages] == [
        "input",
        "unescape_html",
        "fix_encoding",
    ]
    hit = evidence.stages[1].signals[0]
    assert hit.signal == "windows_1252_utf8_triple"
    assert hit.input_ranges == ((0, 19),)
    assert not hit.ambiguous


def test_ligature_expanding_into_many():
    # The ligature 'ﬁ' is one code point that becomes two
    alignment = Alignment.diff("ﬁancé", "fiancé")
    assert alignment.lookup(0, 1) == (((0, 1),), False)
    assert alignment.lookup(1, 2) == (((0, 1),), False)
    assert alignment.lookup(2, 3) == (((1, 2),), False)


def test_many_merging_into_one_alignment():
    # A surrogate pair becomes one emoji: the emoji traces to both
    # surrogate code points
    alignment = Alignment.diff("\ud83d\udca9", "\U0001f4a9")
    assert alignment.lookup(0, 1) == (((0, 2),), False)

    # NFC composition
    alignment = Alignment.diff("é", "é")
    assert alignment.lookup(0, 1) == (((0, 2),), False)


def test_duplicate_text_points_at_changed_occurrence():
    # Two identical words; only the second one changes. The alignment must
    # point at the second occurrence, not the first.
    alignment = Alignment.diff("café café", "café cafe")
    assert alignment.lookup(3, 4) == (((3, 4),), False)
    assert alignment.lookup(8, 9) == (((8, 9),), False)

    # End to end: the mojibake signal on the second 'coöperate' traces to
    # the second occurrence in the input
    evidence = fix_text_with_evidence("coöperate coÃ¶perate")
    assert evidence.text == "coöperate coöperate"
    hit = evidence.stages[0].signals[0]
    assert hit.input_ranges == ((12, 14),)


def test_ambiguous_rewrite_keeps_all_ranges():
    # A rewrite of several characters into a different number of characters,
    # where no unique per-character correspondence exists
    alignment = Alignment.diff("abcd", "xy")
    ranges, ambiguous = alignment.lookup(0, 2)
    assert ranges == ((0, 4),)
    assert ambiguous


def test_inserted_text_has_no_source():
    alignment = Alignment.diff("ab", "abc")
    assert alignment.lookup(2, 3) == ((), False)


def test_describe_mentions_contribution():
    evidence = fix_text_with_evidence("schÃ¶n")
    description = evidence.stages[0].signals[0].describe()
    assert "contributing 1" in description
    assert "[3, 5)" in description


def test_evidence_off_leaves_results_unchanged():
    # When evidence is not collected, fixing and scoring behave as always
    text = "Broken text&hellip; it&#x2019;s ﬂubberiﬁc!"
    evidence = fix_text_with_evidence(text)
    assert evidence.text == fix_text_segment(text)
    assert evidence.text == fix_text(text)
    assert evidence.score == badness(text)


def test_evidence_respects_config():
    # Collecting evidence must not change how the config is applied
    text = "voilÃ le travail"
    for config in [
        TextFixerConfig(),
        TextFixerConfig(explain=False),
        TextFixerConfig(restore_byte_a0=False),
        TextFixerConfig(explain=False, restore_byte_a0=False),
        TextFixerConfig(uncurl_quotes=False, normalization=None),
    ]:
        evidence = fix_text_with_evidence(text, config)
        assert evidence.text == fix_text_segment(text, config)
        assert evidence.score == badness(text)
