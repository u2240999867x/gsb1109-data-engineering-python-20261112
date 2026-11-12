# test_numdb_diff.py - tests for the scripts/numdb_diff.py release tool
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

"""Validate the interval-based numdb diff engine against brute-force
enumeration on a small local dictionary that exercises range boundaries,
overlapping ranges, shared children and shortest-match shadowing."""

import importlib.util
import io
import os
import unittest

from stdnum import numdb


def _load_engine():
    path = os.path.join(
        os.path.dirname(__file__), os.pardir, 'scripts', 'numdb_diff.py')
    spec = importlib.util.spec_from_file_location('numdb_diff', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load_engine()

# a small hierarchical dictionary with overlapping ranges (00-09 vs 05-07),
# children shared between ranges of one line (60-69,70-79), a fully shadowed
# rule (000-004 ghost) and ranges touching the 0000/9999 space boundaries
TOY_SOURCE = '''\
# toy dictionary for boundary and shadowing validation
0-1 kind="low"
  00-09 reg="a"
    000-009 sub="deep"
  05-07 reg="b" extra="x"
  000-004 ghost="shadowed"
2 kind="mid"
  00-49 reg="c"
  400-499 reg="d"
  60-69,70-79 reg="shared"
    0-4 sub="s"
30-39 kind="high"
  0-4 reg="e"
9 kind="top"
  9 reg="f"
'''

# the same dictionary as it might arrive in a broken wheel: the 00-49 rule
# is lost (shrinks the valid range), the 05-07 properties are reworded, an
# extra observable rule is added for 8xxx and two shadowed rules are
# reworded/added without any observable effect
TOY_INSTALLED = '''\
0-1 kind="low"
  00-09 reg="a"
    000-009 sub="deep"
  05-07 reg="q" extra="x"
  000-004 ghost="reworded"
2 kind="mid"
  400-499 reg="d"
  60-69,70-79 reg="shared"
    0-4 sub="s"
30-39 kind="high"
  0-4 reg="e"
8 kind="new"
9 kind="top"
  9 reg="f"
  99 ghost2="added-shadowed"
'''

TOY_LENGTH = 4


def _numbers(length):
    return ['%0*d' % (length, i) for i in range(10 ** length)]


def _info(text, number):
    db = numdb.read(io.StringIO(text))
    return [(part, props) for part, props in db.info(number)]


def _flat(text):
    tree = engine.parse_dat(text)
    return engine.flatten(engine.compute_regions(tree, TOY_LENGTH), TOY_LENGTH)


class TestParser(unittest.TestCase):
    """The engine parser must reproduce stdnum.numdb.read exactly."""

    def test_toy_parser(self):
        tree = engine.parse_dat(TOY_SOURCE)
        self.assertEqual(
            numdb.read(io.StringIO(TOY_SOURCE)).prefixes,
            engine.to_numdb(tree))

    def test_release_parser(self):
        stdnum_dir = os.path.join(
            os.path.dirname(__file__), os.pardir, 'stdnum')
        for dirpath, _dirnames, filenames in os.walk(stdnum_dir):
            for filename in filenames:
                if not filename.endswith('.dat'):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding='utf-8') as fp:
                    text = fp.read()
                self.assertEqual(
                    numdb.read(io.StringIO(text)).prefixes,
                    engine.to_numdb(engine.parse_dat(text)),
                    f'parser mismatch on {path}')


class TestRegions(unittest.TestCase):
    """The interval engine must match brute-force enumeration."""

    def test_regions_cover_whole_space(self):
        flat = _flat(TOY_SOURCE)
        self.assertEqual('0000', flat[0][0])
        self.assertEqual('9999', flat[-1][1])
        for (_, hi, _, _), (lo2, _, _, _) in zip(flat, flat[1:]):
            self.assertEqual(engine._succ(hi, engine.ALPHABET), lo2)

    def test_labels_match_brute_force(self):
        for text in (TOY_SOURCE, TOY_INSTALLED):
            flat = _flat(text)
            pos = 0
            for lo, hi, label, _lines in flat:
                for number in _numbers(TOY_LENGTH)[
                        int(lo):int(hi) + 1]:
                    self.assertEqual(
                        _info(text, number),
                        engine.label_info(label, number),
                        f'label mismatch for {number}')
                pos += 1
            self.assertEqual(10 ** TOY_LENGTH, int(flat[-1][1]) + 1)

    def test_diff_matches_brute_force(self):
        src_flat = _flat(TOY_SOURCE)
        dst_flat = _flat(TOY_INSTALLED)
        intervals = engine.diff(src_flat, dst_flat)
        # per-number difference flags from the engine
        engine_differs = {}
        for lo, hi, _la, _lb in intervals:
            for i in range(int(lo), int(hi) + 1):
                engine_differs[i] = True
        # per-number difference flags from brute force
        brute_boundaries = []
        previous = False
        for i, number in enumerate(_numbers(TOY_LENGTH)):
            differs = _info(TOY_SOURCE, number) != _info(TOY_INSTALLED, number)
            self.assertEqual(
                differs, engine_differs.get(i, False),
                f'diff mismatch for {number}')
            if differs != previous:
                brute_boundaries.append(number)
            previous = differs
        # engine interval runs must match the brute-force boundaries (the
        # engine may subdivide a run into segments with different labels)
        engine_boundaries = []
        runs = []
        for lo, hi, _la, _lb in intervals:
            if runs and engine._succ(runs[-1][1], engine.ALPHABET) == lo:
                runs[-1][1] = hi
            else:
                runs.append([lo, hi])
        for lo, hi in runs:
            engine_boundaries.append(lo)
            if hi != '9999':
                engine_boundaries.append(engine._succ(hi, engine.ALPHABET))
        self.assertEqual(brute_boundaries, engine_boundaries)

    def test_diff_classification(self):
        intervals = engine.diff(_flat(TOY_SOURCE), _flat(TOY_INSTALLED))
        categories = {}
        for lo, hi, la, lb in intervals:
            categories.setdefault(engine.classify(la, lb), []).append((lo, hi))
        # 2000-2399 loses the second-level match entirely
        self.assertIn(('2000', '2399'), categories['shrunk'])
        # 2400-2499 falls back to the longer 400-499 rule
        self.assertIn(('2400', '2499'), categories['changed'])
        # 0050-0079 and 1050-1079 get reworded properties
        self.assertIn(('0050', '0079'), categories['changed'])
        self.assertIn(('1050', '1079'), categories['changed'])
        # the added 8 rule expands the valid range
        self.assertIn(('8000', '8999'), categories['expanded'])

    def test_shadowed_text_differences(self):
        src_flat = _flat(TOY_SOURCE)
        dst_flat = _flat(TOY_INSTALLED)
        src_prov = engine.provenance(src_flat)
        dst_prov = engine.provenance(dst_flat)
        src_records = engine.dat_line_records(TOY_SOURCE)
        dst_records = engine.dat_line_records(TOY_INSTALLED)
        # the ghost rule never contributes to any lookup
        ghost = [r for r in src_records if 'ghost=' in r[4]]
        self.assertEqual(1, len(ghost))
        self.assertNotIn(ghost[0][0], src_prov)
        # the added ghost2 rule never contributes either
        ghost2 = [r for r in dst_records if 'ghost2=' in r[4]]
        self.assertEqual(1, len(ghost2))
        self.assertNotIn(ghost2[0][0], dst_prov)
        # while observable rules do contribute
        reg_c = [r for r in src_records if 'reg="c"' in r[4]]
        self.assertIn(reg_c[0][0], src_prov)


if __name__ == '__main__':
    unittest.main()
