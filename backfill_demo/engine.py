"""Minimal swappable IBAN validation engine for the backfill experiment.

It mirrors the three layers of ``stdnum.iban.validate`` (see
``stdnum/iban.py``):

  L1  ISO 7064 Mod 97-10 checksum over the whole IBAN   (immutable standard)
  L2  country BBAN structure from a local registry      (versioned data)
  L3  country-specific national rule / bank directory   (versioned rule+data)

Layer 3 is only executed when the caller asks for ``full`` validation
(``check_country=True`` in stdnum).  The engine never decides whether an
account actually exists; that is a separate, always-unknown fact offline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from national_rules import NationalRule, RuleResult

_STRUCT_RE = re.compile(r"([1-9][0-9]*)!([nac])")
_CHARS = {"n": "[0-9]", "a": "[A-Z]", "c": "[A-Za-z0-9]"}

GENERIC_LEVEL = "generic"
FULL_LEVEL = "full"


def compact(number: str) -> str:
    return re.sub(r"[\s\-.]", "", number).upper()


def mod97_10_checksum(number: str) -> int:
    base10 = "".join(str(int(ch, 36)) for ch in number)
    return int(base10) % 97


def calc_iban_check_digits(country: str, bban: str) -> str:
    rebuilt = bban + country + "00"
    return "%02d" % (98 - mod97_10_checksum(rebuilt))


def build_iban(country: str, bban: str) -> str:
    return country + calc_iban_check_digits(country, bban) + bban


def structure_regex(structure: str) -> re.Pattern[str]:
    pattern = _STRUCT_RE.sub(
        lambda m: "%s{%s}" % (_CHARS[m.group(2)], m.group(1)), structure
    )
    return re.compile("^%s$" % pattern)


def _file_hash(path: Path) -> str:
    """Fingerprint the effective lines, so comment-only edits do not look
    like a rule change."""
    effective = "".join(
        line
        for line in path.read_text().splitlines(keepends=True)
        if line.strip() and not line.lstrip().startswith("#")
    )
    return hashlib.sha256(effective.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Snapshot:
    """A versioned, locally replaceable set of country reference material."""

    name: str
    registry_path: Path
    bank_dirs: dict[str, Path]
    rules: dict[str, NationalRule]
    registry_hash: str = field(init=False)
    data_hashes: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "registry_hash", _file_hash(self.registry_path))
        object.__setattr__(
            self,
            "data_hashes",
            {cc: _file_hash(path) for cc, path in self.bank_dirs.items()},
        )

    def load_registry(self) -> dict[str, str]:
        registry: dict[str, str] = {}
        for line in self.registry_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            country, structure = line.split("|")
            registry[country.strip()] = structure.strip()
        return registry

    def manifest(self) -> dict[str, object]:
        return {
            "snapshot": self.name,
            "registry_hash": self.registry_hash,
            "rule_versions": {cc: rule.version for cc, rule in self.rules.items()},
            "data_hashes": self.data_hashes,
        }


@dataclass(frozen=True)
class LayerVerdict:
    checksum_ok: bool
    structure_ok: bool | None
    national_ok: bool | None
    national_reason: str | None


def validate(number: str, snapshot: Snapshot, level: str) -> LayerVerdict:
    """Run L1 always, L2 always, L3 only at ``full`` level."""
    number = compact(number)
    checksum_ok = mod97_10_checksum(number[4:] + number[:4]) == 1
    structure_ok: bool | None = None
    national_ok: bool | None = None
    national_reason: str | None = None

    registry = snapshot.load_registry()
    structure = registry.get(number[:2])
    if structure is not None:
        structure_ok = bool(structure_regex(structure).match(number[4:]))

    if level == FULL_LEVEL:
        rule = snapshot.rules.get(number[:2])
        if rule is not None:
            result: RuleResult = rule.check(number, snapshot)
            national_ok = result.ok
            national_reason = result.reason
        else:
            national_ok = True
            national_reason = "no national module: flag is moot"

    return LayerVerdict(checksum_ok, structure_ok, national_ok, national_reason)


def is_valid(number: str, snapshot: Snapshot, level: str) -> bool:
    verdict = validate(number, snapshot, level)
    if not verdict.checksum_ok or verdict.structure_ok is not True:
        return False
    if verdict.national_ok is False:
        return False
    return True


def assert_matches_stdnum(number: str) -> None:
    """Cross-check the generic layers against the real library."""
    from stdnum import iban

    generic = is_valid(number, _SNAPSHOT_FOR_CROSSCHECK, GENERIC_LEVEL)
    if generic != iban.is_valid(number, check_country=False):
        raise AssertionError("generic layer diverges from stdnum for %s" % number)


_SNAPSHOT_FOR_CROSSCHECK: Snapshot | None = None


def set_crosscheck_snapshot(snapshot: Snapshot) -> None:
    global _SNAPSHOT_FOR_CROSSCHECK
    _SNAPSHOT_FOR_CROSSCHECK = snapshot
