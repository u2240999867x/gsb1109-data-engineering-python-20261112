"""Tests for the ns.PRECISE exact decimal sorting algorithm."""

from __future__ import annotations

import pytest

from natsort import natsort_keygen, natsorted, ns
from natsort.precise_decimal import PreciseDecimal, precise_float

PRECISE_REAL = ns.REAL | ns.PRECISE


def test_precise_enum_value() -> None:
    assert ns.PRECISE == ns.PR == 0x4000


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("2e10000", "20e9999"),  # same value, different notation
        ("200e9998", "0.2e10001"),
        ("5", "5.0"),
        ("5", "5e0"),
        ("5", "+5.00e0"),
        ("0.5", "5e-1"),
        ("0", "-0.0e5"),  # all zeros are equal, even signed
        ("1.5", "1.50"),
    ],
)
def test_equal_values_produce_equal_keys(given: str, expected: str) -> None:
    first = PreciseDecimal.parse(given)
    second = PreciseDecimal.parse(expected)
    assert first == second
    assert hash(first) == hash(second)
    assert not first < second
    assert not second < first


@pytest.mark.parametrize(
    ("smaller", "larger"),
    [
        ("20e9999", "21e9999"),  # differ only in a trailing mantissa digit
        ("2e10000", "21e9999"),  # 21e9999 == 2.1e10000
        ("2e10000", "2.00000000000000000001e10000"),
        ("1.00000000000000000001", "1.00000000000000000002"),
        ("9e9999", "1e10000"),  # exponent dominates mantissa
        ("1e-10000", "1e-9999"),
        ("0.0001", "0.001"),
        ("-2e10000", "-1e10000"),  # negatives order by value
        ("-1e10001", "-2e10000"),
        ("-2e10000", "-20e9998"),
        ("-1e-9999", "-1e-10000"),
        ("-1e10000", "0"),  # any negative below zero
        ("-0.0", "1e-10000"),  # zero below any positive
        ("-5", "5"),
    ],
)
def test_values_order_by_exact_value(smaller: str, larger: str) -> None:
    first = PreciseDecimal.parse(smaller)
    second = PreciseDecimal.parse(larger)
    assert first is not None
    assert second is not None
    assert first < second
    assert second > first
    assert first != second


def test_parse_rejects_non_numbers() -> None:
    assert PreciseDecimal.parse("hello") is None
    assert PreciseDecimal.parse("1.2.3") is None
    assert PreciseDecimal.parse("e5") is None
    assert PreciseDecimal.parse("") is None
    assert PreciseDecimal.parse("nan") is None
    assert PreciseDecimal.parse("inf") is None


def test_exponent_may_have_thousands_of_digits() -> None:
    # Exponents larger than Python's int<->str digit limit must work, and
    # the key must not expand to the represented value.
    exponent = "9" * 5000
    smaller = PreciseDecimal.parse("1e" + exponent)
    larger = PreciseDecimal.parse("2e" + exponent)
    way_smaller = PreciseDecimal.parse("1e1" + "0" * 4999)
    assert smaller is not None
    assert larger is not None
    assert way_smaller is not None
    assert way_smaller < smaller < larger


def test_key_size_tracks_input_size_not_value() -> None:
    # A 5000-digit exponent yields a key proportional to 5000 digits,
    # not to 10**(10**5000).
    key = PreciseDecimal.parse("1e" + "9" * 5000)
    assert key is not None
    assert key._key[1].bit_length() < 20000  # noqa: SLF001


def test_negative_exponent_with_many_digits() -> None:
    tiny = PreciseDecimal.parse("1e-" + "9" * 5000)
    tinier = PreciseDecimal.parse("1e-1" + "0" * 5000)
    assert tiny is not None
    assert tinier is not None
    assert tinier < tiny


def test_unicode_decimal_digits() -> None:
    assert PreciseDecimal.parse("۱۱") == PreciseDecimal.parse("11")
    assert PreciseDecimal.parse("١٢.٥") == PreciseDecimal.parse("12.5")


def test_infinity_and_nan() -> None:
    neg_inf = precise_float("-inf")
    pos_inf = precise_float("inf")
    nan_default = precise_float("nan")
    nan_first = precise_float("nan", nan=float("-inf"))
    five = precise_float("5")
    assert neg_inf < five < pos_inf  # type: ignore[operator]
    assert nan_default == pos_inf
    assert nan_first == neg_inf


def test_non_numeric_strings_pass_through() -> None:
    assert precise_float("hello") == "hello"
    transformed = precise_float("hello", key=str.upper)
    assert transformed == "HELLO"


def test_single_unicode_numeral_character() -> None:
    result = precise_float("⅓")
    assert isinstance(result, PreciseDecimal)
    assert result == PreciseDecimal.parse(str(1.0 / 3.0))


def test_same_bucket_samples_keep_input_order() -> None:
    # All of these are the same value, so the original sampling order
    # must be preserved by the stable sort.
    data = ["s 2e10000", "s 20e9999", "s 200e9998", "s 0.2e10001", "s 2000e9997"]
    assert natsorted(data, alg=ns.FLOAT | ns.PRECISE) == data


def test_neighbor_differing_in_last_digit_sorts_separately() -> None:
    # 20e9999 and 2e10000 are equal and keep input order; 21e9999 is larger.
    data = ["v 21e9999", "v 2e10000", "v 20e9999"]
    assert natsorted(data, alg=ns.PRECISE) == ["v 2e10000", "v 20e9999", "v 21e9999"]


def test_negative_values_order_correctly() -> None:
    given = ["v 3e10000", "v 1e-10000", "v 0", "v -1e10000", "v -2e10000", "v -1e10001"]
    expected = [
        "v -1e10001",
        "v -2e10000",
        "v -1e10000",
        "v 0",
        "v 1e-10000",
        "v 3e10000",
    ]
    assert natsorted(given, alg=PRECISE_REAL) == expected


def test_precise_implies_float_parsing() -> None:
    # Without float parsing "2e3" would split into 2, "e", and 3.
    data = ["x3e3", "x2e3", "x20e2"]
    assert natsorted(data, alg=ns.PRECISE) == ["x2e3", "x20e2", "x3e3"]


def test_numbers_beyond_float_range_sort_exactly() -> None:
    # These overflow/underflow a float64 and would all collapse to
    # inf or 0.0 without PRECISE.
    data = ["m 3e400", "m 1e400", "m 2e400", "m 1e-400", "m 5e-401"]
    expected = ["m 5e-401", "m 1e-400", "m 1e400", "m 2e400", "m 3e400"]
    assert natsorted(data, alg=ns.PRECISE) == expected


def test_text_around_numbers_still_splits() -> None:
    data = ["run10 value2e10000", "run2 value3e10000", "run2 value20e9999"]
    expected = ["run2 value20e9999", "run2 value3e10000", "run10 value2e10000"]
    assert natsorted(data, alg=ns.PRECISE) == expected


def test_path_hierarchy_still_applies() -> None:
    data = ["f/3e10000.txt", "f/2e10000.txt", "f/20e9999.txt"]
    expected = ["f/2e10000.txt", "f/20e9999.txt", "f/3e10000.txt"]
    assert natsorted(data, alg=ns.PATH | ns.FLOAT | ns.PRECISE) == expected


def test_adjacent_numbers_and_text_mix() -> None:
    # Adjacent numeric components must not break tuple alignment.
    # ".10" parses as 0.10, which is less than 0.2.
    data = ["1.5a", "1.5.2", "1.5.10"]
    assert natsorted(data, alg=ns.FLOAT | ns.PRECISE) == ["1.5.10", "1.5.2", "1.5a"]


def test_sign_policy_still_respected() -> None:
    # Without SIGNED, the "-" is text and is not part of the number.
    data = ["a-2", "a-10", "a1"]
    assert natsorted(data, alg=ns.PRECISE) == ["a1", "a-2", "a-10"]
    assert natsorted(data, alg=PRECISE_REAL) == ["a-10", "a-2", "a1"]


def test_keygen_produces_precise_decimal_components() -> None:
    key = natsort_keygen(alg=PRECISE_REAL)
    parsed = key("a-2.5e3")
    assert isinstance(parsed[1], PreciseDecimal)
    assert parsed[1] == PreciseDecimal.parse("-2.5e3")


def test_nan_and_infinity_labels() -> None:
    # NaN is treated as -inf by default and ties with "-inf" (stable).
    assert natsorted(["5", "inf", "-inf", "nan"], alg=PRECISE_REAL) == [
        "-inf",
        "nan",
        "5",
        "inf",
    ]
    assert natsorted(["5", "inf", "-inf", "nan"], alg=PRECISE_REAL | ns.NANLAST) == [
        "-inf",
        "5",
        "inf",
        "nan",
    ]


def test_default_algorithm_is_unchanged() -> None:
    # PRECISE is opt-in; float parsing still overflows to inf by default.
    assert natsorted(["a2e400", "a1e400"], alg=ns.FLOAT) == ["a2e400", "a1e400"]
