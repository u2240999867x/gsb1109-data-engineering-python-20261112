"""SchemaBuilder — unified API for related fake data generation."""

from collections.abc import Sequence
from typing import Any

from mimesis.builder.resolver import (
    FieldRef,
    LazyChoice,
    LazyField,
    LazyWeightedChoice,
    NestedSchema,
    Resolvable,
    SchemaRefProxy,
    snapshot_definition,
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
        "_active_definitions",
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
        # Definitions pinned for the running create() batch. None outside a
        # batch; the dependency graph is derived from this same snapshot.
        self._active_definitions: dict[str, SchemaDefinition] | None = None

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
        self._dependencies = self._build_dependencies(self._schemas)
        return SchemaRef(name)

    def replace_schema(self, name: str, schema: SchemaDefinition) -> SchemaRef:
        """Replace an existing schema definition.

        Unlike mutating the original definition in place, this is the explicit
        way to change a table after registration. The replacement is validated
        immediately: if it introduces a dependency cycle, the builder is left
        untouched and the error is raised before any batch is affected.

        A running :meth:`create` batch keeps generating from its own pinned
        snapshot of the old definitions; the replacement takes effect on the
        next batch.

        :param name: Name of the schema to replace.
        :param schema: New dictionary defining the schema fields.
        :return: A SchemaRef for the replaced name. Previously returned refs
            and foreign keys stay valid because they reference the name.
        :raises ValueError: If the schema is not defined or the replacement
            introduces a circular dependency.
        """
        if name not in self._schemas:
            raise ValueError(f"Schema '{name}' is not defined")

        candidate_definitions = {**self._schemas, name: schema}
        candidate_dependencies = self._build_dependencies(candidate_definitions)
        self._ensure_acyclic(candidate_dependencies)

        self._schemas[name] = schema
        self._dependencies = candidate_dependencies
        return SchemaRef(name)

    def _build_dependencies(
        self,
        definitions: dict[str, SchemaDefinition],
    ) -> dict[str, set[str]]:
        """Build the full dependency graph for a set of definitions.

        :param definitions: Mapping of schema name to its definition.
        :return: Mapping of schema name to the names it depends on.
        """
        return {
            name: self._collect_dependencies(definition, definitions, set())
            for name, definition in definitions.items()
        }

    def _collect_dependencies(
        self,
        obj: Any,
        definitions: dict[str, SchemaDefinition],
        nested_seen: set[str],
    ) -> set[str]:
        deps: set[str] = set()

        if isinstance(obj, (FieldRef, SchemaRefProxy)):
            deps.add(obj._schema_name)
        elif isinstance(obj, NestedSchema):
            nested_name = obj._schema_name
            # Guard against self/cyclic nesting while scanning the graph.
            if nested_name in nested_seen:
                return deps
            nested_seen = nested_seen | {nested_name}
            nested_definition = definitions.get(nested_name)
            if nested_definition is not None:
                deps.update(
                    self._collect_dependencies(
                        nested_definition, definitions, nested_seen
                    )
                )
        elif isinstance(obj, dict):
            for value in obj.values():
                deps.update(self._collect_dependencies(value, definitions, nested_seen))
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                deps.update(self._collect_dependencies(item, definitions, nested_seen))

        return deps

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
            return {k: self._resolve_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._resolve_value(v) for v in value)
        return value

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

    def _ensure_acyclic(self, dependencies: dict[str, set[str]]) -> None:
        """Validate that a dependency graph has no cycles.

        :param dependencies: Mapping of schema name to its dependency names.
        :raises ValueError: If a circular dependency is detected.
        """
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Circular dependency detected involving '{name}'")

            visiting.add(name)
            for dep in dependencies.get(name, set()):
                visit(dep)
            visiting.remove(name)
            visited.add(name)

        for name in dependencies:
            visit(name)

    def _topological_sort(
        self,
        names: list[str],
        dependencies: dict[str, set[str]],
    ) -> list[str]:
        """Sort schema names by their dependencies.

        :param names: List of schema names to sort.
        :param dependencies: The dependency graph to sort against.
        :return: Topologically sorted list.
        :raises ValueError: If circular dependency detected.
        """
        result: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Circular dependency detected involving '{name}'")

            visiting.add(name)

            for dep in dependencies.get(name, set()):
                visit(dep)

            visiting.remove(name)
            visited.add(name)
            result.append(name)

        for name in names:
            visit(name)

        return result

    def _get_batch_definition(self, name: str) -> SchemaDefinition:
        """Return the definition pinned for the running batch.

        :param name: Schema name.
        :return: The snapshotted definition used by the active batch.
        :raises ValueError: If no generation batch is running.
        """
        if self._active_definitions is None:
            raise ValueError(f"Schema '{name}' can only be resolved during create()")
        return self._active_definitions[name]

    def _generate_schema(self, name: str, count: int) -> list[JSON]:
        """Generate data for a single schema.

        :param name: Schema name.
        :param count: Number of items to generate.
        :return: List of generated items.
        """
        definition = self._get_batch_definition(name)
        results: list[JSON] = []

        self._generating.add(name)
        try:
            for _ in range(count):
                item = self._resolve_value(definition)
                results.append(item)
        finally:
            self._generating.discard(name)

        return results

    def create(self, **counts: int) -> dict[str, list[JSON]]:
        """Generate all schemas with specified counts.

        Schemas are automatically sorted by dependencies, so you can
        pass them in any order.

        The whole set of definitions and the dependency graph are snapshotted
        when the batch starts: the batch reads one consistent, immutable view
        even if the original definitions are mutated or replaced while it is
        running. Changes become visible to the next batch.

        :param counts: Schema names are mapped to their counts.
        :return: Dictionary of schema names to generated data lists.
        :raises ValueError: If a schema name is not defined, a count is negative,
            or a schema it references was not requested.

        Example::

            data = sb.create(
                users=10,
                posts=50,
                comments=200,
            )
        """
        self._generated.clear()
        self._generating.clear()

        for name, count in counts.items():
            if name not in self._schemas:
                raise ValueError(f"Schema '{name}' is not defined")
            if count < 0:
                raise ValueError(f"Count for '{name}' must be >= 0")

        # Pin one immutable view of the definitions for the whole batch, and
        # derive the dependency graph from that very same view so the two can
        # never drift apart even if the originals are mutated mid-run. Changes
        # become visible to the next batch.
        active_definitions = {
            name: snapshot_definition(definition)
            for name, definition in self._schemas.items()
        }
        active_dependencies = self._build_dependencies(active_definitions)
        self._active_definitions = active_definitions

        try:
            for name in counts:
                for dep in sorted(active_dependencies[name] - counts.keys()):
                    raise ValueError(
                        f"Schema '{dep}' is required by '{name}' but not yet "
                        f"generated. Pass {dep}=<count> to create() as well."
                    )

            ordered_names = self._topological_sort(
                list(counts.keys()), active_dependencies
            )

            result: dict[str, list[JSON]] = {}
            for name in ordered_names:
                data = self._generate_schema(name, counts[name])
                self._generated[name] = data
                result[name] = data
        finally:
            self._active_definitions = None

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
        self._active_definitions = None

    def __repr__(self) -> str:
        schemas = ", ".join(self._schemas.keys()) or "none"
        return f"SchemaBuilder(locale={self._locale}, schemas=[{schemas}])"
