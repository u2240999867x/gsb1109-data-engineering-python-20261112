import pytest

from email_validator import (
    EmailListItem,
    EmailListParser,
    EmailListRejection,
    EmailListResult,
    validate_email,
    validate_email_list,
)
from email_validator.email_list import (
    REASON_EMPTY_ADDRESS,
    REASON_UNCLOSED_ANGLE,
    REASON_UNCLOSED_QUOTE,
)

# Options shared by the tests: no network access and the optional parsing
# features exercised below are turned on.
OPTIONS = {
    "check_deliverability": False,
    "allow_display_name": True,
    "allow_quoted_local": True,
}

# A compact, position-independent description of a parse result.
DigestRow = tuple[str, int, int, str]


def digest(results: list[EmailListResult]) -> list[DigestRow]:
    # A comparable, position-independent description of a parse result.
    out: list[DigestRow] = []
    for result in results:
        if isinstance(result, EmailListItem):
            out.append(("item", result.start, result.end, result.email.normalized))
        elif isinstance(result, EmailListRejection):
            out.append(("rejection", result.start, result.end, result.reason))
    return out


def parse_whole(field: str) -> list[DigestRow]:
    return digest(validate_email_list([field], **OPTIONS))


def test_single_address_ranges_and_normalization() -> None:
    field = "a@example.com"
    results = validate_email_list([field], check_deliverability=False)
    assert len(results) == 1
    item = results[0]
    assert isinstance(item, EmailListItem)
    assert (item.start, item.end) == (0, len(field))
    assert field[item.start:item.end] == field
    assert item.original == field
    assert item.email == validate_email(field, check_deliverability=False)
    assert item.email.normalized == field


def test_multiple_recipients_with_whitespace() -> None:
    field = "a@example.com, b@example.org"
    results = validate_email_list([field], check_deliverability=False)
    assert len(results) == 2
    first, second = results
    assert isinstance(first, EmailListItem)
    assert isinstance(second, EmailListItem)
    # The intervals point at the trimmed mailbox within the original field.
    assert field[first.start:first.end] == "a@example.com"
    assert field[second.start:second.end] == "b@example.org"
    assert first.email.normalized == "a@example.com"
    assert second.email.normalized == "b@example.org"


def test_comma_inside_quoted_display_name_is_not_a_separator() -> None:
    field = '"Smith, Jane" <jane@example.com>, bob@example.net'
    results = validate_email_list([field], **OPTIONS)
    assert len(results) == 2
    first, second = results
    assert isinstance(first, EmailListItem)
    assert isinstance(second, EmailListItem)
    assert field[first.start:first.end] == '"Smith, Jane" <jane@example.com>'
    assert first.email.normalized == "jane@example.com"
    assert first.email.display_name == "Smith, Jane"
    assert field[second.start:second.end] == "bob@example.net"


def test_escaped_quote_and_backslash_in_display_name() -> None:
    field = r'"A \"B\\C\"" <abc@example.com>'
    results = validate_email_list([field], **OPTIONS)
    assert len(results) == 1
    item = results[0]
    assert isinstance(item, EmailListItem)
    assert field[item.start:item.end] == field
    assert item.email.normalized == "abc@example.com"
    assert item.email.display_name == 'A "B\\C"'


def test_bad_item_has_its_own_reason_and_range() -> None:
    field = "good@example.com, bad@@example.com, also@example.org"
    results = validate_email_list([field], check_deliverability=False)
    assert len(results) == 3
    good, bad, also = results
    assert isinstance(good, EmailListItem)
    assert isinstance(bad, EmailListRejection)
    assert isinstance(also, EmailListItem)
    assert field[bad.start:bad.end] == "bad@@example.com"
    assert "@-sign" in bad.reason
    # The neighbors are still delivered.
    assert good.email.normalized == "good@example.com"
    assert also.email.normalized == "also@example.org"


def test_missing_at_sign_reason() -> None:
    results = validate_email_list(["not-an-address"], check_deliverability=False)
    assert len(results) == 1
    rejection = results[0]
    assert isinstance(rejection, EmailListRejection)
    assert rejection.reason == "An email address must have an @-sign."


def test_empty_element_between_commas_is_zero_width_rejection() -> None:
    field = "a@example.com,, b@example.org"
    results = validate_email_list([field], check_deliverability=False)
    assert len(results) == 3
    item, empty, second = results
    assert isinstance(item, EmailListItem)
    assert isinstance(empty, EmailListRejection)
    assert isinstance(second, EmailListItem)
    assert empty.reason == REASON_EMPTY_ADDRESS
    assert empty.start == empty.end == field.index(",,") + 1
    assert field[empty.start:empty.end] == ""


def test_trailing_comma_does_not_create_phantom_element() -> None:
    field = "a@example.com,"
    results = validate_email_list([field], check_deliverability=False)
    assert len(results) == 1
    item = results[0]
    assert isinstance(item, EmailListItem)
    assert item.email.normalized == "a@example.com"


def test_completely_empty_field_has_no_elements() -> None:
    # An empty list parses to zero elements regardless of how it is split.
    assert validate_email_list([], check_deliverability=False) == []
    assert validate_email_list([""], check_deliverability=False) == []
    assert validate_email_list(["", ""], check_deliverability=False) == []


def test_quote_spans_chunks() -> None:
    # The quoted display name is split at several awkward positions.
    field = '"Smith, Jane" <jane@example.com>, bob@example.net'
    for cut in range(len(field) + 1):
        results = validate_email_list([field[:cut], field[cut:]], **OPTIONS)
        assert digest(results) == parse_whole(field), f"cut at {cut}"


def test_escape_sequence_spans_chunks() -> None:
    # The backslash and the character it escapes land in different chunks.
    field = r'"A \"B\"" <ab@example.com>'
    backslash = field.index("\\")
    for cut in (backslash, backslash + 1, backslash + 2):
        results = validate_email_list([field[:cut], field[cut:]], **OPTIONS)
        assert len(results) == 1
        item = results[0]
        assert isinstance(item, EmailListItem)
        assert item.email.normalized == "ab@example.com"


def test_angle_bracket_address_spans_chunks() -> None:
    field = "Jane <jane@example.com>"
    angle = field.index("<")
    for cut in range(angle, len(field) + 1):
        results = validate_email_list([field[:cut], field[cut:]], **OPTIONS)
        assert len(results) == 1
        item = results[0]
        assert isinstance(item, EmailListItem)
        assert field[item.start:item.end] == field
        assert item.email.normalized == "jane@example.com"


def test_three_way_splits_are_consistent() -> None:
    field = '"Doe, Jane" <jane@example.com>, bad@@x.org, plain@example.net'
    baseline = parse_whole(field)
    for i in range(len(field) + 1):
        for j in range(i, len(field) + 1):
            results = validate_email_list(
                [field[:i], field[i:j], field[j:]], **OPTIONS
            )
            assert digest(results) == baseline, f"split at {i}, {j}"


def test_every_split_position_keeps_ranges_and_rejections_identical() -> None:
    field = (
        '"Doe, Jane" <jane@example.com>, bad@@x.org, '
        r'"esc \"q\"" <eq@example.org>, plain@example.net'
    )
    baseline = parse_whole(field)
    assert [row[0] for row in baseline] == [
        "item", "rejection", "item", "item"
    ]
    for cut in range(len(field) + 1):
        results = validate_email_list([field[:cut], field[cut:]], **OPTIONS)
        assert digest(results) == baseline, f"cut at {cut}"
        # Every interval indexes back into the original field, regardless
        # of where the chunk boundary was.
        for row in digest(results):
            _, start, end, _ = row
            assert 0 <= start <= end <= len(field)


def test_complete_items_are_delivered_immediately() -> None:
    parser = EmailListParser(check_deliverability=False)
    # The first mailbox is complete as soon as the comma is read.
    first = parser.feed("a@example.com,")
    assert len(first) == 1
    assert isinstance(first[0], EmailListItem)
    assert first[0].email.normalized == "a@example.com"

    # Nothing is emitted again for the already-consumed block.
    second = parser.feed("b@example.org")
    assert second == []
    tail = parser.close()
    assert len(tail) == 1
    assert isinstance(tail[0], EmailListItem)
    assert tail[0].email.normalized == "b@example.org"


def test_chunks_cannot_be_reread() -> None:
    parser = EmailListParser(check_deliverability=False)
    parser.feed("a@example.com")
    parser.close()
    with pytest.raises(RuntimeError):
        parser.feed("b@example.org")
    # Closing again is a harmless no-op rather than a duplicate emission.
    assert parser.close() == []


def test_unclosed_quote_resynchronizes_only_at_record_boundary() -> None:
    first_record = '"Jane Doe <jane@example.com>'
    second_record = "next@example.org"

    # With a hard record boundary the malformed record is rejected on its
    # own and the next customer is parsed in a clean state.
    parser = EmailListParser(check_deliverability=False)
    boundary_results = parser.feed(first_record, record_boundary=True)
    assert len(boundary_results) == 1
    rejection = boundary_results[0]
    assert isinstance(rejection, EmailListRejection)
    assert rejection.reason == REASON_UNCLOSED_QUOTE
    assert (rejection.start, rejection.end) == (0, len(first_record))

    next_results = parser.feed(second_record)
    assert next_results == []
    final = parser.close()
    assert len(final) == 1
    item = final[0]
    assert isinstance(item, EmailListItem)
    # The rejection must not have swallowed the next customer.
    assert item.start == len(first_record)
    assert item.email.normalized == "next@example.org"

    # Without a boundary the open quote legitimately continues into the
    # next block, so everything becomes one rejected element.
    joined = first_record + second_record
    no_boundary = validate_email_list([joined], check_deliverability=False)
    assert len(no_boundary) == 1
    assert isinstance(no_boundary[0], EmailListRejection)
    assert (no_boundary[0].start, no_boundary[0].end) == (0, len(joined))


def test_unclosed_angle_bracket_resynchronizes_at_boundary() -> None:
    first_record = "Jane <jane@example.com"
    second_record = "next@example.org"
    parser = EmailListParser(check_deliverability=False, allow_display_name=True)
    results = parser.feed(first_record, record_boundary=True)
    assert len(results) == 1
    rejection = results[0]
    assert isinstance(rejection, EmailListRejection)
    assert rejection.reason == REASON_UNCLOSED_ANGLE
    assert (rejection.start, rejection.end) == (0, len(first_record))

    final = parser.feed(second_record) + parser.close()
    assert len(final) == 1
    assert isinstance(final[0], EmailListItem)
    assert final[0].email.normalized == "next@example.org"


def test_dangling_backslash_inside_quote_at_boundary() -> None:
    record = '"ab\\'
    results = validate_email_list(
        [(record, True), ("next@example.org", False)],
        check_deliverability=False,
    )
    rejection = results[0]
    assert isinstance(rejection, EmailListRejection)
    assert rejection.reason == REASON_UNCLOSED_QUOTE
    assert (rejection.start, rejection.end) == (0, len(record))
    item = results[1]
    assert isinstance(item, EmailListItem)
    assert item.email.normalized == "next@example.org"


def test_record_boundary_splits_into_independent_records() -> None:
    # A hard boundary at any position is equivalent to parsing the text
    # before and after it as two unrelated fields. The offsets of the
    # second record are relative to the concatenated stream, so shift the
    # independently-parsed second record before comparing.
    field = "a@example.com, b@example.org"
    for cut in range(len(field) + 1):
        parser = EmailListParser(check_deliverability=False)
        results = parser.feed(field[:cut], record_boundary=True)
        results += parser.feed(field[cut:])
        results += parser.close()

        prefix = digest(validate_email_list([field[:cut]], check_deliverability=False))
        suffix = [
            (kind, start + cut, end + cut, value)
            for kind, start, end, value in digest(
                validate_email_list([field[cut:]], check_deliverability=False)
            )
        ]
        assert digest(results) == prefix + suffix, f"cut at {cut}"


def test_boundary_at_separator_matches_single_field() -> None:
    # A boundary placed exactly at a separator position does not change
    # the parsed elements compared with parsing the whole field at once.
    field = "a@example.com, b@example.org"
    baseline = parse_whole(field)
    for cut in (0, field.index(",") + 1, len(field)):
        parser = EmailListParser(check_deliverability=False)
        results = parser.feed(field[:cut], record_boundary=True)
        results += parser.feed(field[cut:])
        results += parser.close()
        assert digest(results) == baseline, f"cut at {cut}"


def test_close_reports_unclosed_quote_without_a_boundary() -> None:
    field = '"unclosed'
    results = validate_email_list([field], check_deliverability=False)
    assert len(results) == 1
    rejection = results[0]
    assert isinstance(rejection, EmailListRejection)
    assert rejection.reason == REASON_UNCLOSED_QUOTE
    assert (rejection.start, rejection.end) == (0, len(field))


def test_options_are_forwarded_to_each_address() -> None:
    # Quoted local parts are rejected by default but accepted when enabled.
    field = '"quoted local"@example.com'
    default_results = validate_email_list([field], check_deliverability=False)
    assert isinstance(default_results[0], EmailListRejection)

    enabled = validate_email_list(
        [field], check_deliverability=False, allow_quoted_local=True
    )
    assert isinstance(enabled[0], EmailListItem)
    assert enabled[0].email.normalized == '"quoted local"@example.com'


def test_non_string_chunk_is_a_type_error() -> None:
    parser = EmailListParser(check_deliverability=False)
    with pytest.raises(TypeError, match="chunk must be a str"):
        parser.feed(b"a@example.com")  # type: ignore[arg-type]


def test_syntax_error_messages_are_preserved() -> None:
    field = "me@.bad.com"
    results = validate_email_list([field], check_deliverability=False)
    rejection = results[0]
    assert isinstance(rejection, EmailListRejection)
    assert (
        rejection.reason
        == "An email address cannot have a period immediately after the @-sign."
    )
