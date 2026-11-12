# test_fragments.py - unit tests for the stdnum.fragments module
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
# License along with this library; if not, see <https://www.gnu.org/licenses/

"""Tests for fragment reassembly of Mod 97, 10 numbers."""

import unittest

from stdnum import fragments
from stdnum.exceptions import *
from stdnum.iso7064 import mod_97_10


class TestDigest(unittest.TestCase):
    """Test the per-fragment digest helpers."""

    def test_fragment_digest(self) -> None:
        # numeric fragments keep their length and remainder
        self.assertEqual(fragments.fragment_digest('99991234'), (8, 45))
        # leading zeroes keep their position instead of collapsing
        self.assertEqual(fragments.fragment_digest('0042'), (4, 42))
        # letters are base36 digits expanding to two base10 digits
        self.assertEqual(fragments.fragment_digest('AB'), (4, 41))
        self.assertEqual(fragments.fragment_digest('0a'), (3, 10))

    def test_fragment_digest_invalid(self) -> None:
        self.assertRaises(InvalidFormat, fragments.fragment_digest, '12-4!')
        self.assertRaises(InvalidFormat, fragments.fragment_digest, '')

    def test_fingerprint(self) -> None:
        self.assertEqual(
            fragments.fingerprint('AB-0042'),
            fragments.fingerprint('ab 0042'))
        self.assertNotEqual(
            fragments.fingerprint('AB0042'),
            fragments.fingerprint('AB0043'))

    def test_fragment_repr(self) -> None:
        fragment = fragments.Fragment(0, 'AB', total_length=4)
        self.assertEqual(
            repr(fragment),
            'Fragment(position=0, end=4, version=1, remainder=41, '
            "fingerprint=%r)" % fragment.fingerprint)

    def test_split_fragments(self) -> None:
        number = '9999123456789012141490'
        self.assertEqual(
            fragments.split_fragments(number, 8),
            [(0, '99991234'), (8, '56789012'), (16, '141490')])
        self.assertRaises(
            InvalidChecksum, fragments.split_fragments,
            '9999123456789012141499', 8)
        self.assertRaises(
            InvalidFormat, fragments.split_fragments,
            '9999123456789012141490', 0)

    def test_split_fragments_with_letters(self) -> None:
        body = 'AB0042'
        number = body + mod_97_10.calc_check_digits(body)
        self.assertTrue(mod_97_10.is_valid(number))
        # the letter expands to two digits so it stays inside one fragment
        pieces = fragments.split_fragments(number, 4)
        self.assertEqual(pieces, [(0, 'AB'), (4, '0042'), (8, '29')])
        assembly = fragments.FragmentAssembly(
            fragments.fragment_digest(number)[0])
        for position, content in pieces:
            assembly.deliver(position, content)
        self.assertEqual(assembly.state, fragments.COMPLETE)
        self.assertTrue(assembly.is_valid())


class TestAssembly(unittest.TestCase):
    """Test delivery, redelivery and the three states."""

    def setUp(self) -> None:
        self.number = '9999123456789012141490'
        self.parts = fragments.split_fragments(self.number, 8)
        self.assembly = fragments.FragmentAssembly(len(self.number))

    def deliver_all(self) -> None:
        for position, content in self.parts:
            self.assembly.deliver(position, content)

    def test_out_of_order_delivery(self) -> None:
        result = self.assembly.deliver(self.parts[2][0], self.parts[2][1])
        self.assertEqual(result.state, fragments.INCOMPLETE)
        self.assertEqual(self.assembly.holes, [(0, 16)])
        self.assembly.deliver(self.parts[0][0], self.parts[0][1])
        self.assertEqual(self.assembly.holes, [(8, 16)])
        result = self.assembly.deliver(self.parts[1][0], self.parts[1][1])
        self.assertEqual(result.state, fragments.COMPLETE)
        self.assertEqual(self.assembly.holes, [])
        self.assertTrue(self.assembly.is_valid())
        self.assertEqual(self.assembly.checksum(), 1)
        self.assertEqual(self.assembly.validate(), fragments.COMPLETE)

    def test_redelivery_skipped_by_fingerprint(self) -> None:
        self.deliver_all()
        # omitting the fingerprint regenerates the same content digest
        result = self.assembly.deliver(self.parts[1][0], self.parts[1][1])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.merged, 0)
        # a custom fingerprint only takes effect on the first delivery; a
        # later redelivery carrying that same fingerprint is still skipped
        assembly = fragments.FragmentAssembly(len(self.number))
        position, content = self.parts[0]
        assembly.deliver(position, content, fingerprint='custom')
        result = assembly.deliver(position, content, fingerprint='custom')
        self.assertEqual(result.skipped, 1)

    def test_same_version_contradiction(self) -> None:
        self.deliver_all()
        with self.assertRaises(FragmentConflict) as context:
            self.assembly.deliver(self.parts[1][0], '56789099')
        self.assertIn('position 8', str(context.exception))
        self.assertEqual(self.assembly.state, fragments.CONFLICTING)
        self.assertEqual(self.assembly.conflicts, [(8, 16)])
        self.assertFalse(self.assembly.is_valid())
        self.assertRaises(FragmentConflict, self.assembly.validate)

    def test_repeated_conflict_recorded_once(self) -> None:
        self.deliver_all()
        for _ in range(2):
            self.assertRaises(
                FragmentConflict, self.assembly.deliver,
                self.parts[1][0], '56789099')
        # the same conflicting interval is stored only once
        self.assertEqual(self.assembly.conflicts, [(8, 16)])

    def test_partial_overlap_reports_conflict_position(self) -> None:
        self.assembly.deliver(0, '99991234')
        with self.assertRaises(FragmentConflict) as context:
            self.assembly.deliver(4, '12345678')
        self.assertIn('position 4', str(context.exception))
        self.assertEqual(self.assembly.state, fragments.CONFLICTING)
        # only the actually overlapping positions are recorded
        self.assertEqual(self.assembly.conflicts, [(4, 8)])

    def test_holes_state(self) -> None:
        self.assembly.deliver(0, '99991234')
        self.assembly.deliver(16, '141490')
        self.assertEqual(self.assembly.state, fragments.INCOMPLETE)
        self.assertEqual(self.assembly.holes, [(8, 16)])
        self.assertRaises(InvalidLength, self.assembly.validate)

    def test_complete_but_bad_checksum(self) -> None:
        # cover every position with syntactically valid, but inconsistent,
        # content so coverage is complete while the checksum fails
        assembly = fragments.FragmentAssembly(6)
        assembly.deliver(0, '1234')
        assembly.deliver(4, '56')
        self.assertEqual(assembly.state, fragments.COMPLETE)
        self.assertFalse(assembly.is_valid())
        self.assertRaises(InvalidChecksum, assembly.validate)

    def test_out_of_bounds_fragment(self) -> None:
        self.assertRaises(
            InvalidLength, self.assembly.deliver, 20, '123456')
        self.assertRaises(
            InvalidFormat, self.assembly.deliver, -1, '1234')

    def test_invalid_total_length(self) -> None:
        self.assertRaises(InvalidLength, fragments.FragmentAssembly, 2)


class TestReplacement(unittest.TestCase):
    """Test versioned replacement and its incremental accounting."""

    def setUp(self) -> None:
        self.number = '9999123456789012141490'
        self.parts = fragments.split_fragments(self.number, 8)
        self.assembly = fragments.FragmentAssembly(len(self.number))
        for position, content in self.parts:
            self.assembly.deliver(position, content)

    def test_replace_recomputes_single_digest(self) -> None:
        position, old = self.parts[1]
        new = '56789099'
        result = self.assembly.replace(
            position, new, version=2, fingerprint='v2')
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.recomputed, 1)
        # the running remainder only differs by the swapped absolute term
        old_fragment = fragments.Fragment(position, old, len(self.number))
        new_fragment = fragments.Fragment(
            position, new, len(self.number), version=2)
        expected = (
            self.assembly.checksum() - new_fragment.term +
            old_fragment.term) % 97
        self.assertEqual(expected, 1)

    def test_replace_without_prior_fragment(self) -> None:
        self.assertRaises(
            InvalidComponent, self.assembly.replace, 4, '1234', version=2)

    def test_replace_must_match_boundaries(self) -> None:
        # a longer fragment changes the end boundary and is rejected
        with self.assertRaises(FragmentConflict) as context:
            self.assembly.replace(8, '567890123', version=2)
        self.assertIn('position 8', str(context.exception))
        self.assertEqual(self.assembly.state, fragments.CONFLICTING)

    def test_replace_length_change_is_boundary_mismatch(self) -> None:
        assembly = fragments.FragmentAssembly(8)
        assembly.deliver(0, '1234')
        assembly.deliver(4, '5678')
        # replacing the first interval with a longer one changes its end
        # boundary and would spill into the neighbouring fragment
        with self.assertRaises(FragmentConflict) as context:
            assembly.replace(0, '12345', version=2)
        self.assertIn('position 0', str(context.exception))
        self.assertEqual(assembly.conflicts, [(0, 4)])

    def test_replace_rejects_older_version(self) -> None:
        position, content = self.parts[1]
        self.assembly.replace(position, content, version=3)
        self.assertRaises(
            InvalidComponent, self.assembly.replace,
            position, content, version=2)

    def test_replace_same_version_contradiction(self) -> None:
        position, _ = self.parts[1]
        with self.assertRaises(FragmentConflict):
            self.assembly.replace(position, '56789099', version=1)

    def test_replace_same_content_is_skip(self) -> None:
        position, content = self.parts[1]
        result = self.assembly.replace(position, content, version=1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.recomputed, 0)

    def test_replace_clears_recorded_conflict(self) -> None:
        position, _ = self.parts[1]
        self.assertRaises(
            FragmentConflict, self.assembly.deliver, position, '56789099')
        self.assertEqual(self.assembly.state, fragments.CONFLICTING)
        # a same-boundary new version is the accepted correction
        result = self.assembly.replace(
            position, '56789012', version=2)
        self.assertEqual(result.recomputed, 1)
        self.assertEqual(self.assembly.state, fragments.COMPLETE)
        self.assertEqual(self.assembly.conflicts, [])

    def test_deliver_new_version_requires_replace(self) -> None:
        position, content = self.parts[0]
        self.assertRaises(
            InvalidComponent, self.assembly.deliver,
            position, content, version=2)


class TestMergeResult(unittest.TestCase):
    """Test the merge counter value object."""

    def test_equality_and_repr(self) -> None:
        result = fragments.MergeResult(merged=1, state=fragments.COMPLETE)
        same = fragments.MergeResult(merged=1, state=fragments.COMPLETE)
        other = fragments.MergeResult(skipped=1, state=fragments.COMPLETE)
        self.assertEqual(result, same)
        self.assertNotEqual(result, other)
        self.assertEqual(
            repr(result),
            "MergeResult(merged=1, skipped=0, recomputed=0, "
            "state='complete')")
        self.assertIs(
            result.__eq__(('not', 'a result')), NotImplemented)


if __name__ == '__main__':
    unittest.main()
