"""Migrate non-RFC ``JsonPatchWithAppend`` patches to RFC 6902 patches.

The ``append`` operation of :class:`jsonpatch.JsonPatchWithAppend` adds a
string suffix to a string that already exists. Receivers that only implement
RFC 6902 cannot run it, so each append is translated into a standard
``replace`` carrying the complete string written at that step. The other
operations (including ``test`` and ``move``) are passed through unchanged, so
their order, the actual target of each move and the value written by every
step are preserved.

An append to an array itself (the ``-`` position or an out-of-range index)
or to a non-string array element cannot be translated: the old entry point
rejects it, while the equivalent standard operation would succeed. Rows
containing such an operation are reported on a quarantine list instead of
being delivered. An in-range string array element is appended to normally
and migrated like any object member.

The module also plans the minimum RFC 6902 ``test`` operations that let a
receiver tell the baseline a patch was recorded for apart from the other
candidate baselines, using only fields that exist on every candidate.
"""

import copy
import itertools
import json
from collections.abc import Iterable, Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass
from typing import Any

from jsonpointer import JsonPointer, JsonPointerException

import jsonpatch

_Operation = Mapping[str, Any]
_Row = Mapping[str, Any]
_Baselines = Mapping[Any, Any]
_StandardOp = dict[str, Any]

_RFC_OPERATIONS = frozenset(
    ('add', 'remove', 'replace', 'move', 'copy', 'test'))
_MUTATING_OPERATIONS = frozenset(
    ('add', 'remove', 'replace', 'move', 'copy'))


class MigrationError(jsonpatch.JsonPatchException):
    """A patch cannot be migrated to RFC 6902."""


class QuarantinedRow(MigrationError):
    """A row whose append cannot be expressed with standard operations.

    An append to an array itself or to a non-string element fails in the old
    entry point, whereas the equivalent standard operation would write.
    Emitting a standard patch would therefore change whether the row fails,
    so the row is kept out of delivery and listed for quarantine instead.
    """

    def __init__(self, message: str, *, operation: Any = None,
                 path: str | None = None, index: int | None = None) -> None:
        super().__init__(message, op=operation)
        self.path = path
        self.index = index


@dataclass(frozen=True)
class QuarantineEntry:
    """A row that was not migrated, with the append that blocks it."""

    row_id: Any
    path: str
    index: int
    reason: str


@dataclass(frozen=True)
class DeliveryGroup:
    """Rows that deliver the same standard patch, sent only once."""

    key: str
    patch: tuple[_StandardOp, ...]
    row_ids: tuple[Any, ...]
    baseline_ids: tuple[Any, ...]


@dataclass(frozen=True)
class MigrationReport:
    groups: tuple[DeliveryGroup, ...]
    quarantined: tuple[QuarantineEntry, ...]
    errors: tuple[tuple[Any, str], ...]


@dataclass(frozen=True)
class PrecheckPlan:
    """The RFC 6902 tests to prepend, and the baselines no test can guard."""

    row_ids: tuple[Any, ...]
    baseline_ids: tuple[Any, ...]
    checks: tuple[_StandardOp, ...]
    blocked_before_first_write: tuple[Any, ...]
    unsafe_pairs: tuple[tuple[Any, Any], ...]


def _json_equal(first: Any, second: Any) -> bool:
    """RFC 6902, section 4.6 equality: numbers by value, types otherwise."""
    if isinstance(first, bool) or isinstance(second, bool):
        return isinstance(first, bool) and isinstance(second, bool) \
            and first == second
    if isinstance(first, Mapping) and isinstance(second, Mapping):
        return len(first) == len(second) and all(
            key in second and _json_equal(first[key], second[key])
            for key in first)
    if isinstance(first, MutableSequence) and isinstance(second, MutableSequence):
        return len(first) == len(second) and all(
            _json_equal(left, right) for left, right in zip(first, second))
    return bool(first == second)


def _append_target(doc: Any, operation: _Operation) -> tuple[Any, Any, str]:
    """Resolves the parent container and target of an append operation.

    Returns ``(parent, part, target_value)`` and mirrors the failure modes of
    :class:`jsonpatch.AppendOperation`. A parent that is an array is reported
    separately, as those rows go to the quarantine list.
    """
    try:
        suffix = operation['value']
    except KeyError:
        raise MigrationError(
            "The append operation does not contain a 'value' member",
            op=operation)
    if not isinstance(suffix, str):
        raise MigrationError("Cannot append a non-string value", op=operation)

    pointer = JsonPointer(operation['path'])
    try:
        parent, part = pointer.to_last(doc)
    except JsonPointerException as ex:
        raise MigrationError(str(ex), op=operation)

    if part is None:
        raise MigrationError("Cannot append to the root document",
                             op=operation)
    if isinstance(parent, MutableSequence):
        # The old entry point rejects an append to the array itself ('-' or
        # an index outside the array) and to a non-string element, while the
        # standard add/replace would not, so these rows are quarantined. An
        # in-range string element is appended to normally and can be migrated.
        if not isinstance(part, int) or part < 0 or part >= len(parent) \
                or not isinstance(parent[part], str):
            raise QuarantinedRow(
                "Appending to an array fails in the old entry point",
                operation=operation, path=operation['path'])
    elif isinstance(parent, MutableMapping):
        if part not in parent:
            raise MigrationError(
                "Cannot append to a non-existent member '{0}'".format(part),
                op=operation)
        if not isinstance(parent[part], str):
            raise MigrationError("Cannot append to a non-string value",
                                 op=operation)
    else:
        raise MigrationError("Cannot append to a non-string value",
                             op=operation)
    return parent, part, parent[part]


def migrate_patch(operations: Iterable[_Operation],
                  baseline: Any) -> list[_StandardOp]:
    """Translates one ``JsonPatchWithAppend`` row into RFC 6902 operations.

    ``baseline`` is the document the row was recorded against. The standard
    operations are replayed on a copy of it while each append is rewritten to
    a ``replace`` with the full string the append would write. The returned
    patch applies to exactly the same documents as the original row.

    :raises QuarantinedRow: if an append targets an array itself or a
        non-string array element, which the old entry point rejects.
    :raises MigrationError: if an operation cannot be translated.
    """
    operations = list(operations)
    current = copy.deepcopy(baseline)
    migrated = []

    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise MigrationError("Each operation must be an object",
                                 op=operation)
        kind = operation.get('op')
        if kind == 'append':
            try:
                _parent, part, current_value = _append_target(
                    current, operation)
            except QuarantinedRow as ex:
                ex.index = index
                raise
            replacement = {
                'op': 'replace',
                'path': operation['path'],
                'value': current_value + operation['value'],
            }
            # Replay through the standard engine, so the working document
            # matches what an RFC 6902 receiver would hold at every step.
            jsonpatch.apply_patch(current, [replacement], in_place=True)
            migrated.append(replacement)
            continue

        if kind not in _RFC_OPERATIONS:
            raise MigrationError(
                "Unknown operation {0!r}".format(kind), op=operation)

        standard = copy.deepcopy(dict(operation))
        jsonpatch.apply_patch(current, [standard], in_place=True)
        migrated.append(standard)

    _verify_equivalent(operations, baseline, migrated)
    return migrated


def _verify_equivalent(original_operations: Iterable[_Operation],
                       baseline: Any, migrated: list[_StandardOp]) -> None:
    """The standard patch must leave the same document as the old patch."""
    expected = jsonpatch.JsonPatchWithAppend(
        copy.deepcopy(original_operations)).apply(copy.deepcopy(baseline))
    actual = jsonpatch.apply_patch(copy.deepcopy(baseline),
                                   copy.deepcopy(migrated))
    if not _json_equal(actual, expected):
        raise MigrationError(
            "Migrated patch does not write the same values as the original")


def _patch_key(patch: list[_StandardOp]) -> str:
    return json.dumps(patch, sort_keys=True, ensure_ascii=False)


def migrate_archive(rows: Iterable[_Row],
                    baselines: _Baselines) -> MigrationReport:
    """Migrates every row of an archive and groups the deliverable ones.

    Each row is a mapping with ``id``, ``baseline`` (the id of a candidate
    baseline) and ``operations`` (the old patch). Rows whose standard patch
    is identical form one :class:`DeliveryGroup`, so the patch is produced
    only once. Rows with an array append go to the quarantine list.
    """
    groups: dict[str, dict[str, Any]] = {}
    group_order: list[str] = []
    quarantined: list[QuarantineEntry] = []
    errors: list[tuple[Any, str]] = []

    for row in rows:
        row_id = row['id']
        baseline_id = row['baseline']
        if baseline_id not in baselines:
            errors.append((row_id, "Unknown baseline {0!r}".format(baseline_id)))
            continue
        try:
            patch = migrate_patch(row['operations'], baselines[baseline_id])
        except QuarantinedRow as ex:
            quarantined.append(QuarantineEntry(
                row_id=row_id, path=ex.path or '', index=ex.index or 0,
                reason=str(ex)))
            continue
        except (MigrationError, jsonpatch.JsonPatchException) as ex:
            errors.append((row_id, str(ex)))
            continue

        key = _patch_key(patch)
        if key not in groups:
            groups[key] = {
                'patch': tuple(copy.deepcopy(op) for op in patch),
                'row_ids': [],
                'baseline_ids': set(),
            }
            group_order.append(key)
        groups[key]['row_ids'].append(row_id)
        groups[key]['baseline_ids'].add(baseline_id)

    delivered = tuple(
        DeliveryGroup(
            key=key,
            patch=groups[key]['patch'],
            row_ids=tuple(groups[key]['row_ids']),
            baseline_ids=tuple(sorted(groups[key]['baseline_ids'])),
        )
        for key in group_order
    )
    return MigrationReport(
        groups=delivered,
        quarantined=tuple(quarantined),
        errors=tuple(errors),
    )


def _writes_before_failure(patch: Iterable[_Operation],
                           doc: Any) -> tuple[bool, int]:
    """Applies a standard patch and counts writes completed before failure.

    Returns ``(succeeded, writes)`` where ``writes`` is the number of
    mutating operations already applied. A zero means the first operation
    that changed the document could not write, so no pre-delivery test is
    needed to protect that baseline.
    """
    current = copy.deepcopy(doc)
    writes = 0
    for operation in patch:
        try:
            jsonpatch.apply_patch(current, [operation], in_place=True)
        except jsonpatch.JsonPatchException:
            return False, writes
        if operation['op'] in _MUTATING_OPERATIONS:
            writes += 1
    return True, writes


def _field_values(baselines: _Baselines,
                  field: str) -> dict[Any, Any] | None:
    pointer = JsonPointer(field)
    values = {}
    for baseline_id, doc in baselines.items():
        try:
            values[baseline_id] = pointer.resolve(doc)
        except JsonPointerException:
            return None
    return values


def plan_prechecks(group: DeliveryGroup, baselines: _Baselines,
                   allowed_fields: Iterable[str]) -> PrecheckPlan:
    """Chooses the minimum ``test`` operations to guard one delivery group.

    Only fields of ``allowed_fields`` may be read, and every field has to
    exist on each candidate baseline. A field is usable only if all baselines
    the group was recorded against hold the same value there, so one shared
    ``test`` admits every row of the group. Every other candidate that would
    accept a write has to fail these tests before its first write; candidates
    on which the patch already fails before any write need no test. A wrong
    candidate that the open fields cannot tell from a true baseline is
    returned as an unsafe pair instead of being guarded by a guess.
    """
    true_ids = set(group.baseline_ids)

    to_block = set()
    blocked_before_first_write = []
    for baseline_id, doc in baselines.items():
        if baseline_id in true_ids:
            continue
        _succeeded, writes = _writes_before_failure(group.patch, doc)
        if writes == 0:
            blocked_before_first_write.append(baseline_id)
        else:
            to_block.add(baseline_id)

    usable = []
    for field in allowed_fields:
        values = _field_values(baselines, field)
        if values is None:
            continue
        expected = values[next(iter(true_ids))]
        if not all(_json_equal(values[true_id], expected)
                   for true_id in true_ids):
            continue
        covered = {baseline_id for baseline_id in to_block
                   if not _json_equal(values[baseline_id], expected)}
        usable.append((field, expected, covered))

    chosen = _minimum_cover(usable, to_block)
    covered = set().union(*(covered for _f, _v, covered in chosen)) \
        if chosen else set()
    uncovered = to_block - covered
    unsafe_pairs = tuple(
        (true_id, baseline_id)
        for baseline_id in sorted(uncovered)
        for true_id in sorted(true_ids)
    )
    checks = tuple(
        {'op': 'test', 'path': field, 'value': copy.deepcopy(expected)}
        for field, expected, _covered in chosen
    )
    return PrecheckPlan(
        row_ids=group.row_ids,
        baseline_ids=tuple(sorted(true_ids)),
        checks=checks,
        blocked_before_first_write=tuple(sorted(blocked_before_first_write)),
        unsafe_pairs=unsafe_pairs,
    )


def _minimum_cover(
    usable: list[tuple[str, Any, set[Any]]],
    blockers: set[Any],
) -> list[tuple[str, Any, set[Any]]]:
    """Smallest subset of usable fields covering the most blocking baselines.

    Inputs are small (a handful of open fields and candidates), so subsets are
    tried by size to return an actual minimum. If no subset covers every
    blocking baseline, the smallest subset with the largest achievable
    coverage is returned; callers report the remainder as unsafe pairs.
    """
    best: list[tuple[str, Any, set[Any]]] = []
    best_coverage: set[Any] = set()
    for size in range(1, len(usable) + 1):
        for subset in itertools.combinations(usable, size):
            covered = set().union(*(item[2] for item in subset))
            if len(covered) > len(best_coverage):
                best, best_coverage = list(subset), covered
        if best_coverage == blockers:
            return best
    return best


def deliverable_patch(group: DeliveryGroup,
                      plan: PrecheckPlan) -> list[_StandardOp]:
    """The single RFC 6902 patch to send: prechecks first, then the writes."""
    return [*plan.checks, *copy.deepcopy(group.patch)]
