# Replayable checkpoint diagnosis and recovery

Pure-Python tooling (imports `msgpack.fallback` directly) for diagnosing and
fixing the class of bug where a streaming consumer treats the end of the
`Unpacker` iteration as "last complete message" and checkpoints a `tell()`
offset that actually points *inside* a half-read message.

Run everything from the repository root:

```sh
MSGPACK_PUREPYTHON=1 python -m replay.diagnosis       # replayable diagnosis report
MSGPACK_PUREPYTHON=1 python -m replay.demo_recovery   # end-to-end usage demo
MSGPACK_PUREPYTHON=1 python -m pytest replay -q       # tests
```

## The ambiguity the diagnosis exposes

Two pause points are built on one addressable stream and report the
**identical** `tell()`:

- **Pause A** — exactly one whole business line is committed; zero bytes pending.
- **Pause B** — the same committed line plus the header and 1 of 3 members of
  the next line, i.e. seven bytes of a half message.

Each pause point is replayed against a legal suffix, true EOF, and the
reserved illegal marker `0xC1`. Earliest determination:

| Situation | Earliest moment it is knowable |
|---|---|
| Legal suffix | The first `next()`/member read after the suffix reaches the parser returns a complete line instead of stopping. |
| True EOF | Only when the source itself signals EOF **and** `fed_total - tell() > 0`. The plain iterator raises nothing and silently drops the half message. |
| Illegal marker | The first read that reaches `0xC1` raises `FormatError`; `tell()` does not advance past it. |

The manual `read_array_header()` + `unpack()`/`skip()` consumer removes the
ambiguity: it holds explicit open-container state, so a half message at EOF
is known immediately (`members_done < size`) instead of being hidden behind
`StopIteration`.

## Checkpoint contents

Only persisted offsets and the minimum consumption state are required.

- `LineCheckpoint` — `offset` (start of the next message) plus the fetched but
  unconsumed `pending` tail, needed only for `feed()`-style sources that cannot
  re-read. Against a seekable stream the pending bytes are unnecessary.
- `ManualCheckpoint` — for a pause inside a container: the container header
  offset, declared size, processed member count/values, and any pending tail.

Both have JSON-friendly `to_dict()` / `from_dict()` methods.

## Recovering the exact full-read line list

```python
import io
from replay import FeedLineConsumer, ManualLineConsumer, recover_file, full_read

# Reference: what one uninterrupted read would produce.
reference = full_read(io.BytesIO(stream_bytes), read_size=4)

# 1. feed()-style consumer, crash after fetching part of a line.
consumer = FeedLineConsumer()
consumer.push(prefix_with_half_line)
checkpoint = consumer.checkpoint()          # persist offset + pending bytes

# Seekable stream: reopen at the durable offset and read the remainder.
tail = recover_file(io.BytesIO(stream_bytes), checkpoint)
assert consumer.lines + tail == reference

# Non-seekable feed source: replay the persisted pending bytes, then new chunks.
from replay import recover_feed
tail = recover_feed(checkpoint, future_chunks)

# 2. Manual consumer paused inside a container (with optional skip policy).
manual = ManualLineConsumer(skip_indices=(1,))
manual.push(prefix_with_half_container)
manual_cp = manual.checkpoint()             # persists container state
tail = recover_file(io.BytesIO(stream_bytes), manual_cp, skip_indices=(1,))
assert manual.lines + tail == ManualLineConsumer(skip_indices=(1,)).push(stream_bytes)
```

Recovery seeks back to the container header, `skip()`s the already-processed
members, and combines their persisted values with the freshly read tail, so
the reconstructed business line is byte-for-byte what a single full read
returns. A mismatch between the persisted and re-read container size raises
`ValueError` instead of silently resyncing to the wrong offset.
