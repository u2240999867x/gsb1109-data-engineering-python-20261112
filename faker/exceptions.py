class BaseFakerException(Exception):
    """The base exception for all Faker exceptions."""


class UniquenessException(BaseFakerException):
    """To avoid infinite loops, after a certain number of attempts,
    the "unique" attribute of the Proxy will throw this exception.
    """


class UnsupportedFeature(BaseFakerException):
    """The requested feature is not available on this system."""

    def __init__(self, msg: str, name: str) -> None:
        self.name = name
        super().__init__(msg)


class LocaleCoherenceException(BaseFakerException):
    """Raised when no locale source can supply every requested field as one
    coherent row. The exception carries the minimal groups of conflicting
    fields, i.e. fields that are never served by the same locale source."""

    def __init__(self, message: str, conflicts: list[list[str]]) -> None:
        self.conflicts = conflicts
        super().__init__(message)
