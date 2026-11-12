"""Lazy evaluation types for SchemaBuilder."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from mimesis.types import Key


if TYPE_CHECKING:
    from mimesis.builder.core import SchemaBuilder

__all__ = [
    "OMITTED",
    "FieldRef",
    "LazyChoice",
    "LazyField",
    "LazyWeightedChoice",
    "LazyWhen",
    "NestedSchema",
    "Resolvable",
    "SchemaRefProxy",
    "ValueCondition",
]


class _Omitted:
    """Sentinel marking a conditional branch that is not taken."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "OMITTED"


OMITTED = _Omitted()


class Resolvable(ABC):
    """Abstract base class for resolving values from a schema."""

    __slots__ = ()

    @abstractmethod
    def _resolve(self, builder: "SchemaBuilder") -> Any:
        """Resolve the lazy value to an actual value.

        :param builder: The SchemaBuilder instance.
        :return: The resolved value.
        """
        raise NotImplementedError


class ValueCondition:
    """Deterministic predicate evaluated against per-batch constants.

    The verdict is purely static:

    - ``True`` / ``False`` when the referenced constant is known
    - ``None`` when it cannot be determined before generation, in which
      case dependency analysis conservatively keeps every reachable branch
    """

    __slots__ = ("_actual", "_equal", "_expected")

    def __init__(self, actual: str, expected: Any, equal: bool) -> None:
        self._actual = actual
        self._expected = expected
        self._equal = equal

    def _evaluate(self, constants: Mapping[str, Any] | None) -> bool | None:
        """Evaluate the condition against the given constants.

        :param constants: Mapping of constant names to batch values.
        :return: ``True``/``False`` when decidable, otherwise ``None``.
        """
        if not isinstance(constants, Mapping) or self._actual not in constants:
            return None
        matched = constants[self._actual] == self._expected
        return matched if self._equal else not matched

    def __repr__(self) -> str:
        operator = "==" if self._equal else "!="
        return f"ValueCondition({self._actual!r} {operator} {self._expected!r})"


class LazyWhen(Resolvable):
    """Conditionally include a branch based on a deterministic condition.

    When the condition evaluates to ``False`` the branch is omitted:
    dict fields disappear and list/tuple elements are dropped. When it
    cannot be decided (the referenced constant is absent), the branch
    is kept.

    Example::

        sb.when(sb.ref(cross_border_parents).id, sb.eq("order_type", "cb"))
    """

    __slots__ = ("_condition", "_value")

    def __init__(self, value: Any, condition: ValueCondition) -> None:
        self._value = value
        self._condition = condition

    def _resolve(self, builder: "SchemaBuilder") -> Any:
        if self._condition._evaluate(builder._constants) is False:
            return OMITTED
        return builder._resolve_value(self._value)

    def __repr__(self) -> str:
        return f"LazyWhen({self._value!r}, {self._condition!r})"


class LazyField(Resolvable):
    """A field that evaluates lazily during schema generation.

    Example::

        sb.f("username")
        sb.f("email", key=str.lower)
        sb.f("integer_number", start=1, end=100)
    """

    __slots__ = ("_key", "_kwargs", "_name")

    def __init__(
        self,
        name: str,
        key: Key = None,
        **kwargs: Any,
    ) -> None:
        self._name = name
        self._key = key
        self._kwargs = kwargs

    def _resolve(self, builder: "SchemaBuilder") -> Any:
        return builder._resolve_field(self._name, self._key, **self._kwargs)

    def __repr__(self) -> str:
        return f"LazyField({self._name!r})"


class LazyChoice(Resolvable):
    """Lazy random choice from a list of items.

    Example::

        sb.choice(["active", "inactive", "pending"])
    """

    __slots__ = ("_items",)

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = items

    def _resolve(self, builder: "SchemaBuilder") -> Any:
        items = [
            item
            for item in self._items
            if not (
                isinstance(item, LazyWhen)
                and item._condition._evaluate(builder._constants) is False
            )
        ]
        if not items:
            return OMITTED
        return builder._random.choice(items)

    def __repr__(self) -> str:
        return f"LazyChoice({self._items!r})"


class LazyWeightedChoice(Resolvable):
    """Lazy weighted random choice.

    Example::

        sb.weighted_choice(["common", "rare"], [0.9, 0.1])
    """

    __slots__ = ("_items", "_weights")

    def __init__(
        self,
        items: Sequence[Any],
        weights: Sequence[float],
    ) -> None:
        if len(items) != len(weights):
            raise ValueError("items and weights must have the same length")
        if not items:
            raise ValueError("items must not be empty")
        self._items = items
        self._weights = weights

    def _resolve(self, builder: "SchemaBuilder") -> Any:
        active = [
            (item, weight)
            for item, weight in zip(self._items, self._weights, strict=True)
            if not (
                isinstance(item, LazyWhen)
                and item._condition._evaluate(builder._constants) is False
            )
        ]
        if not active:
            return OMITTED
        items, weights = zip(*active, strict=True)
        return builder._random.choices(items, weights=weights)[0]

    def __repr__(self) -> str:
        return f"LazyWeightedChoice({self._items!r})"


class FieldRef(Resolvable):
    """Reference to a specific field in another schema.

    Resolves to a random value from the referenced schema's field.

    Example::

        sb.ref(users).id  # Returns FieldRef
    """

    __slots__ = ("_field_name", "_schema_name")

    def __init__(self, schema_name: str, field_name: str) -> None:
        self._schema_name = schema_name
        self._field_name = field_name

    def _resolve(self, builder: "SchemaBuilder") -> Any:
        return builder._pick_from(self._schema_name, self._field_name)

    def __repr__(self) -> str:
        return f"FieldRef({self._schema_name!r}.{self._field_name!r})"


class SchemaRefProxy(Resolvable):
    """Reference to an entire schema record.

    When used without field access, resolves to a random complete record.

    Example::

        sb.ref(users)  # Returns SchemaRefProxy (whole record)
        sb.ref(users).id  # Returns FieldRef (specific field)
    """

    __slots__ = ("_schema_name",)

    def __init__(self, schema_name: str) -> None:
        self._schema_name = schema_name

    def __getattr__(self, field_name: str) -> FieldRef:
        """Access a specific field from the referenced schema.

        :param field_name: Name of the field to reference.
        :return: A FieldRef that resolves to a random value from this field.
        """
        if field_name.startswith("_"):
            raise AttributeError(field_name)
        return FieldRef(self._schema_name, field_name)

    def _resolve(self, builder: "SchemaBuilder") -> Any:
        """Resolve to a random complete record from the schema."""
        return builder._pick_record(self._schema_name)

    def __repr__(self) -> str:
        return f"SchemaRefProxy({self._schema_name!r})"


class NestedSchema(Resolvable):
    """Embed N generated items from another schema definition.

    Example::

        addresses = sb.schema("addresses", {"city": sb.f("city")})
        users = sb.schema(
            "users",
            {
                "name": sb.f("full_name"),
                "addresses": addresses(count=3),
            },
        )
    """

    __slots__ = ("_count", "_definition", "_schema_name")

    def __init__(
        self,
        schema_name: str,
        definition: dict[str, Any],
        count: int,
    ) -> None:
        if count < 1:
            raise ValueError("count must be >= 1")
        self._schema_name = schema_name
        self._definition = definition
        self._count = count

    def _resolve(self, builder: "SchemaBuilder") -> list[Any]:
        if self._schema_name in builder._generating:
            raise ValueError(
                f"Circular nesting detected involving '{self._schema_name}'"
            )

        builder._generating.add(self._schema_name)
        try:
            return [
                builder._resolve_value(self._definition) for _ in range(self._count)
            ]
        finally:
            builder._generating.discard(self._schema_name)

    def __repr__(self) -> str:
        return f"NestedSchema({self._schema_name!r}, count={self._count})"
