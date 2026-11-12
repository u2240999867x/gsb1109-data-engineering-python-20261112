"""Runnable end-to-end checkpoint/recovery demo.

It simulates a log consumer that crashes at two different points, persists the
minimum state, then recovers from an addressable byte stream and proves the
reconstructed line list equals one uninterrupted read.

Run from the repository root::

    MSGPACK_PUREPYTHON=1 python -m replay.demo_recovery
"""

from __future__ import annotations

import io
import json

from msgpack.fallback import Unpacker

from .diagnosis import build_fixture
from .recovery import (
    FeedLineConsumer,
    ManualLineConsumer,
    full_read,
    recover_feed,
    recover_file,
)


def _heading(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> None:
    fixture = build_fixture()
    stream_bytes = fixture.whole_stream

    _heading("Reference: a single uninterrupted read")
    reference = full_read(io.BytesIO(stream_bytes), read_size=4)
    print(reference)

    _heading("Case 1: feed-based consumer crashes inside a half message (pause B)")
    consumer = FeedLineConsumer()
    consumer.push(fixture.pause_b_bytes[:4])
    consumer.push(fixture.pause_b_bytes[4:])
    checkpoint = consumer.checkpoint()
    persisted = json.dumps(checkpoint.to_dict())
    print("committed lines before crash:", consumer.lines)
    print("persisted checkpoint          :", persisted)

    # Recovery against a seekable stream needs only the durable offset.
    durable = type(checkpoint).from_dict(json.loads(persisted))
    tail = recover_file(io.BytesIO(stream_bytes), durable, read_size=3)
    print("recovered tail from seek()    :", tail)
    assert consumer.lines + tail == reference

    # Feed-mode recovery reuses the persisted pending bytes instead.
    feed_tail = recover_feed(durable, [fixture.valid_suffix_for_b])
    print("recovered via feed() replay   :", feed_tail)
    assert consumer.lines + feed_tail == reference

    _heading("Case 2: manual header+skip consumer pauses inside a container")
    manual = ManualLineConsumer(skip_indices=(1,))
    manual.push(fixture.pause_b_bytes)
    manual_checkpoint = manual.checkpoint()
    persisted_manual = json.dumps(manual_checkpoint.to_dict())
    print("committed lines before pause  :", manual.lines)
    print("persisted checkpoint          :", persisted_manual)

    restored = type(manual_checkpoint).from_dict(json.loads(persisted_manual))
    manual_tail = recover_file(
        io.BytesIO(stream_bytes), restored, read_size=5, skip_indices=(1,)
    )
    print("rebuilt open line + remainder :", manual_tail)
    assert manual.lines + manual_tail == ManualLineConsumer(skip_indices=(1,)).push(stream_bytes)

    _heading("Case 3: clean boundary (pause A) has no pending bytes")
    clean = FeedLineConsumer()
    clean.push(fixture.pause_a_bytes)
    clean_checkpoint = clean.checkpoint()
    print("persisted checkpoint          :", json.dumps(clean_checkpoint.to_dict()))
    clean_tail = recover_file(io.BytesIO(stream_bytes), clean_checkpoint)
    assert clean.lines + clean_tail == reference
    print("recovery identical to full read:", clean.lines + clean_tail == reference)

    _heading("Sanity: tell() at the two pause points is identical")
    at_a = Unpacker()
    at_a.feed(fixture.pause_a_bytes)
    list(at_a)
    at_b = Unpacker()
    at_b.feed(fixture.pause_b_bytes)
    list(at_b)
    print(f"tell() at A = {at_a.tell()}, tell() at B = {at_b.tell()} "
          f"(B hides {len(fixture.pause_b_bytes) - at_b.tell()} pending bytes)")
    assert at_a.tell() == at_b.tell()

    print("\nAll recovered line lists match the single full read.")


if __name__ == "__main__":
    main()
