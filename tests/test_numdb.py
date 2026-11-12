# test_numdb.py - tests for the compiled number database lookups
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

"""Tests for the stdnum.numdb compiled lookup structures."""

import glob
import io
import os
import random
import unittest

from stdnum.numdb import CompiledNumDB, NumDB, _successor, read


def read_database(text: str) -> NumDB:
    """Parse a database from the specified text."""
    return read(io.StringIO(text))


class TestSuccessor(unittest.TestCase):
    """Test the fixed-length string successor helper."""

    def test_successor(self) -> None:
        """Test simple increments and carries."""
        self.assertEqual(_successor('000'), '001')
        self.assertEqual(_successor('009'), '00:')
        self.assertEqual(_successor('AZ'), 'A[')
        self.assertEqual(_successor('A$'), 'A%')
        self.assertEqual(
            _successor('A' + chr(0x10FFFF)), 'B' + chr(0))

    def test_successor_overflow(self) -> None:
        """Test that the maximum string has no successor."""
        self.assertIsNone(_successor(chr(0x10FFFF)))
        self.assertIsNone(_successor(chr(0x10FFFF) * 2))


class TestCompiledEquivalence(unittest.TestCase):
    """Compare compiled lookups against the linear reference implementation."""

    def test_reference_database(self) -> None:
        """The test database should return identical results."""
        path = os.path.join(os.path.dirname(__file__), 'numdb-test.dat')
        with open(path, 'r') as fp:
            db = read(fp)
        compiled = db.compile()
        # compilation is cached and the raw structure is untouched
        self.assertIs(compiled, db.compile())
        prefixes = db.prefixes
        probes = (
            '', '0', '01006', '02006', '03456', '09999', '10000', '1200333',
            '20000', '30000', '33300', '34500', '59999', '60000', '633322',
            '70000', '89999', '90000', '902006', '909856', '9889', '99000',
            '9999999999', 'abc', '00', '6', '90',
        )
        for number in probes:
            expected = NumDB._find(number, prefixes)
            self.assertEqual(expected, compiled.info(number), number)
            self.assertEqual(expected, db.info(number), number)
            self.assertEqual(
                [part for part, props in expected],
                compiled.split(number), number)
        self.assertEqual([], compiled.info(''))

    def test_shipped_databases(self) -> None:
        """All shipped databases should compile and query equivalently."""
        base = os.path.join(os.path.dirname(__file__), '..', 'stdnum')
        rng = random.Random(20261112)
        characters = '$0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcxyz- '
        for path in sorted(glob.glob(os.path.join(base, '**', '*.dat'),
                                     recursive=True)):
            with open(path, 'r') as fp:
                db = read(fp)
            compiled = db.compile()
            # deterministic probes: random strings plus a few boundary values
            probes = [
                ''.join(rng.choice(characters)
                        for _ in range(rng.randint(0, 12)))
                for _i in range(120)]
            probes.append('')
            for length, low, high, props, children in db.prefixes[:60]:
                probes.extend((low, high))
                successor = _successor(high)
                if successor is not None:
                    probes.append(successor)
            for number in probes:
                self.assertEqual(
                    NumDB._find(number, db.prefixes),
                    compiled.info(number), (path, number))


class TestCompiledSemantics(unittest.TestCase):
    """Targeted tests for semantics that are easy to get wrong."""

    def test_same_length_override(self) -> None:
        """Later same-length ranges override earlier property values."""
        db = read_database('46 campus="Internet"\n46 campus="Philadelphia"\n')
        self.assertEqual(
            [('46', {'campus': 'Philadelphia'})], db.info('46'))
        explained = db.compile().explain('46')
        self.assertEqual(2, len(explained[0][2]))
        # both original entries explain the merged result, in file order
        self.assertEqual(
            ['Internet', 'Philadelphia'],
            [entry[3]['campus'] for entry in explained[0][2]])

    def test_overlapping_properties_merge(self) -> None:
        """Overlapping ranges merge properties without leaking each other."""
        db = read_database(
            '10-19 a="one"\n'
            '15-25 b="two"\n'
            '20-29 c="three"\n')
        self.assertEqual([('10', {'a': 'one'})], db.info('10'))
        self.assertEqual(
            [('15', {'a': 'one', 'b': 'two'})], db.info('15'))
        self.assertEqual([('19', {'a': 'one', 'b': 'two'})], db.info('19'))
        self.assertEqual([('20', {'b': 'two', 'c': 'three'})], db.info('20'))
        self.assertEqual([('25', {'b': 'two', 'c': 'three'})], db.info('25'))
        self.assertEqual([('29', {'c': 'three'})], db.info('29'))
        self.assertEqual([('30', {})], db.info('30'))

    def test_shortest_match_wins(self) -> None:
        """A shorter match resets properties and ignores longer matches."""
        db = read_database(
            '500-599 cc="long"\n'
            '5 cc="short"\n'
            '600-699 cc="other"\n')
        # the length-1 range resets the earlier length-3 match
        self.assertEqual(
            [('5', {'cc': 'short'}), ('50', {})], db.info('550'))
        # the longer range matches where the short one does not cover
        self.assertEqual(
            [('650', {'cc': 'other'})], db.info('650'))
        # a remainder shorter than every prefix length is a single part
        self.assertEqual(
            [('5', {'cc': 'short'}), ('0', {})], db.info('50'))
        self.assertEqual([('700', {})], db.info('700'))

    def test_characters_outside_data_alphabet(self) -> None:
        """Ranges ending at Z must not capture lower-case query characters."""
        db = read_database(
            'A-Z a="upper"\n'
            'a a="lower"\n')
        self.assertEqual([('A', {'a': 'upper'})], db.info('A'))
        self.assertEqual(
            [('Z', {'a': 'upper'}), ('z', {})], db.info('Zz'))
        self.assertEqual([('a', {'a': 'lower'})], db.info('a'))
        self.assertEqual(
            [('a', {'a': 'lower'}), ('b', {})], db.info('ab'))

    def test_max_code_point_range(self) -> None:
        """A range ending at the maximum code point has an open end."""
        maximum = chr(0x10FFFF)
        db = read_database('A-' + maximum + ' a="wide"\n')
        self.assertEqual([('A', {'a': 'wide'})], db.info('A'))
        self.assertEqual([(maximum, {'a': 'wide'})], db.info(maximum))
        self.assertEqual([('@', {})], db.info('@'))

    def test_hierarchy_and_merged_children(self) -> None:
        """Children of overlapping ranges are merged for the next part."""
        db = read_database(
            '10-19 a="one"\n'
            ' 0-4 x="low"\n'
            '15-25 b="two"\n'
            ' 5-9 x="high"\n')
        # inside the overlap both child lists are consulted
        self.assertEqual(
            [('15', {'a': 'one', 'b': 'two'}), ('5', {'x': 'high'})],
            db.info('155'))
        self.assertEqual(
            [('15', {'a': 'one', 'b': 'two'}), ('0', {'x': 'low'})],
            db.info('150'))
        # outside the overlap only the first range's children apply
        self.assertEqual(
            [('10', {'a': 'one'}), ('4', {'x': 'low'})],
            db.info('104'))
        self.assertEqual(
            [('10', {'a': 'one'}), ('9', {})],
            db.info('109'))

    def test_empty_database(self) -> None:
        """An empty database returns the full number as one empty part."""
        db = NumDB()
        self.assertIs(db.compile(), db.compile())
        self.assertEqual([], db.info(''))
        self.assertEqual([('123', {})], db.info('123'))
        self.assertEqual(['123'], db.split('123'))


class TestExplain(unittest.TestCase):
    """Test tracing compiled results back to raw entries."""

    def test_explain_sources(self) -> None:
        """Explain cites the raw entries and reproduces merged properties."""
        db = read_database(
            '10-19 a="one"\n'
            '15-25 b="two"\n')
        part, properties, sources = db.compile().explain('17')[0]
        self.assertEqual('17', part)
        self.assertEqual({'a': 'one', 'b': 'two'}, properties)
        self.assertEqual(
            [(2, '10', '19'), (2, '15', '25')],
            [(entry[0], entry[1], entry[2]) for entry in sources])
        rebuilt = {}
        for entry in sources:
            rebuilt.update(entry[3])
        self.assertEqual(properties, rebuilt)
        # an unmatched part has no sources
        unmatched = db.compile().explain('99')
        self.assertEqual(('99', {}, ()), unmatched[0])

    def test_raw_database_remains_independent(self) -> None:
        """The raw prefixes stay usable without the compiled structure."""
        db = read_database('10-19 a="one"\n')
        raw = list(db.prefixes)
        compiled = CompiledNumDB(db.prefixes)
        self.assertEqual(raw, db.prefixes)
        self.assertEqual(
            NumDB._find('15', db.prefixes), compiled.info('15'))
