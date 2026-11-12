#!/usr/bin/env python3
"""Verify encoding equivalence classes by comparing byte-level mappings.

For single-byte encodings: decode every byte 0x00-0xFF through both encodings
and check if one is a true superset of the other for printable characters
(0x20-0x7E and 0x80-0xFF).

For multi-byte encodings: report the known relationship from encoding standards
since byte-level comparison is not meaningful.
"""

from __future__ import annotations

import codecs
from itertools import combinations

# ---------------------------------------------------------------------------
# Equivalence groups to verify
# ---------------------------------------------------------------------------

GROUPS_TO_VERIFY = [
    ("ascii", "utf-8"),
    ("utf-16", "utf-16-le", "utf-16-be"),
    ("utf-32", "utf-32-le", "utf-32-be"),
    ("gb2312", "gb18030"),
    ("shift_jis", "cp932"),
    ("euc-kr", "cp949"),
    ("cp874", "tis-620", "iso-8859-11"),
    ("iso-8859-8", "windows-1255"),
    ("iso-8859-7", "windows-1253"),
    ("iso-8859-9", "windows-1254"),
    ("iso-8859-2", "windows-1250", "iso-8859-16"),
    ("iso-8859-1", "windows-1252", "iso-8859-15"),
    ("cp037", "cp500", "cp1026"),
    ("cp850", "cp858"),
    ("koi8-r", "koi8-u"),
    ("iso-8859-13", "windows-1257"),
]

# Encodings that are multi-byte (byte-level comparison is not meaningful)
MULTI_BYTE_ENCODINGS = {
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
    "gb2312",
    "gb18030",
    "shift_jis",
    "cp932",
    "euc-kr",
    "cp949",
}

# Known relationships for multi-byte encodings from standards
KNOWN_MULTIBYTE_RELATIONSHIPS = {
    (
        "ascii",
        "utf-8",
    ): "ascii is a subset of utf-8 by specification (ASCII maps to identical bytes in UTF-8)",
    (
        "utf-16",
        "utf-16-le",
    ): "utf-16 = utf-16-le/utf-16-be + BOM; Python utf-16 auto-detects byte order via BOM",
    (
        "utf-16",
        "utf-16-be",
    ): "utf-16 = utf-16-le/utf-16-be + BOM; Python utf-16 auto-detects byte order via BOM",
    (
        "utf-16-le",
        "utf-16-be",
    ): "Same character repertoire (all Unicode), just different byte order",
    (
        "utf-32",
        "utf-32-le",
    ): "utf-32 = utf-32-le/utf-32-be + BOM; Python utf-32 auto-detects byte order via BOM",
    (
        "utf-32",
        "utf-32-be",
    ): "utf-32 = utf-32-le/utf-32-be + BOM; Python utf-32 auto-detects byte order via BOM",
    (
        "utf-32-le",
        "utf-32-be",
    ): "Same character repertoire (all Unicode), just different byte order",
    (
        "gb2312",
        "gb18030",
    ): "gb2312 is a strict subset of gb18030 by specification (GB 18030 extends GB 2312)",
    (
        "shift_jis",
        "cp932",
    ): "shift_jis is a subset of cp932 by specification (CP932 = Shift_JIS + Microsoft extensions)",
    (
        "euc-kr",
        "cp949",
    ): "euc-kr is a subset of cp949 by specification (CP949 = EUC-KR + Microsoft extensions)",
}

# Printable byte ranges for single-byte comparison
PRINTABLE_BYTES = list(range(0x20, 0x7F)) + list(range(0x80, 0x100))


def decode_byte(enc: str, byte_val: int) -> str | None:
    """Try to decode a single byte using the given encoding.

    Returns the decoded character or None if the byte is not valid.
    """
    try:
        return bytes([byte_val]).decode(enc)
    except (UnicodeDecodeError, ValueError):
        return None


def build_printable_map(enc: str) -> dict[int, str | None]:
    """Build a mapping of byte -> decoded character for all printable bytes."""
    return {b: decode_byte(enc, b) for b in PRINTABLE_BYTES}


def is_multibyte_group(group: tuple[str, ...]) -> bool:
    """Check if any encoding in the group is a multi-byte encoding."""
    return any(e.lower() in MULTI_BYTE_ENCODINGS for e in group)


def compare_single_byte_pair(enc_a: str, enc_b: str) -> None:
    """Compare two single-byte encodings at the byte level."""
    map_a = build_printable_map(enc_a)
    map_b = build_printable_map(enc_b)

    # Single pass: classify each printable byte
    different_bytes = []
    only_in_a = []
    only_in_b = []
    decodable_a = 0
    for b in PRINTABLE_BYTES:
        ca = map_a[b]
        cb = map_b[b]
        if ca is not None:
            decodable_a += 1
        if ca is not None and cb is not None and ca != cb:
            different_bytes.append((b, ca, cb))
        elif ca is not None and cb is None:
            only_in_a.append((b, ca))
        elif ca is None and cb is not None:
            only_in_b.append((b, cb))

    # Determine relationship
    if not different_bytes and not only_in_a and not only_in_b:
        # All printable bytes decode identically
        print(f"  {enc_a} = {enc_b}  (identical for all printable bytes)")
        print(f"    All {decodable_a} printable byte mappings are identical.")
        return

    if not different_bytes and not only_in_a and only_in_b:
        # A is a subset of B (B decodes everything A does, plus more)
        print(f"  {enc_a} is a SUBSET of {enc_b}  ({enc_a} < {enc_b})")
        print(
            f"    {enc_b} has {len(only_in_b)} extra byte(s) that {enc_a} cannot decode:"
        )
        for b, c in only_in_b[:10]:
            print(f"      0x{b:02X} -> {c!r} (U+{ord(c):04X} {_char_name(c)})")
        if len(only_in_b) > 10:
            print(f"      ... and {len(only_in_b) - 10} more")
        return

    if not different_bytes and only_in_a and not only_in_b:
        # B is a subset of A
        print(f"  {enc_a} is a SUPERSET of {enc_b}  ({enc_a} > {enc_b})")
        print(
            f"    {enc_a} has {len(only_in_a)} extra byte(s) that {enc_b} cannot decode:"
        )
        for b, c in only_in_a[:10]:
            print(f"      0x{b:02X} -> {c!r} (U+{ord(c):04X} {_char_name(c)})")
        if len(only_in_a) > 10:
            print(f"      ... and {len(only_in_a) - 10} more")
        return

    # Neither is a superset of the other
    print(f"  {enc_a} != {enc_b}  (NEITHER is a superset)")

    if different_bytes:
        print(f"    {len(different_bytes)} byte(s) decode to DIFFERENT characters:")
        for b, ca, cb in different_bytes[:15]:
            name_a = _char_name(ca)
            name_b = _char_name(cb)
            print(
                f"      0x{b:02X}: {enc_a}={ca!r} (U+{ord(ca):04X} {name_a})"
                f"  vs  {enc_b}={cb!r} (U+{ord(cb):04X} {name_b})"
            )
        if len(different_bytes) > 15:
            print(f"      ... and {len(different_bytes) - 15} more")

    if only_in_a:
        print(f"    {len(only_in_a)} byte(s) only decodable in {enc_a}:")
        for b, c in only_in_a[:5]:
            print(f"      0x{b:02X} -> {c!r} (U+{ord(c):04X} {_char_name(c)})")
        if len(only_in_a) > 5:
            print(f"      ... and {len(only_in_a) - 5} more")

    if only_in_b:
        print(f"    {len(only_in_b)} byte(s) only decodable in {enc_b}:")
        for b, c in only_in_b[:5]:
            print(f"      0x{b:02X} -> {c!r} (U+{ord(c):04X} {_char_name(c)})")
        if len(only_in_b) > 5:
            print(f"      ... and {len(only_in_b) - 5} more")


def _char_name(c: str) -> str:
    """Get the Unicode name for a character, or UNKNOWN."""
    import unicodedata  # noqa: PLC0415

    try:
        return unicodedata.name(c)
    except ValueError:
        return "UNKNOWN"


def report_multibyte_pair(enc_a: str, enc_b: str) -> None:
    """Report the known relationship for a multi-byte encoding pair."""
    key = (enc_a.lower(), enc_b.lower())
    rev_key = (enc_b.lower(), enc_a.lower())

    if key in KNOWN_MULTIBYTE_RELATIONSHIPS:
        print(f"  {enc_a} <-> {enc_b}: {KNOWN_MULTIBYTE_RELATIONSHIPS[key]}")
    elif rev_key in KNOWN_MULTIBYTE_RELATIONSHIPS:
        print(f"  {enc_a} <-> {enc_b}: {KNOWN_MULTIBYTE_RELATIONSHIPS[rev_key]}")
    else:
        print(
            f"  {enc_a} <-> {enc_b}: No known relationship documented (needs research)"
        )


def verify_group(group: tuple[str, ...]) -> None:
    """Verify all pairs in an equivalence group."""
    # Validate that Python knows these encodings
    for enc in group:
        try:
            codecs.lookup(enc)
        except LookupError:
            print(f"  WARNING: Python does not recognize encoding '{enc}' - skipping")
            return

    multibyte = is_multibyte_group(group)

    for enc_a, enc_b in combinations(group, 2):
        if multibyte:
            report_multibyte_pair(enc_a, enc_b)
        else:
            compare_single_byte_pair(enc_a, enc_b)


def main() -> None:
    print("=" * 80)
    print("ENCODING EQUIVALENCE CLASS VERIFICATION")
    print("=" * 80)
    print()
    print("Checking if equivalence classes have true superset/subset relationships")
    print("for printable bytes (0x20-0x7E, 0x80-0xFF).")
    print()

    for group in GROUPS_TO_VERIFY:
        group_label = " <-> ".join(group)
        print(f"\n{'─' * 70}")
        print(f"Group: {group_label}")
        print(f"{'─' * 70}")
        verify_group(group)

    print(f"\n\n{'=' * 80}")
    print("SUMMARY OF FINDINGS")
    print("=" * 80)

    print("""
For each group, the verdict on whether it forms a valid equivalence class
for encoding detection purposes:

1. ascii <-> utf-8:
   VALID - ASCII is a strict subset of UTF-8.

2. utf-16 <-> utf-16-le <-> utf-16-be:
   VALID - Same character repertoire; only BOM/byte-order differs.

3. utf-32 <-> utf-32-le <-> utf-32-be:
   VALID - Same character repertoire; only BOM/byte-order differs.

4. gb2312 <-> gb18030:
   VALID - GB2312 is a strict subset of GB18030.

5. shift_jis <-> cp932:
   VALID - Shift_JIS is a subset of CP932 (Microsoft extension).

6. euc-kr <-> cp949:
   VALID - EUC-KR is a subset of CP949 (Microsoft extension).

7. cp874 <-> tis-620 <-> iso-8859-11:
   CHECK OUTPUT ABOVE - verify byte-level mappings.

8. iso-8859-8 <-> windows-1255:
   CHECK OUTPUT ABOVE - verify byte-level mappings.

9. iso-8859-7 <-> windows-1253:
   CHECK OUTPUT ABOVE - verify byte-level mappings.

10. iso-8859-9 <-> windows-1254:
    CHECK OUTPUT ABOVE - verify byte-level mappings.

11. iso-8859-2 <-> windows-1250 <-> iso-8859-16:
    CHECK OUTPUT ABOVE - verify byte-level mappings.

12. iso-8859-1 <-> windows-1252 <-> iso-8859-15:
    CHECK OUTPUT ABOVE - verify byte-level mappings.

13. cp037 <-> cp500 <-> cp1026:
    CHECK OUTPUT ABOVE - verify byte-level mappings.

14. cp850 <-> cp858:
    CHECK OUTPUT ABOVE - verify byte-level mappings.

15. koi8-r <-> koi8-u (proposed):
    CHECK OUTPUT ABOVE - verify byte-level mappings.

16. iso-8859-13 <-> windows-1257 (proposed):
    CHECK OUTPUT ABOVE - verify byte-level mappings.
""")


if __name__ == "__main__":
    main()
