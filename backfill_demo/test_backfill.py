"""Tests pinning the backfill experiment's claims."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (  # noqa: E402
    FULL_LEVEL,
    GENERIC_LEVEL,
    Snapshot,
    assert_matches_stdnum,
    build_iban,
    is_valid,
    validate,
)
from national_rules import BEBankRule, XKCheckRule  # noqa: E402
from run_demo import (  # noqa: E402
    RECORDS,
    UNKNOWN,
    V1,
    V2,
    classify,
)


def test_snapshots_are_distinct_but_generic_registry_identical():
    assert V1.registry_hash == V2.registry_hash
    assert V1.data_hashes["BE"] != V2.data_hashes["BE"]
    assert V1.rules["XK"].version != V2.rules["XK"].version


def test_variation_one_national_rule_flips_while_generic_stays():
    number = RECORDS["xk_rule_change"]
    assert is_valid(number, V1, GENERIC_LEVEL) is True
    assert is_valid(number, V2, GENERIC_LEVEL) is True
    assert is_valid(number, V1, FULL_LEVEL) is True
    assert is_valid(number, V2, FULL_LEVEL) is False


def test_variation_two_data_change_does_not_affect_this_number():
    stable = RECORDS["be_stable"]
    entrant = RECORDS["be_new_entrant"]
    assert is_valid(stable, V1, FULL_LEVEL) is True
    assert is_valid(stable, V2, FULL_LEVEL) is True
    assert is_valid(entrant, V1, FULL_LEVEL) is False
    assert is_valid(entrant, V2, FULL_LEVEL) is True


def test_variation_three_flag_off_boolean_hides_the_choice():
    number = RECORDS["be_unknown_bank"]
    assert is_valid(number, V1, GENERIC_LEVEL) is True
    assert is_valid(number, V1, FULL_LEVEL) is False
    assert validate(number, V1, GENERIC_LEVEL).national_ok is None


def test_two_histories_store_same_bool_but_require_different_actions():
    good = RECORDS["be_stable"]
    shadow = RECORDS["be_unknown_bank"]
    assert is_valid(good, V1, FULL_LEVEL) == is_valid(shadow, V1, GENERIC_LEVEL) is True
    assert is_valid(good, V2, FULL_LEVEL) is True
    assert is_valid(shadow, V2, FULL_LEVEL) is False


def test_migration_classes():
    assert classify(RECORDS["bad_checksum"], V1, V2).startswith("INHERIT")
    assert classify(RECORDS["de_no_module"], V1, V2).startswith("FORMAT-ONLY")
    assert classify(RECORDS["xk_rule_change"], V1, V2).startswith("RECHECK-NATIONAL")
    assert classify(RECORDS["be_stable"], V1, V2).startswith("RECHECK-NATIONAL")


def test_account_existence_is_always_unknown():
    assert UNKNOWN == "UNKNOWN"
    verdict = validate(RECORDS["be_stable"], V2, FULL_LEVEL)
    assert not hasattr(verdict, "account_exists")


def test_generic_layer_matches_real_stdnum():
    for label in ("be_stable", "be_new_entrant", "be_unknown_bank", "de_no_module", "bad_checksum"):
        assert_matches_stdnum(RECORDS[label])


def test_engine_rejects_corrupted_checksum_but_keeps_structure_verdict():
    verdict = validate(RECORDS["bad_checksum"], V1, GENERIC_LEVEL)
    assert verdict.checksum_ok is False
    assert verdict.structure_ok is True
    assert is_valid(RECORDS["bad_checksum"], V1, GENERIC_LEVEL) is False


def test_snapshot_manifest_carries_versioned_fingerprints():
    manifest = V2.manifest()
    assert set(manifest) == {"snapshot", "registry_hash", "rule_versions", "data_hashes"}
    assert manifest["rule_versions"]["BE"] == BEBankRule().version
    assert manifest["rule_versions"]["XK"] == "xk-v2-mod89"


def test_unknown_country_structure_is_none():
    number = build_iban("ZZ", "1234567890123456")
    verdict = validate(number, V1, GENERIC_LEVEL)
    assert verdict.structure_ok is None
    assert is_valid(number, V1, GENERIC_LEVEL) is False


@pytest.mark.parametrize("snapshot", (V1, V2))
def test_built_numbers_are_well_formed(snapshot: Snapshot):
    for label, number in RECORDS.items():
        if label == "bad_checksum":
            continue
        assert validate(number, snapshot, GENERIC_LEVEL).checksum_ok is True
