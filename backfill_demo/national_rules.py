"""Country-specific rules (layer 3) used by the backfill demo.

Two rule families are provided:

* ``BEBankRule``  - national check digit plus a lookup in a local bank
  directory, mirroring ``stdnum/be/iban.py`` (national mod-97 check and an
  ``InvalidComponent`` when the bank code is unknown).
* ``XKCheckRule`` - a versioned national checksum over the BBAN core.
  Version ``v1`` uses modulus 97; version ``v2`` tightens the rule to
  modulus 89.  The generic IBAN layers are untouched, so a number can stay
  generically valid while its national verdict flips.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def compact(number: str) -> str:
    return re.sub(r"[\s\-.]", "", number).upper()


@dataclass(frozen=True)
class RuleResult:
    ok: bool
    reason: str | None = None


class NationalRule:
    country: str
    version: str

    def check(self, number: str, snapshot) -> RuleResult:  # pragma: no cover
        raise NotImplementedError


def _bank_known(bank_code: str, path) -> bool:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rng = line.split("|", 1)[0]
        if "-" in rng:
            low, high = rng.split("-")
            if low <= bank_code <= high:
                return True
        elif rng == bank_code:
            return True
    return False


@dataclass(frozen=True)
class BEBankRule(NationalRule):
    country: str = "BE"
    version: str = "be-nbb-2026-01"

    def check(self, number: str, snapshot) -> RuleResult:
        number = compact(number)
        bban = number[4:]
        expected = int(bban[:-2]) % 97 or 97
        if bban[-2:] != "%02d" % expected:
            return RuleResult(False, "national check digits wrong")
        if not _bank_known(bban[:3], snapshot.bank_dirs["BE"]):
            return RuleResult(False, "bank code not in directory")
        return RuleResult(True, "national check + bank directory ok")


@dataclass(frozen=True)
class XKCheckRule(NationalRule):
    country: str = "XK"
    version: str = "xk-v1-mod97"
    modulus: int = 97

    def check(self, number: str, snapshot) -> RuleResult:
        number = compact(number)
        bban = number[4:]
        expected = int(bban[:-2]) % self.modulus or self.modulus
        if bban[-2:] != "%02d" % expected:
            return RuleResult(False, "fails %s (mod %d)" % (self.version, self.modulus))
        return RuleResult(True, "passes %s" % self.version)
