# candidates.py - budgeted candidate search for validation modules
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

"""Budgeted search for compact numbers close to a supplied value.

This module does not modify the supplied number. Instead it enumerates the
different compact results that can be reached from the number within a fixed
editing budget and that pass a validation module's ``validate()``.

The allowed edits are insertion, deletion, substitution and swapping of two
adjacent characters, each with a configurable positive integer cost.
Characters in the separator set may be removed from the original number at
zero cost, so that numbers that only differ in separator usage converge on
the same candidate. A candidate that can be reached through several edits,
or through different separator cleanings, is reported only once but keeps
the position of every original character that is changed (substituted or
swapped) in one of its lowest-cost alignments.

>>> from stdnum import ean
>>> result = find_candidates(
...     ean, '73513538', '7', k=5, budget=1,
...     insert_cost=10, delete_cost=10, substitute_cost=1,
...     swap_cost=10)
>>> [(c.number, c.cost, c.positions) for c in result]
[('73513537', 1, (7,))]

The same candidate is found when separators are mixed into the number:

>>> result = find_candidates(
...     ean, '73-51 3538', '7', k=5, budget=1,
...     insert_cost=10, delete_cost=10, substitute_cost=1,
...     swap_cost=10, separators=' -')
>>> [(c.number, c.cost, c.positions) for c in result]
[('73513537', 1, (9,))]
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Sequence

from stdnum.exceptions import ValidationError


if TYPE_CHECKING:
    from typing import Protocol

    class ValidationModule(Protocol):
        """Minimal interface for a number validation module."""

        def compact(self, number: str) -> str:
            """Convert the number to the minimal representation."""

        def validate(self, number: str) -> str:
            """Check if the number provided is a valid number of its type."""

else:  # pragma: no cover (runtime duck typing of validation modules)
    ValidationModule = Any


_INFINITY = 10 ** 18


class _Costs(NamedTuple):
    """Positive integer costs of the supported edits."""

    insert: int
    delete: int
    substitute: int
    swap: int


@dataclass(frozen=True)
class Candidate:
    """A single validated candidate for a number.

    ``number`` is the compact representation of the candidate, ``cost`` is
    the lowest total editing cost at which it can be derived from the
    supplied number and ``positions`` holds the zero-based positions of the
    characters of the original number that are changed (substituted or
    swapped) in at least one lowest-cost derivation. Unchanged matches,
    insertions, deletions and zero-cost separator cleanings do not
    contribute positions, so an exact match has an empty position tuple.
    """

    number: str
    cost: int
    positions: tuple[int, ...]


def _require_positive_int(value: Any, name: str) -> None:
    """Check that the value is a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('%s must be a positive integer' % name)


def _require_alphabet(alphabet: Sequence[str]) -> str:
    """Check the alphabet and return it without duplicate characters."""
    chars = tuple(alphabet)
    if not chars or any(not isinstance(char, str) or len(char) != 1
                        for char in chars):
        raise ValueError('alphabet must be a non-empty sequence of '
                         'single-character strings')
    return ''.join(dict.fromkeys(chars))


def _moves(
    i: int,
    j: int,
    source: str,
    alphabet: str,
    separators: str,
    max_len: int,
    costs: _Costs,
) -> Sequence[tuple[int, int, str, int]]:
    """Return the edits that are possible from the supplied state."""
    moves: list[tuple[int, int, str, int]] = []
    n = len(source)
    if i < n:
        char = source[i]
        if char in separators:
            # separators can only be cleaned away, never matched
            moves.append((i + 1, j, '', 0))
        else:
            # delete the character
            moves.append((i + 1, j, '', costs.delete))
            if j < max_len:
                # keep the character unchanged
                moves.append((i + 1, j + 1, char, 0))
                # substitute it with an alphabet character
                for replacement in alphabet:
                    if replacement != char:
                        moves.append((
                            i + 1, j + 1, replacement,
                            costs.substitute))
            # swap two adjacent, different source characters
            if (i + 1 < n and j + 1 < max_len
                    and char != source[i + 1]
                    and source[i + 1] not in separators):
                moves.append((
                    i + 2, j + 2, source[i + 1] + char, costs.swap))
    if j < max_len:
        # insert an alphabet character
        for inserted in alphabet:
            moves.append((i, j + 1, inserted, costs.insert))
    return moves


def _enumerate_raw(
    source: str,
    alphabet: str,
    separators: str,
    budget: int,
    costs: _Costs,
) -> dict[str, int]:
    """Enumerate reachable raw strings and their minimum derivation cost.

    A state records how many source and result characters have been produced.
    Uniform-cost search keeps the lowest cost for each state and merges the
    different editing paths that produce the same resulting string.
    """
    max_len = len(source) + budget
    initial = (0, 0, '')
    best: dict[tuple[int, int, str], int] = {initial: 0}
    results: dict[str, int] = {}
    heap: list[tuple[int, int, int, str]] = [(0, 0, 0, '')]
    while heap:
        cost, i, j, prefix = heapq.heappop(heap)
        state = (i, j, prefix)
        if cost > best.get(state, _INFINITY):
            continue
        if i == len(source):
            results[prefix] = cost
        for ni, nj, added, edge_cost in _moves(
                i, j, source, alphabet, separators, max_len, costs):
            new_cost = cost + edge_cost
            if new_cost > budget:
                continue
            new_prefix = prefix + added
            new_state = (ni, nj, new_prefix)
            if new_cost < best.get(new_state, _INFINITY):
                best[new_state] = new_cost
                heapq.heappush(
                    heap, (new_cost, ni, nj, new_prefix))
    return results


def _align(
    source: str,
    target: str,
    alphabet: str,
    separators: str,
    costs: _Costs,
) -> tuple[int, tuple[int, ...]]:
    """Return the minimum alignment cost and the union of the original
    positions that are changed (substituted or swapped) by minimum-cost
    alignments between both strings. The target is a raw enumerated string
    containing only alphabet characters and unchanged source characters."""
    n, m = len(source), len(target)
    dp = [[_INFINITY] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            options: list[int] = []
            if i:
                if source[i - 1] in separators:
                    # a separator can only be cleaned away for free
                    options.append(dp[i - 1][j])
                else:
                    options.append(dp[i - 1][j] + costs.delete)
            if j and target[j - 1] in alphabet:
                options.append(dp[i][j - 1] + costs.insert)
            if i and j and source[i - 1] not in separators:
                if source[i - 1] == target[j - 1]:
                    options.append(dp[i - 1][j - 1])
                elif target[j - 1] in alphabet:
                    options.append(
                        dp[i - 1][j - 1] + costs.substitute)
            if (i >= 2 and j >= 2
                    and source[i - 2] not in separators
                    and source[i - 1] not in separators
                    and source[i - 2] == target[j - 1]
                    and source[i - 1] == target[j - 2]
                    and source[i - 2] != source[i - 1]):
                options.append(dp[i - 2][j - 2] + costs.swap)
            # an empty option list marks an unreachable alignment
            dp[i][j] = min(options, default=_INFINITY)
    changed: set[int] = set()
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[int, int]] = [(n, m)]
    while stack:
        i, j = stack.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i and source[i - 1] in separators:
            if dp[i - 1][j] == dp[i][j]:
                stack.append((i - 1, j))
            continue
        if i and dp[i - 1][j] + costs.delete == dp[i][j]:
            stack.append((i - 1, j))
        if (j and target[j - 1] in alphabet
                and dp[i][j - 1] + costs.insert == dp[i][j]):
            stack.append((i, j - 1))
        if i and j and source[i - 1] not in separators:
            if source[i - 1] == target[j - 1]:
                if dp[i - 1][j - 1] == dp[i][j]:
                    stack.append((i - 1, j - 1))
            elif (target[j - 1] in alphabet
                    and dp[i - 1][j - 1] + costs.substitute == dp[i][j]):
                changed.add(i - 1)
                stack.append((i - 1, j - 1))
        if (i >= 2 and j >= 2
                and source[i - 2] not in separators
                and source[i - 1] not in separators
                and source[i - 2] == target[j - 1]
                and source[i - 1] == target[j - 2]
                and source[i - 2] != source[i - 1]
                and dp[i - 2][j - 2] + costs.swap == dp[i][j]):
            changed.add(i - 2)
            changed.add(i - 1)
            stack.append((i - 2, j - 2))
    return dp[n][m], tuple(sorted(changed))


def find_candidates(
    module: ValidationModule,
    number: str,
    alphabet: Sequence[str],
    *,
    k: int = 10,
    budget: int = 2,
    insert_cost: int = 1,
    delete_cost: int = 1,
    substitute_cost: int = 1,
    swap_cost: int = 1,
    separators: str = '',
) -> list[Candidate]:
    """List the validated compact candidates for the supplied number.

    ``module`` is a stdnum validation module (it needs ``compact()`` and
    ``validate()``), ``number`` the raw value and ``alphabet`` the finite
    set of characters that insertions and substitutions may use. The four
    edit costs must be positive integers and only edits whose cumulative
    cost stays within ``budget`` are considered. Characters in
    ``separators`` may be removed from ``number`` at zero cost.

    The candidates are ordered by ascending cost and then compact value.
    The first ``k`` costs are returned and every candidate tied with the
    ``k``-th candidate is included. Candidates whose ``validate()`` fails
    are discarded before ranking. Each distinct compact result is returned
    once, together with every original position changed by a lowest-cost
    derivation.
    """
    if (not callable(getattr(module, 'compact', None))
            or not callable(getattr(module, 'validate', None))):
        raise TypeError('module must provide compact() and validate()')
    if not isinstance(number, str):
        raise TypeError('number must be a string')
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError('k must be a positive integer')
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError('budget must be a non-negative integer')
    if not isinstance(separators, str):
        raise TypeError('separators must be a string')
    _require_positive_int(insert_cost, 'insert_cost')
    _require_positive_int(delete_cost, 'delete_cost')
    _require_positive_int(substitute_cost, 'substitute_cost')
    _require_positive_int(swap_cost, 'swap_cost')
    alphabet = _require_alphabet(alphabet)
    separators = ''.join(dict.fromkeys(separators))
    costs = _Costs(insert_cost, delete_cost, substitute_cost, swap_cost)

    raw_costs = _enumerate_raw(
        number, alphabet, separators, budget, costs)

    # Merge raw strings that compact to the same minimal representation.
    groups: dict[str, tuple[int, list[str]]] = {}
    for raw, raw_cost in raw_costs.items():
        try:
            compacted = module.compact(raw)
        except ValidationError:
            continue
        group = groups.get(compacted)
        if group is None or raw_cost < group[0]:
            groups[compacted] = (raw_cost, [raw])
        elif raw_cost == group[0]:
            group[1].append(raw)

    candidates: list[Candidate] = []
    for compacted, (group_cost, raws) in groups.items():
        try:
            module.validate(compacted)
        except ValidationError:
            continue
        positions: set[int] = set()
        for raw in raws:
            _, raw_positions = _align(
                number, raw, alphabet, separators, costs)
            positions.update(raw_positions)
        candidates.append(
            Candidate(compacted, group_cost, tuple(sorted(positions))))

    candidates.sort(key=lambda candidate: (candidate.cost,
                                           candidate.number))
    if len(candidates) <= k:
        return candidates
    limit_cost = candidates[k - 1].cost
    return [candidate for candidate in candidates
            if candidate.cost <= limit_cost]
