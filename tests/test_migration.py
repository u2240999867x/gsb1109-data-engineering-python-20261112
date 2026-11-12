# tests/test_migration.py
"""Tests for chardet.migration — legacy detection-directory migration."""

from __future__ import annotations

import pytest

from chardet.enums import LanguageFilter
from chardet.migration import (
    MIGRATION_POLICY_VERSION,
    LegacyPolicy,
    LegacyRecord,
    MigrationVerdict,
    migrate_directory,
)

UTF8_SAMPLE = ("héllo wörld, this is a töst of détection. " * 5).encode("utf-8")
ASCII_SAMPLE = b"just a plain ascii sentence for detection."
CP1252_SAMPLE = (
    "Le café est prêt — voilà une naïve œuvre, été après été. " * 4
).encode("cp1252")
BINARY_SAMPLE = b"\x00\x01\x02\x03\x00\xff\x00\xfe"


def _cut_mid_character(sample: bytes) -> bytes:
    """Return a prefix of *sample* that ends inside a multi-byte sequence."""
    for end in range(1, len(sample)):
        prefix = sample[:end]
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            return prefix
    msg = "sample has no splittable multi-byte sequence"
    raise AssertionError(msg)


def test_reuse_full_sample() -> None:
    record = LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE)
    result = migrate_directory([record])
    (migration,) = result.records
    assert migration.verdict is MigrationVerdict.REUSE
    assert migration.encoding == "utf-8"
    assert migration.decodable is True
    assert migration.certified_bytes == len(UTF8_SAMPLE)
    assert migration.sample_complete is True
    assert migration.affected_by_policy is False
    assert result.encoding == "utf-8"
    assert result.decodable is True
    assert result.policy_version == MIGRATION_POLICY_VERSION
    assert result.policy_changed is False
    assert result.needs_resampling is False


def test_reuse_display_name_normalized() -> None:
    record = LegacyRecord(encoding="Windows-1252", confidence=0.5, sample=CP1252_SAMPLE)
    result = migrate_directory([record])
    (migration,) = result.records
    assert migration.verdict is MigrationVerdict.REUSE
    assert migration.encoding == "cp1252"
    # The persisted record carries the canonical identity and fresh scores.
    assert migration.migrated_record.encoding == "cp1252"
    assert migration.migrated_record.confidence != record.confidence
    assert migration.migrated_record.sample == CP1252_SAMPLE


def test_resample_on_name_mismatch() -> None:
    record = LegacyRecord(encoding="ISO-8859-1", confidence=0.8, sample=UTF8_SAMPLE)
    result = migrate_directory([record])
    (migration,) = result.records
    assert migration.verdict is MigrationVerdict.RESAMPLE
    assert migration.encoding == "utf-8"
    assert migration.certified_bytes == 0
    assert migration.decodable is True
    # Nothing certified to persist: the original record stands.
    assert migration.migrated_record is record
    assert result.encoding == "utf-8"
    assert result.needs_resampling is True


def test_insufficient_without_bytes() -> None:
    record = LegacyRecord(encoding="Windows-1252", confidence=0.9)
    result = migrate_directory([record])
    (migration,) = result.records
    assert migration.verdict is MigrationVerdict.INSUFFICIENT
    assert migration.encoding == "cp1252"
    assert migration.decodable is None
    assert migration.certified_bytes == 0
    assert migration.sample_complete is False
    assert migration.migrated_record is record
    assert result.encoding == "cp1252"
    assert result.decodable is None


def test_unknown_legacy_name() -> None:
    nameless = LegacyRecord(encoding="x-no-such-codec", confidence=0.7)
    (migration,) = migrate_directory([nameless]).records
    assert migration.verdict is MigrationVerdict.INSUFFICIENT
    assert migration.encoding is None

    with_bytes = LegacyRecord(
        encoding="x-no-such-codec", confidence=0.7, sample=UTF8_SAMPLE
    )
    (migration,) = migrate_directory([with_bytes]).records
    assert migration.verdict is MigrationVerdict.RESAMPLE
    assert migration.encoding == "utf-8"


def test_binary_record_reused() -> None:
    record = LegacyRecord(encoding=None, confidence=0.95, sample=BINARY_SAMPLE)
    result = migrate_directory([record])
    (migration,) = result.records
    assert migration.verdict is MigrationVerdict.REUSE
    assert migration.encoding is None
    assert migration.decodable is None
    assert migration.certified_bytes == len(BINARY_SAMPLE)
    assert result.encoding is None
    assert result.decodable is None


def test_binary_record_not_reused_on_text() -> None:
    record = LegacyRecord(encoding=None, confidence=0.95, sample=UTF8_SAMPLE)
    (migration,) = migrate_directory([record]).records
    assert migration.verdict is MigrationVerdict.RESAMPLE
    assert migration.encoding == "utf-8"


def test_prefix_sample_certifies_less_than_full_sample() -> None:
    prefix = _cut_mid_character(UTF8_SAMPLE)
    full = LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE)
    cut = LegacyRecord(
        encoding="UTF-8", confidence=0.9, sample=prefix, sample_is_prefix=True
    )
    result = migrate_directory([full, cut])
    full_migration, cut_migration = result.records
    # Same stored name and score, but the certifiable range differs.
    assert full_migration.verdict is MigrationVerdict.REUSE
    assert cut_migration.verdict is MigrationVerdict.REUSE
    assert full_migration.certified_bytes == len(UTF8_SAMPLE)
    assert cut_migration.certified_bytes == len(prefix)
    assert cut_migration.certified_bytes < full_migration.certified_bytes
    assert full_migration.sample_complete is True
    assert cut_migration.sample_complete is False
    # A prefix may end on an incomplete sequence and still be decodable.
    assert cut_migration.decodable is True
    assert result.decodable is True


def test_prefix_with_undecodable_bytes() -> None:
    sample = b"just ascii here!" + b"\xff" * 8
    record = LegacyRecord(
        encoding="ascii", confidence=0.9, sample=sample, sample_is_prefix=True
    )
    (migration,) = migrate_directory([record], max_bytes=16).records
    assert migration.verdict is MigrationVerdict.REUSE
    assert migration.certified_bytes == 16
    # 0xFF mid-data is genuine corruption, not a tolerated dangling tail.
    assert migration.decodable is False


def test_decodable_false_beyond_window() -> None:
    sample = b"just ascii here!" + b"\xff" * 8
    record = LegacyRecord(encoding="ascii", confidence=0.9, sample=sample)
    result = migrate_directory([record], max_bytes=16)
    (migration,) = result.records
    assert migration.verdict is MigrationVerdict.REUSE
    assert migration.certified_bytes == 16
    # The window certified ascii; the full sample does not decode under it.
    assert migration.decodable is False
    assert result.encoding == "ascii"
    assert result.decodable is False


def test_directory_data_fallback() -> None:
    matching = LegacyRecord(encoding="UTF-8", confidence=0.9)
    mismatching = LegacyRecord(encoding="ISO-8859-1", confidence=0.8)
    result = migrate_directory([matching, mismatching], data=UTF8_SAMPLE)
    match_migration, mismatch_migration = result.records
    assert match_migration.verdict is MigrationVerdict.REUSE
    assert match_migration.sample_complete is True
    assert match_migration.certified_bytes == len(UTF8_SAMPLE)
    assert mismatch_migration.verdict is MigrationVerdict.RESAMPLE
    assert result.encoding == "utf-8"
    assert result.decodable is True


@pytest.mark.parametrize(
    "policy",
    [
        LegacyPolicy(lang_filter=LanguageFilter.NON_CJK),
        LegacyPolicy(lang_filter=LanguageFilter.JAPANESE),
        LegacyPolicy(chunk_size=1024),
        LegacyPolicy(should_rename_legacy=True),
    ],
)
def test_legacy_policy_marks_records_affected(policy: LegacyPolicy) -> None:
    record = LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE)
    result = migrate_directory([record], policy=policy)
    (migration,) = result.records
    assert migration.affected_by_policy is True
    assert result.policy_changed is True
    # The verdict itself is still evidence-based.
    assert migration.verdict is MigrationVerdict.REUSE


def test_should_rename_legacy_matched_by_prefer_superset() -> None:
    policy = LegacyPolicy(should_rename_legacy=True)
    record = LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE)
    result = migrate_directory([record], policy=policy, prefer_superset=True)
    assert result.policy_changed is False
    assert result.records[0].affected_by_policy is False


def test_current_policy_version_is_equivalent() -> None:
    policy = LegacyPolicy(
        lang_filter=LanguageFilter.NON_CJK,
        chunk_size=1,
        should_rename_legacy=True,
        version=MIGRATION_POLICY_VERSION,
    )
    record = LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE)
    result = migrate_directory([record], policy=policy)
    assert result.policy_changed is False
    assert result.records[0].affected_by_policy is False


def test_remigration_is_stable() -> None:
    records = [
        LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE),
        LegacyRecord(encoding="ISO-8859-1", confidence=0.8, sample=UTF8_SAMPLE),
        LegacyRecord(encoding="Windows-1252", confidence=0.7),
    ]
    first = migrate_directory(records)

    def conclusions(result):  # noqa: ANN001
        return (
            result.encoding,
            result.decodable,
            [
                (m.verdict, m.encoding, m.decodable, m.certified_bytes)
                for m in result.records
            ],
        )

    second = migrate_directory(
        first.migrated_records,
        policy=LegacyPolicy(version=MIGRATION_POLICY_VERSION),
    )
    assert conclusions(second) == conclusions(first)
    third = migrate_directory(
        second.migrated_records,
        policy=LegacyPolicy(version=MIGRATION_POLICY_VERSION),
    )
    assert conclusions(third) == conclusions(second)


def test_remigration_stable_with_directory_data() -> None:
    records = [
        LegacyRecord(encoding="UTF-8", confidence=0.9),
        LegacyRecord(encoding="ISO-8859-1", confidence=0.8),
    ]
    first = migrate_directory(records, data=UTF8_SAMPLE)
    second = migrate_directory(
        first.migrated_records,
        policy=LegacyPolicy(version=MIGRATION_POLICY_VERSION),
        data=UTF8_SAMPLE,
    )
    assert [m.verdict for m in second.records] == [m.verdict for m in first.records]
    assert second.encoding == first.encoding
    assert second.decodable == first.decodable


def test_directory_identity_from_longest_provisional() -> None:
    short = LegacyRecord(
        encoding="ISO-8859-1", confidence=0.8, sample=UTF8_SAMPLE[:100]
    )
    long_ = LegacyRecord(encoding="ISO-8859-2", confidence=0.8, sample=CP1252_SAMPLE)
    result = migrate_directory([short, long_])
    assert all(m.verdict is MigrationVerdict.RESAMPLE for m in result.records)
    assert result.encoding == "cp1252"
    assert result.decodable is True


def test_directory_identity_name_only_records() -> None:
    agreeing = [
        LegacyRecord(encoding="Windows-1252", confidence=0.9),
        LegacyRecord(encoding="cp1252", confidence=0.8),
    ]
    result = migrate_directory(agreeing)
    assert result.encoding == "cp1252"
    assert result.decodable is None

    conflicting = [
        LegacyRecord(encoding="Windows-1252", confidence=0.9),
        LegacyRecord(encoding="UTF-8", confidence=0.8),
    ]
    result = migrate_directory(conflicting)
    assert result.encoding is None
    assert result.decodable is None


def test_empty_directory() -> None:
    result = migrate_directory([])
    assert result.encoding is None
    assert result.decodable is None
    assert result.records == ()
    assert result.migrated_records == ()
    assert result.needs_resampling is False


def test_default_policy_used_when_omitted() -> None:
    record = LegacyRecord(encoding="UTF-8", confidence=0.9, sample=UTF8_SAMPLE)
    result = migrate_directory([record])
    assert result.policy_changed is False
