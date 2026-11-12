"""
`ftfy.evidence` adds traceable evidence to ftfy's mojibake heuristics.

The badness heuristic (see `ftfy.badness`) tells ftfy *that* text looks like
mojibake, and `ftfy.fix_and_explain` tells you *which steps* would fix it.
This module connects the two: it runs the same fixing pipeline, and at each
stage -- the input, and the text after each fix that changed it -- it records
which heuristic signals matched, and maps each match back to the code points
of the *original input* that it came from.

This is what lets an editor, looking at a cleaned-up title, click on a
suspicious word and be taken back to the exact characters in the original
field that made ftfy suspicious.

The mapping is done by an :class:`Alignment`, which tracks where each code
point of a fixed string came from in the input. Alignments handle the cases
where text doesn't line up one-to-one:

- Multiple input characters that merge into one (an HTML entity such as
  ``&amp;`` becoming ``&``, a surrogate pair becoming an emoji, or NFC
  normalization composing ``e`` + ``´`` into ``é``): the output character
  maps to the whole input range it was made from.
- One input character that expands into many (a ligature such as ``ﬁ``
  becoming ``fi``): each output character maps to the input character.
- Repeated text where only one occurrence changed (two identical words, of
  which only the second was mojibake): the change is aligned to the
  occurrence that actually changed, not the first one that looks the same.
- Rewrites where a per-character correspondence can't be determined: the
  evidence keeps the entire range of input that was involved, and marks the
  mapping as ambiguous instead of guessing.

Collecting evidence is opt-in. When you don't ask for it, none of this code
runs, and the fixed text and badness scores are exactly what they have
always been.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

import ftfy
from ftfy import fixes
from ftfy.badness import signal_hits
from ftfy.chardata import HTML_ENTITY_RE

if TYPE_CHECKING:
    from ftfy import ExplainedText, ExplanationStep, TextFixerConfig

# A half-open range of code point offsets, [start, end), in some text.
Range = tuple[int, int]


def _merge_ranges(ranges: list[Range]) -> tuple[Range, ...]:
    """
    Merge overlapping or adjacent ranges into a minimal sorted tuple of
    disjoint ranges.
    """
    if not ranges:
        return ()
    merged: list[Range] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


class Alignment:
    """
    An Alignment tracks, for each code point of some "output" text, which
    code points of an "input" text it came from.

    Each position maps to a tuple of (start, end) ranges of input code
    points, plus a flag telling whether that mapping is ambiguous -- meaning
    the text was rewritten in a way where we can identify the input range
    involved, but not a unique per-character correspondence within it.

    Create an Alignment with :meth:`Alignment.identity` (each character maps
    to itself) or :meth:`Alignment.diff` (aligning two versions of a text),
    chain them with :meth:`compose`, and ask where a span came from with
    :meth:`lookup`.
    """

    def __init__(self, entries: list[tuple[tuple[Range, ...], bool]]) -> None:
        # `entries` has one element per code point of the output text:
        # the input ranges it came from, and whether the mapping is ambiguous.
        self._entries = entries

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Alignment):
            return NotImplemented
        return self._entries == other._entries

    def __repr__(self) -> str:
        return f"Alignment({self._entries!r})"

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def identity(cls, length: int) -> Alignment:
        """
        The alignment of a text with itself: each code point maps to its own
        position, unambiguously.
        """
        return cls([(((index, index + 1),), False) for index in range(length)])

    @classmethod
    def diff(cls, before: str, after: str) -> Alignment:
        """
        Align two versions of a text, mapping each code point of `after` to
        the code points of `before` it came from.

        Unchanged and one-for-one substituted characters map to single
        positions. Characters that were merged or expanded map to the entire
        range that was rewritten; when a rewrite replaces multiple characters
        with a different number of multiple characters, the correspondence
        within that range cannot be determined uniquely, so the whole range
        is kept and marked ambiguous.
        """
        matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
        entries: list[tuple[tuple[Range, ...], bool]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(j2 - j1):
                    entries.append((((i1 + offset, i1 + offset + 1),), False))
            elif tag == "replace":
                len_before = i2 - i1
                len_after = j2 - j1
                if len_before == len_after:
                    # One-for-one substitutions, such as uncurled quotes
                    for offset in range(len_after):
                        entries.append((((i1 + offset, i1 + offset + 1),), False))
                elif len_before == 1 or len_after == 1:
                    # Many characters merged into one, or one character
                    # expanded into many. Either way, the whole input range
                    # is the unique source of each output character.
                    for _offset in range(len_after):
                        entries.append((((i1, i2),), False))
                else:
                    # A rewrite of many characters into a different number of
                    # many characters. We can only point at the whole range.
                    for _offset in range(len_after):
                        entries.append((((i1, i2),), True))
            elif tag == "insert":
                # Inserted characters have no source in the input.
                for _offset in range(j2 - j1):
                    entries.append(((), False))
            # "delete" consumes input characters and produces no output
            # characters, so there is nothing to record.
        return cls(entries)

    def compose(self, other: Alignment) -> Alignment:
        """
        Chain two alignments. If `self` maps text A to text B, and `other`
        maps text B to text C, the result maps text A to text C. Ambiguity
        propagates: a position is ambiguous if either step was ambiguous.
        """
        entries: list[tuple[tuple[Range, ...], bool]] = []
        for ranges, ambiguous in other._entries:
            combined: list[Range] = []
            for start, end in ranges:
                for index in range(start, end):
                    source_ranges, source_ambiguous = self._entries[index]
                    combined.extend(source_ranges)
                    ambiguous = ambiguous or source_ambiguous
            entries.append((_merge_ranges(combined), ambiguous))
        return Alignment(entries)

    def lookup(self, start: int, end: int) -> tuple[tuple[Range, ...], bool]:
        """
        Find where the span [start, end) of the output text came from in the
        input text. Returns the merged input ranges, and whether any part of
        the mapping is ambiguous.
        """
        combined: list[Range] = []
        ambiguous = False
        for index in range(start, end):
            ranges, entry_ambiguous = self._entries[index]
            combined.extend(ranges)
            ambiguous = ambiguous or entry_ambiguous
        return _merge_ranges(combined), ambiguous


def _unescape_html_alignment(before: str, after: str) -> Alignment:
    """
    Align text precisely through `fixes.unescape_html`.

    Each HTML entity is a single unit: whether it decodes to one character
    (``&amp;`` -> ``&``) or several (``&ThickSpace;`` -> two spaces), every
    character it becomes comes from that entity's whole span, unambiguously.
    Text between entities is unchanged and aligns to itself.
    """
    entries: list[tuple[tuple[Range, ...], bool]] = []
    pieces: list[str] = []
    pos = 0
    for match in HTML_ENTITY_RE.finditer(before):
        start, end = match.span()
        pieces.append(before[pos:start])
        for index in range(pos, start):
            entries.append((((index, index + 1),), False))
        replacement = fixes._unescape_fixup(match)
        pieces.append(replacement)
        for _char in replacement:
            entries.append((((start, end),), False))
        pos = end
    pieces.append(before[pos:])
    for index in range(pos, len(before)):
        entries.append((((index, index + 1),), False))
    if "".join(pieces) != after:
        # Defensive: this should be exactly what unescape_html did, but if
        # the text was transformed some other way, fall back to the
        # generic alignment.
        return Alignment.diff(before, after)
    return Alignment(entries)


# Fixers whose structure we know, and can therefore align more precisely
# than by comparing text. Anything not listed here is aligned with
# `Alignment.diff`.
_STAGE_ALIGNERS: dict[str, Callable[[str, str], Alignment]] = {
    "unescape_html": _unescape_html_alignment,
}


def _alignment_for(stage: str, before: str, after: str) -> Alignment:
    """
    Compute the alignment of one stage of the pipeline, using a fixer-specific
    aligner when one is available.
    """
    aligner = _STAGE_ALIGNERS.get(stage)
    if aligner is not None:
        return aligner(before, after)
    return Alignment.diff(before, after)


class SignalEvidence(NamedTuple):
    """
    One heuristic signal that matched at one stage of fixing, traced back to
    the original input.

    - `signal`: the name of the badness rule that matched
    - `matched`: the text that the signal matched at this stage
    - `span`: the (start, end) code point offsets of the match in this
      stage's text
    - `score`: this match's contribution to the stage's badness score
    - `input_ranges`: the (start, end) code point ranges of the original
      input that this match traces back to
    - `ambiguous`: True if the match could not be traced to a unique
      per-character correspondence, in which case `input_ranges` contains
      all of the input ranges that could be involved
    """

    signal: str
    matched: str
    span: Range
    score: int
    input_ranges: tuple[Range, ...]
    ambiguous: bool

    def describe(self) -> str:
        """
        Explain, in one sentence, what matched and how it contributes to the
        badness score of the original input.
        """
        start, end = self.span
        if self.input_ranges:
            source = ", ".join(
                f"[{lo}, {hi})" for lo, hi in self.input_ranges
            )
            tracing = f"traced to input code points {source}"
        else:
            tracing = "introduced by a fixer; no corresponding input code points"
        if self.ambiguous:
            tracing += " (ambiguous: the exact correspondence cannot be determined)"
        return (
            f"signal {self.signal!r} matched {self.matched!r} at code points "
            f"[{start}, {end}) of this stage, contributing {self.score} to the "
            f"badness score; {tracing}"
        )


class StageEvidence(NamedTuple):
    """
    The state of the text at one stage of fixing: the input itself (stage
    "input"), or the text after a named fixer changed it.

    - `stage`: "input", or the name of the fixer that produced this text
    - `text`: the text at this stage
    - `badness`: the badness score of the text at this stage
    - `signals`: the signals that matched this text, each traced back to the
      original input; their scores add up to `badness`
    """

    stage: str
    text: str
    badness: int
    signals: tuple[SignalEvidence, ...]


class FixEvidence(NamedTuple):
    """
    The result of fixing text with evidence collection.

    - `text`: the fixed text, the same as `fix_text_segment` would return
    - `score`: the badness score of the original input
    - `stages`: the state of the text before and after each fix that was
      applied, with the heuristic evidence at each stage
    - `explanation`: the explanation steps, as in `fix_and_explain`
    """

    text: str
    score: int
    stages: tuple[StageEvidence, ...]
    explanation: list[ExplanationStep] | None


def _stage_evidence(stage: str, text: str, alignment: Alignment) -> StageEvidence:
    """
    Compute the badness signals of one stage of text, and trace each of them
    back to the original input through `alignment`.
    """
    signals = []
    for hit in signal_hits(text):
        input_ranges, ambiguous = alignment.lookup(*hit.span)
        signals.append(
            SignalEvidence(
                signal=hit.signal,
                matched=hit.matched,
                span=hit.span,
                score=hit.score,
                input_ranges=input_ranges,
                ambiguous=ambiguous,
            )
        )
    return StageEvidence(
        stage=stage,
        text=text,
        badness=len(signals),
        signals=tuple(signals),
    )


def fix_text_with_evidence(
    text: str, config: TextFixerConfig | None = None, **kwargs: Any
) -> FixEvidence:
    """
    Fix text as a single segment, collecting traceable evidence about the
    mojibake heuristics that fired before and after each fix.

    The returned :class:`FixEvidence` has the same fixed text that
    :func:`ftfy.fix_text_segment` would produce, and its `score` is the same
    as ``ftfy.badness.badness(text)`` -- collecting evidence changes neither
    the fixes nor the scores, it only explains them.

        >>> evidence = fix_text_with_evidence('schÃ¶n')
        >>> evidence.text
        'schön'
        >>> evidence.score
        1
        >>> first_hit = evidence.stages[0].signals[0]
        >>> first_hit.signal, first_hit.input_ranges
        ('capital_then_numeric', ((3, 5),))
    """
    if config is None:
        config = ftfy.TextFixerConfig()

    records: list[tuple[str, str, str]] = []

    def recorder(stage: str, before: str, after: str) -> None:
        records.append((stage, before, after))

    explained: ExplainedText = ftfy._fix_and_explain_impl(
        text, config, kwargs, recorder=recorder
    )

    stages = []
    alignment = Alignment.identity(len(text))
    stages.append(_stage_evidence("input", text, alignment))
    for stage_name, _before, after in records:
        alignment = alignment.compose(_alignment_for(stage_name, _before, after))
        stages.append(_stage_evidence(stage_name, after, alignment))

    return FixEvidence(
        text=explained.text,
        score=stages[0].badness,
        stages=tuple(stages),
        explanation=explained.explanation,
    )
