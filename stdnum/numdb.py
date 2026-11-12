# numdb.py - module for handling hierarchically organised numbers
#
# Copyright (C) 2010-2023 Arthur de Jong
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

"""Query structured number format files with number properties.

This module contains functions for reading and querying a database that
stores numbers that use a hierarchical format (e.g. ISBN, IBAN, phone
numbers, etc).

To read a database from a file:

>>> with open('tests/numdb-test.dat', 'r') as f:
...     dbfile = read(f)

To split a number:

>>> dbfile.split('01006')
['0', '100', '6']
>>> dbfile.split('902006')
['90', '20', '06']
>>> dbfile.split('909856')
['90', '985', '6']

To split the number and get properties for each part:

>>> import pprint
>>> pprint.pprint(dbfile.info('01006'))
[('0', {'prop1': 'foo'}), ('100', {'prop2': 'bar'}), ('6', {})]
>>> pprint.pprint(dbfile.info('02006'))
[('0', {'prop1': 'foo'}), ('200', {'prop2': 'bar', 'prop3': 'baz'}), ('6', {})]
>>> pprint.pprint(dbfile.info('03456'))
[('0', {'prop1': 'foo'}), ('345', {'prop2': 'bar', 'prop3': 'baz'}), ('6', {})]
>>> pprint.pprint(dbfile.info('902006'))
[('90', {'prop1': 'booz'}), ('20', {'prop2': 'foo'}), ('06', {})]
>>> pprint.pprint(dbfile.info('909856'))
[('90', {'prop1': 'booz'}), ('985', {'prop2': 'fooz'}), ('6', {})]
>>> pprint.pprint(dbfile.info('9889'))
[('98', {'prop1': 'booz'}), ('89', {'prop2': 'foo'})]
>>> pprint.pprint(dbfile.info('633322'))
[('6', {'prop1': 'boo'}), ('333', {'prop2': 'bar', 'prop3': 'baz', 'prop4': 'bla'}), ('22', {})]
>>> pprint.pprint(dbfile.info('1200333'))
[('1', {'prop1': 'foo'}), ('200', {'prop2': 'bar', 'prop3': 'baz'}), ('333', {'prop4': 'bax'})]

Malformed lines do not abort reading. The recoverable part of the database
is built and every problem is reported in parsing_errors. Each ParseError
points at the source line and the character span of the offending text;
when a whole broken subtree is quarantined the error span covers the
subtree and the discarded nested rows are linked as children. Parsing
resumes as soon as the indentation returns to a common ancestor:

>>> import io
>>> broken = io.StringIO('''
... A-B prop1="foo"
...   100,200-299 prop2="bar"
...     333 prop3="baz"
... 9-5 prop1="oops"
...   100-199 prop2="child"
... C-D prop4="ok"
... ''')
>>> db = read(broken)
>>> [(error.line, error.end_line, error.message) for error in db.parsing_errors]
[(5, 6, 'no usable number range on line')]
>>> error = db.parsing_errors[0]
>>> (error.column, error.end_column)
(1, 24)
>>> [(child.line, child.message) for child in error.children]
[(5, "range '9-5' is inverted"), (6, 'line discarded with quarantined subtree')]
>>> pprint.pprint(db.info('C5123'))
[('C', {'prop4': 'ok'}), ('5123', {})]
>>> db.split('A23330')
['A', '233', '30']

A line that mixes usable and unusable fragments keeps the usable part and
reports each bad fragment separately instead of poisoning later ranges:

>>> db = read(io.StringIO('0-8 prop1="ok" bad=??\\n  100,200-299\\n'))
>>> [(error.line, error.column, error.end_column) for error in db.parsing_errors]
[(1, 16, 22)]
>>> db.split('01005')
['0', '100', '5']

An indentation that has no parent entry quarantines the dangling subtree
until a line returns to a level that has a known ancestor:

>>> db = read(io.StringIO('    100-199\\n  200\\n0-9\\n  300\\n'))
>>> error = db.parsing_errors[0]
>>> (error.line, error.column, error.end_line, error.end_column, error.message)
(1, 1, 2, 6, 'unexpected indentation of 4 spaces; no parent entry found')
>>> db.split('03001')
['0', '300', '1']

"""

from __future__ import annotations

import re
from typing import NamedTuple


TYPE_CHECKING = False
if TYPE_CHECKING:  # pragma: no cover (only used when type checking)
    from collections.abc import Iterable, Iterator
    from typing import IO, Any
    PrefixInfo = tuple[int, str, str, dict[str, str], list['PrefixInfo']]
    BadSpan = tuple[int, int, str]
    RangeInfo = tuple[str, str, str, int, int]


_line_re = re.compile(
    r'^(?P<indent> *)'
    r'(?P<ranges>[^,\s]*(?:,[^,\s]*)*)\s*'
    r'(?P<props>.*)$')
_prop_re = re.compile(
    r'(?P<prop>[0-9a-zA-Z-_]+)="(?P<value>[^"]*)"')
_range_token_re = re.compile(r'^[0-9a-zA-Z_$]+(-[0-9a-zA-Z_$]+)?$')

# this is a cache of open databases
_open_databases = {}

# the prefixes attribute of NumDB is structured as follows:
# prefixes = [
#   [ length, low, high, props, children ]
#   ...
# ]
# where children is a prefixes structure in its own right
# (there is no expected ordering within the list)


class ParseError(ValueError):
    """Description of a malformed part of a number database file.

    The error location is reported as a 1-based source line number and a
    half-open character span (column .. end_column). When end_line differs
    from line the span covers a whole quarantined subtree. The children
    attribute holds errors and discarded rows that are part of that
    subtree. The quarantined attribute is True when the covered lines were
    kept out of the database entirely; a False value marks a bad fragment
    on a line whose usable ranges were still committed.
    """

    def __init__(
        self,
        message: str,
        line: int,
        column: int,
        end_column: int | None = None,
        end_line: int | None = None,
        children: 'Iterable[ParseError] | None' = None,
        quarantined: bool = False,
    ) -> None:
        self.message = message
        self.line = line
        self.column = column
        self.end_column = end_column if end_column is not None else column
        self.end_line = end_line if end_line is not None else line
        self.children: tuple[ParseError, ...] = tuple(children or ())
        self.quarantined = quarantined
        super().__init__(message)

    def __str__(self) -> str:
        """Return a compact location and message."""
        if self.end_line != self.line:
            location = f'{self.line}:{self.column}-{self.end_line}:{self.end_column}'
        else:
            location = f'{self.line}:{self.column}-{self.end_column}'
        return f'{location}: {self.message}'

    def __repr__(self) -> str:
        """Return a representation that reconstructs the error."""
        return (
            f'ParseError({self.message!r}, line={self.line}, '
            f'column={self.column}, end_line={self.end_line}, '
            f'end_column={self.end_column}, children={self.children!r}, '
            f'quarantined={self.quarantined!r})')


class _ParsedLine(NamedTuple):
    """The parsed contents of a single non-comment source line."""

    line: int
    indent: int | None
    line_end: int
    range_start: int
    range_end: int
    ranges: 'list[RangeInfo]'
    props: dict[str, str]
    bad_ranges: 'list[BadSpan]'
    bad_props: 'list[BadSpan]'


class NumDB():
    """Number database."""

    prefixes: list[PrefixInfo]
    parsing_errors: list[ParseError]

    def __init__(self) -> None:
        """Construct an empty database."""
        self.prefixes = []
        self.parsing_errors = []

    @staticmethod
    def _find(number: str, prefixes: list[PrefixInfo]) -> list[tuple[str, dict[str, str]]]:
        """Lookup the specified number in the list of prefixes, this will
        return basically what info() should return but works recursively."""
        if not number:
            return []
        part = number
        properties: dict[str, Any] = {}
        next_prefixes: list[PrefixInfo] = []
        # go over prefixes and find matches
        for length, low, high, props, children in prefixes:
            if len(part) >= length and low <= part[:length] <= high:
                # only use information from the shortest match
                if length < len(part):
                    part = part[:length]
                    properties = {}
                    next_prefixes = []
                properties.update(props)
                next_prefixes.extend(children)
        # return first part and recursively find next matches
        return [(part, properties)] + NumDB._find(number[len(part):], next_prefixes)

    def info(self, number: str) -> list[tuple[str, dict[str, str]]]:
        """Split the provided number in components and associate properties
        with each component. This returns a tuple of tuples. Each tuple
        consists of a string (a part of the number) and a dict of properties.
        """
        return NumDB._find(number, self.prefixes)

    def split(self, number: str) -> list[str]:
        """Split the provided number in components. This returns a tuple with
        the number of components identified."""
        return [part for part, props in self.info(number)]


def _bad_prop_spans(props_text: str, base: int) -> 'list[BadSpan]':
    """Find property fragments that do not match the key="value" syntax.

    Only fragments containing an assignment are reported; stray text such
    as quote residues from values with embedded quotes is ignored to stay
    compatible with files that were accepted before strict validation.
    """
    bad: list[BadSpan] = []
    position = 0
    for match in _prop_re.finditer(props_text):
        between = props_text[position:match.start()]
        stripped = between.strip()
        if stripped and '=' in stripped:
            offset = position + between.index(stripped)
            bad.append((base + offset, base + offset + len(stripped), stripped))
        position = match.end()
    tail = props_text[position:].strip()
    if tail and '=' in tail:
        offset = position + props_text[position:].index(tail)
        bad.append((base + offset, base + offset + len(tail), tail))
    return bad


def _parse(fp: Iterable[str]) -> 'Iterator[_ParsedLine]':
    """Read lines of text from the file pointer and generate a description
    of each non-comment line with the exact character spans of the ranges,
    properties and any malformed fragments."""
    for line_number, raw_line in enumerate(fp, 1):
        line = raw_line.rstrip('\n\r')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = _line_re.search(line)
        if match is None or not match.group('ranges').replace(',', '').strip():
            yield _ParsedLine(
                line=line_number, indent=None, line_end=len(line) + 1,
                range_start=1, range_end=1, ranges=[], props={},
                bad_ranges=[], bad_props=[])
            continue
        indent = len(match.group('indent'))
        ranges_text = match.group('ranges')
        props_text = match.group('props')
        props = dict(_prop_re.findall(props_text))
        ranges: list[RangeInfo] = []
        bad_ranges: list[BadSpan] = []
        position = 0
        for rnge in ranges_text.split(','):
            column = match.start('ranges') + position + 1
            end_column = match.start('ranges') + position + len(rnge) + 1
            if not rnge:
                # point at the comma that introduces the empty range
                start = max(1, column - 1)
                bad_ranges.append((start, max(column, start + 1),
                                   'empty range'))
            elif not _range_token_re.match(rnge):
                bad_ranges.append((column, end_column, f'malformed range: {rnge!r}'))
            elif '-' in rnge:
                low, high = rnge.split('-', 1)
                if len(low) != len(high):
                    bad_ranges.append((
                        column, end_column,
                        f'range {rnge!r} has parts of unequal length'))
                elif low > high:
                    bad_ranges.append((
                        column, end_column, f'range {rnge!r} is inverted'))
                else:
                    ranges.append((rnge, low, high, column, end_column))
            else:
                ranges.append((rnge, rnge, rnge, column, end_column))
            position += len(rnge) + 1
        yield _ParsedLine(
            line=line_number, indent=indent, line_end=len(line) + 1,
            range_start=match.start('ranges') + 1,
            range_end=match.end('ranges') + 1,
            ranges=ranges, props=props, bad_ranges=bad_ranges,
            bad_props=_bad_prop_spans(props_text, match.start('props') + 1))


class _BuildContext:
    """Accumulator for the prefix entries of one (sub)tree while reading."""

    def __init__(self, stack: 'dict[int, list[PrefixInfo]]') -> None:
        self.stack = stack
        self.last_indent = min(stack)
        self.errors: list[ParseError] = []
        self.last_line = 0
        self.last_end = 1

    def list_for(self, indent: int) -> tuple[list[PrefixInfo] | None, ParseError | None]:
        """Return the list to append entries at the requested indentation.

        A deeper indentation than the previous line binds the level to the
        last entry's children (matching the behaviour of a file read without
        errors); a return to a previously unseen level binds it to the
        nearest known ancestor instead. A None target with an error means
        no parent entry exists to attach the line to.
        """
        if indent > self.last_indent or indent not in self.stack:
            if indent > self.last_indent:
                parent_indent = self.last_indent
            else:
                parent_indent = max(
                    level for level in self.stack if level <= indent)
            parent_list = self.stack[parent_indent]
            if not parent_list:
                return None, ParseError(
                    f'unexpected indentation of {indent} spaces; '
                    'no parent entry found',
                    self.last_line, 1, indent + 1)
            self.stack[indent] = parent_list[-1][4]
        self.last_indent = indent
        return self.stack[indent], None


class _Quarantine:
    """A broken subtree that is kept out of the database while reading.

    The threshold is the indentation level at which parsing resumes: any
    later line at or above that level closes the quarantined subtree.
    """

    def __init__(self, threshold: int, error: ParseError) -> None:
        self.threshold = threshold
        self.error = error
        self.children: list[ParseError] = list(error.children)
        self.last_line = error.line
        self.last_end = error.end_column


def read(fp: Iterable[str]) -> NumDB:
    """Return a new database with the data read from the specified file.

    Malformed lines and their subtrees are quarantined and reported in the
    returned database's parsing_errors attribute instead of aborting the
    read. The entries that remain are exactly those that would have been
    read after removing each complete broken subtree from the source file.
    """
    db = NumDB()
    current = _BuildContext({0: db.prefixes})
    quarantines: list[_Quarantine] = []

    for parsed in _parse(fp):
        current.last_line = parsed.line
        current.last_end = parsed.line_end
        # a line at or above a quarantine threshold closes that subtree
        while quarantines and parsed.indent is not None and \
                parsed.indent <= quarantines[-1].threshold:
            quarantine = quarantines.pop()
            quarantine.error.end_line = quarantine.last_line
            quarantine.error.end_column = quarantine.last_end
            quarantine.error.children = tuple(quarantine.children)
            quarantine.error.quarantined = True
            for child in quarantine.error.children:
                child.quarantined = True
            current.errors.append(quarantine.error)
        if parsed.indent is None:
            error = ParseError(
                'unparseable line', parsed.line, 1, parsed.line_end,
                quarantined=True)
            if quarantines:
                # the line cannot be attached anywhere and belongs to the
                # open subtree; extend its span to cover this row too
                quarantine = quarantines[-1]
                quarantine.last_line = parsed.line
                quarantine.last_end = parsed.line_end
                quarantine.children.append(error)
            else:
                current.errors.append(error)
            continue
        fragment_errors = [
            ParseError(message, parsed.line, column, end_column)
            for column, end_column, message in parsed.bad_ranges]
        fragment_errors.extend(
            ParseError(f'malformed property: {text!r}',
                       parsed.line, column, end_column)
            for column, end_column, text in parsed.bad_props)
        if quarantines:
            # the whole subtree is isolated; still report every bad row
            quarantine = quarantines[-1]
            quarantine.last_line = parsed.line
            quarantine.last_end = parsed.line_end
            quarantine.children.extend(fragment_errors)
            quarantine.children.append(ParseError(
                'line discarded with quarantined subtree',
                parsed.line, 1, parsed.line_end))
            continue
        if not parsed.ranges:
            # no usable fragment on this line: quarantine the line and
            # whatever nested rows claim to be its children
            error = ParseError(
                'no usable number range on line',
                parsed.line, parsed.range_start, parsed.range_end,
                children=fragment_errors, quarantined=True)
            quarantines.append(_Quarantine(parsed.indent, error))
            continue
        current.errors.extend(fragment_errors)
        target, level_error = current.list_for(parsed.indent)
        if target is None:
            # indentation without a parent entry: the subtree cannot be
            # attached anywhere until the indentation returns to an ancestor
            assert level_error is not None
            level_error.children = tuple(fragment_errors)
            level_error.quarantined = True
            threshold = max(
                level for level in current.stack if level <= parsed.indent)
            quarantines.append(_Quarantine(threshold, level_error))
            continue
        children: list[PrefixInfo] = []
        for _rnge, low, high, _column, _end_column in parsed.ranges:
            target.append((len(low), low, high, parsed.props, children))
    while quarantines:
        quarantine = quarantines.pop()
        quarantine.error.end_line = quarantine.last_line
        quarantine.error.end_column = quarantine.last_end
        quarantine.error.children = tuple(quarantine.children)
        quarantine.error.quarantined = True
        for child in quarantine.error.children:
            child.quarantined = True
        current.errors.append(quarantine.error)
    db.parsing_errors = current.errors
    return db


def _get_resource_stream(name: str) -> IO[bytes]:
    """Return a readable file-like object for the resource."""
    try:  # pragma: no cover (Python 3.9 and newer)
        import importlib.resources
        return importlib.resources.files(__package__).joinpath(name).open('rb')
    except (ImportError, AttributeError):  # pragma: no cover (older Python versions)
        import pkg_resources  # type: ignore
        return pkg_resources.resource_stream(__name__, name)  # type: ignore[no-any-return]


def get(name: str) -> NumDB:
    """Open a database with the specified name to perform queries on."""
    if name not in _open_databases:
        import codecs
        reader = codecs.getreader('utf-8')
        with reader(_get_resource_stream(name + '.dat')) as fp:
            _open_databases[name] = read(fp)
    return _open_databases[name]
