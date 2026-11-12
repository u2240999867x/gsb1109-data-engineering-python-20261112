r"""
Chunked-decoding regression tests for the decoders that an archive field
relies on when it stores both the original bytes and a cleaned text layer.

The question being answered here is: after switching to ftfy's permissive
codecs, what level of round-trip can still be promised when decoding happens
in chunks, with the incremental state serialized and resumed in a *different*
decoder instance (or even a different codec)?

Three byte streams are used, each laying the requested features out in one
flow:

* ``UTF8_STREAM``  - a legal multibyte sequence, an undefined lone byte,
                     and a truncated multibyte sequence at the end.
* ``VARIANT_STREAM`` - the same shape, plus a six-byte CESU-8 sequence and a
                     Java-style modified-UTF-8 null, so utf-8 and
                     utf-8-variants can be told apart.
* ``CHARSET_STREAM`` - a single-byte (charmap) flow containing a byte that is
                     undefined in windows-1252 (0x81), framed by mapped bytes.

Findings encoded as assertions below:

* Same-codec migration (decoder A -> A -> A, state carried via
  ``getstate()`` / ``setstate()`` across two pauses) is byte-exact at every
  cut point for every codec and every error handler, including the absolute
  byte offset reported by ``strict`` errors.
* ``replace`` / ``ignore`` only ever swallow bytes that ``strict`` rejects;
  those bytes are recorded separately as a loss ledger.
* Cross-codec-family migration cannot be promised: the charmap decoders do
  not subclass ``BufferedIncrementalDecoder``, so ``setstate()`` silently
  drops buffered bytes, and utf-8 vs utf-8-variants disagree on CESU-8 and
  the Java null. Each unsupported combination gets its shortest witness.
"""

from __future__ import annotations

import codecs
from itertools import product

import pytest

import ftfy.bad_codecs  # noqa: F401  (registers the ftfy codecs)

REPLACEMENT_CHAR = "�"

# --- The three mixed feature streams --------------------------------------
# Layout of UTF8_STREAM:
#   0     : ASCII 'Q'
#   1..4  : legal 3-byte UTF-8 U+4E2D ('中')
#   4     : undefined lone continuation byte 0x81
#   5     : ASCII 'w'
#   6     : truncated 2-byte lead 0xC3 with no continuation
UTF8_STREAM = b"Q" + "中".encode() + b"\x81" + b"w" + b"\xc3"

# VARIANT_STREAM inserts, between the legal UTF-8 and the undefined byte:
#   4..10 : six-byte CESU-8 encoding of U+1F60D (😍)
#   10..12: Java modified-UTF-8 null (0xC0 0x80 -> U+0000)
CESU8_BYTES = b"\xed\xa0\xbd\xed\xb8\x8d"
JAVA_NULL_BYTES = b"\xc0\x80"
VARIANT_STREAM = b"Q" + "中".encode() + CESU8_BYTES + JAVA_NULL_BYTES + b"\x81" + b"w" + b"\xc3"

# Layout of CHARSET_STREAM (windows-1252 / sloppy-windows-1252):
#   0 : 'Q', 1 : 0x80 -> Euro sign, 2 : 0x81 undefined, 3 : 0xE9 -> 'é', 4 : 'w'
CHARSET_STREAM = b"Q\x80\x81\xe9w"

ERROR_HANDLERS = ("strict", "replace", "ignore")

# codecs paired with the mixed stream they can consume
CODEC_STREAMS = [
    ("utf-8", UTF8_STREAM),
    ("utf-8-variants", UTF8_STREAM),
    ("utf-8", VARIANT_STREAM),
    ("utf-8-variants", VARIANT_STREAM),
    ("windows-1252", CHARSET_STREAM),
    ("sloppy-windows-1252", CHARSET_STREAM),
]


# --- Outcome handling ------------------------------------------------------
# A decode outcome is either ("ok", text) or
# ("error", abs_start, abs_end, offending_bytes).


def _absolute_error(exc: UnicodeDecodeError, fed_count: int) -> tuple:
    obj = bytes(exc.object)
    base = fed_count - len(obj)
    start = base + exc.start
    end = base + exc.end
    return ("error", start, end, obj[exc.start : exc.end])


def decode_oneshot(data: bytes, codec: str, errors: str) -> tuple:
    """Decode the whole stream in one fresh incremental decoder."""
    decoder = codecs.getincrementaldecoder(codec)(errors)
    try:
        return ("ok", decoder.decode(data, final=True))
    except UnicodeDecodeError as exc:
        return _absolute_error(exc, len(data))


def decode_chain(data: bytes, plan: list[tuple[str, int]], errors: str) -> tuple:
    """
    Decode ``data`` through several decoders in sequence.

    ``plan`` is a list of ``(codec, cut)`` pairs; decoder i receives
    ``data[previous_cut:cut]`` and starts from the previous decoder's state.
    The last stage always runs with ``final=True``.
    """
    output: list[str] = []
    state = None
    lo = 0
    for stage, (codec, hi) in enumerate(plan):
        decoder = codecs.getincrementaldecoder(codec)(errors)
        if state is not None:
            decoder.setstate(state)
        try:
            output.append(decoder.decode(data[lo:hi], final=(stage == len(plan) - 1)))
        except UnicodeDecodeError as exc:
            return _absolute_error(exc, hi)
        state = decoder.getstate()
        lo = hi
    return ("ok", "".join(output))


def same_codec_plan(codec: str, cut1: int, cut2: int, length: int) -> list[tuple[str, int]]:
    return [(codec, cut1), (codec, cut2), (codec, length)]


def strict_loss_spans(data: bytes, codec: str) -> list[tuple[int, int]]:
    """
    Walk the stream with a strict decoder, resyncing after each error, and
    return the absolute (start, end) spans of every rejected byte run.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(data):
        decoder = codecs.getincrementaldecoder(codec)("strict")
        try:
            decoder.decode(data[pos:], final=True)
            break
        except UnicodeDecodeError as exc:
            base = pos + len(data[pos:]) - len(exc.object)
            start, end = base + exc.start, base + exc.end
            spans.append((start, end))
            pos = end
    return spans


# --- Stream layout ---------------------------------------------------------


def test_stream_layout_pins_feature_positions():
    assert len(UTF8_STREAM) == 7
    assert UTF8_STREAM[1:4] == "中".encode()
    assert UTF8_STREAM[4:5] == b"\x81"
    assert UTF8_STREAM[6:7] == b"\xc3"

    assert len(VARIANT_STREAM) == 15
    assert VARIANT_STREAM[4:10] == CESU8_BYTES
    assert VARIANT_STREAM[10:12] == JAVA_NULL_BYTES
    assert VARIANT_STREAM[12:13] == b"\x81"
    assert VARIANT_STREAM[14:15] == b"\xc3"

    assert CHARSET_STREAM[2:3] == b"\x81"


# --- Prefix output and residual state at every possible pause --------------


@pytest.mark.parametrize("codec, data", CODEC_STREAMS)
@pytest.mark.parametrize("errors", ERROR_HANDLERS)
def test_every_cut_matches_reference_prefix_and_state(codec, data, errors):
    for cut in range(len(data) + 1):
        # The paused decoder under test.
        paused = codecs.getincrementaldecoder(codec)(errors)
        # A reference decoder that has independently seen the same prefix.
        reference = codecs.getincrementaldecoder(codec)(errors)

        try:
            prefix_out = paused.decode(data[:cut], final=False)
        except UnicodeDecodeError as exc:
            # Only strict decoders reach here. The independent decoder must
            # die at exactly the same absolute byte.
            assert errors == "strict"
            with pytest.raises(UnicodeDecodeError) as exc_info:
                reference.decode(data[:cut], final=False)
            assert _absolute_error(exc, cut) == _absolute_error(exc_info.value, cut)
            continue
        assert prefix_out == reference.decode(data[:cut], final=False)

        # Residual incremental state must be identical and resumable.
        assert paused.getstate() == reference.getstate()
        resumed = codecs.getincrementaldecoder(codec)(errors)
        resumed.setstate(paused.getstate())
        try:
            tail = resumed.decode(data[cut:], final=True)
        except UnicodeDecodeError as exc:
            assert decode_oneshot(data, codec, errors) == _absolute_error(exc, len(data))
        else:
            assert decode_oneshot(data, codec, errors) == (
                "ok",
                prefix_out + tail,
            )


def test_pending_multibyte_buffer_is_exposed_in_state():
    # A legal 3-byte UTF-8 character split mid-sequence.
    decoder = codecs.getincrementaldecoder("utf-8")()
    decoder.decode(UTF8_STREAM[:2], final=False)
    assert decoder.getstate() == (b"\xe4", 0)
    decoder.decode(UTF8_STREAM[2:3], final=False)
    assert decoder.getstate() == (b"\xe4\xb8", 0)

    # The truncated tail remains buffered at a non-final pause. Use a stream
    # without the earlier error byte, so strict decoding reaches the tail.
    clean_tail = b"Q" + "中".encode() + b"w" + b"\xc3"
    tail_decoder = codecs.getincrementaldecoder("utf-8")()
    tail_decoder.decode(clean_tail, final=False)
    assert tail_decoder.getstate() == (b"\xc3", 0)

    # CESU-8: each partial six-byte run is held verbatim in the buffer.
    for cut, expected in zip(range(5, 10), [b"\xed", b"\xed\xa0", b"\xed\xa0\xbd"]):
        cesu_decoder = codecs.getincrementaldecoder("utf-8-variants")()
        cesu_decoder.decode(VARIANT_STREAM[:cut], final=False)
        assert cesu_decoder.getstate() == (expected, 0)

    # Java null lead byte waits for its continuation.
    null_decoder = codecs.getincrementaldecoder("utf-8-variants")()
    null_decoder.decode(VARIANT_STREAM[:11], final=False)
    assert null_decoder.getstate() == (b"\xc0", 0)

    # Charmap decoders are stateless: getstate never carries buffered bytes.
    charmap_decoder = codecs.getincrementaldecoder("sloppy-windows-1252")()
    assert not hasattr(charmap_decoder, "buffer")
    charmap_decoder.decode(CHARSET_STREAM, final=False)
    assert charmap_decoder.getstate() == (b"", 0)


# --- Two migrations, same codec: byte-exact replay at every cut pair -------


@pytest.mark.parametrize("codec, data", CODEC_STREAMS)
@pytest.mark.parametrize("errors", ERROR_HANDLERS)
def test_two_migrations_same_codec_byte_exact(codec, data, errors):
    reference = decode_oneshot(data, codec, errors)
    for cut1, cut2 in product(range(len(data) + 1), repeat=2):
        if cut2 < cut1:
            continue
        outcome = decode_chain(data, same_codec_plan(codec, cut1, cut2, len(data)), errors)
        assert outcome == reference, (cut1, cut2, outcome, reference)


def test_utf8_and_variants_agree_on_plain_utf8_streams():
    # On a stream with no variant-specific bytes, the two buffered decoders
    # are interchangeable even across codec names, at every cut pair.
    for errors in ERROR_HANDLERS:
        reference = decode_oneshot(UTF8_STREAM, "utf-8-variants", errors)
        for cut1, cut2 in product(range(len(UTF8_STREAM) + 1), repeat=2):
            if cut2 < cut1:
                continue
            plan = [
                ("utf-8", cut1),
                ("utf-8-variants", cut2),
                ("utf-8-variants", len(UTF8_STREAM)),
            ]
            assert decode_chain(UTF8_STREAM, plan, errors) == reference


# --- strict errors point at the original byte ------------------------------


def test_strict_error_locations_are_absolute_and_stable_across_cuts():
    expected = [
        ("utf-8", UTF8_STREAM, (4, 5, b"\x81")),
        ("utf-8-variants", UTF8_STREAM, (4, 5, b"\x81")),
        ("utf-8-variants", VARIANT_STREAM, (12, 13, b"\x81")),
        ("windows-1252", CHARSET_STREAM, (2, 3, b"\x81")),
    ]
    for codec, data, (start, end, byte) in expected:
        assert decode_oneshot(data, codec, "strict") == (
            "error",
            start,
            end,
            byte,
        )
        # The same absolute location is reported no matter where the stream
        # was paused and migrated before the error surfaced.
        for cut in range(start + 1):
            outcome = decode_chain(
                data,
                [(codec, cut), (codec, len(data))],
                "strict",
            )
            assert outcome == ("error", start, end, byte)

    # utf-8 rejects the CESU-8 lead itself, earlier in the stream.
    assert decode_oneshot(VARIANT_STREAM, "utf-8", "strict") == (
        "error",
        4,
        5,
        b"\xed",
    )

    # At a final pause the truncated tail is itself a strict error. Drop the
    # earlier error byte so decoding actually reaches the tail.
    truncated_tail = b"Q" + "中".encode() + b"w" + b"\xc3"
    assert decode_oneshot(truncated_tail, "utf-8", "strict") == (
        "error",
        5,
        6,
        b"\xc3",
    )


# --- Loss ledger for replace / ignore --------------------------------------


@pytest.mark.parametrize("codec, data", CODEC_STREAMS)
def test_loss_ledger_matches_strict_rejected_bytes(codec, data):
    spans = strict_loss_spans(data, codec)
    lost_bytes = b"".join(data[start:end] for start, end in spans)

    # Rebuild what replace/ignore must produce from the strict walk alone:
    # valid segments decode strictly, each rejected span contributes one
    # replacement char (replace) or nothing (ignore).
    valid_segments: list[str] = []
    previous = 0
    for start, end in spans:
        valid_segments.append(
            codecs.getincrementaldecoder(codec)("strict").decode(data[previous:start], final=True)
        )
        previous = end
    valid_segments.append(
        codecs.getincrementaldecoder(codec)("strict").decode(data[previous:], final=True)
    )

    expected_replace = valid_segments[0] + "".join(
        REPLACEMENT_CHAR + segment for segment in valid_segments[1:]
    )
    expected_ignore = "".join(valid_segments)

    assert decode_oneshot(data, codec, "replace") == ("ok", expected_replace)
    assert decode_oneshot(data, codec, "ignore") == ("ok", expected_ignore)

    # Every swallowed byte is exactly a strict-rejected byte, and vice
    # versa. A lossless codec (e.g. sloppy-* on its target stream) has
    # neither rejected spans nor a delta between the two error handlers.
    replace_outcome = decode_oneshot(data, codec, "replace")[1]
    ignore_outcome = decode_oneshot(data, codec, "ignore")[1]
    assert replace_outcome.count(REPLACEMENT_CHAR) == len(spans)
    assert len(replace_outcome) - len(ignore_outcome) == len(spans)
    if spans:
        assert lost_bytes
        assert all(byte in lost_bytes for byte in (b"\x81", b"\xc3") if byte in data)
    else:
        assert lost_bytes == b""
        assert replace_outcome == ignore_outcome


def test_concrete_loss_ledgers():
    assert (
        b"".join(UTF8_STREAM[s:e] for s, e in strict_loss_spans(UTF8_STREAM, "utf-8"))
        == b"\x81\xc3"
    )
    assert (
        b"".join(
            VARIANT_STREAM[s:e] for s, e in strict_loss_spans(VARIANT_STREAM, "utf-8-variants")
        )
        == b"\x81\xc3"
    )
    assert (
        b"".join(CHARSET_STREAM[s:e] for s, e in strict_loss_spans(CHARSET_STREAM, "windows-1252"))
        == b"\x81"
    )

    # The sloppy codec removes the hole from the ledger: 0x81 round-trips
    # into U+0081 and no loss remains.
    assert strict_loss_spans(CHARSET_STREAM, "sloppy-windows-1252") == []
    assert decode_oneshot(CHARSET_STREAM, "sloppy-windows-1252", "strict") == (
        "ok",
        "Q€\x81éw",
    )


# --- Unsupported combinations: shortest counterexamples --------------------


def test_cross_family_setstate_silently_drops_buffered_bytes():
    # A utf-8 decoder pauses with the lead byte of a 3-byte sequence buffered.
    utf8_decoder = codecs.getincrementaldecoder("utf-8")()
    utf8_decoder.decode(b"\xe4", final=False)
    state = utf8_decoder.getstate()
    assert state == (b"\xe4", 0)

    # The charmap decoder accepts the state tuple but has nowhere to keep the
    # buffer, so the lead byte vanishes and the continuations decode alone.
    charmap_decoder = codecs.getincrementaldecoder("sloppy-windows-1252")()
    charmap_decoder.setstate(state)
    migrated = charmap_decoder.decode(b"\xb8\xad", final=True)
    assert migrated == "¸\xad"

    # The same bytes decoded as one utf-8 stream give a completely different,
    # correct result - the migration cannot be replayed byte-exactly.
    assert b"\xe4\xb8\xad".decode("utf-8") == "中"
    assert migrated != "中"


def test_shortest_witness_utf8_vs_charmap_family():
    # A single byte is enough to distinguish the families: windows-1252 maps
    # 0x80 to the Euro sign, while UTF-8 has no one-byte meaning for it.
    data = b"\x80"

    # utf-8 -> sloppy, migrating before any byte is consumed: the charmap
    # decoder happily emits the Euro sign in every error handler.
    for errors in ERROR_HANDLERS:
        migrated = decode_chain(data, [("utf-8", 0), ("sloppy-windows-1252", 1)], errors)
        assert migrated == ("ok", "€")
        assert migrated != decode_oneshot(data, "utf-8", errors)

    # The reverse direction: the sloppy decoder consumes 0x80 first, so even
    # strict mode never sees an error, whereas utf-8 alone rejects the byte.
    strict_migrated = decode_chain(data, [("sloppy-windows-1252", 1), ("utf-8", 1)], "strict")
    assert strict_migrated == ("ok", "€")
    assert decode_oneshot(data, "utf-8", "strict") == (
        "error",
        0,
        1,
        b"\x80",
    )
    for errors in ("replace", "ignore"):
        migrated = decode_chain(data, [("sloppy-windows-1252", 1), ("utf-8", 1)], errors)
        assert migrated == ("ok", "€")
        assert migrated != decode_oneshot(data, "utf-8", errors)


def test_shortest_witness_charmap_hole_sloppy_vs_strict_cp1252():
    data = b"\x81"
    for errors in ERROR_HANDLERS:
        # Pause after the sloppy decoder has consumed the "undefined" byte.
        sloppy_first = decode_chain(
            data,
            [("sloppy-windows-1252", 1), ("windows-1252", 1)],
            errors,
        )
        strict_cp = decode_oneshot(data, "windows-1252", errors)
        if errors == "strict":
            assert sloppy_first == ("ok", "\x81")
            assert strict_cp == ("error", 0, 1, b"\x81")
        elif errors == "replace":
            assert sloppy_first == ("ok", "\x81")
            assert strict_cp == ("ok", REPLACEMENT_CHAR)
        else:
            assert sloppy_first == ("ok", "\x81")
            assert strict_cp == ("ok", "")

        # The reverse migration fails (strict) or changes the output as soon
        # as the strict codec is asked to decode the hole.
        cp_first = decode_chain(
            data,
            [("windows-1252", 1), ("sloppy-windows-1252", 1)],
            errors,
        )
        sloppy_reference = decode_oneshot(data, "sloppy-windows-1252", errors)
        assert cp_first != sloppy_reference


def test_shortest_witness_java_null_distinguishes_utf8_variants():
    # Two bytes is enough: 0xC0 0x80 is a Java null only in utf-8-variants.
    data = JAVA_NULL_BYTES
    for errors in ("replace", "ignore"):
        migrated = decode_chain(data, [("utf-8", 1), ("utf-8-variants", 2)], errors)
        reference = decode_oneshot(data, "utf-8-variants", errors)
        assert migrated != reference
        assert reference == ("ok", "\x00")
        assert migrated == (
            "ok",
            REPLACEMENT_CHAR + REPLACEMENT_CHAR if errors == "replace" else "",
        )

    # In strict mode the shortest divergence is the full six-byte CESU-8
    # sequence: variants accepts it, utf-8 rejects its lead byte.
    cesu = CESU8_BYTES
    assert decode_oneshot(cesu, "utf-8-variants", "strict") == ("ok", "😍")
    assert decode_oneshot(cesu, "utf-8", "strict") == (
        "error",
        0,
        1,
        b"\xed",
    )
    # Any shorter truncation is rejected identically by both codecs.
    for length in range(1, 6):
        truncated = cesu[:length]
        assert decode_oneshot(truncated, "utf-8", "strict") == decode_oneshot(
            truncated, "utf-8-variants", "strict"
        )


def test_cesu_divergence_surfaces_after_a_real_migration_cut():
    # Pause inside the CESU-8 sequence in a plain utf-8 decoder, then resume
    # in utf-8-variants: the variant semantics are lost and replacement
    # chars appear instead of the intended supplementary character.
    cut = 7  # after 'Q', '中', and the first CESU-8 surrogate triple
    outcome = decode_chain(
        VARIANT_STREAM,
        [("utf-8", cut), ("utf-8-variants", len(VARIANT_STREAM))],
        "replace",
    )
    reference = decode_oneshot(VARIANT_STREAM, "utf-8-variants", "replace")
    assert outcome != reference
    assert reference[1].startswith("Q中😍")
    assert "😍" not in outcome[1]


# --- Minimality guard: no shorter witness exists over the byte alphabet ----

SHORT_ALPHABET = (0x41, 0x80, 0x81, 0xC0, 0xC3, 0xE4, 0xB8, 0xAD, 0xED, 0xA0, 0xBD)


def _find_shortest_witness(codec1: str, codec2: str, errors: str, max_len: int = 2):
    for length in range(1, max_len + 1):
        for values in product(SHORT_ALPHABET, repeat=length):
            data = bytes(values)
            reference = decode_oneshot(data, codec2, errors)
            for cut in range(length + 1):
                if decode_chain(data, [(codec1, cut), (codec2, length)], errors) != reference:
                    return data
    return None


def test_documented_single_byte_witnesses_are_minimal():
    assert _find_shortest_witness("utf-8", "sloppy-windows-1252", "strict") == b"\x80"
    assert _find_shortest_witness("sloppy-windows-1252", "windows-1252", "replace") == b"\x81"
    assert _find_shortest_witness("windows-1252", "sloppy-windows-1252", "replace") == b"\x81"
    assert _find_shortest_witness("utf-8", "utf-8-variants", "replace") == b"\xc0\x80"
    assert _find_shortest_witness("utf-8-variants", "utf-8", "ignore") == b"\xc0\x80"
