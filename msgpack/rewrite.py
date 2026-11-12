"""Field level rewriting of MessagePack streams (pure Python).

This module rewrites selected fields of MessagePack messages without ever
decoding the whole message into Python objects.  It reuses the pure Python
:class:`msgpack.Packer` and MessagePack type constants and preserves every
byte that is not addressed by a rewrite plan: unselected map pairs, extension
types, floating point widths and the ordering of elements are all copied
verbatim.

A rewrite plan is a collection of :class:`Replace`, :class:`Delete` and
:class:`Copy` instructions addressed by :class:`Path` objects.  Paths locate
nodes by array position and map key; repeated map keys are distinguished by
their zero based occurrence number (see :class:`MapEntry`).

The rewrite is transactional and streaming:

* The input is parsed in a single forward pass while being spooled.  Memory
  used by the parser grows with the nesting depth, the plan size and a fixed
  I/O window; spool data larger than ``spill_size`` is moved to reclaimable
  temporary files.
* A message is only published after every path has been located, all
  conflicts have been checked and all container length updates have been
  computed.  Truncated input or an invalid plan aborts the current message
  without emitting anything; previously published events stay readable.
* Output is spooled as well, so a failure while writing never produces a
  half written event.
* Chunk boundaries of the input do not affect the result, and deleting a
  subtree that was used as a copy source does not change the copied bytes.
"""

import os
import struct
import tempfile

from .exceptions import (
    ExtraData,
    FormatError,
    RewriteError,
    StackError,
)
from .ext import ExtType
from .fallback import (
    DEFAULT_RECURSE_LIMIT,
    TYPE_ARRAY,
    TYPE_BIN,
    TYPE_EXT,
    TYPE_IMMEDIATE,
    TYPE_MAP,
    TYPE_RAW,
    _MSGPACK_HEADERS,
    Packer,
)

__all__ = [
    "Path",
    "MapEntry",
    "Replace",
    "Delete",
    "Copy",
    "RewritePlan",
    "StreamRewriter",
    "rewriteb",
    "rewrite_stream",
    "RewriteError",
    "PathError",
    "PlanError",
    "TruncatedMessage",
]


class PathError(RewriteError):
    """A path cannot be resolved in the message."""


class PlanError(RewriteError):
    """The rewrite plan is inconsistent or conflicting."""


class TruncatedMessage(RewriteError):
    """The input ended in the middle of a MessagePack message."""


class MapEntry(tuple):
    """A map step selecting one occurrence of a (possibly duplicated) key.

    :param key: the map key, matched against the decoded key value.
    :param occurrence: zero based occurrence of *key* inside the map.
    """

    __slots__ = ()

    def __new__(cls, key, occurrence=0):
        if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0:
            raise ValueError("occurrence must be a non-negative int")
        return tuple.__new__(cls, (key, occurrence))

    def __repr__(self):
        return f"MapEntry({self[0]!r}, {self[1]})"

    @property
    def key(self):
        return self[0]

    @property
    def occurrence(self):
        return self[1]


class Path(tuple):
    """A location inside a MessagePack value.

    A path is a sequence of steps.  An ``int`` step addresses an array
    position.  A ``str``/``bytes`` step addresses the first occurrence of a
    map key; use :class:`MapEntry` to select later occurrences of duplicated
    keys.  An empty path addresses the message root.  Paths address values,
    never map keys themselves.
    """

    __slots__ = ()

    def __new__(cls, *steps):
        return tuple.__new__(cls, tuple(_normalize_step(step) for step in steps))


def _normalize_step(step):
    if isinstance(step, MapEntry):
        return step
    if isinstance(step, bool):
        raise TypeError("path steps must be int, str, bytes or MapEntry")
    if isinstance(step, int):
        if step < 0:
            raise ValueError("array positions must be non-negative")
        return step
    if isinstance(step, (str, bytes)):
        return MapEntry(step, 0)
    raise TypeError(f"path steps must be int, str, bytes or MapEntry, not {type(step)!r}")


class _Instruction:
    __slots__ = ("target",)

    def __init__(self, target):
        self.target = target if isinstance(target, Path) else Path(*target)


class Replace(_Instruction):
    """Replace the node at *target* with a freshly packed *value*."""

    __slots__ = ("value", "payload")

    def __init__(self, target, value, *, packer=None):
        super().__init__(target)
        self.value = value
        self.payload = (packer or Packer()).pack(value)


class Delete(_Instruction):
    """Delete the node at *target*.

    Inside a map the whole key/value pair is removed; inside an array the
    element is removed while later elements keep their original ordering.
    """

    __slots__ = ()


class Copy(_Instruction):
    """Copy the raw bytes of the subtree at *source* onto *target*.

    The source subtree is copied byte for byte from the original message; it
    may be located before or after the destination.
    """

    __slots__ = ("source",)

    def __init__(self, source, target):
        super().__init__(target)
        self.source = source if isinstance(source, Path) else Path(*source)


class RewritePlan:
    """A validated, reusable collection of rewrite instructions."""

    def __init__(self, instructions=(), *, packer=None):
        normalized = []
        for ins in instructions:
            if not isinstance(ins, _Instruction):
                raise TypeError("instructions must be Replace, Delete or Copy")
            if packer is not None and isinstance(ins, Replace):
                ins = Replace(ins.target, ins.value, packer=packer)
            normalized.append(ins)
        self._instructions = tuple(normalized)
        self._targets = {}
        self._sources = set()
        for ins in self._instructions:
            if ins.target in self._targets:
                raise PlanError(f"duplicate instruction target: {ins.target!r}")
            self._targets[ins.target] = ins
            if isinstance(ins, Copy):
                self._sources.add(ins.source)
        self._all_paths = set(self._targets) | self._sources
        self._prefixes = _strict_prefixes(self._all_paths)
        self._interesting = self._all_paths | self._prefixes
        self._watch_keys = _watch_keys(self._interesting)

    def __iter__(self):
        return iter(self._instructions)

    def __len__(self):
        return len(self._instructions)


def _strict_prefixes(paths):
    prefixes = set()
    for path in paths:
        for depth in range(len(path)):
            prefixes.add(path[:depth])
    return prefixes


def _watch_keys(interesting_paths):
    """Map frame path -> set of map keys that matter inside that frame."""
    watches = {}
    for path in interesting_paths:
        if not path:
            continue
        parent = path[:-1]
        step = path[-1]
        if isinstance(step, MapEntry):
            watches.setdefault(parent, set()).add(step.key)
    return watches


# Node dictionary slots (plain dicts keep the state machine readable).
_N_START = "start"
_N_HDR_END = "hdr_end"
_N_END = "end"
_N_KIND = "kind"  # None | TYPE_ARRAY | TYPE_MAP
_N_COUNT = "count"
_N_PATH = "path"
_N_KEY_START = "key_start"


class _NeedData(Exception):
    """Internal control signal: more input is required to continue parsing."""


class _Cursor:
    """Rewind free forward cursor over the bytes held by a spool.

    Unlike :class:`msgpack.fallback.Unpacker`, this cursor never rolls back on
    underflow.  It reads directly from the spool's memory window or temp file,
    so a parse suspended mid message resumes at exactly the same byte once new
    data arrives.  Only the fixed ``window`` sized scratch buffer is held in
    memory at any time.
    """

    def __init__(self, spool, window):
        self._spool = spool
        self._window = window
        self.pos = 0

    def _take(self, length):
        end = self.pos + length
        if end > self._spool.size:
            raise _NeedData
        chunk = self._spool.read_span(self.pos, length)
        self.pos = end
        return chunk

    def read_byte(self):
        return self._take(1)[0]

    def read(self, length):
        # Large payloads are streamed window by window; callers that need the
        # value itself (map keys) only ever use small, bounded reads.
        if length <= self._window:
            return self._take(length)
        pieces = []
        remaining = length
        while remaining:
            part = self._take(min(self._window, remaining))
            pieces.append(part)
            remaining -= len(part)
        return b"".join(pieces)

    def skip(self, length):
        end = self.pos + length
        if end > self._spool.size:
            raise _NeedData
        self.pos = end

    def read_header(self):
        """Read one value header.

        Returns ``(type, count, value)``.  ``count`` is the payload length for
        raw/bin/ext or the member count for containers.  Variable length
        payloads are *not* consumed: callers read them (map keys) or skip them
        (everything else), so huge values never materialize in memory.
        """
        b = self.read_byte()
        if b & 0b10000000 == 0:
            return TYPE_IMMEDIATE, 0, b
        if b & 0b11100000 == 0b11100000:
            return TYPE_IMMEDIATE, 0, -1 - (b ^ 0xFF)
        if b & 0b11100000 == 0b10100000:
            return TYPE_RAW, b & 0b00011111, None
        if b & 0b11110000 == 0b10010000:
            return TYPE_ARRAY, b & 0b00001111, None
        if b & 0b11110000 == 0b10000000:
            return TYPE_MAP, b & 0b00001111, None
        if b == 0xC0:
            return TYPE_IMMEDIATE, 0, None
        if b == 0xC2:
            return TYPE_IMMEDIATE, 0, False
        if b == 0xC3:
            return TYPE_IMMEDIATE, 0, True
        if 0xC4 <= b <= 0xC6:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            return typ, self._decode_size(size, fmt), None
        if 0xC7 <= b <= 0xC9:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            length, code = struct.unpack(fmt, self.read(size))
            return typ, length, code
        if 0xCA <= b <= 0xD3:
            size, fmt = _MSGPACK_HEADERS[b]
            data = self.read(size)
            if fmt:
                return TYPE_IMMEDIATE, 0, struct.unpack(fmt, data)[0]
            return TYPE_IMMEDIATE, 0, data[0]
        if 0xD4 <= b <= 0xD8:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            code, payload = struct.unpack(fmt, self.read(size + 1))
            # Fixext payloads are at most 16 bytes; consume them as part of
            # the header and report zero trailing payload bytes.
            return typ, 0, ExtType(code, payload)
        if 0xD9 <= b <= 0xDB:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            return typ, self._decode_size(size, fmt), None
        if 0xDC <= b <= 0xDD:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            return typ, self._decode_size(size, fmt), None
        if 0xDE <= b <= 0xDF:
            size, fmt, typ = _MSGPACK_HEADERS[b]
            return typ, self._decode_size(size, fmt), None
        raise FormatError("Unknown header: 0x%x" % b)

    def _decode_size(self, size, fmt):
        data = self.read(size)
        if fmt:
            return struct.unpack(fmt, data)[0]
        return data[0]

    def skip_payload(self, typ, count):
        """Skip the trailing payload of a raw/bin/ext value."""
        if typ in (TYPE_RAW, TYPE_BIN, TYPE_EXT) and count:
            self.skip(count)

    def skip_node(self):
        """Skip one complete value, including nested containers."""
        typ, count, _value = self.read_header()
        if typ == TYPE_ARRAY:
            for _ in range(count):
                self.skip_node()
        elif typ == TYPE_MAP:
            for _ in range(count):
                self.skip_node()
                self.skip_node()
        else:
            self.skip_payload(typ, count)

    def skip_remaining_container(self, typ, count):
        """Skip the children of a container whose header was already read."""
        if typ == TYPE_ARRAY:
            for _ in range(count):
                self.skip_node()
        elif typ == TYPE_MAP:
            for _ in range(count):
                self.skip_node()
                self.skip_node()


class _Indexer:
    """Single forward pass recording spans of plan relevant nodes.

    Only nodes whose path is part of the plan (or is a container enclosing
    such a path) are recorded, so the recorded state is bounded by the plan
    size and the nesting depth, never by the message size.
    """

    def __init__(self, plan, spool, *, window, strict_map_key=True):
        self._plan = plan
        self._cursor = _Cursor(spool, window)
        self._strict_map_key = strict_map_key
        self.nodes = []
        self._by_path = {}
        self._stack = []
        self._root = None
        self.end_pos = None
        self._depth = 0

    def _record(self, start, hdr_end, kind, count, path, key_start=None):
        node = {
            _N_START: start,
            _N_HDR_END: hdr_end,
            _N_END: None,
            _N_KIND: kind,
            _N_COUNT: count,
            _N_PATH: path,
            _N_KEY_START: key_start,
        }
        node_id = len(self.nodes)
        self.nodes.append(node)
        self._by_path[path] = node_id
        return node_id

    def pump(self):
        """Drive the state machine until the message ends or data runs out.

        Every state machine step is atomic w.r.t. underflow: if a read needs
        more bytes, the cursor rolls back to the step start and the whole step
        is retried once new data has been spooled.  The spool never discards
        bytes, so rolling back an in spool position loses nothing.
        """
        cursor = self._cursor
        while self.end_pos is None:
            mark = cursor.pos
            stack_len = len(self._stack)
            nodes_len = len(self.nodes)
            depth = self._depth
            root = self._root
            try:
                self._step()
            except _NeedData:
                cursor.pos = mark
                del self._stack[stack_len:]
                del self.nodes[nodes_len:]
                self._depth = depth
                self._root = root
                return
            except RecursionError:
                raise StackError("Too nested")

    def _step(self):
        if not self._stack and self._root is None:
            self._read_root()
            return
        frame = self._stack[-1]
        if frame["remaining"] == 0:
            self._close_frame()
            return
        if frame["kind"] == TYPE_MAP and frame["stage"] == "key":
            self._read_map_key(frame)
        else:
            self._read_child(frame)

    def _read_root(self):
        cursor = self._cursor
        start = cursor.pos
        typ, count, _obj = cursor.read_header()
        hdr_end = cursor.pos
        if typ in (TYPE_ARRAY, TYPE_MAP):
            node_id = self._record(start, hdr_end, typ, count, ())
            self._root = node_id
            self._depth = 1
            self._push_frame(node_id, typ, count, ())
        else:
            node_id = self._record(start, hdr_end, None, 0, ())
            self.nodes[node_id][_N_END] = cursor.pos
            self._root = node_id
            self.end_pos = cursor.pos

    def _push_frame(self, node_id, kind, count, path):
        self._stack.append(
            {
                "node": node_id,
                "kind": kind,
                "remaining": count,
                "path": path,
                "stage": "key" if kind == TYPE_MAP else "value",
                "child_index": 0,
                "key_occurrences": {},
                "key_start": None,
                "key_value": None,
                "key_occurrence": 0,
                "watched": self._plan._watch_keys.get(path),
            }
        )

    def _read_map_key(self, frame):
        cursor = self._cursor
        start = cursor.pos
        typ, count, value = cursor.read_header()
        if typ in (TYPE_ARRAY, TYPE_MAP):
            # Container keys are legal msgpack; skip them without decoding a
            # Python key value.  They can never match scalar plan keys.
            cursor.skip_remaining_container(typ, count)
            if self._strict_map_key:
                raise ValueError("complex type is not allowed for map key")
            frame["key_start"] = start
            frame["key_value"] = None
            frame["key_occurrence"] = 0
        else:
            if typ == TYPE_RAW:
                # Match the default unpacker (raw=False): string keys decode
                # as UTF-8 regardless of strict_map_key.
                key_value = bytes(cursor.read(count)).decode("utf-8", "strict")
            elif typ == TYPE_BIN:
                key_value = bytes(cursor.read(count))
            elif typ == TYPE_EXT:
                key_value = ExtType(value, cursor.read(count)) if count else value
            else:
                key_value = value
            if self._strict_map_key and not isinstance(key_value, (str, bytes)):
                raise ValueError(f"{type(key_value)} is not allowed for map key")
            frame["key_start"] = start
            frame["key_value"] = key_value
            watched = frame["watched"]
            if watched is not None and key_value in watched:
                occurrences = frame["key_occurrences"]
                occ = occurrences.get(key_value, 0)
                occurrences[key_value] = occ + 1
                frame["key_occurrence"] = occ
            else:
                frame["key_occurrence"] = 0
        frame["stage"] = "value"

    def _read_child(self, frame):
        cursor = self._cursor
        is_array = frame["kind"] == TYPE_ARRAY
        if is_array:
            step = frame["child_index"]
            key_start = None
        else:
            step = MapEntry(frame["key_value"], frame["key_occurrence"])
            key_start = frame["key_start"]
        child_path = frame["path"] + (step,)
        start = cursor.pos
        typ, count, _obj = cursor.read_header()
        hdr_end = cursor.pos
        if typ in (TYPE_ARRAY, TYPE_MAP):
            if child_path in self._plan._interesting:
                pair_key_start = (
                    key_start
                    if child_path in self._plan._targets and key_start is not None
                    else None
                )
                node_id = self._record(
                    start, hdr_end, typ, count, child_path, pair_key_start
                )
                self._depth += 1
                if self._depth > DEFAULT_RECURSE_LIMIT:
                    raise StackError("Too nested")
                self._push_frame(node_id, typ, count, child_path)
                self._commit_child(frame, step + 1 if is_array else None)
                return
            self._skip_container(typ, count)
            self._commit_child(frame, step + 1 if is_array else None)
            return
        if child_path in self._plan._interesting:
            pair_key_start = (
                key_start
                if child_path in self._plan._targets and key_start is not None
                else None
            )
            node_id = self._record(
                start, hdr_end, None, 0, child_path, pair_key_start
            )
        cursor.skip_payload(typ, count)
        if child_path in self._plan._interesting:
            self.nodes[node_id][_N_END] = cursor.pos
        self._commit_child(frame, step + 1 if is_array else None)

    def _skip_container(self, typ, count):
        cursor = self._cursor
        if typ == TYPE_ARRAY:
            for _ in range(count):
                cursor.skip_node()
        else:
            for _ in range(count):
                cursor.skip_node()
                cursor.skip_node()

    def _commit_child(self, frame, next_index):
        frame["remaining"] -= 1
        if frame["kind"] == TYPE_MAP:
            frame["stage"] = "key"
        elif next_index is not None:
            frame["child_index"] = next_index

    def _close_frame(self):
        frame = self._stack.pop()
        self._depth -= 1
        end = self._cursor.pos
        self.nodes[frame["node"]][_N_END] = end
        if not self._stack:
            self.end_pos = end

    def resolve(self):
        if self.end_pos is None:
            raise TruncatedMessage("message is incomplete")
        missing = [p for p in self._plan._all_paths if p not in self._by_path]
        if missing:
            raise PathError(f"path(s) not found in message: {missing!r}")
        return self._root, dict(self._by_path)


class _Spool:
    """Binary buffer kept in memory until it exceeds ``spill_size``.

    Once the threshold is crossed the contents are moved to a temporary file
    which is deleted by :meth:`discard`.  All spans use absolute offsets.
    """

    def __init__(self, *, spill_size, tmpdir, prefix):
        self._mem = bytearray()
        self._file = None
        self._path = None
        self._spill_size = spill_size
        self._tmpdir = tmpdir
        self._prefix = prefix
        self.size = 0

    def append(self, data):
        if not data:
            return
        self.size += len(data)
        if self._file is not None:
            # Reads move the file position; always append at the true end.
            self._file.seek(0, os.SEEK_END)
            self._file.write(data)
            return
        self._mem.extend(data)
        if len(self._mem) > self._spill_size:
            fd, self._path = tempfile.mkstemp(prefix=self._prefix, dir=self._tmpdir)
            self._file = os.fdopen(fd, "w+b")
            self._file.write(self._mem)
            self._mem.clear()

    def read_span(self, offset, length):
        if self._file is not None:
            self._file.seek(offset)
            return self._file.read(length)
        return bytes(self._mem[offset : offset + length])

    def copy_span(self, offset, length, sink, window):
        """Stream ``[offset, offset+length)`` into ``sink.append`` in windows."""
        while length > 0:
            chunk_size = min(window, length)
            sink.append(self.read_span(offset, chunk_size))
            offset += chunk_size
            length -= chunk_size

    def replay(self, sink, window):
        self.copy_span(0, self.size, sink, window)

    def flush(self):
        if self._file is not None:
            self._file.flush()

    def getvalue(self):
        if self._file is not None:
            self._file.seek(0)
            return self._file.read()
        return bytes(self._mem)

    def discard(self):
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None
                if self._path is not None:
                    try:
                        os.unlink(self._path)
                    except OSError:
                        pass
                    self._path = None
        self._mem.clear()
        self.size = 0

    close = discard


def _array_header(n):
    if n <= 0x0F:
        return struct.pack("B", 0x90 + n)
    if n <= 0xFFFF:
        return struct.pack(">BH", 0xDC, n)
    if n <= 0xFFFFFFFF:
        return struct.pack(">BI", 0xDD, n)
    raise PlanError(f"array size {n} exceeds MessagePack limits")


def _map_header(n):
    if n <= 0x0F:
        return struct.pack("B", 0x80 + n)
    if n <= 0xFFFF:
        return struct.pack(">BH", 0xDE, n)
    if n <= 0xFFFFFFFF:
        return struct.pack(">BI", 0xDF, n)
    raise PlanError(f"map size {n} exceeds MessagePack limits")


class _Validator:
    """Checks conflicts and computes container count deltas."""

    def __init__(self, plan, nodes, path_to_node):
        self.plan = plan
        self.nodes = nodes
        self.path_to_node = path_to_node
        self.target_node = {
            path_to_node[path]: ins for path, ins in plan._targets.items()
        }
        self.deltas = {}

    def run(self):
        for path, ins in self.plan._targets.items():
            self._check_ancestors(self.path_to_node[path], ins, path, writing=True)
        for ins in self.plan._instructions:
            if isinstance(ins, Copy):
                src_id = self.path_to_node[ins.source]
                self._check_ancestors(src_id, ins, ins.source, writing=False)
                self._check_copy_descendants(self.path_to_node[ins.target], ins)
        for ins in self.plan._instructions:
            if isinstance(ins, Delete):
                self._apply_delete(self.path_to_node[ins.target])
        nodes = self.nodes
        for node_id, delta in self.deltas.items():
            new_count = nodes[node_id][_N_COUNT] + delta
            if not 0 <= new_count <= 0xFFFFFFFF:
                raise PlanError(
                    f"container at {nodes[node_id][_N_PATH]!r} would have {new_count} members"
                )

    def _ancestors(self, node_id):
        node = self.nodes[node_id]
        start = node[_N_START]
        end = node[_N_END]
        chain = []
        for other_id, other in enumerate(self.nodes):
            if other_id == node_id or other[_N_KIND] is None:
                continue
            if other[_N_START] < start and other[_N_END] >= end:
                chain.append(other_id)
        chain.sort(key=lambda nid: self.nodes[nid][_N_START])
        return chain

    def _check_ancestors(self, node_id, ins, path, writing):
        for ancestor_id in self._ancestors(node_id):
            ancestor_ins = self.target_node.get(ancestor_id)
            if isinstance(ancestor_ins, (Replace, Delete)):
                role = "target" if writing else "copy source"
                raise PlanError(
                    f"{role} path {path!r} runs through a replaced or deleted ancestor "
                    f"{self.nodes[ancestor_id][_N_PATH]!r}"
                )

    def _check_copy_descendants(self, dst_node_id, ins):
        dst = self.nodes[dst_node_id]
        start = dst[_N_START]
        end = dst[_N_END]
        for other_id, other_ins in self.target_node.items():
            if other_id == dst_node_id:
                continue
            other = self.nodes[other_id]
            if other[_N_START] > start and other[_N_END] <= end:
                raise PlanError(
                    f"copy target {ins.target!r} contains another instruction target "
                    f"{other[_N_PATH]!r}; subtrees overwritten by Copy cannot be addressed"
                )

    def _apply_delete(self, node_id):
        chain = self._ancestors(node_id)
        if not chain:
            return  # deleting the root message
        parent_id = chain[-1]
        self.deltas[parent_id] = self.deltas.get(parent_id, 0) - 1

    def header_events(self):
        events = []
        for node_id, delta in self.deltas.items():
            if delta == 0 or node_id in self.target_node:
                continue
            node = self.nodes[node_id]
            count = node[_N_COUNT] + delta
            header = _array_header(count) if node[_N_KIND] == TYPE_ARRAY else _map_header(count)
            events.append((node[_N_START], node[_N_HDR_END], header))
        return events


class _RewriteEngine:
    def __init__(self, plan, *, window, spill_size, tmpdir, strict_map_key):
        if not isinstance(window, int) or window <= 0:
            raise ValueError("window must be a positive int")
        if not isinstance(spill_size, int) or spill_size < 0:
            raise ValueError("spill_size must be a non-negative int")
        self.plan = plan
        self.window = window
        self._spill_size = spill_size
        self._tmpdir = tmpdir
        self._in_spool = self._new_spool("msgin-")
        self._indexer = _Indexer(
            plan, self._in_spool, window=window, strict_map_key=strict_map_key
        )
        self._out_spool = None

    def _new_spool(self, prefix):
        return _Spool(
            spill_size=self._spill_size, tmpdir=self._tmpdir, prefix=prefix
        )

    def feed(self, data):
        self._in_spool.append(data)
        self._indexer.pump()

    @property
    def end_pos(self):
        return self._indexer.end_pos

    @property
    def fed_size(self):
        return self._in_spool.size

    def tail_after_message(self):
        end_pos = self._indexer.end_pos
        if end_pos is None or self._in_spool.size <= end_pos:
            return b""
        return self._in_spool.read_span(end_pos, self._in_spool.size - end_pos)

    def finish(self, *, allow_extra):
        indexer = self._indexer
        if indexer.end_pos is None:
            raise TruncatedMessage("message is incomplete")
        if not allow_extra and indexer.end_pos < self._in_spool.size:
            extra = self._in_spool.read_span(
                indexer.end_pos, self._in_spool.size - indexer.end_pos
            )
            raise ExtraData(None, extra)
        root_id, path_to_node = indexer.resolve()
        validator = _Validator(self.plan, indexer.nodes, path_to_node)
        validator.run()
        out = self._new_spool("msgout-")
        self._out_spool = out
        try:
            self._emit(indexer, validator, path_to_node, out)
        except BaseException:
            out.discard()
            raise
        return out

    def _emit(self, indexer, validator, path_to_node, out):
        nodes = indexer.nodes
        events = []
        for path, ins in self.plan._targets.items():
            node = nodes[path_to_node[path]]
            start = node[_N_START]
            end = node[_N_END]
            if isinstance(ins, Delete):
                if node[_N_KEY_START] is not None:
                    start = node[_N_KEY_START]
                events.append((start, 0, end, None))
            elif isinstance(ins, Replace):
                events.append((start, 1, end, ins.payload))
            else:
                src = nodes[path_to_node[ins.source]]
                events.append((start, 2, end, (src[_N_START], src[_N_END])))
        events.extend(
            (h_start, 3, h_end, header)
            for h_start, h_end, header in validator.header_events()
        )
        events.sort(key=lambda event: event[0])
        self._check_disjoint(events)

        position = 0
        window = self.window
        in_spool = self._in_spool
        for start, kind, end, payload in events:
            if start > position:
                in_spool.copy_span(position, start - position, out, window)
            if kind == 1:
                out.append(payload)
            elif kind == 2:
                src_start, src_end = payload
                in_spool.copy_span(src_start, src_end - src_start, out, window)
            # kind 0 (delete) and the header part of kind 3 emit nothing here.
            if kind == 3:
                out.append(payload)
            position = end
        root_end = nodes[indexer._root][_N_END]
        if position < root_end:
            in_spool.copy_span(position, root_end - position, out, window)

    @staticmethod
    def _check_disjoint(events):
        previous_end = 0
        for start, _kind, end, _payload in events:
            if start < previous_end:
                raise PlanError("internal rewrite conflict: overlapping events")
            previous_end = end

    def discard(self):
        self._in_spool.discard()
        if self._out_spool is not None:
            self._out_spool.discard()
            self._out_spool = None


def rewriteb(
    data,
    plan,
    *,
    window=65536,
    spill_size=16 * 1024 * 1024,
    tmpdir=None,
    strict_map_key=True,
):
    """Rewrite one complete MessagePack message and return the new bytes.

    Trailing data raises :class:`msgpack.ExtraData`; truncated input raises
    :class:`TruncatedMessage`; plan problems raise :class:`PathError` or
    :class:`PlanError`.  On any error *data* is left untouched.
    """
    if not isinstance(plan, RewritePlan):
        plan = RewritePlan(plan)
    engine = _RewriteEngine(
        plan,
        window=window,
        spill_size=spill_size,
        tmpdir=tmpdir,
        strict_map_key=strict_map_key,
    )
    try:
        engine.feed(bytes(data))
        out = engine.finish(allow_extra=False)
        return out.getvalue()
    finally:
        engine.discard()


class _SinkWriter:
    """Adapt a ``write()`` stream to the spool sink interface."""

    __slots__ = ("_write",)

    def __init__(self, destination):
        self._write = destination.write

    def append(self, data):
        self._write(data)


class StreamRewriter:
    """Rewrite a stream of MessagePack messages transactionally.

    :param source: readable binary file-like object (``read(n)``).
    :param destination: writable binary file-like object (``write(data)``).
    :param plan: a :class:`RewritePlan` or iterable of instructions.
    :param window: fixed I/O window used for every spool transfer and read.
    :param spill_size: byte threshold after which spools move to temp files.
    :param tmpdir: directory for reclaimable temporary files (default: OS temp).

    A message is written to *destination* only after it has been fully
    validated; a truncated or invalid message aborts without touching
    *destination*, while earlier messages remain readable.
    """

    def __init__(
        self,
        source,
        destination,
        plan,
        *,
        window=65536,
        spill_size=16 * 1024 * 1024,
        tmpdir=None,
        strict_map_key=True,
    ):
        if not callable(source.read) or not callable(destination.write):
            raise TypeError("source needs read(n) and destination needs write(data)")
        self._source = source
        self._destination = destination
        self.plan = plan if isinstance(plan, RewritePlan) else RewritePlan(plan)
        self.window = window
        self.spill_size = spill_size
        self.tmpdir = tmpdir
        self.strict_map_key = strict_map_key
        self._tail = b""
        self._at_eof = False
        self.messages_published = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True

    def _read_more(self):
        if self._at_eof:
            return b""
        chunk = self._source.read(self.window)
        if not chunk:
            self._at_eof = True
            return b""
        return chunk

    def process_one(self):
        """Rewrite the next message. Return ``False`` at clean end of stream."""
        if self.closed:
            raise RewriteError("rewriter is closed")
        engine = _RewriteEngine(
            self.plan,
            window=self.window,
            spill_size=self.spill_size,
            tmpdir=self.tmpdir,
            strict_map_key=self.strict_map_key,
        )
        try:
            if self._tail:
                engine.feed(self._tail)
                self._tail = b""
            while engine.end_pos is None:
                chunk = self._read_more()
                if not chunk:
                    if engine.fed_size == 0:
                        return False
                    raise TruncatedMessage("stream ended inside a message")
                engine.feed(chunk)
            # The message boundary is known, so bytes beyond it (already read
            # from the source) can be retained for resync even if validation
            # of this particular message fails below.
            tail = engine.tail_after_message()
            try:
                out = engine.finish(allow_extra=True)
            except Exception:
                self._tail = tail
                raise
            self._tail = tail
            out.flush()
            out.replay(_SinkWriter(self._destination), self.window)
            self.messages_published += 1
            return True
        finally:
            engine.discard()

    def run(self):
        """Process messages until end of stream; return the published count."""
        while self.process_one():
            pass
        return self.messages_published


def rewrite_stream(
    source,
    destination,
    plan,
    *,
    window=65536,
    spill_size=16 * 1024 * 1024,
    tmpdir=None,
    strict_map_key=True,
):
    """Rewrite every message from *source* into *destination*; return count."""
    with StreamRewriter(
        source,
        destination,
        plan,
        window=window,
        spill_size=spill_size,
        tmpdir=tmpdir,
        strict_map_key=strict_map_key,
    ) as rewriter:
        return rewriter.run()
