"""
Parsing and precedence comparison of semantic versions (SemVer 2.0.0).

The :class:`SemVer` class implements the SemVer precedence rules while
treating build metadata as opaque data: it is stored verbatim and never
participates in ordering. Numeric identifiers are kept as arbitrary
precision integers, so prerelease fields of any length are compared
exactly rather than coerced to floating point.
"""

from __future__ import annotations

import re
from functools import total_ordering
from typing import Any

# A numeric prerelease identifier sorts before an alphanumeric one.
# The integer keeps arbitrarily long digit sequences exact.
_IdentifierKey = tuple[int, Any]


class SemVerParseError(ValueError):
    """Raised when a string cannot be parsed as a semantic version."""


# Strict SemVer 2.0.0 grammar.
_VERSION_CORE = r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
_PRERELEASE_ID = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
_PRERELEASE = rf"(?:-(?P<prerelease>(?:{_PRERELEASE_ID})(?:\.(?:{_PRERELEASE_ID}))*))?"
_BUILD_ID = r"[0-9a-zA-Z-]+"
_BUILD = rf"(?:\+(?P<build>{_BUILD_ID}(?:\.{_BUILD_ID})*))?"
_STRICT_SEMVER_RE = re.compile(rf"^{_VERSION_CORE}{_PRERELEASE}{_BUILD}$")

# Lenient grammar: an optional leading "v", one to three numeric core
# components, and dot separated identifiers without leading-zero rules.
_LENIENT_SEMVER_RE = re.compile(
    r"^[vV]?"
    r"(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _identifier_key(identifier: str) -> _IdentifierKey:
    """Return a comparable key for a single prerelease identifier."""
    if identifier.isdigit():
        # Python integers have arbitrary precision, keeping very long
        # numeric prerelease fields exact.
        return (0, int(identifier))
    return (1, identifier)


@total_ordering
class SemVer:
    """
    A parsed semantic version.

    Parameters
    ----------
    major, minor, patch : int
        The version core components.
    prerelease : tuple[str, ...], optional
        The dot separated prerelease identifiers, in order.
    build : str | None, optional
        The raw build metadata, without the leading ``+``. It is stored
        as given and never affects precedence.
    raw : str, optional
        The original string from which this version was parsed.

    Attributes
    ----------
    major, minor, patch : int
        The version core components.
    prerelease : tuple[str, ...]
        The prerelease identifiers; an empty tuple means a final release.
    build : str | None
        The verbatim build metadata, or `None` when absent.

    See Also
    --------
    parse_semver

    Examples
    --------
        >>> SemVer.parse("1.0.0-alpha+exp.sha.5114f85").build
        'exp.sha.5114f85'
        >>> SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0")
        True

    """

    def __init__(  # noqa: D107, PLR0913
        self,
        major: int,
        minor: int,
        patch: int,
        prerelease: tuple[str, ...] = (),
        build: str | None = None,
        raw: str = "",
    ) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = tuple(prerelease)
        self.build = build
        self.raw = raw

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def parse(cls, version: str, *, strict: bool = False) -> SemVer:
        """
        Parse *version* into a :class:`SemVer`.

        Parameters
        ----------
        version : str
            The semantic version string.
        strict : bool, optional
            When `True`, require full SemVer 2.0.0 compliance, including
            no leading zeros and a three component version core. When
            `False`, a leading ``v``, one or two component cores, and
            leading zeros are also accepted. The default is `False`.

        Returns
        -------
        SemVer
            The parsed version.

        Raises
        ------
        SemVerParseError
            If *version* is not a recognizable semantic version.
        TypeError
            If *version* is not a string.

        """
        if not isinstance(version, str):
            msg = f"expected a str, got {type(version).__name__}"
            raise TypeError(msg)

        if strict:
            match = _STRICT_SEMVER_RE.fullmatch(version)
        else:
            match = _LENIENT_SEMVER_RE.fullmatch(version)
        if match is None:
            mode = "strict" if strict else "lenient"
            msg = f"{version!r} is not a valid {mode} semantic version"
            raise SemVerParseError(msg)

        parts = match.groupdict()
        major = int(parts["major"])
        minor = int(parts["minor"]) if parts["minor"] is not None else 0
        patch = int(parts["patch"]) if parts["patch"] is not None else 0
        prerelease_text = parts["prerelease"]
        prerelease = tuple(prerelease_text.split(".")) if prerelease_text else ()
        return cls(major, minor, patch, prerelease, parts["build"], version)

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------
    def prerelease_key(self) -> tuple[Any, ...]:
        """
        Return a comparable key for the prerelease component.

        A version without prerelease identifiers sorts after every
        prerelease of the same core. Otherwise identifiers are compared
        per SemVer: numeric before alphanumeric, numerically or
        lexicographically within each kind, and a shorter prefix sorts
        first.
        """
        if not self.prerelease:
            return (1,)
        return (
            0,
            *(_identifier_key(identifier) for identifier in self.prerelease),
        )

    def precedence_key(self) -> tuple[Any, ...]:
        """Return the full SemVer precedence key, excluding build metadata."""
        return (self.major, self.minor, self.patch, self.prerelease_key())

    def __eq__(self, other: object) -> bool:  # noqa: D105
        if not isinstance(other, SemVer):
            return NotImplemented
        return self.precedence_key() == other.precedence_key()

    def __lt__(self, other: object) -> bool:  # noqa: D105
        if not isinstance(other, SemVer):
            return NotImplemented
        return self.precedence_key() < other.precedence_key()

    def __hash__(self) -> int:  # noqa: D105
        return hash(self.precedence_key())

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    def __str__(self) -> str:  # noqa: D105
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text = f"{text}-{'.'.join(self.prerelease)}"
        if self.build is not None:
            text = f"{text}+{self.build}"
        return text

    def __repr__(self) -> str:  # noqa: D105
        return f"SemVer.parse({str(self)!r})"


def parse_semver(version: str, *, strict: bool = False) -> SemVer | None:
    """
    Parse a semantic version, returning `None` when it is not valid.

    This is a non-raising counterpart to :meth:`SemVer.parse`.

    Parameters
    ----------
    version : str
        The candidate semantic version string.
    strict : bool, optional
        When `True`, require full SemVer 2.0.0 compliance. The default
        is `False`, which accepts a leading ``v``, shortened version
        cores, and leading zeros.

    Returns
    -------
    SemVer | None
        The parsed version, or `None` if *version* is not a string or
        does not parse under the selected mode.

    Examples
    --------
        >>> parse_semver("v2.1") is not None
        True
        >>> parse_semver("v2.1", strict=True) is None
        True
        >>> parse_semver("not-a-version") is None
        True

    """
    if not isinstance(version, str):
        return None
    try:
        return SemVer.parse(version, strict=strict)
    except SemVerParseError:
        return None
