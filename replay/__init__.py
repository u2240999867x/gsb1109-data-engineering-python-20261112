"""Replayable checkpoint diagnosis and byte-offset recovery helpers.

The modules in this package import :mod:`msgpack.fallback` directly, so every
behavior below is exercised against the pure-Python entry point regardless of
whether the C extension is installed.
"""

from .recovery import (
    FeedLineConsumer,
    LineCheckpoint,
    ManualCheckpoint,
    ManualLineConsumer,
    full_read,
    recover_feed,
    recover_file,
)

__all__ = [
    "FeedLineConsumer",
    "LineCheckpoint",
    "ManualCheckpoint",
    "ManualLineConsumer",
    "full_read",
    "recover_feed",
    "recover_file",
]
