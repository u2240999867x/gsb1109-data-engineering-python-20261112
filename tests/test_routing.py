# test_routing.py - unit tests for the stdnum.routing module
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

"""Tests for the routing preview of supplier master data columns."""

import pytest

from stdnum import ean, routing
from stdnum.de import idnr, vat


COLUMNS = {
    'tax_id': {
        'names': [('de', 'vat'), ('de', 'idnr'), ('de', 'stnr')],
    },
}

ROWS = [
    {'row': 0, 'column': 'tax_id', 'value': '136695976'},
    {'row': 1, 'column': 'tax_id', 'value': '65299970078'},
    {'row': 2, 'column': 'tax_id', 'value': '36574261809'},
    {'row': 3, 'column': 'tax_id', 'value': '123456780'},
]


class _BrokenModule:
    """A module-like object whose validate() always fails badly."""

    __name__ = 'stdnum.fake.broken'

    def validate(self, value: str) -> str:
        raise RuntimeError('boom')


class _PlainObject:
    """An object that does not look like a validation module."""

    __name__ = 'stdnum.fake.plain'


class _EmptyMessageModule:
    """A module-like object that raises an exception without a message."""

    __name__ = 'stdnum.fake.empty'

    def validate(self, value: str) -> str:
        raise RuntimeError()


def test_get_candidates_deduplicates_aliases() -> None:
    """Old aliases that resolve to the same module count once."""
    assert routing.get_candidates(
        {'names': [('at', 'vat'), ('at', 'uid')]}) == ('stdnum.at.uid',)
    assert routing.get_candidates(
        {'modules': ['stdnum.de.vat', ('de', 'vat'), vat]}) == \
        ('stdnum.de.vat',)
    assert routing.get_candidates({'modules': ['stdnum.ean', ean]}) == \
        ('stdnum.ean',)


def test_preview_without_relation_keeps_row_level_outcomes() -> None:
    """Without a stated relation every row is judged on its own."""
    result = routing.preview(ROWS, COLUMNS)
    assert result.consistent is True
    assert result.conflicts == ()
    assert result.broken_modules == ()
    statuses = [(row.row, row.status, row.module) for row in result.rows]
    assert statuses == [
        (0, 'committed', 'stdnum.de.vat'),
        (1, 'committed', 'stdnum.de.stnr'),
        (2, 'ambiguous', None),
        (3, 'invalid', None),
    ]
    assert result[2].candidates == ('stdnum.de.idnr', 'stdnum.de.stnr')
    assert result[2].compact is None
    assert result[0].compact == '136695976'
    invalid = result[3]
    assert invalid.errors['stdnum.de.vat'] == 'InvalidChecksum'
    assert invalid.errors['stdnum.de.idnr'] == 'InvalidLength'
    assert [row.row for row in result.committed] == [0, 1]
    assert [row.row for row in result.ambiguous] == [2]
    assert [row.row for row in result.invalid] == [3]
    assert result[0] is result.rows[0]
    with pytest.raises(KeyError):
        result[99]


def test_default_row_identifiers_are_positions() -> None:
    """Rows without an explicit row id get their position as id."""
    rows = [dict(row) for row in ROWS]
    for row in rows:
        del row['row']
    result = routing.preview(rows, COLUMNS)
    assert [row.row for row in result.rows] == [0, 1, 2, 3]


def test_column_relation_narrows_to_common_module() -> None:
    """A consistent column relation commits the rows on the shared module."""
    columns = {'tax_id': dict(COLUMNS['tax_id'], relation='column')}
    result = routing.preview(ROWS[1:3], columns)
    assert result.consistent is True
    assert [(row.row, row.status, row.module) for row in result.rows] == [
        (1, 'committed', 'stdnum.de.stnr'),
        (2, 'committed', 'stdnum.de.stnr'),
    ]
    assert result[2].candidates == ('stdnum.de.stnr',)


def test_column_relation_reports_conflict_and_hitting_sets() -> None:
    """An inconsistent column relation names rows, rules and repair sets."""
    columns = {'tax_id': dict(COLUMNS['tax_id'], relation='column')}
    result = routing.preview(ROWS[:3], columns)
    assert result.consistent is False
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.scope == 'column'
    assert conflict.key == 'tax_id'
    assert conflict.rows == (0, 1, 2)
    assert conflict.candidates == (
        'stdnum.de.idnr', 'stdnum.de.stnr', 'stdnum.de.vat')
    assert conflict.hitting_sets == ((0,), (1, 2))
    assert conflict.rules == (
        "all rows of column 'tax_id' must share one number type",)
    # rows keep their row-level statuses next to the conflict report
    assert [(row.row, row.status) for row in result.rows] == [
        (0, 'committed'), (1, 'committed'), (2, 'ambiguous')]


def test_single_row_column_relation_has_no_conflict() -> None:
    """A relation on a single row cannot conflict with anything."""
    columns = {'tax_id': dict(COLUMNS['tax_id'], relation='column')}
    result = routing.preview(ROWS[2:3], columns)
    assert result.consistent is True
    assert result[2].status == 'ambiguous'


def test_supplier_relation_scopes_narrowing_per_supplier() -> None:
    """Each supplier gets its own consistent assignment."""
    supplier_rows = [
        {'row': 0, 'column': 'tax_id', 'supplier': 'A',
         'value': '136695976'},
        {'row': 1, 'column': 'tax_id', 'supplier': 'A',
         'value': '36574261809'},
        {'row': 2, 'column': 'tax_id', 'supplier': 'B',
         'value': '65299970078'},
        {'row': 3, 'column': 'tax_id', 'supplier': 'B',
         'value': '86035742796'},
    ]
    columns = {'tax_id': dict(COLUMNS['tax_id'], relation='supplier')}
    result = routing.preview(supplier_rows, columns)
    assert result.consistent is False
    conflict = result.conflicts[0]
    assert conflict.scope == 'supplier'
    assert conflict.key == 'A'
    assert conflict.rows == (0, 1)
    assert conflict.hitting_sets == ((0,), (1,))
    assert conflict.rules == (
        "all rows of supplier 'A' in column 'tax_id' must share one "
        "number type",)
    committed = [(row.row, row.module) for row in result.committed]
    assert committed == [
        (0, 'stdnum.de.vat'),
        (2, 'stdnum.de.stnr'),
        (3, 'stdnum.de.stnr'),
    ]
    assert result[1].status == 'ambiguous'
    assert result[1].supplier == 'A'


def test_supplier_relation_requires_supplier_key() -> None:
    """A missing supplier key is a caller error, not an invalid row."""
    columns = {'tax_id': dict(COLUMNS['tax_id'], relation='supplier')}
    with pytest.raises(ValueError, match='has no supplier'):
        routing.preview(ROWS[:1], columns)


def test_broken_modules_are_separated_from_invalid_values() -> None:
    """Unresolvable and failing modules never pollute row errors."""
    config = {
        'modules': [
            'stdnum.de.missing',
            'stdnum.too.many.parts',
            'stdnum.ghost',
            'stdnum.de',
            _BrokenModule(),
            _PlainObject(),
            _EmptyMessageModule(),
            (123, 'vat'),
            ('de', 'vat', 'extra'),
        ],
    }
    result = routing.preview(
        [{'row': 0, 'column': 'tax_id', 'value': '123456780'}],
        {'tax_id': config})
    names = [name for name, _message in result.broken_modules]
    assert 'stdnum.de.missing' in names
    assert 'stdnum.too.many.parts' in names
    assert 'stdnum.ghost' in names
    assert 'stdnum.de' in names
    assert 'stdnum.fake.broken' in names
    assert 'stdnum.fake.plain' in names
    assert 'stdnum.fake.empty' in names
    assert 'stdnum.123.vat' in names
    assert "('de', 'vat', 'extra')" in names
    messages = dict(result.broken_modules)
    assert messages['stdnum.de.missing'] == 'module could not be found'
    assert messages['stdnum.fake.broken'] == 'boom'
    assert messages['stdnum.fake.empty'] == 'RuntimeError'
    assert messages['stdnum.de'] == 'module has no validate() function'
    assert messages['stdnum.fake.plain'] == 'module could not be found'
    assert 'too many values to unpack' in messages["('de', 'vat', 'extra')"]
    # no working candidate means the row is invalid, not ambiguous
    assert result.rows[0].status == 'invalid'
    assert result.rows[0].errors == {}


def test_broken_module_is_dropped_before_other_rows_validate() -> None:
    """A module that blows up on one row still validates the others."""
    config = {'modules': [_BrokenModule(), idnr]}
    result = routing.preview(ROWS[1:3], {'tax_id': config})
    assert result.broken_modules == (
        ('stdnum.fake.broken', 'boom'),)
    assert result[1].status == 'invalid'
    assert result[2].status == 'committed'
    assert result[2].module == 'stdnum.de.idnr'


def test_non_string_value_is_invalid_not_broken() -> None:
    """Non-string values are reported as a row error."""
    rows = [
        {'row': 0, 'column': 'tax_id', 'value': 123456780},
        {'row': 1, 'column': 'tax_id', 'value': None},
    ]
    result = routing.preview(rows, COLUMNS)
    assert result.broken_modules == ()
    assert all(row.status == 'invalid' for row in result.rows)
    assert result[0].errors == {'__value__': 'value must be a string'}
    assert result[1].errors == {'__value__': 'value must be a string'}


def test_unknown_column_and_relation_are_rejected() -> None:
    """Bad configuration fails loudly instead of silently dropping rows."""
    with pytest.raises(ValueError, match='unknown column'):
        routing.preview(
            [{'row': 0, 'column': 'other', 'value': 'x'}], COLUMNS)
    columns = {'tax_id': dict(COLUMNS['tax_id'], relation='mystery')}
    with pytest.raises(ValueError, match='unknown relation'):
        routing.preview(ROWS[:1], columns)


def test_configured_column_without_rows_is_skipped() -> None:
    """Columns without any rows simply produce nothing."""
    result = routing.preview([], COLUMNS)
    assert result.rows == ()
    assert result.committed == ()
    assert result.consistent is True


def test_multiple_columns_are_previewed_independently() -> None:
    """Rows of different columns use their own candidate sets."""
    columns = {
        'tax_id': COLUMNS['tax_id'],
        'ean': {'modules': [ean]},
    }
    rows = [
        {'row': 0, 'column': 'tax_id', 'value': '136695976'},
        {'row': 1, 'column': 'ean', 'value': '9780143007234'},
    ]
    result = routing.preview(rows, columns)
    assert result[0].module == 'stdnum.de.vat'
    assert result[1].module == 'stdnum.ean'
    assert result[1].column == 'ean'
    assert result[1].compact == '9780143007234'
