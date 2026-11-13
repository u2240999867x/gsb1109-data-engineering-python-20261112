# tests/test_admission_decision.py
"""Pruning's effect on the admit-vs-manual-review business decision.

The existing pruning tests pin the statistical facts: the pruned and full
scorers keep the same winner and position 1, and everything a rank
correction can examine carries its exact full-ranking score.  This module
asks the downstream question those facts exist to protect — given the same
post-processing, does a candidate the scorer might have skipped change
whether a caller would admit the bytes automatically or route them to
manual review?

The comparison uses two independent references built from the *full*
candidate set:

* the **reference** ranking — every candidate scored, then run through the
  real :func:`postprocess_results`;
* a **controlled stand-in** — the same ranking with exactly one candidate
  removed, run through the same post-processing.  It models a pruner that
  leaves one fewer candidate at the boundary without changing any score.

A finite, explicitly listed byte domain keeps the search tiny and
deterministic.  Inside the domain every sample has far fewer distinct
bigrams than the prescreen threshold, so production's pruned and full
paths run the identical ``_score_all`` code and cannot diverge; the
stand-in is therefore the only way to exhibit a decision flip, and it does
so through the single rank correction that is deliberately outside the
pruning contract — the decode-safety flip.
"""

from __future__ import annotations

import dataclasses

import pytest

from chardet.enums import EncodingEra
from chardet.models import BigramProfile
from chardet.output_names import decodes_under_public_name
from chardet.pipeline import DetectionResult
from chardet.pipeline.postprocess import postprocess_results, scoring_floor
from chardet.pipeline.statistical import (
    _MIN_NONZERO_FOR_PRESCREEN,
    score_candidates,
)
from chardet.registry import get_candidates

# ---------------------------------------------------------------------------
# The explicit finite sample domain
# ---------------------------------------------------------------------------

#: Printable lowercase letters and the space — the only ASCII bytes a
#: domain sample may use.
_ALPHABET_BYTES: bytes = b"abcdefghijklmnopqrstuvwxyz "

#: The single trailing high byte each sample carries.  It is invalid as a
#: complete UTF-8 sequence (a lead byte with no continuation), which is
#: what lets ``utf-8`` win the raw statistics yet fail the decode-safety
#: check while every single-byte rival decodes.
_TAIL_BYTES: tuple[int, ...] = (0xC4, 0xD6, 0xE1, 0xE9, 0xF6, 0xFC)

#: Inclusive upper bound on total sample length (ASCII prefix + tail byte).
_MAX_SAMPLE_LEN = 8

#: Prefixes are prefixes of these words, so every enumerated byte is drawn
#: from the finite table above rather than generated ad hoc.
_SEED_WORDS: tuple[bytes, ...] = (
    b"the quick brown fox",
    b"hello world test",
    b"plain english text",
    b"she sells sea shells",
    b"detection on bytes",
    b"a short small note",
)

#: The sample the bounded, fully deterministic search in
#: :func:`_search_flip_candidates` converges on: widest gap interval, then
#: shortest, then lexicographically smallest.
_PINNED_SAMPLE: bytes = b"plain \xc4"

#: The last candidate whose presence still moves the decision for the
#: pinned sample.  It is the first decodable rival in the raw ranking and
#: the target the decode-safety flip promotes.
_PINNED_PIVOT = "koi8-t"


def _domain_samples() -> list[bytes]:
    """Enumerate every (prefix, tail) sample in the finite domain."""
    alphabet = set(_ALPHABET_BYTES)
    samples: list[bytes] = []
    for word in _SEED_WORDS:
        assert set(word) <= alphabet
        # length counts the ASCII prefix only; adding one tail byte keeps
        # the total at or below _MAX_SAMPLE_LEN.
        for length in range(2, _MAX_SAMPLE_LEN):
            prefix = word[:length]
            for tail in _TAIL_BYTES:
                sample = prefix + bytes((tail,))
                assert len(sample) <= _MAX_SAMPLE_LEN
                assert set(sample) <= alphabet | set(_TAIL_BYTES)
                samples.append(sample)
    return samples


def _decodable(results: list[DetectionResult], data: bytes) -> list[DetectionResult]:
    """Results whose public name decodes the whole input, best score first."""
    return sorted(
        (
            r
            for r in results
            if r.encoding is not None
            and decodes_under_public_name(data, r.encoding, complete=True)
        ),
        key=lambda r: r.confidence,
        reverse=True,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _FlipCase:
    """One domain sample where dropping the pivot flips the postprocessed winner."""

    sample: bytes
    pivot: str
    reference_winner: str
    stand_in_winner: str
    reference_margin: float
    stand_in_margin: float

    @property
    def interval_width(self) -> float:
        """Width of the gap-threshold band that separates the two decisions."""
        return self.stand_in_margin - self.reference_margin


def _evaluate(sample: bytes, candidates: tuple) -> _FlipCase | None:
    """Return a flip case for *sample*, or ``None`` when it does not qualify."""
    full = score_candidates(sample, candidates, full_ranking=True)
    pruned = score_candidates(sample, candidates)
    if len(full) < 4 or len(pruned) < 4:
        return None
    # The raw number one must not move between the two production scorers.
    if full[0].encoding != "utf-8" or pruned[0].encoding != "utf-8":
        return None
    if [(r.encoding, r.confidence) for r in full[:2]] != [
        (r.encoding, r.confidence) for r in pruned[:2]
    ]:
        return None

    reference = postprocess_results(sample, full, input_truncated=False)
    if reference[0].encoding == "utf-8":
        return None  # the decode-safety flip did not engage

    decodable = _decodable(full, sample)
    if len(decodable) < 3:
        return None
    pivot = decodable[0]

    # The controlled stand-in: the identical ranking minus the pivot.
    stand_in_ranking = [r for r in full if r.encoding != pivot.encoding]
    if stand_in_ranking[0].encoding != "utf-8":
        return None  # removing the pivot changed the raw winner — not allowed
    stand_in = postprocess_results(sample, stand_in_ranking, input_truncated=False)
    if stand_in[0].encoding == reference[0].encoding:
        return None  # no decision flip

    reference_decodable = _decodable(reference, sample)
    stand_in_decodable = _decodable(stand_in, sample)
    reference_margin = (
        reference_decodable[0].confidence - reference_decodable[1].confidence
    )
    stand_in_margin = (
        stand_in_decodable[0].confidence - stand_in_decodable[1].confidence
    )
    if stand_in_margin <= reference_margin:
        return None  # no non-empty gap-threshold interval
    return _FlipCase(
        sample=sample,
        pivot=pivot.encoding or "",
        reference_winner=reference[0].encoding or "",
        stand_in_winner=stand_in[0].encoding or "",
        reference_margin=reference_margin,
        stand_in_margin=stand_in_margin,
    )


def _search_flip_candidates() -> list[_FlipCase]:
    """Score every domain sample and keep the ones that exhibit a flip."""
    candidates = get_candidates(EncodingEra.ALL)
    cases = [
        case
        for sample in _domain_samples()
        if (case := _evaluate(sample, candidates)) is not None
    ]
    # Widest gap interval first, then shortest sample, then lexicographic —
    # a total, dependency-free order the pinned constants must match.
    cases.sort(key=lambda c: (-c.interval_width, len(c.sample), c.sample))
    return cases


# ---------------------------------------------------------------------------
# The two business gates under comparison
# ---------------------------------------------------------------------------


def _confidence_gate(winner: DetectionResult, threshold: float) -> bool:
    """Admit when the winner's own confidence clears *threshold*."""
    return winner.confidence >= threshold


def _gap_gate(
    top: DetectionResult, runner_up: DetectionResult, threshold: float
) -> bool:
    """Admit when the winner's lead over the runner-up clears *threshold*."""
    return top.confidence - runner_up.confidence >= threshold


# ---------------------------------------------------------------------------
# Domain equivalence: production cannot diverge inside the finite domain
# ---------------------------------------------------------------------------


def test_domain_stays_below_the_prescreen_so_scoring_paths_are_equivalent():
    """Every domain sample takes the shared unpruned scoring path.

    With fewer than ``_MIN_NONZERO_FOR_PRESCREEN`` distinct bigrams,
    ``score_candidates`` calls ``_score_all`` for both its pruned and full
    modes, so the two production scorers return identical rankings
    throughout the domain — the equivalence the controlled stand-in relies
    on.
    """
    max_nonzero = 0
    for sample in _domain_samples():
        max_nonzero = max(max_nonzero, len(BigramProfile(sample).nonzero))
    assert max_nonzero < _MIN_NONZERO_FOR_PRESCREEN

    candidates = get_candidates(EncodingEra.ALL)
    for sample in _domain_samples():
        full = score_candidates(sample, candidates, full_ranking=True)
        pruned = score_candidates(sample, candidates)
        assert full == pruned


# ---------------------------------------------------------------------------
# The independent full-candidate reference
# ---------------------------------------------------------------------------


def test_full_candidate_reference_keeps_utf8_first_then_flips_on_decode_safety():
    """The reference ranking's raw winner is utf-8; post-processing overturns it.

    The trailing high byte is an incomplete UTF-8 sequence, so utf-8 tops
    the bigram statistics but cannot decode the whole input.  The
    decode-safety flip — the one correction outside the pruning floor —
    hands the win to the best decodable rival.  Marking the input
    truncated stands that flip down and leaves utf-8 on top.
    """
    candidates = get_candidates(EncodingEra.ALL)
    full = score_candidates(_PINNED_SAMPLE, candidates, full_ranking=True)
    assert full[0].encoding == "utf-8"

    pivot = _decodable(full, _PINNED_SAMPLE)[0]
    assert pivot.encoding == _PINNED_PIVOT
    # Production pruning must keep this candidate exactly: it sits at or
    # above the contract floor the pruner honors.
    floor = scoring_floor(full[0].confidence, full[1].confidence)
    assert pivot.confidence >= floor
    pruned_by_enc = {
        r.encoding: r for r in score_candidates(_PINNED_SAMPLE, candidates)
    }
    assert _PINNED_PIVOT in pruned_by_enc
    assert pruned_by_enc[_PINNED_PIVOT].confidence == pytest.approx(pivot.confidence)

    reference = postprocess_results(_PINNED_SAMPLE, full, input_truncated=False)
    assert reference[0].encoding == _PINNED_PIVOT

    truncated = postprocess_results(_PINNED_SAMPLE, full, input_truncated=True)
    assert truncated[0].encoding == "utf-8"


# ---------------------------------------------------------------------------
# The controlled stand-in: one fewer boundary candidate flips the decision
# ---------------------------------------------------------------------------


def _reference_and_stand_in() -> tuple[
    list[DetectionResult], list[DetectionResult], list[DetectionResult]
]:
    """Build (full raw ranking, reference result, stand-in result) for the pin."""
    candidates = get_candidates(EncodingEra.ALL)
    full = score_candidates(_PINNED_SAMPLE, candidates, full_ranking=True)
    reference = postprocess_results(_PINNED_SAMPLE, full, input_truncated=False)
    stand_in_ranking = [r for r in full if r.encoding != _PINNED_PIVOT]
    stand_in = postprocess_results(
        _PINNED_SAMPLE, stand_in_ranking, input_truncated=False
    )
    return full, reference, stand_in


def test_stand_in_keeps_the_raw_winner_but_flips_the_postprocessed_decision():
    """Removing only the pivot leaves utf-8 first yet changes the winner.

    This is the seam the test exists to catch: no score changes and the
    raw number one is unchanged, but the controlled stand-in promotes the
    next decodable rival instead.  The promoted rival inherits utf-8's top
    confidence, so the two decisions report the same confidence value even
    though they name different encodings.
    """
    full, reference, stand_in = _reference_and_stand_in()

    stand_in_ranking = [r for r in full if r.encoding != _PINNED_PIVOT]
    assert stand_in_ranking[0].encoding == "utf-8"
    assert reference[0].encoding == _PINNED_PIVOT
    assert stand_in[0].encoding != reference[0].encoding
    assert stand_in[0].confidence == pytest.approx(reference[0].confidence)


def test_pivot_is_the_unique_last_candidate_that_still_moves_the_decision():
    """No other single-candidate removal changes the reference winner.

    The pivot is therefore the *last* candidate the decision depends on:
    the lower-scoring tail can differ item by item without affecting the
    business call.
    """
    full, reference, _ = _reference_and_stand_in()
    for victim in _decodable(full, _PINNED_SAMPLE):
        if victim.encoding == _PINNED_PIVOT:
            continue
        reduced = [r for r in full if r.encoding != victim.encoding]
        outcome = postprocess_results(_PINNED_SAMPLE, reduced, input_truncated=False)
        assert outcome[0].encoding == reference[0].encoding


def test_low_scoring_tail_is_not_part_of_the_decision():
    """Trimming every entry below the top decodable rivals changes neither call."""
    full, reference, stand_in = _reference_and_stand_in()
    decodable = _decodable(full, _PINNED_SAMPLE)
    keep = {
        "utf-8",
        decodable[0].encoding,
        decodable[1].encoding,
        decodable[2].encoding,
    }
    minimal = [r for r in full if r.encoding in keep]
    minimal_reference = postprocess_results(
        _PINNED_SAMPLE, minimal, input_truncated=False
    )
    minimal_stand_in = postprocess_results(
        _PINNED_SAMPLE,
        [r for r in minimal if r.encoding != _PINNED_PIVOT],
        input_truncated=False,
    )
    assert minimal_reference[0].encoding == reference[0].encoding
    assert minimal_stand_in[0].encoding == stand_in[0].encoding


# ---------------------------------------------------------------------------
# Confidence threshold vs. candidate-gap threshold
# ---------------------------------------------------------------------------


def test_confidence_gate_cannot_distinguish_the_two_decisions():
    """A confidence-only threshold admits or reviews both outcomes together.

    The flip carries the top confidence onto whichever rival wins, so the
    winner's confidence is invariant; sweeping the threshold across that
    value never separates the reference from the stand-in.
    """
    _, reference, stand_in = _reference_and_stand_in()
    winner_confidence = reference[0].confidence
    assert stand_in[0].confidence == pytest.approx(winner_confidence)
    for offset in (-0.05, -1e-9, 0.0, 1e-9, 0.05):
        threshold = winner_confidence + offset
        assert _confidence_gate(reference[0], threshold) == _confidence_gate(
            stand_in[0], threshold
        )


def test_gap_gate_separates_the_decisions_over_a_recorded_interval():
    """A gap threshold inside the recorded band reviews one and admits the other.

    The reference winner leads its runner-up by ``reference_margin``; the
    stand-in winner, facing a weaker runner-up, leads by
    ``stand_in_margin``.  Any threshold in the open interval between them
    sends the reference to manual review while admitting the stand-in.
    """
    _, reference, stand_in = _reference_and_stand_in()
    reference_top = _decodable(reference, _PINNED_SAMPLE)
    stand_in_top = _decodable(stand_in, _PINNED_SAMPLE)
    reference_margin = reference_top[0].confidence - reference_top[1].confidence
    stand_in_margin = stand_in_top[0].confidence - stand_in_top[1].confidence
    assert reference_margin < stand_in_margin

    # Below or at the reference margin both clear the gate ...
    assert _gap_gate(reference_top[0], reference_top[1], reference_margin)
    assert _gap_gate(stand_in_top[0], stand_in_top[1], reference_margin)
    # ... inside the band only the stand-in still clears it ...
    middle = (reference_margin + stand_in_margin) / 2
    assert not _gap_gate(reference_top[0], reference_top[1], middle)
    assert _gap_gate(stand_in_top[0], stand_in_top[1], middle)
    # ... and above the stand-in margin neither does.
    assert not _gap_gate(reference_top[0], reference_top[1], stand_in_margin + 1e-12)
    assert not _gap_gate(stand_in_top[0], stand_in_top[1], stand_in_margin + 1e-12)


# ---------------------------------------------------------------------------
# The bounded search itself
# ---------------------------------------------------------------------------


def test_bounded_search_finds_and_pins_the_minimal_decision_flip():
    """The deterministic domain search converges on the pinned sample/pivot.

    Running the search rather than hard-coding the outcome keeps the pin
    honest: the constants must be what the finite, explicitly listed domain
    actually selects under the documented tie-breaking rule.
    """
    cases = _search_flip_candidates()
    assert cases  # the domain must contain at least one real flip
    best = cases[0]
    assert best.sample == _PINNED_SAMPLE
    assert best.pivot == _PINNED_PIVOT
    assert best.reference_winner == _PINNED_PIVOT
    assert best.stand_in_winner != best.reference_winner
    # The recorded threshold interval is non-empty and straddles a
    # threshold that separates the two business decisions.
    assert best.reference_margin < best.stand_in_margin
