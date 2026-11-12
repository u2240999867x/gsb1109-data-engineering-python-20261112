"""
Length-limit regression tests around the two independent byte columns a customer
database typically enforces:

  * the Unicode address column -- the original input and its NFC-normalized form,
    measured in UTF-8 bytes (RFC 6532 / SMTPUTF8);
  * the SMTP envelope column -- the actually transmittable representation, which
    is the all-ASCII form (local part + IDNA-encoded domain) when it exists, or
    the UTF-8 normalized form when SMTPUTF8 is required (RFC 5321 max 254 octets).

For every sample we independently compute four byte lengths without trusting
validator internals:

  original_bytes  len(raw_input.encode("utf-8"))
  nfc_local_bytes len(NFC(local_part).encode("utf-8"))
  idna_domain_bytes  len(IDNA-ASCII(domain))           (RFC 1035 column, <=253)
  envelope_bytes  len(transmittable form.encode("utf-8")) (<=254)

Each boundary sample flips a single layer from writable (exactly 254 bytes) to
over-limit (exactly one byte more) while the neighboring layer stays writable,
so a regression that conflated the layers would move the boundary and fail here.
"""

import unicodedata
from typing import Any

import idna
import pytest

from email_validator import EmailSyntaxError, ValidatedEmail, validate_email
from email_validator.rfc_constants import (
    DOMAIN_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    LOCAL_PART_MAX_LENGTH,
)

# A fixed, ASCII-only suffix keeps the byte arithmetic explicit: one byte for
# the @-sign plus the eleven bytes of "example.com".
DOMAIN = "example.com"
SUFFIX = "@" + DOMAIN
SUFFIX_BYTES = len(SUFFIX.encode("utf-8"))
# Largest local part (in UTF-8 bytes) that still fits the 254-octet envelope.
LOCAL_CAPACITY_BYTES = EMAIL_MAX_LENGTH - SUFFIX_BYTES


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def idna_domain(domain: str) -> str:
    # Independent IDNA encoding of each label using the same IDNA 2008 codec
    # the library wraps, but invoked here directly on the constructed labels.
    return ".".join(
        idna.alabel(label).decode("ascii") if any(ord(ch) > 127 for ch in label) else label
        for label in domain.split(".")
    )


def envelope_representation(info: ValidatedEmail) -> str:
    # The representation an MTA can actually transmit: the 7-bit ASCII form when
    # it exists, otherwise the UTF-8 normalized form that requires SMTPUTF8.
    return info.ascii_email if info.ascii_email is not None else info.normalized


def measure_layers(info: ValidatedEmail) -> dict[str, int]:
    """Layer byte lengths derived from the result with stdlib calls only."""
    return {
        "original": utf8_len(info.original),
        "nfc_local": utf8_len(info.local_part),
        "idna_domain": utf8_len(info.ascii_domain),
        "normalized": utf8_len(info.normalized),
        "envelope": utf8_len(envelope_representation(info)),
    }


def assert_at_writable_boundary(info: ValidatedEmail, **expected: int) -> None:
    layers = measure_layers(info)
    for name, length in expected.items():
        assert layers[name] == length, (name, length, layers)
    # The transmittable envelope must sit exactly on the 254-octet line.
    assert layers["envelope"] == EMAIL_MAX_LENGTH


# ---------------------------------------------------------------------------
# Layer 1 -- the original input overflows, on an address that needs SMTPUTF8.
# ---------------------------------------------------------------------------

def test_original_layer_boundary_requires_smtputf8() -> None:
    # One 3-byte CJK character plus ASCII padding: the local part is exactly
    # LOCAL_CAPACITY_BYTES (242) UTF-8 bytes, so the address totals 254 bytes.
    local = "a" * (LOCAL_CAPACITY_BYTES - 3) + "中"
    assert utf8_len(local) == LOCAL_CAPACITY_BYTES
    address = local + SUFFIX
    assert utf8_len(address) == EMAIL_MAX_LENGTH

    info = validate_email(address, check_deliverability=False)
    assert info.smtputf8 is True
    assert info.ascii_email is None  # no ASCII envelope exists; SMTPUTF8 is mandatory
    assert_at_writable_boundary(info, original=254, nfc_local=242, idna_domain=11,
                                normalized=254, envelope=254)

    # One more ASCII byte pushes only the original over the line.
    one_more = address + "a"
    assert utf8_len(one_more) == EMAIL_MAX_LENGTH + 1
    with pytest.raises(EmailSyntaxError, match=r"1 character too many"):
        validate_email(one_more, check_deliverability=False)

    # One more 3-byte character overflows by three octets and is reported as a
    # byte range because character count no longer equals byte count.
    one_cjk_more = local + "中" + SUFFIX
    assert utf8_len(one_cjk_more) == EMAIL_MAX_LENGTH + 3
    with pytest.raises(EmailSyntaxError, match=r"1-3 characters too many"):
        validate_email(one_cjk_more, check_deliverability=False)


# ---------------------------------------------------------------------------
# Layer 2 -- NFC normalization expands the local part; the original input is
# still writable but the normalized column overflows.
# ---------------------------------------------------------------------------

def test_nfc_layer_overflows_while_original_stays_writable() -> None:
    expansion = chr(0xFB2C)  # Hebrew presentation form; NFC expands 3 bytes -> 6 bytes
    assert utf8_len(expansion) == 3
    assert utf8_len(nfc(expansion)) == 6

    # 40 expanded sequences (240 NFC bytes) plus a 2-byte pad put the NFC local
    # part at exactly 242 bytes -> normalized address of 254 bytes.
    boundary_local = expansion * 40 + "aa"
    assert utf8_len(boundary_local) == 122          # original column has room to spare
    assert utf8_len(nfc(boundary_local)) == LOCAL_CAPACITY_BYTES

    boundary = boundary_local + SUFFIX
    info = validate_email(boundary, check_deliverability=False)
    assert info.smtputf8 is True
    assert_at_writable_boundary(info, original=134, nfc_local=242, idna_domain=11,
                                normalized=254, envelope=254)

    # A single extra ASCII byte leaves the original well under 254 (135 bytes)
    # but the NFC representation reaches 255 and is the layer that fails.
    over_local = boundary_local + "a"
    over = over_local + SUFFIX
    assert utf8_len(over) <= EMAIL_MAX_LENGTH                       # original still writable
    assert utf8_len(nfc(over_local)) + SUFFIX_BYTES == EMAIL_MAX_LENGTH + 1
    with pytest.raises(EmailSyntaxError,
                       match=r"too long after normalization \(1 byte too many\)"):
        validate_email(over, check_deliverability=False)


# ---------------------------------------------------------------------------
# Layer 3 -- IDNA encoding expands the domain; the Unicode normalized address
# fits but the ASCII SMTP envelope column overflows.
# ---------------------------------------------------------------------------

def _idna_domain_sample(ascii_pad: int) -> tuple[str, str]:
    # Each single-character U+00E9 label occupies 2 UTF-8 bytes as Unicode but
    # seven bytes as its "xn--9ca" A-label, so IDNA grows the domain.
    unicode_labels = ["é"] * 30 + ["x" * ascii_pad, "com"]
    unicode_domain = ".".join(unicode_labels)
    return unicode_domain, idna_domain(unicode_domain)


def test_idna_envelope_overflows_while_normalized_stays_writable() -> None:
    unicode_domain, ascii_domain = _idna_domain_sample(7)
    assert len(ascii_domain.encode("ascii")) == 251                  # RFC 1035 column fits
    boundary = "me@" + unicode_domain
    assert utf8_len(boundary) < EMAIL_MAX_LENGTH                    # Unicode form is short

    info = validate_email(boundary, check_deliverability=False)
    assert info.smtputf8 is False
    assert info.ascii_email is not None
    assert_at_writable_boundary(info, original=104, nfc_local=2, idna_domain=251,
                                normalized=104, envelope=254)

    # Widening the ASCII label by one byte leaves the domain under its own
    # 253-octet limit (252) but pushes the 254-octet total envelope to 255.
    over_domain, over_ascii_domain = _idna_domain_sample(8)
    assert len(over_ascii_domain.encode("ascii")) == 252
    assert len(over_ascii_domain.encode("ascii")) <= DOMAIN_MAX_LENGTH
    over = "me@" + over_domain
    assert utf8_len(over) <= EMAIL_MAX_LENGTH                       # normalized still writable
    with pytest.raises(
        EmailSyntaxError,
        match=r"too long when the part after the @-sign is converted to IDNA ASCII "
              r"\(1 byte too many\)",
    ):
        validate_email(over, check_deliverability=False)


# ---------------------------------------------------------------------------
# Quoted local parts -- ASCII, quoting mandatory because of an embedded space.
# ---------------------------------------------------------------------------

def test_quoted_ascii_local_part_envelope() -> None:
    # 240 bytes of content that cannot appear unquoted (contains a space); the
    # mandatory surrounding quotes bring the local part to 242 bytes -> 254.
    content = "a" * 239 + " "
    assert utf8_len(content) == 240
    boundary = '"' + content + '"' + SUFFIX
    assert utf8_len(boundary) == EMAIL_MAX_LENGTH

    info = validate_email(boundary, allow_quoted_local=True, check_deliverability=False)
    assert info.smtputf8 is False
    assert info.local_part.startswith('"') and info.local_part.endswith('"')  # quotes retained
    assert_at_writable_boundary(info, original=254, nfc_local=242, idna_domain=11,
                                normalized=254, envelope=254)

    with pytest.raises(EmailSyntaxError, match=r"1 character too many"):
        validate_email('"' + "a" * 240 + ' "' + SUFFIX,
                       allow_quoted_local=True, check_deliverability=False)

    # With the option off the same 254-byte string is a feature/syntax rejection,
    # not a capacity rejection -- callers can tell the two apart.
    with pytest.raises(EmailSyntaxError,
                       match=r"Quoting the part before the @-sign is not allowed here\."):
        validate_email(boundary, check_deliverability=False)


# ---------------------------------------------------------------------------
# Quoted local parts -- internationalized content requiring both quoting and
# SMTPUTF8; there is no ASCII envelope for this address.
# ---------------------------------------------------------------------------

def test_quoted_intl_local_part_requires_smtputf8() -> None:
    # 79 CJK characters (237 bytes) + a space that forces quoting + 2 ASCII
    # bytes = 240 bytes of content; quotes make the local part 242 bytes.
    content = "中" * 79 + " " + "aa"
    assert utf8_len(content) == 240
    boundary = '"' + content + '"' + SUFFIX
    assert utf8_len(boundary) == EMAIL_MAX_LENGTH

    info = validate_email(boundary, allow_quoted_local=True,
                          allow_smtputf8=True, check_deliverability=False)
    assert info.smtputf8 is True
    assert info.ascii_email is None
    assert info.local_part.startswith('"')
    assert_at_writable_boundary(info, original=254, nfc_local=242, idna_domain=11,
                                normalized=254, envelope=254)

    with pytest.raises(EmailSyntaxError, match=r"1 character too many"):
        validate_email('"' + content + "a" + '"' + SUFFIX,
                       allow_quoted_local=True, allow_smtputf8=True,
                       check_deliverability=False)

    # Disabling SMTPUTF8 rejects the internationalized characters outright,
    # which is again distinct from the column-not-wide-enough failure.
    with pytest.raises(EmailSyntaxError,
                       match=r"Internationalized characters before the @-sign are not supported"):
        validate_email(boundary, allow_quoted_local=True,
                       allow_smtputf8=False, check_deliverability=False)


# ---------------------------------------------------------------------------
# Re-importing the normalized form must keep every derived length conclusion.
# The `original` layer may shrink (NFC expansion), but NFC local, IDNA domain
# and transmittable envelope byte lengths must be identical and still fit.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "address,options",
    [
        ("a" * (LOCAL_CAPACITY_BYTES - 3) + "中" + SUFFIX, {}),
        (chr(0xFB2C) * 40 + "aa" + SUFFIX, {}),
        ("me@" + _idna_domain_sample(7)[0], {}),
        ('"' + "a" * 239 + ' "' + SUFFIX, {"allow_quoted_local": True}),
        ('"' + "中" * 79 + " " + 'aa"' + SUFFIX,
         {"allow_quoted_local": True, "allow_smtputf8": True}),
    ],
)
def test_reimport_normalized_address_keeps_length_conclusion(
    address: str, options: dict[str, Any]
) -> None:
    options = dict(options, check_deliverability=False)
    first = validate_email(address, **options)
    second = validate_email(first.normalized, **options)

    first_layers = measure_layers(first)
    second_layers = measure_layers(second)
    for layer in ("nfc_local", "idna_domain", "normalized", "envelope"):
        assert first_layers[layer] == second_layers[layer], (layer, first_layers, second_layers)
    assert second_layers["envelope"] == EMAIL_MAX_LENGTH
    assert envelope_representation(second) == envelope_representation(first)


# ---------------------------------------------------------------------------
# Rejected addresses: shrink the offending segment to the critical size and
# prove a syntactically invalid address fails on syntax even though every
# length layer has capacity, so callers can distinguish "invalid" from
# "target column too small".
# ---------------------------------------------------------------------------

def test_syntax_error_is_distinct_from_capacity_at_the_limit() -> None:
    # A 242-byte local part containing consecutive dots -- exactly at the
    # envelope limit -- must fail on the dot rule, never on a length rule.
    bad_local = "a" * 120 + ".." + "a" * 120
    assert utf8_len(bad_local) == LOCAL_CAPACITY_BYTES
    bad_address = bad_local + SUFFIX
    assert utf8_len(bad_address) == EMAIL_MAX_LENGTH

    with pytest.raises(EmailSyntaxError,
                       match=r"An email address cannot have two periods in a row\.") as exc:
        validate_email(bad_address, check_deliverability=False)
    assert "too long" not in str(exc.value)

    # The same byte budget with the dots repaired validates, proving the column
    # genuinely had room and only the syntax was at fault.
    repaired = "a" * LOCAL_CAPACITY_BYTES + SUFFIX
    assert utf8_len(repaired) == EMAIL_MAX_LENGTH
    assert measure_layers(validate_email(repaired, check_deliverability=False))["envelope"] \
        == EMAIL_MAX_LENGTH


def test_strict_local_part_column_is_a_separate_limit() -> None:
    # The 64-character local-part column is enforced only in strict mode and is
    # counted in characters, independently of the 254-octet envelope column.
    at_limit = "a" * LOCAL_PART_MAX_LENGTH + SUFFIX
    assert validate_email(at_limit, strict=True, check_deliverability=False) is not None

    over_limit = "a" * (LOCAL_PART_MAX_LENGTH + 1) + SUFFIX
    with pytest.raises(EmailSyntaxError,
                       match=r"too long before the @-sign \(1 character too many\)"):
        validate_email(over_limit, strict=True, check_deliverability=False)

    # Outside strict mode the over-64-character but under-254-octet address is
    # accepted: the two columns and the strict option remain independently usable.
    assert validate_email(over_limit, strict=False, check_deliverability=False) is not None
    assert utf8_len(over_limit) <= EMAIL_MAX_LENGTH
