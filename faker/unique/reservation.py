"""Row-level unique value reservation.

Unlike :class:`faker.proxy.UniqueProxy`, which tracks one provider method
at a time, this module reserves *whole rows*: every business key of a row
(primary key and aliases potentially produced by different Faker
providers) is normalized into one shared key space and only occupies the
key space once the complete row is committed.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence, Type

from faker.exceptions import UniqueConflict, UniqueReservationClosed

DEFAULT_RESERVATION_ATTEMPTS = 1000

NormalizedKey = Any
RowValues = dict[str, Any]


def _normalize_numeric(value: Decimal | str) -> tuple[str, str] | None:
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not decimal_value.is_finite():
        return None
    if decimal_value == 0:
        # Canonicalize positive/negative zero and "0.00".
        return "num", "0"
    if decimal_value == decimal_value.to_integral_value():
        return "num", format(decimal_value.to_integral_value(), "f")
    return "num", format(decimal_value.normalize(), "f")


def normalize_business_key(value: Any) -> NormalizedKey:
    """Normalize a raw business key into a hashable canonical form.

    Numeric-looking values are unified across types so that ``7``,
    ``7.0``, ``"7"`` and ``"007"`` all map to the same key, while plain
    strings keep their textual identity. Containers are converted
    recursively so the result is always hashable.
    """

    if value is None:
        return "none", ()
    if isinstance(value, bool):
        # Keep booleans distinct from the integers 0 and 1.
        return "bool", value
    if isinstance(value, Decimal):
        numeric = _normalize_numeric(value)
        if numeric is not None:
            return numeric
        return "decimal", str(value)
    if isinstance(value, int):
        return "num", str(value)
    if isinstance(value, float):
        numeric = _normalize_numeric(Decimal(str(value)))
        if numeric is not None:
            return numeric
        return "float", repr(value)
    if isinstance(value, str):
        text = value.strip()
        numeric = _normalize_numeric(text)
        if numeric is not None:
            return numeric
        return "str", text
    if isinstance(value, bytes):
        try:
            return normalize_business_key(value.decode("utf-8"))
        except UnicodeDecodeError:
            return "bytes", tuple(value)
    if isinstance(value, Mapping):
        return (
            "dict",
            tuple(sorted((str(k), normalize_business_key(v)) for k, v in value.items())),
        )
    if isinstance(value, (list, tuple)):
        return "seq", tuple(normalize_business_key(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return "set", frozenset(normalize_business_key(item) for item in value)
    return type(value).__name__, value


@dataclass(frozen=True)
class Column:
    """Description of one reserved column inside a row.

    :param name: column name, used as the key of the returned row mapping
    :param function: zero/further-argument callable producing raw values
    :param args: positional arguments forwarded to ``function``
    :param kwargs: keyword arguments forwarded to ``function``
    :param key: optional callable deriving the business key from the
        produced value; defaults to :func:`normalize_business_key`
    """

    name: str
    function: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    key: Callable[[Any], NormalizedKey] | None = None

    def generate(self) -> Any:
        return self.function(*self.args, **self.kwargs)

    def normalize(self, value: Any) -> NormalizedKey:
        if self.key is not None:
            return normalize_business_key(self.key(value))
        return normalize_business_key(value)


class UniqueReservation:
    """A pending reservation of one row of business keys."""

    def __init__(
        self,
        manager: UniqueReservationManager,
        columns: Sequence[Column],
        attempts: int,
    ) -> None:
        names = [column.name for column in columns]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"Duplicate column names in reservation: {sorted(duplicates)!r}")
        if attempts < 1:
            raise ValueError("attempts must be a positive integer.")
        self._manager = manager
        self._columns = tuple(columns)
        self._attempts = attempts
        self._owner = object()
        self._held_keys: set[NormalizedKey] = set()
        self._values: RowValues = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def values(self) -> RowValues:
        """A copy of the currently reserved row values."""
        return dict(self._values)

    def reserve(self, **overrides: Any) -> RowValues:
        """Generate and reserve a full row, retrying the whole row on conflict.

        Column values may be supplied through ``overrides`` instead of
        calling the column generator. A conflict on any single column
        discards the attempt and regenerates every column, so values
        never occupy the shared key space before the whole row is ready.
        """

        if self._closed:
            raise UniqueReservationClosed("This reservation has already been committed or released.")
        self._manager.release(self)
        self._values = {}

        names = {column.name for column in self._columns}
        unknown = set(overrides) - names
        if unknown:
            raise TypeError(f"Unexpected column overrides: {sorted(unknown)!r}")

        last_conflicts: list[NormalizedKey] = []
        for _ in range(self._attempts):
            candidate_row = self._build_candidate(overrides)
            row_keys = [column.normalize(candidate_row[column.name]) for column in self._columns]
            intra_conflicts = self._intra_row_conflicts(row_keys)
            if intra_conflicts:
                last_conflicts = intra_conflicts
                continue
            self._held_keys = set(row_keys)
            conflicts = self._manager.claim(self)
            if not conflicts:
                self._values = candidate_row
                return self.values
            self._held_keys.clear()
            last_conflicts = conflicts
        raise UniqueConflict(
            f"Unable to reserve a unique row after {self._attempts:,} attempts; "
            f"conflicting keys: {last_conflicts!r}",
            conflicting_keys=last_conflicts,
        )

    def _intra_row_conflicts(self, row_keys: Sequence[NormalizedKey]) -> list[NormalizedKey]:
        seen: set[NormalizedKey] = set()
        duplicates: list[NormalizedKey] = []
        for key in row_keys:
            if key in seen and key not in duplicates:
                duplicates.append(key)
            seen.add(key)
        return duplicates

    def _build_candidate(self, overrides: Mapping[str, Any]) -> RowValues:
        row: RowValues = {}
        for column in self._columns:
            if column.name in overrides:
                row[column.name] = overrides[column.name]
            else:
                row[column.name] = column.generate()
        return row

    def commit(self) -> RowValues:
        """Promote the reserved keys into the durable key space."""

        if self._closed:
            raise UniqueReservationClosed("This reservation has already been committed or released.")
        if not self._values:
            raise UniqueReservationClosed("Nothing has been reserved yet; call reserve() first.")
        self._manager.commit(self)
        self._closed = True
        return self.values

    def release(self) -> None:
        """Abandon the reservation, returning only this row's pending keys."""

        if self._closed:
            return
        self._manager.release(self)
        self._closed = True

    def __enter__(self) -> UniqueReservation:
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> Literal[False]:
        if exc_type is None:
            if not self._closed:
                self.commit()
        else:
            self.release()
        return False


class UniqueReservationManager:
    """Shared key-space registry for row-level reservations.

    Managers are normally obtained through the named-domain registry so
    that independent generators can interleave reservations against the
    same business key space.
    """

    _domains: dict[str, UniqueReservationManager] = {}
    _domain_lock = threading.RLock()

    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self._lock = threading.RLock()
        self._committed_keys: set[NormalizedKey] = set()
        self._pending_claims: dict[NormalizedKey, set[object]] = {}

    @classmethod
    def domain(cls, name: str) -> UniqueReservationManager:
        """Return the manager registered under ``name``, creating it if needed."""

        with cls._domain_lock:
            manager = cls._domains.get(name)
            if manager is None:
                manager = cls(name=name)
                cls._domains[name] = manager
            return manager

    @classmethod
    def close_domain(cls, name: str) -> None:
        """Remove a named domain from the registry."""

        with cls._domain_lock:
            cls._domains.pop(name, None)

    @classmethod
    def _clear_domains(cls) -> None:
        with cls._domain_lock:
            cls._domains.clear()

    def reserve_row(
        self,
        columns: Sequence[Column],
        attempts: int = DEFAULT_RESERVATION_ATTEMPTS,
    ) -> UniqueReservation:
        """Open a new reservation over the given columns."""

        if not columns:
            raise ValueError("At least one column is required to reserve a row.")
        return UniqueReservation(self, tuple(columns), attempts)

    def claim(self, reservation: UniqueReservation) -> list[NormalizedKey]:
        """Atomically claim the reservation's keys if none is occupied.

        Returns the conflicting normalized keys; when non-empty, nothing
        has been claimed and the reservation must retry the whole row.
        """

        with self._lock:
            conflicts = self._detect_conflicts(reservation._held_keys, reservation._owner)
            if conflicts:
                return conflicts
            for key in reservation._held_keys:
                self._pending_claims.setdefault(key, set()).add(reservation._owner)
            return []

    def _detect_conflicts(self, keys: Iterable[NormalizedKey], owner: object) -> list[NormalizedKey]:
        conflicts: list[NormalizedKey] = []
        for key in keys:
            if key in self._committed_keys:
                conflicts.append(key)
                continue
            owners = self._pending_claims.get(key)
            if owners and owners != {owner}:
                conflicts.append(key)
        return conflicts

    def commit(self, reservation: UniqueReservation) -> None:
        with self._lock:
            conflicts = self._detect_conflicts(reservation._held_keys, reservation._owner)
            if conflicts:
                raise UniqueConflict(
                    "Reserved keys became occupied before commit; conflicting keys: "
                    f"{conflicts!r}",
                    conflicting_keys=conflicts,
                )
            for key in reservation._held_keys:
                owners = self._pending_claims.get(key)
                if owners is not None:
                    owners.discard(reservation._owner)
                    if not owners:
                        del self._pending_claims[key]
                self._committed_keys.add(key)

    def release(self, reservation: UniqueReservation) -> None:
        with self._lock:
            for key in reservation._held_keys:
                owners = self._pending_claims.get(key)
                if owners is None:
                    continue
                owners.discard(reservation._owner)
                if not owners and key not in self._committed_keys:
                    del self._pending_claims[key]
            reservation._held_keys.clear()
            reservation._values = {}

    def conflicting_keys(self, keys: Iterable[NormalizedKey], owner: object) -> list[NormalizedKey]:
        """Return the subset of ``keys`` currently occupied by anyone else."""

        with self._lock:
            return self._detect_conflicts(keys, owner)

    def is_occupied(self, value: Any) -> bool:
        """Whether the normalized form of ``value`` currently occupies a key."""

        key = normalize_business_key(value)
        with self._lock:
            return key in self._committed_keys or key in self._pending_claims

    def is_committed(self, value: Any) -> bool:
        """Whether the normalized form of ``value`` has been delivered."""

        key = normalize_business_key(value)
        with self._lock:
            return key in self._committed_keys

    def rebuild(
        self,
        committed_rows: Iterable[Mapping[str, Any]],
        columns: Sequence[Column],
    ) -> int:
        """Rebuild the committed key space from already delivered rows.

        Every normalized key found in ``committed_rows`` is re-registered
        as committed so the next batch can continue on top of the existing
        data. Keys held by live pending reservations stay reserved.
        """

        column_map = {column.name: column for column in columns}
        with self._lock:
            rebuilt: set[NormalizedKey] = set()
            count = 0
            for row in committed_rows:
                missing = [name for name in column_map if name not in row]
                if missing:
                    raise KeyError(f"Committed row is missing columns: {missing!r}")
                for name, column in column_map.items():
                    rebuilt.add(column.normalize(row[name]))
                count += 1
            self._committed_keys = rebuilt
            return count

    @property
    def committed_keys(self) -> frozenset[NormalizedKey]:
        with self._lock:
            return frozenset(self._committed_keys)

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.RLock()


class UniqueRowFactory:
    """Convenience entry point exposed as ``Faker.reserve``.

    Columns are bound to provider methods of the owning :class:`~faker.Faker`
    instance, while reservations themselves live in named domains shared
    across generators.
    """

    def __init__(self, proxy: Any) -> None:
        self._proxy = proxy

    def column(
        self,
        name: str,
        method: str,
        *args: Any,
        key: Callable[[Any], NormalizedKey] | None = None,
        **kwargs: Any,
    ) -> Column:
        """Build a :class:`Column` backed by a Faker provider method."""

        function = getattr(self._proxy, method)
        if not callable(function):
            raise TypeError(f"Faker method {method!r} is not callable.")
        return Column(name=name, function=function, args=args, kwargs=kwargs, key=key)

    def domain(self, name: str) -> UniqueReservationManager:
        return UniqueReservationManager.domain(name)

    def row(
        self,
        domain: str,
        columns: Sequence[Column],
        attempts: int = DEFAULT_RESERVATION_ATTEMPTS,
    ) -> UniqueReservation:
        """Open (without filling) a reservation in the named domain."""

        return self.domain(domain).reserve_row(columns, attempts=attempts)

    def rebuild(
        self,
        domain: str,
        committed_rows: Iterable[Mapping[str, Any]],
        columns: Sequence[Column],
    ) -> int:
        return self.domain(domain).rebuild(committed_rows, columns)
