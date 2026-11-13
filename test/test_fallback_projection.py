"""Projection regression for the fallback streaming unpacker.

A consumer that reads only a few map columns calls ``read_map_header``,
``unpack`` selected keys, ``skip`` the rest, and uses ``read_array_header`` /
``read_map_header`` for nested containers.

The test builds such a projection plan from an independent wire parse.  Each
planned action carries its row, field, and exact byte span, and a separate
container stack verifies that every member is consumed from the innermost
container opened for that row and field.  A decoder that lands on the right
top-level ``tell()`` but attributes a nested sibling to the wrong column is
therefore caught.

The stream repeats keys and byte-identical values, so order and declared frame
lengths -- not key text or value equality -- determine position.

Every value is also fed only half-way and retried.  The retry must resume where
the failing action started and finish exactly at its span end.

Negative controls pin the two failure modes: equal-valued siblings must still
be distinguished by container identity, and a span that runs off the declared
end of a row must be rejected even when the bytes parse as a valid value.
"""

import pytest

from msgpack.exceptions import OutOfData
from msgpack.fallback import Packer, Unpacker


# --- independent wire parser -------------------------------------------------


def parse(data, i=0):
    """Return (value, end, node).

    ``node`` is ``("leaf", start, end, value)``, ``("array", start,
    payload_start, end, [nodes])``, or ``("map", start, payload_start, end,
    [(key_node, value_node), ...])``.
    """
    b = data[i]
    if b <= 0x7F:
        return b, i + 1, ("leaf", i, i + 1, b)
    if b >= 0xE0:
        return b - 0x100, i + 1, ("leaf", i, i + 1, b - 0x100)
    if b == 0xC0:
        return None, i + 1, ("leaf", i, i + 1, None)
    if b == 0xC2:
        return False, i + 1, ("leaf", i, i + 1, False)
    if b == 0xC3:
        return True, i + 1, ("leaf", i, i + 1, True)
    if 0xA0 <= b <= 0xBF:
        n = b & 0x1F
        raw = data[i + 1:i + 1 + n]
        text = raw.decode()
        return text, i + 1 + n, ("leaf", i, i + 1 + n, text)
    if 0x90 <= b <= 0x9F or b in (0xDC, 0xDD):
        if b in (0xDC, 0xDD):
            size = 2 if b == 0xDC else 4
            count = int.from_bytes(data[i + 1:i + 1 + size], "big")
            start = i + 1 + size
        else:
            count, start = b & 0x0F, i + 1
        values = []
        nodes = []
        j = start
        for _ in range(count):
            value, j, node = parse(data, j)
            values.append(value)
            nodes.append(node)
        return ("array", values), j, ("array", i, start, j, nodes)
    if 0x80 <= b <= 0x8F or b in (0xDE, 0xDF):
        if b in (0xDE, 0xDF):
            size = 2 if b == 0xDE else 4
            count = int.from_bytes(data[i + 1:i + 1 + size], "big")
            start = i + 1 + size
        else:
            count, start = b & 0x0F, i + 1
        pairs = []
        nodes = []
        j = start
        for _ in range(count):
            key, j, key_node = parse(data, j)
            value, j, value_node = parse(data, j)
            pairs.append((key, value))
            nodes.append((key_node, value_node))
        return ("map", pairs), j, ("map", i, start, j, nodes)
    raise AssertionError("unparsed byte 0x%02x at %d" % (b, i))


# --- rows and projection plan ------------------------------------------------


ROWS = [
    # Duplicate field names; "tags" appears twice with byte-identical lists.
    [
        ("id", 1),
        ("tags", ["a", "b"]),
        ("tags", ["a", "b"]),
        ("meta", {"x": [1, 2], "y": [1, 2]}),
        ("meta", {"x": [3], "y": [3]}),
        ("tail", 7),
    ],
    # Same shape with different lengths and values.
    [
        ("id", 2),
        ("tags", []),
        ("tags", ["a"]),
        ("meta", {"x": [], "y": [9]}),
        ("meta", {"x": [9], "y": [9]}),
        ("tail", 7),
    ],
    # Repeated element bytes in one list and an empty nested map.
    [
        ("id", 3),
        ("tags", ["a", "a", "a"]),
        ("tags", ["b"]),
        ("meta", {"x": [1], "y": {}}),
        ("meta", {"x": [], "y": []}),
        ("tail", 9),
    ],
]


class Action:
    """One caller-visible step and its owning row, field, and byte span."""

    __slots__ = ("kind", "row", "field", "parent", "start", "end", "expected", "container")

    def __init__(self, kind, row, field, parent, start, end, expected, container=False):
        self.kind = kind
        self.row = row
        self.field = field
        self.parent = parent
        self.start = start
        self.end = end
        self.expected = expected
        self.container = container

    def __repr__(self):
        return "<%s row=%r field=%r [%d,%d)>" % (
            self.kind,
            self.row,
            self.field,
            self.start,
            self.end,
        )


def plan_row(row_index, node, base):
    """Projection plan: walk duplicate "tags" fields; skip other odd fields."""
    actions = []
    kind, start, payload_start, _end, pairs = node
    assert kind == "map"
    root = Action(
        "rmh", row_index, None, None, base + start, base + payload_start,
        len(pairs), container=True,
    )
    actions.append(root)

    for ordinal, (key_node, value_node) in enumerate(pairs):
        key = key_node[3]
        key_start, key_end = key_node[1], key_node[2]
        field = (key, ordinal)
        actions.append(
            Action("unpack", row_index, field, root, base + key_start, base + key_end, key)
        )
        value_kind = value_node[0]
        value_start, value_end = value_node[1], value_node[3 if value_kind != "leaf" else 2]
        value_payload = value_node[2] if value_kind != "leaf" else None

        # Only duplicate "tags" fields are walked; every other odd field is
        # skipped as a whole, the way a narrow projection would do it.
        if ordinal % 2 == 1 and key != "tags":
            actions.append(
                Action("skip", row_index, field, root,
                       base + value_start, base + value_end, None)
            )
            continue

        if value_kind == "leaf":
            actions.append(
                Action("unpack", row_index, field, root,
                       base + value_start, base + value_end, value_node[3])
            )
            continue

        header = Action(
            "rah" if value_kind == "array" else "rmh",
            row_index,
            field,
            root,
            base + value_start,
            base + value_payload,
            len(value_node[4]),
            container=True,
        )
        actions.append(header)

        if value_kind == "array":
            for element_index, element in enumerate(value_node[4]):
                actions.append(
                    Action(
                        "unpack",
                        row_index,
                        (key, ordinal, "elem", element_index),
                        header,
                        base + element[1],
                        base + element[2],
                        element[3],
                    )
                )
            continue

        for pair_index, (sub_key_node, sub_value_node) in enumerate(value_node[4]):
            sub_key = sub_key_node[3]
            sub_key_start, sub_key_end = sub_key_node[1], sub_key_node[2]
            actions.append(
                Action(
                    "unpack",
                    row_index,
                    (key, ordinal, "subkey", pair_index),
                    header,
                    base + sub_key_start,
                    base + sub_key_end,
                    sub_key,
                )
            )
            sub_kind = sub_value_node[0]
            sub_start, sub_end = sub_value_node[1], sub_value_node[3 if sub_kind != "leaf" else 2]
            sub_payload = sub_value_node[2] if sub_kind != "leaf" else None
            if pair_index % 2 == 0 and sub_kind == "array":
                sub = Action(
                    "rah", row_index, (key, ordinal, "sublist", pair_index),
                    header, base + sub_start, base + sub_payload,
                    len(sub_value_node[4]), container=True,
                )
                actions.append(sub)
                for sub_index, element in enumerate(sub_value_node[4]):
                    actions.append(
                        Action(
                            "unpack",
                            row_index,
                            (key, ordinal, "subelem", pair_index, sub_index),
                            sub,
                            base + element[1],
                            base + element[2],
                            element[3],
                        )
                    )
            else:
                actions.append(
                    Action("skip", row_index, (key, ordinal, "subvalue", pair_index),
                           header, base + sub_start, base + sub_end, None)
                )
    return actions


def build():
    packer = Packer()
    frames = [packer.pack_map_pairs(row) for row in ROWS]
    starts = []
    offset = 0
    for frame in frames:
        starts.append(offset)
        offset += len(frame)
    actions = []
    for row_index, (frame, start) in enumerate(zip(frames, starts)):
        _, end, node = parse(frame, 0)
        assert end == len(frame)
        actions.extend(plan_row(row_index, node, start))
    return b"".join(frames), frames, starts, actions


STREAM, FRAMES, FRAME_STARTS, ACTIONS = build()


def call(unpacker, kind):
    if kind == "rmh":
        return unpacker.read_map_header()
    if kind == "rah":
        return unpacker.read_array_header()
    if kind == "unpack":
        return unpacker.unpack()
    if kind == "skip":
        return unpacker.skip()
    raise AssertionError(kind)


# --- independent container stack ---------------------------------------------


class ContainerStack:
    """Owns open containers and rejects a member from any other container."""

    def __init__(self):
        self.frames = []

    def apply(self, action):
        if action.container:
            if action.parent is not None:
                self.consume(action)
            remaining = action.expected * (2 if action.kind == "rmh" else 1)
            if remaining:
                self.frames.append([action, remaining])
            return
        self.consume(action)

    def consume(self, action):
        if not self.frames:
            raise AssertionError("%r outside any container" % (action,))
        top, remaining = self.frames[-1]
        if top is not action.parent:
            raise AssertionError(
                "%r attributed to %r, innermost container is %r"
                % (action, _owner(action), _owner(top))
            )
        remaining -= 1
        if remaining == 0:
            self.frames.pop()
        else:
            self.frames[-1][1] = remaining

    def finish(self):
        if self.frames:
            raise AssertionError("unclosed containers: %r" % ([f[0] for f in self.frames],))


def _owner(action):
    return (action.row, action.field)


def walk(actions):
    stack = ContainerStack()
    for action in actions:
        stack.apply(action)
    stack.finish()


# --- projection checks -------------------------------------------------------


def test_spans_form_contiguous_per_row_partitions():
    for action in ACTIONS:
        assert FRAME_STARTS[action.row] <= action.start < action.end
    walk(ACTIONS)
    for row_index, frame in enumerate(FRAMES):
        cursor = FRAME_STARTS[row_index]
        for action in (a for a in ACTIONS if a.row == row_index):
            assert action.start == cursor, (action, "expected", cursor)
            cursor = action.end
        assert cursor == FRAME_STARTS[row_index] + len(frame)


def test_projection_matches_values_and_offsets():
    unpacker = Unpacker()
    unpacker.feed(STREAM)
    for index, action in enumerate(ACTIONS):
        before = unpacker.tell()
        result = call(unpacker, action.kind)
        assert before == action.start, (index, action, before)
        assert result == action.expected, (index, action, result)
        assert unpacker.tell() == action.end, (index, action, unpacker.tell())
    assert unpacker.tell() == len(STREAM)


@pytest.mark.parametrize("row_index", range(len(FRAMES)))
@pytest.mark.parametrize("cut", range(max(map(len, FRAMES)) + 1))
def test_row_resumes_with_only_its_declared_frame(row_index, cut):
    """Never append the next row, so a next-frame header cannot be consumed."""
    frame = FRAMES[row_index]
    if cut > len(frame):
        pytest.skip("cut longer than this row")
    base = FRAME_STARTS[row_index]
    unpacker = Unpacker()
    unpacker.feed(frame[:cut])
    for action in (a for a in ACTIONS if a.row == row_index):
        try:
            result = call(unpacker, action.kind)
        except OutOfData:
            if cut == len(frame):
                pytest.fail(
                    "complete declared row %d raised OutOfData at %r" % (row_index, action)
                )
            unpacker.feed(frame[cut:])
            cut = len(frame)
            result = call(unpacker, action.kind)
        assert result == action.expected, (action, result)
        assert action.end - base <= len(frame)
        if cut == len(frame):
            assert unpacker.tell() == action.end - base, (action, unpacker.tell())
    assert cut == len(frame)
    assert unpacker.tell() == len(frame)


def safe_resume_boundary(action, tell_before, tell_after_failure, completed):
    """Classify what a failed step left behind.

    Returns the boundary from which the caller may retry.  A failure that
    exposed no partial progress resumes at the action start.  A decoder that
    published a partial result may only resume at the last completed action
    boundary; if it advanced the position without completing anything, no
    boundary is safe and this raises.
    """
    if tell_after_failure == tell_before:
        return tell_before
    if completed:
        return completed[-1]
    raise AssertionError(
        "%r advanced tell from %d to %d without completing an action"
        % (action, tell_before, tell_after_failure)
    )


def test_each_value_retries_from_half_and_resumes_at_its_start():
    for index, action in enumerate(ACTIONS):
        half = action.start + (action.end - action.start) // 2
        assert half < action.end

        unpacker = Unpacker()
        unpacker.feed(STREAM[:half])
        completed = []
        for prefix in ACTIONS[:index]:
            result = call(unpacker, prefix.kind)
            assert result == prefix.expected, (prefix, result)
            completed.append(prefix.end)

        boundary = unpacker.tell()
        assert boundary == action.start, (index, action, boundary)

        with pytest.raises(OutOfData):
            call(unpacker, action.kind)
        resumed = safe_resume_boundary(action, boundary, unpacker.tell(), completed)
        # For this projection a failed value must expose no partial progress.
        assert resumed == boundary, (
            index, action, "safe boundary moved to %d" % resumed
        )

        unpacker.feed(STREAM[half:action.end])
        result = call(unpacker, action.kind)
        assert result == action.expected, (index, action, result)
        assert unpacker.tell() == action.end, (index, action, unpacker.tell())


# --- negative controls -------------------------------------------------------


def test_stack_rejects_sibling_mixup_with_identical_value_and_tell():
    """Two equal sibling lists swap attribution; values and total tell agree."""
    first = next(
        a for a in ACTIONS
        if a.kind == "unpack" and a.row == 0 and a.field == ("tags", 1, "elem", 0)
    )
    second = next(
        a for a in ACTIONS
        if a.kind == "unpack" and a.row == 0 and a.field == ("tags", 2, "elem", 0)
    )
    assert first.expected == second.expected == "a"

    misattributed = Action(
        first.kind, first.row, second.field, second.parent, first.start, first.end,
        first.expected,
    )
    broken = [misattributed if a is first else a for a in ACTIONS]
    with pytest.raises(AssertionError, match="attributed"):
        walk(broken)
    walk(ACTIONS)


def test_declared_frame_length_exposes_truncation_splicing():
    """A truncated row must not borrow the next row's map header."""
    packer = Packer()
    frame = packer.pack_map_pairs([("id", 1), ("tags", ["a", "b"])])
    declared_end = len(frame)
    # Drop the complete "b" element so the next row's 0x82 map header can
    # occupy the missing slot.
    truncated = frame[:-2]
    next_frame = packer.pack_map_pairs([("id", 2), ("tags", ["c"])])
    spans = [(0, declared_end), (declared_end, declared_end + len(next_frame))]

    unpacker = Unpacker()
    unpacker.feed(truncated)
    assert unpacker.read_map_header() == 2
    assert unpacker.unpack() == "id"
    assert unpacker.unpack() == 1
    assert unpacker.unpack() == "tags"
    assert unpacker.read_array_header() == 2
    assert unpacker.unpack() == "a"
    with pytest.raises(OutOfData):
        unpacker.unpack()
    # No partial progress: the retry boundary is right after the last element.
    assert unpacker.tell() == len(truncated)

    missing_start = unpacker.tell()
    unpacker.feed(next_frame)
    spliced = unpacker.unpack()
    spliced_end = unpacker.tell()
    assert spliced == {"id": 2, "tags": ["c"]}
    # The member ran from the truncated first row into the next row's header
    # and body.  With the declared line lengths supplied upstream, no single
    # row contains that span, so the attribution is rejected.
    with pytest.raises(AssertionError, match="crosses a declared frame"):
        row_of_span(missing_start, spliced_end, spans)


def row_of_span(start, end, spans):
    """Map a member span to its declared row, rejecting a frame-crossing span."""
    for index, (row_start, row_end) in enumerate(spans):
        if row_start <= start and end <= row_end:
            return index
    raise AssertionError(
        "member [%d, %d) crosses a declared frame boundary in %r" % (start, end, spans)
    )
