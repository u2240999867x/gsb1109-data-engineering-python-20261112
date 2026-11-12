"""
A quality gate for running ftfy over a mixed corpus.

The same string can be a product title that ought to be fixed, or mojibake that
a document displays on purpose. Whether ftfy's output is correct therefore
depends on the *purpose* of the field, not only on the string itself.

This module evaluates paired, hand-labeled regression records. Each record
contains the original string, the field's purpose (``"should_fix"`` or
``"should_keep"``), and the expected output. For every record it records:

- the intervals ftfy actually changed,
- fragments of an intended fix that were erroneously retained,
- and changes ftfy introduced that the human labeler did not expect.

It can also search the space of ftfy configuration switches, switching fixers
off one at a time and then in combination, to find every *minimal* set of
switches whose disabling explains (suppresses) an erroneous rewrite. When more
than one minimal combination explains the same rewrite, all of them are
returned.

Summary counts are counted per distinct business record (keyed by
``record_id``), so exporting the same document ten times cannot change the
false-change proportion of any field purpose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Protocol

from ftfy import TextFixerConfig, fix_text

# The boolean fixer switches of TextFixerConfig, in a fixed order. The
# non-boolean options (normalization, max_decode_length, explain) are not
# on/off fixers and are excluded from the switch search.
TOGGLE_SWITCHES: tuple[str, ...] = tuple(
    name
    for name in TextFixerConfig._fields
    if name not in ("normalization", "max_decode_length", "explain")
)

PURPOSE_SHOULD_FIX = "should_fix"
PURPOSE_SHOULD_KEEP = "should_keep"
KNOWN_PURPOSES = frozenset({PURPOSE_SHOULD_FIX, PURPOSE_SHOULD_KEEP})


class ChangeInterval(tuple):
    """
    A changed span, expressed as half-open index intervals.

    ``source_start:source_end`` indexes the "before" string and
    ``target_start:target_end`` indexes the "after" string. Insertions have an
    empty source interval and deletions have an empty target interval, so that
    the change point is still visible.
    """

    __slots__ = ()

    def __new__(
        cls, source_start: int, source_end: int, target_start: int, target_end: int
    ) -> "ChangeInterval":
        return tuple.__new__(cls, (source_start, source_end, target_start, target_end))

    @property
    def source_start(self) -> int:
        return self[0]

    @property
    def source_end(self) -> int:
        return self[1]

    @property
    def target_start(self) -> int:
        return self[2]

    @property
    def target_end(self) -> int:
        return self[3]

    def source_overlaps(self, other: "ChangeInterval") -> bool:
        """Whether two intervals overlap on their "before" (source) side."""
        return self.source_start < other.source_end and other.source_start < self.source_end


def diff_intervals(before: str, after: str) -> list[ChangeInterval]:
    """
    Return the spans that differ between ``before`` and ``after``.

    Equal spans are omitted. Each returned interval locates the span in both
    strings, including zero-width intervals for pure insertions or deletions.
    """
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    intervals: list[ChangeInterval] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            intervals.append(ChangeInterval(i1, i2, j1, j2))
    return intervals


@dataclass(frozen=True)
class QualityRecord:
    """
    One hand-labeled regression record.

    - ``record_id``: the stable identity of the business record. Repeated
      exports of the same document share an id and are counted only once.
    - ``purpose``: ``"should_fix"`` for a field that ftfy ought to repair, such
      as a product title, or ``"should_keep"`` for a field that displays text
      on purpose and must be left alone.
    - ``original``: the string fed to ftfy.
    - ``expected``: the output the human labeler expects.
    - ``label``: optional human-readable description.
    - ``document_id``: optional source document, used only for diagnostics.
    """

    record_id: str
    purpose: str
    original: str
    expected: str
    label: str = ""
    document_id: str = ""

    def __post_init__(self) -> None:
        if not self.record_id:
            raise ValueError("record_id must be a non-empty string")
        if self.purpose not in KNOWN_PURPOSES:
            raise ValueError(
                f"unknown purpose {self.purpose!r}; expected one of "
                f"{sorted(KNOWN_PURPOSES)}"
            )
        if self.purpose == PURPOSE_SHOULD_KEEP and self.expected != self.original:
            raise ValueError(
                f"record {self.record_id!r} is marked should_keep, so its "
                "expected output must equal its original string"
            )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "QualityRecord":
        try:
            return cls(
                record_id=str(data["record_id"]),
                purpose=str(data["purpose"]),
                original=str(data["original"]),
                expected=str(data["expected"]),
                label=str(data.get("label", "")),
                document_id=str(data.get("document_id", "")),
            )
        except KeyError as err:
            raise ValueError(f"quality record is missing required field: {err}") from err


@dataclass(frozen=True)
class RecordOutcome:
    """
    The verdict for one evaluated :class:`QualityRecord`.

    - ``changed_intervals``: every span ftfy changed relative to the original.
    - ``retained_intervals``: intended changes that ftfy left untouched. The
      intervals index the original string.
    - ``introduced_intervals``: spans of the actual output that do not match
      the expected output. Source indices address ``expected`` and target
      indices address the actual output.
    - ``minimal_switch_combos``: every minimal set of switches that must be
      disabled to make ftfy return the expected output for this record.
    """

    record: QualityRecord
    actual: str
    changed: bool
    false_change: bool
    false_retain: bool
    changed_intervals: list[ChangeInterval]
    retained_intervals: list[ChangeInterval]
    introduced_intervals: list[ChangeInterval]
    minimal_switch_combos: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def erroneous_rewrite(self) -> bool:
        """Whether ftfy rewrote the string in a way the labeler did not expect."""
        return self.changed and self.actual != self.record.expected


class Fixer(Protocol):
    def __call__(self, text: str) -> str: ...


class ToggleFixer(Protocol):
    """A fixer that runs with a set of configuration switches forced off."""

    def __call__(self, text: str, disabled: frozenset[str]) -> str: ...


def default_toggle_fixer(text: str, disabled: frozenset[str]) -> str:
    """Run ftfy with every switch in ``disabled`` set to ``False``."""
    unknown = disabled.difference(TOGGLE_SWITCHES)
    if unknown:
        raise ValueError(f"unknown ftfy switches: {sorted(unknown)}")
    return fix_text(text, **{name: False for name in disabled})


def minimal_switch_combos(
    text: str,
    expected: str,
    toggle_fixer: ToggleFixer = default_toggle_fixer,
    switches: tuple[str, ...] = TOGGLE_SWITCHES,
) -> list[tuple[str, ...]]:
    """
    Find every minimal combination of switches that suppresses a bad rewrite.

    A combination is a set of switches which, when disabled together, makes the
    fixer return ``expected`` for ``text``. Combinations are searched from
    smallest to largest and filtered so that no returned combination contains
    another one (minimal hitting sets of the fixer's behavior). If several
    unrelated combinations can each explain the rewrite, all of them are
    returned.

    The search is exhaustive over the power set of the switches (8192
    combinations for the default configuration) and injectable so the logic can
    be tested without running ftfy itself.
    """
    suppressing: set[frozenset[str]] = set()
    for size in range(len(switches) + 1):
        for combo in combinations(switches, size):
            disabled = frozenset(combo)
            if toggle_fixer(text, disabled) == expected:
                suppressing.add(disabled)

    minimal = [
        combo
        for combo in suppressing
        if not any(other < combo for other in suppressing)
    ]
    return [tuple(sorted(combo)) for combo in sorted(minimal, key=lambda c: (len(c), sorted(c)))]


def evaluate_record(
    record: QualityRecord,
    fixer: Fixer = fix_text,
    toggle_fixer: ToggleFixer | None = default_toggle_fixer,
) -> RecordOutcome:
    """
    Evaluate one record against ftfy and classify its discrepancies.

    For a ``should_keep`` record, any change is a false change and every
    changed span is an introduced change. For a ``should_fix`` record, an
    unchanged string is a false retain; a changed string that misses the
    expected output splits into retained intended spans and introduced spans.
    """
    actual = fixer(record.original)
    changed = actual != record.original

    changed_intervals = diff_intervals(record.original, actual)
    intended_intervals = diff_intervals(record.original, record.expected)

    retained_intervals: list[ChangeInterval] = []
    introduced_intervals: list[ChangeInterval] = []
    false_change = False
    false_retain = False

    if record.purpose == PURPOSE_SHOULD_KEEP:
        false_change = changed
        if changed:
            introduced_intervals = list(changed_intervals)
    else:
        false_retain = not changed
        if not changed:
            # ftfy touched nothing, so every intended change is retained.
            retained_intervals = list(intended_intervals)
        else:
            retained_intervals = [
                span
                for span in intended_intervals
                if not any(span.source_overlaps(changed) for changed in changed_intervals)
            ]
            if actual != record.expected:
                introduced_intervals = diff_intervals(record.expected, actual)

    combos: list[tuple[str, ...]] = []
    if changed and actual != record.expected and toggle_fixer is not None:
        combos = minimal_switch_combos(
            record.original, record.expected, toggle_fixer=toggle_fixer
        )

    return RecordOutcome(
        record=record,
        actual=actual,
        changed=changed,
        false_change=false_change,
        false_retain=false_retain,
        changed_intervals=changed_intervals,
        retained_intervals=retained_intervals,
        introduced_intervals=introduced_intervals,
        minimal_switch_combos=combos,
    )


@dataclass(frozen=True)
class PurposeMetrics:
    """Counts for one field purpose, over distinct business records."""

    purpose: str
    total: int
    false_change_count: int
    false_retain_count: int
    introduced_change_count: int
    retained_change_count: int

    @property
    def false_change_rate(self) -> float:
        return self.false_change_count / self.total if self.total else 0.0

    @property
    def false_retain_rate(self) -> float:
        return self.false_retain_count / self.total if self.total else 0.0


@dataclass(frozen=True)
class QualityGateResult:
    """
    The outcome of evaluating a whole corpus.

    ``outcomes`` holds one :class:`RecordOutcome` per distinct
    :attr:`QualityRecord.record_id`; ``duplicate_record_ids`` lists ids that
    appeared more than once in the input and were therefore counted once.
    """

    outcomes: list[RecordOutcome]
    metrics_by_purpose: dict[str, PurposeMetrics]
    duplicate_record_ids: list[str]

    def outcomes_for_purpose(self, purpose: str) -> list[RecordOutcome]:
        return [outcome for outcome in self.outcomes if outcome.record.purpose == purpose]

    def as_dict(self) -> dict[str, object]:
        """JSON-serializable view of the result, suitable for a report."""
        return {
            "metrics": {
                purpose: {
                    "total": metrics.total,
                    "false_change_count": metrics.false_change_count,
                    "false_change_rate": metrics.false_change_rate,
                    "false_retain_count": metrics.false_retain_count,
                    "false_retain_rate": metrics.false_retain_rate,
                    "introduced_change_count": metrics.introduced_change_count,
                    "retained_change_count": metrics.retained_change_count,
                }
                for purpose, metrics in sorted(self.metrics_by_purpose.items())
            },
            "duplicate_record_ids": list(self.duplicate_record_ids),
            "records": [
                {
                    "record_id": outcome.record.record_id,
                    "purpose": outcome.record.purpose,
                    "label": outcome.record.label,
                    "changed": outcome.changed,
                    "false_change": outcome.false_change,
                    "false_retain": outcome.false_retain,
                    "changed_intervals": [list(span) for span in outcome.changed_intervals],
                    "retained_intervals": [list(span) for span in outcome.retained_intervals],
                    "introduced_intervals": [
                        list(span) for span in outcome.introduced_intervals
                    ],
                    "minimal_switch_combos": [
                        list(combo) for combo in outcome.minimal_switch_combos
                    ],
                }
                for outcome in self.outcomes
            ],
        }


def load_records(path: str | Path) -> list[QualityRecord]:
    """Load regression records from a JSON file containing a list of objects."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("quality gate fixture file must contain a JSON list")
    return [QualityRecord.from_dict(item) for item in data]


def evaluate_corpus(
    records: list[QualityRecord],
    fixer: Fixer = fix_text,
    toggle_fixer: ToggleFixer | None = default_toggle_fixer,
) -> QualityGateResult:
    """
    Evaluate a corpus and summarize error rates per field purpose.

    Records are deduplicated by ``record_id`` before anything is counted:
    repeated exports of the same document (the same id appearing in ten files,
    say) contribute a single business record, so they cannot inflate or dilute
    a purpose's false-change proportion. Distinct business records that happen
    to contain the same string still count separately, because they have
    distinct ids.
    """
    deduped: dict[str, QualityRecord] = {}
    duplicates: set[str] = set()
    for record in records:
        if record.record_id in deduped:
            duplicates.add(record.record_id)
            continue
        deduped[record.record_id] = record

    outcomes = [
        evaluate_record(record, fixer=fixer, toggle_fixer=toggle_fixer)
        for record in deduped.values()
    ]

    metrics: dict[str, PurposeMetrics] = {}
    for purpose in sorted(KNOWN_PURPOSES):
        purpose_outcomes = [
            outcome for outcome in outcomes if outcome.record.purpose == purpose
        ]
        metrics[purpose] = PurposeMetrics(
            purpose=purpose,
            total=len(purpose_outcomes),
            false_change_count=sum(outcome.false_change for outcome in purpose_outcomes),
            false_retain_count=sum(outcome.false_retain for outcome in purpose_outcomes),
            introduced_change_count=sum(
                bool(outcome.introduced_intervals) for outcome in purpose_outcomes
            ),
            retained_change_count=sum(
                bool(outcome.retained_intervals) for outcome in purpose_outcomes
            ),
        )

    return QualityGateResult(
        outcomes=outcomes,
        metrics_by_purpose=metrics,
        duplicate_record_ids=sorted(duplicates),
    )
