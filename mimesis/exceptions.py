"""Custom exceptions used by Mimesis."""

import typing as t

from mimesis.enums import Locale


class LocaleError(ValueError):
    """Raised when a locale isn't supported."""

    def __init__(self, locale: Locale | str) -> None:
        """Initialize attributes for informative output.

        :param locale: Locale.
        """
        self.locale = locale

    def __str__(self) -> str:
        return f"Invalid locale «{self.locale}»"


class SchemaError(ValueError):
    """Raised when a schema is invalid."""

    def __str__(self) -> str:
        return "The schema must be a callable object that returns a dict."


class NonEnumerableError(TypeError):
    """Raised when an object is not a member of the expected Enum."""

    def __init__(self, enum_obj: t.Any) -> None:
        """Initialize attributes for informative output.

        :param enum_obj: Enum object.
        """
        if enum_obj:
            self.name = enum_obj
            self.items = ", ".join(map(str, enum_obj))
        else:
            self.name = None
            self.items = ""

    def __str__(self) -> str:
        if not self.name:
            return "Expected a member of an Enum."
        return (
            f"Argument must be a member of mimesis.enums.{self.name.__name__}: "
            f"«{self.items}»"
        )


class FieldError(ValueError):
    """Raised when a field is missing or invalid."""

    def __init__(self, name: str | None = None) -> None:
        """Initialize attributes for more informative output.

        :param name: Name of the field.
        """
        self.name = name

    def __str__(self) -> str:
        if self.name is None:
            return "A field name is required."
        return f"Field «{self.name}» is not supported."


class FieldsetError(ValueError):
    """Raised when the fieldset iteration count is invalid."""

    def __str__(self) -> str:
        return "The «i» parameter must be at least 1."


class FieldNameError(ValueError):
    """Raised when a field name is invalid."""

    def __init__(self, name: str | None = None) -> None:
        """Initialize attributes for more informative output.

        :param name: Name of the field.
        """
        self.name = name

    def __str__(self) -> str:
        return f"The field name «{self.name}» is not a valid Python identifier."


class FieldArityError(ValueError):
    """Raised when a registered field handler has incompatible arity."""

    def __str__(self) -> str:
        return (
            "The custom handler must accept at least two arguments: "
            "'random' and '**kwargs'"
        )


class BindingError(ValueError):
    """Base class for errors raised while resolving bound references."""


class NoMatchingRowsError(BindingError):
    """Raised when a binding finds no candidate rows."""

    def __init__(
        self,
        source: str,
        conditions: t.Iterable[str] | None = None,
    ) -> None:
        """Initialize attributes for informative output.

        :param source: Name of the bound table.
        :param conditions: Field names used in the join conditions.
        """
        self.source = source
        self.conditions = tuple(conditions or ())

    def __str__(self) -> str:
        if self.conditions:
            fields = ", ".join(self.conditions)
            return f"No rows in «{self.source}» match the join conditions on: {fields}."
        return f"No candidate rows available from «{self.source}»."


class NullBoundValueError(BindingError):
    """Raised when a bound column resolves to None."""

    def __init__(self, source: str, field: str) -> None:
        """Initialize attributes for informative output.

        :param source: Name of the bound table.
        :param field: Name of the column whose value is None.
        """
        self.source = source
        self.field = field

    def __str__(self) -> str:
        return f"Column «{self.field}» of the bound row from «{self.source}» is None."


class CircularBindingError(BindingError):
    """Raised when bound references depend on each other in a cycle."""

    def __init__(self, source: str) -> None:
        """Initialize attributes for informative output.

        :param source: Name of the bound table closing the cycle.
        """
        self.source = source

    def __str__(self) -> str:
        return f"Circular binding detected involving «{self.source}»."


class AliasesTypeError(TypeError):
    """Raised when aliases is not a flat dictionary."""

    def __str__(self) -> str:
        return (
            "The 'aliases' attribute must be a non-nested dictionary where "
            "keys are aliases and values are the corresponding field names."
        )
