"""
Named-slot template rendering on top of a Faker ``Generator``.

A template is a string, dict, or list built from ordinary Faker provider
tokens (``{{ formatter:args }}``) plus named slots:

* ``{{ let customer_id: bothify:CID-#### }}`` declares a slot. The token
  itself renders to an empty string; the generated value is memoized in the
  current scope and reused every time ``{{ customer_id }}`` is referenced.
* A bare token (``{{ customer_id }}``) resolves to the innermost slot with
  that name, walking outwards through enclosing scopes. If no slot matches,
  it falls back to a provider method of the same name.
* A detail line (a dict inside a list) runs in its own scope. It may declare
  a slot sharing the name of an outer slot, which shadows the outer one for
  that detail only.
* A detail dict may carry an ``"optional"`` control key (bool, probability
  between 0 and 1, or truthy string). Details that are not selected are
  skipped before any provider is called, so they generate no data.

Slot sources are evaluated lazily on first use, so forward references
(referencing a slot declared later in the same scope) produce exactly the
same value as declaring it first. Circular references, including cycles that
cross scope boundaries, raise :class:`SlotCycleError` naming every slot
involved.
"""

from __future__ import annotations

import ast
import re

from typing import Any, List, Optional, Tuple

from .exceptions import BaseFakerException

_TOKEN_RE = re.compile(r"\{\{\s*(?P<body>.*?)\s*\}\}")
_SLOT_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
_OPTIONAL_KEY = "optional"
_LET_KEYWORD = "let"


class TemplateError(BaseFakerException):
    """Raised when a template is malformed or references an unknown slot."""


class SlotCycleError(TemplateError):
    """Raised when resolving a slot leads back to a slot already being resolved."""


class _Frame:
    """One lexical scope: declared slot sources and their memoized values."""

    def __init__(self, label: str, parent: Optional["_Frame"] = None) -> None:
        self.label = label
        self.parent = parent
        self.specs: dict[str, str] = {}
        self.values: dict[str, Any] = {}

    def find(self, name: str) -> Optional["_Frame"]:
        frame: Optional[_Frame] = self
        while frame is not None:
            if name in frame.specs or name in frame.values:
                return frame
            frame = frame.parent
        return None


def _split_body(body: str) -> List[str]:
    # maxsplit keeps colons that belong to provider arguments, e.g.
    # ``date:Y-m-d`` -> ["date", "Y-m-d"].
    return [part.strip() for part in body.split(":", 1)]


def _parse_let(body: str) -> Optional[Tuple[str, str]]:
    """
    Parse a ``let <name>: <source>`` token, returning (name, source).

    Returns ``None`` when the token is not a ``let`` declaration. A token that
    starts with the ``let`` keyword but is malformed raises :class:`TemplateError`.
    """
    if body != _LET_KEYWORD and not body.startswith(_LET_KEYWORD + " ") and not body.startswith(_LET_KEYWORD + ":"):
        return None

    remainder = body[len(_LET_KEYWORD) :].lstrip()
    name, separator, source = remainder.partition(":")
    name = name.strip()
    source = source.strip()
    if not separator or not name or not source:
        raise TemplateError(
            f"Slot declaration {body!r} must follow the syntax 'let <name>: <provider token>'"
        )
    if not _SLOT_NAME_RE.match(name):
        raise TemplateError(f"Invalid slot name {name!r}")
    return name, source


def _coerce_argument(raw: str) -> Any:
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _parse_arguments(raw: str) -> Tuple[list, dict]:
    args: list = []
    kwargs: dict = {}
    if not raw.strip():
        return args, kwargs
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            kwargs[key.strip()] = _coerce_argument(value.strip())
        else:
            args.append(_coerce_argument(token))
    return args, kwargs


class TemplateRenderer:
    """Renders named-slot templates against a Faker :class:`~faker.generator.Generator`."""

    def __init__(self, generator: Any) -> None:
        self._generator = generator
        self._resolving: List[Tuple["_Frame", str]] = []

    def render(self, template: Any, variables: Optional[dict] = None) -> Any:
        """Render one document in a fresh, isolated root scope."""
        root = _Frame("root")
        if variables:
            root.values.update(variables)
        try:
            return self._render_value(template, root)
        finally:
            # A failed detail must never leave half-resolved state behind.
            self._resolving.clear()

    def render_many(self, templates: list, variables: Optional[dict] = None) -> list:
        """Render several documents, each in its own isolated root scope."""
        return [self.render(template, variables) for template in templates]

    # ------------------------------------------------------------------
    # Value dispatch
    # ------------------------------------------------------------------
    def _render_value(self, value: Any, frame: _Frame) -> Any:
        if isinstance(value, str):
            return self._render_string(value, frame)
        if isinstance(value, dict):
            return self._render_dict(value, frame)
        if isinstance(value, list):
            return self._render_list(value, frame)
        return value

    def _render_string(self, text: str, frame: _Frame) -> str:
        self._register_slots(text, frame)

        def replace(match: re.Match) -> str:
            body = match.group("body").strip()
            if _parse_let(body) is not None:
                return ""
            return self._eval_body(body, frame)

        return _TOKEN_RE.sub(replace, text)

    def _render_dict(self, data: dict, frame: _Frame) -> dict:
        # Register every slot in this scope up front so references that appear
        # earlier in the document can still resolve later declarations.
        for key, value in data.items():
            if key != _OPTIONAL_KEY:
                self._collect_slots(value, frame)

        rendered: dict = {}
        for key, value in data.items():
            if key == _OPTIONAL_KEY:
                continue
            if isinstance(value, list):
                rendered[key] = self._render_list(value, frame, key)
            else:
                rendered[key] = self._render_value(value, frame)
        return rendered

    def _collect_slots(self, value: Any, frame: _Frame) -> None:
        if isinstance(value, str):
            self._register_slots(value, frame)
        elif isinstance(value, dict):
            for nested in value.values():
                if not isinstance(nested, list):
                    self._collect_slots(nested, frame)

    def _render_list(self, items: list, parent: _Frame, label: str = "detail") -> list:
        rendered: list = []
        for index, item in enumerate(items):
            frame = _Frame(f"{parent.label}.{label}[{index}]", parent)
            if isinstance(item, dict) and _OPTIONAL_KEY in item:
                if not self._is_selected(item[_OPTIONAL_KEY]):
                    # Unselected details are dropped without generating data.
                    continue
            rendered.append(self._render_value(item, frame))
        return rendered

    # ------------------------------------------------------------------
    # Slot registration and resolution
    # ------------------------------------------------------------------
    def _register_slots(self, text: str, frame: _Frame) -> None:
        for match in _TOKEN_RE.finditer(text):
            parsed = _parse_let(match.group("body").strip())
            if parsed is None:
                continue
            name, source = parsed
            existing = frame.specs.get(name)
            if existing is not None and existing != source:
                raise TemplateError(f"Slot {name!r} is declared more than once in scope {frame.label!r}")
            frame.specs.setdefault(name, source)

    def _resolve_slot(self, name: str, frame: _Frame) -> Any:
        owner = frame.find(name)
        if owner is None:
            return self._fallback_provider(name)

        if name in owner.values:
            return owner.values[name]

        entry = (owner, name)
        if entry in self._resolving:
            self._raise_cycle(owner, name)

        self._resolving.append(entry)
        try:
            # Resolve names inside the source from the frame that triggered
            # first use, so references can see slots declared in nested detail
            # scopes. This is also what makes cross-layer cycles reachable.
            value = self._eval_body(owner.specs[name], frame, self_name=name)
        finally:
            self._resolving.pop()
        owner.values[name] = value
        return value

    def _raise_cycle(self, owner: _Frame, name: str) -> None:
        start = next(i for i, entry in enumerate(self._resolving) if entry == (owner, name))
        chain = [f"{frame.label}.{slot}" for frame, slot in self._resolving[start:]]
        chain.append(f"{owner.label}.{name}")
        raise SlotCycleError(f"Circular slot reference detected: {' -> '.join(chain)}")

    def _fallback_provider(self, name: str) -> str:
        formatter = getattr(self._generator, name, None)
        if callable(formatter) and not name.startswith("_"):
            return str(formatter())
        raise TemplateError(f"Unknown slot {name!r}")

    # ------------------------------------------------------------------
    # Token evaluation
    # ------------------------------------------------------------------
    def _eval_body(self, body: str, frame: _Frame, self_name: Optional[str] = None) -> str:
        parts = _split_body(body)
        if len(parts) == 1:
            name = parts[0]
            # Inside a slot's own source, a reference to its own name invokes
            # the provider method instead of recursing into the slot.
            if name == self_name:
                return self._fallback_provider(name)
            return str(self._resolve_slot(name, frame))
        formatter, raw_arguments = parts
        args, kwargs = _parse_arguments(raw_arguments)
        return str(self._generator.format(formatter, *args, **kwargs))

    def _is_selected(self, option: Any) -> bool:
        if isinstance(option, bool):
            return option
        if isinstance(option, (int, float)):
            if not 0 <= option <= 1:
                raise TemplateError(f"Optional probability must be between 0 and 1, got {option!r}")
            return self._generator.random.random() < option
        if isinstance(option, str):
            normalized = option.strip().lower()
            if normalized in ("true", "yes", "1"):
                return True
            if normalized in ("false", "no", "0"):
                return False
            try:
                probability = float(normalized)
            except ValueError as exc:
                raise TemplateError(f"Invalid optional marker {option!r}") from exc
            return self._is_selected(probability)
        raise TemplateError(f"Invalid optional marker {option!r}")
