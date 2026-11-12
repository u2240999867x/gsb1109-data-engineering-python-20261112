"""SchemaBuilder — unified API for related fake data generation."""

from collections.abc import Mapping, Sequence
from typing import Any

from mimesis.builder.resolver import (
    OMITTED,
    FieldRef,
    LazyChoice,
    LazyField,
    LazyWeightedChoice,
    LazyWhen,
    NestedSchema,
    Resolvable,
    SchemaRefProxy,
    ValueCondition,
)
from mimesis.builder.schema import SchemaDefinition, SchemaRef
from mimesis.locales import Locale
from mimesis.schema import Field
from mimesis.types import JSON, Key, MissingSeed, Seed


__all__ = ["SchemaBuilder"]


class SchemaBuilder:
    """Unified builder for generating related fake data.

    Example::

        from mimesis import SchemaBuilder
        from mimesis.locales import Locale

        sb = SchemaBuilder(Locale.EN, seed=0xFF)

        users = sb.schema(
            "users",
            {
                "id": sb.f("increment"),
                "username": sb.f("username"),
                "email": sb.f("email"),
            },
        )

        posts = sb.schema(
            "posts",
            {
                "id": sb.f("increment"),
                "title": sb.f("sentence"),
                "user_id": sb.ref(users).id,
            },
        )

        data = sb.create(users=10, posts=50)
    """

    __slots__ = (
        "_constants",
        "_dependencies",
        "_field",
        "_generated",
        "_generating",
        "_locale",
        "_random",
        "_schemas",
        "_seed",
    )

    def __init__(
        self,
        locale: Locale = Locale.DEFAULT,
        seed: Seed = MissingSeed,
    ) -> None:
        """Initialize the SchemaBuilder.

        :param locale: Locale for data generation.
        :param seed: Seed for reproducible random generation.
        """
        self._locale = locale
        self._seed = seed
        self._field = Field(locale, seed)
        self._random = self._field.get_random_instance()
        self._schemas: dict[str, SchemaDefinition] = {}
        self._dependencies: dict[str, set[str]] = {}
        self._generated: dict[str, list[JSON]] = {}
        self._generating: set[str] = set()
        self._constants: dict[str, Any] = {}

    def reseed(self, seed: Seed = MissingSeed) -> None:
        """Reseed all random generators.

        :param seed: New seed value.
        """
        self._seed = seed
        self._field.reseed(seed)
        self._random = self._field.get_random_instance()

    def f(self, name: str, key: Key = None, **kwargs: Any) -> LazyField:
        """Create a lazy field that evaluates during generation.

        :param name: Field name (e.g., "username", "person.email").
        :param key: Optional key function to transform the result.
        :param kwargs: Additional arguments for the field method.
        :return: A lazy field that resolves during data generation.

        Example::

            sb.f("username")
            sb.f("person.email", domains=["example.com"])
            sb.f("full_name", key=str.upper)
        """
        return LazyField(name, key, **kwargs)

    def choice(self, items: Sequence[Any]) -> LazyChoice:
        """Create a lazy random choice from items.

        :param items: Sequence of items to choose from.
        :return: A lazy choice that resolves during data generation.
        :raises ValueError: If ``items`` is empty.

        Example::

            sb.choice(["active", "inactive", "pending"])
        """
        if not items:
            raise ValueError("items must not be empty")
        return LazyChoice(items)

    def weighted_choice(
        self, items: Sequence[Any], weights: Sequence[float]
    ) -> LazyWeightedChoice:
        """Create a lazy weighted random choice.

        :param items: Sequence of items to choose from.
        :param weights: Weights for each item (must match items length).
        :return: A lazy weighted choice that resolves during data generation.
        :raises ValueError: If ``items`` is empty or lengths do not match.

        Example::

            sb.weighted_choice(["common", "rare", "legendary"], [0.7, 0.25, 0.05])
        """
        return LazyWeightedChoice(items, weights)

    def eq(self, name: str, value: Any) -> ValueCondition:
        """Condition that holds when a batch constant equals ``value``.

        Conditions are deterministic: they are evaluated against the
        constants supplied to :meth:`create` and never consume random
        state. Use them with :meth:`when` to guard optional branches.

        :param name: Name of the batch constant.
        :param value: Value the constant must equal.
        :return: A condition usable with :meth:`when`.

        Example::

            sb.when(sb.ref(customs).id, sb.eq("channel", "cross_border"))
        """
        return ValueCondition(name, value, equal=True)

    def ne(self, name: str, value: Any) -> ValueCondition:
        """Condition that holds when a batch constant differs from ``value``.

        :param name: Name of the batch constant.
        :param value: Value the constant must not equal.
        :return: A condition usable with :meth:`when`.
        """
        return ValueCondition(name, value, equal=False)

    def when(self, value: Any, condition: ValueCondition) -> LazyWhen:
        """Guard a branch with a deterministic condition.

        When the condition is false the branch is omitted: the field
        disappears from records and list/tuple elements are dropped.
        When the referenced constant is not supplied to :meth:`create`,
        the condition is undecidable and the branch is kept.

        :param value: Branch value, typically a reference such as
            ``sb.ref(parents).id`` (may be nested arbitrarily deep).
        :param condition: Condition produced by :meth:`eq` or :meth:`ne`.
        :return: A lazy conditional branch.
        :raises TypeError: If ``condition`` is not a :class:`ValueCondition`.

        Example::

            "customs_id": sb.when(sb.ref(customs).id, sb.eq("channel", "cb"))
        """
        if not isinstance(condition, ValueCondition):
            raise TypeError(
                "when() expects a condition from sb.eq() or sb.ne(), "
                f"got {type(condition).__name__}"
            )
        return LazyWhen(value, condition)

    def ref(self, schema: SchemaRef) -> SchemaRefProxy:
        """Create a reference to another schema for foreign keys.

        :param schema: The SchemaRef returned by sb.schema().
        :return: A proxy that allows field access for FK references.
        :raises TypeError: If ``schema`` is not a
            :class:`~mimesis.builder.schema.SchemaRef`.

        Example::

            users = sb.schema("users", {"id": sb.f("increment")})

            posts = sb.schema(
                "posts",
                {
                    "user_id": sb.ref(users).id,  # FK to users.id
                    "author": sb.ref(users),  # Whole user record
                },
            )
        """
        if not isinstance(schema, SchemaRef):
            raise TypeError(
                f"ref() expects a SchemaRef, got {type(schema).__name__}. "
                f"Use sb.ref(schema_var) where schema_var = sb.schema(...)."
            )
        return SchemaRefProxy(schema._name)

    def schema(self, name: str, schema: SchemaDefinition) -> SchemaRef:
        """Define a schema and return a reference.

        :param name: Unique name for this schema.
        :param schema: Dictionary defining the schema fields.
        :return: A SchemaRef for FK references and nesting.
        :raises ValueError: If a schema with this name is already defined.

        Example::

            users = sb.schema(
                "users",
                {
                    "id": sb.f("increment"),
                    "username": sb.f("username"),
                    "profile": {
                        "bio": sb.f("text"),
                    },
                },
            )
        """
        if name in self._schemas:
            raise ValueError(f"Schema '{name}' is already defined")
        self._schemas[name] = schema
        self._dependencies[name] = self._extract_dependencies(schema)
        return SchemaRef(name, schema)

    def _extract_dependencies(
        self,
        obj: Any,
        active_nests: set[str] | None = None,
    ) -> set[str]:
        """Extract dependencies reachable with the current batch constants.

        References hidden inside choices and weighted choices are found
        structurally — no random generation or sampling is performed.
        Zero-weight branches and branches disabled by deterministic
        conditions contribute no dependencies; undecidable conditions
        conservatively keep every reachable dependency.

        :param obj: Object to scan for dependencies.
        :param active_nests: Nested schemas currently being expanded,
            used to stop at recursive nesting instead of looping.
        :return: Set of schema names this definition may depend on.
        """
        if isinstance(obj, (FieldRef, SchemaRefProxy)):
            deps: set[str] = {obj._schema_name}
        elif isinstance(obj, NestedSchema):
            deps = self._deps_from_nested(obj, active_nests)
        elif isinstance(obj, LazyWhen):
            deps = self._deps_from_when(obj, active_nests)
        elif isinstance(obj, LazyWeightedChoice):
            deps = self._deps_from_weighted(obj, active_nests)
        elif isinstance(obj, LazyChoice):
            deps = self._deps_from_choice(obj, active_nests)
        elif isinstance(obj, Resolvable):
            deps = set()
        elif isinstance(obj, dict):
            deps = self._deps_from_items(obj.values(), active_nests)
        elif isinstance(obj, (list, tuple)):
            deps = self._deps_from_items(obj, active_nests)
        else:
            deps = set()
        return deps

    def _deps_from_items(
        self,
        items: Any,
        active_nests: set[str] | None,
    ) -> set[str]:
        """Collect dependencies from every item of an iterable."""
        deps: set[str] = set()
        for item in items:
            deps.update(self._extract_dependencies(item, active_nests))
        return deps

    def _deps_from_nested(
        self,
        nested: NestedSchema,
        active_nests: set[str] | None,
    ) -> set[str]:
        """Expand a nested schema, stopping on recursive nesting."""
        if active_nests is None:
            active_nests = set()
        if nested._schema_name in active_nests:
            return set()
        active_nests.add(nested._schema_name)
        try:
            return self._extract_dependencies(nested._definition, active_nests)
        finally:
            active_nests.discard(nested._schema_name)

    def _deps_from_when(
        self,
        branch: LazyWhen,
        active_nests: set[str] | None,
    ) -> set[str]:
        """Keep a guarded branch unless its condition is statically false."""
        if branch._condition._evaluate(self._constants) is False:
            return set()
        return self._extract_dependencies(branch._value, active_nests)

    def _deps_from_weighted(
        self,
        choice: LazyWeightedChoice,
        active_nests: set[str] | None,
    ) -> set[str]:
        """Collect dependencies from branches carrying positive weight."""
        deps: set[str] = set()
        for item, weight in zip(choice._items, choice._weights, strict=True):
            if weight > 0:
                deps.update(self._extract_dependencies(item, active_nests))
        return deps

    def _deps_from_choice(
        self,
        choice: LazyChoice,
        active_nests: set[str] | None,
    ) -> set[str]:
        """Collect dependencies from every branch of a plain choice."""
        return self._deps_from_items(choice._items, active_nests)

    def _rebuild_dependencies(self) -> None:
        """Recompute every schema's dependency graph from its definition."""
        self._dependencies = {
            name: self._extract_dependencies(definition)
            for name, definition in self._schemas.items()
        }

    def _resolve_field(self, name: str, key: Key = None, **kwargs: Any) -> Any:
        """Resolve a field name to its value.

        :param name: Field name.
        :param key: Optional key function.
        :param kwargs: Field arguments.
        :return: Generated value.
        """
        return self._field(name, key=key, **kwargs)

    def _resolve_value(self, value: Any) -> Any:
        """Recursively resolve lazy values to actual values.

        :param value: Value to resolve (may be lazy or nested).
        :return: Resolved value.
        """
        if isinstance(value, Resolvable):
            # Resolve again so choice/weighted_choice can yield nested
            # lazy values (e.g. sb.choice([None, sb.f("sentence")])).
            return self._resolve_value(value._resolve(self))
        if isinstance(value, SchemaRef):
            raise TypeError(
                f"Bare SchemaRef({value._name!r}) cannot be used as a field value. "
                f"Use sb.ref({value._name}) for foreign keys or "
                f"{value._name}(count=N) for nesting."
            )
        if isinstance(value, dict):
            return self._resolve_dict(value)
        if isinstance(value, list):
            return self._resolve_list(value)
        if isinstance(value, tuple):
            return tuple(self._resolve_list(value))
        return value

    def _resolve_dict(self, value: dict[str, Any]) -> dict[str, Any]:
        """Resolve a dict, dropping fields whose branch is omitted."""
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            resolved_item = self._resolve_value(item)
            if resolved_item is not OMITTED:
                resolved[key] = resolved_item
        return resolved

    def _resolve_list(self, value: list[Any] | tuple[Any, ...]) -> list[Any]:
        """Resolve a sequence, dropping elements whose branch is omitted."""
        return [
            resolved_item
            for item in value
            if (resolved_item := self._resolve_value(item)) is not OMITTED
        ]

    def _pick_from(self, schema_name: str, field_name: str) -> Any:
        """Pick a random field value from generated data.

        :param schema_name: Name of the schema.
        :param field_name: Name of the field to extract.
        :return: Random value from the specified field.
        :raises ValueError: If schema hasn't been generated yet.
        """
        if schema_name not in self._generated:
            raise ValueError(
                f"Schema '{schema_name}' not yet generated. "
                f"Make sure to include it in create()."
            )

        items = self._generated[schema_name]
        if not items:
            raise ValueError(f"Schema '{schema_name}' has no items")

        item = self._random.choice(items)
        if field_name not in item:
            raise KeyError(f"Field '{field_name}' not found in schema '{schema_name}'")
        return item[field_name]

    def _pick_record(self, schema_name: str) -> JSON:
        """Pick a random complete record from generated data.

        :param schema_name: Name of the schema.
        :return: Random record (dict) from the schema.
        :raises ValueError: If schema hasn't been generated yet.
        """
        if schema_name not in self._generated:
            raise ValueError(
                f"Schema '{schema_name}' not yet generated. "
                f"Make sure to include it in create()."
            )

        items = self._generated[schema_name]
        if not items:
            raise ValueError(f"Schema '{schema_name}' has no items")

        return self._random.choice(items)

    def _topological_sort(self, names: list[str]) -> list[str]:
        """Sort schema names by their dependencies.

        Only dependencies that are part of the requested batch are
        followed, so a cycle hidden behind an unreachable branch can
        never reach this point. Ties are broken by schema declaration
        order, which keeps generation order (and therefore the random
        stream consumed) identical regardless of ``create()`` kwarg
        order.

        :param names: List of schema names to sort.
        :return: Topologically sorted list.
        :raises ValueError: If circular dependency detected.
        """
        requested = set(names)
        result: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()
        path: list[str] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                cycle_start = path.index(name)
                cycle = " -> ".join([*path[cycle_start:], name])
                raise ValueError(f"Circular dependency detected: {cycle}")

            visiting.add(name)
            path.append(name)

            deps = self._dependencies.get(name, set()) & requested
            for dep in self._schemas:
                if dep in deps:
                    visit(dep)

            path.pop()
            visiting.remove(name)
            visited.add(name)
            result.append(name)

        for name in self._schemas:
            if name in requested:
                visit(name)

        return result

    def _find_cycles(self, names: list[str]) -> list[list[str]]:
        """Return all dependency cycles reachable from ``names``.

        The graph is analyzed structurally — nothing is generated or
        sampled. Each cycle is reported once as the list of schema names
        in dependency order, closed back onto its first element.

        :param names: Schema names included in the batch.
        :return: A list of cycles (each a list of schema names).
        """
        requested = set(names)
        color: dict[str, int] = {}
        stack: list[str] = []
        cycles: list[list[str]] = []
        seen: set[frozenset[str]] = set()

        def visit(name: str) -> None:
            color[name] = 1
            stack.append(name)
            for dep in sorted(self._dependencies.get(name, set()) & requested):
                dep_color = color.get(dep, 0)
                if dep_color == 0:
                    visit(dep)
                elif dep_color == 1:
                    cycle_start = stack.index(dep)
                    cycle_nodes = frozenset(stack[cycle_start:])
                    if cycle_nodes not in seen:
                        seen.add(cycle_nodes)
                        cycles.append([*stack[cycle_start:], dep])
            stack.pop()
            color[name] = 2

        for name in sorted(requested):
            if color.get(name, 0) == 0:
                visit(name)

        return cycles

    def _generate_schema(self, name: str, count: int) -> list[JSON]:
        """Generate data for a single schema.

        :param name: Schema name.
        :param count: Number of items to generate.
        :return: List of generated items.
        """
        definition = self._schemas[name]
        results: list[JSON] = []

        self._generating.add(name)
        try:
            for _ in range(count):
                item = self._resolve_value(definition)
                results.append(item)
        finally:
            self._generating.discard(name)

        return results

    def create(
        self,
        *,
        constants: Mapping[str, Any] | None = None,
        **counts: int,
    ) -> dict[str, list[JSON]]:
        """Generate all schemas with specified counts.

        Schemas are automatically sorted by dependencies, so you can
        pass them in any order.

        :param constants: Per-batch constants used to evaluate
            deterministic conditions created with :meth:`eq`/
            :meth:`ne`. Conditions referencing missing constants are
            undecidable, so every reachable branch stays active.
        :param counts: Schema names are mapped to their counts.
        :return: Dictionary of schema names to generated data lists.
        :raises ValueError: If a schema name is not defined, a count is negative,
            a schema it references was not requested, or a reachable dependency
            cycle exists. All validation happens before any row is written.

        Example::

            data = sb.create(
                constants={"order_type": "cross_border"},
                users=10,
                posts=50,
                comments=200,
            )
        """
        self._generated.clear()
        self._generating.clear()
        self._constants = dict(constants) if constants is not None else {}

        for name, count in counts.items():
            if name not in self._schemas:
                raise ValueError(f"Schema '{name}' is not defined")
            if count < 0:
                raise ValueError(f"Count for '{name}' must be >= 0")

        # Re-analyze the definitions against this batch's constants:
        # purely structural, so no random state is consumed.
        self._rebuild_dependencies()

        for name in counts:
            for dep in sorted(self._dependencies[name] - counts.keys()):
                raise ValueError(
                    f"Schema '{dep}' is required by '{name}' but not yet generated. "
                    f"Pass {dep}=<count> to create() as well."
                )

        cycles = self._find_cycles(list(counts.keys()))
        if cycles:
            formatted = "; ".join(" -> ".join(cycle) for cycle in cycles)
            raise ValueError(f"Circular dependency detected: {formatted}")

        ordered_names = self._topological_sort(list(counts.keys()))

        result: dict[str, list[JSON]] = {}
        for name in ordered_names:
            data = self._generate_schema(name, counts[name])
            self._generated[name] = data
            result[name] = data

        return result

    def clear(self) -> None:
        """Clear all generated data (keeps schema definitions)."""
        self._generated.clear()
        self._generating.clear()

    def reset(self) -> None:
        """Reset builder completely (clears schemas and generated data)."""
        self._schemas.clear()
        self._dependencies.clear()
        self._generated.clear()
        self._generating.clear()
        self._constants.clear()

    def __repr__(self) -> str:
        schemas = ", ".join(self._schemas.keys()) or "none"
        return f"SchemaBuilder(locale={self._locale}, schemas=[{schemas}])"
