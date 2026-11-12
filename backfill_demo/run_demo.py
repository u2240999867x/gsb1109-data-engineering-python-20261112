"""End-to-end backfill experiment on historical IBAN records.

Run with:  .venv/bin/python backfill_demo/run_demo.py

The script builds a fixed set of IBANs, stores them through two "offline
dictionaries" that only persist an ``is_valid`` boolean, and shows:

  1. three kinds of change between snapshots;
  2. two histories that stored the same boolean but demand different
     migration actions;
  3. the migration class of every record;
  4. the minimum evidence needed for precise incremental revalidation.

Account existence is never evaluated: no offline data can answer it, so it
is carried as a separate ``UNKNOWN`` value.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (  # noqa: E402
    FULL_LEVEL,
    GENERIC_LEVEL,
    Snapshot,
    build_iban,
    compact,
    is_valid,
    set_crosscheck_snapshot,
    validate,
)
from national_rules import BEBankRule, XKCheckRule  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
UNKNOWN = "UNKNOWN"


def make_snapshot(name: str, registry: str, be_banks: str, xk_rule: XKCheckRule) -> Snapshot:
    return Snapshot(
        name=name,
        registry_path=DATA / registry,
        bank_dirs={"BE": DATA / be_banks},
        rules={"BE": BEBankRule(), "XK": xk_rule},
    )


V1 = make_snapshot("v1", "registry_v1.txt", "be_banks_v1.dat", XKCheckRule())
V2 = make_snapshot(
    "v2",
    "registry_v2.txt",
    "be_banks_v2.dat",
    XKCheckRule(version="xk-v2-mod89", modulus=89),
)
set_crosscheck_snapshot(V1)


@dataclass(frozen=True)
class HistoricalRecord:
    """What a careful pipeline *could* have kept; the legacy table itself
    only kept the last field."""

    iban: str
    checked_at: str
    snapshot_name: str
    level_used: str
    is_valid: bool
    account_exists: str = UNKNOWN


def be_iban(bank: str, account: str) -> str:
    national = int(bank + account) % 97 or 97
    return build_iban("BE", bank + account + "%02d" % national)


def xk_iban(bank: str, account: str) -> str:
    core = bank + account
    national = int(core) % 97 or 97
    return build_iban("XK", core + "%02d" % national)


RECORDS = {
    "be_stable": be_iban("435", "4111611"),       # bank 435 covered in both dirs
    "be_new_entrant": be_iban("750", "0754703"),  # bank 750 only in v2
    "be_unknown_bank": be_iban("999", "0000001"), # bank 999 in neither dir
    "xk_rule_change": xk_iban("1234", "0000001234"),  # valid under mod97 only
    "de_no_module": build_iban("DE", "370400440532013000"),
    "bad_checksum": "BE32435411161155",           # one IBAN check digit flipped
}


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_verdict(label: str, number: str, snapshot: Snapshot) -> None:
    generic = validate(number, snapshot, GENERIC_LEVEL)
    full = validate(number, snapshot, FULL_LEVEL)
    print(
        "%-16s %s  generic=%-5s (L1=%s L2=%s)  full=%-5s (L3=%s %s)"
        % (
            label,
            compact(number),
            is_valid(number, snapshot, GENERIC_LEVEL),
            generic.checksum_ok,
            generic.structure_ok,
            is_valid(number, snapshot, FULL_LEVEL),
            full.national_ok,
            full.national_reason,
        )
    )


def variation_one_rule_changes() -> None:
    banner("Variation 1: generic checks unchanged, national rule changes (XK mod97 -> mod89)")
    number = RECORDS["xk_rule_change"]
    show_verdict("v1", number, V1)
    show_verdict("v2", number, V2)
    g1 = is_valid(number, V1, GENERIC_LEVEL)
    g2 = is_valid(number, V2, GENERIC_LEVEL)
    f1 = is_valid(number, V1, FULL_LEVEL)
    f2 = is_valid(number, V2, FULL_LEVEL)
    assert g1 is g2 is True, "generic verdict must remain stable"
    assert f1 is True and f2 is False, "national verdict must flip"
    print("-> L1/L2 identical across snapshots; only the versioned L3 rule flips the result.")


def variation_two_data_changes_elsewhere() -> None:
    banner("Variation 2: reference data changed, but not the slice this number uses (BE 435)")
    stable = RECORDS["be_stable"]
    entrant = RECORDS["be_new_entrant"]
    print("BE bank dir hash  v1=%s  v2=%s" % (V1.data_hashes["BE"], V2.data_hashes["BE"]))
    show_verdict("be_stable v1", stable, V1)
    show_verdict("be_stable v2", stable, V2)
    show_verdict("be_new v1", entrant, V1)
    show_verdict("be_new v2", entrant, V2)
    assert is_valid(stable, V1, FULL_LEVEL) is is_valid(stable, V2, FULL_LEVEL) is True
    assert is_valid(entrant, V1, FULL_LEVEL) is False
    assert is_valid(entrant, V2, FULL_LEVEL) is True
    print("-> whole-file hash changed, yet bank-435 verdicts stay valid; a file hash alone")
    print("   cannot tell the two records apart - subrange-level evidence (000-499) is needed.")


def variation_three_flag_off_no_trace() -> None:
    banner("Variation 3: historical run had check_country off, and the boolean kept no trace")
    number = RECORDS["be_unknown_bank"]
    show_verdict("unknown-bank", number, V1)
    legacy_row = HistoricalRecord(
        iban=number,
        checked_at="2026-01-10",
        snapshot_name="v1",
        level_used=GENERIC_LEVEL,  # this column was NOT persisted in the old table
        is_valid=is_valid(number, V1, GENERIC_LEVEL),
    )
    print("legacy table row kept only: iban=%s is_valid=%s" % (legacy_row.iban, legacy_row.is_valid))
    print("if the same run had used full validation, is_valid would be %s"
          % is_valid(number, V1, FULL_LEVEL))
    assert legacy_row.is_valid is True
    assert is_valid(number, V1, FULL_LEVEL) is False
    print("-> identical stored True as a fully-valid BE record, but revalidation flips it;")
    print("   the missing check_country flag makes the two histories indistinguishable offline.")


def two_histories_same_bool() -> None:
    banner("Two histories, identical stored boolean, different migration actions")
    good = HistoricalRecord(
        RECORDS["be_stable"], "2026-01-10", "v1", FULL_LEVEL,
        is_valid(RECORDS["be_stable"], V1, FULL_LEVEL),
    )
    shadow = HistoricalRecord(
        RECORDS["be_unknown_bank"], "2026-01-10", "v1", GENERIC_LEVEL,
        is_valid(RECORDS["be_unknown_bank"], V1, GENERIC_LEVEL),
    )
    for name, row in (("H1 flag-on, bank 435", good), ("H2 flag-off, bank 999", shadow)):
        print(
            "%-24s stored is_valid=%s  (level_used=%s, snapshot=%s)"
            % (name, row.is_valid, row.level_used, row.snapshot_name)
        )
    assert good.is_valid == shadow.is_valid is True
    print("\nrequired migration actions:")
    print("  H1: re-run L3 against v2 (or exempt with unchanged-range evidence); verdict stays True.")
    print("      re-check result = %s" % is_valid(good.iban, V2, FULL_LEVEL))
    print("  H2: cannot inherit; full revalidation mandatory including L3; verdict flips to False.")
    print("      re-check result = %s" % is_valid(shadow.iban, V2, FULL_LEVEL))


def classify(number: str, snapshot_v1: Snapshot, snapshot_v2: Snapshot) -> str:
    """Migration class for one legacy record, assuming evidence is available."""
    v1_generic = validate(number, snapshot_v1, GENERIC_LEVEL)
    v2_full = validate(number, snapshot_v2, FULL_LEVEL)
    country = compact(number)[:2]
    has_national_module = country in snapshot_v2.rules

    if not v1_generic.checksum_ok or v1_generic.structure_ok is False:
        return "INHERIT: failure sits on immutable L1/L2 evidence (recompute to confirm)"
    if not has_national_module:
        return "FORMAT-ONLY CEILING: no national module exists; recheck can prove format at most"
    if v2_full.national_ok is None:
        return "FORMAT-ONLY CEILING: national layer unavailable"
    return "RECHECK-NATIONAL: L1/L2 can be inherited; L3 must be re-run on current rule+data"


def migration_classes() -> None:
    banner("Migration classification of every record")
    for label, number in sorted(RECORDS.items()):
        print("%-16s %s -> %s" % (label, compact(number), classify(number, V1, V2)))
    print("\naccount_exists for every record: %s (offline validation never opens an account)" % UNKNOWN)


def minimal_evidence() -> None:
    banner("Minimum evidence for precise incremental revalidation")
    for item in (
        "1. normalized IBAN itself, so immutable L1 (ISO 7064 mod97) is recomputable;",
        "2. the level actually run: check_country on/off (the flag the boolean discarded);",
        "3. engine/library version and per-country national rule version;",
        "4. fingerprints of referenced material: registry BBAN row hash and national data hash;",
        "5. the precise subrange/entry consulted (e.g. BE bank range 000-499), not only file hash;",
        "6. a layered verdict (checksum_ok/structure_ok/national_ok + failure layer), not one bool;",
        "7. check timestamp bound to a snapshot manifest so each hash is resolvable later;",
        "8. account existence as a separate field, defaulting to UNKNOWN.",
    ):
        print(" " + item)


def main() -> None:
    print("snapshots:")
    print(" v1 manifest: %s" % V1.manifest())
    print(" v2 manifest: %s" % V2.manifest())
    variation_one_rule_changes()
    variation_two_data_changes_elsewhere()
    variation_three_flag_off_no_trace()
    two_histories_same_bool()
    migration_classes()
    minimal_evidence()


if __name__ == "__main__":
    main()
