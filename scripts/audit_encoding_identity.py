#!/usr/bin/env python3
"""Audit encoding identity across filtering, detection, and rendering.

Three layers of identity are easy to conflate when chardet's answer is used
as a directory or file name:

1. **Pre-filter identity** -- the canonical codec the caller's whitelist
   entry normalizes to.  A familiar alias can name a *different* real codec
   (``shift_jis`` -> ``shift_jis_2004``, ``gb2312`` -> ``gb18030``,
   ``iso-8859-11`` -> ``tis-620``), so the filter does not select the codec
   the name suggests.
2. **Detected bytes** -- the text the pipeline's internal codec actually
   produces from the examined window.
3. **Persisted name** -- the string the caller sees, and the codec it
   resolves to when the bytes are decoded later.  Rendering guarantees the
   name *decodes* the window; it does not guarantee it decodes to the same
   *text*.

The audit prints a table per layer and then locates byte witnesses for every
pair of codecs that rendering can collapse onto one name.  A witness is one
short byte string whose presence distinguishes the two candidates: either
the two decode it to different text (a **text witness**) or one decodes it
and the other raises (an **acceptance witness**).  Pairs with no text
witness at all are listed separately in the fallback table, with the scope
over which their decodes are provably identical.

Finally the audit compares the three public entry points on one set of
original bytes and reports the minimal filter constraints under which they
part company.

Usage::

    uv run python scripts/audit_encoding_identity.py
    uv run python scripts/audit_encoding_identity.py --section identity
"""

from __future__ import annotations

import argparse
import codecs
import itertools
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chardet
from chardet.enums import EncodingEra
from chardet.output_names import (
    _COMPAT_NAMES,
    NARROWER_DISPLAY_NAMES,
    PREFERRED_SUPERSET,
    display_codec,
)
from chardet.registry import REGISTRY, lookup_encoding

# --------------------------------------------------------------------------
# Names that can collapse onto one persisted name
# --------------------------------------------------------------------------


#: Persisted names ``prefer_superset`` can produce, and the internal names
#: that land on each. Derived, never hand-written: a pair added to the public
#: selection shows up here without an edit.
def preferred_collisions() -> dict[str, tuple[str, ...]]:
    """Map each ``prefer_superset`` output name to the internal names reaching it."""
    groups: dict[str, list[str]] = {}
    for name in REGISTRY:
        target = PREFERRED_SUPERSET.get(name, name)
        final = _COMPAT_NAMES.get(target, target)
        groups.setdefault(final, []).append(name)
    return {k: tuple(v) for k, v in sorted(groups.items()) if len(v) > 1}


#: Persisted names whose display codec is *narrower* than the internal codec,
#: with the two codec names to compare.
def narrowing_pairs() -> dict[str, tuple[str, str]]:
    """Map the narrowed display names to ``(internal, display codec)``."""
    return {
        internal: (internal, display_codec(internal))
        for internal in sorted(NARROWER_DISPLAY_NAMES)
    }


def codec_of(name: str) -> str:
    """Return the Python codec a persisted name resolves to."""
    return codecs.lookup(name).name


# --------------------------------------------------------------------------
# Decode primitives
# ------------------------------------------------------------------


def try_decode(codec: str, raw: bytes) -> str | None:
    """Return the decoded text, or ``None`` when the codec rejects the bytes."""
    try:
        return raw.decode(codec)
    except (UnicodeDecodeError, UnicodeError):
        return None


def classify_pair(
    codec_a: str,
    codec_b: str,
    *,
    wide: bool,
) -> PairEvidence:
    """Compare two codecs over their shortest byte domain.

    ``wide`` extends the scan to the two-byte domain, which is where the
    multi-byte CJK pairs can differ at all.  For single-byte pairs the
    one-byte domain is exhaustive: no byte string distinguishes codecs that
    agree on every single byte and accept exactly the same set.
    """
    text: tuple[str, str, str] | None = None
    accept: tuple[str, str, str, str] | None = None
    both = only_a = only_b = 0

    domains = [(bytes([x]) for x in range(256))]
    domain_name = "1-byte domain (exhaustive)"
    if wide:
        domains.append(bytes([x, y]) for x in range(0x80, 0x100) for y in range(256))
        # The 0x8E/0x8F three-byte forms of EUC and the extension planes of the
        # HKSCS/Big5 families only appear at length three.
        domains.append(
            bytes([lead, x, y])
            for lead in (0x8E, 0x8F)
            for x in range(0xA1, 0xFF)
            for y in range(0xA1, 0xFF)
        )
        domain_name = "1-, 2-, and 3-byte domain (EUC 0x8E/0x8F planes included)"
    for domain in domains:
        for raw in domain:
            a, b = try_decode(codec_a, raw), try_decode(codec_b, raw)
            if a is not None and b is not None:
                both += 1
                if a != b and text is None:
                    text = (raw.hex(), a, b)
            elif a is not None:
                only_a += 1
                if accept is None:
                    accept = (raw.hex(), codec_a, a, codec_b, "error")
            elif b is not None:
                only_b += 1
                if accept is None:
                    accept = (raw.hex(), codec_b, b, codec_a, "error")
    return PairEvidence(text, accept, both, only_a, only_b, domain_name)


@dataclass(frozen=True, slots=True)
class PairEvidence:
    """What one codec pair can be told apart by.

    :param text_witness: ``(hex, text_a, text_b)`` for the shortest byte
        string both codecs accept to different text, or ``None``.
    :param accept_witness: ``(hex, codec_ok, text, codec_err, "error")`` for
        the shortest string exactly one accepts, or ``None``.
    :param both: How many domain strings both accepted.
    :param only_a: How many only the first accepted.
    :param only_b: How many only the second accepted.
    :param domain: The byte domain the counts cover.
    """

    text_witness: tuple[str, str, str] | None
    accept_witness: tuple[str, str, str, str, str] | None
    both: int
    only_a: int
    only_b: int
    domain: str = ""


def classify_iso2022(codec_a: str, codec_b: str) -> PairEvidence:
    """Compare an ISO-2022 pair over escape-designation sequences.

    ISO-2022 is stateful: a designator such as ``ESC $( D`` selects a
    character set and the following bytes mean different things.  A
    one-byte scan never enters a shifted state, so it can show nothing.  This
    walks every designator family the pair might share, three payload
    bytes each, and reports the first difference.
    """
    text: tuple[str, str, str] | None = None
    accept: tuple[str, str, str, str, str] | None = None
    both = only_a = only_b = 0
    families = (
        b"\x1b$(",
        b"\x1b$",
        b"\x1b(",
        b"\x1b.",
        b"\x1b$)A",
        b"\x1b$)B",
        b"\x1b$)C",
    )
    for designator in families:
        for final in range(0x20, 0x7F):
            for hi in range(0x20, 0x7F):
                for lo in range(0x20, 0x7F):
                    raw = designator + bytes([final, hi, lo]) + b"\x1b(B"
                    a, b = try_decode(codec_a, raw), try_decode(codec_b, raw)
                    if a is not None and b is not None:
                        both += 1
                        if a != b and text is None:
                            text = (raw.hex(), a, b)
                    elif a is not None:
                        only_a += 1
                        if accept is None:
                            accept = (raw.hex(), codec_a, a, codec_b, "error")
                    elif b is not None:
                        only_b += 1
                        if accept is None:
                            accept = (raw.hex(), codec_b, b, codec_a, "error")
    return PairEvidence(
        text,
        accept,
        both,
        only_a,
        only_b,
        "1-byte domain + ESC-designation scan (designators ESC $(X, ESC $X, "
        "ESC (X, ESC .X and ESC $)A/B/C; three payload bytes each)",
    )


# --------------------------------------------------------------------------
# Section 1: three-layer identity table
# ------------------------------------------------------------------


def section_identity() -> list[str]:
    """Print canonical codec, persisted names, and the codec they resolve to."""
    lines = ["== Layer table: internal codec -> persisted name -> real codec =="]
    lines.append(
        f"{'internal':18s} {'python codec':16s} {'compat':12s} {'compat codec':14s} "
        f"{'narrow':6s} {'+superset name':14s} {'superset codec':14s}"
    )
    for name in REGISTRY:
        compat = _COMPAT_NAMES.get(name, name)
        compat_codec = codec_of(compat)
        target = PREFERRED_SUPERSET.get(name, name)
        sup_name = _COMPAT_NAMES.get(target, target)
        sup_codec = codec_of(sup_name)
        narrow = "yes" if name in NARROWER_DISPLAY_NAMES else ""
        lines.append(
            f"{name:18s} {codecs.lookup(name).name:16s} {compat:12s} "
            f"{compat_codec:14s} {narrow:6s} {sup_name:14s} {sup_codec:14s}"
        )
    lines.append("")
    lines.append("Display names that resolve to a narrower codec:")
    for internal, (_, display_codec_name) in narrowing_pairs().items():
        lines.append(
            f"  {internal:18s} -> {_COMPAT_NAMES[internal]:12s} "
            f"-> codec {display_codec_name} (narrower than "
            f"{codecs.lookup(internal).name})"
        )
    return lines


# --------------------------------------------------------------------------
# Section 2: whitelist aliases normalize to a different real codec
# ------------------------------------------------------------------

ALIAS_PROBES = (
    "shift_jis",
    "shift-jis",
    "sjis",
    "cp932",
    "windows-31j",
    "euc-jp",
    "euc_jis_2004",
    "big5",
    "big5hkscs",
    "cp950",
    "gb2312",
    "gbk",
    "gb18030",
    "cp936",
    "iso-2022-jp",
    "iso-8859-11",
    "tis-620",
    "cp874",
    "latin-1",
    "iso-8859-1",
    "windows-1252",
    "ascii",
    "us-ascii",
    "euc-kr",
    "cp949",
    "uhc",
)


def section_alias() -> list[str]:
    """Print where a familiar whitelist spelling actually lands."""
    lines = ["== Whitelist alias -> canonical codec =="]
    lines.append(
        f"{'whitelist name':16s} {'canonical':16s} {'python codec':16s} "
        f"{'persisted':12s} {'persisted codec':16s}"
    )
    for alias in ALIAS_PROBES:
        canonical = lookup_encoding(alias)
        if canonical is None:
            lines.append(f"{alias:16s} <unknown>")
            continue
        persisted = _COMPAT_NAMES.get(canonical, canonical)
        lines.append(
            f"{alias:16s} {canonical:16s} {codecs.lookup(canonical).name:16s} "
            f"{persisted:12s} {codec_of(persisted):16s}"
        )

    lines.append("")
    lines.append("Whitelists that collapse several familiar names onto one codec:")
    groups: dict[str, set[str]] = {}
    for alias in ALIAS_PROBES:
        canonical = lookup_encoding(alias)
        if canonical is not None:
            groups.setdefault(canonical, set()).add(alias)
    for canonical, names in sorted(groups.items()):
        if len(names) > 1:
            lines.append(f"  {canonical:16s} <- {', '.join(sorted(names))}")

    lines.append("")
    lines.append("Detection through a whitelist name that selects another codec:")
    euc = ("日本語のテキストです。" * 2).encode("euc_jis_2004") + b"\xa2\xaf"
    sjis = ("日本語のテキストです。" * 3).encode("shift_jis_2004") + b"\x5c100\x7e"
    zh = "這是一個用於測試編碼偵測的中文句子。".encode("big5")
    rows = [
        (
            "euc_jis_2004 bytes, include ['euc-jp']",
            euc,
            {"include_encodings": ["euc-jp"]},
        ),
        (
            "euc_jis_2004 bytes, include ['euc_jis_2004']",
            euc,
            {"include_encodings": ["euc_jis_2004"]},
        ),
        (
            "euc_jis_2004 bytes, include ['cp932']",
            euc,
            {"include_encodings": ["cp932"]},
        ),
        (
            "shift_jis_2004 bytes, include ['shift_jis']",
            sjis,
            {"include_encodings": ["shift_jis"]},
        ),
        ("big5 bytes, include ['big5']", zh, {"include_encodings": ["big5"]}),
        ("big5 bytes, include ['big5hkscs']", zh, {"include_encodings": ["big5hkscs"]}),
    ]
    for label, data, kwargs in rows:
        with warnings.catch_warnings():
            # A whitelist that drops the no-match sentinel legitimately
            # answers encoding=None; the warning belongs in a caller's log,
            # not in this audit's table.
            warnings.simplefilter("ignore", UserWarning)
            result = chardet.detect(data, **kwargs)
        lines.append(f"  {label:48s} -> {result['encoding']}")
    return lines


# --------------------------------------------------------------------------
# Section 3: collision witnesses
# ------------------------------------------------------------------


def section_collisions() -> list[str]:
    """Print byte witnesses for every pair one persisted name can hide."""
    lines = ["== Superset-remap collisions: byte witnesses =="]
    lines.append(
        "A text witness is the shortest string both codecs accept to *different* "
        "text; an acceptance witness is the shortest exactly one accepts."
    )
    fallback: list[str] = []
    for final, members in preferred_collisions().items():
        lines.append(f"\n-- persisted name {final!r} (members: {', '.join(members)})")
        for a, b in itertools.combinations(members, 2):
            wide = any(REGISTRY[name].is_multibyte_cjk for name in (a, b))
            ev = classify_pair(a, b, wide=wide)
            scope = ev.domain
            lines.append(
                f"   {a:14s} vs {b:14s} [{scope}] "
                f"both={ev.both} onlyA={ev.only_a} onlyB={ev.only_b}"
            )
            lines.append(f"     text witness:   {ev.text_witness}")
            lines.append(f"     accept witness: {ev.accept_witness}")
            if ev.text_witness is None:
                fallback.append(
                    f"   {final:14s} {a:14s} vs {b:14s}: no text witness "
                    f"({scope}; both={ev.both}, onlyA={ev.only_a}, onlyB={ev.only_b}); "
                    f"acceptance witness {ev.accept_witness}"
                )
    lines.append("\n== Display-name narrowing: byte witnesses ==")
    lines.append(
        "Rendering may report a display name that resolves to a *narrower* "
        "codec; these are their witnesses."
    )
    for final, (internal, strict) in narrowing_pairs().items():
        if internal == "iso2022_jp_2":
            ev = classify_iso2022(internal, strict)
        else:
            ev = classify_pair(internal, strict, wide=True)
        lines.append(
            f"   {final:12s} {internal:16s} -> {strict:14s} "
            f"both={ev.both} onlyInternal={ev.only_a} onlyDisplay={ev.only_b}"
        )
        lines.append(f"     domain:         {ev.domain}")
        lines.append(f"     text witness:   {ev.text_witness}")
        lines.append(f"     accept witness: {ev.accept_witness}")
        if ev.text_witness is None:
            fallback.append(
                f"   {final:12s} {internal:16s} vs {strict:14s}: no text witness "
                f"({ev.domain}; onlyInternal={ev.only_a}); "
                f"acceptance witness {ev.accept_witness}"
            )

    lines.append("\n== Fallback table: pairs with no text witness ==")
    lines.append(
        "Where no string decodes to different text under both codecs, the "
        "persisted name cannot be distinguished by decoded text alone.  The "
        "scope column states how far that equality reaches; persistence must "
        "rely on the acceptance witness (a byte one codec rejects) instead."
    )
    lines.extend(fallback)
    return lines


# --------------------------------------------------------------------------
# Section 4: entry points on identical bytes
# ------------------------------------------------------------------

OptionValue = str | EncodingEra | list[str] | bool


def stream_until_done(
    data: bytes,
    cap: int,
    chunk: int,
    **kwargs: OptionValue,
) -> tuple[str | None, int, bool]:
    """Feed *data* in *chunk*-byte pieces, stopping when ``done`` fires.

    Returns ``(encoding, bytes_fed, input_truncated)``.  This is the loop the
    docs show callers: the buffer is filled to ``cap`` and the caller stops.

    ``_input_truncated`` is read deliberately: this audit is exactly about how
    the cap and the caller's stop point set that flag, and no public accessor
    exposes it.
    """
    detector = chardet.UniversalDetector(max_bytes=cap, **kwargs)
    fed = 0
    for i in range(0, len(data), chunk):
        piece = data[i : i + chunk]
        detector.feed(piece)
        fed += len(piece)
        if detector.done:
            break
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        encoding = detector.close()["encoding"]
        # Private on purpose: the audit is about this flag, and no public
        # accessor exposes it.
        return encoding, fed, detector._input_truncated  # noqa: SLF001


def entry_row(
    data: bytes,
    cap: int,
    **kwargs: OptionValue,
) -> tuple[str | None, ...]:
    """One row per entry point, all told the same bytes and the same cap."""
    full = dict(kwargs, max_bytes=cap)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        detect = chardet.detect(data, **full)["encoding"]
        all_default = chardet.detect_all(data, **full)[0]["encoding"]
        all_ignore = chardet.detect_all(data, ignore_threshold=True, **full)[0][
            "encoding"
        ]
    streamed, fed, truncated = stream_until_done(data, cap, 1, **kwargs)
    return detect, all_default, all_ignore, streamed, fed, truncated


def section_entries() -> list[str]:
    """Compare the three entry points on one set of original bytes."""
    lines = ["== Entry points on identical bytes =="]
    lines.append(
        "All three run the same DetectionRequest.  They part company only in "
        "how much of the caller's input they have seen past the cap, which "
        "sets `input_truncated` and feeds the BOM alignment check and the "
        "decode-safety flip."
    )

    # Family A: a UTF-32 BOM whose window ends mid-code-unit.
    base_a = b"\xff\xfe\x00\x00" + b"A\x00\x00\x00" + b"B\x00\x00\x00" + b"C"
    cap_a = 9
    lines.append(
        f"\n-- Family A: UTF-32-LE BOM, cap={cap_a}, window payload "
        f"{cap_a - 4} bytes (not a multiple of 4); original is {len(base_a)} bytes"
    )
    lines.append(f"   original bytes = {base_a.hex()}")
    lines.append(f"   examined window = {base_a[:cap_a].hex()}")
    combos_a = [
        ("defaults", {}),
        ("include ['utf-16']", {"include_encodings": ["utf-16"]}),
        ("include ['utf-32']", {"include_encodings": ["utf-32"]}),
        ("include ['utf-16','utf-32']", {"include_encodings": ["utf-16", "utf-32"]}),
        ("exclude ['utf-16']", {"exclude_encodings": ["utf-16"]}),
        ("exclude ['utf-32']", {"exclude_encodings": ["utf-32"]}),
        ("era MODERN_WEB", {"encoding_era": EncodingEra.MODERN_WEB}),
    ]
    lines.append(
        f"   {'constraints':34s} {'detect':9s} {'all(all)':9s} {'all(ignore)':11s} "
        f"{'stream@done':11s} {'fed':4s} {'truncated':10s} split"
    )
    for label, kwargs in combos_a:
        d, ad, ai, s, fed, trunc = entry_row(base_a, cap_a, **kwargs)
        split = "yes" if d != s else ""
        lines.append(
            f"   {label:34s} {d!s:9s} {ad!s:9s} {ai!s:11s} "
            f"{s!s:11s} {fed:4d} {trunc!s:10s} {split}"
        )

    # Family B: dangling tail + a decodable rival, flip controlled by truncation.
    window_b = b"hello mam\xe1"
    data_b = window_b + b"ZZZZ"
    cap_b = len(window_b)
    lines.append(
        f"\n-- Family B: window {window_b!r} (cap={cap_b}) is ASCII text plus a "
        f"dangling 0xE1; original is {len(data_b)} bytes"
    )
    combos_b = [
        ("defaults", {}),
        ("include utf-8+mac-iceland", {"include_encodings": ["utf-8", "mac-iceland"]}),
        ("include utf-8+cp720", {"include_encodings": ["utf-8", "cp720"]}),
        ("include utf-8+cp1125", {"include_encodings": ["utf-8", "cp1125"]}),
        ("include utf-8+koi8-t", {"include_encodings": ["utf-8", "koi8-t"]}),
        ("include utf-8+cp1252", {"include_encodings": ["utf-8", "cp1252"]}),
        ("include utf-8+iso8859-1", {"include_encodings": ["utf-8", "iso8859-1"]}),
        (
            "inc utf-8+mac-iceland, era MODERN_WEB",
            {
                "include_encodings": ["utf-8", "mac-iceland"],
                "encoding_era": EncodingEra.MODERN_WEB,
            },
        ),
        (
            "inc utf-8+cp720, era LEGACY_REGIONAL",
            {
                "include_encodings": ["utf-8", "cp720"],
                "encoding_era": EncodingEra.LEGACY_REGIONAL,
            },
        ),
        ("include utf-8 only", {"include_encodings": ["utf-8"]}),
    ]
    lines.append(
        f"   {'constraints':38s} {'detect':12s} {'all(all)':11s} {'stream@done':12s} "
        f"{'fed':4s} {'truncated':10s} split"
    )
    for label, kwargs in combos_b:
        d, ad, _ai, s, fed, trunc = entry_row(data_b, cap_b, **kwargs)
        split = "yes" if d != s else ""
        lines.append(
            f"   {label:38s} {d!s:12s} {ad!s:11s} {s!s:12s} "
            f"{fed:4d} {trunc!s:10s} {split}"
        )

    lines.append(
        "\n   Minimal constraint set for both families: (1) the caller's bytes "
        "run past the cap by at least one byte, so detect()/detect_all() set "
        "input_truncated=True, and (2) the streaming caller stops on the exact "
        "chunk boundary where the buffer reaches the cap, so its flag stays "
        "False.  Family A needs no filter at all; Family B needs utf-8 to head "
        "the window and a lower-ranked rival that decodes the whole window."
    )
    lines.append(
        "   Family A's second constraint is a BOM whose window payload is not "
        "a whole code unit (payload % 4 in {1,2,3}); dropping the truncation "
        "flag makes the BOM stage reject the mark and fall back to UTF-16."
    )
    lines.append(
        "   Family B's second constraint is utf-8 leading a dangling-tail "
        "window with a decodable rival; dropping the truncation flag lets the "
        "decode-safety flip promote that rival."
    )
    lines.append(
        "   A filter that removes the codec on one side (include only utf-16, "
        "include only utf-32, era=MODERN_WEB for a LEGACY_REGIONAL rival) "
        "turns the split into that side reporting encoding=None, which is "
        "still explainable and still asymmetric."
    )
    return lines


# --------------------------------------------------------------------------
# Section 5: persisted name decodes, but may decode different text
# ------------------------------------------------------------------


def section_persisted_text() -> list[str]:
    """Show where the persisted name decodes the window to different text."""
    lines = ["== Persisted name vs the text detection validated =="]
    lines.append(
        "Rendering checks that the reported name *decodes* the window; it "
        "does not check that it decodes to the same *text* the internal codec "
        "produced.  These samples make the difference visible."
    )
    big5 = ("中文測試粵語" * 4).encode("big5hkscs") + b"\xc6\xa1"
    sjis = ("日本語のテキストです。" * 3).encode("shift_jis_2004") + b"\x5c100\x7e"
    euc = ("日本語のテキストです。" * 2).encode("euc_jis_2004") + b"\xa2\xaf"

    samples = [
        ("Big5 narrowing, 0xC6A1", big5, "big5hkscs", {}),
        (
            "SHIFT_JIS narrowing, 0x5C/0x7E",
            sjis,
            "shift_jis_2004",
            {"include_encodings": ["shift_jis"]},
        ),
        ("EUC-JP narrowing, 0xA2AF", euc, "euc_jis_2004", {}),
    ]
    for label, data, internal, kwargs in samples:
        result = chardet.detect(data, **kwargs)
        name = result["encoding"]
        lines.append(f"\n   {label}: persisted {name!r} (internal {internal})")
        if name is None:
            continue
        persisted = try_decode(name, data)
        lines.append(f"     decode under persisted name : {persisted!r}")
        internal_text = try_decode(internal, data)
        lines.append(f"     decode under {internal:12s}: {internal_text!r}")
        if internal_text != persisted:
            lines.append("     -> same bytes, different text: the stored name is lossy")
        elif name == internal:
            lines.append(
                "     -> same codec: the narrower display codec refused the "
                "window, so rendering kept the internal name"
            )
    return lines


# --------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

SECTIONS = {
    "identity": section_identity,
    "alias": section_alias,
    "collisions": section_collisions,
    "entries": section_entries,
    "persisted": section_persisted_text,
}


def main(argv: list[str] | None = None) -> None:
    """Run the audit and print the requested sections."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--section",
        choices=[*SECTIONS, "all"],
        default="all",
        help="Which audit section to run (default: all)",
    )
    args = parser.parse_args(argv)

    names = list(SECTIONS) if args.section == "all" else [args.section]
    for index, name in enumerate(names):
        if index:
            print()
        for line in SECTIONS[name]():
            print(line)


if __name__ == "__main__":
    main()
