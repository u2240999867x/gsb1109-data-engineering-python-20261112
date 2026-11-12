# test_numdb.py - tests for the stdnum.numdb module
# coding: utf-8
#
# Copyright (C) 2025 Arthur de Jong
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

"""Tests for malformed database handling in stdnum.numdb."""

import io

from stdnum import numdb


def _read(text):
    """Read a database from a string."""
    return numdb.read(io.StringIO(text))


def test_valid_file_has_no_errors():
    """A well-formed file parses without errors."""
    db = _read(
        '0-8 prop1="foo"\n'
        '  100-199 prop2="bar"\n'
        '  200,300-399 prop3="baz"\n'
        '    333 prop4="bax"\n'
        '9 prop1="booz"\n')
    assert db.parsing_errors == []
    assert db.split('01500') == ['0', '150', '0']
    assert db.info('03339') == [
        ('0', {'prop1': 'foo'}),
        ('333', {'prop3': 'baz'}),
        ('9', {})]
    assert db.info('0333333') == [
        ('0', {'prop1': 'foo'}),
        ('333', {'prop3': 'baz'}),
        ('333', {'prop4': 'bax'})]


def test_comments_and_blank_lines_are_ignored():
    """Indented comments and whitespace-only lines do not break parsing."""
    db = _read(
        '# top comment\n'
        '  # indented comment\n'
        '   \n'
        '0-9\n'
        '  # another comment\n'
        '  100\n')
    assert db.parsing_errors == []
    assert db.split('01005') == ['0', '100', '5']


def test_empty_file():
    """An empty file yields an empty database without errors."""
    db = _read('')
    assert db.prefixes == []
    assert db.parsing_errors == []


def test_malformed_property_keeps_line():
    """A bad property fragment is reported but the range stays usable."""
    db = _read('0-8 prop1="ok" bad=??\n  100\n')
    (error,) = db.parsing_errors
    assert (error.line, error.column, error.end_column) == (1, 16, 22)
    assert error.message == "malformed property: 'bad=??'"
    assert error.end_line == 1
    assert error.children == ()
    assert db.split('01005') == ['0', '100', '5']
    assert db.info('01005')[0][1] == {'prop1': 'ok'}


def test_each_bad_fragment_is_reported_separately():
    """All bad fragments of one read are listed, not just the first."""
    db = _read(
        '0-9 prop1="ok" junk=??\n'
        '  100,9-5,200 other=!\n'
        '5\n')
    messages = [(error.line, error.message) for error in db.parsing_errors]
    assert messages == [
        (1, "malformed property: 'junk=??'"),
        (2, "range '9-5' is inverted"),
        (2, "malformed property: 'other=!'")]
    # the valid ranges of line 2 are not poisoned by the broken fragment
    assert db.split('01005') == ['0', '100', '5']
    assert db.split('02005') == ['0', '200', '5']


def test_bad_range_span_points_at_token():
    """Range errors report the exact character span of the bad token."""
    db = _read('0-9\n  100,,200\n')
    (empty,) = [e for e in db.parsing_errors if e.message == 'empty range']
    assert (empty.line, empty.column, empty.end_column) == (2, 6, 7)
    db = _read('0-9\n  100,1-22\n')
    (error,) = db.parsing_errors
    assert (error.column, error.end_column) == (7, 11)
    assert 'unequal length' in error.message
    db = _read('0-9\n  100,1-2-3\n')
    (error,) = db.parsing_errors
    assert (error.column, error.end_column) == (7, 12)
    assert error.message == "malformed range: '1-2-3'"
    db = _read('0-9\n  1-22\n')
    (quarantine,) = db.parsing_errors
    (error,) = quarantine.children
    assert (error.column, error.end_column) == (3, 7)
    assert 'unequal length' in error.message


def test_quarantined_subtree_reports_implicated_rows():
    """A fully broken line isolates its nested rows until a common ancestor."""
    db = _read(
        '0-8 prop1="foo"\n'
        '  200,300-399 prop2="bar"\n'
        '    333 prop3="baz"\n'
        '9-5 prop1="oops"\n'
        '  100-199 prop2="child"\n'
        '90-99 prop4="ok"\n')
    (error,) = db.parsing_errors
    assert error.line == 4
    assert error.end_line == 5
    assert error.column == 1
    assert error.end_column == 24
    assert error.message == 'no usable number range on line'
    assert [c.message for c in error.children] == [
        "range '9-5' is inverted",
        'line discarded with quarantined subtree']
    # parsing recovered at the root level
    assert db.info('95123') == [('95', {'prop4': 'ok'}), ('123', {})]
    # earlier valid subtree is untouched
    assert db.split('03339') == ['0', '333', '9']


def test_quarantine_closes_at_nearest_known_ancestor():
    """Deeper and sibling rows are skipped; indentation back to the parent
    level resumes parsing."""
    db = _read(
        '0-9\n'
        '  9-5\n'
        '    1\n'
        '    2\n'
        '  100\n'
        '3\n')
    (error,) = db.parsing_errors
    assert (error.line, error.end_line) == (2, 4)
    assert [c.line for c in error.children] == [2, 3, 4]
    assert [c.message for c in error.children] == [
        "range '9-5' is inverted",
        'line discarded with quarantined subtree',
        'line discarded with quarantined subtree']
    assert db.split('01005') == ['0', '100', '5']
    assert db.split('31') == ['3', '1']


def test_unexpected_indentation_without_parent():
    """A first line that is over-indented cannot attach anywhere."""
    db = _read('    100-199\n  200\n0-9\n  300\n')
    (error,) = db.parsing_errors
    assert (error.line, error.column, error.end_line, error.end_column) == (
        1, 1, 2, 6)
    assert 'unexpected indentation' in error.message
    assert [c.line for c in error.children] == [2]
    assert db.split('03001') == ['0', '300', '1']


def test_unparseable_line():
    """A line that cannot be tokenised at all is reported and skipped."""
    db = _read('0-9\n  100\n,\n  200\n3\n')
    (error,) = db.parsing_errors
    assert error.line == 3
    assert error.column == 1
    assert error.message == 'unparseable line'
    assert error.quarantined is True
    assert db.split('01005') == ['0', '100', '5']
    assert db.split('02005') == ['0', '200', '5']


def test_unparseable_line_extends_open_quarantine():
    """An unparseable row nested in a bad subtree extends the subtree span."""
    db = _read('0-9\n  9-5\n    1\n,,,,\n3\n')
    (error,) = db.parsing_errors
    assert (error.line, error.end_line) == (2, 4)
    messages = [child.message for child in error.children]
    assert 'unparseable line' in messages
    assert all(child.quarantined for child in error.children)
    assert db.split('30') == ['3', '0']


def test_dedent_to_unseen_level_attaches_to_ancestor():
    """A return at an indentation never seen before binds to the nearest
    known ancestor instead of crashing."""
    db = _read(
        '0-9\n'
        '    100\n'
        '    200\n'
        '  300\n'
        '5\n')
    assert db.parsing_errors == []
    assert db.split('01005') == ['0', '100', '5']
    assert db.split('03005') == ['0', '300', '5']
    assert db.split('50') == ['5', '0']


def test_bad_property_between_valid_properties():
    """A bad fragment sandwiched between valid properties keeps both."""
    db = _read('0-9 a="1" junk=?? b="2"\n  100\n')
    (error,) = db.parsing_errors
    assert error.message == "malformed property: 'junk=??'"
    assert db.info('01005')[0][1] == {'a': '1', 'b': '2'}


def test_embedded_quote_residue_is_tolerated():
    """Quote residues without an assignment are ignored as before."""
    db = _read('0-9 operator="SIA "UNISTARS""\n  100\n')
    assert db.parsing_errors == []
    assert db.info('01005')[0][1] == {'operator': 'SIA '}


def test_get_returns_cached_database():
    """get() loads a shipped database and caches the parsed result."""
    database = numdb.get('isbn')
    assert database is numdb.get('isbn')
    assert database.parsing_errors == []
    assert database.split('978030640615')[:1] == ['978']


def test_fragment_errors_are_not_marked_quarantined():
    """A bad fragment on a committed line stays separately fixable."""
    db = _read('0-9 p="ok" bad=??\n  100\n')
    (error,) = db.parsing_errors
    assert error.quarantined is False
    subtree = _read('0-9\n  9-5\n    1\n3\n').parsing_errors[0]
    assert subtree.quarantined is True


def test_quarantine_open_at_end_of_file():
    """A broken subtree that reaches EOF still gets a closed span."""
    db = _read('0-9\n  9-5\n    1\n')
    (error,) = db.parsing_errors
    assert (error.line, error.end_line) == (2, 3)
    assert error.end_column == 6


def test_multiple_bad_subtrees_are_all_listed():
    """One read reports every independently repairable bad subtree."""
    db = _read(
        '0-9\n'
        '  9-5\n'
        '    1\n'
        '  8-4\n'
        '    2\n'
        '  100\n'
        '3\n')
    assert len(db.parsing_errors) == 2
    assert [error.line for error in db.parsing_errors] == [2, 4]
    assert db.split('01005') == ['0', '100', '5']


def test_multi_range_shared_children_survive_partial_failure():
    """Ranges on one line share their children; a bad range must not leave
    a valid range without its children."""
    db = _read(
        '0-9\n'
        '  100,9-5,200-299\n'
        '    333 prop1="x"\n'
        '5\n')
    assert [e.message for e in db.parsing_errors] == ["range '9-5' is inverted"]
    assert db.split('0100333') == ['0', '100', '333']
    assert db.split('0200333') == ['0', '200', '333']
    for number in ('0100333', '0200333'):
        assert db.info(number)[2][1] == {'prop1': 'x'}


def test_parse_error_locations_and_repr():
    """ParseError renders a readable location and an informative repr."""
    error = numdb.ParseError('boom', 3, 2, 5)
    assert str(error) == '3:2-5: boom'
    nested = numdb.ParseError('sub', 4, 1, 2, end_line=6, children=[error])
    assert str(nested) == '4:1-6:2: sub'
    rendered = repr(nested)
    assert rendered.startswith('ParseError(')
    assert "line=4, column=1, end_line=6, end_column=2" in rendered
    assert "children=(ParseError('boom', line=3, column=2" in rendered


def test_committed_part_matches_subtree_removal():
    """The committed database equals a fresh read after deleting every
    complete quarantined subtree using the reported spans. Bad fragments on
    committed lines are reported but do not require removing the line."""
    source_lines = [
        '0-8 prop1="foo"\n',
        '  200,300-399 prop2="bar"\n',
        '    333 prop3="baz"\n',
        '9-5 prop1="oops"\n',
        '  100-199 prop2="child"\n',
        '  8-4 prop2="more"\n',
        '    1 prop3="deep"\n',
        '20-29 prop4="ok"\n',
        '  7-7\n',
        '30-39 prop5="fine"\n',
        '40-49 p="ok" bad=??\n',
    ]
    db = _read(''.join(source_lines))
    # one read lists both the isolated subtree and the fixable fragment
    assert any(error.quarantined for error in db.parsing_errors)
    assert any(not error.quarantined for error in db.parsing_errors)
    removed = set()
    for error in db.parsing_errors:
        if error.quarantined:
            removed.update(range(error.line, error.end_line + 1))
    cleaned = ''.join(
        line for number, line in enumerate(source_lines, 1)
        if number not in removed)
    clean_db = _read(cleaned)
    # the remaining file no longer isolates anything, though the fixable
    # fragment is still reported
    assert not [e for e in clean_db.parsing_errors if e.quarantined]
    assert db.prefixes == clean_db.prefixes
    samples = (
        '02003330', '03503339', '25123', '35000', '99999', '31', '07777',
        '40123', '20700')
    for number in samples:
        assert db.split(number) == clean_db.split(number)
        assert db.info(number) == clean_db.info(number)


def test_all_shipped_databases_parse_cleanly():
    """The data files shipped with the library contain no bad rows."""
    import pathlib
    for path in pathlib.Path(numdb.__file__).parent.rglob('*.dat'):
        with open(path, encoding='utf-8') as fp:
            db = numdb.read(fp)
        assert db.parsing_errors == [], path
