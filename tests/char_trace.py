"""Character-level tracing helpers for cross-representation regression tests.

When a roster import splits a "Display Name <local@domain>" field into
columns, customer support needs to map a rejection back to the exact
characters of the original field. These helpers re-run the transformations
that email_validator.syntax applies (splitting, unquoting, unescaping,
Unicode normalization, UTS-46 remapping) while tracking, for every
character that actually enters a syntax check, the offset of the source
character it came from.

Everything here is test infrastructure: the ground truth for behavior is
always the library itself (split_email / validate_email); the tracked
re-implementations exist only to recover source offsets.
"""

import difflib
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

import idna

from email_validator import EmailNotValidError, EmailSyntaxError, validate_email
from email_validator.syntax import split_email

# Legal stand-ins for the regions that are not under test, so that a
# region's check can be exercised with the other fields fixed to legal
# text ("其他字段固定为合法文本").
LEGAL_LOCAL_PART = "mailbox"
LEGAL_DOMAIN = "example.com"

# Keyword arguments that enable every optional representation so that
# display names, quoted local parts, and internationalized text are all
# judged on their syntax rather than rejected for being optional forms.
ALL_REPRESENTATIONS = dict(
    check_deliverability=False,
    allow_quoted_local=True,
    allow_display_name=True,
)


# ---------------------------------------------------------------------------
# Identifiable rendering of invisible characters
# ---------------------------------------------------------------------------

def render_char(c: str) -> str:
    """Render a single character so it is identifiable in an offline report.

    Letters, numbers, punctuation, and symbols print as themselves.
    Everything else -- control, format, separator, combining, surrogate,
    private-use, and unassigned characters -- is replaced by a
    ``⟦U+XXXX NAME⟧` token so invisible characters stay recognizable
    when the report is viewed as plain text or diffed.
    """
    if unicodedata.category(c)[0] in ("L", "N", "P", "S"):
        return c
    code = f"U+{ord(c):04X}" if ord(c) <= 0xFFFF else f"U+{ord(c):08X}"
    name = unicodedata.name(c, "<no name>")
    return f"⟦{code} {name}⟧"


def render_chars(text: str) -> str:
    """Render a string with every invisible character made identifiable."""
    return "".join(render_char(c) for c in text)


# ---------------------------------------------------------------------------
# Tracked splitting: same decisions as split_email, but with offsets
# ---------------------------------------------------------------------------

def _split_index(text: str, specials: tuple[str, ...]) -> Optional[int]:
    """Index of the first unquoted special character, mirroring
    split_string_at_unquoted_special in email_validator.syntax."""
    inside_quote = False
    escaped = False
    for i, c in enumerate(text):
        # A character that merges with a following combining character
        # under NFC is never treated as a special symbol.
        if unicodedata.normalize("NFC", text[i:i + 32])[0] != c:
            continue
        elif inside_quote:
            if c == '\\' and not escaped:
                escaped = True
            elif c == '"' and not escaped:
                inside_quote = False
                escaped = False
            else:
                escaped = False
        elif c == '"':
            inside_quote = True
        elif c in specials:
            return i
    return None


@dataclass
class EmailParts:
    """The regions of a source input, as raw source text with offsets."""
    display_name: Optional[str]  # source text incl. quotes, None if no angle brackets
    display_name_span: Optional[tuple[int, int]]
    local_part: str              # source text incl. quotes if quoted
    local_part_span: tuple[int, int]
    domain: str
    domain_span: tuple[int, int]


def extract_parts(source: str) -> EmailParts:
    """Split a source input into region source texts with offsets.

    Raises EmailSyntaxError exactly when split_email does; otherwise the
    returned regions are guaranteed to agree with split_email's output.
    """
    # Ground truth first: raises the library's own error if unsplittable.
    display_name, local_part, _unescaped, _quoted, domain = split_email(source)

    first = _split_index(source, ("@", "<"))
    assert first is not None  # split_email succeeded, so a split point exists

    if source[first] == "<":
        display_span = (0, len(source[:first].rstrip()))
        display_source: Optional[str] = source[:first].rstrip()
        right = source[first:].rstrip(" ")
        addr_spec = right[1:-1]  # strip angle brackets
        addr_base = first + 1
    else:
        display_span = None
        display_source = None
        addr_spec = source
        addr_base = 0

    at = _split_index(addr_spec, ("@",))
    assert at is not None
    local_source = addr_spec[:at]
    domain_source = addr_spec[at + 1:]

    parts = EmailParts(
        display_name=display_source,
        display_name_span=display_span,
        local_part=local_source,
        local_part_span=(addr_base, addr_base + len(local_source)),
        domain=domain_source,
        domain_span=(addr_base + at + 1, addr_base + at + 1 + len(domain_source)),
    )

    # The tracked split must agree with the library on the region texts.
    assert parts.local_part == local_part
    assert parts.domain == domain
    if display_name is None:
        assert parts.display_name is None
    return parts


# ---------------------------------------------------------------------------
# Tracked transformations: unquoting, unescaping, normalization
# ---------------------------------------------------------------------------

@dataclass
class CheckedFragment:
    """One stage of a region's pipeline.

    text is the fragment that enters a check (or is stored); offsets[i]
    is the offset in the original input of the source character that
    produced text[i], or None for characters with no source (none of the
    transformations here invent characters, but normalization can merge
    them, in which case the merged character maps to the first source
    character of its cluster).
    """
    stage: str
    text: str
    offsets: list[Optional[int]]
    checked: bool  # True if this fragment actually enters a syntax check


@dataclass
class RegionTrace:
    region: str  # "display_name" | "local_part" | "domain"
    source_span: tuple[int, int]
    source_text: str
    fragments: list[CheckedFragment] = field(default_factory=list)


def _unquote_tracked(text: str, base: int) -> tuple[str, list[int]]:
    """Mirror unquote_quoted_string, returning the unquoted/unescaped
    value and the source offset of each surviving character. Assumes the
    text already passed split_email (so quoting is well-formed)."""
    value: list[str] = []
    offsets: list[int] = []
    quoted = False
    escaped = False
    for i, c in enumerate(text):
        if quoted:
            if escaped:
                value.append(c)
                offsets.append(base + i)  # the escaped char, not the backslash
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '"':
                break
            else:
                value.append(c)
                offsets.append(base + i)
        elif i == 0 and c == '"':
            quoted = True
        else:
            value.append(c)
            offsets.append(base + i)
    return "".join(value), offsets


def _align_offsets(before: str, before_offsets: list[Optional[int]], after: str) -> list[Optional[int]]:
    """Map each character of a normalized string back to a source offset,
    aligning before/after with difflib. Characters created by a merge map
    to the first source character of the replaced block."""
    result: list[Optional[int]] = [None] * len(after)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                result[j1 + k] = before_offsets[i1 + k]
        else:
            fallback = before_offsets[i1] if i1 < i2 else None
            for j in range(j1, j2):
                result[j] = fallback
    return result


def trace_email(source: str) -> list[RegionTrace]:
    """Trace each region of a source input through the transformations
    that lead to its syntax checks, recording the checked fragments and
    the source offset of every character in them.

    Raises EmailSyntaxError when split_email rejects the input (the
    split-time display name checks included).
    """
    parts = extract_parts(source)
    traces: list[RegionTrace] = []

    if parts.display_name is not None:
        assert parts.display_name_span is not None
        base = parts.display_name_span[0]
        unquoted, offsets = _unquote_tracked(parts.display_name, base)
        # split_email checks the unquoted form; validate_email stores the
        # NFC-normalized form.
        nfc = unicodedata.normalize("NFC", unquoted)
        traces.append(RegionTrace(
            region="display_name",
            source_span=parts.display_name_span,
            source_text=parts.display_name,
            fragments=[
                CheckedFragment("unquoted+unescaped", unquoted, offsets, checked=True),
                CheckedFragment("nfc", nfc, _align_offsets(unquoted, offsets, nfc), checked=False),
            ],
        ))

    local_base = parts.local_part_span[0]
    unescaped, local_offsets = _unquote_tracked(parts.local_part, local_base)
    nfc_local = unicodedata.normalize("NFC", unescaped)
    traces.append(RegionTrace(
        region="local_part",
        source_span=parts.local_part_span,
        source_text=parts.local_part,
        fragments=[
            CheckedFragment("unescaped", unescaped, local_offsets, checked=True),
            # validate_email re-validates the NFC form when it differs.
            CheckedFragment("nfc", nfc_local, _align_offsets(unescaped, local_offsets, nfc_local),
                            checked=nfc_local != unescaped),
        ],
    ))

    domain_base = parts.domain_span[0]
    domain_offsets: list[Optional[int]] = [domain_base + i for i in range(len(parts.domain))]
    domain_fragments = [
        CheckedFragment("raw", parts.domain, domain_offsets, checked=True),
    ]
    try:
        remapped = idna.uts46_remap(parts.domain, std3_rules=False, transitional=False)
        domain_fragments.append(
            CheckedFragment("uts46", remapped, _align_offsets(parts.domain, domain_offsets, remapped),
                            checked=True))
    except idna.IDNAError:
        # The raw fragment is what the rejection is reported against.
        pass
    traces.append(RegionTrace(
        region="domain",
        source_span=parts.domain_span,
        source_text=parts.domain,
        fragments=domain_fragments,
    ))

    return traces


# ---------------------------------------------------------------------------
# Region-isolated checks and minimal-range localization
# ---------------------------------------------------------------------------

REGIONS = ("display_name", "local_part", "domain")


def region_predicate(region: str) -> Callable[[str], bool]:
    """Return a predicate telling whether a source input passes the
    check of one region, with the other fields fixed to legal text."""
    def passes(source: str) -> bool:
        try:
            parts = extract_parts(source)
        except EmailSyntaxError:
            # Split-time failures (including the display name checks,
            # which run inside split_email) belong to the input as a
            # whole; no region passes on an unsplittable input.
            return False
        if region == "display_name":
            if parts.display_name is None:
                candidate = f"{LEGAL_LOCAL_PART}@{LEGAL_DOMAIN}"
            else:
                candidate = f"{parts.display_name} <{LEGAL_LOCAL_PART}@{LEGAL_DOMAIN}>"
        elif region == "local_part":
            candidate = f"{parts.local_part}@{LEGAL_DOMAIN}"
        elif region == "domain":
            candidate = f"{LEGAL_LOCAL_PART}@{parts.domain}"
        else:
            raise ValueError(f"unknown region {region!r}")
        try:
            validate_email(candidate, **ALL_REPRESENTATIONS)
        except EmailNotValidError:
            return False
        return True
    return passes


def failing_regions(source: str) -> list[str]:
    """The regions whose own check rejects the source, each judged with
    the other fields fixed to legal text."""
    try:
        extract_parts(source)
    except EmailSyntaxError:
        # The display name checks run inside split_email, so an input
        # with angle brackets can fail before the local part and domain
        # are ever looked at. Replace the display name portion with
        # legal text and see whether the rest then splits and validates;
        # whatever still fails there is a fault of its own region.
        idx = _split_index(source, ("@", "<"))
        if idx is None or source[idx] != "<":
            return []  # structural fault (e.g. missing @-sign)
        fixed = "x " + source[idx:]
        try:
            extract_parts(fixed)
        except EmailSyntaxError:
            return []
        return ["display_name"] + [region for region in failing_regions(fixed)
                                   if region != "display_name"]
    return [region for region in REGIONS if not region_predicate(region)(source)]


def _region_span(source: str, region: str) -> tuple[int, int]:
    """The source span of a region, even when the input as a whole
    fails to split (the display name checks run inside split_email, so
    a display name fault must not hide the display name's own span)."""
    try:
        parts = extract_parts(source)
        span = {"display_name": parts.display_name_span,
                "local_part": parts.local_part_span,
                "domain": parts.domain_span}[region]
        if span is not None:
            return span
        return (0, 0)  # region absent from the input
    except EmailSyntaxError:
        if region == "display_name":
            idx = _split_index(source, ("@", "<"))
            if idx is not None and source[idx] == "<":
                return (0, len(source[:idx].rstrip()))
        return (0, 0)


def find_minimal_deletion_ranges(source: str, region: str) -> list[tuple[int, int]]:
    """All shortest contiguous source ranges whose deletion makes the
    given region's check pass (other fields fixed to legal text).

    Only ranges inside the region's own source span are considered:
    deleting structural characters (the angle brackets, the @-sign)
    would make the region's check pass vacuously by removing the
    region, not by fixing it. Returns every equally short explanation,
    as a list of (start, end) offset pairs into the original input.
    Empty if no deletion helps.
    """
    lo, hi = _region_span(source, region)
    passes = region_predicate(region)
    for length in range(1, hi - lo + 1):
        hits = [(i, i + length)
                for i in range(lo, hi - length + 1)
                if passes(source[:i] + source[i + length:])]
        if hits:
            return hits
    return []


# ---------------------------------------------------------------------------
# Offline diff report
# ---------------------------------------------------------------------------

def diff_report(source: str) -> str:
    """A plain-text offline report for one input: the rendered source,
    each region's checked fragments with their source spans, the
    verdict, and -- for rejected inputs -- the minimal source ranges
    whose deletion fixes the failing region. Invisible characters are
    rendered as ⟦U+XXXX NAME⟧ tokens throughout."""
    lines = [f"input: {render_chars(source)}"]

    try:
        traces = trace_email(source)
    except EmailSyntaxError as e:
        lines.append(f"verdict: REJECTED while splitting: {e}")
        return "\n".join(lines)

    for trace in traces:
        start, end = trace.source_span
        lines.append(f"region {trace.region}: source[{start}:{end}] {render_chars(trace.source_text)}")
        for fragment in trace.fragments:
            role = "checked" if fragment.checked else "stored"
            lines.append(f"  {fragment.stage} ({role}): {render_chars(fragment.text)}")
            if fragment.text:
                pairs = ", ".join(
                    f"{render_chars(ch)}<-[{off}]" if off is not None else f"{render_chars(ch)}<-[-]"
                    for ch, off in zip(fragment.text, fragment.offsets))
                lines.append(f"    source map: {pairs}")
            # Diff the region's source text against the checked fragment
            # so removals (quotes, backslashes) and normalization show up.
            for line in difflib.ndiff([render_chars(trace.source_text)],
                                      [render_chars(fragment.text)]):
                if line[0] != " ":
                    lines.append(f"    diff {line}")

    bad = failing_regions(source)
    if not bad:
        lines.append("verdict: ACCEPTED")
    else:
        for region in bad:
            lines.append(f"verdict: REJECTED in {region}")
            for start, end in find_minimal_deletion_ranges(source, region):
                lines.append(f"  minimal fix: delete source[{start}:{end}] "
                             f"{render_chars(source[start:end])}")
    return "\n".join(lines)
