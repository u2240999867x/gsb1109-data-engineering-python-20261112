"""Tests for the independent multi-sample consensus entry point.

The behavior pinned here mirrors the contract in
:mod:`chardet.consensus`: samples are analyzed independently (never
concatenated), BOM and empty input get their own verdicts, identical
contents count once, and conflicts are reported rather than averaged away.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import chardet
from chardet.consensus import (
    ConsensusResult,
    ConsensusStatus,
    EncodingRejection,
    EncodingSupport,
    SampleReport,
    SampleVerdict,
    detect_consensus,
)
from chardet.registry import get_candidates

_FRENCH = (
    "Le café était fermé, alors nous sommes allés à la gare. "
    "Nous avons acheté des croissants, du beurre et de la confiture. "
    "Le matin, François préfère lire le journal en mangeant des tartines. "
    "L'été dernier, nous avons visité plusieurs villages très anciens. "
).encode("cp1252")
_FRENCH_2 = (
    "La réunion commença à huit heures, dans une salle très chaleureuse. "
    "Chacun expliqua son projet avec patience et une grande précision. "
    "Les décisions furent prises à l'unanimité après une longue discussion. "
).encode("cp1252")
_GREEK = (
    "Η πρωινή συνάντηση ξεκίνησε εγκαίρως με όλους τους συνεργάτες παρόντες. "
    "Συζητήθηκαν τα οικονομικά στοιχεία και οι νέες προτάσεις για την χρονιά. "
    "Όλοι συμφώνησαν ότι η συνεργασία ήταν εξαιρετικά εποικοδομητική. "
).encode("iso8859-7")
_GREEK_UTF8 = "Καλημέρα κόσμε, ελληνικό κείμενο με πολλούς χαρακτήρες.".encode()


# ---------------------------------------------------------------------------
# Status outcomes
# ---------------------------------------------------------------------------


def test_statistical_fleet_reaches_agreement_on_common_encoding():
    result = detect_consensus([_FRENCH, _FRENCH_2])

    assert result.status is ConsensusStatus.AGREEMENT
    assert result.conflicts == ()
    assert "cp1252" in result.common_encodings
    # Every reported common encoding must decode every informative sample.
    for report in result.samples:
        assert set(result.common_encodings) <= set(report.compatible)


def test_contradictory_statistical_fleets_conflict_without_a_forced_winner():
    result = detect_consensus([_FRENCH, _FRENCH_2, _GREEK])

    assert result.status is ConsensusStatus.CONFLICT
    assert result.common_encodings == ()
    assert result.conflicts
    # A statistical disagreement is a set-level conflict, not blamed on one
    # structurally committed sample.
    assert any(conflict.sample_index == -1 for conflict in result.conflicts)
    # The losing side's encoding must not be smuggled back as the answer.
    assert "cp1252" not in result.common_encodings
    assert "cp1253" not in result.common_encodings


def test_inconclusive_when_no_sample_clears_the_support_threshold():
    # High bytes present, but far too little text for above-threshold scores.
    result = detect_consensus([b"caf\xe9", b"cr\xe8me"])

    assert result.status is ConsensusStatus.INCONCLUSIVE
    assert result.common_encodings == ()
    assert result.conflicts == ()
    # Both no-veto verdicts are acceptable; the point is that no sample
    # produces above-threshold positive evidence.
    assert {report.verdict for report in result.samples} <= {
        SampleVerdict.INDETERMINATE,
        SampleVerdict.NO_MATCH,
    }


def test_all_empty_samples_is_no_evidence_not_an_encoding_claim():
    result = detect_consensus([b"", b"", b""])

    assert result.status is ConsensusStatus.NO_EVIDENCE
    assert result.common_encodings == ()
    assert result.conflicts == ()
    assert len(result.samples) == 1
    assert result.samples[0].verdict is SampleVerdict.EMPTY
    assert result.samples[0].occurrences == 3


def test_empty_samples_are_ignored_alongside_real_ones():
    result = detect_consensus([b"", _FRENCH, _FRENCH_2, b""])

    assert result.status is ConsensusStatus.AGREEMENT
    verdicts = {report.index: report.verdict for report in result.samples}
    assert verdicts[0] is SampleVerdict.EMPTY
    assert all(
        report.verdict is not SampleVerdict.EMPTY
        for report in result.samples
        if report.index != 0
    )


# ---------------------------------------------------------------------------
# BOM and structural commitments
# ---------------------------------------------------------------------------


def test_bom_sample_gets_its_own_verdict_and_keeps_decode_reasons():
    bom_sample = b"\xef\xbb\xbfhello world"
    result = detect_consensus([bom_sample, b"plain ascii text"])

    assert result.status is ConsensusStatus.AGREEMENT
    bom_report = result.samples[0]
    assert bom_report.verdict is SampleVerdict.BOM
    assert bom_report.supported[0].encoding == "utf-8-sig"
    assert bom_report.supported[0].verdict is SampleVerdict.BOM
    assert "utf-8-sig" in bom_report.compatible
    # The BOM answer does not suppress the per-candidate rejection record.
    assert all(
        isinstance(rejection, EncodingRejection) for rejection in bom_report.rejected
    )


def test_conflicting_boms_are_a_structural_conflict():
    utf8_signed = b"\xef\xbb\xbf" + _GREEK_UTF8
    utf16_signed = _GREEK_UTF8.decode("utf-8").encode("utf-16")
    result = detect_consensus([utf8_signed, utf16_signed])

    assert result.status is ConsensusStatus.CONFLICT
    committed = {conflict.encoding for conflict in result.conflicts}
    assert "utf-8-sig" in committed or "utf-16" in committed
    assert all(report.verdict is SampleVerdict.BOM for report in result.samples)


def test_structural_utf8_commitment_conflicts_with_latin_bytes():
    result = detect_consensus([_FRENCH, _GREEK_UTF8])

    assert result.status is ConsensusStatus.CONFLICT
    conflict = next(
        conflict for conflict in result.conflicts if conflict.encoding == "utf-8"
    )
    # The conflict names both samples, so the reason is auditable.
    assert str(conflict.sample_index) in conflict.reason


def test_pure_ascii_does_not_veto_a_legacy_fleet():
    result = detect_consensus([b"only printable ascii here", _FRENCH, _FRENCH_2])

    ascii_report = next(
        report
        for report in result.samples
        if report.verdict is SampleVerdict.STRUCTURAL
        and report.supported[0].encoding == "ascii"
    )
    assert result.status is ConsensusStatus.AGREEMENT
    assert "cp1252" in result.common_encodings
    # ASCII opens the floor to the codecs that decode it, not just ascii.
    assert "cp1252" in ascii_report.compatible


def test_all_ascii_fleet_agrees_on_ascii():
    result = detect_consensus([b"hello", b"world"])

    assert result.status is ConsensusStatus.AGREEMENT
    assert "ascii" in result.common_encodings


# ---------------------------------------------------------------------------
# Independent sampling: no concatenation, no cross-file statistics
# ---------------------------------------------------------------------------


def test_multibyte_char_split_across_samples_is_never_reassembled():
    # U+03B1 (alpha) is CE B1 in UTF-8.  Split between the samples: only
    # their concatenation is valid UTF-8; each fragment on its own is not.
    lead_sample = b"prefix text \xce"
    tail_sample = b"\xb1 suffix text"
    result = detect_consensus([lead_sample, tail_sample])

    tail_report = next(report for report in result.samples if report.index == 1)
    assert "utf-8" in {rejection.encoding for rejection in tail_report.rejected}
    assert "utf-8" not in {support.encoding for support in tail_report.supported}
    # A fabricated cross-file UTF-8 verdict must not appear anywhere.
    assert "utf-8" not in result.common_encodings


def test_consensus_matches_independent_single_shot_detections():
    samples = [_FRENCH, _GREEK]
    result = detect_consensus(samples)

    for report, sample in zip(result.samples, samples, strict=True):
        assert report.byte_length == len(sample)
        assert report.verdict in set(SampleVerdict)
        # Canonical names: run detect_all without the display-name remap so
        # its entries line up with the consensus report's codec names.
        ranked = chardet.detect_all(sample, compat_names=False)
        ranked_names = {entry["encoding"] for entry in ranked}
        reported = {support.encoding for support in report.supported}
        # Every above-threshold consensus entry is an independent ranking
        # entry for the same bytes.
        assert reported <= ranked_names or report.verdict in (
            SampleVerdict.INDETERMINATE,
            SampleVerdict.NO_MATCH,
            SampleVerdict.BINARY,
        )


# ---------------------------------------------------------------------------
# Duplicate content counts once
# ---------------------------------------------------------------------------


def test_duplicated_cannot_outvote_a_structural_contradiction():
    # Twenty copies of one Latin sample versus one structurally committed
    # UTF-8 sample.  Copies add no evidence, so the conflict must survive.
    result = detect_consensus([_FRENCH] * 20 + [_GREEK_UTF8])

    assert result.status is ConsensusStatus.CONFLICT
    assert len(result.samples) == 2
    latin_report = result.samples[0]
    assert latin_report.occurrences == 20
    greek_report = result.samples[1]
    assert greek_report.occurrences == 1
    assert any(conflict.encoding == "utf-8" for conflict in result.conflicts)


def test_duplicate_contents_produce_one_report_and_one_pipeline_run():
    result_a = detect_consensus([_FRENCH, _FRENCH, _FRENCH])
    result_b = detect_consensus([_FRENCH])

    assert len(result_a.samples) == 1
    assert result_a.samples[0].occurrences == 3
    assert result_a.samples[0].supported == result_b.samples[0].supported
    assert result_a.samples[0].rejected == result_b.samples[0].rejected


# ---------------------------------------------------------------------------
# Per-sample support and rejection reasons
# ---------------------------------------------------------------------------


def test_each_report_keeps_support_and_rejection_reasons():
    result = detect_consensus([_FRENCH, _GREEK])

    for report in result.samples:
        assert isinstance(report, SampleReport)
        for support in report.supported:
            assert isinstance(support, EncodingSupport)
            assert support.verdict in set(SampleVerdict)
            assert 0.0 <= support.confidence <= 1.0
        for rejection in report.rejected:
            assert rejection.reason == "undecodable"
            assert rejection.encoding not in report.compatible
        # Compatible and rejected partition the candidate set for text.
        assert not (
            set(report.compatible)
            & {rejection.encoding for rejection in report.rejected}
        )


def test_binary_sample_is_conflict_with_text_encoding_reason():
    result = detect_consensus([_FRENCH, b"\x00\x01\x02\x03" * 20])

    assert result.status is ConsensusStatus.CONFLICT
    binary_report = next(
        report for report in result.samples if report.verdict is SampleVerdict.BINARY
    )
    assert binary_report.supported == ()
    assert any(
        conflict.sample_index == binary_report.index and "binary" in conflict.reason
        for conflict in result.conflicts
    )


# ---------------------------------------------------------------------------
# Caller-controlled candidate sets
# ---------------------------------------------------------------------------


def test_include_encodings_restricts_every_per_sample_record():
    result = detect_consensus(
        [_FRENCH, _FRENCH_2],
        include_encodings=["utf-8", "windows-1252"],
    )
    allowed = {"utf-8", "cp1252"}
    for report in result.samples:
        assert set(report.compatible) <= allowed
        assert {rejection.encoding for rejection in report.rejected} <= allowed

    # The Latin bytes reject utf-8, so only cp1252 can be common.
    assert result.status is ConsensusStatus.AGREEMENT
    assert result.common_encodings == ("cp1252",)


def test_exclude_encodings_removes_candidates_everywhere():
    result = detect_consensus([_FRENCH], exclude_encodings=["utf-8"])

    for report in result.samples:
        assert "utf-8" not in report.compatible
        assert "utf-8" not in {rejection.encoding for rejection in report.rejected}
        assert "utf-8" not in {support.encoding for support in report.supported}


def test_unknown_candidate_name_raises_value_error():
    with pytest.raises(ValueError, match="include_encodings"):
        detect_consensus([_FRENCH], include_encodings=["not-a-real-codec"])


def test_encoding_era_restricts_the_candidate_set():
    era = chardet.EncodingEra.MODERN_WEB
    era_names = {info.name for info in get_candidates(era)}
    result = detect_consensus([_FRENCH, _FRENCH_2], encoding_era=era)

    for report in result.samples:
        assert set(report.compatible) <= era_names
        assert {
            rejection.encoding for rejection in report.rejected
        } <= era_names
        assert {
            support.encoding for support in report.supported
        } <= era_names
    assert set(result.common_encodings) <= era_names


# ---------------------------------------------------------------------------
# Input validation and result shape
# ---------------------------------------------------------------------------


def test_empty_sample_sequence_raises():
    with pytest.raises(ValueError, match="at least one"):
        detect_consensus([])


def test_scalar_bytes_is_rejected_not_treated_as_a_sequence_of_ints():
    with pytest.raises(TypeError, match="sequence of byte strings"):
        detect_consensus(b"raw bytes")
    with pytest.raises(TypeError, match="sequence of byte strings"):
        chardet.detect_consensus(b"raw bytes")


def test_non_bytes_element_raises_type_error():
    with pytest.raises(TypeError, match=r"samples\[0\]"):
        detect_consensus([123])  # type: ignore[list-item]


def test_generators_and_bytearrays_are_accepted():
    result = detect_consensus(bytearray(sample) for sample in [_FRENCH, _FRENCH_2])
    assert result.status is ConsensusStatus.AGREEMENT


def test_to_dict_is_json_serializable_with_string_enums():
    result = detect_consensus([_FRENCH, _GREEK_UTF8])
    payload = result.to_dict()

    encoded = json.dumps(payload)
    assert json.loads(encoded) == payload
    assert payload["status"] == ConsensusStatus.CONFLICT.value
    sample = payload["samples"][0]
    assert set(sample) == {
        "index",
        "occurrences",
        "byte_length",
        "verdict",
        "supported",
        "compatible",
        "rejected",
    }
    assert isinstance(sample["verdict"], str)


def test_result_and_reports_are_frozen():
    result = detect_consensus([_FRENCH, _FRENCH_2])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = ConsensusStatus.CONFLICT  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.samples[0].byte_length = 0  # type: ignore[misc]


def test_runs_are_deterministic():
    first = detect_consensus([_FRENCH, _GREEK, b""])
    second = detect_consensus([_FRENCH, _GREEK, b""])
    assert first == second


def test_max_bytes_is_applied_per_sample():
    result = detect_consensus([_FRENCH], max_bytes=16)
    assert result.samples[0].byte_length == 16


def test_public_api_exports_consensus_entry_point_and_types():
    assert "detect_consensus" in chardet.__all__
    for name in (
        "ConsensusResult",
        "ConsensusStatus",
        "ConsensusConflict",
        "SampleReport",
        "SampleVerdict",
        "EncodingSupport",
        "EncodingRejection",
    ):
        assert name in chardet.__all__
        assert getattr(chardet, name) is not None
    assert isinstance(chardet.detect_consensus([_FRENCH, _FRENCH_2]), ConsensusResult)
