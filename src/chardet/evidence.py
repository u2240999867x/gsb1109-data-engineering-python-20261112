"""Per-detection evidence records and policy-change replay.

A detection run answers more than "which encoding won": it also knows why
the winner won, which candidates were excluded at which gate, and which
names the public API rendered.  This module captures that record once,
during the single detection pass, as a :class:`DetectionEvidence`, and
replays hypothetical policy changes against the record *without re-reading
the original bytes* via :meth:`DetectionEvidence.can_reuse_decision`.

Two outcomes can carry the same public name while resting on opposite
evidence.  An encoding the evidence **selected** survived every gate, was
scored, and ranked first; deleting other fully-scored losers cannot change
that.  The no-match **fallback** is never checked against the data at all:
it answers only because nothing survived, so a whitelist change is safe
only when the record shows every removed candidate was itself rejected for
a recorded, byte-determined reason.  When the record cannot answer, the
:class:`ReplayVerdict` names the stage whose byte evidence must be
re-gathered.

The record deliberately holds no input bytes: it stores names, scores,
gate verdicts, and window lengths, so an archived record stays small and
a replay never turns into a second detection.
"""

from __future__ import annotations

import codecs
import dataclasses
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, cast

from chardet.enums import EncodingEra
from chardet.output_names import NARROWER_DISPLAY_NAMES, PREFERRED_SUPERSET
from chardet.registry import REGISTRY, get_candidates, normalize_encodings

if TYPE_CHECKING:
    from chardet.pipeline import DetectionDict, DetectionResult

# ---------------------------------------------------------------------------
# Vocabulary: outcome kinds, pipeline stages, and gate names
# ---------------------------------------------------------------------------

#: The winner was committed by a stage that examined the bytes.
OUTCOME_EVIDENCE = "evidence"
#: No candidate survived; the answer is the caller's no-match sentinel.
OUTCOME_FALLBACK = "no_match_fallback"
#: The input was empty; the answer is the empty-input sentinel.
OUTCOME_EMPTY = "empty_input_fallback"
#: The bytes were classified as not-text (encoding is ``None``).
OUTCOME_BINARY = "binary"

#: Deterministic early-exit and scoring stage names.
STAGE_BOM = "bom"
STAGE_UTF1632 = "utf1632_patterns"
STAGE_ESCAPE = "escape_sequences"
STAGE_MAGIC = "magic_number"
STAGE_BINARY = "binary_detection"
STAGE_MARKUP = "markup_charset"
STAGE_ASCII = "ascii"
STAGE_UTF8 = "utf8_validation"
STAGE_STATISTICAL = "statistical_scoring"

#: Gate names identify where a candidate was excluded or which stage a
#: replay must re-run when the recorded evidence cannot settle the question.
GATE_CANDIDATE_SET = "candidate_whitelist"
GATE_VALIDITY = "byte_validity"
GATE_CJK = "cjk_gate"
GATE_SCORING = "statistical_scoring"
GATE_POSTPROCESS = "postprocess"
GATE_WINDOW_HOLD = "window_decode_hold"
GATE_OUTPUT_RENDER = "output_render"

#: Rank-correction names, in postprocess execution order.
CORRECTION_DEAD_HEAT_SUPERSET = "dead_heat_superset_promotion"
CORRECTION_ERA_PREVALENCE = "era_prevalence_promotion"
CORRECTION_RARE_LANGUAGE = "rare_language_arbitration"
CORRECTION_CONFUSION = "confusion_group_resolution"
CORRECTION_NICHE_LATIN = "niche_latin_demotion"
CORRECTION_KOI8T = "koi8t_promotion"
CORRECTION_MAC_CR = "classic_mac_cr_promotion"
CORRECTION_DECODE_SAFETY = "decode_safety_flip"

#: Sentinel meaning "leave this policy at the value of the recorded run".
UNCHANGED = object()


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateVerdict:
    """One candidate's recorded fate.

    :param encoding: Canonical registry name of the candidate.
    :param gate: Where its fate was decided: a ``GATE_*`` constant for
        excluded candidates, the committing ``STAGE_*`` constant for the
        winner, or :data:`GATE_SCORING` for a candidate that reached the
        ranking.
    :param reason: Why an excluded candidate was dropped, or ``None`` for a
        candidate that survived that gate.
    :param scored: Whether the candidate received a full statistical score.
    :param score: The statistical score when :attr:`scored`, else ``None``.
    :param final_rank: Zero-based position in the final ranking, or ``None``
        when the candidate never reached it.
    """

    encoding: str
    gate: str
    reason: str | None
    scored: bool
    score: float | None
    final_rank: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class RenderEvent:
    """One output-name transform observed while rendering the public dict.

    :param transform: ``"preferred_superset"`` or ``"compat_names"``.
    :param from_name: The internal canonical name before the transform.
    :param to_name: The name after the transform (equal to *from_name* when
        it was skipped).
    :param applied: Whether the transform changed the reported name.
    :param codec_changed: Whether the new name resolves to a different
        Python codec, rather than another spelling of the same codec.
    :param decode_verified: Whether a window decode was run and passed
        (``True``), run and failed (``False``), or never involved (``None``).
    :param skip_reason: ``"whitelist"`` when filters blocked the remap,
        ``"decode_failed"`` when the window rejected it, else ``None``.
    """

    transform: str
    from_name: str | None
    to_name: str | None
    applied: bool
    codec_changed: bool
    decode_verified: bool | None
    skip_reason: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ReplayVerdict:
    """Whether a recorded decision can stand under a new policy.

    :param reusable: ``True`` when the recorded evidence alone proves the
        same decoding decision is reached without re-reading the bytes.
    :param reason: Human-readable explanation of the judgment.
    :param rerun_stage: The ``GATE_*`` / ``STAGE_*`` whose byte evidence
        must be re-gathered when not reusable, else ``None``.
    :param blocking_candidates: Candidate names that force the re-run.
    """

    reusable: bool
    reason: str
    rerun_stage: str | None = None
    blocking_candidates: tuple[str, ...] = ()

    @classmethod
    def reuse(cls, reason: str) -> ReplayVerdict:
        """Build a reusable verdict carrying *reason*."""
        return cls(reusable=True, reason=reason)

    @classmethod
    def rerun(
        cls,
        reason: str,
        stage: str,
        blocking: Iterable[str] = (),
    ) -> ReplayVerdict:
        """Build a must-rerun verdict naming the stage and blockers."""
        return cls(
            reusable=False,
            reason=reason,
            rerun_stage=stage,
            blocking_candidates=tuple(blocking),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class CorrectionEvent:
    """One rank correction that actually reordered the statistical ranking.

    :param correction: A ``CORRECTION_*`` constant.
    :param participants: Canonical names whose rank position changed when
        the correction fired; a deleted candidate outside this set could not
        have driven the correction.
    """

    correction: str
    participants: frozenset[str]


@dataclasses.dataclass(frozen=True, slots=True)
class DetectionEvidence:
    """The archived judgment credentials of one detection run.

    Built by :class:`EvidenceRecorder` during the (single) pipeline pass and
    returned alongside the normal detection result.  Holds no input bytes.

    :param encoding_era: The era flag the run used.
    :param include_encodings: The resolved include filter, if any.
    :param exclude_encodings: The resolved exclude filter, if any.
    :param admitted: Every canonical candidate name the whitelist admitted.
    :param registry_names: Every canonical name in the registry at run time,
        in registry order, with its era — the universe a future whitelist
        can draw from without becoming unknown.
    :param outcome: One of the ``OUTCOME_*`` constants.
    :param stage: The stage that committed the answer (``STAGE_*``) or the
        fallback marker.
    :param winner: Canonical name of the answer, or ``None`` for binary.
    :param fallback_encoding: The sentinel name for fallback outcomes.
    :param examined_bytes: Bytes in the examination window (``max_bytes``
        slice); the size of the data the verdict is about.
    :param evidence_bytes: Bytes the capped filtering/scoring stages
        actually consumed (the evidence cap, or the window when smaller).
    :param ranking: Final ranking as ``(encoding, confidence)`` pairs.
    :param verdicts: One :class:`CandidateVerdict` per registry entry:
        whitelist-level exclusions carry their filter reason, later gates
        carry theirs.
    :param corrections: Rank corrections that actually reordered results,
        each with the participants it moved.
    :param render: The output-name trace, when rendering was recorded.
    """

    encoding_era: EncodingEra
    include_encodings: frozenset[str] | None
    exclude_encodings: frozenset[str] | None
    admitted: frozenset[str]
    registry_names: tuple[tuple[str, EncodingEra], ...]
    outcome: str
    stage: str
    winner: str | None
    fallback_encoding: str | None
    examined_bytes: int
    evidence_bytes: int
    ranking: tuple[tuple[str, float], ...]
    verdicts: tuple[CandidateVerdict, ...]
    corrections: tuple[CorrectionEvent, ...]
    render: tuple[RenderEvent, ...]

    def verdict_for(self, encoding: str) -> CandidateVerdict | None:
        """Return the recorded verdict for *encoding*, if it was ever a candidate."""
        for verdict in self.verdicts:
            if verdict.encoding == encoding:
                return verdict
        return None

    @property
    def is_fallback(self) -> bool:
        """Whether the answer is the no-match or empty-input sentinel, not evidence."""
        return self.outcome in (OUTCOME_FALLBACK, OUTCOME_EMPTY)

    def can_reuse_decision(
        self,
        *,
        encoding_era: object = UNCHANGED,
        include_encodings: object = UNCHANGED,
        exclude_encodings: object = UNCHANGED,
        compat_names: object = UNCHANGED,
        prefer_superset: object = UNCHANGED,
    ) -> ReplayVerdict:
        """Judge whether this decision survives a policy change, from the record.

        Every argument defaults to :data:`UNCHANGED`, so a caller asks about
        only the policies it would change.  The whitelist arguments accept
        the same name/alias iterables as :func:`chardet.detect`; the output
        arguments accept booleans.

        :returns: A :class:`ReplayVerdict`; when it is not reusable, its
            ``rerun_stage`` names the exact stage whose byte evidence must
            be re-gathered.
        """
        whitelist = self._replay_whitelist(
            encoding_era, include_encodings, exclude_encodings
        )
        if isinstance(whitelist, ReplayVerdict):
            return whitelist
        new_allowed, unknown = whitelist

        verdict = self._replay_whitelist_change(new_allowed, unknown)
        if not verdict.reusable:
            return verdict
        output_verdict = self._replay_output_policy(
            new_allowed,
            compat_names,
            prefer_superset,
        )
        if not output_verdict.reusable:
            return output_verdict
        reasons = [verdict.reason, output_verdict.reason]
        return ReplayVerdict.reuse("; ".join(reasons))

    # -- whitelist replay -------------------------------------------------

    def _replay_whitelist(
        self,
        era: object,
        include: object,
        exclude: object,
    ) -> tuple[frozenset[str], tuple[str, ...]] | ReplayVerdict:
        new_era = self.encoding_era if era is UNCHANGED else era
        if not isinstance(new_era, EncodingEra):
            return ReplayVerdict.rerun(
                "the replacement encoding_era is not an EncodingEra member",
                GATE_CANDIDATE_SET,
            )
        new_include = self.include_encodings if include is UNCHANGED else include
        new_exclude = self.exclude_encodings if exclude is UNCHANGED else exclude
        include_iter = cast("Iterable[str] | None", new_include)
        exclude_iter = cast("Iterable[str] | None", new_exclude)
        try:
            canonical_include = normalize_encodings(
                None if include_iter is None else list(include_iter),
                "include_encodings",
            )
            canonical_exclude = normalize_encodings(
                None if exclude_iter is None else list(exclude_iter),
                "exclude_encodings",
            )
        except ValueError as exc:
            return ReplayVerdict.rerun(
                f"the new whitelist names an encoding outside the recorded "
                f"registry universe: {exc}",
                GATE_CANDIDATE_SET,
            )
        candidates = get_candidates(new_era, canonical_include, canonical_exclude)
        new_allowed = frozenset(enc.name for enc in candidates)
        requested = self._requested_names(canonical_include, canonical_exclude)
        registry_universe = frozenset(name for name, _era in self.registry_names)
        unknown = tuple(sorted(requested - registry_universe))
        return new_allowed, unknown

    @staticmethod
    def _requested_names(
        include: frozenset[str] | None,
        exclude: frozenset[str] | None,
    ) -> frozenset[str]:
        names: set[str] = set()
        if include is not None:
            names.update(include)
        if exclude is not None:
            names.update(exclude)
        return frozenset(names)

    def _replay_whitelist_change(
        self,
        new_allowed: frozenset[str],
        unknown: tuple[str, ...],
    ) -> ReplayVerdict:
        if unknown:
            return ReplayVerdict.rerun(
                "the new whitelist names encodings absent from the registry "
                "universe the record was built from, so no byte evidence "
                f"exists for: {', '.join(unknown)}",
                GATE_CANDIDATE_SET,
                unknown,
            )
        added = tuple(sorted(new_allowed - self.admitted))
        removed = tuple(sorted(self.admitted - new_allowed))

        if self.outcome == OUTCOME_BINARY:
            return ReplayVerdict.reuse(
                "the binary verdict is an exhaustive byte-level classification "
                "that no whitelist member can override"
            )
        if self.winner is not None and self.winner not in new_allowed:
            if self.is_fallback:
                return ReplayVerdict.rerun(
                    f"the sentinel answer {self.winner!r} is excluded by the "
                    "new whitelist, and the record contains no alternative winner",
                    GATE_VALIDITY,
                    (self.winner,),
                )
            return ReplayVerdict.rerun(
                f"the evidence-selected winner {self.winner!r} is excluded by "
                "the new whitelist; a new winner must be re-scored",
                self.stage if self.stage != STAGE_STATISTICAL else GATE_SCORING,
                (self.winner,),
            )
        if added:
            if self.outcome == OUTCOME_EVIDENCE and self.stage != STAGE_STATISTICAL:
                return ReplayVerdict.reuse(
                    f"the {self.stage} stage committed deterministically before "
                    "candidate scoring; an added whitelist member cannot preempt it"
                )
            return ReplayVerdict.rerun(
                "newly admitted candidates were never examined for this input and "
                f"could decode and outrank the recorded answer: {', '.join(added)}",
                GATE_VALIDITY,
                added,
            )
        if not removed:
            return ReplayVerdict.reuse("the candidate whitelist is unchanged")
        if self.is_fallback:
            return self._fallback_removal_verdict(removed)
        return self._evidence_removal_verdict(removed)

    def _fallback_removal_verdict(self, removed: tuple[str, ...]) -> ReplayVerdict:
        if self.outcome == OUTCOME_EMPTY:
            return ReplayVerdict.reuse(
                "empty input decodes under every codec, so the empty-input "
                "sentinel is selected by policy, not by any candidate's evidence"
            )
        unexplained: list[str] = []
        for name in removed:
            verdict = self.verdict_for(name)
            if verdict is None or not self._has_exclusion_boundary(verdict):
                unexplained.append(name)
        if unexplained:
            return ReplayVerdict.rerun(
                "the fallback answer rests on no positive evidence, and the "
                "record cannot prove the removed candidates were already "
                f"rejected: {', '.join(unexplained)}",
                GATE_VALIDITY,
                unexplained,
            )
        return ReplayVerdict.reuse(
            "every removed candidate has a recorded byte-level exclusion "
            f"({self._exclusion_summary(removed)}); removing them cannot create "
            "a survivor, so the fallback triggers again"
        )

    @staticmethod
    def _has_exclusion_boundary(verdict: CandidateVerdict) -> bool:
        """Whether the record proves the candidate could not have won.

        A gate reason is a byte-determined exclusion; a pruned candidate's
        upper bound sat below the scoring contract floor, which guarantees
        it could affect neither the winner nor any rank correction.
        """
        return verdict.reason is not None

    def _evidence_removal_verdict(self, removed: tuple[str, ...]) -> ReplayVerdict:
        if self.stage != STAGE_STATISTICAL:
            return ReplayVerdict.reuse(
                f"the {self.stage} verdict is independent of the other "
                "whitelist members, all of which outlived it only as non-winners"
            )
        unexplained = tuple(
            name
            for name in removed
            if (verdict := self.verdict_for(name)) is None
            or (not verdict.scored and not self._has_exclusion_boundary(verdict))
        )
        if unexplained:
            return ReplayVerdict.rerun(
                "the record holds neither a score nor an exclusion boundary for "
                f"the removed candidate(s): {', '.join(unexplained)}",
                GATE_POSTPROCESS,
                unexplained,
            )
        unscored = tuple(
            name
            for name in removed
            if (verdict := self.verdict_for(name)) is not None
            and not verdict.scored
            and verdict.reason is not None
        )
        scored_losers = tuple(
            name
            for name in removed
            if (verdict := self.verdict_for(name)) is not None and verdict.scored
        )
        notes: list[str] = []
        if unscored:
            notes.append(
                f"{len(unscored)} removed candidate(s) never competed for the "
                f"top ({self._exclusion_summary(unscored)})"
            )
        if scored_losers:
            notes.append(
                f"{len(scored_losers)} removed candidate(s) were fully scored "
                "and finished below the recorded winner"
            )
        if self.corrections:
            notes.append(
                "the fired rank corrections only reordered recorded losers "
                "under the same bytes"
            )
        return ReplayVerdict.reuse(
            "; ".join(notes)
            + "; shrinking the candidate set cannot move a recorded loser above "
            "the winner, which keeps every score it earned"
        )

    # -- output policy replay ---------------------------------------------

    def _replay_output_policy(
        self,
        new_allowed: frozenset[str],
        compat_names: object,
        prefer_superset: object,
    ) -> ReplayVerdict:
        if self.winner is None:
            return ReplayVerdict.reuse(
                "a binary result carries no encoding name to remap"
            )
        compat_verdict = self._replay_compat_names(compat_names)
        if not compat_verdict.reusable:
            return compat_verdict
        superset_verdict = self._replay_prefer_superset(prefer_superset, new_allowed)
        if not superset_verdict.reusable:
            return superset_verdict
        reasons = [compat_verdict.reason, superset_verdict.reason]
        return ReplayVerdict.reuse("; ".join(reasons))

    def _replay_compat_names(self, compat_names: object) -> ReplayVerdict:
        if compat_names is UNCHANGED:
            return ReplayVerdict.reuse("compat_names policy unchanged")
        if self.winner is None:
            return ReplayVerdict.reuse("no display name applies to a binary result")
        same_codec_either_way = (
            codecs.lookup(self.winner).name
            == codecs.lookup(self._compat_display(self.winner)).name
        )
        if same_codec_either_way:
            return ReplayVerdict.reuse(
                f"the display name of {self.winner!r} is another alias of the "
                "same codec, so renaming changes no decoding decision"
            )
        if not compat_names:
            return ReplayVerdict.reuse(
                "turning display names off restores the pipeline-validated "
                f"internal codec {self.winner!r}"
            )
        event = self._render_event("compat_names")
        if event is not None and event.applied and event.decode_verified:
            return ReplayVerdict.reuse(
                "the recorded render trace already verified the narrower display "
                "codec against the examined window"
            )
        return ReplayVerdict.rerun(
            f"{self.winner!r} displays as a narrower codec, and no window decode "
            "of that codec is on record",
            GATE_OUTPUT_RENDER,
        )

    def _replay_prefer_superset(
        self, prefer_superset: object, new_allowed: frozenset[str]
    ) -> ReplayVerdict:
        if prefer_superset is UNCHANGED:
            return ReplayVerdict.reuse("prefer_superset policy unchanged")
        if self.winner is None:
            return ReplayVerdict.reuse("no superset remap applies to a binary result")
        superset = PREFERRED_SUPERSET.get(self.winner)
        if superset is None:
            return ReplayVerdict.reuse(
                f"{self.winner!r} has no preferred-superset entry, so the flag "
                "does not affect it"
            )
        if not prefer_superset:
            return ReplayVerdict.reuse(
                "turning the remap off restores the pipeline-validated internal "
                f"name {self.winner!r}"
            )
        if superset not in new_allowed:
            return ReplayVerdict.reuse(
                f"the superset {superset!r} is outside the new whitelist, so the "
                "remap is skipped and the internal name stands"
            )
        event = self._render_event("preferred_superset")
        if event is not None and event.applied and event.decode_verified:
            return ReplayVerdict.reuse(
                f"the recorded run already verified that {superset!r} decodes "
                "the examined window"
            )
        target = self.verdict_for(superset)
        if (
            target is not None
            and target.reason == "fails_window_decode"
            and target.gate == GATE_VALIDITY
        ):
            return ReplayVerdict.reuse(
                f"the record shows {superset!r} cannot decode the examined "
                "window, so the remap would be skipped and the internal name stands"
            )
        return ReplayVerdict.rerun(
            f"remapping {self.winner!r} to the different codec {superset!r} "
            "requires a window decode the record does not hold",
            GATE_OUTPUT_RENDER,
        )

    def _render_event(self, transform: str) -> RenderEvent | None:
        for event in self.render:
            if event.transform == transform:
                return event
        return None

    @staticmethod
    def _compat_display(encoding: str) -> str:
        from chardet.output_names import _COMPAT_NAMES  # noqa: PLC0415

        return _COMPAT_NAMES.get(encoding, encoding)

    def _exclusion_summary(self, names: Iterable[str]) -> str:
        parts: list[str] = []
        for name in sorted(names):
            verdict = self.verdict_for(name)
            if verdict is not None and verdict.reason is not None:
                parts.append(f"{name}@{verdict.gate}:{verdict.reason}")
        return ", ".join(parts)


@dataclasses.dataclass(frozen=True, slots=True)
class DetectionReport:
    """A detection dict paired with its :class:`DetectionEvidence`."""

    result: DetectionDict
    evidence: DetectionEvidence


class EvidenceRecorder:
    """Mutable per-run builder of :class:`DetectionEvidence`.

    The orchestrator owns one instance when the caller asked for evidence;
    the default detection path constructs none and pays nothing.  Methods
    tolerate being called in any order the pipeline takes and freeze into an
    immutable record at :meth:`build`.
    """

    def __init__(  # noqa: PLR0913
        self,
        era: EncodingEra,
        include: frozenset[str] | None,
        exclude: frozenset[str] | None,
        registry: tuple[tuple[str, EncodingEra], ...],
        admitted: tuple[str, ...],
        *,
        examined_bytes: int,
        evidence_bytes: int,
        no_match_encoding: str,
        empty_input_encoding: str,
    ) -> None:
        """Initialize the per-run verdict table from the whitelist decision."""
        self._era = era
        self._include = include
        self._exclude = exclude
        self._examined = examined_bytes
        self._evidence = evidence_bytes
        self._no_match = no_match_encoding
        self._empty = empty_input_encoding
        self._registry = registry
        admitted_set = frozenset(admitted)
        self._admitted = admitted_set
        self._verdicts: dict[str, CandidateVerdict] = {}
        for name, candidate_era in registry:
            if name in admitted_set:
                gate = GATE_CANDIDATE_SET
                reason = None
            elif not candidate_era & era:
                gate = GATE_CANDIDATE_SET
                reason = "outside_encoding_era"
            elif include is not None and name not in include:
                gate = GATE_CANDIDATE_SET
                reason = "not_in_include_encodings"
            else:
                gate = GATE_CANDIDATE_SET
                reason = "in_exclude_encodings"
            self._verdicts[name] = CandidateVerdict(
                name, gate, reason, scored=False, score=None, final_rank=None
            )
        self._outcome: str = OUTCOME_EVIDENCE
        self._stage: str = STAGE_STATISTICAL
        self._winner: str | None = None
        self._fallback: str | None = None
        self._ranking: tuple[tuple[str, float], ...] = ()
        self._corrections: list[CorrectionEvent] = []
        self._render: list[RenderEvent] = []

    # -- pipeline hooks ---------------------------------------------------

    def record_early_exit(self, winner: str | None, stage: str) -> None:
        """Record a deterministic stage committing *winner* (or binary)."""
        self._outcome = OUTCOME_BINARY if winner is None else OUTCOME_EVIDENCE
        self._stage = stage
        self._winner = winner
        if winner is not None and winner in self._verdicts:
            self._verdicts[winner] = dataclasses.replace(
                self._verdicts[winner], gate=stage
            )
        self._ranking = ((winner, 0.0),) if winner is not None else ()

    def record_fallback(self, encoding: str | None, *, empty: bool) -> None:
        """Record the no-match (or empty-input) sentinel outcome."""
        self._outcome = OUTCOME_EMPTY if empty else OUTCOME_FALLBACK
        self._stage = OUTCOME_EMPTY if empty else OUTCOME_FALLBACK
        self._winner = encoding
        self._fallback = encoding
        self._ranking = ((encoding, 0.10),) if encoding is not None else ()

    def record_validity(self, survivors: tuple[str, ...]) -> None:
        """Mark every non-surviving admitted candidate a validity failure."""
        surviving = frozenset(survivors)
        for name in self._verdicts:
            if name not in surviving and self._verdicts[name].reason is None:
                self._verdicts[name] = dataclasses.replace(
                    self._verdicts[name],
                    gate=GATE_VALIDITY,
                    reason="fails_window_decode",
                )

    def record_exclusion(self, encoding: str, gate: str, reason: str) -> None:
        """Record that *encoding* was dropped at *gate* for *reason*."""
        if encoding in self._verdicts:
            self._verdicts[encoding] = CandidateVerdict(
                encoding, gate, reason, scored=False, score=None, final_rank=None
            )

    def record_scoring(
        self,
        candidate_names: tuple[str, ...],
        scored: tuple[tuple[str, float], ...],
        *,
        full_ranking: bool,
    ) -> None:
        """Attach statistical scores, or the pruning boundary, per candidate."""
        scored_map = dict(scored)
        for name in candidate_names:
            verdict = self._verdicts[name]
            if name in scored_map:
                self._verdicts[name] = CandidateVerdict(
                    name,
                    GATE_SCORING,
                    None,
                    scored=True,
                    score=scored_map[name],
                    final_rank=None,
                )
            elif verdict.reason is None:
                self._verdicts[name] = CandidateVerdict(
                    name,
                    GATE_SCORING,
                    "no_nonzero_score"
                    if full_ranking
                    else "pruned_below_scoring_floor",
                    scored=False,
                    score=None,
                    final_rank=None,
                )

    def record_correction(self, correction: str, participants: Iterable[str]) -> None:
        """Append a fired rank correction, merging with an identical last one."""
        names = frozenset(participants)
        if self._corrections and self._corrections[-1].correction == correction:
            previous = self._corrections[-1]
            merged = CorrectionEvent(correction, previous.participants | names)
            self._corrections[-1] = merged
        else:
            self._corrections.append(CorrectionEvent(correction, names))

    def record_hold_dropped(self, names: tuple[str, ...]) -> None:
        """Record candidates the whole-window decode hold dropped."""
        for name in names:
            if name in self._verdicts:
                self._verdicts[name] = dataclasses.replace(
                    self._verdicts[name],
                    gate=GATE_WINDOW_HOLD,
                    reason="winner_decodes_more_of_window",
                )

    def record_final_ranking(
        self, ranking: tuple[tuple[str | None, float], ...]
    ) -> None:
        """Freeze the final order and stamp each survivor with its rank."""
        clean: list[tuple[str, float]] = []
        for rank, (name, confidence) in enumerate(ranking):
            if name is None:
                continue
            clean.append((name, confidence))
            if name in self._verdicts:
                verdict = self._verdicts[name]
                self._verdicts[name] = dataclasses.replace(verdict, final_rank=rank)
        self._ranking = tuple(clean)
        if self._winner is None and clean:
            self._winner = clean[0][0]

    def record_render(self, events: Iterable[RenderEvent]) -> None:
        """Attach the output-name transform trace."""
        self._render.extend(events)

    # -- freeze -----------------------------------------------------------

    def build(self) -> DetectionEvidence:
        """Freeze the recorded hooks into an immutable evidence record."""
        return DetectionEvidence(
            encoding_era=self._era,
            include_encodings=self._include,
            exclude_encodings=self._exclude,
            admitted=self._admitted,
            registry_names=self._registry,
            outcome=self._outcome,
            stage=self._stage,
            winner=self._winner,
            fallback_encoding=self._fallback,
            examined_bytes=self._examined,
            evidence_bytes=self._evidence,
            ranking=self._ranking,
            verdicts=tuple(self._verdicts[name] for name in sorted(self._verdicts)),
            corrections=tuple(self._corrections),
            render=tuple(self._render),
        )


def registry_snapshot() -> tuple[tuple[str, EncodingEra], ...]:
    """Return ``(name, era)`` for every registry entry, in registry order."""
    return tuple((name, info.era) for name, info in REGISTRY.items())


def _ranking_order(results: list) -> tuple[str | None, ...]:
    return tuple(r.encoding for r in results)


def _moved_participants(
    before: tuple[str | None, ...],
    after: tuple[str | None, ...],
) -> frozenset[str]:
    return frozenset(
        enc
        for enc in (*before, *after)
        if enc is not None and before.index(enc) != after.index(enc)
    )


def apply_postprocess_with_evidence(
    recorder: EvidenceRecorder,
    data: bytes,
    results: list,
    *,
    input_truncated: bool,
) -> list:
    """Run the postprocess chain one correction at a time with recording.

    Mirrors :func:`chardet.pipeline.postprocess.postprocess_results` step
    for step; the only addition is an order comparison after each step, so
    a correction that actually reorders results records the candidates it
    moved and a no-op correction records nothing.  Kept here rather than in
    the mypyc-compiled postprocess module so the evidence path costs the
    hot path nothing.
    """
    from chardet.pipeline import postprocess as pp  # noqa: PLC0415

    def rare_language(_data: bytes, current: list) -> list:
        return pp._arbitrate_rare_language(  # noqa: SLF001
            current
        )

    correction_fn = Callable[[bytes, list["DetectionResult"]], list["DetectionResult"]]
    chain: tuple[tuple[str, correction_fn], ...] = (
        (CORRECTION_DEAD_HEAT_SUPERSET, pp._promote_superset_on_dead_heat),  # noqa: SLF001
        (CORRECTION_ERA_PREVALENCE, pp._prefer_prevalent_on_dead_heat),  # noqa: SLF001
        (CORRECTION_RARE_LANGUAGE, rare_language),
        (CORRECTION_CONFUSION, pp.resolve_confusion_groups),
        (CORRECTION_NICHE_LATIN, pp._demote_niche_latin),  # noqa: SLF001
        (CORRECTION_KOI8T, pp._promote_koi8t),  # noqa: SLF001
        (CORRECTION_MAC_CR, pp._promote_mac_on_cr_line_endings),  # noqa: SLF001
    )
    for name, correction in chain:
        before = _ranking_order(results)
        results = correction(data, results)
        after = _ranking_order(results)
        if before != after:
            recorder.record_correction(name, _moved_participants(before, after))
    before = _ranking_order(results)
    results = pp._prefer_decodable_on_tie(  # noqa: SLF001
        data, results, input_truncated=input_truncated
    )
    after = _ranking_order(results)
    if before != after:
        recorder.record_correction(
            CORRECTION_DECODE_SAFETY, _moved_participants(before, after)
        )
    return results


def build_render_events(  # noqa: PLR0913
    *,
    internal_name: str | None,
    window: bytes,
    prefer_superset: bool,
    compat_names: bool,
    include_encodings: frozenset[str] | None,
    exclude_encodings: frozenset[str] | None,
    encoding_era: EncodingEra,
) -> tuple[RenderEvent, ...]:
    """Reconstruct the output-name trace of rendering one top result.

    Rendering is deterministic from the request options and the frozen
    tables in :mod:`chardet.output_names`, so the trace is rebuilt here
    without touching the render path ordinary detection uses.  Decode
    checks run only because the caller explicitly asked for an evidence
    record.
    """
    from chardet._utils import decodes_without_error  # noqa: PLC0415
    from chardet.output_names import _COMPAT_NAMES  # noqa: PLC0415

    if internal_name is None:
        return ()
    events: list[RenderEvent] = []

    superset = PREFERRED_SUPERSET.get(internal_name)
    superset_applied = False
    if prefer_superset and superset is not None:
        blocked = (
            (include_encodings is not None and superset not in include_encodings)
            or (exclude_encodings is not None and superset in exclude_encodings)
            or not (REGISTRY[superset].era & encoding_era)
        )
        codec_changed = (
            codecs.lookup(internal_name).name != codecs.lookup(superset).name
        )
        if blocked:
            events.append(
                RenderEvent(
                    "preferred_superset",
                    internal_name,
                    internal_name,
                    applied=False,
                    codec_changed=codec_changed,
                    decode_verified=None,
                    skip_reason="whitelist",
                )
            )
        elif decodes_without_error(window, superset):
            superset_applied = True
            events.append(
                RenderEvent(
                    "preferred_superset",
                    internal_name,
                    superset,
                    applied=True,
                    codec_changed=codec_changed,
                    decode_verified=True,
                    skip_reason=None,
                )
            )
        else:
            events.append(
                RenderEvent(
                    "preferred_superset",
                    internal_name,
                    internal_name,
                    applied=False,
                    codec_changed=codec_changed,
                    decode_verified=False,
                    skip_reason="decode_failed",
                )
            )

    if prefer_superset and superset_applied and superset is not None:
        current: str = superset
    else:
        current = internal_name
    if compat_names:
        display = _COMPAT_NAMES.get(current, current)
        codec_changed = codecs.lookup(current).name != codecs.lookup(display).name
        if display != current and current in NARROWER_DISPLAY_NAMES:
            verified = decodes_without_error(window, display)
            events.append(
                RenderEvent(
                    "compat_names",
                    current,
                    display if verified else current,
                    applied=verified,
                    codec_changed=codec_changed,
                    decode_verified=verified,
                    skip_reason=None if verified else "decode_failed",
                )
            )
        else:
            events.append(
                RenderEvent(
                    "compat_names",
                    current,
                    display,
                    applied=display != current,
                    codec_changed=codec_changed,
                    decode_verified=None,
                    skip_reason=None,
                )
            )
    return tuple(events)
