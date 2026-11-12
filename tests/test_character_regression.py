"""Cross-representation character regression tests.

Roster imports split a "Display Name <local@domain>" field into columns,
so when validation rejects an address, customer support must be able to
trace the rejection back to the exact characters of the original field.
These tests pin down, for the same code point, how it is judged in each
representation (display name, quoted local part, domain); which source
characters actually enter each check after unescaping, unquoting, and
normalization; and the minimal contiguous source ranges responsible for
a rejection.
"""

import difflib

import pytest

from email_validator import EmailSyntaxError, validate_email
from email_validator.syntax import split_email

from char_trace import (
    ALL_REPRESENTATIONS,
    diff_report,
    extract_parts,
    failing_regions,
    find_minimal_deletion_ranges,
    region_predicate,
    render_char,
    render_chars,
    trace_email,
)


def verdict(source: str) -> bool:
    """True if the source passes validation with all optional
    representations (display name, quoted local part) enabled."""
    try:
        validate_email(source, **ALL_REPRESENTATIONS)
        return True
    except EmailSyntaxError:
        return False


# ---------------------------------------------------------------------------
# The same code point is judged separately in each representation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,char,in_display_unquoted,in_display_quoted,in_local_unquoted,in_local_quoted,in_domain",
    [
        ("SPACE", " ", True, True, False, True, False),
        ("LATIN SMALL LETTER E WITH ACUTE", "é", False, True, True, True, True),
        ("COMMA", ",", False, True, False, True, False),
        ("ZERO WIDTH SPACE", "\u200b", False, False, False, False, False),
        ("SOFT HYPHEN", "\u00ad", False, False, False, False, False),
    ],
)
def test_same_codepoint_judged_separately_per_region(
        label, char,
        in_display_unquoted, in_display_quoted,
        in_local_unquoted, in_local_quoted, in_domain):
    # Display name (unquoted and quoted forms).
    assert verdict(f"a{char}b <mailbox@example.com>") == in_display_unquoted, label
    assert verdict(f'"a{char}b" <mailbox@example.com>') == in_display_quoted, label
    # Local part (unquoted and quoted forms).
    assert verdict(f"a{char}b@example.com") == in_local_unquoted, label
    assert verdict(f'"a{char}b"@example.com') == in_local_quoted, label
    # Domain.
    assert verdict(f"mailbox@exa{char}mple.com") == in_domain, label


# ---------------------------------------------------------------------------
# The tracked split agrees with the library and spans index the source
# ---------------------------------------------------------------------------

SPLIT_BATTERY = [
    "mailbox@example.com",
    '"a@b"@example.com',
    '"a\\"b"@example.com',
    "John Doe <mailbox@example.com>",
    '"Doe, John" <"jd"@example.com>',
    "<jd@example.com>",
    "jd@[IPv6:2001:db8::1]",
    '"é" <é@éxample.com>',
    "x@example。com",
]


@pytest.mark.parametrize("source", SPLIT_BATTERY)
def test_tracked_split_agrees_with_library(source):
    parts = extract_parts(source)
    display_name, local_part, _unescaped, _quoted, domain = split_email(source)
    assert parts.local_part == local_part
    assert parts.domain == domain
    for text, span in ((parts.local_part, parts.local_part_span),
                       (parts.domain, parts.domain_span)):
        assert source[span[0]:span[1]] == text
    if display_name is None:
        assert parts.display_name is None
    else:
        assert parts.display_name is not None
        assert source[parts.display_name_span[0]:parts.display_name_span[1]] == parts.display_name


@pytest.mark.parametrize("source", SPLIT_BATTERY)
def test_first_checked_fragment_offsets_point_at_source_characters(source):
    # Every character of the first (pre-normalization) checked fragment
    # is a character of the original field at the recorded offset.
    for trace in trace_email(source):
        first = trace.fragments[0]
        assert first.checked
        for char, offset in zip(first.text, first.offsets):
            assert offset is not None
            assert source[offset] == char
        start, end = trace.source_span
        assert source[start:end] == trace.source_text


# ---------------------------------------------------------------------------
# Source characters are recorded through escaping, unquoting, normalization
# ---------------------------------------------------------------------------

def traces_by_region(source):
    return {trace.region: trace for trace in trace_email(source)}


def test_trace_records_unescaping_of_source_characters():
    # The backslash is consumed by unescaping; the escaped character
    # enters the check and is recorded at its own source offset.
    local = traces_by_region('"a\\bc"@example.com')["local_part"]
    assert local.source_span == (0, 6)
    assert local.source_text == '"a\\bc"'
    unescaped = local.fragments[0]
    assert unescaped.stage == "unescaped"
    assert unescaped.text == "abc"
    assert unescaped.offsets == [1, 3, 4]


def test_trace_records_escaped_quote_offset():
    local = traces_by_region('"a\\"b"@example.com')["local_part"]
    unescaped = local.fragments[0]
    assert unescaped.text == 'a"b'
    assert unescaped.offsets == [1, 3, 4]


def test_trace_records_unquoting_of_display_name():
    display = traces_by_region('"Doe, John" <mailbox@example.com>')["display_name"]
    assert display.source_span == (0, 11)
    unquoted = display.fragments[0]
    assert unquoted.stage == "unquoted+unescaped"
    assert unquoted.text == "Doe, John"
    assert unquoted.offsets == list(range(1, 10))


def test_trace_records_nfc_merge_back_to_source():
    local = traces_by_region("e\u0301@example.com")["local_part"]
    unescaped, nfc = local.fragments
    assert unescaped.text == "e\u0301"
    assert nfc.stage == "nfc"
    assert nfc.text == "é"
    # The merged character maps back to the first source character of
    # its cluster, and the NFC form is re-validated when it differs.
    assert nfc.offsets == [0]
    assert nfc.checked


def test_trace_records_uts46_dot_mapping():
    source = "x@example。com"
    domain = traces_by_region(source)["domain"]
    assert domain.source_span == (2, 13)
    raw, uts46 = domain.fragments
    assert raw.text == "example。com"
    assert uts46.stage == "uts46"
    assert uts46.text == "example.com"
    dot_offset = uts46.offsets[uts46.text.index(".")]
    assert dot_offset == 9
    assert source[dot_offset] == "。"


# ---------------------------------------------------------------------------
# Equivalent spellings must produce the same stored (normalized) value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "canonical,variant",
    [
        ("abc@example.com", '"abc"@example.com'),
        ("abc@example.com", '"a\\bc"@example.com'),
        ("é@example.com", "e\u0301@example.com"),
        ("x@example.com", "x@example。com"),
        ("x@example.com", "x@EXAMPLE.COM"),
        ("x@éxample.com", "x@xn--xample-9ua.com"),
        ('"é" <x@example.com>', '"e\u0301" <x@example.com>'),
    ],
)
def test_equivalent_spellings_share_stored_value(canonical, variant):
    def stored(source):
        result = validate_email(source, **ALL_REPRESENTATIONS)
        return (result.normalized, result.local_part, result.domain,
                result.ascii_domain, result.display_name)
    assert stored(canonical) == stored(variant)


# ---------------------------------------------------------------------------
# Single-position valid/invalid pairs find the deciding region
# ---------------------------------------------------------------------------

SINGLE_POSITION_PAIRS = [
    ("abc@example.com", "ab c@example.com", "local_part"),
    ("abc@example.com", "ab\u200bc@example.com", "local_part"),
    ('"ab"@example.com', '"a\u00adb"@example.com', "local_part"),
    ("abc@example.com", "abc@exa_mple.com", "domain"),
    ("abc@example.com", "abc@example..com", "domain"),
    ("JohnDoe <mailbox@example.com>", "John,Doe <mailbox@example.com>", "display_name"),
]


def single_changed_position(valid: str, invalid: str) -> int:
    """The one position at which the invalid input differs from the
    valid one (an insertion into or replacement of the valid input)."""
    matcher = difflib.SequenceMatcher(a=valid, b=invalid, autojunk=False)
    changed = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    assert len(changed) == 1, "pair must differ at exactly one position"
    tag, i1, i2, j1, j2 = changed[0]
    assert tag in ("insert", "replace")
    assert j2 - j1 == 1, "pair must differ by exactly one character"
    return j1


@pytest.mark.parametrize("valid,invalid,region", SINGLE_POSITION_PAIRS)
def test_single_position_pair_finds_deciding_region(valid, invalid, region):
    assert verdict(valid)
    assert not verdict(invalid)
    # The region isolated by the pair is exactly the failing one.
    assert failing_regions(invalid) == [region]
    # The minimal explanation covers the position the pair changed.
    position = single_changed_position(valid, invalid)
    ranges = find_minimal_deletion_ranges(invalid, region)
    assert any(start <= position < end for start, end in ranges)


# ---------------------------------------------------------------------------
# Rejection causes are located at minimal contiguous source ranges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source,region,expected_ranges",
    [
        # Two equally short explanations: deleting either period fixes
        # the "two periods in a row" fault, so both ranges are kept.
        ("ab..c@example.com", "local_part", [(2, 3), (3, 4)]),
        ("x@example..com", "domain", [(9, 10), (10, 11)]),
        # A single invisible character is the whole explanation.
        ("ab\u200bc@example.com", "local_part", [(2, 3)]),
        ('"a\u00adb"@example.com', "local_part", [(2, 3)]),
        # Two adjacent faults merge into one contiguous range: deleting
        # either comma alone leaves the other one invalid.
        ("John,,Doe <mailbox@example.com>", "display_name", [(4, 6)]),
    ],
)
def test_minimal_contiguous_source_ranges(source, region, expected_ranges):
    assert failing_regions(source) == [region]
    ranges = find_minimal_deletion_ranges(source, region)
    assert ranges == expected_ranges
    # Every reported range genuinely fixes the region's check, with the
    # other fields fixed to legal text.
    passes = region_predicate(region)
    for start, end in ranges:
        assert passes(source[:start] + source[end:])
    # All explanations are equally short (minimality is by construction
    # of the search, which returns the shortest length with any hit).
    assert len({end - start for start, end in ranges}) <= 1


def test_no_deletion_helps_an_empty_region():
    source = "mailbox@"
    assert failing_regions(source) == ["domain"]
    assert find_minimal_deletion_ranges(source, "domain") == []


# ---------------------------------------------------------------------------
# Two faults: which is reported first, and can messages tell them apart
# ---------------------------------------------------------------------------

def error_message(source: str) -> str:
    with pytest.raises(EmailSyntaxError) as excinfo:
        validate_email(source, **ALL_REPRESENTATIONS)
    return str(excinfo.value)


def test_local_fault_is_reported_before_domain_fault():
    source = "a b@exa mple.com"
    assert failing_regions(source) == ["local_part", "domain"]
    message = error_message(source)
    assert "before the @-sign" in message
    assert "after the @-sign" not in message


def test_invalid_character_messages_distinguish_the_two_sources():
    local_message = error_message("a b@example.com")
    domain_message = error_message("ab@exa mple.com")
    assert "before the @-sign" in local_message
    assert "after the @-sign" in domain_message
    assert local_message != domain_message


def test_unsafe_character_messages_do_not_distinguish_the_two_sources():
    # The unsafe-character message is shared by both regions, so the
    # message alone cannot tell a local-part fault from a domain fault;
    # the source offsets in the trace/report are what distinguish them.
    local_only = "a\u200bb@example.com"
    domain_only = "ab@ex\u200bample.com"
    both = "a\u200bb@ex\u200bample.com"
    assert failing_regions(both) == ["local_part", "domain"]
    local_message = error_message(local_only)
    assert error_message(domain_only) == local_message
    # The local part is still the one reported first.
    assert error_message(both) == local_message
    # The report locates each fault in the original field regardless.
    assert "source[1:2]" in diff_report(local_only)
    assert "source[5:6]" in diff_report(domain_only)


# ---------------------------------------------------------------------------
# Invisible characters are identifiable in the offline diff report
# ---------------------------------------------------------------------------

def test_render_char_makes_invisible_characters_identifiable():
    assert render_chars("a.b-c@x") == "a.b-c@x"
    assert render_char("é") == "é"
    assert render_char("\u200b") == "⟦U+200B ZERO WIDTH SPACE⟧"
    assert render_char("\u00ad") == "⟦U+00AD SOFT HYPHEN⟧"
    assert render_char(" ") == "⟦U+0020 SPACE⟧"
    assert render_char("\u0301") == "⟦U+0301 COMBINING ACUTE ACCENT⟧"
    assert render_char("\u2028") == "⟦U+2028 LINE SEPARATOR⟧"


def test_diff_report_makes_invisible_local_part_char_identifiable():
    report = diff_report("ab\u200bc@example.com")
    assert "U+200B" in report
    assert "ZERO WIDTH SPACE" in report
    assert "\u200b" not in report  # no raw invisible character leaks through
    assert "source[2:3]" in report  # minimal fix located in the source


def test_diff_report_makes_invisible_domain_char_identifiable():
    report = diff_report("x@ex\u00adample.com")
    assert "U+00AD" in report
    assert "SOFT HYPHEN" in report
    assert "\u00ad" not in report
    assert "source[4:5]" in report


def test_report_maps_rejection_back_to_original_field_with_display_name():
    # The roster-import scenario: the input is the full original field,
    # and the report's offsets index into it directly.
    source = "John Doe <jd\u00adh@example.com>"
    assert source[12:13] == "\u00ad"
    report = diff_report(source)
    assert "U+00AD" in report
    assert "\u00ad" not in report
    assert "source[12:13]" in report
