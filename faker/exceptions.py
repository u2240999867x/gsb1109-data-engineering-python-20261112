from typing import Any


class BaseFakerException(Exception):
    """The base exception for all Faker exceptions."""


class UniquenessException(BaseFakerException):
    """To avoid infinite loops, after a certain number of attempts,
    the "unique" attribute of the Proxy will throw this exception.
    """


class UniqueReservationException(BaseFakerException):
    """Base class for errors raised while reserving unique values."""


class UniqueConflict(UniqueReservationException):
    """Raised when a reserved row cannot be made unique within the
    configured number of attempts.

    The normalized business keys that kept colliding are attached so
    the caller can decide how to handle the conflicting values.
    """

    def __init__(self, message: str, conflicting_keys: list[Any]) -> None:
        self.conflicting_keys = conflicting_keys
        super().__init__(message)


class UniqueReservationClosed(UniqueReservationException):
    """Raised when a reservation is used after it has been committed
    or released.
    """


class UnsupportedFeature(BaseFakerException):
    """The requested feature is not available on this system."""

    def __init__(self, msg: str, name: str) -> None:
        self.name = name
        super().__init__(msg)
