"""Replayable diagnosis of the half-message checkpoint problem.

Two pause points are built from the same addressable byte stream so that
``Unpacker.tell()`` reports the *same* offset at both of them:

* ``A`` -- a whole business line has just been committed; no bytes are pending.
* ``B`` -- the same committed line is followed by a container header and one
  of three members, so seven bytes of a half message are pending.

Each pause point is replayed against three continuations:

* a legal suffix that completes the pending object (or starts a fresh line),
* the genuine end of the stream (true EOF, no further ``feed``),
* an illegal marker byte (``0xC1`` is never used by MessagePack).

Every observation is timestamped with a monotonically increasing step number
so the report states the *earliest* moment at which the situation can be
determined.  Two consumption styles are checked: the plain iterator/``unpack``
entry point and the manual ``read_array_header`` + per-member
``unpack``/``skip`` style.

Only :mod:`msgpack.fallback` is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import msgpack
from msgpack.exceptions import FormatError, OutOfData
from msgpack.fallback import Unpacker

# 0xC1 is explicitly reserved ("never used") by the MessagePack spec.
INVALID_MARKER = b"\xc1"


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    """All byte strings used by the diagnosis."""

    line1: object
    line2: object
    line1_bytes: bytes
    line2_bytes: bytes
    partial_bytes: bytes
    valid_suffix_for_b: bytes
    invalid_marker: bytes = INVALID_MARKER

    @property
    def pause_a_bytes(self) -> bytes:
        return self.line1_bytes

    @property
    def pause_b_bytes(self) -> bytes:
        return self.line1_bytes + self.partial_bytes

    @property
    def whole_stream(self) -> bytes:
        return self.line1_bytes + self.line2_bytes


def build_fixture() -> Fixture:
    line1 = ["event", 1, {"k": "v"}]
    line2 = ["event", 2, [10, 20, 30]]
    line1_bytes = msgpack.packb(line1)
    line2_bytes = msgpack.packb(line2)

    packer = msgpack.Packer()
    # Half of line2: the 3-element array header plus member #0 only.
    partial_bytes = packer.pack_array_header(3) + msgpack.packb(line2[0])
    # The exact bytes needed to finish that half message.
    valid_suffix_for_b = msgpack.packb(line2[1]) + msgpack.packb(line2[2])

    assert partial_bytes + valid_suffix_for_b == line2_bytes
    return Fixture(
        line1=line1,
        line2=line2,
        line1_bytes=line1_bytes,
        line2_bytes=line2_bytes,
        partial_bytes=partial_bytes,
        valid_suffix_for_b=valid_suffix_for_b,
    )


# ---------------------------------------------------------------------------
# Observation recording
# ---------------------------------------------------------------------------


@dataclass
class Event:
    step: int
    where: str
    detail: str

    def rendered(self) -> str:
        return f"  step {self.step:d} [{self.where}] {self.detail}"


@dataclass
class ReplayResult:
    style: str
    pause: str
    continuation: str
    events: list[Event] = field(default_factory=list)
    earliest_verdict: str = ""
    tell_at_pause: int = 0
    tell_after: int = 0
    pending_at_pause: int = 0

    def log(self, where: str, detail: str) -> None:
        self.events.append(Event(len(self.events) + 1, where, detail))


# ---------------------------------------------------------------------------
# Style 1: feed() + iteration / unpack()
# ---------------------------------------------------------------------------


def _drain_iterator(unpacker: Unpacker, result: ReplayResult) -> list[object]:
    """Drain complete lines, treating StopIteration as a normal pause."""
    lines: list[object] = []
    while True:
        try:
            line = next(unpacker)
        except StopIteration:
            result.log("next()", "iteration ended (StopIteration)")
            return lines
        except FormatError as exc:
            result.log("next()", f"FormatError: {exc}")
            raise
        else:
            result.log("next()", f"yielded complete line {line!r}")
            lines.append(line)


def replay_iterator(pause_bytes: bytes, continuation: bytes, result: ReplayResult) -> None:
    unpacker = Unpacker()
    unpacker.feed(pause_bytes)
    result.log("feed()", f"fed {len(pause_bytes)} bytes")

    committed = _drain_iterator(unpacker, result)
    result.tell_at_pause = unpacker.tell()
    result.pending_at_pause = len(pause_bytes) - unpacker.tell()
    result.log(
        "checkpoint",
        f"tell()={unpacker.tell()} fed={len(pause_bytes)} "
        f"pending={result.pending_at_pause} committed_lines={committed!r}",
    )

    if continuation:
        unpacker.feed(continuation)
        result.log("feed()", f"fed continuation {continuation.hex()} ({len(continuation)} bytes)")
    else:
        result.log("source", "true EOF: source closed, nothing more can be fed")

    try:
        more = _drain_iterator(unpacker, result)
    except FormatError:
        result.tell_after = unpacker.tell()
        result.earliest_verdict = (
            "illegal marker known at the first next() after it is reached "
            "(FormatError); tell() does not advance"
        )
        return

    result.tell_after = unpacker.tell()
    if not continuation:
        if result.pending_at_pause:
            result.earliest_verdict = (
                "truncation knowable only when EOF is observed AND "
                "fed - tell() > 0; the iterator itself raises no error and "
                "silently drops the half message"
            )
        else:
            result.earliest_verdict = "clean end known at the EOF next() (StopIteration, no pending bytes)"
    else:
        result.earliest_verdict = (
            "legal suffix known at the first next(): it returns a complete "
            "line instead of stopping or raising"
        )
    result.log("result", f"additional lines after continuation: {more!r}")


# ---------------------------------------------------------------------------
# Style 2: read_array_header() + per-member unpack()/skip()
# ---------------------------------------------------------------------------


class _ManualSession:
    """Stateful manual reader spanning several feed() calls.

    The open-container state is kept explicitly so that, after an OutOfData
    inside a container, the next feed resumes reading members instead of
    wrongly asking for another array header.
    """

    def __init__(self) -> None:
        self._state: dict | None = None
        self.lines: list[object] = []

    def pump(self, unpacker: Unpacker, result: ReplayResult) -> None:
        while True:
            if self._state is None:
                try:
                    size = unpacker.read_array_header()
                except OutOfData:
                    result.log("read_array_header()", "OutOfData: no new top-level object")
                    return
                except FormatError as exc:
                    result.log("read_array_header()", f"FormatError: {exc}")
                    raise
                result.log("read_array_header()", f"container declares {size} members")
                self._state = {"size": size, "members": []}

            state = self._state
            while len(state["members"]) < state["size"]:
                index = len(state["members"])
                try:
                    if index == 1:
                        # Exercise skip() on one member; it must advance tell()
                        # exactly like unpack() while producing no value.
                        unpacker.skip()
                        result.log(f"member[{index}]", "skip() consumed one member")
                        state["members"].append("<skipped>")
                    else:
                        value = unpacker.unpack()
                        result.log(f"member[{index}]", f"unpack() -> {value!r}")
                        state["members"].append(value)
                except OutOfData:
                    result.log(
                        f"member[{index}]",
                        f"OutOfData after {index}/{state['size']} members; "
                        "container stays open",
                    )
                    return
                except FormatError as exc:
                    result.log(f"member[{index}]", f"FormatError: {exc}")
                    raise

            self.lines.append(state["members"])
            result.log("container", "all members present; line committed")
            self._state = None

    @property
    def open_container(self) -> dict | None:
        if self._state is None:
            return None
        return {
            "size": self._state["size"],
            "members_done": len(self._state["members"]),
        }


def replay_manual(pause_bytes: bytes, continuation: bytes, result: ReplayResult,
                  fixture: Fixture) -> None:
    unpacker = Unpacker()
    unpacker.feed(pause_bytes)
    result.log("feed()", f"fed {len(pause_bytes)} bytes")

    session = _ManualSession()
    session.pump(unpacker, result)
    result.tell_at_pause = unpacker.tell()
    result.pending_at_pause = len(pause_bytes) - unpacker.tell()
    open_container = session.open_container
    if open_container is not None:
        # The container header starts right after the fully committed line 1.
        open_container["header_offset"] = len(fixture.line1_bytes)
    result.log(
        "checkpoint",
        f"tell()={unpacker.tell()} fed={len(pause_bytes)} "
        f"pending={result.pending_at_pause} open_container={open_container} "
        f"committed_lines={session.lines!r}",
    )

    if continuation:
        unpacker.feed(continuation)
        result.log("feed()", f"fed continuation {continuation.hex()} ({len(continuation)} bytes)")
    else:
        result.log("source", "true EOF: source closed, nothing more can be fed")

    try:
        session.pump(unpacker, result)
    except FormatError:
        result.tell_after = unpacker.tell()
        result.earliest_verdict = (
            "illegal marker known when the next member/header read reaches "
            "0xC1 (FormatError); explicit container state proves the offset "
            "was inside a half message"
        )
        return

    result.tell_after = unpacker.tell()
    if not continuation:
        if open_container is not None:
            result.earliest_verdict = (
                "truncation known immediately from the explicit open "
                "container state (members_done < size) at EOF; no reliance "
                "on tell() alone"
            )
        else:
            result.earliest_verdict = "clean end known from OutOfData at the next header read"
    else:
        result.earliest_verdict = (
            "legal suffix known as the remaining member reads succeed and "
            "the container closes"
        )
    result.log(
        "result",
        f"lines finished after continuation: {session.lines!r} "
        f"open_container={session.open_container}",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


CONTINUATIONS = ("legal suffix", "true EOF", "illegal marker")


def run_diagnostic() -> list[ReplayResult]:
    fixture = build_fixture()
    results: list[ReplayResult] = []

    pauses = (
        ("A", fixture.pause_a_bytes, fixture.line2_bytes),
        ("B", fixture.pause_b_bytes, fixture.valid_suffix_for_b),
    )
    for pause_name, pause_bytes, legal_suffix in pauses:
        for continuation_name in CONTINUATIONS:
            if continuation_name == "legal suffix":
                continuation = legal_suffix
            elif continuation_name == "true EOF":
                continuation = b""
            else:
                continuation = fixture.invalid_marker

            for style, runner in (
                ("iterator", replay_iterator),
                ("manual header+skip", replay_manual),
            ):
                result = ReplayResult(
                    style=style,
                    pause=pause_name,
                    continuation=continuation_name,
                )
                if style == "iterator":
                    runner(pause_bytes, continuation, result)
                else:
                    runner(pause_bytes, continuation, result, fixture)
                results.append(result)
    return results


def format_report(results: list[ReplayResult]) -> str:
    fixture = build_fixture()
    out: list[str] = []
    out.append("Replayable checkpoint diagnosis (pure-Python msgpack.fallback)")
    out.append("=" * 72)
    out.append(f"line1 = {fixture.line1!r}")
    out.append(f"line2 = {fixture.line2!r}")
    out.append(f"line1 bytes ({len(fixture.line1_bytes)}): {fixture.line1_bytes.hex()}")
    out.append(f"line2 bytes ({len(fixture.line2_bytes)}): {fixture.line2_bytes.hex()}")
    out.append(f"half line2 ({len(fixture.partial_bytes)}): {fixture.partial_bytes.hex()}")
    out.append("")
    out.append(
        f"Pause A feeds {len(fixture.pause_a_bytes)} bytes; pause B feeds "
        f"{len(fixture.pause_b_bytes)} bytes. Both checkpoints below show the "
        "same iterator tell()."
    )
    out.append("")

    for result in results:
        out.append(
            f"-- pause {result.pause} | {result.style} | {result.continuation} "
            f"(tell@pause={result.tell_at_pause}, pending={result.pending_at_pause})"
        )
        out.extend(event.rendered() for event in result.events)
        out.append(f"  => earliest verdict: {result.earliest_verdict}")
        out.append("")
    return "\n".join(out)


def main() -> None:
    print(format_report(run_diagnostic()))


if __name__ == "__main__":
    main()
