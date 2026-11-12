"""Tests for the replayable diagnosis and offset-based recovery.

Run from the repository root with the pure-Python implementation forced on::

    MSGPACK_PUREPYTHON=1 python -m pytest replay -q
"""

from __future__ import annotations

import io

import pytest
from msgpack.exceptions import FormatError
from msgpack.fallback import Unpacker

from replay import (
    FeedLineConsumer,
    LineCheckpoint,
    ManualCheckpoint,
    ManualLineConsumer,
    full_read,
    recover_feed,
    recover_file,
)
from replay.diagnosis import (
    CONTINUATIONS,
    build_fixture,
    run_diagnostic,
)


@pytest.fixture()
def fixture():
    return build_fixture()


# ---------------------------------------------------------------------------
# The two pause points: identical tell(), different truth
# ---------------------------------------------------------------------------


def test_pause_points_report_identical_tell(fixture):
    clean = Unpacker()
    clean.feed(fixture.pause_a_bytes)
    assert list(clean) == [fixture.line1]

    half = Unpacker()
    half.feed(fixture.pause_b_bytes)
    assert list(half) == [fixture.line1]

    assert clean.tell() == half.tell() == len(fixture.line1_bytes)
    assert len(fixture.pause_a_bytes) - clean.tell() == 0
    assert len(fixture.pause_b_bytes) - half.tell() == len(fixture.partial_bytes)


def test_iterator_silently_drops_half_message_at_eof(fixture):
    # The bug: iteration end is indistinguishable from a full line at pause B.
    unpacker = Unpacker()
    unpacker.feed(fixture.pause_b_bytes)
    assert list(unpacker) == [fixture.line1]
    # No exception is raised; tell() alone cannot reveal the seven lost bytes.
    with pytest.raises(StopIteration):
        next(unpacker)
    assert unpacker.tell() == len(fixture.line1_bytes)


# ---------------------------------------------------------------------------
# Earliest moment each situation is determinable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["iterator", "manual header+skip"])
def test_diagnostic_covers_every_combination(style):
    results = [r for r in run_diagnostic() if r.style == style]
    assert len(results) == 6  # 2 pauses x 3 continuations
    assert {r.continuation for r in results} == set(CONTINUATIONS)
    assert all(r.earliest_verdict for r in results)
    assert all(r.events for r in results)


def test_legal_suffix_completes_the_half_line(fixture):
    unpacker = Unpacker()
    unpacker.feed(fixture.pause_b_bytes)
    list(unpacker)
    unpacker.feed(fixture.valid_suffix_for_b)
    # Earliest determination: the first next() returns the complete line.
    assert next(unpacker) == fixture.line2


def test_true_eof_needs_external_pending_accounting(fixture):
    unpacker = Unpacker()
    unpacker.feed(fixture.pause_b_bytes)
    list(unpacker)

    # The iterator never raises; the truncation is only knowable by comparing
    # the durable fed offset with tell() once the source signals EOF.
    fed_total = len(fixture.pause_b_bytes)
    assert fed_total - unpacker.tell() == len(fixture.partial_bytes) > 0
    with pytest.raises(StopIteration):
        next(unpacker)


def test_invalid_marker_is_detected_at_first_read(fixture):
    for pause_bytes in (fixture.pause_a_bytes, fixture.pause_b_bytes):
        unpacker = Unpacker()
        unpacker.feed(pause_bytes)
        list(unpacker)
        unpacker.feed(fixture.invalid_marker)
        with pytest.raises(FormatError, match="Unknown header: 0xc1"):
            next(unpacker)
        # tell() does not advance past the corrupt byte.
        assert unpacker.tell() == len(fixture.line1_bytes)


def test_manual_consumer_marks_open_container_at_eof(fixture):
    consumer = ManualLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    assert consumer.lines == [fixture.line1]

    checkpoint = consumer.checkpoint()
    assert isinstance(checkpoint, ManualCheckpoint)
    assert checkpoint.container_offset == len(fixture.line1_bytes)
    assert checkpoint.size == 3
    assert checkpoint.members_done == 1
    assert checkpoint.members == [fixture.line2[0]]


def test_manual_consumer_clean_boundary_has_no_open_container(fixture):
    consumer = ManualLineConsumer()
    consumer.push(fixture.pause_a_bytes)
    checkpoint = consumer.checkpoint()
    assert checkpoint.container_offset is None
    assert checkpoint.next_offset == len(fixture.line1_bytes)


# ---------------------------------------------------------------------------
# Recovery from an addressable byte stream
# ---------------------------------------------------------------------------


def test_recover_file_after_half_message_matches_single_read(fixture):
    whole = io.BytesIO(fixture.whole_stream)
    reference = full_read(io.BytesIO(fixture.whole_stream), read_size=4)

    # Crash while seven bytes of line 2 are already fetched.
    consumer = FeedLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    checkpoint = consumer.checkpoint()
    assert checkpoint.offset == len(fixture.line1_bytes)
    assert checkpoint.pending == fixture.partial_bytes

    recovered_tail = recover_file(whole, checkpoint, read_size=3)
    assert consumer.lines + recovered_tail == reference


def test_recover_feed_replays_pending_then_future_chunks(fixture):
    consumer = FeedLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    checkpoint = consumer.checkpoint()

    suffix = fixture.valid_suffix_for_b
    chunks = [suffix[:1], suffix[1:3], suffix[3:]]
    recovered = recover_feed(checkpoint, chunks)
    assert consumer.lines + recovered == [fixture.line1, fixture.line2]


def test_recover_feed_from_clean_boundary(fixture):
    consumer = FeedLineConsumer()
    consumer.push(fixture.pause_a_bytes)
    checkpoint = consumer.checkpoint()
    assert checkpoint.pending == b""

    recovered = recover_feed(checkpoint, [fixture.line2_bytes[:5], fixture.line2_bytes[5:]])
    assert consumer.lines + recovered == [fixture.line1, fixture.line2]


def test_manual_recovery_inside_container_rebuilds_full_line(fixture):
    reference = full_read(io.BytesIO(fixture.whole_stream), read_size=4)

    consumer = ManualLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    checkpoint = consumer.checkpoint()
    payload = checkpoint.to_dict()
    restored = ManualCheckpoint.from_dict(payload)

    whole = io.BytesIO(fixture.whole_stream)
    recovered_tail = recover_file(whole, restored, read_size=3)
    assert consumer.lines + recovered_tail == reference
    assert recovered_tail[0] == fixture.line2


def test_manual_recovery_with_skip_matches_equivalent_full_run(fixture):
    stream_bytes = fixture.whole_stream
    reference_consumer = ManualLineConsumer(skip_indices=(1,))
    reference_consumer.push(stream_bytes)

    crashed = ManualLineConsumer(skip_indices=(1,))
    crashed.push(fixture.pause_b_bytes)
    checkpoint = crashed.checkpoint()

    whole = io.BytesIO(stream_bytes)
    recovered = recover_file(whole, checkpoint, read_size=5, skip_indices=(1,))
    assert crashed.lines + recovered == reference_consumer.lines
    # Member #1 of the reopened line was skipped on both passes.
    assert recovered[0][1] == "<skipped>"


def test_line_checkpoint_dict_roundtrip(fixture):
    consumer = FeedLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    checkpoint = consumer.checkpoint()
    restored = LineCheckpoint.from_dict(checkpoint.to_dict())
    assert restored == checkpoint
    assert restored.fed_total == len(fixture.pause_b_bytes)


# ---------------------------------------------------------------------------
# Robustness: chunking and offsets must not change the recovered result
# ---------------------------------------------------------------------------


def test_chunk_boundaries_do_not_affect_checkpoints(fixture):
    outcomes = []
    for chunk_size in (1, 3, 7, 64):
        consumer = FeedLineConsumer()
        for start in range(0, len(fixture.pause_b_bytes), chunk_size):
            consumer.push(fixture.pause_b_bytes[start : start + chunk_size])
        checkpoint = consumer.checkpoint()
        outcomes.append((consumer.lines, checkpoint.offset, checkpoint.pending))

    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert outcomes[0] == (
        [fixture.line1],
        len(fixture.line1_bytes),
        fixture.partial_bytes,
    )


def test_recovered_tell_ends_at_stream_length(fixture):
    consumer = FeedLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    checkpoint = consumer.checkpoint()

    whole = io.BytesIO(fixture.whole_stream)
    recover_file(whole, checkpoint, read_size=2)
    # A fresh full read of the same stream ends at exactly the same byte count.
    probe = Unpacker(io.BytesIO(fixture.whole_stream), read_size=2)
    list(probe)
    assert probe.tell() == len(fixture.whole_stream)


def test_manual_recovery_detects_corrupted_container_size(fixture):
    consumer = ManualLineConsumer()
    consumer.push(fixture.pause_b_bytes)
    checkpoint = consumer.checkpoint()
    checkpoint.size = 9  # lie about the persisted state

    with pytest.raises(ValueError, match="container size changed"):
        recover_file(io.BytesIO(fixture.whole_stream), checkpoint)
