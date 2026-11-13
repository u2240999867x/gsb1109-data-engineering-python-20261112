import hashlib

from random import Random
from typing import Any, Dict, Generic, Iterable, List, Optional, Tuple, TypeVar, Union

from faker.generator import random as mod_random

T = TypeVar("T")

# A record is either a ``(key, weight)`` pair or a ``(key, value, weight)`` triple.
Record = Union[Tuple[Any, int], Tuple[Any, Any, int]]

_STATE_VERSION = 1


class WeightedSamplingFrame(Generic[T]):
    """A synthetic sampling frame with integer weights, sampled without replacement.

    Records are identified by their business key, not by their display value:
    two records showing the same value but carrying different keys are two
    distinct entries, while several records sharing one business key are merged
    into a single entry whose weight is the sum of the individual weights.
    Drawing a key removes the whole key (and its merged weight) from the frame.

    Weights are arbitrary precision integers and are never converted to floats,
    so a frame holding weights such as ``2**80``, ``1`` and ``1`` still gives
    every remaining key a chance exactly proportional to its remaining weight.

    Progress can be persisted with :meth:`save_state` and picked up later -
    even by another process that receives the same frame in a different order -
    via :meth:`restore` or :meth:`resume`. The resulting key sequence is
    identical to sampling in one go. If the keys or weights of the frame
    changed in the meantime, resuming is refused with a ``ValueError``.
    """

    def __init__(
        self,
        records: Iterable[Record] = (),
        random: Optional[Random] = None,
    ) -> None:
        self.random = random if random is not None else mod_random
        self._weights: Dict[T, int] = {}
        self._values: Dict[T, Any] = {}
        self._drawn: List[T] = []
        self._order: Optional[List[T]] = None
        self._fingerprint: Optional[str] = None
        self.add(records)

    @staticmethod
    def _normalize(record: Record) -> Tuple[Any, Any, int]:
        try:
            length = len(record)  # type: ignore[arg-type]
        except TypeError:
            raise ValueError(f"Invalid sampling frame record: {record!r}") from None
        if length == 2:
            key, weight = record  # type: ignore[misc]
            value = key
        elif length == 3:
            key, value, weight = record  # type: ignore[misc]
        else:
            raise ValueError(f"Invalid sampling frame record: {record!r}")
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise ValueError(f"Sampling frame weights must be integers, got {weight!r}")
        if weight < 0:
            raise ValueError(f"Sampling frame weights must be non-negative, got {weight!r}")
        return key, value, weight

    def add(self, records: Iterable[Record]) -> None:
        """Merge ``records`` into the frame. Not allowed once sampling started."""
        if self._fingerprint is not None:
            raise ValueError("Cannot add records once sampling or bookkeeping has started.")
        for record in records:
            key, value, weight = self._normalize(record)
            if key in self._weights:
                self._weights[key] += weight
            else:
                self._weights[key] = weight
                self._values[key] = value

    def _canonical_order(self) -> List[T]:
        keys = list(self._weights)
        try:
            return sorted(keys)  # type: ignore[type-var]
        except TypeError:
            return sorted(keys, key=lambda key: (type(key).__qualname__, repr(key)))

    def _ensure_started(self) -> None:
        if self._fingerprint is None:
            self._order = self._canonical_order()
            digest = hashlib.sha256()
            for key in self._order:
                digest.update(repr(key).encode("utf-8"))
                digest.update(b"\x00")
                digest.update(repr(self._weights[key]).encode("ascii"))
                digest.update(b"\xff")
            self._fingerprint = digest.hexdigest()

    @property
    def fingerprint(self) -> str:
        """Digest of the merged keys and weights, independent of input order."""
        self._ensure_started()
        assert self._fingerprint is not None
        return self._fingerprint

    @property
    def drawn(self) -> Tuple[T, ...]:
        """Keys drawn so far, in draw order."""
        return tuple(self._drawn)

    @property
    def remaining(self) -> int:
        """Number of keys still in the frame."""
        return len(self._weights)

    def __len__(self) -> int:
        return len(self._weights)

    def __contains__(self, key: T) -> bool:
        return key in self._weights

    def weight_of(self, key: T) -> int:
        """Remaining merged weight of ``key``."""
        return self._weights[key]

    def value_of(self, key: T) -> Any:
        """Display value first seen for ``key``."""
        return self._values[key]

    def draw(self) -> T:
        """Draw one key without replacement, proportionally to remaining weights."""
        self._ensure_started()
        if not self._weights:
            raise ValueError("The sampling frame is exhausted.")
        total = sum(self._weights.values())
        if total <= 0:
            raise ValueError("No positive weight remains in the sampling frame.")
        threshold = self.random.randrange(total)
        assert self._order is not None
        for key in self._order:
            weight = self._weights.get(key)
            if weight is None:
                continue
            threshold -= weight
            if threshold < 0:
                del self._weights[key]
                self._drawn.append(key)
                return key
        raise AssertionError("unreachable: weighted draw fell through")  # pragma: no cover

    def sample(self, k: int) -> List[T]:
        """Draw ``k`` keys without replacement, in draw order."""
        if k < 0:
            raise ValueError("Sample size cannot be negative.")
        if k > len(self._weights):
            raise ValueError("Cannot draw more keys than remain in the sampling frame.")
        return [self.draw() for _ in range(k)]

    def save_state(self) -> Dict[str, Any]:
        """Snapshot progress so sampling can be resumed later, possibly elsewhere."""
        self._ensure_started()
        return {
            "version": _STATE_VERSION,
            "fingerprint": self._fingerprint,
            "drawn": tuple(self._drawn),
            "random_state": self.random.getstate(),
        }

    def restore(self, state: Dict[str, Any]) -> "WeightedSamplingFrame[T]":
        """Continue from ``state``. Refuses if the frame's keys or weights changed."""
        if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
            raise ValueError("Unsupported sampling frame state.")
        if state.get("fingerprint") != self.fingerprint:
            raise ValueError("The sampling frame's keys or weights changed; refusing to resume.")
        if self._drawn:
            raise ValueError("Cannot restore onto a frame that has already drawn keys.")
        drawn = tuple(state["drawn"])
        for key in drawn:
            del self._weights[key]
        self._drawn = list(drawn)
        self.random.setstate(state["random_state"])
        return self

    @classmethod
    def resume(
        cls,
        records: Iterable[Record],
        state: Dict[str, Any],
        random: Optional[Random] = None,
    ) -> "WeightedSamplingFrame[T]":
        """Build a frame from ``records`` (any order) and continue from ``state``."""
        return cls(records, random=random).restore(state)
