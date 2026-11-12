"""
Exact decimal parsing and comparison for natsort.

This module provides :class:`PreciseDecimal`, a sort key that orders
decimal numbers by their exact mathematical value without ever
expanding them into a machine ``int`` or ``float``. The exponent may
contain thousands of digits; the space consumed by a key grows with
the number of characters in the input, not with the magnitude of the
number itself.
"""

from __future__ import annotations

import re
import unicodedata
from functools import total_ordering
from typing import TYPE_CHECKING, Callable, Union

from natsort.compat.fake_fastnumbers import NAN_INF, POTENTIAL_FIRST_CHAR

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

StrOrPreciseDecimal = Union[str, "PreciseDecimal"]

# Matches an optionally-signed decimal number with an optional exponent.
# `\d` is unicode-aware so that non-ASCII decimal digits are accepted,
# consistent with the regular expressions natsort uses to split strings.
_DECIMAL_RE = re.compile(
    r"""
    ^
    (?P<sign>[-+]?)
    (?P<mantissa>\d+(?:\.\d*)?|\.\d+)
    (?:[eE](?P<exponent>[-+]?\d+))?
    $
    """,
    flags=re.VERBOSE,
)

# Number of digits converted to an int at a time. Parsing the exponent
# in chunks avoids Python's limit on the number of digits allowed in
# int<->str conversions, so exponents with thousands of digits work.
_INT_CHUNK = 9

# Sort ranks for the categories of values, in sort order.
_NEG_INF = 0
_NEGATIVE = 1
_ZERO = 2
_POSITIVE = 3
_POS_INF = 4

# Map each ASCII digit to its nines-complement. Used to reverse the
# ordering of digit strings for negative numbers.
_DIGIT_COMPLEMENT = str.maketrans("0123456789", "9876543210")

# Sentinel appended to complemented digit strings. It is ordinal-greater
# than every complemented digit so that a complemented string always
# sorts after any complemented string it is a proper prefix of, which
# keeps the reversed ordering correct when one digit string is a prefix
# of another.
_COMPLEMENT_SENTINEL = ":"


def _digits_to_int(digits: str) -> int:
    """
    Convert a string of (possibly unicode) decimal digits to an int.

    The conversion is performed in small chunks so that digit strings
    longer than Python's int<->str digit limit are supported.
    """
    value = 0
    for start in range(0, len(digits), _INT_CHUNK):
        chunk = digits[start : start + _INT_CHUNK]
        value = value * 10 ** len(chunk) + int(chunk)
    return value


def _normalize_digits(digits: str) -> str:
    """Translate any non-ASCII decimal digits to their ASCII value."""
    if digits.isascii():
        return digits
    return "".join(str(unicodedata.decimal(char)) for char in digits)


@total_ordering
class PreciseDecimal:
    """
    A sort key representing an exact decimal number.

    Instances compare by exact mathematical value, so different textual
    representations of the same number (e.g. ``2e10000`` and ``20e9999``)
    compare as equal. The internal representation is proportional in
    size to the input text, never to the magnitude of the number.

    Not intended to be instantiated directly - use :func:`parse` or
    :func:`try_precise_float`.
    """

    __slots__ = ("_key",)

    _key: tuple[int, int, str]

    def __init__(self, key: tuple[int, int, str]) -> None:  # noqa: D107
        self._key = key

    @classmethod
    def infinity(cls, *, negative: bool = False) -> PreciseDecimal:
        """Return a key representing positive or negative infinity."""
        return cls((_NEG_INF if negative else _POS_INF, 0, ""))

    @classmethod
    def parse(cls, x: str) -> PreciseDecimal | None:
        """
        Parse a string into a precise decimal key.

        Returns `None` if the string is not a decimal number.

        The accepted form is an optionally-signed number with an
        optional decimal fraction and an optional exponent, e.g.
        ``-12.5e+300``. Unicode decimal digits are accepted.
        """
        match = _DECIMAL_RE.match(x)
        if match is None:
            return None

        mantissa = match["mantissa"]
        int_part, _, frac_part = mantissa.partition(".")
        digits = _normalize_digits(int_part + frac_part)

        exponent = 0
        exp_text = match["exponent"]
        if exp_text is not None:
            exponent = _digits_to_int(exp_text.lstrip("+").lstrip("-") or "0")
            if exp_text.startswith("-"):
                exponent = -exponent

        # Normalize to 0.D1D2...Dn * 10**E with D1 != 0 and Dn != 0 so
        # that equal values always produce equal keys.
        stripped = digits.lstrip("0")
        if not stripped:
            # Every representation of zero (including "-0.0e5") is equal.
            return cls((_ZERO, 0, ""))
        leading_zeros = len(digits) - len(stripped)
        stripped = stripped.rstrip("0")
        exponent += len(int_part) - leading_zeros

        if match["sign"] == "-":
            # Reverse the ordering for negatives: larger exponents and
            # larger digit strings must sort first.
            return cls(
                (
                    _NEGATIVE,
                    -exponent,
                    stripped.translate(_DIGIT_COMPLEMENT) + _COMPLEMENT_SENTINEL,
                )
            )
        return cls((_POSITIVE, exponent, stripped))

    def __eq__(self, other: object) -> bool:  # noqa: D105
        if isinstance(other, PreciseDecimal):
            return self._key == other._key
        return NotImplemented

    def __lt__(self, other: object) -> bool:  # noqa: D105
        if isinstance(other, PreciseDecimal):
            return self._key < other._key
        return NotImplemented

    def __hash__(self) -> int:  # noqa: D105
        return hash(self._key)

    def __repr__(self) -> str:  # noqa: D105
        return f"PreciseDecimal({self._key!r})"


def _unicode_numeral_or_key(
    x: str,
    key: Callable[[str], str],
    _uni: Callable[..., object] = unicodedata.numeric,
) -> StrOrPreciseDecimal:
    """Convert a single unicode numeral, otherwise apply the fail key."""
    try:
        if len(x) == 1:
            numeric = _uni(x, None)
            if numeric is not None:
                parsed = PreciseDecimal.parse(str(numeric))
                if parsed is not None:
                    return parsed
        return key(x)
    except TypeError:  # pragma: no cover
        return key(x)


def precise_float(
    x: str,
    key: Callable[[str], str] = lambda x: x,
    nan: float = float("inf"),
    _uni: Callable[..., object] = unicodedata.numeric,
    _nan_inf: frozenset[str] = NAN_INF,
    _first_char: frozenset[str] = POTENTIAL_FIRST_CHAR,
) -> StrOrPreciseDecimal:
    """
    Convert a string to a PreciseDecimal, return input as-is if not possible.

    Mirrors the behavior of ``fast_float`` from the fastnumbers compat
    layer, but produces exact decimal keys instead of floats.

    Parameters
    ----------
    x : str
        String to attempt to convert to a precise decimal.
    key : callable
        Single-argument function to apply to *x* if conversion fails.
    nan : float
        Value NaN should be replaced with; must be positive or negative
        infinity.

    Returns
    -------
    *str* or *PreciseDecimal*

    """
    if x[:1] not in _first_char and x.lstrip()[:3] not in _nan_inf:
        return _unicode_numeral_or_key(x, key, _uni)
    result = PreciseDecimal.parse(x)
    if result is not None:
        return result
    try:
        ret = float(x)
    except ValueError:
        return _unicode_numeral_or_key(x, key, _uni)
    if ret != ret:  # NaN
        return PreciseDecimal.infinity(negative=nan < 0)
    if ret in (float("inf"), float("-inf")):
        return PreciseDecimal.infinity(negative=ret < 0)
    # A finite value that float() accepts but the exact parser does not
    # (e.g. "1_0"); use the shortest exact decimal of the double.
    parsed = PreciseDecimal.parse(repr(ret))
    return parsed if parsed is not None else key(x)


def try_precise_float(
    x: Iterable[str],
    *,
    map: bool,
    nan: float = float("inf"),
    on_fail: Callable[[str], str] = lambda x: x,
) -> Iterator[StrOrPreciseDecimal]:
    """
    Attempt to convert each string in an iterable to a PreciseDecimal.

    Mirrors the ``try_float`` interface of the fastnumbers compat layer.
    """
    assert map is True  # noqa: S101
    return (precise_float(y, nan=nan, key=on_fail) for y in x)
