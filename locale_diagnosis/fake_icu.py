"""Deterministic, controllable stand-in for the real ``icu`` (PyICU) package.

It implements only the surface that ``natsort.compat.locale`` touches, so the
ICU code path can be exercised on machines without PyICU.  The collation rule
table is a process-global, mutable object: tests flip it to emulate a locale
change or an ICU upgrade, while every ``Collator`` instance snapshots the
rules at creation time and records itself in an audit log.
"""

from __future__ import annotations

import itertools

_ID_COUNTER = itertools.count(1)

# Mutable "environment". A real process would change this via setlocale or
# by upgrading ICU; tests call set_rules() to make the change deterministic.
_RULES: dict[str, object] = {
    # Ordering of whole scripts; flip to emulate a locale change.
    "script_order": ("latin", "cyrillic"),
    # "upper_first" or "lower_first".
    "case_order": "upper_first",
}

_CREATE_LOG: list[tuple[int, str, dict[str, object]]] = []

IS_FAKE = True
ICU_VERSION = "fake-1.0"


def reset() -> None:
    """Restore default rules and clear the audit log."""
    _RULES["script_order"] = ("latin", "cyrillic")
    _RULES["case_order"] = "upper_first"
    _CREATE_LOG.clear()


def set_rules(**kwargs: object) -> None:
    """Overwrite part of the process-global collation rules."""
    _RULES.update(kwargs)


def create_log() -> list[tuple[int, str, dict[str, object]]]:
    """Return a copy of every Collator creation event: (id, locale, rules)."""
    return [entry for entry in _CREATE_LOG]


class Locale:
    """Minimal icu.Locale replacement."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name or "root"

    def __str__(self) -> str:
        return self.name


def _script_rank(char: str, script_order: tuple[str, str]) -> int:
    code = ord(char)
    if 0x0400 <= code <= 0x04FF:
        script = "cyrillic"
    elif char.isalpha():
        script = "latin"
    else:
        script = "latin"
    return script_order.index(script)


class Collator:
    """Snapshot-based collator, mirroring PyICU semantics."""

    def __init__(self, locale_name: str) -> None:
        self.identity = next(_ID_COUNTER)
        self.locale_name = locale_name
        # The decisive line: rules are fixed when the instance is created.
        self.rules_snapshot = dict(_RULES)
        self.numeric_collation = False
        _CREATE_LOG.append((self.identity, locale_name, dict(_RULES)))

    @classmethod
    def createInstance(cls, loc: Locale | str) -> "Collator":  # noqa: N802
        return cls(str(loc))

    def setAttribute(self, attr: object, value: object) -> None:  # noqa: N802
        self.numeric_collation = value == UCollAttributeValue.ON

    def getSortKey(self, text: str) -> bytes:  # noqa: N802
        script_order = self.rules_snapshot["script_order"]
        upper_first = self.rules_snapshot["case_order"] == "upper_first"
        chunks = [b"\x00"]
        for char in text:
            if not char.isalpha():
                chunks.append(bytes([0, ord(char) & 0xFF]))
                continue
            script = _script_rank(char, script_order)
            letter_rank = ord(char.lower())
            case_bit = 0 if (char.isupper() == upper_first) else 1
            # Fixed-width two-byte codepoint keeps byte order aligned with
            # codepoint order without exceeding the byte range.
            chunks.append(bytes([script + 1]) + letter_rank.to_bytes(2, "big")
                          + bytes([case_bit]))
        return b"".join(chunks)


class UCollAttribute:
    NUMERIC_COLLATION = "numeric"


class UCollAttributeValue:
    ON = "on"
    OFF = "off"


class DecimalFormatSymbols:
    """Minimal icu.DecimalFormatSymbols replacement."""

    kGroupingSeparatorSymbol = "group"
    kDecimalSeparatorSymbol = "decimal"

    _SYMBOLS = {"group": ",", "decimal": "."}

    def __init__(self, loc: Locale | str) -> None:
        self.locale_name = str(loc)

    def getSymbol(self, which: str) -> str:  # noqa: N802
        return self._SYMBOLS[which]
