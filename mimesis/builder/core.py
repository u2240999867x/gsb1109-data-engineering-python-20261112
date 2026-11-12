"""SchemaBuilder — unified API for related fake data generation."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from mimesis.builder.resolver import (
    Binding,
    BoundColumn,
    FieldRef,
    LazyChoice,
    LazyField,
    LazyWeightedChoice,
    NestedSchema,
    Resolvable,
    SchemaRefProxy,
)
from mimesis.builder.schema import SchemaDefinition, SchemaRef
from mimesis.exceptions import (
    CircularBindingError,
    NoMatchingRowsError,
    NullBoundValueError,
)
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
        "_bound_rows",
        "_dependencies",
        "_fed",
        "_feed_deps",
        "_field",
        "_generated",
        "_generating",
        "_locale",
        "_random",
        "_resolving_bindings",
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
        self._feed_deps: dict[str, set[str]] = {}
        self._fed: dict[str, list[JSON]] = {}
        self._generated: dict[str, list[JSON]] = {}
        self._generating: set[str] = set()
        self._bound_rows: dict[Binding, JSON] = {}
        self._resolving_bindings: set[Binding] = set()

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

    def bind(
        self,
        source: SchemaRef | str,
        where: Mapping[str, Any] | None = None,
    ) -> Binding:
        """Create a bound reference to a parent table.

        Within one generated row, all columns taken from the same
        binding come from a single pinned parent row. The ``where``
        mapping filters candidate rows before sampling: keys are parent
        field names, values are literals or columns of other bindings
        (join conditions). Sampling counts each candidate row, so
        duplicate rows are weighted by their multiplicity.

        :param source: A SchemaRef of a generated schema, or the name
            of a table fed via :meth:`feed`.
        :param where: Optional join conditions as a mapping of field
            names to literals or bound columns.
        :return: A Binding whose columns resolve from one shared row.
        :raises TypeError: If ``source`` is not a SchemaRef or a table
            name, or ``where`` is not a mapping.

        Example::

            author = sb.bind(users)
            site = sb.bind(sites, where={"country": author.country})

            posts = sb.schema(
                "posts",
                {
                    "author_id": author.id,
                    "author_country": author.country,
                    "site_id": site.id,
                },
            )
        """
        if isinstance(source, SchemaRef):
            source_name = source._name
        elif isinstance(source, str):
            source_name = source
        else:
            raise TypeError(
                f"bind() expects a SchemaRef or a fed table name, got "
                f"{type(source).__name__}. Use sb.bind(schema_var) or "
                f"sb.bind('table') with sb.feed('table', rows)."
            )

        if where is None:
            conditions: dict[str, Any] = {}
        elif isinstance(where, Mapping):
            conditions = dict(where)
        else:
            raise TypeError(
                f"where must be a mapping of field names to literals or "
                f"bound columns, got {type(where).__name__}."
            )

        for field_name in conditions:
            if not isinstance(field_name, str):
                raise TypeError(
                    f"where keys must be field names (str), got "
                    f"{type(field_name).__name__}."
                )

        return Binding(source, source_name, conditions)

    def feed(self, name: str, rows: Iterable[Mapping[str, Any]]) -> str:
        """Feed external parent rows for bound references.

        Rows are materialized immediately, so one-shot iterables (e.g.
        generators or database cursors) are safe. Calling ``feed()``
        again with the same name appends rows, preserving order —
        chunked feeding yields the same seeded samples as feeding all
        rows at once. Duplicate rows are kept and counted individually
        when bindings sample candidates.

        :param name: Table name used in ``sb.bind(name)``.
        :param rows: Iterable of row mappings.
        :return: The table name, so ``sb.bind(sb.feed(...))`` works.
        :raises TypeError: If ``name`` is not a non-empty string or a
            row is not a mapping.
        :raises ValueError: If ``name`` collides with a defined schema.

        Example::

            sb.feed("users", [{"id": 1, "country": "US"}])
            sb.feed("users", [{"id": 2, "country": "DE"}])  # appends

            author = sb.bind("users")
        """
        if not isinstance(name, str) or not name:
            raise TypeError("feed() expects a non-empty table name (str)")
        if name in self._schemas:
            raise ValueError(
                f"Table '{name}' collides with a defined schema. "
                f"Use a different name for fed rows."
            )

        materialized: list[JSON] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise TypeError(
                    f"feed() rows must be mappings, got {type(row).__name__}."
                )
            materialized.append(dict(row))

        self._fed.setdefault(name, []).extend(materialized)
        return name

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
        if name in self._fed:
            raise ValueError(
                f"Schema '{name}' collides with a fed table. "
                f"Use a different schema name."
            )
        self._schemas[name] = schema
        deps, feed_deps = self._walk_references(schema)
        self._dependencies[name] = deps
        self._feed_deps[name] = feed_deps
        return SchemaRef(name, schema)

    def _walk_references(self, obj: Any) -> tuple[set[str], set[str]]:
        """Collect schema and fed-table dependencies from a definition.

        :param obj: Object to scan for references.
        :return: A tuple of (schema dependencies, fed table dependencies).
        """
        schema_deps: set[str] = set()
        feed_deps: set[str] = set()
        self._collect_references(obj, schema_deps, feed_deps, set())
        return schema_deps, feed_deps

    def _collect_references(
        self,
        value: Any,
        schema_deps: set[str],
        feed_deps: set[str],
        seen_bindings: set[int],
    ) -> None:
        """Walk a definition, collecting dependencies into the given sets."""
        if isinstance(value, (FieldRef, SchemaRefProxy)):
            schema_deps.add(value._schema_name)
        elif isinstance(value, BoundColumn):
            self._collect_binding_references(
                value._binding, schema_deps, feed_deps, seen_bindings
            )
        elif isinstance(value, Binding):
            self._collect_binding_references(
                value, schema_deps, feed_deps, seen_bindings
            )
        elif isinstance(value, NestedSchema):
            self._collect_references(
                value._definition, schema_deps, feed_deps, seen_bindings
            )
        elif isinstance(value, dict):
            for item in value.values():
                self._collect_references(item, schema_deps, feed_deps, seen_bindings)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._collect_references(item, schema_deps, feed_deps, seen_bindings)

    def _collect_binding_references(
        self,
        binding: Binding,
        schema_deps: set[str],
        feed_deps: set[str],
        seen_bindings: set[int],
    ) -> None:
        """Collect dependencies of a single binding and its conditions."""
        if id(binding) in seen_bindings:
            return
        seen_bindings.add(id(binding))

        if isinstance(binding._source, SchemaRef):
            schema_deps.add(binding._source._name)
        else:
            feed_deps.add(binding._source)

        for expected in binding._where.values():
            self._collect_references(expected, schema_deps, feed_deps, seen_bindings)

    def _extract_dependencies(self, obj: Any) -> set[str]:
        """Extract schema dependencies from a definition.

        :param obj: Object to scan for dependencies.
        :return: Set of schema names this definition depends on.
        """
        schema_deps, _ = self._walk_references(obj)
        return schema_deps

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

    def _resolve_binding(self, binding: Binding) -> JSON:
        """Resolve a binding to its pinned parent row for the current row.

        The pinned row is cached per generated row, so every column of
        the same binding comes from the same parent row.

        :param binding: The binding to resolve.
        :return: The pinned parent row.
        :raises CircularBindingError: If bindings depend on each other.
        :raises NoMatchingRowsError: If no candidate row matches.
        """
        if binding in self._bound_rows:
            return self._bound_rows[binding]

        if binding in self._resolving_bindings:
            raise CircularBindingError(binding._source_name)

        self._resolving_bindings.add(binding)
        try:
            candidates = self._binding_candidates(binding)
            row = self._random.choice(candidates)
        finally:
            self._resolving_bindings.discard(binding)

        self._bound_rows[binding] = row
        return row

    def _binding_candidates(self, binding: Binding) -> list[JSON]:
        """Collect candidate parent rows for a binding.

        :param binding: The binding to collect candidates for.
        :return: Candidate rows matching the join conditions.
        :raises ValueError: If a generated source schema is missing.
        :raises NoMatchingRowsError: If no candidate row matches.
        """
        source = binding._source
        if isinstance(source, SchemaRef):
            if source._name not in self._generated:
                raise ValueError(
                    f"Schema '{source._name}' not yet generated. "
                    f"Make sure to include it in create()."
                )
            rows: list[JSON] = self._generated[source._name]
        else:
            rows = self._fed.get(source, [])

        conditions = binding._where
        if conditions:
            candidates = [row for row in rows if self._binding_matches(row, conditions)]
        else:
            candidates = list(rows)

        if not candidates:
            raise NoMatchingRowsError(binding._source_name, sorted(conditions))
        return candidates

    def _binding_matches(self, row: JSON, conditions: dict[str, Any]) -> bool:
        """Check whether a candidate row satisfies the join conditions.

        :param row: Candidate parent row.
        :param conditions: Field names mapped to literals or bound columns.
        :return: True if every condition matches.
        """
        for field_name, expected in conditions.items():
            expected_value = expected
            if isinstance(expected_value, BoundColumn):
                expected_value = expected_value._resolve(self)
            if field_name not in row or row[field_name] != expected_value:
                return False
        return True

    def _pick_bound(self, binding: Binding, field_name: str) -> Any:
        """Pick a column value from the row pinned by a binding.

        :param binding: The binding pinning the parent row.
        :param field_name: Name of the column to extract.
        :return: The column value from the pinned row.
        :raises KeyError: If the column is missing in the pinned row.
        :raises NullBoundValueError: If the column value is None.
        """
        row = self._resolve_binding(binding)
        if field_name not in row:
            raise KeyError(
                f"Field '{field_name}' not found in '{binding._source_name}'"
            )
        value = row[field_name]
        if value is None:
            raise NullBoundValueError(binding._source_name, field_name)
        return value

    def _topological_sort(self, names: list[str]) -> list[str]:
        """Sort schema names by their dependencies.

        :param names: List of schema names to sort.
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

            for dep in self._dependencies.get(name, set()):
                visit(dep)

            visiting.remove(name)
            visited.add(name)
            result.append(name)

        for name in names:
            visit(name)

        return result

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
                # Each row gets a fresh binding scope, and a failed row
                # never leaks pinned rows into the next one.
                self._bound_rows.clear()
                try:
                    item = self._resolve_value(definition)
                finally:
                    self._bound_rows.clear()
                results.append(item)
        finally:
            self._generating.discard(name)

        return results

    def create(self, **counts: int) -> dict[str, list[JSON]]:
        """Generate all schemas with specified counts.

        Schemas are automatically sorted by dependencies, so you can
        pass them in any order.

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
        self._bound_rows.clear()
        self._resolving_bindings.clear()

        for name, count in counts.items():
            if name not in self._schemas:
                raise ValueError(f"Schema '{name}' is not defined")
            if count < 0:
                raise ValueError(f"Count for '{name}' must be >= 0")

        for name in counts:
            for dep in sorted(self._dependencies[name] - counts.keys()):
                raise ValueError(
                    f"Schema '{dep}' is required by '{name}' but not yet generated. "
                    f"Pass {dep}=<count> to create() as well."
                )
            for table in sorted(self._feed_deps.get(name, ())):
                if table not in self._fed:
                    raise ValueError(
                        f"Table '{table}' is required by '{name}' but has "
                        f"not been fed. Call sb.feed('{table}', rows) "
                        f"before create()."
                    )

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
        self._bound_rows.clear()
        self._resolving_bindings.clear()

    def reset(self) -> None:
        """Reset builder completely (clears schemas and generated data)."""
        self._schemas.clear()
        self._dependencies.clear()
        self._feed_deps.clear()
        self._fed.clear()
        self._generated.clear()
        self._generating.clear()
        self._bound_rows.clear()
        self._resolving_bindings.clear()

    def __repr__(self) -> str:
        schemas = ", ".join(self._schemas.keys()) or "none"
        return f"SchemaBuilder(locale={self._locale}, schemas=[{schemas}])"
