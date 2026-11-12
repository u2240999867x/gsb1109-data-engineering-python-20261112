"""
Explain why natsort orders two values the way it does.

This module provides :func:`explain`, a reviewable companion to the
public comparison entry points (:func:`natsort.natsort_keygen`,
:func:`natsort.natsorted`, ...). Given two values it reports the sorting
policy in effect, the (possibly nested) natural sort keys, the span of
the *original* strings that produced each key component, and the first
key component that decided the order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Callable, Literal, NamedTuple, Union, cast
from unicodedata import combining, normalize

import natsort.compat.locale
from natsort import utils
from natsort.natsort import natsort_keygen
from natsort.ns_enum import NS_DUMB, NSType, ns

if TYPE_CHECKING:
    from re import Pattern

from collections.abc import Iterable

__all__ = [
    "MISSING",
    "ComparisonExplanation",
    "ExplainedComponent",
    "SkippedEqualNumber",
    "Span",
    "explain",
]

_SEP_NOTE = "inserted by natsort to align adjacent numbers; no source span"
_FIRST_LETTER_NOTE = "gross sort by first character (UNGROUPLETTERS)"
_LOCALE_NOTE = "locale transform is opaque; span marks the whole segment"
_GROUP_NOTE = "letters grouped by groupletters()"


class _MissingType:
    """Sentinel for a component that is absent on one side of a comparison."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _MissingType()


@dataclass(frozen=True)
class Span:
    """
    Half-open ``[start, end)`` character offsets into an original string.

    Attributes
    ----------
    start : int
        Offset of the first character (inclusive).
    end : int
        Offset one past the last character (exclusive).

    """

    start: int
    end: int

    def __str__(self) -> str:
        return f"[{self.start}:{self.end}]"


@dataclass(frozen=True)
class ExplainedComponent:
    """
    One aligned pair of key components from the left and right values.

    Attributes
    ----------
    path : tuple of int
        Index path of this component within the (possibly nested) sort
        key, preserving the key hierarchy.
    left, right : object
        The key component from the left and right value. ``MISSING`` if
        the key on that side was exhausted.
    kind : str
        The component kind, e.g. ``"number"``, ``"string"``,
        ``"separator"``, ``"first-letter"``, ``"bytes"``.
    left_span, right_span : Span or None
        The half-open interval of the *original* input string that this
        component was parsed from, or *None* if there is no source span
        (e.g. inserted separators or non-string input).
    left_text, right_text : str or None
        The original text covered by the span, if available.
    decisive : bool
        *True* if this is the first component that differs and therefore
        decided the order.
    note : str or None
        Extra review information, e.g. that a locale transform could
        only be attributed to the whole segment.

    """

    path: tuple[int, ...]
    left: Any
    right: Any
    kind: str
    left_span: Span | None
    right_span: Span | None
    left_text: str | None
    right_text: str | None
    decisive: bool
    note: str | None = None


@dataclass(frozen=True)
class SkippedEqualNumber:
    """
    A pair of numerically equal components that did not decide the order.

    These are numbers that compared equal (and were therefore "skipped")
    before a later component decided the order, even though their
    original text differs (e.g. ``"01"`` versus ``"1"``).

    Attributes
    ----------
    path : tuple of int
        Index path of the component within the sort key.
    value : int or float
        The numeric value both sides parsed to.
    left_span, right_span : Span or None
        The interval of the original strings the number was parsed from.
    left_text, right_text : str or None
        The original text of the number on each side.

    """

    path: tuple[int, ...]
    value: Any
    left_span: Span | None
    right_span: Span | None
    left_text: str | None
    right_text: str | None


@dataclass(frozen=True)
class ComparisonExplanation:
    """
    A reviewable explanation of how natsort orders two values.

    Attributes
    ----------
    left, right : object
        The original inputs, as passed to :func:`explain`.
    left_value, right_value : object
        The inputs after the user-supplied *key* was applied (exactly
        once per input). The sort keys, the comparison, and this
        explanation all reuse these values.
    left_key, right_key : tuple
        The full natural sort keys, with their hierarchy preserved.
    alg : ns enum
        The sorting policy that was in effect.
    result : int
        -1 if *left* sorts before *right*, 1 if after, 0 if the keys
        compare equal.
    components : tuple of ExplainedComponent
        The aligned key components in the order they were compared.
    skipped_equal_numbers : tuple of SkippedEqualNumber
        Numerically equal components with differing original text that
        were skipped before the decisive component.

    """

    left: Any
    right: Any
    left_value: Any
    right_value: Any
    left_key: Any
    right_key: Any
    alg: NSType
    result: int
    components: tuple[ExplainedComponent, ...]
    skipped_equal_numbers: tuple[SkippedEqualNumber, ...]

    @property
    def decisive(self) -> ExplainedComponent | None:
        """The first component that decided the order, or None if equal."""
        for component in self.components:
            if component.decisive:
                return component
        return None

    def __str__(self) -> str:
        order = {
            -1: f"{self.left!r} sorts before {self.right!r}",
            0: f"{self.left!r} and {self.right!r} compare equal",
            1: f"{self.left!r} sorts after {self.right!r}",
        }[self.result if self.result in (-1, 0, 1) else 0]
        lines = [
            f"policy (alg): {_format_alg(self.alg)}",
            f"result: {self.result} ({order})",
            f"left key:  {self.left_key!r}",
            f"right key: {self.right_key!r}",
        ]
        decisive = self.decisive
        if decisive is not None:
            lines.append(
                f"decisive component at {decisive.path}: "
                f"{decisive.left!r} vs {decisive.right!r} "
                f"(left {_fmt_span(decisive.left_span, decisive.left_text)}, "
                f"right {_fmt_span(decisive.right_span, decisive.right_text)})"
            )
            if decisive.note:
                lines.append(f"  note: {decisive.note}")
        lines.extend(
            f"skipped equal number at {skipped.path}: "
            f"{skipped.value!r} == {skipped.value!r} "
            f"(left {_fmt_span(skipped.left_span, skipped.left_text)}, "
            f"right {_fmt_span(skipped.right_span, skipped.right_text)})"
            for skipped in self.skipped_equal_numbers
        )
        return "\n".join(lines)


def _fmt_span(span: Span | None, text: str | None) -> str:
    """Format a span and its text for human consumption."""
    if span is None:
        return "without source span"
    return f"{span} {text!r}"


def _format_alg(alg: NSType) -> str:
    """Render the sorting policy as a readable combination of ns options."""
    if alg & NS_DUMB:
        base = _format_alg(alg & ~NS_DUMB)
        return f"{base} | NS_DUMB (broken locale workaround)"
    names = [
        member.name
        for member in ns
        if member.value
        and not member.value & (member.value - 1)  # atomic options only
        and alg & member.value
    ]
    return "ns." + " | ns.".join(names) if names else "ns.DEFAULT"


class _Leaf(NamedTuple):
    """A sort key leaf annotated with its source span and review notes."""

    span: Span | None
    kind: str
    note: str | None = None


class _Ctx(NamedTuple):
    """The algorithm-dependent machinery shared by the span trackers."""

    alg: NSType
    regex: Pattern[str]
    component_transform: utils.StrTransformer


class _Tracked:
    """A string where each character knows its span in the original string."""

    __slots__ = ("spans", "text")

    def __init__(self, text: str, spans: list[Span | None]) -> None:
        self.text = text
        self.spans = spans

    @classmethod
    def identity(cls, text: str) -> _Tracked:
        """Create a tracked string that maps to itself."""
        return cls(text, [Span(i, i + 1) for i in range(len(text))])

    def span_of(self, start: int, end: int) -> Span | None:
        """Return the original span covering ``self.text[start:end]``."""
        spans = [span for span in self.spans[start:end] if span is not None]
        if not spans:
            return None
        return Span(min(span.start for span in spans), max(span.end for span in spans))


def _remap(tracked: _Tracked, pieces: list[str], whole: str) -> _Tracked:
    """
    Rebuild a tracked string from per-character transformation pieces.

    *whole* is the result of applying the transform to the whole string.
    If it does not match the concatenation of the per-character pieces,
    the transform expanded one character into many or regrouped many
    characters across boundaries; in that case every output character is
    attributed to the whole input span so offsets still map back to the
    correct original interval.
    """
    joined = "".join(pieces)
    if joined == whole:
        spans: list[Span | None] = []
        for piece, span in zip(pieces, tracked.spans):
            spans.extend([span] * len(piece))
        return _Tracked(joined, spans)
    whole_span = tracked.span_of(0, len(tracked.text))
    return _Tracked(whole, [whole_span] * len(whole))


_NormalizeForm = Literal["NFC", "NFD", "NFKC", "NFKD"]


def _combining_clusters(text: str) -> list[str]:
    """Split text into starters each followed by their combining marks."""
    if not text:
        return []
    clusters = []
    start = 0
    for index, char in enumerate(text):
        if index and not combining(char):
            clusters.append(text[start:index])
            start = index
    clusters.append(text[start:])
    return clusters


def _tracked_normalize(tracked: _Tracked, form: _NormalizeForm) -> _Tracked:
    """Normalize unicode while tracking original spans."""
    whole = normalize(form, tracked.text)
    # First try to map per character.
    units = list(tracked.text)
    pieces = [normalize(form, unit) for unit in units]
    if "".join(pieces) != whole:
        # One character expanded or several regrouped (e.g. a precomposed
        # character and its decomposition): retry per combining cluster so
        # the regrouped characters still map to their own original interval.
        units = _combining_clusters(tracked.text)
        pieces = [normalize(form, unit) for unit in units]
    if "".join(pieces) != whole:
        # Characters regrouped across cluster boundaries (e.g. Hangul
        # jamo): attribute the whole span so offsets still map back to the
        # correct original interval.
        whole_span = tracked.span_of(0, len(tracked.text))
        return _Tracked(whole, [whole_span] * len(whole))
    spans: list[Span | None] = []
    pos = 0
    for unit, piece in zip(units, pieces):
        span = tracked.span_of(pos, pos + len(unit))
        spans.extend([span] * len(piece))
        pos += len(unit)
    return _Tracked("".join(pieces), spans)


def _tracked_per_char(tracked: _Tracked, func: Callable[[str], str]) -> _Tracked:
    """Apply a per-character string method while tracking original spans."""
    return _remap(tracked, [func(char) for char in tracked.text], func(tracked.text))


def _tracked_sub(tracked: _Tracked, pattern: Pattern[str], repl: str) -> _Tracked:
    """Apply a regex substitution while tracking original spans."""
    parts: list[str] = []
    spans: list[Span | None] = []
    pos = 0
    for match in pattern.finditer(tracked.text):
        parts.append(tracked.text[pos : match.start()])
        spans.extend(tracked.spans[pos : match.start()])
        replacement = match.expand(repl)
        span = tracked.span_of(match.start(), match.end())
        spans.extend([span] * len(replacement))
        parts.append(replacement)
        pos = match.end()
    parts.append(tracked.text[pos:])
    spans.extend(tracked.spans[pos:])
    return _Tracked("".join(parts), spans)


def _tracked_input_transform(tracked: _Tracked, alg: NSType) -> _Tracked:
    """Mirror input_string_transform_factory while tracking original spans."""
    lowfirst = alg & ns.LOWERCASEFIRST
    dumb = alg & NS_DUMB
    if (dumb and not lowfirst) or (lowfirst and not dumb):
        tracked = _tracked_per_char(tracked, str.swapcase)
    if alg & ns.IGNORECASE:
        tracked = _tracked_per_char(tracked, str.casefold)
    if alg & ns.LOCALENUM:
        tracked = _tracked_sub(tracked, utils._strip_thousands_regex(alg), "")  # noqa: SLF001
        switch_decimal_re = utils._switch_decimal_regex(alg)  # noqa: SLF001
        if switch_decimal_re is not None:
            tracked = _tracked_sub(tracked, switch_decimal_re, ".")
    return tracked


def _split_tracked(
    tracked: _Tracked,
    regex: Pattern[str],
) -> list[tuple[str, Span | None]]:
    """Split into numeric and non-numeric components, keeping spans."""
    components: list[tuple[str, Span | None]] = []
    pos = 0
    for match in regex.finditer(tracked.text):
        if match.start() > pos:
            components.append(
                (tracked.text[pos : match.start()], tracked.span_of(pos, match.start()))
            )
        components.append((match.group(), tracked.span_of(match.start(), match.end())))
        pos = match.end()
    if pos < len(tracked.text):
        components.append((tracked.text[pos:], tracked.span_of(pos, len(tracked.text))))
    return components


def _string_leaves(x: str, ctx: _Ctx) -> tuple[list[_Leaf], _Leaf]:
    """
    Parse a string like parse_string_factory, tracking original spans.

    Returns the leaves aligned with the flat portion of the sort key and
    the leaf for the "first letter" gross-sort component (only used with
    ``UNGROUPLETTERS | LOCALEALPHA``).
    """
    alg = ctx.alg
    # Mirror parse_string_factory: normalize, transform, compose, split.
    form: _NormalizeForm = "NFKD" if alg & ns.COMPATIBILITYNORMALIZE else "NFD"
    decomposed = _tracked_normalize(_Tracked.identity(x), form)
    transformed = _tracked_input_transform(decomposed, alg)
    original = decomposed if alg & NS_DUMB and alg & ns.LOCALEALPHA else transformed
    ready = transformed
    if alg & ns.LOCALEALPHA:
        compose_form: _NormalizeForm = (
            "NFKC" if alg & ns.COMPATIBILITYNORMALIZE else "NFC"
        )
        ready = _tracked_normalize(transformed, compose_form)
    components = _split_tracked(ready, ctx.regex)
    values = list(ctx.component_transform(text for text, _ in components))

    group_letters = (alg & ns.GROUPLETTERS) or (alg & ns.LOCALEALPHA and alg & NS_DUMB)
    leaves: list[_Leaf] = []
    prev_numeric = True  # ensures a separator before a leading number
    for (_text, span), value in zip(components, values):
        numeric = type(value) in (int, float)
        if numeric:
            if prev_numeric:
                leaves.append(_Leaf(None, "separator", _SEP_NOTE))
            leaves.append(_Leaf(span, "number"))
        else:
            notes = []
            if group_letters:
                notes.append(_GROUP_NOTE)
            if alg & ns.LOCALEALPHA:
                notes.append(_LOCALE_NOTE)
            leaves.append(_Leaf(span, "string", "; ".join(notes) or None))
        prev_numeric = numeric

    first = _Leaf(original.span_of(0, 1), "first-letter", _FIRST_LETTER_NOTE)
    return leaves, first


def _kind_of(value: Any) -> str:  # noqa: ANN401
    """Classify a key component for review purposes."""
    if isinstance(value, bool):
        return "opaque"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bytes):
        return "bytes"
    return "opaque"


# A tree of leaves mirroring the (possibly nested) hierarchy of a sort key.
_SpanTree = Union[_Leaf, list["_SpanTree"]]


def _mirror(node: Any) -> _SpanTree:  # noqa: ANN401
    """Mirror a key's hierarchy with span-less leaves."""
    if isinstance(node, tuple):
        return [_mirror(element) for element in node]
    return _Leaf(None, _kind_of(node))


def _shift_tree(tree: list[_SpanTree], offset: int) -> list[_SpanTree]:
    """Shift every span in a span tree by a constant offset."""
    shifted: list[_SpanTree] = []
    for node in tree:
        if isinstance(node, list):
            shifted.append(_shift_tree(node, offset))
        elif node.span is None:
            shifted.append(node)
        else:
            span = Span(node.span.start + offset, node.span.end + offset)
            shifted.append(node._replace(span=span))
    return shifted


def _flat_string_tree(x: str, key: Any, ctx: _Ctx) -> list[_SpanTree]:  # noqa: ANN401
    """Build the span tree for a string parsed without PATH."""
    leaves, first = _string_leaves(x, ctx)
    if ctx.alg & ns.UNGROUPLETTERS and ctx.alg & ns.LOCALEALPHA:
        # key == ((first_letter,), rest), or ((), ()) for empty input -
        # keep the hierarchy intact.
        if (
            len(key) == 2  # noqa: PLR2004
            and isinstance(key[0], tuple)
            and isinstance(key[1], tuple)
            and len(key[0]) == (1 if leaves else 0)
            and len(key[1]) == len(leaves)
        ):
            return [[first] if key[0] else [], list(leaves)]
        return cast("list[_SpanTree]", _mirror(key))
    if isinstance(key, tuple) and len(key) == len(leaves):
        return list(leaves)
    # Defensive fallback: if the tracked parse ever diverges from the real
    # pipeline, report the key without spans rather than misattribute them.
    return cast("list[_SpanTree]", _mirror(key))


def _string_span_tree(x: str, key: Any, ctx: _Ctx) -> list[_SpanTree]:  # noqa: ANN401
    """Build the span tree for a string, descending into path components."""
    if not (ctx.alg & ns.PATH):
        return _flat_string_tree(x, key, ctx)
    parts = list(utils.path_splitter(x))
    if not (isinstance(key, tuple) and len(key) == len(parts)):
        return cast("list[_SpanTree]", _mirror(key))
    tree: list[_SpanTree] = []
    offset = 0
    for part, subkey in zip(parts, key):
        index = x.find(part, offset)
        if index < 0:
            # The path splitter normalized the part away from the original
            # text; report that part without spans.
            tree.append(_mirror(subkey))
            continue
        offset = index + len(part)
        tree.append(_shift_tree(_flat_string_tree(part, subkey, ctx), index))
    return tree


def _build_span_tree(value: Any, key: Any, ctx: _Ctx) -> _SpanTree:  # noqa: ANN401
    """Build a tree of leaves mirroring the hierarchy of a sort key."""
    if isinstance(value, (str, PurePath)):
        return _string_span_tree(str(value), key, ctx)
    if isinstance(value, bytes):
        return _mirror(key)
    if isinstance(value, Iterable) and isinstance(key, tuple):
        subtrees = [_build_span_tree(v, k, ctx) for v, k in zip(value, key)]
        return subtrees if len(subtrees) == len(key) else _mirror(key)
    return _mirror(key)


def _compare(left: Any, right: Any) -> tuple[int, str | None]:  # noqa: ANN401
    """Compare two key components the way Python's tuple comparison would."""
    try:
        if left < right:
            return -1, None
        if left > right:
            return 1, None
    except TypeError:
        note = "components not directly comparable; compared repr() instead"
        left_repr, right_repr = repr(left), repr(right)
        if left_repr < right_repr:
            return -1, note
        if left_repr > right_repr:
            return 1, note
        return 0, note
    else:
        return 0, None


def _make_component(  # noqa: PLR0913
    path: tuple[int, ...],
    left: Any,  # noqa: ANN401
    right: Any,  # noqa: ANN401
    left_leaf: _Leaf,
    right_leaf: _Leaf,
    left_source: str | None,
    right_source: str | None,
    extra_note: str | None,
) -> ExplainedComponent:
    """Combine a compared pair of key components and their leaves."""

    def text_of(leaf: _Leaf, source: str | None) -> str | None:
        if leaf.span is None or source is None:
            return None
        return source[leaf.span.start : leaf.span.end]

    kind = (
        left_leaf.kind
        if left_leaf.kind == right_leaf.kind
        else (f"{left_leaf.kind}/{right_leaf.kind}")
    )
    notes = [left_leaf.note, right_leaf.note, extra_note]
    return ExplainedComponent(
        path=path,
        left=left,
        right=right,
        kind=kind,
        left_span=left_leaf.span,
        right_span=right_leaf.span,
        left_text=text_of(left_leaf, left_source),
        right_text=text_of(right_leaf, right_source),
        decisive=False,
        note="; ".join(dict.fromkeys(note for note in notes if note)) or None,
    )


def _walk(  # noqa: PLR0913
    left_key: Any,  # noqa: ANN401
    right_key: Any,  # noqa: ANN401
    left_tree: _SpanTree,
    right_tree: _SpanTree,
    left_source: str | None,
    right_source: str | None,
) -> tuple[list[ExplainedComponent], int]:
    """
    Compare two keys the way Python does, recording each component.

    Returns the aligned components in comparison order (stopping at the
    decisive one) and the comparison result (-1, 0, or 1).
    """
    components: list[ExplainedComponent] = []
    missing = _Leaf(None, "missing")
    result = 0

    def leaf_of(node: object, value: object) -> _Leaf:
        # The span tree mirrors the key hierarchy; coerce anything
        # unexpected (e.g. mismatched nesting) to a span-less leaf.
        return node if isinstance(node, _Leaf) else _Leaf(None, _kind_of(value))

    def visit(
        lk: tuple,
        rk: tuple,
        lt: list[_SpanTree],
        rt: list[_SpanTree],
        path: tuple[int, ...],
    ) -> None:
        nonlocal result
        for index in range(min(len(lk), len(rk))):
            lval, rval = lk[index], rk[index]
            subpath = (*path, index)
            if isinstance(lval, tuple) and isinstance(rval, tuple):
                lsub = lt[index] if isinstance(lt[index], list) else _mirror(lval)
                rsub = rt[index] if isinstance(rt[index], list) else _mirror(rval)
                visit(
                    lval,
                    rval,
                    cast("list[_SpanTree]", lsub),
                    cast("list[_SpanTree]", rsub),
                    subpath,
                )
            else:
                outcome, note = _compare(lval, rval)
                components.append(
                    _make_component(
                        subpath,
                        lval,
                        rval,
                        leaf_of(lt[index], lval),
                        leaf_of(rt[index], rval),
                        left_source,
                        right_source,
                        note,
                    )
                )
                if outcome:
                    result = outcome
            if result:
                return
        if not result and len(lk) != len(rk):
            # One key is a prefix of the other; the shorter sorts first.
            index = min(len(lk), len(rk))
            result = -1 if len(lk) < len(rk) else 1
            shorter = "left" if result == -1 else "right"
            lval = lk[index] if index < len(lk) else MISSING
            rval = rk[index] if index < len(rk) else MISSING
            components.append(
                _make_component(
                    (*path, index),
                    lval,
                    rval,
                    leaf_of(lt[index], lval) if index < len(lt) else missing,
                    leaf_of(rt[index], rval) if index < len(rt) else missing,
                    left_source,
                    right_source,
                    f"{shorter} key exhausted; the shorter key sorts first",
                )
            )

    visit(
        left_key,
        right_key,
        cast("list[_SpanTree]", left_tree),
        cast("list[_SpanTree]", right_tree),
        (),
    )
    if result and components:
        components[-1] = replace(components[-1], decisive=True)
    return components, result


def _materialize(value: Any) -> Any:  # noqa: ANN401
    """Materialize non-string iterables so they are consumed only once."""
    if isinstance(value, (str, bytes, PurePath)) or not isinstance(value, Iterable):
        return value
    return list(value)


def explain(
    left: Any,  # noqa: ANN401
    right: Any,  # noqa: ANN401
    key: Callable[[Any], Any] | None = None,
    alg: NSType = ns.DEFAULT,
) -> ComparisonExplanation:
    """
    Explain how natsort orders *left* relative to *right*.

    This is a reviewable companion to the public comparison entry points
    (:func:`natsort_keygen`, :func:`natsorted`, ...): it computes the
    natural sort keys with the exact same pipeline, then reports which
    segments of the original labels caused the ordering.

    Parameters
    ----------
    left, right : object
        The two values to compare.
    key : callable, optional
        A key used to manipulate each input before parsing, like the
        *key* argument of :func:`natsort_keygen`. It is applied exactly
        once per input, and the explanation and the comparison both
        reuse that single value.
    alg : ns enum, optional
        The sorting policy to use, like the *alg* argument of
        :func:`natsort_keygen`. The default is `ns.INT`.

    Returns
    -------
    explanation : ComparisonExplanation
        A reviewable explanation containing the sorting policy, the
        hierarchical sort keys, each compared component with the spans
        of the original strings it came from, the first component that
        decided the order, and any numerically equal components (with
        differing original text) that were skipped along the way.

    See Also
    --------
    natsort_keygen
    natsorted

    Examples
    --------
    Two labels whose numbers are equivalent, decided by the suffix::

        >>> ex = explain("a01x", "a1y")
        >>> ex.result
        -1
        >>> ex.decisive.left, ex.decisive.right
        ('x', 'y')
        >>> [(s.left_text, s.right_text) for s in ex.skipped_equal_numbers]
        [('01', '1')]

    """
    try:
        ns.DEFAULT | alg
    except TypeError:
        msg = "explain: 'alg' argument must be from the enum 'ns'"
        raise ValueError(msg + f", got {alg!s}") from None

    # Apply the user's key exactly once per input; the explanation and
    # the actual comparison both reuse these values.
    left_value = _materialize(key(left)) if key is not None else _materialize(left)
    right_value = _materialize(key(right)) if key is not None else _materialize(right)

    # The actual sort keys, computed with the real natsort pipeline so
    # that the explanation always matches what natsorted would do.
    key_func = natsort_keygen(alg=alg)
    left_key = key_func(left_value)
    right_key = key_func(right_value)

    # Mirror natsort_keygen's policy adjustments for the span tracking.
    if alg & ns.LOCALEALPHA and natsort.compat.locale.dumb_sort():
        alg |= NS_DUMB
    ctx = _Ctx(
        alg=alg,
        regex=utils.regex_chooser(alg),
        component_transform=utils.string_component_transform_factory(alg),
    )

    left_tree = _build_span_tree(left_value, left_key, ctx)
    right_tree = _build_span_tree(right_value, right_key, ctx)
    left_source = str(left_value) if isinstance(left_value, (str, PurePath)) else None
    right_source = (
        str(right_value) if isinstance(right_value, (str, PurePath)) else None
    )
    components, result = _walk(
        left_key,
        right_key,
        left_tree,
        right_tree,
        left_source,
        right_source,
    )

    skipped = tuple(
        SkippedEqualNumber(
            path=component.path,
            value=component.left,
            left_span=component.left_span,
            right_span=component.right_span,
            left_text=component.left_text,
            right_text=component.right_text,
        )
        for component in components
        if not component.decisive
        and isinstance(component.left, (int, float))
        and isinstance(component.right, (int, float))
        and not isinstance(component.left, bool)
        and not isinstance(component.right, bool)
        and component.left == component.right
        and component.left_text is not None
        and component.right_text is not None
        and component.left_text != component.right_text
    )

    return ComparisonExplanation(
        left=left,
        right=right,
        left_value=left_value,
        right_value=right_value,
        left_key=left_key,
        right_key=right_key,
        alg=alg,
        result=result,
        components=tuple(components),
        skipped_equal_numbers=skipped,
    )
