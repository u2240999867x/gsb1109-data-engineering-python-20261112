"""Tests for the fallback Packer's keep_packed_prefix option."""

from io import BytesIO

import pytest

from msgpack import ExtType, Packer, Unpacker, packb

pytestmark = pytest.mark.skipif(
    "msgpack.fallback" not in Packer.__module__,
    reason="keep_packed_prefix is a fallback feature",
)


class Boom(Exception):
    pass


def _unpack_all(data):
    unpacker = Unpacker(BytesIO(data), raw=False)
    return list(unpacker)


def test_legacy_failure_clears_buffer():
    packer = Packer(autoreset=False)
    packer.pack(1)
    with pytest.raises(TypeError):
        packer.pack(object())
    assert packer.bytes() == b""


def test_failure_keeps_committed_prefix_and_can_continue():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    packer.pack(1)
    packer.pack("two")
    prefix = packer.bytes()

    with pytest.raises(TypeError):
        packer.pack(object())

    assert packer.bytes() == prefix
    assert packer._pending is None
    assert packer._packing is False

    packer.pack(3)
    assert _unpack_all(packer.bytes()) == [1, "two", 3]


def test_big_column_then_failing_field_keeps_prefix():
    big = b"x" * (256 * 1024)
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    packer.pack(big)
    prefix = packer.bytes()
    buffer_obj = packer._buffer

    # The large column is packed first; the next message fails midway through
    # a map whose value cannot be serialized.
    with pytest.raises(TypeError):
        packer.pack({"key": object()})

    assert packer.bytes() == prefix
    # The committed batch must not be rebuilt (which would copy its bytes).
    assert packer._buffer is buffer_obj

    packer.pack(b"after")
    assert _unpack_all(packer.bytes()) == [big, b"after"]


def test_map_pairs_partial_failure_rolls_back():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    packer.pack("seed")
    prefix = packer.bytes()

    with pytest.raises(TypeError):
        packer.pack_map_pairs([("a", 1), ("b", object())])

    assert packer.bytes() == prefix
    packer.pack_map_pairs([("only", 2)])
    assert _unpack_all(packer.bytes()) == ["seed", {"only": 2}]


def test_ext_type_rejected_keeps_prefix():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    packer.pack(ExtType(1, b"committed"))
    prefix = packer.bytes()

    with pytest.raises(ValueError):
        packer.pack_ext_type(200, b"ignored")

    assert packer.bytes() == prefix
    packer.pack_ext_type(5, b"ok")
    assert _unpack_all(packer.bytes()) == [ExtType(1, b"committed"), ExtType(5, b"ok")]


def test_large_column_is_staged_by_reference():
    column = b"z" * (64 * 1024)
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    captured = {}

    def default(obj):
        captured["chunks"] = list(packer._pending)
        raise Boom

    packer._default = default
    with pytest.raises(Boom):
        packer.pack([column, object()])

    # The caller-owned column is appended by reference, not copied, and the
    # failed message never reaches the committed buffer.
    assert any(chunk is column for chunk in captured["chunks"])
    assert packer.bytes() == b""


def test_success_path_stages_large_column_by_reference():
    column = b"y" * (128 * 1024)
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    captured = {}

    def default(obj):
        # While the message is still in flight, staging must reference the
        # caller's column rather than duplicating its bytes.
        captured["chunks"] = list(packer._pending)
        return "tail"

    packer._default = default
    packer.pack([column, object()])

    assert any(chunk is column for chunk in captured["chunks"])
    assert _unpack_all(packer.bytes()) == [[column, "tail"]]


def test_failure_does_not_copy_accumulated_batch():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    for value in range(50):
        packer.pack(value)
    prefix = packer.bytes()
    buffer_obj = packer._buffer

    with pytest.raises(TypeError):
        packer.pack(object())

    assert packer.bytes() == prefix
    assert packer._buffer is buffer_obj


def test_reentrant_pack_from_default_is_rejected():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    seen = {}

    def default(obj):
        with pytest.raises(RuntimeError, match="reentrant"):
            packer.pack(999)
        seen["called"] = True
        return "converted"

    packer._default = default
    packer.pack(object())
    assert seen["called"] is True
    assert _unpack_all(packer.bytes()) == ["converted"]


def test_default_raising_leaves_prefix_and_state_clean():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    packer.pack(7)
    prefix = packer.bytes()

    def default(obj):
        raise Boom

    packer._default = default
    with pytest.raises(Boom):
        packer.pack(object())

    assert packer.bytes() == prefix
    assert packer._pending is None
    assert packer._packing is False


def test_success_bytes_match_non_retaining_packer():
    retaining = Packer(autoreset=False, keep_packed_prefix=True)
    plain = Packer(autoreset=False)
    messages = [1, b"bin", "str", [1, 2], {"k": "v"}, ExtType(3, b"x")]
    for message in messages:
        retaining.pack(message)
        plain.pack(message)
    assert retaining.bytes() == plain.bytes()


def test_keep_option_ignored_with_autoreset():
    packer = Packer(autoreset=True, keep_packed_prefix=True)
    assert packer._keep_packed_prefix is False
    assert packer.pack(1) == packb(1)


def test_reset_clears_prefix_and_state():
    packer = Packer(autoreset=False, keep_packed_prefix=True)
    packer.pack(1)
    packer.reset()
    assert packer.bytes() == b""
    assert packer._pending is None
    assert packer._packing is False
