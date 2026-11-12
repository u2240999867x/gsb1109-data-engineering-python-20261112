# routing.py - routing preview for supplier master data columns
# coding: utf-8
#
# Copyright (C) 2026 Arthur de Jong
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/>.

"""Preview how the values of a supplier master data column should be routed
to the number validation modules of stdnum.

A column of supplier master data (for example a "country plus number purpose"
column) is rarely mapped to a single stdnum module directly. The configured
purpose name may be an old alias that now points at another module and a
single configuration may restrict the whole column to a handful of usable
modules. On top of that the caller may state that the column holds the same
number type on every row or that all rows of one supplier share a number
type. This module validates every row against every candidate module and
uses those stated relations to narrow down the possible number types.

The candidate modules of a column are configured with a list of
``(country, purpose)`` pairs (where the purpose may be an old alias, see
:func:`stdnum.get_cc_module`) and/or a list of explicit module references
(dotted module names or module objects). Aliases that resolve to the same
module are only counted once:

>>> get_candidates({'names': [('at', 'vat'), ('at', 'uid')]})
('stdnum.at.uid',)
>>> get_candidates({'modules': ['stdnum.de.vat', ('de', 'vat')]})
('stdnum.de.vat',)

Each row is validated against the resolved candidates. Rows that pass a
single module are committed, rows that pass several modules are reported as
ambiguous with their feasible types and rows that fail every module are
reported separately as invalid, together with the exception type that each
module raised:

>>> columns = {
...     'tax_id': {
...         'names': [('de', 'vat'), ('de', 'idnr'), ('de', 'stnr')],
...     },
... }
>>> rows = [
...     {'row': 0, 'column': 'tax_id', 'value': '136695976'},
...     {'row': 1, 'column': 'tax_id', 'value': '65299970078'},
...     {'row': 2, 'column': 'tax_id', 'value': '36574261809'},
...     {'row': 3, 'column': 'tax_id', 'value': '123456780'},
... ]
>>> result = preview(rows, columns)
>>> [(row.row, row.status, row.module) for row in result.rows]
[(0, 'committed', 'stdnum.de.vat'), (1, 'committed', 'stdnum.de.stnr'), (2, 'ambiguous', None), (3, 'invalid', None)]
>>> result[2].candidates
('stdnum.de.idnr', 'stdnum.de.stnr')
>>> result[3].errors['stdnum.de.vat']
'InvalidChecksum'

When the caller states that the whole column holds a single number type
(``relation='column'``) the module has to validate every row at the same
time. The rows on which all consistent explanations agree are committed and
the other valid rows keep their feasible types:

>>> columns['tax_id']['relation'] = 'column'
>>> result = preview(rows[1:3], columns)
>>> result.consistent
True
>>> [(row.row, row.status, row.module) for row in result.committed]
[(1, 'committed', 'stdnum.de.stnr'), (2, 'committed', 'stdnum.de.stnr')]

If no module can be assigned consistently the conflicting rows and the rule
that they violate are returned, together with the inclusion-minimal sets of
rows whose removal restores a consistent assignment (removing any one of
those sets is enough):

>>> result = preview(rows[:3], columns)
>>> result.consistent
False
>>> conflict = result.conflicts[0]
>>> conflict.scope, conflict.key
('column', 'tax_id')
>>> conflict.hitting_sets
((0,), (1, 2))
>>> conflict.rules
("all rows of column 'tax_id' must share one number type",)
>>> [(row.row, row.status) for row in result.rows]
[(0, 'committed'), (1, 'committed'), (2, 'ambiguous')]

With a supplier relation (``relation='supplier'``) all rows of one supplier
must share a number type, while different suppliers may use different types:

>>> supplier_rows = [
...     {'row': 0, 'column': 'tax_id', 'supplier': 'A', 'value': '136695976'},
...     {'row': 1, 'column': 'tax_id', 'supplier': 'A', 'value': '36574261809'},
...     {'row': 2, 'column': 'tax_id', 'supplier': 'B', 'value': '65299970078'},
...     {'row': 3, 'column': 'tax_id', 'supplier': 'B', 'value': '86035742796'},
... ]
>>> columns['tax_id']['relation'] = 'supplier'
>>> result = preview(supplier_rows, columns)
>>> [(conflict.key, conflict.hitting_sets) for conflict in result.conflicts]
[('A', ((0,), (1,)))]
>>> [(row.row, row.status, row.module) for row in result.committed]
[(0, 'committed', 'stdnum.de.vat'), (2, 'committed', 'stdnum.de.stnr'), (3, 'committed', 'stdnum.de.stnr')]
>>> [(row.row, row.candidates) for row in result.ambiguous]
[(1, ('stdnum.de.idnr', 'stdnum.de.stnr'))]

Modules that cannot be resolved or that misbehave while validating never end
up mixed with illegal values; they are reported as broken modules instead:

>>> class BrokenModule:
...     __name__ = 'stdnum.fake.broken'
...     def validate(self, value):
...         raise RuntimeError('boom')
>>> config = {'modules': ['stdnum.de.missing', BrokenModule()]}
>>> result = preview([{'column': 'tax_id', 'value': '123456780'}],
...                  {'tax_id': config})
>>> result.broken_modules
(('stdnum.de.missing', 'module could not be found'), ('stdnum.fake.broken', 'boom'))
>>> result.rows[0].status
'invalid'
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from stdnum.exceptions import ValidationError
from stdnum.util import get_cc_module


# relation values that a column configuration may state
RELATION_COLUMN = 'column'
RELATION_SUPPLIER = 'supplier'


@dataclass(frozen=True)
class RowPreview:
    """The routing preview of a single row.

    The ``status`` is one of ``committed`` (a single module was determined),
    ``ambiguous`` (several modules validate the value) or ``invalid`` (no
    module validates the value).
    """

    row: Any
    column: str
    supplier: Any
    value: Any
    status: str
    module: str | None = None
    compact: str | None = None
    candidates: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Conflict:
    """A group of rows for which no single number type can be assigned.

    The ``hitting_sets`` are the inclusion-minimal sets of rows whose
    removal restores a consistent assignment; when a conflict is caused by a
    single row each hitting set contains exactly that row.
    """

    scope: str
    key: Any
    rows: tuple[Any, ...]
    candidates: tuple[str, ...]
    rules: tuple[str, ...]
    hitting_sets: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class RoutingPreview:
    """The result of a routing preview.

    Rows are split into ``committed`` rows (a unique module was found),
    ``ambiguous`` rows (several modules validate the value), ``invalid``
    rows (no module validates the value) and ``conflicts`` (relations that
    cannot be satisfied). Broken modules are reported separately.
    """

    rows: tuple[RowPreview, ...]
    committed: tuple[RowPreview, ...]
    ambiguous: tuple[RowPreview, ...]
    invalid: tuple[RowPreview, ...]
    conflicts: tuple[Conflict, ...]
    broken_modules: tuple[tuple[str, str], ...]
    consistent: bool

    def __getitem__(self, row: Any) -> RowPreview:
        """Return the preview of the row with the specified row identifier."""
        for row_preview in self.rows:
            if row_preview.row == row:
                return row_preview
        raise KeyError(row)


def _exception_message(exc: Exception) -> str:
    """Return a readable message for an exception."""
    message = str(exc)
    if message:
        return message
    return type(exc).__name__


def _reference_name(reference: Any) -> str:
    """Return a readable name for a module reference."""
    if isinstance(reference, tuple):
        return 'stdnum.%s.%s' % reference
    if isinstance(reference, str):
        return reference
    return getattr(reference, '__name__', repr(reference))


def _resolve_reference(reference: Any) -> Any:
    """Resolve a module reference to a validation module.

    Return None when the reference does not point at a module.
    """
    if isinstance(reference, tuple):
        country, purpose = reference
        return get_cc_module(country, purpose)
    if isinstance(reference, str):
        parts = reference.removeprefix('stdnum.').split('.')
        if len(parts) == 1:
            try:
                return importlib.import_module('stdnum.' + parts[0])
            except ImportError:
                return None
        if len(parts) == 2:
            return get_cc_module(parts[0], parts[1])
        return None
    if hasattr(reference, 'validate'):
        return reference
    return None


def _column_references(config: dict[str, Any]) -> list[Any]:
    """Collect the configured module references of a column."""
    references: list[Any] = list(config.get('modules', ()))
    references.extend(config.get('names', ()))
    return references


def _resolve_candidates(
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Resolve the candidate modules of a column.

    Aliases that point at the same module are only counted once. References
    that cannot be resolved or that do not provide a validate() function are
    returned as broken modules.
    """
    candidates: dict[str, Any] = {}
    broken: list[tuple[str, str]] = []
    for reference in _column_references(config):
        try:
            name = _reference_name(reference)
        except Exception:  # noqa: B902 (a malformed reference must not crash the preview)
            name = repr(reference)
        try:
            module = _resolve_reference(reference)
        except Exception as exc:  # noqa: B902 (a bad reference must not crash the preview)
            broken.append((name, _exception_message(exc)))
            continue
        if module is None:
            broken.append((name, 'module could not be found'))
        elif not hasattr(module, 'validate'):
            broken.append((name, 'module has no validate() function'))
        elif module.__name__ not in candidates:
            candidates[module.__name__] = module
    return dict(sorted(candidates.items())), broken


def get_candidates(config: dict[str, Any]) -> tuple[str, ...]:
    """Return the deduplicated module names that a column configuration
    resolves to.
    """
    candidates, _broken = _resolve_candidates(config)
    return tuple(candidates)


def _validate_rows(
    rows: list[dict[str, Any]],
    candidates: dict[str, Any],
) -> tuple[dict[Any, dict[str, Any]], list[tuple[str, str]]]:
    """Validate every row against every candidate module.

    A module that raises something other than a ValidationError on any row
    is considered broken, discarded and reported as a broken module; values
    that are not strings are reported on the row rather than validated.
    """
    broken: list[tuple[str, str]] = []
    outcomes: dict[Any, dict[str, Any]] = {
        row['row']: {'valid': {}, 'errors': {}} for row in rows}
    for row in rows:
        if not isinstance(row['value'], str):
            outcomes[row['row']]['errors']['__value__'] = \
                'value must be a string'
    for name, module in candidates.items():
        tentative_valid: dict[Any, str] = {}
        tentative_errors: dict[Any, str] = {}
        module_broken = False
        for row in rows:
            row_id = row['row']
            value = row['value']
            if not isinstance(value, str):
                continue
            try:
                compact = module.validate(value)
            except ValidationError as exc:
                tentative_errors[row_id] = type(exc).__name__
            except Exception as exc:  # noqa: B902 (broken modules must be isolated)
                broken.append((name, _exception_message(exc)))
                module_broken = True
                break
            else:
                tentative_valid[row_id] = compact
        if module_broken:
            continue
        for row_id, compact in tentative_valid.items():
            outcomes[row_id]['valid'][name] = compact
        for row_id, error in tentative_errors.items():
            outcomes[row_id]['errors'][name] = error
    return outcomes, broken


def _relation_rule(relation: str, column: str, supplier: Any) -> str:
    """Build the human-readable description of a relation rule."""
    if relation == RELATION_SUPPLIER:
        return (
            "all rows of supplier %r in column %r must share one number "
            "type" % (supplier, column))
    return "all rows of column %r must share one number type" % column


def _minimal_hitting_sets(
    row_sets: list[tuple[Any, frozenset[str]]],
) -> tuple[tuple[Any, ...], ...]:
    """Return the inclusion-minimal sets of rows whose removal leaves a
    common candidate module.

    For every candidate module the rows that do not support it form a set
    whose removal restores consistency; only the inclusion-minimal of those
    sets are returned.
    """
    modules = frozenset().union(*(valid for _row, valid in row_sets))
    solutions: set[frozenset[Any]] = set()
    for module in modules:
        # this is only called when no module validates every row, so the
        # set of rows missing each module is never empty
        missing = frozenset(
            row_id for row_id, valid in row_sets if module not in valid)
        solutions.add(missing)
    minimal = [
        solution for solution in solutions
        if not any(other < solution for other in solutions)]
    minimal.sort(key=lambda solution: (
        len(solution), tuple(str(value) for value in solution)))
    return tuple(
        tuple(sorted(solution, key=lambda value: str(value)))
        for solution in minimal)


def _narrow_group(
    group: list[dict[str, Any]],
    outcomes: dict[Any, dict[str, Any]],
    relation: str,
    column: str,
    supplier: Any,
    scope: str,
) -> tuple[dict[Any, frozenset[str]], tuple[Conflict, ...]]:
    """Apply a relation to one group of rows.

    Returns the narrowed feasible modules per row when every consistent
    explanation shares a module, or a conflict when no single number type
    can be assigned to the whole group.
    """
    row_sets = [
        (row['row'], frozenset(outcomes[row['row']]['valid']))
        for row in group if outcomes[row['row']]['valid']]
    if len(row_sets) < 2:
        return {}, ()
    intersection = frozenset.intersection(*(valid for _row, valid in row_sets))
    if intersection:
        return {row_id: intersection for row_id, _valid in row_sets}, ()
    conflict = Conflict(
        scope=scope,
        key=column if scope == RELATION_COLUMN else supplier,
        rows=tuple(row_id for row_id, _valid in row_sets),
        candidates=tuple(sorted(frozenset().union(
            *(valid for _row, valid in row_sets)))),
        rules=(_relation_rule(relation, column, supplier),),
        hitting_sets=_minimal_hitting_sets(row_sets))
    return {}, (conflict,)


def _apply_relation(
    relation: Any,
    column: str,
    column_rows: list[dict[str, Any]],
    outcomes: dict[Any, dict[str, Any]],
) -> tuple[dict[Any, frozenset[str]], list[Conflict]]:
    """Narrow the feasible modules using the relation stated for a column."""
    narrowed: dict[Any, frozenset[str]] = {}
    conflicts: list[Conflict] = []
    if relation == RELATION_SUPPLIER:
        groups: dict[Any, list[dict[str, Any]]] = {}
        for row in column_rows:
            if 'supplier' not in row:
                raise ValueError(
                    'row %r has no supplier for supplier relation'
                    % row['row'])
            groups.setdefault(row['supplier'], []).append(row)
        for supplier, group in groups.items():
            group_narrowed, group_conflicts = _narrow_group(
                group, outcomes, relation, column, supplier,
                RELATION_SUPPLIER)
            narrowed.update(group_narrowed)
            conflicts.extend(group_conflicts)
    elif relation == RELATION_COLUMN:
        narrowed, conflicts = _narrow_group(
            column_rows, outcomes, relation, column, None,
            RELATION_COLUMN)
    elif relation is not None:
        raise ValueError('unknown relation: %r' % relation)
    return narrowed, conflicts


def _build_preview(
    row: dict[str, Any],
    column: str,
    outcomes: dict[Any, dict[str, Any]],
    narrowed: dict[Any, frozenset[str]],
) -> RowPreview:
    """Build the row preview from its validation outcome."""
    row_id = row['row']
    valid = outcomes[row_id]['valid']
    errors = outcomes[row_id]['errors']
    feasible = narrowed.get(row_id, frozenset(valid))
    if not feasible:
        status = 'invalid'
        module_name = None
        compact = None
    elif len(feasible) == 1:
        status = 'committed'
        module_name = next(iter(feasible))
        compact = valid[module_name]
    else:
        status = 'ambiguous'
        module_name = None
        compact = None
    return RowPreview(
        row=row_id,
        column=column,
        supplier=row.get('supplier'),
        value=row['value'],
        status=status,
        module=module_name,
        compact=compact,
        candidates=tuple(sorted(feasible)),
        errors=dict(errors))


def _prepare_rows(
    rows: list[dict[str, Any]],
    columns: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assign a row identifier to every row and check its column."""
    prepared: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        column = row['column']
        if column not in columns:
            raise ValueError('unknown column: %r' % column)
        prepared.append(dict(row, row=row.get('row', index)))
    return prepared


def preview(
    rows: list[dict[str, Any]],
    columns: dict[str, dict[str, Any]],
) -> RoutingPreview:
    """Validate every row against the candidates of its column and narrow
    the possible number types using the relations stated by the caller.

    Each row is a dictionary with a ``column`` key, a ``value`` key and, for
    columns with a supplier relation, a ``supplier`` key. An optional ``row``
    key provides the row identifier that is echoed in the result.

    Each column configuration may state ``names`` (``(country, purpose)``
    pairs, aliases allowed), explicit ``modules`` (dotted names, module
    objects or ``(country, purpose)`` pairs) and a ``relation`` of
    ``'column'`` (the whole column holds one number type) or ``'supplier'``
    (rows of one supplier share a number type).
    """
    prepared = _prepare_rows(rows, columns)
    previews: dict[Any, RowPreview] = {}
    committed: list[RowPreview] = []
    ambiguous: list[RowPreview] = []
    invalid: list[RowPreview] = []
    conflicts: list[Conflict] = []
    broken: dict[tuple[str, str], None] = {}
    for column, config in columns.items():
        column_rows = [row for row in prepared if row['column'] == column]
        if not column_rows:
            continue
        candidates, resolve_broken = _resolve_candidates(config)
        for item in resolve_broken:
            broken.setdefault(item, None)
        outcomes, validation_broken = _validate_rows(column_rows, candidates)
        for item in validation_broken:
            broken.setdefault(item, None)
        narrowed, column_conflicts = _apply_relation(
            config.get('relation'), column, column_rows, outcomes)
        conflicts.extend(column_conflicts)
        for row in column_rows:
            row_preview = _build_preview(row, column, outcomes, narrowed)
            previews[row['row']] = row_preview
            if row_preview.status == 'committed':
                committed.append(row_preview)
            elif row_preview.status == 'ambiguous':
                ambiguous.append(row_preview)
            else:
                invalid.append(row_preview)
    ordered = tuple(previews[row['row']] for row in prepared)
    return RoutingPreview(
        rows=ordered,
        committed=tuple(committed),
        ambiguous=tuple(ambiguous),
        invalid=tuple(invalid),
        conflicts=tuple(conflicts),
        broken_modules=tuple(broken),
        consistent=not conflicts)
