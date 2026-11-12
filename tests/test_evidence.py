# tests/test_evidence.py
"""Tests for the per-detection evidence record and policy-change replay."""

from __future__ import annotations

import dataclasses
import warnings

import pytest

from chardet import (
    DetectionEvidence,
    DetectionReport,
    ReplayVerdict,
    UniversalDetector,
    detect,
    detect_with_evidence,
)
from chardet.enums import EncodingEra
from chardet.evidence import (
    GATE_OUTPUT_RENDER,
    GATE_SCORING,
    GATE_VALIDITY,
    OUTCOME_EVIDENCE,
    OUTCOME_FALLBACK,
    STAGE_BOM,
    STAGE_STATISTICAL,
    UNCHANGED,
)

_FRENCH = "C'est un café très français, avec des œufs et des crêpes délicieuses. " * 20
# Bytes cp1252 leaves undefined but iso8859-1 maps, wrapped in prose.
_UNMAPPABLE = (
    b"Legacy memo with an unmappable byte: " + bytes([0x81]) + b" and cafe text."
)


@pytest.fixture
def evidence_report() -> DetectionReport:
    return detect_with_evidence(_FRENCH.encode("cp1252"))


@pytest.fixture
def fallback_report() -> DetectionReport:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return detect_with_evidence(_UNMAPPABLE, include_encodings=["utf-8", "cp1252"])


# ---------------------------------------------------------------------------
# Record construction and detect() parity
# ---------------------------------------------------------------------------


def test_evidence_detection_matches_plain_detect():
    data = _FRENCH.encode("cp1252")
    report = detect_with_evidence(data)
    assert isinstance(report, DetectionReport)
    assert report.result == detect(data)
    assert isinstance(report.evidence, DetectionEvidence)


def test_record_holds_no_input_bytes(evidence_report: DetectionReport):
    # The record is built from names/scores/gates, never the source bytes.
    dumped = repr(evidence_report.evidence)
    assert _FRENCH[:8].encode("cp1252").hex() not in dumped


def test_evidence_selected_winner_record(evidence_report: DetectionReport):
    evidence = evidence_report.evidence
    assert evidence.outcome == OUTCOME_EVIDENCE
    assert evidence.stage == STAGE_STATISTICAL
    assert evidence.winner == "cp1252"
    assert not evidence.is_fallback
    # The winner carries a real score and rank zero.
    winner = evidence.verdict_for("cp1252")
    assert winner is not None
    assert winner.scored
    assert winner.final_rank == 0
    assert evidence.ranking[0][0] == "cp1252"


def test_fallback_record(fallback_report: DetectionReport):
    evidence = fallback_report.evidence
    assert evidence.outcome == OUTCOME_FALLBACK
    assert evidence.is_fallback
    assert evidence.winner == "cp1252"
    # Both files ultimately report cp1252, but on opposite evidence.
    assert fallback_report.result["encoding"] == "Windows-1252"
    assert fallback_report.result["confidence"] == pytest.approx(0.10)
    utf8 = evidence.verdict_for("utf-8")
    assert utf8 is not None
    assert utf8.gate == GATE_VALIDITY
    assert utf8.reason == "fails_window_decode"
    assert not utf8.scored


def test_whitelist_filter_reasons_are_recorded():
    report = detect_with_evidence(
        _FRENCH.encode("cp1252"), encoding_era=EncodingEra.MODERN_WEB
    )
    evidence = report.evidence
    # A DOS-era encoding was never admitted, with its reason on record.
    cp437 = evidence.verdict_for("cp437")
    assert cp437 is not None
    assert cp437.reason == "outside_encoding_era"
    assert "cp437" not in evidence.admitted


# ---------------------------------------------------------------------------
# Evidence-selected answer: whitelist tightening
# ---------------------------------------------------------------------------


def test_deleting_scored_losers_is_reusable(evidence_report: DetectionReport):
    evidence = evidence_report.evidence
    losers = [name for name, _ in evidence.ranking[1:3]]
    assert losers  # sanity: there are recorded losers
    verdict = evidence.can_reuse_decision(exclude_encodings=losers)
    assert verdict.reusable
    assert verdict.rerun_stage is None


def test_deleting_pruned_candidate_is_reusable(evidence_report: DetectionReport):
    evidence = evidence_report.evidence
    verdict = evidence.can_reuse_decision(exclude_encodings=["cp437"])
    assert verdict.reusable


def test_deleting_winner_requires_scoring_rerun(evidence_report: DetectionReport):
    verdict = evidence_report.evidence.can_reuse_decision(exclude_encodings=["cp1252"])
    assert not verdict.reusable
    assert verdict.rerun_stage == GATE_SCORING
    assert verdict.blocking_candidates == ("cp1252",)


def test_adding_previously_excluded_candidate_requires_validity(
    evidence_report: DetectionReport,
):
    modern = detect_with_evidence(
        _FRENCH.encode("cp1252"), encoding_era=EncodingEra.MODERN_WEB
    )
    verdict = modern.evidence.can_reuse_decision(encoding_era=EncodingEra.ALL)
    assert not verdict.reusable
    assert verdict.rerun_stage == GATE_VALIDITY
    assert "cp437" in verdict.blocking_candidates


def test_unknown_whitelist_name_is_unanswerable(evidence_report: DetectionReport):
    verdict = evidence_report.evidence.can_reuse_decision(
        include_encodings=["cp1252", "totally-fake-codec"]
    )
    assert not verdict.reusable
    assert verdict.rerun_stage == "candidate_whitelist"


def test_deterministic_stage_survives_whitelist_additions():
    report = detect_with_evidence(b"\xef\xbb\xbfhello")
    assert report.evidence.stage == STAGE_BOM
    verdict = report.evidence.can_reuse_decision(
        include_encodings=["utf-8-sig", "cp1252", "iso8859-1"]
    )
    assert verdict.reusable


# ---------------------------------------------------------------------------
# Fallback answer: tightening has a narrower safe zone
# ---------------------------------------------------------------------------


def test_fallback_reuse_when_removed_candidate_was_recorded_rejected(
    fallback_report: DetectionReport,
) -> None:
    verdict = fallback_report.evidence.can_reuse_decision(include_encodings=["cp1252"])
    assert verdict.reusable
    assert "fails_window_decode" in verdict.reason


def test_fallback_cannot_reuse_when_sentinel_removed(fallback_report: DetectionReport):
    verdict = fallback_report.evidence.can_reuse_decision(include_encodings=["utf-8"])
    assert not verdict.reusable
    assert verdict.rerun_stage == GATE_VALIDITY
    assert "cp1252" in verdict.blocking_candidates


def test_fallback_widening_requires_rerun_and_matches_real_detection(
    fallback_report: DetectionReport,
) -> None:
    evidence = fallback_report.evidence
    verdict = evidence.can_reuse_decision(
        include_encodings=["utf-8", "cp1252", "iso8859-1"]
    )
    assert not verdict.reusable
    assert verdict.rerun_stage == GATE_VALIDITY
    assert verdict.blocking_candidates == ("iso8859-1",)
    # The predicted instability is real: a fresh run picks the new codec.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        widened = detect(
            _UNMAPPABLE, include_encodings=["utf-8", "cp1252", "iso8859-1"]
        )
    assert widened["encoding"] != fallback_report.result["encoding"]


# ---------------------------------------------------------------------------
# Output-name (alias) policy changes
# ---------------------------------------------------------------------------


def test_alias_only_rename_is_reusable(
    evidence_report: DetectionReport,
    fallback_report: DetectionReport,
) -> None:
    for report in (evidence_report, fallback_report):
        verdict = report.evidence.can_reuse_decision(compat_names=False)
        assert verdict.reusable
        assert "same codec" in verdict.reason


def test_narrower_display_name_replay_governed_by_recorded_decode() -> None:
    japanese = "日本語のテキストです。文字コード判定。".encode("euc_jp") * 4
    options = {"include_encodings": ["euc_jis_2004", "utf-8"]}

    # compat_names on: the render trace verifies the narrower display codec.
    display = detect_with_evidence(japanese, **options)
    assert display.evidence.winner == "euc_jis_2004"
    assert any(
        event.transform == "compat_names"
        and event.codec_changed
        and event.decode_verified
        for event in display.evidence.render
    )
    assert display.evidence.can_reuse_decision(compat_names=False).reusable

    # compat_names off: no display-codec decode is on record, so switching
    # it on cannot be answered without re-gathering the render evidence.
    raw = detect_with_evidence(japanese, compat_names=False, **options)
    verdict = raw.evidence.can_reuse_decision(compat_names=True)
    assert not verdict.reusable
    assert verdict.rerun_stage == GATE_OUTPUT_RENDER


def test_superset_remap_skipped_when_target_fails_decode():
    text = ("Legacy line with a control glyph: " + chr(0x8D) + " café. ") * 10
    report = detect_with_evidence(
        text.encode("iso8859-1"),
        include_encodings=["iso8859-1", "cp1252", "utf-8"],
    )
    assert report.evidence.winner == "iso8859-1"
    verdict = report.evidence.can_reuse_decision(prefer_superset=True)
    assert verdict.reusable
    assert "cannot decode" in verdict.reason


def test_superset_remap_needs_render_gate_without_recorded_decode():
    report = detect_with_evidence(b"Pure seven-bit prose, nothing above 0x7F. " * 5)
    assert report.evidence.winner == "ascii"
    verdict = report.evidence.can_reuse_decision(prefer_superset=True)
    assert not verdict.reusable
    assert verdict.rerun_stage == GATE_OUTPUT_RENDER


def test_recorded_superset_decode_supports_turning_flag_off():
    report = detect_with_evidence(
        b"Pure seven-bit prose, nothing above 0x7F. " * 5,
        prefer_superset=True,
    )
    assert any(
        event.transform == "preferred_superset"
        and event.applied
        and event.decode_verified
        for event in report.evidence.render
    )
    verdict = report.evidence.can_reuse_decision(prefer_superset=False)
    assert verdict.reusable


# ---------------------------------------------------------------------------
# Empty / binary outcomes
# ---------------------------------------------------------------------------


def test_empty_input_fallback_reuses_on_tightening():
    report = detect_with_evidence(b"")
    assert report.evidence.is_fallback
    verdict = report.evidence.can_reuse_decision(
        exclude_encodings=["cp1252", "iso8859-1"]
    )
    assert verdict.reusable


def test_binary_verdict_is_whitelist_independent():
    report = detect_with_evidence(b"\x00\x01\x02\x03" * 20)
    assert report.evidence.winner is None
    verdict = report.evidence.can_reuse_decision(exclude_encodings=["cp1252", "utf-8"])
    assert verdict.reusable


# ---------------------------------------------------------------------------
# Streaming interface and the unchanged sentinel
# ---------------------------------------------------------------------------


def test_universal_detector_close_with_evidence():
    detector = UniversalDetector()
    detector.feed(_FRENCH.encode("cp1252"))
    report = detector.close_with_evidence()
    assert report.result == detect(_FRENCH.encode("cp1252"))
    assert report.evidence.winner == "cp1252"


def test_close_with_evidence_after_close_raises():
    detector = UniversalDetector()
    detector.feed(b"hi")
    detector.close()
    with pytest.raises(ValueError, match="close_with_evidence"):
        detector.close_with_evidence()


def test_unchanged_sentinel_leaves_policy_alone(evidence_report: DetectionReport):
    verdict = evidence_report.evidence.can_reuse_decision(
        encoding_era=UNCHANGED,
        include_encodings=UNCHANGED,
        exclude_encodings=UNCHANGED,
        compat_names=UNCHANGED,
        prefer_superset=UNCHANGED,
    )
    assert isinstance(verdict, ReplayVerdict)
    assert verdict.reusable


def test_evidence_record_is_frozen(evidence_report: DetectionReport):
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence_report.evidence.winner = "iso8859-1"  # type: ignore[misc]
