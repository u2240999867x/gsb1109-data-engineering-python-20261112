# tests/test_sampling_state.py
"""State regression for file sampling at the head of a byte stream.

Detection only ever examines a prefix of what the caller owns.  That prefix
can be the whole file (the input really ends at the cut) or an active
truncation (more bytes exist that the cap could not buffer), and the two
situations are not semantically equivalent:

* an incomplete multi-byte sequence at the end of a *whole* file is corrupt
  for the caller's next ``decode()`` and must be answered against strictly;
* the same dangling bytes in a *cut* view are an expected mid-character stop,
  so the tail is tolerated rather than punished.

``chardet.detect()`` and ``UniversalDetector.feed()/close()`` learn about
that distinction through different channels.  ``detect()`` infers a cut from
``len(data) > max_bytes`` and has no information at all once the caller has
already sliced; the streaming detector remembers the overflow itself in its
``_input_truncated`` flag, because its buffer sitting at exactly ``max_bytes``
is otherwise indistinguishable from a file of that length.

These tests enumerate every cut of a few short streams, treat "is there
really more input" as an independent condition, and cross-check the two
entries under each contract.  BOM prefixes, dangling multi-byte tails, and
``reset()`` reuse are exercised on the same detector so that previous-file
residue, boundary-byte loss, and a wrongly announced end-of-input would each
show up as an observable change in candidate, confidence, or returned copy.
"""

from __future__ import annotations

import codecs

import pytest

import chardet
from chardet import UniversalDetector
from chardet.pipeline import _NONE_RESULT

# A single-byte (Latin) word whose final high byte is a dangling UTF-8 lead:
# the whole-file reading flips to a decodable Latin codec, the cut reading
# keeps the tolerant multi-byte answer.
_LATIN = "mamá mamá mamá".encode("iso-8859-1")

# A BOM-prefixed four-byte-per-unit stream.  A cut that is not a multiple of
# four lands mid code unit, which only the truncation condition explains.
_UTF32_STREAM = codecs.BOM_UTF32_LE + "Hé".encode("utf-32-le") * 3

# A two-byte CJK stream whose final cut can leave a lone lead byte; unlike the
# Latin case both contracts converge here because real pairs decoded first.
_SJIS_STREAM = "こんにちは".encode("shift_jis")


def _verdicts(stream: bytes, cap: int) -> dict[str, object]:
    """Run one cut through both entries and both truncation conditions.

    ``whole`` treats ``stream[:cap]`` as a file that genuinely ends there;
    ``cut`` treats the same window as a capped view of the longer *stream*.
    Each condition is observed through ``detect()`` and ``feed()/close()``.
    """
    window = stream[:cap]

    detect_whole = chardet.detect(window, max_bytes=cap)
    detect_cut = chardet.detect(stream, max_bytes=cap)

    exact = UniversalDetector(max_bytes=cap)
    exact.feed(window)  # fills the buffer exactly, nothing is dropped
    ud_whole = exact.close()

    overflow = UniversalDetector(max_bytes=cap)
    overflow.feed(stream)  # bytes past the cap are discarded
    ud_cut = overflow.close()

    return {
        "window": window,
        "detect_whole": detect_whole,
        "detect_cut": detect_cut,
        "ud_whole": ud_whole,
        "ud_cut": ud_cut,
        "exact_flag": exact._input_truncated,
        "overflow_flag": overflow._input_truncated,
        "overflow_buffer_len": len(overflow._buffer),
    }


# ---------------------------------------------------------------------------
# Central enumeration: every cut, both conditions, both entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", range(1, len(_LATIN) + 1))
def test_latin_cuts_agree_across_entries_per_contract(cap: int) -> None:
    v = _verdicts(_LATIN, cap)
    # The end-of-input contract: feed/close with nothing dropped must match
    # detect() on bytes that really are the whole input.
    assert v["ud_whole"] == v["detect_whole"]
    assert v["exact_flag"] is False
    # The truncation contract: an overflowing stream must match detect()'s
    # own len(data) > max_bytes inference, never the whole-file reading.  At
    # the final cut the whole stream fits, so there is no remainder and the
    # two conditions are genuinely the same.
    assert v["ud_cut"] == v["detect_cut"]
    assert v["overflow_buffer_len"] == cap
    if cap < len(_LATIN):
        assert v["overflow_flag"] is True
    else:
        # The whole stream fits the final cut: no remainder, so the two
        # conditions collapse onto one end-of-input reading.
        assert v["overflow_flag"] is False
        assert v["ud_cut"] == v["ud_whole"]


@pytest.mark.parametrize("cap", range(4, 12))
def test_utf32_cuts_agree_across_entries_per_contract(cap: int) -> None:
    v = _verdicts(_UTF32_STREAM, cap)
    assert v["ud_whole"] == v["detect_whole"]
    assert v["exact_flag"] is False
    assert v["ud_cut"] == v["detect_cut"]
    assert v["overflow_flag"] is True
    assert v["overflow_buffer_len"] == cap


@pytest.mark.parametrize("cap", range(2, len(_SJIS_STREAM) + 1))
def test_sjis_cuts_agree_across_entries_per_contract(cap: int) -> None:
    v = _verdicts(_SJIS_STREAM, cap)
    assert v["ud_whole"] == v["detect_whole"]
    assert v["ud_cut"] == v["detect_cut"]


# ---------------------------------------------------------------------------
# BOM prefix + multi-byte tail: the condition alone flips the verdict
# ---------------------------------------------------------------------------


def test_bom_verdict_changes_only_when_more_input_exists() -> None:
    """A non-aligned UTF-32 cut is UTF-16 at end-of-file, UTF-32 when continued.

    The buffered bytes are identical in the two columns; only the answer to
    "is there really more input?" differs.  An aligned window is UTF-32 under
    either condition because no code unit is split.
    """
    for cap in range(4, 12):
        window = _UTF32_STREAM[:cap]
        whole = chardet.detect(window, max_bytes=cap)
        cut = chardet.detect(_UTF32_STREAM, max_bytes=cap)
        assert whole["confidence"] == cut["confidence"] == 1.0
        if cap % 4 == 0:
            assert whole["encoding"] == cut["encoding"] == "UTF-32"
        else:
            assert whole["encoding"] == "UTF-16"
            assert cut["encoding"] == "UTF-32"
            # The cut-view name must still decode the bytes the caller holds.
            _UTF32_STREAM.decode(cut["encoding"])


def test_streaming_remembers_the_continuation_across_a_feed_boundary() -> None:
    """The remainder arriving in a later feed is still a cut, not an ending."""
    cap = 6
    detector = UniversalDetector(max_bytes=cap)
    detector.feed(_UTF32_STREAM[:cap])  # fills exactly
    detector.feed(_UTF32_STREAM[cap:])  # continuation in a separate feed
    assert detector._input_truncated is True
    assert detector.close()["encoding"] == "UTF-32"


# ---------------------------------------------------------------------------
# The public entry that cannot tell the two situations apart
# ---------------------------------------------------------------------------


def test_detect_has_no_channel_for_more_input_after_a_caller_slice() -> None:
    """A sliced-off prefix and a genuinely short file are one input to detect().

    Both situations hand ``detect()`` the same four bytes with
    ``max_bytes == len(data)``; there is no out-of-band channel saying more
    bytes exist, so it can only return the strict, whole-file reading.  The
    streaming entry is the one that keeps both readings.  This pins the
    actual limitation rather than pretending the distinction is recoverable.
    """
    cap = 4
    window = _LATIN[:cap]

    # Situation 1: a file that is exactly `cap` bytes.  Situation 2: the same
    # bytes as a prefix the caller sliced off a longer file.  detect() sees
    # one indistinguishable argument and gives one answer.
    sliced_prefix = chardet.detect(window, max_bytes=cap)
    whole_file = chardet.detect(window, max_bytes=cap)
    assert sliced_prefix == whole_file

    # That single answer is the end-of-input verdict (the strict flip).
    ended = UniversalDetector(max_bytes=cap)
    ended.feed(window)
    assert ended.close() == whole_file

    # The continued reading is only reachable by a detector that saw the
    # overflow, proving the information detect() lacks is what separates them.
    continued = UniversalDetector(max_bytes=cap)
    continued.feed(_LATIN)
    cut_verdict = continued.close()
    assert continued._input_truncated is True
    assert cut_verdict != whole_file
    assert cut_verdict == chardet.detect(_LATIN, max_bytes=cap)


# ---------------------------------------------------------------------------
# Multi-byte tail: where the two contracts deliberately converge
# ---------------------------------------------------------------------------


def test_cjk_dangling_lead_is_not_punished_under_either_contract() -> None:
    """A lone trailing lead byte keeps the CJK family whether cut or ended.

    The decode-safety flip only fires on a winner whose sole non-ASCII
    evidence is the dangling tail.  Here complete pairs decoded before it, so
    the end-of-input and cut readings must agree rather than flip to Latin.
    """
    cut = len(_SJIS_STREAM) - 1  # drop the final trail byte
    window = _SJIS_STREAM[:cut]
    assert window[-1] >= 0x80  # the window really ends on a dangling lead byte
    whole = chardet.detect(window)
    view = chardet.detect(_SJIS_STREAM, max_bytes=cut)
    for result in (whole, view):
        assert (result["encoding"] or "").lower().startswith(("shift_jis", "cp932"))
    assert whole["encoding"] == view["encoding"]


# ---------------------------------------------------------------------------
# Termination announcement: an empty remainder must not claim a cut
# ---------------------------------------------------------------------------


def test_termination_announcement_requires_a_nonempty_remainder() -> None:
    """Feeding nothing after a full buffer ends the file; feeding bytes cuts it."""
    cap = 5
    quiet = UniversalDetector(max_bytes=cap)
    quiet.feed(b"hello")
    quiet.feed(b"")  # no further input: this is the end, not a truncation
    assert quiet._input_truncated is False
    assert quiet.done is True

    loud = UniversalDetector(max_bytes=cap)
    loud.feed(b"hello")
    loud.feed(b"x")  # a real, discarded remainder
    assert loud._input_truncated is True


def test_feed_after_close_is_blocked_until_reset() -> None:
    detector = UniversalDetector(max_bytes=4)
    detector.feed(_UTF32_STREAM)
    detector.close()
    with pytest.raises(ValueError, match="reset"):
        detector.feed(b"more")
    detector.reset()
    detector.feed(b"abc")  # usable again after reset, no exception
    assert detector.close() == chardet.detect(b"abc")


# ---------------------------------------------------------------------------
# Reuse: BOM residue and truncation memory must not leak into the next file
# ---------------------------------------------------------------------------


def test_reset_clears_bom_and_truncation_residue_between_files() -> None:
    detector = UniversalDetector(max_bytes=6)

    # File 1: BOM-prefixed, overflows the cap so truncation memory is set.
    detector.feed(_UTF32_STREAM)
    first = detector.close()
    assert first["encoding"] == "UTF-32"
    assert detector._input_truncated is True
    assert detector.done is True

    detector.reset()
    assert bytes(detector._buffer) == b""
    assert detector._input_truncated is False
    assert detector.done is False
    assert detector._closed is False
    assert detector.result == _NONE_RESULT.to_dict()

    # File 2: a short, complete, BOM-less input with no bytes dropped.  Any
    # carried-over buffer or flag would push this toward the UTF-32 answer.
    detector.feed(b"abc")
    second = detector.close()
    assert detector._input_truncated is False
    assert second == chardet.detect(b"abc")
    assert second["encoding"] == "ascii"


# ---------------------------------------------------------------------------
# Observable copies: callers cannot mutate the detector's held result
# ---------------------------------------------------------------------------


def test_close_and_result_hand_back_independent_copies() -> None:
    detector = UniversalDetector(max_bytes=8)
    detector.feed(_UTF32_STREAM)
    first = detector.close()
    first["encoding"] = "TAMPERED"
    first["confidence"] = 9.9

    reread = detector.result
    assert reread["encoding"] == "UTF-32"
    assert reread["confidence"] == 1.0
    # Two reads do not share a mutable container either.
    assert reread is not detector.result
    assert detector.close()["encoding"] == "UTF-32"


def test_candidate_ranking_is_observable_under_both_conditions() -> None:
    """detect_all exposes the candidates the top-result flip reorders.

    The examined window is identical; only the truncation condition changes
    which survivor ranks first.  Both rankings still surface the same two
    contenders (the tolerant multi-byte answer and a strictly decodable Latin
    one), so the divergence is visible in ordering rather than in hidden state.
    """
    cap = 4
    window = _LATIN[:cap]
    whole_names = {r["encoding"] for r in chardet.detect_all(window, max_bytes=cap)}
    cut_names = {r["encoding"] for r in chardet.detect_all(_LATIN, max_bytes=cap)}

    assert "utf-8" in whole_names
    assert "utf-8" in cut_names
    assert any(name and name.lower().startswith("windows-125") for name in whole_names)
    assert any(name and name.lower().startswith("windows-125") for name in cut_names)
    # The single best answer is where the two contracts visibly part ways.
    assert chardet.detect(window, max_bytes=cap) != chardet.detect(
        _LATIN, max_bytes=cap
    )
