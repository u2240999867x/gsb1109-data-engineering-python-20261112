"""Cross-sample encoding consensus over independently analyzed byte samples.

:func:`chardet.detect` answers for one byte string.  A supplier often hands
over a dozen small files that ought to share one encoding, and guessing each
one separately produces mutually contradictory answers: short samples give
the statistical stages little to work with, so the per-file winner is largely
noise.  This module answers a different question --- *do these samples admit
one common encoding, and which samples say otherwise?* --- instead of forcing
the highest average score.

The design rules, each pinned by a test:

- **Samples stay separate.**  Every sample runs the full pipeline on its own
  bytes.  They are never concatenated, so a multi-byte sequence can never be
  assembled across a file boundary and no cross-file bigram statistics exist.
- **BOM and empty input are their own verdicts.**  A byte order mark is
  certainty about one sample; an empty sample is no evidence at all.  Neither
  is fed through scoring or voting.
- **Deduplication is by distinct content, not by occurrence count.**  Handing
  in the same bytes fifty times is one sample's worth of evidence.  One other
  sample with a deterministic structural contradiction therefore cannot be
  out-voted by copies.
- **Reasons are kept per sample.**  Each report records the encodings its
  pipeline positively supported (with verdict kind and confidence), the
  codecs that decode it, and the codecs that reject it.
- **The caller may narrow the candidate set** through the same
  ``include_encodings`` / ``exclude_encodings`` / ``encoding_era`` options the
  single-shot API takes.

Three evidence layers decide the outcome, hardest first:

1. **Decode facts.**  A candidate codec either decodes a sample's examined
   window, tolerating a cut multi-byte tail exactly as the validity stage
   does, or it does not.  A failure is a hard rejection no score can undo.
2. **Structural commitments.**  A BOM, an escape sequence, BOM-less
   UTF-16/32, a markup charset declaration, or multi-byte UTF-8 validation
   proves one encoding for that sample.  When another sample's bytes refuse
   that encoding, the run is a conflict, however many copies of the other
   samples were handed in.  A pure-ASCII verdict is deliberately *not* a
   commitment: ASCII is a subset of every ASCII-superset codec, so an ASCII
   file carries no negative evidence against the rest of the fleet.
3. **Positive support.**  Each statistical sample contributes the ranking
   entries above :data:`~chardet.MINIMUM_THRESHOLD`.  A pure-ASCII sample
   proves ASCII but is neutral about every ASCII-superset codec, so it
   contributes its full decodable set rather than vetoing the fleet.  The
   common encodings are the candidates every determinate sample positively
   supports *and* every informative sample decodes; samples with no
   above-threshold entry keep their decode constraints but cast no veto.
   When nothing contradicts yet nothing is positively endorsed, the run is
   :data:`ConsensusStatus.INCONCLUSIVE` rather than a forced winner.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from enum import Enum

from chardet._request import DetectionRequest
from chardet._utils import DEFAULT_MAX_BYTES, MINIMUM_THRESHOLD, decodes_without_error
from chardet.enums import EncodingEra
from chardet.pipeline import DetectionResult
from chardet.pipeline.bom import detect_bom
from chardet.registry import get_candidates

#: Confidence at or above which a pipeline verdict is a structural fact
#: rather than a statistical guess.  The deterministic early exits score
#: 0.95+ (BOM and pure ASCII score 1.0), and the exhaustive UTF-8 stage never
#: scores below 0.80.  Statistical bigram scoring lands below this band, so
#: the boundary also separates "the bytes prove this" from "the models
#: prefer this".
_STRUCTURAL_CONFIDENCE: float = 0.80

#: A fallback verdict (no candidate survived) carries 0.10.
_FALLBACK_CONFIDENCE: float = 0.10


class SampleVerdict(str, Enum):
    """What kind of answer one sample produced on its own bytes.

    The string values appear verbatim in the serialized result, so they are
    part of the public API.
    """

    #: A byte order mark named the encoding (confidence 1.0).
    BOM = "bom"
    #: A deterministic early-exit stage named the encoding: escape sequences,
    #: a markup charset declaration, ASCII, BOM-less UTF-16/32, or the UTF-8
    #: structural validation.
    STRUCTURAL = "structural"
    #: The top answer won statistical bigram scoring and clears
    #: :data:`~chardet.MINIMUM_THRESHOLD`.
    STATISTICAL = "statistical"
    #: The pipeline ranked candidates, but none cleared
    #: :data:`~chardet.MINIMUM_THRESHOLD`: the sample is too short or too
    #: bland for positive evidence.  It keeps hard decode constraints but
    #: casts no support veto.
    INDETERMINATE = "indeterminate"
    #: No candidate survived the pipeline; the sample names the configured
    #: no-match fallback, which is not evidence for any real encoding.
    NO_MATCH = "no_match"
    #: The bytes were classified as binary rather than text.
    BINARY = "binary"
    #: The sample carried no bytes and therefore no evidence.
    EMPTY = "empty"


class ConsensusStatus(str, Enum):
    """Overall outcome of a cross-sample consensus run."""

    #: The hard constraints hold and every determinate sample positively
    #: supports at least one shared encoding.
    AGREEMENT = "agreement"
    #: No sample produced evidence (all inputs were empty).
    NO_EVIDENCE = "no_evidence"
    #: The samples do not contradict each other, but none yields positive
    #: support strong enough to name a common encoding.  More bytes are
    #: needed; this is deliberately distinct from :data:`CONFLICT`.
    INCONCLUSIVE = "inconclusive"
    #: The informative samples cannot share an encoding: a structural
    #: commitment was rejected by another sample's bytes, a sample is binary
    #: or matches no codec, or the positive-support intersection is empty.
    CONFLICT = "conflict"


@dataclasses.dataclass(frozen=True, slots=True)
class EncodingSupport:
    """One encoding positively supported by a sample's pipeline run."""

    #: Canonical codec name (e.g. ``"utf-8"``, ``"cp1252"``).
    encoding: str
    #: How the sample decided: a BOM, another structural check, or scoring.
    verdict: SampleVerdict
    #: The confidence the pipeline reported for this encoding.
    confidence: float


@dataclasses.dataclass(frozen=True, slots=True)
class EncodingRejection:
    """One candidate encoding a sample's bytes refuse to decode under."""

    #: Canonical codec name that rejected the sample.
    encoding: str
    #: Machine-readable reason class; currently always ``"undecodable"``.
    reason: str
    #: Human-readable explanation of the rejection.
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class SampleReport:
    """The full support/rejection record for one distinct sample."""

    #: Zero-based position of the sample's first occurrence in the input.
    index: int
    #: How many input positions held exactly these bytes (duplicate copies).
    occurrences: int
    #: Number of bytes examined from the sample.
    byte_length: int
    #: Verdict kind of the sample's own top pipeline result.
    verdict: SampleVerdict
    #: Ranking entries above :data:`~chardet.MINIMUM_THRESHOLD`, strongest
    #: evidence first.  Empty for empty, binary, no-match, and indeterminate
    #: samples.
    supported: tuple[EncodingSupport, ...]
    #: Every candidate codec that decodes the sample's examined window
    #: cleanly --- decode facts, including codecs the pipeline did not need
    #: to score (an early-exit sample never builds a full ranking).
    compatible: tuple[str, ...]
    #: Candidate codecs whose strict validation rejects the sample.
    rejected: tuple[EncodingRejection, ...]

    @property
    def is_informative(self) -> bool:
        """Whether the sample carries any bytes at all."""
        return self.verdict is not SampleVerdict.EMPTY

    @property
    def has_positive_support(self) -> bool:
        """Whether the sample carries any above-threshold positive evidence.

        Pure-ASCII samples prove ASCII (the threshold is met at confidence
        1.0); indeterminate samples have no above-threshold entry at all.
        """
        return bool(self.supported)

    def support_set(self, decodable: frozenset[str]) -> frozenset[str]:
        """Return the encodings this sample allows into the common set.

        A pure-ASCII verdict endorses ASCII alone yet contradicts nothing,
        so it opens the floor to every codec that decodes it.  Every other
        determinate sample restricts the floor to its above-threshold
        ranking.  Indeterminate, no-match, and empty samples impose no
        positive restriction (their hard decode constraints still apply
        through the decode intersection).
        """
        if self.verdict is SampleVerdict.STRUCTURAL and (
            self.supported and self.supported[0].encoding == "ascii"
        ):
            return decodable
        return frozenset(s.encoding for s in self.supported)


@dataclasses.dataclass(frozen=True, slots=True)
class ConsensusConflict:
    """One concrete reason the samples do not share an encoding."""

    #: Index of the sample driving the conflict, or ``-1`` for a set-level
    #: conflict with no single responsible sample.
    sample_index: int
    #: The encoding the sample structurally commits to, when applicable.
    encoding: str | None
    #: Human-readable explanation naming the encodings involved.
    reason: str


@dataclasses.dataclass(frozen=True, slots=True)
class ConsensusResult:
    """The outcome of :func:`detect_consensus`.

    Frozen and slot-based like the rest of the public data types.  Use
    :meth:`to_dict` for a plain nested-dict view (JSON-friendly).
    """

    #: Whether the samples agree on a common encoding.
    status: ConsensusStatus
    #: Encodings every informative sample decodes and every determinate
    #: sample positively supports, best-supported first.  Empty unless
    #: :attr:`status` is :data:`ConsensusStatus.AGREEMENT`.
    common_encodings: tuple[str, ...]
    #: One report per *distinct* sample, in first-occurrence order.
    samples: tuple[SampleReport, ...]
    #: The conflicts that prevented agreement, empty on agreement.
    conflicts: tuple[ConsensusConflict, ...]

    def to_dict(self) -> dict[str, object]:
        """Convert to a plain nested dictionary with string values.

        :returns: A JSON-serializable dict; every enum is rendered by its
            string value.
        """
        return {
            "status": self.status.value,
            "common_encodings": list(self.common_encodings),
            "samples": [
                {
                    "index": report.index,
                    "occurrences": report.occurrences,
                    "byte_length": report.byte_length,
                    "verdict": report.verdict.value,
                    "supported": [
                        {
                            "encoding": support.encoding,
                            "verdict": support.verdict.value,
                            "confidence": support.confidence,
                        }
                        for support in report.supported
                    ],
                    "compatible": list(report.compatible),
                    "rejected": [
                        {
                            "encoding": rejection.encoding,
                            "reason": rejection.reason,
                            "detail": rejection.detail,
                        }
                        for rejection in report.rejected
                    ],
                }
                for report in self.samples
            ],
            "conflicts": [
                {
                    "sample_index": conflict.sample_index,
                    "encoding": conflict.encoding,
                    "reason": conflict.reason,
                }
                for conflict in self.conflicts
            ],
        }


def _verdict_rank(verdict: SampleVerdict) -> int:
    """Ordering of verdict kinds, strongest evidence first."""
    return {
        SampleVerdict.BOM: 0,
        SampleVerdict.STRUCTURAL: 1,
        SampleVerdict.STATISTICAL: 2,
        SampleVerdict.INDETERMINATE: 3,
        SampleVerdict.NO_MATCH: 4,
        SampleVerdict.BINARY: 5,
        SampleVerdict.EMPTY: 6,
    }[verdict]


def _classify_ranked(result: DetectionResult) -> SampleVerdict:
    """Classify a ranking entry by its confidence band."""
    if result.confidence <= _FALLBACK_CONFIDENCE:
        return SampleVerdict.NO_MATCH
    if result.confidence >= _STRUCTURAL_CONFIDENCE:
        return SampleVerdict.STRUCTURAL
    if result.confidence > MINIMUM_THRESHOLD:
        return SampleVerdict.STATISTICAL
    return SampleVerdict.INDETERMINATE


def _classify_top(window: bytes, top: DetectionResult) -> SampleVerdict:
    """Classify the pipeline's top result for one sample.

    BOM and pure-ASCII verdicts both carry confidence 1.0, so the BOM stage
    itself is the only thing that tells them apart.
    """
    if top.encoding is None:
        return SampleVerdict.BINARY
    bom = detect_bom(window)
    if bom is not None and bom.encoding == top.encoding:
        return SampleVerdict.BOM
    return _classify_ranked(top)


def _support_from_ranking(
    window: bytes, results: Sequence[DetectionResult]
) -> tuple[SampleVerdict, tuple[EncodingSupport, ...]]:
    """Turn a pipeline ranking into positively-supported encodings."""
    top = results[0]
    verdict = _classify_top(window, top)
    if verdict in (SampleVerdict.BINARY, SampleVerdict.NO_MATCH):
        return verdict, ()
    supported: list[EncodingSupport] = []
    seen: set[str] = set()
    for result in results:
        if result.encoding is None or result.encoding in seen:
            continue
        if result.confidence <= MINIMUM_THRESHOLD:
            continue
        seen.add(result.encoding)
        entry_verdict = verdict if result is top else _classify_ranked(result)
        supported.append(
            EncodingSupport(
                encoding=result.encoding,
                verdict=entry_verdict,
                confidence=result.confidence,
            )
        )
    return verdict, tuple(supported)


def _decode_compatibility(
    window: bytes, candidate_names: Sequence[str]
) -> tuple[tuple[str, ...], tuple[EncodingRejection, ...]]:
    """Split the candidate set into codecs that decode the window and ones that do not.

    The tolerant predicate matches the pipeline's validity stage: an
    incomplete multi-byte sequence at the very end of the window is accepted.
    """
    compatible: list[str] = []
    rejected: list[EncodingRejection] = []
    for name in candidate_names:
        if decodes_without_error(window, name):
            compatible.append(name)
        else:
            rejected.append(
                EncodingRejection(
                    encoding=name,
                    reason="undecodable",
                    detail=(
                        "the sample's bytes fail strict validation under "
                        f"codec {name!r}"
                    ),
                )
            )
    return tuple(compatible), tuple(rejected)


def _structural_commit(
    verdict: SampleVerdict, supported: Sequence[EncodingSupport]
) -> str | None:
    """Return the encoding a sample proves structurally, if any.

    ASCII is excluded on purpose: it is a subset of every ASCII-superset
    codec, so an ASCII-only sample cannot contradict the fleet's encoding.
    """
    if verdict is SampleVerdict.BOM:
        return supported[0].encoding
    if verdict is SampleVerdict.STRUCTURAL and supported[0].encoding != "ascii":
        return supported[0].encoding
    return None


def _build_conflicts(
    reports: Sequence[SampleReport],
    decodable_sets: Sequence[frozenset[str] | None],
    commits: Sequence[tuple[int, str]],
    support_intersection: frozenset[str],
) -> list[ConsensusConflict]:
    """Explain why no common encoding survived, hardest evidence first."""
    by_index = {
        report.index: (report, decodable)
        for report, decodable in zip(reports, decodable_sets, strict=True)
    }
    conflicts: list[ConsensusConflict] = [
        ConsensusConflict(
            sample_index=report.index,
            encoding=None,
            reason=(
                f"sample {report.index} was classified as binary, so no text "
                "encoding can be common to all samples"
            ),
        )
        for report in reports
        if report.verdict is SampleVerdict.BINARY
    ]

    # A structural commitment contradicted by another sample's bytes is the
    # strongest possible disagreement and cannot be averaged away.
    for index, committed in commits:
        for other_index, (_report, decodable) in by_index.items():
            if other_index == index or decodable is None:
                continue
            if committed not in decodable:
                conflicts.append(
                    ConsensusConflict(
                        sample_index=index,
                        encoding=committed,
                        reason=(
                            f"sample {index} structurally commits to "
                            f"{committed!r}, but sample {other_index} fails "
                            "to decode under it"
                        ),
                    )
                )
                break

    # Samples that no candidate codec can decode at all.
    for report, decodable in zip(reports, decodable_sets, strict=True):
        if decodable is None or report.verdict is SampleVerdict.BINARY:
            continue
        if not decodable:
            conflicts.append(
                ConsensusConflict(
                    sample_index=report.index,
                    encoding=None,
                    reason=(
                        f"sample {report.index} decodes under none of the "
                        "candidate encodings"
                    ),
                )
            )

    if not support_intersection and not conflicts:
        picks = ", ".join(
            f"sample {report.index} supports "
            f"{[s.encoding for s in report.supported][:3]}"
            for report in reports
            if report.has_positive_support
        )
        conflicts.append(
            ConsensusConflict(
                sample_index=-1,
                encoding=None,
                reason=(
                    "no encoding is positively supported by every "
                    f"determinate sample ({picks})"
                ),
            )
        )

    if not conflicts:  # pragma: no cover - every empty-intersection path is covered
        conflicts.append(
            ConsensusConflict(
                sample_index=-1,
                encoding=None,
                reason=(
                    "the samples' decodable-encoding sets have an empty "
                    "intersection; no candidate codec decodes every sample"
                ),
            )
        )
    return conflicts


def _order_common(common: frozenset[str], reports: Sequence[SampleReport]) -> list[str]:
    """Rank common encodings by the strongest evidence any sample gives."""
    best: dict[str, tuple[int, float]] = {}
    for report in reports:
        for support in report.supported:
            if support.encoding not in common:
                continue
            key = (_verdict_rank(support.verdict), -support.confidence)
            if support.encoding not in best or key < best[support.encoding]:
                best[support.encoding] = key
    return sorted(
        common,
        key=lambda name: (
            name not in best,
            best.get(name, (99, 0.0))[0],
            best.get(name, (99, 0.0))[1],
            name,
        ),
    )


def detect_consensus(  # noqa: PLR0913, PLR0915
    samples: Iterable[bytes | bytearray],
    *,
    encoding_era: EncodingEra = EncodingEra.ALL,
    max_bytes: int = DEFAULT_MAX_BYTES,
    include_encodings: Iterable[str] | None = None,
    exclude_encodings: Iterable[str] | None = None,
    no_match_encoding: str = "cp1252",
    empty_input_encoding: str = "utf-8",
) -> ConsensusResult:
    """Evaluate whether independent byte samples support a common encoding.

    Each sample is analyzed on its own through the full detection pipeline —
    samples are never concatenated, so no statistic can span a file boundary.
    Three layers decide the answer, hardest first: (1) codecs that fail to
    decode a sample are hard rejections; (2) structural commitments (BOM,
    escape sequences, BOM-less UTF-16/32, markup declarations, multi-byte
    UTF-8) must be decodable by every other sample or the run is a conflict;
    (3) the common encodings are those every determinate sample positively
    supports above :data:`~chardet.MINIMUM_THRESHOLD` and every informative
    sample decodes.

    Duplicate samples count once: the distinct byte contents are analyzed,
    and each :class:`SampleReport` records how many positions held that
    content.  Copying one sample cannot outweigh another sample's structural
    contradiction.

    Empty samples carry no evidence: a run where every sample is empty
    returns :data:`ConsensusStatus.NO_EVIDENCE`, and empty entries are
    otherwise ignored by the intersection.  Samples whose ranking never
    clears the threshold are :data:`SampleVerdict.INDETERMINATE`; when no
    sample has positive evidence and nothing contradicts, the run is
    :data:`ConsensusStatus.INCONCLUSIVE` rather than a forced winner.

    :param samples: The byte strings to compare, one per file or chunk.
    :param encoding_era: Restrict candidate encodings to the given era.
    :param max_bytes: Maximum number of bytes examined from each sample.
    :param include_encodings: If given, restrict the candidate set to these
        encoding names or aliases.
    :param exclude_encodings: If given, remove these encodings from the
        candidate set.
    :param no_match_encoding: Encoding named when a sample's pipeline has no
        surviving candidate; the fallback is not treated as evidence.
    :param empty_input_encoding: Encoding named for empty samples; empty
        samples still carry no consensus weight.
    :returns: A :class:`ConsensusResult` with per-sample support/rejection
        reasons and either the common encodings or the conflicts.
    :raises TypeError: If *samples* is not a sequence of byte strings or an
        element is neither bytes nor bytearray.
    :raises ValueError: If *samples* is empty or an option is invalid.
    """
    if isinstance(samples, (str, bytes, bytearray)):
        msg = (
            "samples must be a sequence of byte strings, not a single "
            "byte string or scalar"
        )
        raise TypeError(msg)
    if not isinstance(samples, Iterable):
        msg = "samples must be an iterable of byte strings"
        raise TypeError(msg)
    samples = list(samples)
    if len(samples) == 0:
        msg = "samples must contain at least one entry"
        raise ValueError(msg)

    request = DetectionRequest.resolve(
        encoding_era=encoding_era,
        max_bytes=max_bytes,
        include_encodings=include_encodings,
        exclude_encodings=exclude_encodings,
        no_match_encoding=no_match_encoding,
        empty_input_encoding=empty_input_encoding,
        stacklevel=2,
    )
    candidate_names = tuple(
        info.name
        for info in get_candidates(
            encoding_era,
            request.include_encodings,
            request.exclude_encodings,
        )
    )

    # Deduplicate by content while remembering every position the content
    # appeared at.  Equality on bytes means identical copies never get a
    # second pipeline run or a second vote in the intersection.
    first_seen: dict[bytes, int] = {}
    occurrence_count: dict[bytes, int] = {}
    distinct: list[bytes] = []
    for position, sample in enumerate(samples):
        if not isinstance(sample, bytes | bytearray):
            msg = (
                f"samples[{position}] must be bytes or bytearray, got "
                f"{type(sample).__name__}"
            )
            raise TypeError(msg)
        data = sample if isinstance(sample, bytes) else bytes(sample)
        occurrence_count[data] = occurrence_count.get(data, 0) + 1
        if data not in first_seen:
            first_seen[data] = position
            distinct.append(data)

    reports: list[SampleReport] = []
    # Aligned with ``reports``; ``None`` marks an empty sample that carries
    # no weight in the intersection.
    decodable_sets: list[frozenset[str] | None] = []
    commits: list[tuple[int, str]] = []

    for data in distinct:
        index = first_seen[data]
        if not data:
            reports.append(
                SampleReport(
                    index=index,
                    occurrences=occurrence_count[data],
                    byte_length=0,
                    verdict=SampleVerdict.EMPTY,
                    supported=(),
                    compatible=(),
                    rejected=(),
                )
            )
            decodable_sets.append(None)
            continue

        window = data[:max_bytes]
        results = request.run(data, full_ranking=True)
        verdict, supported = _support_from_ranking(window, results)
        compatible, rejected = _decode_compatibility(window, candidate_names)

        reports.append(
            SampleReport(
                index=index,
                occurrences=occurrence_count[data],
                byte_length=len(window),
                verdict=verdict,
                supported=supported,
                compatible=compatible,
                rejected=rejected,
            )
        )

        if verdict is SampleVerdict.BINARY:
            # Binary is not text evidence: it cannot share a text encoding.
            decodable_sets.append(frozenset())
        else:
            decodable_sets.append(frozenset(compatible))

        committed = _structural_commit(verdict, supported)
        if committed is not None:
            commits.append((index, committed))

    informative = [
        (report, decodable)
        for report, decodable in zip(reports, decodable_sets, strict=True)
        if decodable is not None
    ]
    if not informative:
        return ConsensusResult(
            status=ConsensusStatus.NO_EVIDENCE,
            common_encodings=(),
            samples=tuple(reports),
            conflicts=(),
        )

    decode_intersection: frozenset[str] = frozenset.intersection(
        *(decodable for _report, decodable in informative)
    )
    veto_reports = [report for report in reports if report.has_positive_support]
    support_sets = [
        report.support_set(decodable_sets[order] or frozenset())
        for order, report in enumerate(reports)
        if report.has_positive_support
    ]
    support_intersection = (
        frozenset.intersection(*support_sets) if support_sets else frozenset()
    )
    # Only encodings at least one sample positively endorses may be named;
    # the raw decode intersection alone is never an answer.
    endorsed = frozenset(
        support.encoding for report in veto_reports for support in report.supported
    )
    common = decode_intersection & support_intersection & endorsed

    if common:
        status = ConsensusStatus.AGREEMENT
        conflicts: list[ConsensusConflict] = []
    elif not veto_reports and decode_intersection:
        # Nothing contradicts, but no sample has positive evidence either.
        status = ConsensusStatus.INCONCLUSIVE
        conflicts = []
    else:
        status = ConsensusStatus.CONFLICT
        conflicts = _build_conflicts(
            reports,
            decodable_sets,
            commits,
            support_intersection,
        )

    return ConsensusResult(
        status=status,
        common_encodings=tuple(_order_common(common, reports)),
        samples=tuple(reports),
        conflicts=tuple(conflicts),
    )
