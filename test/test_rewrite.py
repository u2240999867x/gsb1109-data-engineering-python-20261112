import os
import struct
import tempfile
from io import BytesIO

import pytest

from msgpack import (
    Copy,
    Delete,
    ExtType,
    MapEntry,
    Path,
    PathError,
    PlanError,
    Replace,
    RewritePlan,
    StreamRewriter,
    TruncatedMessage,
    Timestamp,
    packb,
    rewrite_stream,
    rewriteb,
    unpackb,
)
from msgpack.exceptions import ExtraData, RewriteError


def test_empty_plan_returns_identical_bytes():
    data = packb({"a": [1, 2, 3], "b": "x", "c": None})
    assert rewriteb(data, RewritePlan()) == data
    assert rewriteb(data, []) == data


def test_replace_scalar_and_nested():
    data = packb({"a": 1, "b": [10, 20, 30], "c": "x"})
    plan = RewritePlan([Replace(("a",), 999), Replace(("b", 1), -7)])
    assert unpackb(rewriteb(data, plan)) == {"a": 999, "b": [10, -7, 30], "c": "x"}


def test_replace_root():
    data = packb({"a": 1})
    assert unpackb(rewriteb(data, RewritePlan([Replace((), [1, 2, 3])]))) == [1, 2, 3]


def test_delete_array_element_keeps_order():
    data = packb([0, 1, 2, 3, 4])
    out = rewriteb(data, RewritePlan([Delete((0,)), Delete((2,))]))
    assert unpackb(out) == [1, 3, 4]


def test_delete_map_pair_removes_key_and_value():
    data = packb({"a": 1, "b": 2, "c": 3})
    out = rewriteb(data, RewritePlan([Delete(("b",))]))
    assert unpackb(out) == {"a": 1, "c": 3}
    # The untouched pairs survive byte for byte.
    assert b"\xa1a\x01" in out and b"\xa1c\x03" in out


def test_delete_root_emits_empty_message():
    assert rewriteb(packb({"a": 1}), RewritePlan([Delete(())])) == b""


def test_copy_source_after_destination():
    data = packb([1, 2, {"big": list(range(50))}])
    out = rewriteb(data, RewritePlan([Copy((2, "big"), (0,))]))
    result = unpackb(out)
    assert result[0] == list(range(50))
    assert result[2]["big"] == list(range(50))


def test_copy_source_before_destination():
    data = packb({"src": [1, 2, 3], "dst": 0})
    out = rewriteb(data, RewritePlan([Copy(("src",), ("dst",))]))
    assert unpackb(out) == {"src": [1, 2, 3], "dst": [1, 2, 3]}


def test_two_copies_swap_original_subtrees():
    data = packb({"a": [1, 2], "b": [3, 4]})
    plan = RewritePlan([Copy(("a",), ("b",)), Copy(("b",), ("a",))])
    assert unpackb(rewriteb(data, plan)) == {"a": [3, 4], "b": [1, 2]}


def test_copy_onto_itself_is_identity():
    data = packb({"a": [1, 2]})
    assert rewriteb(data, RewritePlan([Copy(("a",), ("a",))])) == data


def test_copy_reads_original_even_when_source_is_deleted():
    data = packb({"a": 5, "b": 0})
    plan = RewritePlan([Copy(("a",), ("b",)), Delete(("a",))])
    assert unpackb(rewriteb(data, plan)) == {"b": 5}


def test_copy_reads_original_even_when_source_is_replaced():
    data = packb({"a": [1, 2], "b": 0})
    plan = RewritePlan([Copy(("a",), ("b",)), Replace(("a",), 99)])
    assert unpackb(rewriteb(data, plan)) == {"a": 99, "b": [1, 2]}


def test_copy_source_kept_while_other_target_inside_is_rewritten():
    data = packb({"a": {"x": 1}, "b": None})
    # The copy takes the original bytes of "a"; the in-place replacement of
    # "a.x" must not leak into the copied subtree.
    plan = RewritePlan([Copy(("a",), ("b",)), Replace(("a", "x"), 9)])
    result = unpackb(rewriteb(data, plan))
    assert result == {"a": {"x": 9}, "b": {"x": 1}}


def test_duplicate_map_keys_distinguished_by_occurrence():
    data = b"\x83\xa1k\x01\xa1k\x02\xa1z\x03"  # {"k":1,"k":2,"z":3}
    plan = RewritePlan([Replace((MapEntry("k", 1),), 7)])
    out = rewriteb(data, plan)
    pairs = unpackb(out, strict_map_key=False, object_pairs_hook=list)
    assert pairs == [("k", 1), ("k", 7), ("z", 3)]

    out2 = rewriteb(data, RewritePlan([Delete((MapEntry("k", 0),))]))
    assert unpackb(out2, strict_map_key=False, object_pairs_hook=list) == [
        ("k", 2),
        ("z", 3),
    ]


def test_mapentry_repr_and_path_validation():
    assert repr(MapEntry("k", 2)) == "MapEntry('k', 2)"
    assert Path("a", 3, MapEntry("b", 1)) == (MapEntry("a", 0), 3, MapEntry("b", 1))
    with pytest.raises(ValueError):
        Path(-1)
    with pytest.raises(TypeError):
        Path(1.5)
    with pytest.raises(ValueError):
        MapEntry("k", -1)


def test_bytes_keys_match_bin_and_non_strict_raw_keys():
    data = packb({b"k": 1})
    assert unpackb(rewriteb(data, RewritePlan([Replace((b"k",), 2)]))) == {b"k": 2}
    # String (raw) keys decode to str even with strict_map_key disabled.
    raw = b"\x81\xa1k\x05"
    out = rewriteb(raw, RewritePlan([Replace(("k",), 6)]), strict_map_key=False)
    assert out == b"\x81\xa1k\x06"


def test_missing_path_is_rejected():
    with pytest.raises(PathError):
        rewriteb(packb({"a": 1}), RewritePlan([Delete(("z",))]))
    with pytest.raises(PathError):
        rewriteb(packb([1, 2]), RewritePlan([Replace((5,), 0)]))
    with pytest.raises(PathError):
        rewriteb(
            b"\x81\xa1k\x01",
            RewritePlan([Replace((MapEntry("k", 1),), 0)]),
        )


def test_duplicate_target_rejected_at_plan_build_time():
    with pytest.raises(PlanError):
        RewritePlan([Delete(("a",)), Replace(("a",), 1)])
    with pytest.raises(PlanError):
        RewritePlan([Copy(("x",), ("a",)), Delete(("a",))])


def test_descendant_target_through_replaced_or_deleted_ancestor_rejected():
    data = packb({"a": {"b": 1}})
    with pytest.raises(PlanError):
        rewriteb(data, RewritePlan([Delete(("a",)), Replace(("a", "b"), 2)]))
    with pytest.raises(PlanError):
        rewriteb(data, RewritePlan([Replace(("a",), {}), Delete(("a", "b"))]))


def test_copy_source_through_replaced_ancestor_rejected():
    data = packb({"a": {"b": 1}, "z": 0})
    with pytest.raises(PlanError):
        rewriteb(
            data,
            RewritePlan([Replace(("a",), {}), Copy(("a", "b"), ("z",))]),
        )
    with pytest.raises(PlanError):
        rewriteb(
            data,
            RewritePlan([Delete(("a",)), Copy(("a", "b"), ("z",))]),
        )


def test_copy_target_cannot_contain_other_targets():
    data = packb({"a": {"b": 1}, "z": 0})
    with pytest.raises(PlanError):
        rewriteb(
            data,
            RewritePlan([Copy(("z",), ("a",)), Replace(("a", "b"), 2)])
        )


def test_array_count_rewritten_across_header_formats():
    data = packb([0] * 200)  # array16
    assert data[0] == 0xDC
    # 200 -> 150 stays array16
    out = rewriteb(data, RewritePlan([Delete((i,)) for i in range(50)]))
    assert out[0] == 0xDC and len(unpackb(out)) == 150
    # 200 -> 10 collapses into fixarray
    out2 = rewriteb(data, RewritePlan([Delete((i,)) for i in range(190)]))
    assert out2[0] == 0x9A and len(unpackb(out2)) == 10


def test_map_count_rewritten_across_header_formats():
    data = packb({f"k{i}": i for i in range(200)})  # map16
    assert data[0] == 0xDE
    targets = [f"k{i}" for i in range(190)]
    out = rewriteb(data, RewritePlan([Delete((k,)) for k in targets]))
    assert out[0] == 0x8A  # fixmap with 10 pairs
    assert len(unpackb(out)) == 10


def test_fixarray_grows_into_array16_is_not_needed_for_delete():
    # Replacement keeps member counts; only deletions change counts and only
    # ever shrink them, so growing headers never occur.
    data = packb([1, 2, 3])
    out = rewriteb(data, RewritePlan([Replace((0,), [1, 2, 3, 4])]))
    assert unpackb(out) == [[1, 2, 3, 4], 2, 3]
    assert out[0] == 0x93  # outer fixarray header untouched


def test_unselected_bytes_preserved_including_widths_and_ext():
    ts = Timestamp(1234567890, 0)
    assert unpackb(packb({"ts": ts})) == {"ts": ts}  # types pack fine
    # float32, float64 and fixext2 coexist and only the last pair is rewritten.
    raw = (
        b"\x84\xa1a\xca\x40\x49\x0f\xdb"
        b"\xa1b\xcb\x3f\xf0\x00\x00\x00\x00\x00\x00"
        b"\xa1e\xd5\x05hi"
        b"\xa1n\x01"
    )
    out = rewriteb(raw, RewritePlan([Replace(("n",), 2)]))
    assert b"\xca\x40\x49\x0f\xdb" in out  # float32 width preserved
    assert b"\xcb\x3f\xf0" in out  # float64 width preserved
    assert b"\xd5\x05hi" in out  # fixext preserved
    assert out[-1] == 2 and len(out) == len(raw)


def test_large_str_bin_ext_headers_pass_through_unchanged():
    payload = b"Q" * 300
    data = packb({"s": "x" * 300, "b": payload, "e": ExtType(7, b"R" * 300), "n": 0})
    out = rewriteb(data, RewritePlan([Replace(("n",), 1)]))
    assert data.index(b"\xda") == out.index(b"\xda")  # str16 header position
    assert data.index(b"\xc5") == out.index(b"\xc5")  # bin16
    assert data.index(b"\xc8") == out.index(b"\xc8")  # ext16
    assert unpackb(out)["n"] == 1


def test_truncated_message_raises_and_leaves_input_untouched():
    data = packb([1, 2, 3])
    for cut in range(1, len(data)):
        with pytest.raises(TruncatedMessage):
            rewriteb(data[:cut], RewritePlan([Replace((0,), 9)]))


def test_trailing_data_raises_extra_data():
    data = packb({"a": 1}) + packb({"b": 2})
    with pytest.raises(ExtraData):
        rewriteb(data, RewritePlan([Replace(("a",), 0)]))


@pytest.mark.parametrize("window", [1, 2, 3, 5, 7, 64, 4096])
def test_chunking_does_not_change_result(window):
    data = packb(
        {
            "users": [{"id": i, "secret": "x" * i} for i in range(12)],
            "meta": {"ok": True, "tags": [1, 2, 3]},
        }
    )
    plan = RewritePlan(
        [Delete(("users", i, "secret")) for i in range(12)]
        + [Replace(("meta", "ok"), False), Copy(("meta", "tags"), ("users", 0, "id"))]
    )
    assert rewriteb(data, plan, window=window) == rewriteb(data, plan, window=4096)


def test_stream_publishes_only_validated_messages_and_keeps_prior_events():
    source = BytesIO(
        packb({"a": 1}) + packb({"a": 2}) + packb([1, 2, 3])[:2]
    )
    destination = BytesIO()
    rewriter = StreamRewriter(
        source, destination, RewritePlan([Replace(("a",), -1)]), window=3
    )
    assert rewriter.process_one() is True
    assert rewriter.process_one() is True
    with pytest.raises(TruncatedMessage):
        rewriter.process_one()
    expected = packb({"a": -1}) + packb({"a": -1})
    assert destination.getvalue() == expected
    assert rewriter.messages_published == 2


def test_stream_aborts_message_without_writing_on_plan_error():
    class StrictSink:
        def __init__(self):
            self.written = bytearray()

        def write(self, data):
            self.written += data

    source = BytesIO(packb({"a": 1}) + packb({"b": 2}))
    sink = StrictSink()
    rewriter = StreamRewriter(
        source, sink, RewritePlan([Replace(("a",), 0)]), window=2
    )
    assert rewriter.process_one() is True
    before = bytes(sink.written)
    with pytest.raises(PathError):
        rewriter.process_one()  # second message has no "a"
    assert bytes(sink.written) == before


def test_rewrite_stream_helper_and_clean_eof():
    source = BytesIO(b"".join(packb({"i": i}) for i in range(5)))
    destination = BytesIO()
    count = rewrite_stream(
        source, destination, RewritePlan([Replace(("i",), -1)]), window=4
    )
    assert count == 5
    events = list(unpacker_for(destination.getvalue()))
    assert events == [{"i": -1}] * 5

    empty = BytesIO(b"")
    out = BytesIO()
    assert rewrite_stream(empty, out, RewritePlan()) == 0


def unpacker_for(data):
    from msgpack import Unpacker

    return Unpacker(BytesIO(data))


def test_stream_resyncs_after_a_rejected_message_and_keeps_prior_events():
    source = BytesIO(
        packb({"a": 1})          # valid, will be published
        + packb({"b": 2})        # plan path "a" missing -> rejected
        + packb({"a": 3})        # valid again; resync must reach it
    )
    destination = BytesIO()
    rewriter = StreamRewriter(
        source, destination, RewritePlan([Replace(("a",), 0)]), window=3
    )
    assert rewriter.process_one() is True
    with pytest.raises(PathError):
        rewriter.process_one()
    assert rewriter.process_one() is True
    assert destination.getvalue() == packb({"a": 0}) + packb({"a": 0})
    assert rewriter.messages_published == 2


def test_plan_is_reusable_across_messages():
    plan = RewritePlan([Replace(("a",), 0), Delete(("b",))])
    first = rewriteb(packb({"a": 1, "b": 2, "c": 3}), plan)
    second = rewriteb(packb({"a": 9, "b": 8}), plan)
    assert unpackb(first) == {"a": 0, "c": 3}
    assert unpackb(second) == {"a": 0}


def test_temporary_files_are_reclaimed(tmp_path):
    data = packb({"blob": b"Z" * 200_000, "n": 1})
    out = rewriteb(
        data,
        RewritePlan([Replace(("n",), 2)]),
        window=4096,
        spill_size=16 * 1024,
        tmpdir=str(tmp_path),
    )
    assert unpackb(out)["blob"] == b"Z" * 200_000
    assert os.listdir(str(tmp_path)) == []


def test_stream_bounded_memory_for_message_larger_than_window():
    total = 1_000_000
    header = b"\x82\xa4blob" + b"\xc6" + struct.pack(">I", total)
    footer = b"\xa1n\x01"

    class GeneratedSource:
        def __init__(self):
            self.pos = 0
            self.length = len(header) + total + len(footer)

        def read(self, n):
            out = bytearray()
            while len(out) < n and self.pos < self.length:
                if self.pos < len(header):
                    segment = header
                    offset = self.pos
                elif self.pos < len(header) + total:
                    segment = None
                    offset = self.pos - len(header)
                else:
                    segment = footer
                    offset = self.pos - len(header) - total
                take = min(n - len(out), (len(segment) if segment else total) - offset)
                out += segment[offset : offset + take] if segment else b"Z" * take
                self.pos += take
            return bytes(out)

    class NullSink:
        def __init__(self):
            self.size = 0

        def write(self, data):
            self.size += len(data)

    with tempfile.TemporaryDirectory() as tmpdir:
        sink = NullSink()
        rewriter = StreamRewriter(
            GeneratedSource(),
            sink,
            RewritePlan([Replace(("n",), 2)]),
            window=8192,
            spill_size=64 * 1024,
            tmpdir=tmpdir,
        )
        assert rewriter.run() == 1
        assert sink.size == len(header) + total + len(footer)
        assert os.listdir(tmpdir) == []


def test_deeply_nested_path_and_container_key():
    data = packb({"a": [[[{"deep": 42}]]]})
    out = rewriteb(data, RewritePlan([Replace(("a", 0, 0, 0, "deep"), "x")]))
    assert unpackb(out) == {"a": [[[{"deep": "x"}]]]}

    # A container key is skipped (it cannot match a scalar plan step) while
    # the following scalar key stays addressable.
    container_key = b"\x82\x91\x01\x01\xa1k\x01"  # {[1]: 1, "k": 1}
    out = rewriteb(
        container_key,
        RewritePlan([Replace(("k",), 2)]),
        strict_map_key=False,
    )
    assert out == b"\x82\x91\x01\x01\xa1k\x02"
    with pytest.raises(ValueError):
        rewriteb(container_key, RewritePlan(), strict_map_key=True)


def test_replacement_can_change_type_and_size_freely():
    data = packb({"a": 1})
    out = rewriteb(
        data, RewritePlan([Replace(("a",), {"nested": ["s", None, True, 1.5]})])
    )
    assert unpackb(out) == {"a": {"nested": ["s", None, True, 1.5]}}


def test_errors_share_common_base():
    assert issubclass(PathError, RewriteError)
    assert issubclass(PlanError, RewriteError)
    assert issubclass(TruncatedMessage, RewriteError)


def test_non_strict_integer_map_keys_with_map_entry():
    from msgpack import Packer

    packer = Packer()
    data = b"".join(
        [packer.pack_map_header(2), packer.pack(1), packer.pack("a"),
         packer.pack(1), packer.pack("b")]
    )  # {1: "a", 1: "b"}
    out = rewriteb(
        data,
        RewritePlan([Replace((MapEntry(1, 1),), "X")]),
        strict_map_key=False,
    )
    pairs = unpackb(out, strict_map_key=False, object_pairs_hook=list)
    assert pairs == [(1, "a"), (1, "X")]
    with pytest.raises(ValueError):
        rewriteb(data, RewritePlan(), strict_map_key=True)


def test_replacement_uses_provided_packer():
    from msgpack import Packer

    packer = Packer(use_single_float=True)
    plan = RewritePlan([Replace(("a",), 1.0)], packer=packer)
    out = rewriteb(packb({"a": 0}), plan)
    assert b"\xca" in out  # single precision float header


def test_unserializable_replacement_fails_before_io():
    class Weird:
        pass

    with pytest.raises(TypeError):
        Replace(("a",), Weird())


def test_deleting_every_member_emits_empty_container_headers():
    out = rewriteb(
        packb([1, 2, 3]), RewritePlan([Delete((i,)) for i in range(3)])
    )
    assert out == b"\x90"  # empty fixarray
    out_map = rewriteb(
        packb({"a": 1, "b": 2}), RewritePlan([Delete(("a",)), Delete(("b",))])
    )
    assert out_map == b"\x80"  # empty fixmap


def test_deeply_nested_message_raises_stack_error():
    depth = 1100
    data = b"\x01"
    for _ in range(depth):
        data = b"\x91" + data
    from msgpack.exceptions import StackError

    with pytest.raises(StackError):
        rewriteb(data, RewritePlan(), window=128)


def test_byte_by_byte_streaming_truncation_aborts_cleanly():
    source = BytesIO(packb({"a": 1}) + packb([1, 2, 3])[:2])

    class OneAtATime:
        def __init__(self, inner):
            self.inner = inner

        def read(self, n):
            return self.inner.read(1)

    destination = BytesIO()
    rewriter = StreamRewriter(
        OneAtATime(source),
        destination,
        RewritePlan([Replace(("a",), 7)]),
        window=1,
    )
    assert rewriter.process_one() is True
    assert destination.getvalue() == packb({"a": 7})
    with pytest.raises(TruncatedMessage):
        rewriter.process_one()
    assert destination.getvalue() == packb({"a": 7})


def test_copy_can_duplicate_whole_document_into_a_field():
    data = packb({"doc": {"keep": [1, 2]}, "slot": None})
    out = rewriteb(data, RewritePlan([Copy((), ("slot",))]))
    result = unpackb(out)
    assert result["slot"] == {"doc": {"keep": [1, 2]}, "slot": None}
    assert result["doc"] == {"keep": [1, 2]}


def test_invalid_window_and_spill_size_rejected():
    with pytest.raises(ValueError):
        rewriteb(packb(1), RewritePlan(), window=0)
    with pytest.raises(ValueError):
        rewriteb(packb(1), RewritePlan(), spill_size=-1)


def test_delete_container_valued_map_pair_and_nested_count_updates():
    data = packb({"keep": 1, "nested": {"x": [1, 2, 3]}, "tail": 2})
    out = rewriteb(data, RewritePlan([Delete(("nested",))]))
    assert unpackb(out) == {"keep": 1, "tail": 2}
    assert b"nested" not in out and out[:1] == b"\x82"  # map count 3 -> 2

    inner = [0] * 20  # array16
    data2 = packb({"outer": [inner, inner], "n": 1})
    plan = RewritePlan(
        [Delete(("outer", 0, 5)), Delete(("outer", 0, 6)), Replace(("n",), 2)]
    )
    out2 = rewriteb(data2, plan)
    result = unpackb(out2)
    assert len(result["outer"][0]) == 18
    assert len(result["outer"][1]) == 20  # sibling untouched
    assert b"\xdc\x00\x12" in out2  # edited array counts 18
    assert out2.count(b"\xdc\x00\x14") == 1  # sibling still counts 20

    # Replacing a container rewrites its whole span; its old header must not
    # also be emitted as a count update event.
    data3 = packb({"a": [1, 2, 3], "b": 9})
    out3 = rewriteb(data3, RewritePlan([Replace(("a",), []), Delete(("b",))]))
    assert out3 == b"\x81\xa1a\x90"
