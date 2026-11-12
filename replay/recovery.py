"""Checkpointing and recovery from an addressable byte stream.

The goal is to recover, after a crash or a pause, the exact list of business
lines that a single uninterrupted read would have produced, using only:

* the absolute byte offset that was durably committed, and
* the minimum consumption state needed to interpret bytes already fetched
  past that offset.

Two checkpoint granularities are supported:

* :class:`LineCheckpoint` -- persisted only at a *complete line* boundary.
  ``offset`` is where the next message starts.  In ``feed()`` mode a partial
  next line may already have been fetched, so its raw ``pending`` bytes are
  carried alongside the offset.  Against a seekable stream no pending bytes
  are needed because the stream can simply re-read from ``offset``.

* :class:`ManualCheckpoint`` -- used by the manual container-header consumer,
  which may pause *inside* a container.  It records the container header
  offset, the declared member count, how many members were already processed,
  and the processed member values.  Recovery re-reads from the container
  header and skips the processed members, which is why mid-container
  checkpoints need more state than line-boundary checkpoints.

Everything here uses :mod:`msgpack.fallback`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Iterable

from msgpack.exceptions import OutOfData
from msgpack.fallback import Unpacker

LineHandler = Callable[[object], None]


# ---------------------------------------------------------------------------
# Persistable checkpoint state
# ---------------------------------------------------------------------------


@dataclass
class LineCheckpoint:
    """Durable state at a complete-line boundary."""

    offset: int
    pending: bytes = b""

    @property
    def fed_total(self) -> int:
        """Absolute stream position one byte past everything fetched."""
        return self.offset + len(self.pending)

    def to_dict(self) -> dict:
        return {"offset": self.offset, "pending_hex": self.pending.hex()}

    @classmethod
    def from_dict(cls, data: dict) -> "LineCheckpoint":
        return cls(offset=data["offset"], pending=bytes.fromhex(data["pending_hex"]))


@dataclass
class ManualCheckpoint:
    """Durable state for the manual container-header consumer.

    When ``container_offset is None`` the consumer is idle between top-level
    lines and this is equivalent to a line boundary.  Otherwise a container
    is open and recovery must replay it from its header.
    """

    next_offset: int
    container_offset: int | None = None
    size: int = 0
    members_done: int = 0
    members: list = field(default_factory=list)
    pending: bytes = b""

    def to_dict(self) -> dict:
        return {
            "next_offset": self.next_offset,
            "container_offset": self.container_offset,
            "size": self.size,
            "members_done": self.members_done,
            "members": self.members,
            "pending_hex": self.pending.hex(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ManualCheckpoint":
        return cls(
            next_offset=data["next_offset"],
            container_offset=data["container_offset"],
            size=data["size"],
            members_done=data["members_done"],
            members=list(data["members"]),
            pending=bytes.fromhex(data["pending_hex"]),
        )


# ---------------------------------------------------------------------------
# Consumer 1: feed() + iteration, line-granular checkpoints
# ---------------------------------------------------------------------------


class FeedLineConsumer:
    """Drains complete lines from ``feed()`` chunks.

    The full fed byte sequence is mirrored locally so the pending tail can be
    snapshotted without touching the unpacker's private buffer.  A checkpoint
    is handed out only after a whole line has been yielded.
    """

    def __init__(self, on_line: LineHandler | None = None):
        self._unpacker = Unpacker()
        self._on_line = on_line
        self._fed = bytearray()
        self.lines: list[object] = []

    def push(self, chunk: bytes) -> list[object]:
        self._unpacker.feed(chunk)
        self._fed.extend(chunk)
        produced: list[object] = []
        while True:
            try:
                line = next(self._unpacker)
            except StopIteration:
                break
            produced.append(line)
            self.lines.append(line)
            if self._on_line is not None:
                self._on_line(line)
        return produced

    def checkpoint(self) -> LineCheckpoint:
        offset = self._unpacker.tell()
        pending = bytes(self._fed[offset:])
        # The mirror must agree with the unpacker's own accounting.
        assert len(pending) == len(self._fed) - offset
        return LineCheckpoint(offset=offset, pending=pending)


def recover_feed(
    checkpoint: LineCheckpoint,
    future_chunks: Iterable[bytes],
    on_line: LineHandler | None = None,
) -> list[object]:
    """Resume a feed-based consumer from a line checkpoint.

    The persisted pending half-message is fed first; subsequent chunks are
    exactly the bytes the source would deliver after ``checkpoint.fed_total``.
    """
    unpacker = Unpacker()
    if checkpoint.pending:
        unpacker.feed(checkpoint.pending)
    lines: list[object] = []
    for chunk in future_chunks:
        unpacker.feed(chunk)
        while True:
            try:
                line = next(unpacker)
            except StopIteration:
                break
            lines.append(line)
            if on_line is not None:
                on_line(line)
    return lines


# ---------------------------------------------------------------------------
# Consumer 2: read_array_header() + per-member unpack()/skip()
# ---------------------------------------------------------------------------


class ManualLineConsumer:
    """Consumes array lines one member at a time, with explicit state.

    Pausing inside a container is represented explicitly instead of being
    hidden behind an iterator's StopIteration, so EOF on an open container is
    distinguishable from a clean line boundary.
    """

    def __init__(self, on_line: LineHandler | None = None, skip_indices: tuple[int, ...] = ()):
        self._unpacker = Unpacker()
        self._on_line = on_line
        self._skip_indices = set(skip_indices)
        self._fed = bytearray()
        self.lines: list[object] = []
        self._state: dict | None = None

    def push(self, chunk: bytes) -> list[object]:
        self._unpacker.feed(chunk)
        self._fed.extend(chunk)
        produced: list[object] = []
        self._pump(produced)
        return produced

    def _pump(self, produced: list[object]) -> None:
        unpacker = self._unpacker
        while True:
            if self._state is None:
                header_offset = unpacker.tell()
                try:
                    size = unpacker.read_array_header()
                except OutOfData:
                    return
                self._state = {
                    "header_offset": header_offset,
                    "size": size,
                    "members": [],
                }

            state = self._state
            while len(state["members"]) < state["size"]:
                index = len(state["members"])
                try:
                    if index in self._skip_indices:
                        unpacker.skip()
                        state["members"].append("<skipped>")
                    else:
                        state["members"].append(unpacker.unpack())
                except OutOfData:
                    return

            line = state["members"]
            self._state = None
            produced.append(line)
            self.lines.append(line)
            if self._on_line is not None:
                self._on_line(line)

    def checkpoint(self) -> ManualCheckpoint:
        unpacker = self._unpacker
        if self._state is None:
            offset = unpacker.tell()
            return ManualCheckpoint(
                next_offset=offset,
                pending=bytes(self._fed[offset:]),
            )
        state = self._state
        header_offset = state["header_offset"]
        return ManualCheckpoint(
            next_offset=unpacker.tell(),
            container_offset=header_offset,
            size=state["size"],
            members_done=len(state["members"]),
            members=list(state["members"]),
            pending=bytes(self._fed[unpacker.tell():]),
        )


# ---------------------------------------------------------------------------
# Recovery from a seekable, addressable byte stream
# ---------------------------------------------------------------------------


def recover_file(
    stream: BinaryIO,
    checkpoint: LineCheckpoint | ManualCheckpoint,
    read_size: int = 16 * 1024,
    skip_indices: tuple[int, ...] = (),
) -> list[object]:
    """Recover lines by seeking into an addressable stream.

    Returns the lines not yet durably committed.  For an open manual
    container, the already-processed member values stored in the checkpoint
    are combined with the members re-read after skipping, so the assembled
    line is identical to a single full read.

    ``skip_indices`` mirrors the consumer's static skip policy and is applied
    to re-read members by their absolute index, so a consumer that skips
    members gets the same placeholders after recovery as in an uninterrupted
    run.
    """
    skips = set(skip_indices)
    if isinstance(checkpoint, ManualCheckpoint) and checkpoint.container_offset is not None:
        stream.seek(checkpoint.container_offset)
        unpacker = Unpacker(stream, read_size=read_size)
        size = unpacker.read_array_header()
        if size != checkpoint.size:
            raise ValueError(
                f"container size changed under replay: {size} != {checkpoint.size}"
            )
        for index in range(checkpoint.members_done):
            unpacker.skip()
        tail: list[object] = []
        for index in range(checkpoint.members_done, size):
            if index in skips:
                unpacker.skip()
                tail.append("<skipped>")
            else:
                tail.append(unpacker.unpack())
        current_line = checkpoint.members + tail
        return [current_line] + list(unpacker)

    if isinstance(checkpoint, LineCheckpoint):
        offset = checkpoint.offset
    else:
        offset = checkpoint.next_offset
    stream.seek(offset)
    return list(Unpacker(stream, read_size=read_size))


def full_read(stream: BinaryIO, read_size: int = 16 * 1024) -> list[object]:
    """Reference behavior: one uninterrupted read of the whole stream."""
    stream.seek(0)
    return list(Unpacker(stream, read_size=read_size))
