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

The database can be compiled into a lookup structure that avoids scanning
all ranges of a level on every query. The compiled database returns the
same info() and split() results and a single compilation is reused for
repeated queries:

>>> compiled = dbfile.compile()
>>> compiled is dbfile.compile()
True
>>> compiled.split('01006') == dbfile.split('01006')
True
>>> compiled.info('02006') == dbfile.info('02006')
True

The explain() method additionally exposes the original prefix entries that
contributed to a part, so compiled results can be traced back to the raw
database one entry at a time:

>>> part, properties, sources = compiled.explain('02006')[1]
>>> part, properties
('200', {'prop2': 'bar', 'prop3': 'baz'})
>>> len(sources)
2
>>> sources[0][1:3]
('100', '999')
>>> sources[1][1:3]
('200', '200')

The uncompiled database remains fully usable on its own through the raw
prefixes structure and the linear _find() lookup:

>>> NumDB._find('02006', dbfile.prefixes) == dbfile.info('02006')
True

"""

from __future__ import annotations

import re
from bisect import bisect_right


TYPE_CHECKING = False
if TYPE_CHECKING:  # pragma: no cover (only used when type checking)
    from collections.abc import Generator, Iterable
    from typing import IO, Any, Optional
    PrefixInfo = tuple[int, str, str, dict[str, str], list['PrefixInfo']]
    SourceInfo = tuple[PrefixInfo, ...]
    Match = tuple[int, dict[str, str], 'CompiledLevel', SourceInfo]
    LookupTable = tuple[
        tuple[str, ...], tuple[Optional[str], ...], tuple[dict[str, str], ...],
        tuple['CompiledLevel', ...], tuple[SourceInfo, ...]]
    CompileCache = dict[int, tuple['CompiledLevel', list[PrefixInfo]]]

_line_re = re.compile(
    r'^(?P<indent> *)'
    r'(?P<ranges>([^-,\s]+(-[^-,\s]+)?)(,[^-,\s]+(-[^-,\s]+)?)*)\s*'
    r'(?P<props>.*)$')
_prop_re = re.compile(
    r'(?P<prop>[0-9a-zA-Z-_]+)="(?P<value>[^"]*)"')

# this is a cache of open databases
_open_databases = {}

# the prefixes attribute of NumDB is structured as follows:
# prefixes = [
#   [ length, low, high, props, children ]
#   ...
# ]
# where children is a prefixes structure in its own right
# (there is no expected ordering within the list)


def _successor(value: str) -> str | None:
    """Return the smallest fixed-length string that sorts after value, or
    None if value is the largest possible string of its length.

    Strings compare by Unicode code point so incrementing the rightmost
    code point (carrying left at the maximum code point) yields the
    successor. Range values only use digits, uppercase letters and '$', but
    queries may contain other characters, so no smaller alphabet is
    assumed."""
    result = list(value)
    for index in range(len(result) - 1, -1, -1):
        code = ord(result[index])
        if code < 0x10FFFF:
            result[index] = chr(code + 1)
            return ''.join(result)
        result[index] = chr(0)
    return None


class NumDB():
    """Number database."""

    prefixes: list[PrefixInfo]
    _compiled: CompiledNumDB | None

    def __init__(self) -> None:
        """Construct an empty database."""
        self.prefixes = []
        self._compiled = None

    @staticmethod
    def _find(number: str, prefixes: list[PrefixInfo]) -> list[tuple[str, dict[str, str]]]:
        """Lookup the specified number in the list of prefixes, this will
        return basically what info() should return but works recursively.

        This is the uncompiled, linear lookup over the raw prefixes
        structure and is kept as the reference implementation for the
        compiled lookup."""
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

    def compile(self) -> CompiledNumDB:
        """Return a compiled copy of the database that is optimised for
        repeated queries. The compilation is done once and the result is
        cached on the database; the raw prefixes remain available for
        independent use."""
        if self._compiled is None:
            self._compiled = CompiledNumDB(self.prefixes)
        return self._compiled

    def info(self, number: str) -> list[tuple[str, dict[str, str]]]:
        """Split the provided number in components and associate properties
        with each component. This returns a tuple of tuples. Each tuple
        consists of a string (a part of the number) and a dict of properties.
        """
        return self.compile().info(number)

    def split(self, number: str) -> list[str]:
        """Split the provided number in components. This returns a tuple with
        the number of components identified."""
        return [part for part, props in self.info(number)]


class CompiledLevel():
    """A compiled version of a single prefixes list.

    The original entries are grouped by prefix length. For each length the
    ranges are split into disjoint half-open segments with a constant set of
    covering ranges; the segments hold the combined properties (with later
    file entries overriding earlier ones), the merged children and the
    original prefix entries that explain the segment."""

    lengths: tuple[int, ...]
    tables: dict[int, LookupTable]

    def __init__(self) -> None:
        self.lengths = ()
        self.tables = {}


# shared level for empty children lists; the object is immutable so a single
# instance avoids rebuilding an empty level for every leaf segment
_empty_level = CompiledLevel()


class CompiledNumDB():
    """Number database compiled for fast repeated lookups.

    Unlike the uncompiled NumDB._find() this does not try every range in a
    level. Instead each level's ranges are grouped by prefix length and
    split into disjoint segments that are located with a binary search.
    Overlapping ranges of the same length keep their original file-order
    semantics: properties are applied in order and children are merged.

    This is not a longest-prefix match: just like NumDB._find() the shortest
    matching prefix length wins and any remaining number is looked up in the
    merged children of that match."""

    _root: CompiledLevel

    def __init__(self, prefixes: list[PrefixInfo]) -> None:
        """Construct a compiled database from the raw prefixes structure."""
        cache: CompileCache = {}
        self._root = self._compile(prefixes, cache)

    @staticmethod
    def _compile(
        prefixes: list[PrefixInfo],
        cache: CompileCache,
    ) -> CompiledLevel:
        """Build a CompiledLevel for the specified prefixes list."""
        if not prefixes:
            return _empty_level
        # the same children list may be attached to multiple entries so
        # compile it only once; the cache value keeps the list alive
        cached = cache.get(id(prefixes))
        if cached is not None:
            return cached[0]
        level = CompiledLevel()
        cache[id(prefixes)] = (level, prefixes)
        # group entries by prefix length
        grouped: dict[int, list[PrefixInfo]] = {}
        for entry in prefixes:
            grouped.setdefault(entry[0], []).append(entry)
        level.lengths = tuple(sorted(grouped))
        for length, entries in grouped.items():
            # turn inclusive ranges into half-open [low, successor(high))
            # intervals and record where the set of active ranges changes
            events: list[tuple[str, int, int]] = []
            for position, entry in enumerate(entries):
                events.append((entry[1], 1, position))
                end = _successor(entry[2])
                if end is not None:
                    events.append((end, 0, position))
            # starts are applied before ends at the same coordinate
            events.sort(key=lambda event: (event[0], -event[1]))
            starts: list[str] = []
            ends: list[str | None] = []
            properties: list[dict[str, str]] = []
            child_levels: list[CompiledLevel] = []
            sources: list[SourceInfo] = []
            active: dict[int, PrefixInfo] = {}
            index = 0
            while index < len(events):
                coord = events[index][0]
                # apply all ranges starting at this coordinate
                while index < len(events) and events[index][0] == coord and events[index][1] == 1:
                    position = events[index][2]
                    active[position] = entries[position]
                    index += 1
                # remove all ranges ending at this coordinate
                while index < len(events) and events[index][0] == coord:
                    active.pop(events[index][2], None)
                    index += 1
                next_coord = events[index][0] if index < len(events) else None
                if active:
                    # the active set is constant over [coord, next_coord);
                    # apply the covering ranges in original file order
                    ordered = tuple(active[position] for position in sorted(active))
                    if len(ordered) == 1:
                        # reuse the entry's own properties and children so
                        # single-source segments add no extra copies
                        props = ordered[0][3]
                        children = ordered[0][4]
                    else:
                        props = {}
                        for entry in ordered:
                            props.update(entry[3])
                        children = CompiledNumDB._merge_children(ordered)
                    starts.append(coord)
                    ends.append(next_coord)
                    properties.append(props)
                    child_levels.append(
                        CompiledNumDB._compile(children, cache))
                    sources.append(ordered)
            level.tables[length] = (
                tuple(starts), tuple(ends), tuple(properties),
                tuple(child_levels), tuple(sources))
        return level

    @staticmethod
    def _merge_children(ordered: SourceInfo) -> list[PrefixInfo]:
        """Concatenate the children of the covering ranges in file order,
        mirroring the extend() calls in the linear lookup."""
        children: list[PrefixInfo] = []
        for entry in ordered:
            children.extend(entry[4])
        return children

    @staticmethod
    def _match(
        level: CompiledLevel,
        part: str,
    ) -> Match | None:
        """Find the shortest matching segment in the compiled level."""
        for length in level.lengths:
            if length > len(part):
                # all remaining lengths are even longer
                return None
            starts, ends, props, child_levels, sources = level.tables[length]
            prefix = part[:length]
            position = bisect_right(starts, prefix) - 1
            if position >= 0:
                end = ends[position]
                if end is None or prefix < end:
                    return length, props[position], child_levels[position], sources[position]
        return None

    def info(self, number: str) -> list[tuple[str, dict[str, str]]]:
        """Split the provided number in components and associate properties
        with each component. This returns the same results as
        NumDB.info()."""
        result: list[tuple[str, dict[str, str]]] = []
        remainder = number
        level = self._root
        while remainder:
            match = self._match(level, remainder)
            if match is None:
                # no prefix matches: the leftover is the final empty part
                result.append((remainder, {}))
                break
            length, props, child_level, _sources = match
            result.append((remainder[:length], dict(props)))
            remainder = remainder[length:]
            level = child_level
        return result

    def explain(
        self,
        number: str,
    ) -> list[tuple[str, dict[str, str], SourceInfo]]:
        """Like info() but each part is accompanied by the original prefix
        entries (in file order) that contributed its properties and child
        levels. This makes it possible to trace compiled results back to the
        raw database entries."""
        result: list[tuple[str, dict[str, str], SourceInfo]] = []
        remainder = number
        level = self._root
        while remainder:
            match = self._match(level, remainder)
            if match is None:
                result.append((remainder, {}, ()))
                break
            length, props, child_level, sources = match
            result.append((remainder[:length], dict(props), sources))
            remainder = remainder[length:]
            level = child_level
        return result

    def split(self, number: str) -> list[str]:
        """Split the provided number in components. This returns the same
        results as NumDB.split()."""
        return [part for part, props in self.info(number)]


def _parse(
    fp: Iterable[str],
) -> Generator[tuple[int, int, str, str, dict[str, str], list[PrefixInfo]]]:
    """Read lines of text from the file pointer and generate indent, length,
    low, high, properties tuples."""
    for line in fp:
        # ignore comments
        if line[0] == '#' or line.strip() == '':
            continue  # pragma: no cover (optimisation takes it out)
        # any other line should parse
        match = _line_re.search(line)
        assert match is not None
        indent = len(match.group('indent'))
        ranges = match.group('ranges')
        props = dict(_prop_re.findall(match.group('props')))
        children: list[PrefixInfo] = []
        for rnge in ranges.split(','):
            if '-' in rnge:
                low, high = rnge.split('-')
            else:
                low, high = rnge, rnge
            yield indent, len(low), low, high, props, children


def read(fp: Iterable[str]) -> NumDB:
    """Return a new database with the data read from the specified file."""
    last_indent = 0
    db = NumDB()
    stack = {0: db.prefixes}
    for indent, length, low, high, props, children in _parse(fp):
        if indent > last_indent:
            # set our stack location to the last parent entry
            stack[indent] = stack[last_indent][-1][4]
        stack[indent].append((length, low, high, props, children))
        last_indent = indent
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
