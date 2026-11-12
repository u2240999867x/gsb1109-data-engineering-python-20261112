"""Migration of legacy detection-result directories to the current policy.

Data directories written by chardet 5.x/6.x era tooling hold detection
results next to the policy that produced them: often just a
``lang_filter``, a ``chunk_size``, and the encoding name the detector
reported when its ``done`` flag went set.  Some records keep the full
sample they were derived from, some keep only a prefix, some keep no
bytes at all.  This module re-checks such directories against the
current chardet and sorts every record into one of three verdicts:

* :attr:`MigrationVerdict.REUSE` -- the stored answer is reproduced by
  the current detector on the stored bytes and can be carried over.
* :attr:`MigrationVerdict.RESAMPLE` -- the stored answer cannot be
  reproduced; the record must be re-derived from fresh (or fuller) data.
* :attr:`MigrationVerdict.INSUFFICIENT` -- the directory kept no bytes
  for the record, so nothing can be certified either way.

The directory-level result names the migrated encoding identity and
whether the stored data decodes under it.  When the legacy policy's
filters cannot be reproduced by the current candidate set
(``lang_filter`` and ``chunk_size`` are inert in this version), the
affected records are flagged via
:attr:`RecordMigration.affected_by_policy`.

Re-running :func:`migrate_directory` on a directory that already carries
:data:`MIGRATION_POLICY_VERSION` is a pure re-check: verdicts, encoding
identities, certified ranges, and decodability conclusions come back
unchanged.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable

from chardet._request import _DEFAULT_CHUNK_SIZE, DetectionRequest
from chardet._utils import (
    DEFAULT_MAX_BYTES,
    decodes_completely,
    decodes_without_error,
)
from chardet.enums import EncodingEra, LanguageFilter
from chardet.registry import lookup_encoding

#: Policy version written by this migration.  Directories whose policy
#: carries this version (or newer) are re-checked, not re-migrated.
MIGRATION_POLICY_VERSION: int = 1


class MigrationVerdict(enum.Enum):
    """How one legacy record fares under the current chardet."""

    #: The current detector reproduces the stored answer on the stored
    #: bytes; the record can be carried over as is.
    REUSE = "reuse"
    #: The stored answer cannot be reproduced; re-derive the record from
    #: fresh (or fuller) data.
    RESAMPLE = "resample"
    #: No bytes were kept for the record, so nothing can be certified.
    INSUFFICIENT = "insufficient"


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyPolicy:
    """The policy a legacy directory's results were produced under.

    ``version`` is the migration policy version the directory carries:
    ``0`` for a pre-migration (legacy) directory,
    :data:`MIGRATION_POLICY_VERSION` once migrated.  The remaining fields
    are inert in the current chardet; a non-default value in a
    pre-migration policy means the old run's candidate set cannot be
    reproduced and the records are flagged as affected.
    """

    lang_filter: LanguageFilter = LanguageFilter.ALL
    chunk_size: int = _DEFAULT_CHUNK_SIZE
    should_rename_legacy: bool = False
    version: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyRecord:
    """One stored detection result from a legacy directory.

    ``encoding`` is the name the old detector reported (its display
    spelling, e.g. ``"Windows-1252"``), or ``None`` for a binary
    verdict.  ``sample`` holds the bytes the record was derived from
    when the directory kept them; ``sample_is_prefix`` marks a sample
    that is only a prefix of the record's data, which bounds what the
    migration can certify.
    """

    encoding: str | None
    confidence: float = 0.0
    language: str | None = None
    sample: bytes | None = None
    sample_is_prefix: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class RecordMigration:
    """The migration outcome for one legacy record.

    ``encoding`` is the migrated encoding identity (a canonical registry
    name): the confirmed identity for ``REUSE``, the provisional
    current-chardet identity for ``RESAMPLE``, the best-effort
    normalized stored name for ``INSUFFICIENT``.  ``certified_bytes`` is
    how many leading bytes of the record's data the verdict certifies --
    the examination window for ``REUSE``, zero otherwise.  A record that
    kept only a prefix certifies a shorter range than one that kept the
    full sample, even when their stored names and scores are identical.
    ``migrated_record`` is what to persist under the new policy version:
    the confirmed record for ``REUSE``, the untouched original
    otherwise.
    """

    record: LegacyRecord
    verdict: MigrationVerdict
    encoding: str | None
    decodable: bool | None
    certified_bytes: int
    sample_complete: bool
    affected_by_policy: bool
    migrated_record: LegacyRecord


@dataclasses.dataclass(frozen=True, slots=True)
class DirectoryMigration:
    """The migration outcome for a whole directory.

    ``encoding`` is the directory's migrated encoding identity: the
    identity of the best-certified ``REUSE`` record when there is one,
    else the provisional identity of the longest-sampled ``RESAMPLE``
    record, else the stored name when every record agrees on one, else
    ``None``.  ``decodable`` tells whether all stored data decodes under
    that identity (``None`` when there is no identity or no data).
    """

    encoding: str | None
    decodable: bool | None
    records: tuple[RecordMigration, ...]
    policy_version: int
    policy_changed: bool

    @property
    def migrated_records(self) -> tuple[LegacyRecord, ...]:
        """The records to persist under :data:`MIGRATION_POLICY_VERSION`."""
        return tuple(m.migrated_record for m in self.records)

    @property
    def needs_resampling(self) -> bool:
        """Whether any record must be re-derived from fresh data."""
        return any(m.verdict is MigrationVerdict.RESAMPLE for m in self.records)


def _normalize(name: str | None) -> str | None:
    """Resolve a stored encoding name to its canonical registry name."""
    if name is None:
        return None
    return lookup_encoding(name)


def _record_evidence(
    record: LegacyRecord, fallback: bytes | None
) -> tuple[bytes | None, bool]:
    """Return the bytes a record can be checked against, and their completeness.

    The record's own sample takes precedence; *fallback* (the
    directory's raw bytes) stands in when the record kept none and is
    treated as complete, since the caller vouches it is the raw input.
    A sample known to be a prefix is never complete evidence.
    """
    if record.sample is None:
        if fallback is None:
            return None, False
        return fallback, True
    return record.sample, not record.sample_is_prefix


def _decodable(sample: bytes, encoding: str | None, *, complete: bool) -> bool | None:
    """Return whether *sample* decodes under *encoding*.

    A complete sample must decode strictly; a prefix is allowed to end
    on an incomplete multi-byte sequence.  ``None`` when there is no
    encoding identity to decode under (a binary verdict).
    """
    if encoding is None:
        return None
    if complete:
        return decodes_completely(sample, encoding)
    return decodes_without_error(sample, encoding)


def _policy_equivalent(policy: LegacyPolicy, request: DetectionRequest) -> bool:
    """Return whether the legacy policy's candidate set is reproducible.

    A directory already carrying the current policy version is
    equivalent by definition.  Otherwise the old run's filters must be
    inert: ``lang_filter`` and ``chunk_size`` at their defaults, and the
    legacy renaming flag matching the request's ``prefer_superset``.
    """
    if policy.version >= MIGRATION_POLICY_VERSION:
        return True
    return (
        policy.lang_filter == LanguageFilter.ALL
        and policy.chunk_size == _DEFAULT_CHUNK_SIZE
        and policy.should_rename_legacy == request.prefer_superset
    )


def _migrate_record(
    record: LegacyRecord,
    request: DetectionRequest,
    fallback: bytes | None,
    *,
    affected: bool,
) -> RecordMigration:
    """Re-check one legacy record against the current detector."""
    old_name = _normalize(record.encoding)
    sample, complete = _record_evidence(record, fallback)
    if sample is None:
        return RecordMigration(
            record=record,
            verdict=MigrationVerdict.INSUFFICIENT,
            encoding=old_name,
            decodable=None,
            certified_bytes=0,
            sample_complete=False,
            affected_by_policy=affected,
            migrated_record=record,
        )
    window = sample[: request.max_bytes]
    rendered = request.render(request.run(window), window)[0]
    new_name = _normalize(rendered["encoding"])
    if record.encoding is None:
        reproduced = new_name is None
    else:
        reproduced = old_name is not None and old_name == new_name
    if reproduced:
        verdict = MigrationVerdict.REUSE
        identity = old_name
        certified = len(window)
        migrated = LegacyRecord(
            encoding=old_name,
            confidence=rendered["confidence"],
            language=rendered["language"],
            sample=record.sample,
            sample_is_prefix=record.sample_is_prefix,
        )
    else:
        verdict = MigrationVerdict.RESAMPLE
        identity = new_name
        certified = 0
        migrated = record
    return RecordMigration(
        record=record,
        verdict=verdict,
        encoding=identity,
        decodable=_decodable(sample, identity, complete=complete),
        certified_bytes=certified,
        sample_complete=complete,
        affected_by_policy=affected,
        migrated_record=migrated,
    )


def _directory_encoding(migrations: tuple[RecordMigration, ...]) -> str | None:
    """Pick the directory's encoding identity from its record migrations."""
    reusable = [
        (m.certified_bytes, m.encoding)
        for m in migrations
        if m.verdict is MigrationVerdict.REUSE and m.encoding is not None
    ]
    if reusable:
        return max(reusable)[1]
    provisional = [
        (len(m.record.sample or b""), m.encoding)
        for m in migrations
        if m.verdict is MigrationVerdict.RESAMPLE and m.encoding is not None
    ]
    if provisional:
        return max(provisional)[1]
    names = {m.encoding for m in migrations if m.encoding is not None}
    if len(names) == 1:
        return names.pop()
    return None


def migrate_directory(  # noqa: PLR0913
    records: Iterable[LegacyRecord],
    policy: LegacyPolicy | None = None,
    data: bytes | None = None,
    *,
    encoding_era: EncodingEra = EncodingEra.ALL,
    max_bytes: int = DEFAULT_MAX_BYTES,
    prefer_superset: bool = False,
    compat_names: bool = True,
    include_encodings: Iterable[str] | None = None,
    exclude_encodings: Iterable[str] | None = None,
) -> DirectoryMigration:
    """Migrate one legacy detection-result directory to the current policy.

    :param records: The directory's stored detection results.
    :param policy: The policy the stored results were produced under;
        a default-constructed :class:`LegacyPolicy` when omitted.
    :param data: The directory's raw bytes, used for records that kept
        no sample of their own.
    :param encoding_era: Restrict the current detector's candidates to
        the given era.
    :param max_bytes: Examination window for the re-check; also the
        bound on what a ``REUSE`` verdict can certify.
    :param prefer_superset: Remap results to Windows/CP supersets, as
        :func:`chardet.detect` does.
    :param compat_names: Compare against chardet 5.x/6.x display names.
    :param include_encodings: Restrict detection to these encodings.
    :param exclude_encodings: Remove these encodings from the candidates.
    :returns: The directory's migration outcome.
    """
    if policy is None:
        policy = LegacyPolicy()
    request = DetectionRequest.resolve(
        encoding_era=encoding_era,
        max_bytes=max_bytes,
        prefer_superset=prefer_superset,
        compat_names=compat_names,
        include_encodings=include_encodings,
        exclude_encodings=exclude_encodings,
        stacklevel=3,
    )
    affected = not _policy_equivalent(policy, request)
    migrations = tuple(
        _migrate_record(record, request, data, affected=affected) for record in records
    )
    encoding = _directory_encoding(migrations)
    decodable: bool | None = None
    if encoding is not None:
        checks = []
        for migration in migrations:
            sample, complete = _record_evidence(migration.record, data)
            if sample is not None:
                checks.append(_decodable(sample, encoding, complete=complete))
        if checks:
            decodable = all(checks)
    return DirectoryMigration(
        encoding=encoding,
        decodable=decodable,
        records=migrations,
        policy_version=MIGRATION_POLICY_VERSION,
        policy_changed=affected,
    )
