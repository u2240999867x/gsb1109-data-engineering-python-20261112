"""Identity layers: filtering, detection, and the persisted name.

These tests pin the three things a caller that turns a detected encoding into
a directory name needs to keep apart:

1. the canonical codec the filter actually selected,
2. the text the internal codec produced from the examined window, and
3. the text the persisted name produces when the bytes are decoded later.

Rendering only guarantees (3) *decodes*; it does not guarantee it decodes to
the same text as (2).  The witnesses here are the byte strings that make the
difference visible.

Also pinned: the one place where the three entry points can disagree on the
same bytes -- how far past ``max_bytes`` the caller's input runs, which is
what sets ``input_truncated``.
"""

from __future__ import annotations

import codecs
import warnings

import pytest

import chardet
from chardet.enums import EncodingEra
from chardet.output_names import _COMPAT_NAMES, PREFERRED_SUPERSET
from chardet.registry import REGISTRY, lookup_encoding, normalize_encodings


def _decode(name: str, data: bytes) -> str | None:
    """Return the decoded text, or None when the codec rejects the bytes."""
    try:
        return data.decode(name)
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# Whitelist names select a different real codec than the spelling suggests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("whitelist_name", "canonical"),
    [
        # Familiar spelling -> the internal codec the pipeline really runs.
        ("shift_jis", "shift_jis_2004"),
        ("sjis", "shift_jis_2004"),
        ("cp932", "cp932"),
        ("euc-jp", "euc_jis_2004"),
        ("big5", "big5hkscs"),
        ("cp950", "big5hkscs"),
        ("gb2312", "gb18030"),
        ("gbk", "gb18030"),
        ("cp936", "gb18030"),
        ("iso-2022-jp", "iso2022_jp_2"),
        ("iso-8859-11", "tis-620"),
        ("cp874", "cp874"),
        ("latin-1", "iso8859-1"),
        ("windows-1252", "cp1252"),
        ("uhc", "cp949"),
    ],
)
def test_whitelist_alias_lands_on_a_different_codec(
    whitelist_name: str, canonical: str
) -> None:
    """The filter selects a canonical codec, not the name the caller typed."""
    assert lookup_encoding(whitelist_name) == canonical


def test_normalized_whitelist_keeps_the_alias_target_not_the_spelling() -> None:
    """``iso-8859-11`` and ``tis-620`` are one entry, not two codecs."""
    frozen = normalize_encodings(["iso-8859-11", "tis-620"], "include_encodings")
    assert frozen == frozenset({"tis-620"})


def test_shift_jis_whitelist_admits_jis_x_0213_rows() -> None:
    """A ``shift_jis`` whitelist accepts bytes plain shift_jis rejects."""
    data = ("日本語のテキストです。" * 3).encode("shift_jis_2004")
    data += b"\x5c100"  # 0x5C: yen under 2004, backslash under sjis
    result = chardet.detect(data, include_encodings=["shift_jis"])
    assert result["encoding"] == "SHIFT_JIS"
    assert _decode("shift_jis_2004", data) is not None


def test_cp932_whitelist_rejects_shift_jis_2004_rows() -> None:
    """``cp932`` is its own entry, so a 2004-only row finds no candidate."""
    data = ("日本語のテキストです。" * 3).encode("shift_jis_2004")
    data += "〖".encode("shift_jis_2004")  # CP932 does not map U+3016
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = chardet.detect(data, include_encodings=["cp932"])
    assert result["encoding"] is None


# ---------------------------------------------------------------------------
# Persisted name decodes, but may decode different text
# ---------------------------------------------------------------------------


def test_big5_display_name_is_lossy_but_decodes() -> None:
    """0xC6A1 is ① under big5hkscs and ヾ under big5."""
    data = ("中文測試粵語" * 4).encode("big5hkscs") + b"\xc6\xa1"
    persisted = chardet.detect(data)["encoding"]
    assert persisted == "Big5"
    assert _decode("Big5", data) is not None
    assert _decode("Big5", data) != _decode("big5hkscs", data)
    assert _decode("big5hkscs", data).endswith("①")
    assert _decode("Big5", data).endswith("ヾ")


def test_shift_jis_display_name_is_lossy_but_decodes() -> None:
    """0x5C is ¥ under shift_jis_2004 and backslash under shift_jis."""
    data = ("日本語のテキストです。" * 3).encode("shift_jis_2004") + b"\x5c100"
    result = chardet.detect(data, include_encodings=["shift_jis"])
    assert result["encoding"] == "SHIFT_JIS"
    assert _decode("SHIFT_JIS", data) is not None
    assert "\\100" in _decode("SHIFT_JIS", data)
    assert "¥100" in _decode("shift_jis_2004", data)


def test_euc_jis_2004_keeps_its_internal_name_when_the_display_codec_fails() -> None:
    """0xA2AF is a JIS X 0213 row euc_jp cannot decode, so the rename is skipped."""
    data = ("日本語のテキストです。" * 2).encode("euc_jis_2004") + b"\xa2\xaf"
    result = chardet.detect(data)
    assert result["encoding"] == "euc_jis_2004"
    assert _decode("euc_jis_2004", data) is not None
    assert _decode("EUC-JP", data) is None


# ---------------------------------------------------------------------------
# Superset witnesses: one persisted name hiding two codecs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subset", "superset", "text_witness", "subset_text", "superset_text"),
    [
        ("iso8859-1", "cp1252", b"\x80", "\x80", "€"),
        ("iso8859-2", "cp1250", b"\x80", "\x80", "€"),
        ("iso8859-5", "cp1251", b"\x80", "\x80", "Ђ"),
        ("iso8859-7", "cp1253", b"\x80", "\x80", "€"),
        ("iso8859-9", "cp1254", b"\x80", "\x80", "€"),
        ("iso8859-8", "cp1255", b"\x80", "\x80", "€"),
        ("iso8859-6", "cp1256", b"\x80", "\x80", "€"),
        ("iso8859-13", "cp1257", b"\x80", "\x80", "€"),
        ("tis-620", "cp874", b"\x80", "\x80", "€"),
    ],
)
def test_superset_text_witness(
    subset: str,
    superset: str,
    text_witness: bytes,
    subset_text: str,
    superset_text: str,
) -> None:
    """A one-byte text witness separates every single-byte superset pair."""
    assert PREFERRED_SUPERSET[subset] == superset
    assert _decode(subset, text_witness) == subset_text
    assert _decode(superset, text_witness) == superset_text


def test_cp949_euc_kr_has_no_text_witness_only_an_acceptance_witness() -> None:
    """Every byte both codecs accept decodes identically; the extension does not."""
    for lead in range(0x81, 0xFF):
        for trail in range(0x100):
            raw = bytes([lead, trail])
            a = _decode("cp949", raw)
            b = _decode("euc_kr", raw)
            if a is not None and b is not None:
                assert a == b
    # 0x8141 is the shortest acceptance witness: cp949 decodes it, euc_kr raises.
    assert _decode("cp949", b"\x81\x41") == "갂"
    assert _decode("euc_kr", b"\x81\x41") is None


def test_ascii_has_no_text_witness_against_its_supersets() -> None:
    """ASCII accepts no high byte, so it can never decode to different text."""
    for byte in range(0x80, 0x100):
        assert _decode("ascii", bytes([byte])) is None
    assert _decode("cp1252", b"\x80") == "€"


# ---------------------------------------------------------------------------
# Entry points on identical bytes: the truncation signal
# ---------------------------------------------------------------------------

_UTF32_BOM_DATA = b"\xff\xfe\x00\x00" + b"A\x00\x00\x00" + b"B\x00\x00\x00" + b"C"
_UTF32_CAP = 9

_DANGLING_WINDOW = b"hello mam\xe1"
_DANGLING_DATA = _DANGLING_WINDOW + b"ZZZZ"


def _stream_until_done(
    data: bytes, cap: int, chunk: int = 1, **kwargs: object
) -> str | None:
    """Feed *data* in chunks and stop as soon as the buffer is full."""
    detector = chardet.UniversalDetector(max_bytes=cap, **kwargs)
    for index in range(0, len(data), chunk):
        detector.feed(data[index : index + chunk])
        if detector.done:
            break
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return detector.close()["encoding"]


def test_utf32_bom_split_on_identical_bytes() -> None:
    """``detect`` sees the stray byte past the cap; a full-buffer stream does not."""
    detected = chardet.detect(_UTF32_BOM_DATA, max_bytes=_UTF32_CAP)["encoding"]
    streamed = _stream_until_done(_UTF32_BOM_DATA, _UTF32_CAP)

    assert detected == "UTF-32"
    assert streamed == "UTF-16"


@pytest.mark.parametrize(
    ("chunk", "expected"),
    [
        (1, "UTF-16"),  # lands exactly on the cap: no truncation flag
        (2, "UTF-32"),  # feeds one byte past the cap: truncated
        (3, "UTF-16"),  # lands exactly on the cap again
        (4, "UTF-32"),
        (9, "UTF-16"),
    ],
)
def test_utf32_bom_split_depends_on_the_chunk_boundary(
    chunk: int, expected: str
) -> None:
    """The split follows from whether the feed crosses the cap, not the codec."""
    assert _stream_until_done(_UTF32_BOM_DATA, _UTF32_CAP, chunk=chunk) == expected


@pytest.mark.parametrize(
    ("include", "detect_result"),
    [
        (["utf-16"], None),
        (["utf-32"], "UTF-32"),
        (["utf-16", "utf-32"], "UTF-32"),
    ],
)
def test_utf32_bom_split_with_filters(
    include: list[str], detect_result: str | None
) -> None:
    """A filter can turn the divergence into UTF-32 against ``None``."""
    detected = chardet.detect(
        _UTF32_BOM_DATA, max_bytes=_UTF32_CAP, include_encodings=include
    )["encoding"]
    assert detected == detect_result


def test_dangling_tail_split_on_identical_bytes() -> None:
    """The decode-safety flip is off when the caller's bytes run past the cap."""
    kwargs = {"include_encodings": ["utf-8", "cp720"]}
    detected = chardet.detect(
        _DANGLING_DATA, max_bytes=len(_DANGLING_WINDOW), **kwargs
    )["encoding"]
    streamed = _stream_until_done(_DANGLING_DATA, len(_DANGLING_WINDOW), **kwargs)

    assert detected == "utf-8"
    assert streamed == "cp720"


def test_dangling_tail_whole_input_promotes_the_decodable_rival() -> None:
    """With the whole input, the flip sees no truncation and fires."""
    data = _DANGLING_WINDOW
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = chardet.detect(data, include_encodings=["utf-8", "cp720"])
    assert result["encoding"] == "cp720"


@pytest.mark.parametrize("era", [EncodingEra.MODERN_WEB, EncodingEra.LEGACY_REGIONAL])
def test_dangling_tail_split_needs_the_rival_in_the_selected_era(
    era: EncodingEra,
) -> None:
    """An era filter that drops the rival removes the divergence."""
    kwargs = {
        "include_encodings": ["utf-8", "cp720"],
        "encoding_era": era,
    }
    detected = chardet.detect(
        _DANGLING_DATA, max_bytes=len(_DANGLING_WINDOW), **kwargs
    )["encoding"]
    streamed = _stream_until_done(_DANGLING_DATA, len(_DANGLING_WINDOW), **kwargs)
    if era == EncodingEra.MODERN_WEB:
        # cp720 is LEGACY_REGIONAL, so only utf-8 survives either way.
        assert detected == streamed == "utf-8"
    else:
        assert detected == streamed == "cp720"


def test_all_three_entries_agree_when_no_bytes_cross_the_cap() -> None:
    """Same window, no wrapper: detect, detect_all, and streaming agree."""
    window = _DANGLING_WINDOW
    kwargs = {"include_encodings": ["utf-8", "cp720"]}
    detected = chardet.detect(window, **kwargs)["encoding"]
    top_all = chardet.detect_all(window, **kwargs)[0]["encoding"]
    streamed = _stream_until_done(window, len(window), **kwargs)
    assert detected == top_all == streamed


# ---------------------------------------------------------------------------
# The registry has no two names that resolve to the same Python codec
# ---------------------------------------------------------------------------


def test_registry_names_resolve_to_distinct_codecs() -> None:
    """Every registry name is a distinct codec; collisions live in the output layer."""
    codecs_by_name = {name: codecs.lookup(name).name for name in REGISTRY}
    assert len(set(codecs_by_name.values())) == len(codecs_by_name)


def test_compat_names_collapse_only_through_the_superset_selection() -> None:
    """Display names alone never collide; ``prefer_superset`` is what merges."""
    display_groups: dict[str, list[str]] = {}
    for name in REGISTRY:
        display_groups.setdefault(_COMPAT_NAMES.get(name, name), []).append(name)
    assert all(len(members) == 1 for members in display_groups.values())

    superset_groups: dict[str, list[str]] = {}
    for name in REGISTRY:
        target = PREFERRED_SUPERSET.get(name, name)
        final = _COMPAT_NAMES.get(target, target)
        superset_groups.setdefault(final, []).append(name)
    merged = [members for members in superset_groups.values() if len(members) > 1]
    assert merged
