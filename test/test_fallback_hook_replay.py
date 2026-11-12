#!/usr/bin/env python
"""Regression tests for the pure-Python fallback Unpacker.

These tests pin down what the decoding hooks (``object_pairs_hook``,
``list_hook`` and ``ext_hook``) actually observe when msgpack data is fed to
the fallback implementation either in one block or incrementally:

* the wire order of map entries, including duplicate keys;
* the absolute stream position at which every nesting level finishes;
* the fact that, after a child container is fully buffered while its parent
  still needs bytes, re-entering the parser replays the finished child and its
  hooks from the last checkpoint instead of resuming inside the parent;
* which failures leave a published position that can be retried safely and
  which ones can trigger hook side effects more than once.

The C extension keeps its parse stack between calls, so it does not replay
hooks; everything below is specific to ``msgpack.fallback``.
"""

from io import BytesIO

from pytest import mark, raises

from msgpack import ExtType, OutOfData, Packer, Unpacker

pytestmark = mark.skipif(
    Unpacker.__module__ != "msgpack.fallback",
    reason="hook replay on partial feed is specific to the pure-Python fallback",
)


class HookFailure(Exception):
    pass


def fail_pairs(pairs):
    raise HookFailure()


def fail_list(values):
    raise HookFailure()


def fail_ext(code, data):
    raise HookFailure()


class ChunkedFile:
    """File-like object that never returns more than ``cap`` bytes per read."""

    def __init__(self, data, cap):
        self._data = data
        self._pos = 0
        self._cap = cap
        self.read_calls = 0

    def read(self, n=-1):
        self.read_calls += 1
        if n is None or n < 0:
            n = self._cap
        size = min(n, self._cap, len(self._data) - self._pos)
        chunk = self._data[self._pos : self._pos + size]
        self._pos += size
        return chunk


class HookRecorder:
    """Records each hook call together with the absolute stream position.

    The fallback only publishes positions through ``tell()`` when a whole
    top-level object is consumed, so the in-flight absolute position is
    reconstructed from the internal buffer indexes.
    """

    def __init__(self, unpacker):
        self.unpacker = unpacker
        self.events = []

    def position(self):
        up = self.unpacker
        return up.tell() + up._buff_i - up._buf_checkpoint

    def pairs(self, pairs):
        self.events.append(("pairs", [tuple(pair) for pair in pairs], self.position()))
        return dict(pairs)

    def list_hook(self, values):
        self.events.append(("list", list(values), self.position()))
        return values

    def ext_hook(self, code, data):
        self.events.append(("ext", code, bytes(data), self.position()))
        return ExtType(code, data)


def build_nested_payload():
    """Pack a record that exercises every hook on a nested structure.

    Wire layout (offsets included for the end-position assertions)::

        00: fixmap(3)                       outer map header
        01: fixstr(1) "a"
        03: fixmap(3)                       inner map header
        04: "k" 1  "k" 2  "z" 3             duplicate key "k"
        13:                                 inner map ends here
        13: "b"  fixarray(2) 10 20
        18:                                 list ends here
        18: "c"  fixext2(7) "xy"
        24:                                 ext and outer map end here
    """
    packer = Packer()
    inner = packer.pack_map_pairs([("k", 1), ("k", 2), ("z", 3)])
    payload = (
        packer.pack_map_header(3)
        + packer.pack("a")
        + inner
        + packer.pack("b")
        + packer.pack([10, 20])
        + packer.pack("c")
        + packer.pack(ExtType(7, b"xy"))
    )
    return payload, {
        "inner_end": 13,
        "list_end": 18,
        "outer_end": 24,
        "inner_pairs": [("k", 1), ("k", 2), ("z", 3)],
        "outer_pairs": [
            ("a", {"k": 2, "z": 3}),
            ("b", [10, 20]),
            ("c", ExtType(7, b"xy")),
        ],
    }


def make_recording_unpacker(file_like=None, read_size=0):
    unpacker = Unpacker(file_like, read_size=read_size)
    recorder = HookRecorder(unpacker)
    unpacker._object_pairs_hook = recorder.pairs
    unpacker._list_hook = recorder.list_hook
    unpacker._ext_hook = recorder.ext_hook
    return unpacker, recorder


def feed_byte_by_byte(unpacker, payload):
    for index in range(len(payload)):
        unpacker.feed(payload[index : index + 1])
        try:
            while True:
                unpacker.unpack()
        except OutOfData:
            continue


def test_hooks_see_wire_order_duplicate_keys_and_end_positions():
    payload, expected = build_nested_payload()
    unpacker, recorder = make_recording_unpacker()

    unpacker.feed(payload)
    result = unpacker.unpack()

    assert result == {"a": {"k": 2, "z": 3}, "b": [10, 20], "c": ExtType(7, b"xy")}
    assert unpacker.tell() == expected["outer_end"]

    # The pairs hook keeps the wire order and preserves the duplicate key;
    # dict construction below the hook is what collapses it to last-wins.
    assert recorder.events == [
        ("pairs", expected["inner_pairs"], expected["inner_end"]),
        ("list", [10, 20], expected["list_end"]),
        ("ext", 7, b"xy", expected["outer_end"]),
        ("pairs", expected["outer_pairs"], expected["outer_end"]),
    ]


def test_block_reading_file_like_does_not_replay_hooks():
    payload, expected = build_nested_payload()
    stream = ChunkedFile(payload, cap=1)
    unpacker, recorder = make_recording_unpacker(stream, read_size=1)

    assert unpacker.unpack() == {
        "a": {"k": 2, "z": 3},
        "b": [10, 20],
        "c": ExtType(7, b"xy"),
    }
    assert stream.read_calls == len(payload)
    assert unpacker.tell() == expected["outer_end"]

    # Every hook fires exactly once even though the file returned one byte at
    # a time: block reads refill the buffer instead of restarting the object.
    assert recorder.events == [
        ("pairs", expected["inner_pairs"], expected["inner_end"]),
        ("list", [10, 20], expected["list_end"]),
        ("ext", 7, b"xy", expected["outer_end"]),
        ("pairs", expected["outer_pairs"], expected["outer_end"]),
    ]


def test_chunked_feed_replays_finished_child_hooks_until_parent_completes():
    payload, expected = build_nested_payload()
    unpacker, recorder = make_recording_unpacker()

    feed_byte_by_byte(unpacker, payload)

    inner_fires = [
        event for event in recorder.events if event[0] == "pairs" and event[1][0][0] == "k"
    ]
    list_fires = [event for event in recorder.events if event[0] == "list"]
    ext_fires = [event for event in recorder.events if event[0] == "ext"]
    outer_fires = [
        event for event in recorder.events if event[0] == "pairs" and event[1][0][0] == "a"
    ]

    # The inner map becomes complete at byte 13; each later feed restarts the
    # outer object from its checkpoint and runs the inner hook again. Every
    # replay reports the same end position and the same duplicate-key pairs.
    assert len(inner_fires) == 12
    assert all(event[1] == expected["inner_pairs"] for event in inner_fires)
    assert {event[2] for event in inner_fires} == {expected["inner_end"]}

    # The list finishes at byte 18 and is likewise replayed on every attempt
    # made before the outer map completes at byte 24.
    assert len(list_fires) == 7
    assert all(event[2] == expected["list_end"] for event in list_fires)

    # The ext object is the last value of the outer map, so it only exists on
    # the final, successful pass.
    assert len(ext_fires) == 1
    assert len(outer_fires) == 1
    assert outer_fires[0][2] == expected["outer_end"]
    assert unpacker.tell() == expected["outer_end"]


def test_ext_hook_is_replayed_when_a_later_sibling_is_missing():
    packer = Packer()
    # {"x": ExtType(7, b"ab"), "y": 1}
    payload = (
        packer.pack_map_header(2)
        + packer.pack("x")
        + packer.pack(ExtType(7, b"ab"))
        + packer.pack("y")
        + packer.pack(1)
    )
    ext_cut = 1 + 2 + 4  # header + "x" + fixext2; the next key is still missing

    events = []

    def record_ext(code, data):
        events.append((code, bytes(data)))
        return ExtType(code, data)

    unpacker = Unpacker(ext_hook=record_ext)
    unpacker.feed(payload[:ext_cut])
    with raises(OutOfData):
        unpacker.unpack()
    assert events == [(7, b"ab")]

    unpacker.feed(payload[ext_cut:])
    assert unpacker.unpack() == {"x": ExtType(7, b"ab"), "y": 1}
    assert events == [(7, b"ab"), (7, b"ab")]


def test_completed_top_level_row_hook_fires_once_across_a_feed_boundary():
    payload, expected = build_nested_payload()
    following_row = Packer().pack({"next": "row"})
    stream = payload + following_row

    unpacker, recorder = make_recording_unpacker()
    unpacker.feed(stream[: len(payload) + 1])  # whole first row plus one byte
    assert unpacker.unpack() == {
        "a": {"k": 2, "z": 3},
        "b": [10, 20],
        "c": ExtType(7, b"xy"),
    }
    with raises(OutOfData):
        unpacker.unpack()

    unpacker.feed(stream[len(payload) + 1 :])
    assert unpacker.unpack() == {"next": "row"}

    # The finished row was consumed at a checkpoint, so its hooks are never
    # replayed; only the incomplete second row is retried.
    inner_fires = [
        event for event in recorder.events if event[0] == "pairs" and event[1][0][0] == "k"
    ]
    assert len(inner_fires) == 1
    assert inner_fires[0][2] == expected["inner_end"]


def test_outofdata_rolls_back_to_checkpoint_and_keeps_published_position():
    payload, expected = build_nested_payload()
    unpacker, recorder = make_recording_unpacker()

    unpacker.feed(payload[: expected["inner_end"]])
    with raises(OutOfData):
        unpacker.unpack()

    # OutOfData rewinds buff_i to the checkpoint without publishing anything:
    # retrying after more bytes loses no committed stream position.
    assert unpacker.tell() == 0
    assert unpacker._buf_checkpoint == 0
    assert unpacker._buff_i == 0
    assert len(recorder.events) == 1
    assert recorder.events[0] == ("pairs", expected["inner_pairs"], expected["inner_end"])

    unpacker.feed(payload[expected["inner_end"] :])
    assert unpacker.unpack() == {
        "a": {"k": 2, "z": 3},
        "b": [10, 20],
        "c": ExtType(7, b"xy"),
    }
    assert unpacker.tell() == expected["outer_end"]

    # The retry is position-safe but not side-effect-free: the finished inner
    # map hook ran again during the second pass over the same bytes.
    inner_fires = [
        event for event in recorder.events if event[0] == "pairs" and event[1][0][0] == "k"
    ]
    assert len(inner_fires) == 2


def test_iterator_stopiteration_uses_the_same_rollback():
    payload, expected = build_nested_payload()
    unpacker, recorder = make_recording_unpacker()

    unpacker.feed(payload[: expected["inner_end"]])
    with raises(StopIteration):
        next(unpacker)
    assert unpacker.tell() == 0
    assert unpacker._buff_i == 0

    unpacker.feed(payload[expected["inner_end"] :])
    assert next(unpacker) == {
        "a": {"k": 2, "z": 3},
        "b": [10, 20],
        "c": ExtType(7, b"xy"),
    }
    inner_fires = [
        event for event in recorder.events if event[0] == "pairs" and event[1][0][0] == "k"
    ]
    assert len(inner_fires) == 2


def test_hook_exception_rewinds_to_checkpoint_and_desyncs_retry():
    payload, expected = build_nested_payload()
    following_row = Packer().pack({"next": "row"})
    stream = payload + following_row

    cases = [
        ("object_pairs_hook", dict(object_pairs_hook=fail_pairs), expected["inner_end"], "b"),
        ("list_hook", dict(list_hook=fail_list), expected["list_end"], "c"),
        ("ext_hook", dict(ext_hook=fail_ext), expected["outer_end"], {"next": "row"}),
    ]

    for _hook_name, kwargs, failure_position, first_resume in cases:
        unpacker = Unpacker(**kwargs)
        unpacker.feed(stream)
        with raises(HookFailure):
            unpacker.unpack()

        # The exception propagates before _consume(): tell() stays at the last
        # published boundary while buff_i points just past the failed object.
        assert unpacker.tell() == 0
        assert unpacker._buf_checkpoint == 0
        assert unpacker._buff_i == failure_position

        # Continuing on the same unpacker does not re-enter the failed hook;
        # it parses the remaining bytes as standalone objects and loses the
        # enclosing map's framing.
        assert unpacker.unpack() == first_resume


def test_hook_failure_then_feed_more_does_not_reenter_hook_but_desyncs():
    payload, expected = build_nested_payload()

    attempts = []

    def failing_pairs(pairs):
        attempts.append([tuple(pair) for pair in pairs])
        raise HookFailure()

    unpacker = Unpacker(object_pairs_hook=failing_pairs)
    unpacker.feed(payload[: expected["inner_end"]])  # inner map done, parent incomplete
    with raises(HookFailure):
        unpacker.unpack()
    assert len(attempts) == 1

    # Feeding the missing bytes does not restore the map framing: parsing
    # resumes after the failed inner map instead of rebuilding the parent.
    unpacker.feed(payload[expected["inner_end"] :])
    assert unpacker.unpack() == "b"
    assert len(attempts) == 1  # the failing hook is not invoked a second time


def test_recovery_starts_fresh_from_the_last_published_tell():
    payload, _ = build_nested_payload()
    packer = Packer()
    good_row = packer.pack({"ok": 1})
    following_row = packer.pack({"next": "row"})
    stream = good_row + payload + following_row

    def pairs_hook(pairs):
        if any(key == "k" for key, _ in pairs):
            raise HookFailure()
        return dict(pairs)

    unpacker = Unpacker(object_pairs_hook=pairs_hook)
    unpacker.feed(stream)
    assert unpacker.unpack() == {"ok": 1}
    boundary = unpacker.tell()
    assert boundary == len(good_row)

    with raises(HookFailure):
        unpacker.unpack()
    # The failed row was never published, so tell() remains a row boundary.
    assert unpacker.tell() == boundary

    # A fresh unpacker over the unconsumed suffix retries the poison row
    # deterministically, and a hook-free unpacker can skip the whole row and
    # reach the following row intact.
    retry = Unpacker(object_pairs_hook=pairs_hook)
    retry.feed(stream[boundary:])
    with raises(HookFailure):
        retry.unpack()

    recovery = Unpacker()
    recovery.feed(stream[boundary:])
    recovery.skip()
    assert recovery.tell() == len(payload)
    assert recovery.unpack() == {"next": "row"}


def test_duplicate_keys_reach_pairs_hook_but_collapse_in_object_hook():
    packer = Packer()
    payload = packer.pack_map_pairs([("k", 1), ("k", 2), ("z", 3)])

    seen_pairs = []

    def record_pairs(pairs):
        seen_pairs.append(list(pairs))
        return dict(pairs)

    pairs_result = Unpacker(object_pairs_hook=record_pairs)
    pairs_result.feed(payload)
    assert pairs_result.unpack() == {"k": 2, "z": 3}
    assert seen_pairs == [[("k", 1), ("k", 2), ("z", 3)]]

    seen_dicts = []

    def record_dict(obj):
        seen_dicts.append(dict(obj))
        return obj

    dict_result = Unpacker(object_hook=record_dict)
    dict_result.feed(payload)
    assert dict_result.unpack() == {"k": 2, "z": 3}
    assert seen_dicts == [{"k": 2, "z": 3}]


def test_unpackb_hook_failure_does_not_publish_a_position():
    payload, _ = build_nested_payload()
    unpacker = Unpacker(max_buffer_size=len(payload), object_pairs_hook=fail_pairs)
    unpacker.feed(payload)
    with raises(HookFailure):
        unpacker._unpack()
    assert unpacker.tell() == 0
    assert unpacker._buf_checkpoint == 0
    assert unpacker._buff_i == 13


def test_block_reading_file_like_hook_failure_keeps_checkpoint():
    payload, expected = build_nested_payload()
    unpacker = Unpacker(
        ChunkedFile(payload, cap=3),
        read_size=3,
        object_pairs_hook=fail_pairs,
    )
    with raises(HookFailure):
        unpacker.unpack()
    assert unpacker.tell() == 0
    assert unpacker._buf_checkpoint == 0
    assert unpacker._buff_i == expected["inner_end"]

    # Even in file mode the framing is lost after a hook failure; retrying
    # parses the remainder as standalone values rather than the outer map.
    assert unpacker.unpack() == "b"


def test_bytesio_and_feed_present_identical_hook_trace_on_success():
    payload, expected = build_nested_payload()

    fed, fed_recorder = make_recording_unpacker()
    fed.feed(payload)
    fed.unpack()

    streamed, streamed_recorder = make_recording_unpacker(BytesIO(payload))
    streamed.unpack()

    assert fed_recorder.events == streamed_recorder.events
    assert fed.tell() == streamed.tell() == expected["outer_end"]
