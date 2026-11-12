"""The module **mimesis.keys** provides a set of key functions.

Key functions can be applied to fields and fieldsets using the **key** argument.
These functions are applied after the field's value is generated and before the
field is returned to the caller.
"""

import base64
import contextlib
import hashlib
import inspect
import re
from collections.abc import Callable, Iterable
from typing import Any

from mimesis.datasets import COMMON_LETTERS, ROMANIZATION_DICT
from mimesis.exceptions import PipeCheckpointError
from mimesis.locales import Locale, validate_locale
from mimesis.random import Random
from mimesis.types import KeyFunc


__all__ = [
    "PipeCheckpoint",
    "PipeKey",
    "apply_if",
    "base64_encode",
    "camel_case",
    "hash_with",
    "join",
    "kebab_case",
    "maybe",
    "pipe",
    "prefix",
    "redact",
    "remove_whitespace",
    "reverse",
    "romanize",
    "slugify",
    "snake_case",
    "suffix",
    "truncate",
    "urlsafe_base64_encode",
    "wrap",
]


def romanize(locale: Locale) -> Callable[[str], str]:
    """Create a closure function to romanize a given string in the specified locale.

    Supported locales are:

    - Locale.RU (Russian)
    - Locale.UK (Ukrainian)
    - Locale.KK (Kazakh)

    :param locale: Locale.
    :return: A closure that takes a string and returns a romanized string.
    """
    locale = validate_locale(locale)

    if locale not in (Locale.RU, Locale.UK, Locale.KK):
        raise ValueError(f"Romanization is not available for locale {locale}")

    table = str.maketrans({**ROMANIZATION_DICT[locale.value], **COMMON_LETTERS})

    def key(v: str) -> str:
        """Romanize a given string in the specified locale.

        :param v: Cyrillic string.
        :return: Romanized string.
        """
        if not isinstance(v, str):
            raise TypeError(f"romanize() requires a string, got {type(v).__name__}")

        return v.translate(table)

    return key


def maybe(value: Any, probability: float = 0.5) -> Callable[[Any, Random], Any]:
    """Return a closure (a key function).

    The returned closure itself returns either **value** or
    the first argument passed to the closure with a certain
    probability (0.5 by default).

    :param value: The value that may be returned.
    :param probability: The probability of returning **value**.
    :return: A closure that takes two arguments.
    """

    def key(result: Any, random: Random) -> Any:
        if 0 < probability <= 1:
            value_weight = 1 - probability
            (result,) = random.choices(
                population=[result, value],
                weights=[value_weight, probability],
                k=1,
            )
        return result

    return key


def wrap(before: str = "<", after: str = ">") -> Callable[[str], str]:
    """Wrap the result with before and after strings.

    Example:
        >>> field("word", key=wrap("[", "]"))
        '[dynamics]'

    :param before: String to prepend.
    :param after: String to append.
    :return: A closure that wraps a string.
    :raises TypeError: If the result is not a string.
    """

    def key(v: str) -> str:
        if not isinstance(v, str):
            raise TypeError(f"wrap() requires a string, got {type(v).__name__}")
        return f"{before}{v}{after}"

    return key


def reverse(value: str) -> str:
    """Reverse the string.

    :param value: Input string to reverse.
    :return: Reversed string.
    :raises TypeError: If result is not a string.

    Example:
        >>> field("word", key=reverse)
        'ebircsed'

    """
    if not isinstance(value, str):
        raise TypeError(f"reverse() requires a string, got {type(value).__name__}")
    return value[::-1]


def slugify(value: str) -> str:
    """Convert to URL-friendly slug.

    Example:
        >>> field("sentence", key=slugify)
        'where-are-my-pants'

    :param value: Input string to slugify.
    :return: URL-friendly slug.
    :raises TypeError: If result is not a string.
    """
    if not isinstance(value, str):
        raise TypeError(f"slugify() requires a string, got {type(value).__name__}")
    value = value.lower()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[-\s]+", "-", value)
    return value.strip("-")


def snake_case(value: str) -> str:
    """Convert to snake_case.

    Example:
        >>> field("full_name", key=snake_case)
        'michael_caldwell'

    :param value: Input string to convert.
    :return: snake_case string.
    :raises TypeError: If result is not a string.
    """
    if not isinstance(value, str):
        raise TypeError(f"snake_case() requires a string, got {type(value).__name__}")
    value = value.strip().replace(" ", "_")
    value = re.sub(r"[^\w_]", "", value)
    return value.lower()


def camel_case(value: str) -> str:
    """Convert to camelCase.

    Example:
        >>> field("sentence", key=camel_case)
        'makeMeASandwich.'

    :param value: Input string to convert.
    :return: camelCase string.
    :raises TypeError: If result is not a string.
    """
    if not isinstance(value, str):
        raise TypeError(f"camel_case() requires a string, got {type(value).__name__}")

    words = value.split()
    if not words:
        return ""

    return words[0].lower() + "".join(w.capitalize() for w in words[1:])


def kebab_case(value: str) -> str:
    """Convert to kebab-case.

    :param value: Input string to convert.
    :return: kebab-case string.
    :raises TypeError: If result is not a string.
    """
    return slugify(value)


def truncate(max_length: int, suffix: str = "...") -> Callable[[str], str]:
    """Truncate to maximum length.

    Example:
        >>> field("sentence", key=truncate(20))
        'Ports are created...'

    :param max_length: Maximum length of the result.
    :param suffix: Suffix to add when truncating.
    :return: A closure that truncates a string.
    :raises TypeError: If result is not a string.
    :raises ValueError: If max_length is not positive.
    """
    if max_length <= 0:
        raise ValueError(f"max_length must be positive, got {max_length}")

    def key(result: str) -> str:
        if not isinstance(result, str):
            raise TypeError(
                f"truncate() requires a string, got {type(result).__name__}"
            )
        if len(result) <= max_length:
            return result
        return result[: max_length - len(suffix)] + suffix

    return key


def remove_whitespace(value: str) -> str:
    """Remove all whitespace.

    :param value: Input string.
    :return: String with all whitespace removed.
    :raises TypeError: If result is not a string.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"remove_whitespace() requires a string, got {type(value).__name__}"
        )
    return "".join(value.split())


def prefix(text: str) -> Callable[[str], str]:
    """Add a prefix to the result.

    Example:
        >>> field("word", key=prefix("user_"))
        'user_order'

    :param text: Prefix text to add.
    :return: A closure that adds a prefix.
    :raises TypeError: If result is not a string.
    """

    def key(v: str) -> str:
        if not isinstance(v, str):
            raise TypeError(f"prefix() requires a string, got {type(v).__name__}")
        return f"{text}{v}"

    return key


def suffix(text: str) -> Callable[[str], str]:
    """Add a suffix to the result.

    Example:
        >>> field("word", key=suffix(".io"))
        'ecipe.io'

    :param text: Suffix text to add.
    :return: A closure that adds a suffix.
    :raises TypeError: If result is not a string.
    """

    def key(v: str) -> str:
        if not isinstance(v, str):
            raise TypeError(f"suffix() requires a string, got {type(v).__name__}")
        return f"{v}{text}"

    return key


def hash_with(algorithm: str = "sha256") -> Callable[[str], str]:
    """Return a function that hashes a string using the given algorithm.

    Supported algorithms are those available in :mod:`hashlib.algorithms_available`.

    Example:
        >>> field("password", key=hash_with("sha1"))
        'd3e7130d657733468b10c1fd207c4d62b7180cda'

    :param algorithm: Hash algorithm name.
    :return: A closure that hashes a string.
    :raises ValueError: If algorithm is not supported.
    :raises TypeError: If result is not a string.
    """
    if algorithm not in hashlib.algorithms_available:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")

    def key(v: str) -> str:
        if not isinstance(v, str):
            raise TypeError(f"hash_with() requires a string, got {type(v).__name__}")
        hash_func = hashlib.new(algorithm)
        hash_func.update(v.encode())
        return hash_func.hexdigest()

    return key


def base64_encode(value: str) -> str:
    """Encode as base64.

    :param value: Input string to encode.
    :return: Base64 encoded string.
    :raises TypeError: If result is not a string.

    Example:
        >>> field("word", key=base64_encode)
        'cHJlcGFyZWQ='

    """
    if not isinstance(value, str):
        raise TypeError(
            f"base64_encode() requires a string, got {type(value).__name__}"
        )
    return base64.b64encode(value.encode()).decode()


def urlsafe_base64_encode(value: str) -> str:
    """Encode as URL-safe base64.

    :param value: Input string to encode.
    :return: URL-safe base64 encoded string.
    :raises TypeError: If result is not a string.

    Example:
        >>> field("word", key=urlsafe_base64_encode)
        'YXBwZWFscw=='
    """
    if not isinstance(value, str):
        raise TypeError(
            f"urlsafe_base64_encode() requires a string, got {type(value).__name__}"
        )
    return base64.urlsafe_b64encode(value.encode()).decode()


def redact(replacement: str = "[REDACTED]") -> Callable[[Any], str]:
    """Replace the entire value with redaction marker.

    Example:
        >>> field("password", key=redact("[CLASSIFIED]"))
        '[CLASSIFIED]'

    :param replacement: Replacement text.
    :return: A closure that returns the replacement text.
    """

    def key(_: Any) -> str:
        return replacement

    return key


def join(sep: str = ", ") -> Callable[[list[Any]], str]:
    """Join list items with separator.

    Example:
        >>> field("words", quantity=3, key=join(" | "))
        'pci | promise | excel'

    :param sep: Separator string.
    :return: A closure that joins items.
    :raises TypeError: If the result is not iterable.
    """

    def key(v: Iterable[Any]) -> str:
        if not isinstance(v, Iterable):
            raise TypeError(f"join() requires an iterable, got {type(v).__name__}")
        return sep.join(str(item) for item in v)

    return key


def apply_if(
    condition: Callable[[Any], bool],
    transform: Callable[[Any], Any],
    otherwise: Callable[[Any], Any] | None = None,
) -> Callable[[Any], Any]:
    """Apply transform only if the condition is true.

    Example:
        >>> field("word", key=apply_if(lambda x: len(x) > 3, str.upper, str.lower))
        'FIELDS'

    :param condition: Condition function.
    :param transform: Transform function to apply if condition is True.
    :param otherwise: Optional transform to apply if condition is False.
    :return: A closure that conditionally transforms the result.
    """

    def key(v: Any) -> Any:
        if condition(v):
            return transform(v)
        if otherwise:
            return otherwise(v)
        return v

    return key


#: Dispatch modes for key functions.
_MODE_VALUE_ONLY = 0
_MODE_RANDOM_POSITIONAL = 1
_MODE_RANDOM_KEYWORD = 2
_MODE_LEGACY = 3

#: Resolved dispatch modes, cached per callable.
_dispatch_mode_cache: dict[Any, int] = {}


def _resolve_dispatch_mode(func: KeyFunc) -> int:
    """Decide how to call a key function based on the signature it exposes.

    The exposed signature is the one reported by :func:`inspect.signature`,
    which already accounts for partially bound arguments, decorators using
    :func:`functools.wraps` and bound methods.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return _MODE_LEGACY

    parameters = list(signature.parameters.values())
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]

    if len(positional) >= 2 and positional[1].name == "random":
        return _MODE_RANDOM_POSITIONAL

    if any(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.name == "random"
        for parameter in parameters
    ):
        return _MODE_RANDOM_KEYWORD

    required = [
        parameter
        for parameter in positional
        if parameter.default is inspect.Parameter.empty
    ]
    if len(required) >= 2 or any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    ):
        return _MODE_RANDOM_POSITIONAL

    return _MODE_VALUE_ONLY


def _dispatch_mode(func: KeyFunc) -> int:
    """Return the (cached) dispatch mode for a key function."""
    try:
        mode = _dispatch_mode_cache.get(func)
    except TypeError:  # Unhashable callable.
        return _resolve_dispatch_mode(func)

    if mode is None:
        mode = _resolve_dispatch_mode(func)
        with contextlib.suppress(TypeError):  # Unhashable callable.
            _dispatch_mode_cache[func] = mode

    return mode


def _call_key_func(
    func: KeyFunc,
    result: Any,
    random: Random | None,
) -> Any:
    """Call a key function according to the signature it exposes.

    - Callables exposing a single parameter are called as ``func(result)``.
    - Callables exposing a second positional parameter (conventionally named
      ``random``) are called as ``func(result, random)``.
    - Callables exposing a keyword-only ``random`` parameter are called as
      ``func(result, random=random)``.

    Each callable is invoked exactly once, so a ``TypeError`` raised from
    inside the key function itself always propagates to the caller.
    """
    mode = _dispatch_mode(func)

    if mode == _MODE_VALUE_ONLY:
        return func(result)  # type: ignore[call-arg]

    if mode == _MODE_RANDOM_POSITIONAL:
        return func(result, random)  # type: ignore[call-arg]

    if mode == _MODE_RANDOM_KEYWORD:
        return func(result, random=random)  # type: ignore[call-arg]

    # Legacy fallback for callables that cannot be inspected: try
    # ``func(result, random)`` first and fall back to ``func(result)``
    # only when the ``TypeError`` was raised by the call itself (wrong
    # arity), not from inside the key function.
    try:
        return func(result, random)  # type: ignore[call-arg]
    except TypeError as err:
        if err.__traceback__ is not None and err.__traceback__.tb_next is not None:
            raise
        return func(result)  # type: ignore[call-arg]


def _chain_version(functions: tuple[KeyFunc, ...]) -> int:
    """Compute a version fingerprint for a chain of key functions."""
    parts: list[int] = []
    for func in functions:
        version = getattr(func, "version", None)
        parts.append(version if isinstance(version, int) else id(func))
    return hash(tuple(parts))


class PipeCheckpoint:
    """Progress snapshot of a failed :func:`pipe` run.

    Records the stage at which the run failed, the value that stage
    received when it was first entered and the state of the random
    generator at that moment, so that the run can later be resumed from
    the failed stage — possibly through another field with the same
    definition.

    :ivar version: Version of the transform chain the checkpoint belongs to.
    :ivar stage_index: Index of the failed stage (0-based).
    :ivar value: The value the failed stage received on first entry.
    :ivar random_state: The random state captured on first entry of the stage.
    """

    __slots__ = ("random_state", "stage_index", "value", "version")

    def __init__(
        self,
        *,
        version: int,
        stage_index: int,
        value: Any,
        random_state: tuple[Any, ...] | None,
    ) -> None:
        """Initialize the checkpoint."""
        self.version = version
        self.stage_index = stage_index
        self.value = value
        self.random_state = random_state

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"stage_index={self.stage_index}, version={self.version})"
        )


class PipeKey:
    """A callable chain of key functions with resumable failures.

    Applies each function in sequence, passing the random instance to the
    functions that accept it. If a stage fails, the original exception
    propagates unchanged and the progress is recorded in
    :attr:`PipeKey.checkpoint`, so the run can later be resumed from the
    failed stage: stages that already completed are not executed again,
    and the failed stage receives the value and the random state captured
    when it was first entered.

    The checkpoint can be handed to a newly created field with the same
    definition (simply reuse this key), or attached to another pipe built
    from the same transform chain. Resuming with a chain whose version
    has changed raises :class:`mimesis.exceptions.PipeCheckpointError`.
    """

    __slots__ = ("_checkpoint", "_functions", "_version")

    def __init__(
        self,
        functions: Iterable[KeyFunc] = (),
        checkpoint: PipeCheckpoint | None = None,
    ) -> None:
        """Initialize the pipe.

        :param functions: Key functions to pipe together.
        :param checkpoint: An optional checkpoint to resume from.
        """
        self._functions = tuple(functions)
        self._version = _chain_version(self._functions)
        self._checkpoint: PipeCheckpoint | None = None
        if checkpoint is not None:
            self.attach(checkpoint)

    def __call__(self, result: Any, random: Random | None = None) -> Any:
        """Apply the chain, resuming from the checkpoint if one is pending."""
        checkpoint = self._checkpoint
        if checkpoint is not None:
            return self._resume(checkpoint, random)
        return self._run(result, random, start=0)

    @property
    def functions(self) -> tuple[KeyFunc, ...]:
        """The piped key functions."""
        return self._functions

    @property
    def version(self) -> int:
        """Version fingerprint of the transform chain."""
        return self._version

    @property
    def checkpoint(self) -> PipeCheckpoint | None:
        """The recorded progress, or ``None`` if nothing is pending."""
        return self._checkpoint

    def attach(self, checkpoint: PipeCheckpoint) -> None:
        """Attach a checkpoint recorded by a pipe with the same chain version.

        :param checkpoint: The checkpoint to resume from.
        :raises PipeCheckpointError: If the transform chain version has changed.
        """
        if checkpoint.version != self._version:
            raise PipeCheckpointError(
                "The checkpoint belongs to a different transform chain version."
            )
        self._checkpoint = checkpoint

    def resume(
        self,
        random: Random | None = None,
        *,
        checkpoint: PipeCheckpoint | None = None,
    ) -> Any:
        """Resume the run from the recorded (or given) checkpoint.

        :param random: The random instance to restore the state into.
        :param checkpoint: An explicit checkpoint to resume from.
        :raises PipeCheckpointError: If there is no checkpoint to resume from,
            or if the transform chain version has changed.
        :return: The final result of the chain.
        """
        if checkpoint is not None:
            self.attach(checkpoint)
        if self._checkpoint is None:
            raise PipeCheckpointError("There is no checkpoint to resume from.")
        return self._resume(self._checkpoint, random)

    def _resume(self, checkpoint: PipeCheckpoint, random: Random | None) -> Any:
        if checkpoint.version != self._version:
            raise PipeCheckpointError(
                "The checkpoint belongs to a different transform chain version."
            )
        if random is not None and checkpoint.random_state is not None:
            random.setstate(checkpoint.random_state)
        return self._run(checkpoint.value, random, start=checkpoint.stage_index)

    def _run(self, result: Any, random: Random | None, start: int) -> Any:
        functions = self._functions
        for index in range(start, len(functions)):
            random_state = random.getstate() if random is not None else None
            try:
                result = _call_key_func(functions[index], result, random)
            except BaseException:
                self._checkpoint = PipeCheckpoint(
                    version=self._version,
                    stage_index=index,
                    value=result,
                    random_state=random_state,
                )
                raise
        self._checkpoint = None
        return result


def pipe(*functions: KeyFunc, checkpoint: PipeCheckpoint | None = None) -> PipeKey:
    """Pipe multiple key functions together.

    Example:
        >>> field("full_name", key=pipe(str.lower, slugify, prefix("user-")))
        'user-john-doe'

    The returned callable records its progress when a stage fails, so the
    run can later be resumed from the failed stage — see :class:`PipeKey`.

    :param functions: Key functions to pipe together.
    :param checkpoint: An optional checkpoint to resume from.
    :return: A callable that applies all functions in sequence.
    """
    return PipeKey(functions, checkpoint=checkpoint)
