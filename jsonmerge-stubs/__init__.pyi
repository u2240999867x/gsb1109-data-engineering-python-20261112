# Type stubs for jsonmerge, the offline batch merge engine.
#
# Mirrors the style of __init__.pyi: JSON documents are typed as Any and the
# internal tree nodes (_Node, _Array, _Engine, ...) are not part of the public
# API, so only the documented entry points are declared here.

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from jsonpointer import JsonPointer as JsonPointer

_Operation = dict[str, Any]
_Batch = dict[str, Any]


class JsonMergeException(Exception): ...
class DuplicateBatchError(JsonMergeException): ...
class InvalidBatch(JsonMergeException): ...


class Conflict:
    WRITE: str
    STALE_COPY: str
    MOVED_INTO_REMOVED: str
    TEST_FAILED: str
    MISSING_LOCATION: str
    UNKNOWN_PARENT: str
    CAUSAL_CYCLE: str
    REASONS: frozenset[str]
    reason: str
    operations: list[dict[str, Any]]
    dependents: list[dict[str, Any]]
    def __init__(
        self,
        reason: str,
        operations: Iterable[dict[str, Any]],
        dependents: Iterable[dict[str, Any]] = ...,
    ) -> None: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...
    def to_dict(self) -> dict[str, Any]: ...


class PatchMerger:
    pointer_cls: type[JsonPointer]
    def __init__(
        self, baseline: Any, pointer_cls: type[JsonPointer] = ...
    ) -> None: ...
    def receive(self, batch: _Batch) -> None: ...
    def receive_many(self, batches: Iterable[_Batch]) -> None: ...
    def merge(self) -> PatchMerger: ...
    def absorb(self, other: PatchMerger) -> PatchMerger: ...
    @property
    def document(self) -> Any: ...
    @property
    def conflicts(self) -> list[Conflict]: ...
    @property
    def canonical_patch(self) -> list[_Operation]: ...
    @property
    def batch_order(self) -> list[Any]: ...
    def replay_canonical(self) -> Any: ...


def merge_batches(
    baseline: Any,
    batches: Iterable[_Batch],
    pointer_cls: type[JsonPointer] = ...,
) -> PatchMerger: ...
