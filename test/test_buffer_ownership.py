"""Ownership semantics of the pure-Python (fallback) buffers.

Receive side: Unpacker reuses one internal bytearray across feed() calls.
Send side: Packer(autoreset=False) hands out getbuffer() views that batch
consumers may acknowledge (release) independently and in any order.

Expected bytes are always saved independently via packb() (autoreset copy),
so a stale or aliased view is detectable by comparison.
"""

import pytest

from msgpack import packb
from msgpack.exceptions import BufferFull, OutOfData
from msgpack.fallback import Packer, Unpacker


# ---------------------------------------------------------------------------
# Receive side: feed() -> internal bytearray reuse -> downstream objects
# ---------------------------------------------------------------------------


def test_feed_copies_input_and_releases_borrow():
    unpacker = Unpacker()
    source = bytearray(packb([1, "two", b"three"]))
    view = memoryview(source)
    unpacker.feed(view)

    # feed() released only its own temporary view; the caller's view and
    # bytearray are fully owned by the caller again.
    assert bytes(view) == bytes(source)
    view.release()

    # The caller may reuse/resize the same bytearray immediately: the
    # unpacker holds no borrow and already copied the bytes it needs.
    source[:] = packb("garbage replacement")
    assert unpacker.unpack() == [1, "two", b"three"]


def test_unpacked_results_survive_buffer_compaction():
    unpacker = Unpacker()
    first = ["alpha", b"\x00\x01", 12345]
    second = ["beta", b"\x02\x03", 67890]
    unpacker.feed(packb(first))
    got_first = unpacker.unpack()

    buffer_id = id(unpacker._buffer)
    unpacker.feed(packb(second))  # del _buffer[:checkpoint] + extend in place
    assert id(unpacker._buffer) == buffer_id  # same bytearray is reused

    assert unpacker.unpack() == second
    # Previously delivered objects are owned copies, immune to the reuse.
    assert got_first == first


def test_read_bytes_returns_owned_copy():
    unpacker = Unpacker()
    unpacker.feed(packb("header") + b"\x00\x11\x22")
    assert unpacker.unpack() == "header"
    chunk = unpacker.read_bytes(3)
    assert type(chunk) is bytes
    unpacker.feed(packb("next batch"))  # compacts and reuses the bytearray
    assert chunk == b"\x00\x11\x22"
    assert unpacker.unpack() == "next batch"


def test_partial_feed_rolls_back_and_resumes():
    unpacker = Unpacker()
    full = packb([1, 2, 3]) + packb("done")
    unpacker.feed(full[:3])
    with pytest.raises(OutOfData):
        unpacker.unpack()
    # Failed unpack rolled back to the checkpoint; nothing was lost.
    unpacker.feed(full[3:])
    assert unpacker.unpack() == [1, 2, 3]
    assert unpacker.unpack() == "done"


def test_feed_buffer_full_keeps_pending_data():
    unpacker = Unpacker(max_buffer_size=8)
    unpacker.feed(packb("ab"))
    with pytest.raises(BufferFull):
        unpacker.feed(b"\x00" * 16)
    # The rejected feed did not disturb the bytes already buffered.
    assert unpacker.unpack() == "ab"


# ---------------------------------------------------------------------------
# Send side: getbuffer() views handed to two batch consumers
# ---------------------------------------------------------------------------


def test_getbuffer_zero_copy_and_write_blocked_while_borrowed():
    packer = Packer(autoreset=False)
    packer.pack("batch-1")
    expected = packb("batch-1")

    view = packer.getbuffer()
    assert type(view) is memoryview
    assert bytes(view) == expected

    # Buffer is legitimately borrowed: resizing writes are refused.
    with pytest.raises(BufferError):
        packer.pack("batch-2")
    # The failed pack force-reset the packer; the borrowed view is untouched
    # and does not observe the new batch.
    assert bytes(view) == expected
    assert packer.bytes() == b""

    view.release()
    packer.pack("batch-2")
    assert packer.bytes() == packb("batch-2")


def test_reset_hands_off_buffer_without_invalidating_views():
    packer = Packer(autoreset=False)
    packer.pack([1, 2])
    expected_1 = packb([1, 2])
    view_1 = packer.getbuffer()

    # reset() swaps in a fresh BytesIO; view_1 keeps the old buffer alive.
    packer.reset()
    packer.pack([3, 4])
    expected_2 = packb([3, 4])
    view_2 = packer.getbuffer()

    # Old batch never observes new batch content.
    assert bytes(view_1) == expected_1
    assert bytes(view_2) == expected_2

    # Consumers may acknowledge in reverse order.
    view_2.release()
    assert bytes(view_1) == expected_1
    view_1.release()


def test_two_consumers_ack_in_reverse_order():
    packer = Packer(autoreset=False)
    packer.pack("batch-A")
    packer.pack("batch-B")
    expected_a = packb("batch-A")
    expected_b = packb("batch-B")

    view = packer.getbuffer()
    consumer_a = view[: len(expected_a)]
    consumer_b = view[len(expected_a) :]
    # The parent view may go away first; slices keep the buffer pinned.
    view.release()

    borrowed = packer._buffer  # the shared buffer both consumers read

    # Legitimately borrowed: the first write attempt is refused, and the
    # fallback packer reacts by abandoning the borrowed buffer.
    with pytest.raises(BufferError):
        packer.pack("batch-C")
    assert packer._buffer is not borrowed

    # Consumer B acknowledges first; A's slice still pins the old buffer.
    assert bytes(consumer_b) == expected_b
    consumer_b.release()
    with pytest.raises(BufferError):
        borrowed.write(b"x")

    # Consumer A acknowledges; no exports remain on the old buffer.
    assert bytes(consumer_a) == expected_a
    consumer_a.release()
    borrowed.write(b"x")  # writable again now that nothing borrows it

    # The packer moved on to its fresh buffer regardless of ack order.
    packer.pack("batch-C")
    assert packer.bytes() == packb("batch-C")


def test_failed_pack_resets_buffer_but_preserves_delivered_views():
    packer = Packer(autoreset=False)
    packer.pack("delivered")
    expected = packb("delivered")
    view = packer.getbuffer()

    with pytest.raises(TypeError):
        packer.pack(object())
    # Failure path force-resets the packer; the delivered view of the old
    # buffer stays authoritative.
    assert bytes(view) == expected
    assert packer.bytes() == b""
    view.release()


def test_autoreset_pack_returns_independent_bytes():
    packer = Packer()  # autoreset=True: copy-based handoff
    first = packer.pack("one")
    second = packer.pack("two")
    assert type(first) is bytes
    assert first == packb("one")
    assert second == packb("two")
    assert packer.bytes() == b""


def test_control_in_place_reuse_would_corrupt_old_batch():
    # Control: if a producer rewrote a shared buffer in place, an old
    # consumer's slice WOULD observe the new batch.  The fallback Packer
    # avoids this by swapping buffers on reset()/failure; this control
    # shows the comparisons above can actually tell the two apart.
    shared = bytearray(packb("old-batch"))
    old_slice = memoryview(shared)[:5]
    shared[:5] = b"new-b"
    assert bytes(old_slice) == b"new-b"  # hazard: old batch sees new content
    old_slice.release()
