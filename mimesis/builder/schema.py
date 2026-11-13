"""Schema reference types for SchemaBuilder."""

from typing import Any

from mimesis.builder.resolver import NestedSchema


__all__ = ["SchemaDefinition", "SchemaRef"]

SchemaDefinition = dict[str, Any]


class SchemaRef:
    """Reference to a defined schema.

    Returned by ``sb.schema()``. Used for:

    1. Foreign keys via ``sb.ref(schema_ref).field``
    2. Nesting via ``schema_ref(count=N)``

    Example::

        users = sb.schema("users", {"id": sb.f("increment")})

        posts = sb.schema(
            "posts",
            {
                "user_id": sb.ref(users).id,  # Foreign key
            },
        )

        companies = sb.schema(
            "companies",
            {
                "name": sb.f("company"),
                "employees": users(count=5),  # Nested users
            },
        )
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        """Initialize schema reference.

        :param name: The schema name.
        """
        self._name = name

    @property
    def name(self) -> str:
        """Return the schema name."""
        return self._name

    def __call__(self, *, count: int = 1) -> NestedSchema:
        """Create a nested embedding of this schema.

        The embedded definition is looked up on the builder from the active
        batch snapshot when the value is resolved, so a replacement made with
        :meth:`~mimesis.builder.SchemaBuilder.replace_schema` takes effect on
        the next batch instead of being frozen at registration time.

        :param count: Number of items to generate and embed.
        :return: A NestedSchema that resolves to a list of items.
        :raises ValueError: If ``count`` is less than 1.
        """
        return NestedSchema(self._name, count)

    def __repr__(self) -> str:
        return f"SchemaRef({self._name!r})"
