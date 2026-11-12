# test_candidates.py - tests for the stdnum.candidates module
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

"""Tests for the budgeted candidate search in stdnum.candidates."""

import itertools
import random
import unittest

from stdnum import candidates, ean, luhn
from stdnum.exceptions import InvalidFormat, ValidationError
from stdnum.util import clean


class ExactModule:
    """Validation module that accepts an explicit set of compact values."""

    def __init__(self, valid, strip=' -'):
        self.valid = set(valid)
        self.strip = strip

    def compact(self, number):
        return clean(number, self.strip).strip()

    def validate(self, number):
        number = self.compact(number)
        if number not in self.valid:
            raise InvalidFormat()
        return number


class LuhnModule:
    """Validation module for digit strings with a Luhn check digit."""

    def compact(self, number):
        return clean(number, ' -').strip()

    def validate(self, number):
        number = self.compact(number)
        if not number or not number.isdigit():
            raise InvalidFormat()
        return luhn.validate(number)


def brute_force(source, alphabet, separators, budget, costs, module):
    """Independent reference implementation using exhaustive string
    enumeration and a dynamic-programming alignment routine."""
    insert_cost, delete_cost, substitute_cost, swap_cost = costs

    def delete_price(char):
        return 0 if char in separators else delete_cost

    def align(source, target):
        infinity = 10 ** 9
        n, m = len(source), len(target)
        dp = [[infinity] * (m + 1) for _ in range(n + 1)]
        dp[0][0] = 0
        for i in range(n + 1):
            for j in range(m + 1):
                if i == 0 and j == 0:
                    continue
                options = []
                if i:
                    options.append(
                        dp[i - 1][j] + delete_price(source[i - 1]))
                if j and target[j - 1] in alphabet:
                    options.append(dp[i][j - 1] + insert_cost)
                if i and j and source[i - 1] not in separators:
                    price = (0 if source[i - 1] == target[j - 1]
                             else substitute_cost)
                    options.append(dp[i - 1][j - 1] + price)
                if (i >= 2 and j >= 2
                        and source[i - 2] not in separators
                        and source[i - 1] not in separators
                        and source[i - 2] == target[j - 1]
                        and source[i - 1] == target[j - 2]
                        and source[i - 2] != source[i - 1]):
                    options.append(dp[i - 2][j - 2] + swap_cost)
                dp[i][j] = min(options)
        # trace every minimum-cost alignment and union changed positions
        positions = set()
        stack = [(n, m)]
        seen = set()
        while stack:
            i, j = stack.pop()
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if i and (dp[i - 1][j] + delete_price(source[i - 1])
                      == dp[i][j]):
                stack.append((i - 1, j))
            if (j and target[j - 1] in alphabet
                    and dp[i][j - 1] + insert_cost == dp[i][j]):
                stack.append((i, j - 1))
            if i and j and source[i - 1] not in separators:
                price = (0 if source[i - 1] == target[j - 1]
                         else substitute_cost)
                if dp[i - 1][j - 1] + price == dp[i][j]:
                    if price:
                        positions.add(i - 1)
                    stack.append((i - 1, j - 1))
            if (i >= 2 and j >= 2
                    and source[i - 2] not in separators
                    and source[i - 1] not in separators
                    and source[i - 2] == target[j - 1]
                    and source[i - 1] == target[j - 2]
                    and source[i - 2] != source[i - 1]
                    and dp[i - 2][j - 2] + swap_cost == dp[i][j]):
                positions.add(i - 2)
                positions.add(i - 1)
                stack.append((i - 2, j - 2))
        return dp[n][m], frozenset(positions)

    groups = {}
    max_len = len(source) + budget
    for length in range(max_len + 1):
        for chars in itertools.product(alphabet, repeat=length):
            raw = ''.join(chars)
            cost, _ = align(source, raw)
            if cost > budget:
                continue
            compacted = module.compact(raw)
            group = groups.get(compacted)
            if group is None or cost < group[0]:
                groups[compacted] = (cost, [raw])
            elif cost == group[0]:
                group[1].append(raw)
    expected = {}
    for compacted, (cost, raws) in groups.items():
        try:
            module.validate(compacted)
        except ValidationError:
            continue
        positions = set()
        for raw in raws:
            _, raw_positions = align(source, raw)
            positions.update(raw_positions)
        expected[compacted] = (cost, tuple(sorted(positions)))
    return expected


class FindCandidatesTest(unittest.TestCase):
    """Tests for find_candidates()."""

    def test_substitution_with_ean(self):
        """Every single-position repair is returned, tied at the same cost."""
        result = candidates.find_candidates(
            ean, '73513538', '0123456789', k=5, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10)
        # k is smaller than the number of cost-1 ties: all are listed
        self.assertEqual(
            result,
            [
                candidates.Candidate('03513538', 1, (0,)),
                candidates.Candidate('72513538', 1, (1,)),
                candidates.Candidate('73503538', 1, (3,)),
                candidates.Candidate('73513438', 1, (5,)),
                candidates.Candidate('73513537', 1, (7,)),
                candidates.Candidate('73513568', 1, (6,)),
                candidates.Candidate('73516538', 1, (4,)),
                candidates.Candidate('73813538', 1, (2,)),
            ])

    def test_separators_are_cleaned_for_free(self):
        """Separators do not add cost and positions point at the raw value."""
        without = candidates.find_candidates(
            ean, '73513538', '0123456789', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10, separators=' -')
        with_separators = candidates.find_candidates(
            ean, '73-51 3538', '0123456789', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10, separators=' -')
        self.assertEqual(
            [(item.number, item.cost) for item in without],
            [(item.number, item.cost) for item in with_separators])
        repaired = next(
            item for item in with_separators
            if item.number == '73513537')
        self.assertEqual(repaired.positions, (9,))

    def test_repeated_characters_and_separators(self):
        """Repeated characters and mixed separators keep raw positions."""
        module = ExactModule(('112',))
        result = candidates.find_candidates(
            module, '111', '012', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10)
        self.assertEqual(
            result,
            [candidates.Candidate('112', 1, (2,))])
        # the same derivation with separators mixed into the number
        result = candidates.find_candidates(
            module, '1-1 1', '012', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10, separators=' -')
        self.assertEqual(
            result,
            [candidates.Candidate('112', 1, (4,))])

    def test_positions_union_of_tied_alignments(self):
        """All minimum-cost alignments contribute their changed positions."""
        # substituting the first character (cost 2) ties with deleting it and
        # inserting the replacement (cost 1 + 1); only the substitution marks
        # an original position, but both paths keep the candidate at cost 2
        module = ExactModule(('21',))
        kwargs = dict(
            alphabet='012', budget=2, insert_cost=1, delete_cost=1,
            substitute_cost=2, swap_cost=10)
        result = candidates.find_candidates(module, '11', k=10, **kwargs)
        self.assertEqual(
            result,
            [candidates.Candidate('21', 2, (0,))])
        # separators shift the reported position but not the cost
        result = candidates.find_candidates(
            module, '1-1', k=10, separators='-', **kwargs)
        self.assertEqual(
            result,
            [candidates.Candidate('21', 2, (0,))])

    def test_adjacent_swap(self):
        """An adjacent swap is a single, separately priced edit."""
        module = ExactModule(('21',))
        result = candidates.find_candidates(
            module, '12', '012', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=10,
            swap_cost=1)
        self.assertEqual(
            result,
            [candidates.Candidate('21', 1, (0, 1))])

    def test_ties_at_k_are_all_listed(self):
        """All candidates sharing the k-th cost are returned."""
        module = ExactModule(('00', '01', '10'))
        kwargs = dict(
            alphabet='01', budget=1, insert_cost=10, delete_cost=10,
            substitute_cost=1, swap_cost=10)
        first = candidates.find_candidates(module, '00', k=1, **kwargs)
        self.assertEqual(
            [(item.number, item.cost, item.positions) for item in first],
            [('00', 0, ())])
        tied = candidates.find_candidates(module, '00', k=2, **kwargs)
        self.assertEqual(
            [(item.number, item.cost, item.positions) for item in tied],
            [('00', 0, ()), ('01', 1, (1,)), ('10', 1, (0,))])

    def test_failed_validation_never_occupies_a_slot(self):
        """Only values accepted by validate() are returned."""
        module = ExactModule(('10',))
        result = candidates.find_candidates(
            module, '00', '01', k=1, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10)
        self.assertEqual(
            result,
            [candidates.Candidate('10', 1, (0,))])

    def test_zero_budget(self):
        """Without a budget only the compacted number itself can qualify."""
        valid = candidates.find_candidates(
            ean, '73513537', '0123456789', k=10, budget=0)
        self.assertEqual(
            valid,
            [candidates.Candidate('73513537', 0, ())])
        invalid = candidates.find_candidates(
            ean, '73513538', '0123456789', k=10, budget=0)
        self.assertEqual(invalid, [])

    def test_identical_result_is_reported_once(self):
        """Different separator cleanings merge into one candidate."""
        module = ExactModule(('12',))
        result = candidates.find_candidates(
            module, '1-2', '012', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=10,
            swap_cost=10, separators='-')
        self.assertEqual(
            result,
            [candidates.Candidate('12', 0, ())])

    def test_swap_cannot_cross_a_separator(self):
        """A swap only applies to adjacent non-separator characters."""
        module = ExactModule(('21',))
        # the digits are adjacent once the separator is cleaned, but swapping
        # them in the original would move the separator's neighbours
        result = candidates.find_candidates(
            module, '1-2', '012', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=10,
            swap_cost=1, separators='-')
        self.assertEqual(result, [])

    def test_separator_only_input(self):
        """Cleaning all characters away leaves nothing to validate."""
        module = ExactModule(('1',))
        result = candidates.find_candidates(
            module, '- -', '012', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=10,
            swap_cost=10, separators='- ')
        self.assertEqual(result, [])

    def test_out_of_alphabet_characters_are_kept(self):
        """Characters outside the alphabet can only be matched or removed."""
        module = ExactModule(('A1',))
        result = candidates.find_candidates(
            module, 'A0', '01', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10)
        self.assertEqual(
            result,
            [candidates.Candidate('A1', 1, (1,))])
        exact = candidates.find_candidates(
            module, 'A1', '01', k=10, budget=0)
        self.assertEqual(
            exact,
            [candidates.Candidate('A1', 0, ())])

    def test_compact_merges_distinct_raw_strings(self):
        """Equal-cost raws that compact alike merge once with union positions."""
        module = ExactModule(('1',), strip='0')
        result = candidates.find_candidates(
            module, '11', '01', k=10, budget=1,
            insert_cost=10, delete_cost=10, substitute_cost=1,
            swap_cost=10)
        # substituting either character gives '01' or '10', both compact '1'
        self.assertEqual(
            result,
            [candidates.Candidate('1', 1, (0, 1))])

    def test_invalid_arguments(self):
        """Bad parameters are rejected up front."""
        module = ExactModule(('1',))
        with self.assertRaises(ValueError):
            candidates.find_candidates(module, '0', '01', insert_cost=0)
        with self.assertRaises(ValueError):
            candidates.find_candidates(module, '0', '01', swap_cost=-1)
        with self.assertRaises(ValueError):
            candidates.find_candidates(module, '0', '01', k=0)
        with self.assertRaises(ValueError):
            candidates.find_candidates(module, '0', '01', budget=-1)
        with self.assertRaises(ValueError):
            candidates.find_candidates(module, '0', '')
        with self.assertRaises(ValueError):
            candidates.find_candidates(module, '0', ('01',))
        with self.assertRaises(TypeError):
            candidates.find_candidates(object(), '0', '01')
        with self.assertRaises(TypeError):
            candidates.find_candidates(module, 0, '01')
        with self.assertRaises(TypeError):
            candidates.find_candidates(
                module, '0', '01', separators=('-',))
        # duplicate alphabet characters are tolerated and deduplicated
        self.assertEqual(
            candidates.find_candidates(
                module, '0', '001', k=10, budget=1,
                insert_cost=10, delete_cost=10, substitute_cost=1,
                swap_cost=10),
            [candidates.Candidate('1', 1, (0,))])

    def test_matches_brute_force(self):
        """The search agrees with an independent exhaustive implementation."""
        random.seed(20261112)
        module = LuhnModule()
        for _ in range(60):
            length = random.randint(1, 5)
            source = ''.join(random.choice('012- ') for _ in range(length))
            alphabet = '012'
            costs = (
                random.randint(1, 3), random.randint(1, 3),
                random.randint(1, 3), random.randint(1, 3))
            budget = random.randint(0, 2)
            found = candidates.find_candidates(
                module, source, alphabet, k=1000, budget=budget,
                insert_cost=costs[0], delete_cost=costs[1],
                substitute_cost=costs[2], swap_cost=costs[3],
                separators=' -')
            expected = brute_force(
                source, alphabet, ' -', budget, costs, module)
            self.assertEqual(
                {(item.number, item.cost, item.positions) for item in found},
                {(number, cost, positions)
                 for number, (cost, positions) in expected.items()})
            self.assertTrue(
                all(found[i].cost <= found[i + 1].cost
                    for i in range(len(found) - 1)))


if __name__ == '__main__':
    unittest.main()
