# test_unicode_cleaning.py - tests for Unicode cleaning versus compact()
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

"""Tests for Unicode cleaning versus the modules' own compact().

Number imports commonly keep both the original number and its compact
key. These tests exercise modules with different separator rules
(stdnum.iban, stdnum.ch.uid and stdnum.by.unp) and compare, per sample,
whether the original passes validation, whether a broadly cleaned
(NFKC) form passes validation and whether two originals fall into the
same key.

Pairs of inputs on which a broad compatibility normalisation and the
module's own compact() disagree (one merges the pair, the other keeps
the sides apart) are searched automatically and shrunk to the shortest
pair. The module mapping is taken as is: compact() includes the
stdnum.util.clean() replacement table, which already maps many Unicode
spaces, dashes and full-width digits to ASCII. Shrinking preserves
whether each side passes validation (check digit included) so it stays
visible at which level a sample is misplaced.
"""

from __future__ import annotations

import unicodedata
from typing import Callable, Optional, Protocol

import pytest

from stdnum import iban
from stdnum.by import unp
from stdnum.ch import uid


class CompactModule(Protocol):
    """The parts of a number module that these tests rely on."""

    def compact(self, number: str) -> str:
        """Return the module's own minimal representation."""
        ...

    def is_valid(self, number: str) -> bool:
        """Return whether the number passes validation."""
        ...


Relation = Callable[[CompactModule, str, str], bool]
Category = Callable[[str], bool]


def broad_clean(number: str) -> str:
    """Clean a number the way a broad import pipeline would: NFKC
    compatibility normalisation, stripping of surrounding whitespace
    and case folding."""
    return unicodedata.normalize('NFKC', number).strip().upper()


def broad_merges_only(module: CompactModule, a: str, b: str) -> bool:
    """Broad cleaning merges the pair while the module keeps it apart."""
    return (
        broad_clean(a) == broad_clean(b) and
        module.compact(a) != module.compact(b))


def module_merges_only(module: CompactModule, a: str, b: str) -> bool:
    """The module merges the pair while broad cleaning keeps it apart."""
    return (
        module.compact(a) == module.compact(b) and
        broad_clean(a) != broad_clean(b))


RELATIONS: dict[str, Relation] = {
    'broad_merges_only': broad_merges_only,
    'module_merges_only': module_merges_only,
}


def _has_combining(number: str) -> bool:
    """Return whether the number contains a combining character."""
    return any(unicodedata.combining(ch) for ch in number)


def _has_fullwidth(number: str) -> bool:
    """Return whether the number contains a full-width character."""
    return any(
        unicodedata.east_asian_width(ch) in ('F', 'W') for ch in number)


CATEGORIES: dict[str, Category] = {
    'anything': lambda number: True,
    'combining': _has_combining,
    'fullwidth': _has_fullwidth,
}


# Look-alikes for the separators the modules strip. The mapping also
# crosses between ASCII separators so that pairs the module merges can
# be found.
_SEPARATOR_VARIANTS = {
    ' ': '\t\u00a0\u2000\u2007\u202f\u3000-.',
    '-': '\u2010\u2011\u2012\u2013\u2212\uff0d .',
    '.': '\u00b7\u2024\u2027\uff0e -',
}

# Cyrillic letters that stdnum.by.unp maps to Latin ones.
_CYRILLIC_LOOKALIKES = {
    'A': '\u0410', 'B': '\u0412', 'E': '\u0415', 'K': '\u041a', 'M': '\u041c',
    'H': '\u041d', 'O': '\u041e', 'P': '\u0420', 'C': '\u0421', 'T': '\u0422',
}


def _char_variants(ch: str) -> set[str]:
    """Return look-alike replacements for a single character."""
    variants = set(_SEPARATOR_VARIANTS.get(ch, ''))
    if ch.isascii() and ch.isdigit():
        variants.add(chr(0xff10 + int(ch)))  # full-width digit
    if ch.isascii() and ch.isalpha():
        variants.add(chr(ord(ch) + 0xfee0))  # full-width letter
        variants.add(ch.lower() if ch.isupper() else ch.upper())
        for mark in ('\u0301', '\u0327'):
            variants.add(ch + mark)  # original combining sequence
            composed = unicodedata.normalize('NFC', ch + mark)
            if len(composed) == 1:
                variants.add(composed)  # precomposed form
        variants.add(_CYRILLIC_LOOKALIKES.get(ch, ch))
    variants.discard(ch)
    return variants


def _candidates(seeds: tuple[str, ...]) -> list[str]:
    """Return the seeds and all their single-character look-alike
    variants, ordered by length so shorter pairs are found first."""
    found = set(seeds)
    for seed in seeds:
        for pos, ch in enumerate(seed):
            for variant in _char_variants(ch):
                found.add(seed[:pos] + variant + seed[pos + 1:])
    return sorted(found, key=lambda number: (len(number), number))


def find_pair(
    module: CompactModule,
    relation: Relation,
    category: Category,
    seeds: tuple[str, ...],
) -> Optional[tuple[str, str]]:
    """Find the pair of candidates with the shortest longest side on
    which the relation holds and at least one side falls in the
    category. Return None if no such pair exists."""
    earlier: list[str] = []
    for candidate in _candidates(seeds):
        for other in earlier:
            if (relation(module, other, candidate) and
                    (category(other) or category(candidate))):
                return other, candidate
        earlier.append(candidate)
    return None


def _reductions(a: str, b: str) -> list[tuple[str, str]]:
    """Return all one-step reductions of the pair: stripping a common
    affix or deleting one character from either side."""
    reductions = []
    if a and b and a[0] == b[0]:
        reductions.append((a[1:], b[1:]))
    if a and b and a[-1] == b[-1]:
        reductions.append((a[:-1], b[:-1]))
    reductions.extend(
        (a[:i] + a[i + 1:], b) for i in range(len(a)))
    reductions.extend(
        (a, b[:i] + b[i + 1:]) for i in range(len(b)))
    return reductions


def _acceptable(
    module: CompactModule,
    relation: Relation,
    category: Category,
    valid: tuple[bool, bool],
    a: str,
    b: str,
) -> bool:
    """Return whether the pair still shows the same divergence, stays
    in the category and keeps each side's validation verdict (check
    digit included) intact."""
    return (
        bool(a) and bool(b) and
        relation(module, a, b) and
        (category(a) or category(b)) and
        (module.is_valid(a), module.is_valid(b)) == valid)


def shrink_pair(
    module: CompactModule,
    relation: Relation,
    category: Category,
    pair: tuple[str, str],
) -> tuple[str, str]:
    """Shrink the pair while keeping the divergence, the category and
    each side's validation verdict (check digit included) intact."""
    a, b = pair
    valid = (module.is_valid(a), module.is_valid(b))
    reduced = True
    while reduced:
        reduced = False
        for x, y in _reductions(a, b):
            if _acceptable(module, relation, category, valid, x, y):
                a, b = x, y
                reduced = True
                break
    return a, b


MODULE_SEEDS = [
    pytest.param(
        iban, ('GR16 0110 1050 0000 1054 7023 795', 'BE31435411161155'),
        id='iban'),
    pytest.param(
        uid, ('CHE-100.155.212', '100.155.212'),
        id='ch.uid'),
    pytest.param(
        unp, ('200988541', '\u0423\u041d\u041f MA1953684'),
        id='by.unp'),
]

# Samples with the expected validation verdict of the original and of
# the broadly cleaned form. The expectations follow the modules' real
# mapping: compact() applies the stdnum.util.clean() replacement table,
# which already covers many spaces, dashes and full-width digits but
# not, for example, full-width letters or superscripts.
VERDICTS = [
    pytest.param(
        iban, 'GR16 0110 1050 0000 1054 7023 795', True, True,
        id='iban-plain'),
    pytest.param(
        iban,
        'GR16\u30000110\u30001050\u30000000\u30001054\u30007023\u3000795',
        True, True, id='iban-ideographic-space'),
    pytest.param(
        iban, '\uff22\uff2531435411161155', False, True,
        id='iban-fullwidth-letters'),
    pytest.param(
        iban, 'BE\u030131435411161155', False, False,
        id='iban-combining-acute'),
    pytest.param(
        uid, 'CHE-100.155.212', True, True,
        id='ch.uid-plain'),
    pytest.param(
        uid, 'CHE\uff0d100.155.212', True, True,
        id='ch.uid-fullwidth-hyphen'),
    pytest.param(
        uid, 'CHE\u2011100.155.212', True, True,
        id='ch.uid-non-breaking-hyphen'),
    pytest.param(
        uid, 'CHE\u0301-100.155.212', False, False,
        id='ch.uid-combining-acute'),
    pytest.param(
        uid, '\uff23HE-100.155.212', False, True,
        id='ch.uid-fullwidth-letter'),
    pytest.param(
        unp, '200988541', True, True,
        id='by.unp-plain'),
    pytest.param(
        unp, '\u0423\u041d\u041f MA1953684', True, True,
        id='by.unp-cyrillic-prefix'),
    pytest.param(
        unp, '\u0423\u041d\u041f\u3000MA1953684', True, True,
        id='by.unp-ideographic-space'),
    pytest.param(
        unp, '\u0423\u041d\u041f\u00a0MA1953684', True, True,
        id='by.unp-no-break-space'),
    pytest.param(
        unp, '\uff12\uff10\uff10988541', True, True,
        id='by.unp-fullwidth-digits'),
    pytest.param(
        unp, '\u0423\u041d\u041f \u041cA1953684', True, True,
        id='by.unp-cyrillic-letter'),
    pytest.param(
        unp, '\u0423\u041d\u041f \uff2dA1953684', False, True,
        id='by.unp-fullwidth-letter'),
    pytest.param(
        unp, '\u00b200988541', False, True,
        id='by.unp-superscript'),
]

# Pairs of originals with the expected outcome of "do they fall into
# the same key", both for the module's compact() and for broad
# cleaning.
KEY_PAIRS = [
    pytest.param(
        iban,
        'GR16 0110 1050 0000 1054 7023 795',
        'GR16-0110-1050-0000-1054-7023-795',
        True, False, id='iban-separator-styles'),
    pytest.param(
        iban, 'BE31435411161155', '\uff22\uff2531435411161155',
        False, True, id='iban-fullwidth-letters'),
    pytest.param(
        uid, 'CHE-100.155.212', '100.155.212',
        True, False, id='ch.uid-optional-prefix'),
    pytest.param(
        uid, 'CHE-100.155.212', 'CHE\uff0d100.155.212',
        True, True, id='ch.uid-fullwidth-hyphen'),
    pytest.param(
        uid, 'CHE-100.155.212', 'CHE\u2011100.155.212',
        True, False, id='ch.uid-non-breaking-hyphen'),
    pytest.param(
        uid, 'CHE-100.155.212', '\uff23HE-100.155.212',
        False, True, id='ch.uid-fullwidth-letter'),
    pytest.param(
        unp, '\u0423\u041d\u041f MA1953684', 'unp ma1953684',
        True, False, id='by.unp-prefix-and-case'),
    pytest.param(
        unp, '200988541', '\uff12\uff10\uff10988541',
        True, True, id='by.unp-fullwidth-digits'),
    pytest.param(
        unp, '\u0423\u041d\u041f MA1953684', '\u0423\u041d\u041f \uff2dA1953684',
        False, True, id='by.unp-fullwidth-letter'),
]


@pytest.mark.parametrize('module,number,original,cleaned', VERDICTS)
def test_validation_verdicts(
    module: CompactModule,
    number: str,
    original: bool,
    cleaned: bool,
) -> None:
    """The original and the broadly cleaned form get the expected
    validation verdicts."""
    assert module.is_valid(number) is original
    assert module.is_valid(broad_clean(number)) is cleaned


@pytest.mark.parametrize('module,a,b,same_module_key,same_broad_key', KEY_PAIRS)
def test_key_assignment(
    module: CompactModule,
    a: str,
    b: str,
    same_module_key: bool,
    same_broad_key: bool,
) -> None:
    """Two originals fall into the same key exactly as expected, both
    for the module's compact() and for broad cleaning."""
    assert (module.compact(a) == module.compact(b)) is same_module_key
    assert (broad_clean(a) == broad_clean(b)) is same_broad_key


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
def test_valid_numbers_stay_valid_and_apart(
    module: CompactModule,
    seeds: tuple[str, ...],
) -> None:
    """Broad cleaning keeps every accepted number valid and in the same
    compact key, and never merges two accepted numbers with different
    keys."""
    valid = [
        number for number in _candidates(seeds) if module.is_valid(number)]
    assert valid
    for number in valid:
        assert module.is_valid(broad_clean(number))
        assert module.compact(broad_clean(number)) == module.compact(number)
    for a in valid:
        for b in valid:
            if module.compact(a) != module.compact(b):
                assert broad_clean(a) != broad_clean(b)


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
@pytest.mark.parametrize('relation_name,category_name', [
    ('broad_merges_only', 'anything'),
    ('broad_merges_only', 'combining'),
    ('broad_merges_only', 'fullwidth'),
    ('module_merges_only', 'anything'),
])
def test_shortest_divergent_pair(
    module: CompactModule,
    seeds: tuple[str, ...],
    relation_name: str,
    category_name: str,
) -> None:
    """The shortest divergent pair exists, survives shrinking with each
    side's validation verdict intact and cannot be shrunk any further."""
    relation = RELATIONS[relation_name]
    category = CATEGORIES[category_name]
    pair = find_pair(module, relation, category, seeds)
    assert pair is not None
    valid = (module.is_valid(pair[0]), module.is_valid(pair[1]))
    shrunk = shrink_pair(module, relation, category, pair)
    assert _acceptable(module, relation, category, valid, *shrunk)
    for reduced in _reductions(*shrunk):
        assert not _acceptable(module, relation, category, valid, *reduced)


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
def test_fullwidth_pair_crosses_validity_boundary(
    module: CompactModule,
    seeds: tuple[str, ...],
) -> None:
    """The shortest broad-merges pair involving a full-width character
    merges a valid number with an invalid one, and shrinking keeps it
    that way."""
    pair = find_pair(
        module, broad_merges_only, CATEGORIES['fullwidth'], seeds)
    assert pair is not None
    valid = (module.is_valid(pair[0]), module.is_valid(pair[1]))
    assert sorted(valid) == [False, True]
    shrunk = shrink_pair(
        module, broad_merges_only, CATEGORIES['fullwidth'], pair)
    assert (module.is_valid(shrunk[0]), module.is_valid(shrunk[1])) == valid


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
def test_combining_pair_shrinks_to_composition_difference(
    module: CompactModule,
    seeds: tuple[str, ...],
) -> None:
    """The shortest broad-merges pair involving a combining character
    shrinks to a precomposed character versus its decomposition."""
    pair = find_pair(
        module, broad_merges_only, CATEGORIES['combining'], seeds)
    assert pair is not None
    shrunk = shrink_pair(
        module, broad_merges_only, CATEGORIES['combining'], pair)
    precomposed = [side for side in shrunk if not _has_combining(side)]
    decomposed = [side for side in shrunk if _has_combining(side)]
    assert len(precomposed) == 1
    assert len(decomposed) == 1
    assert len(precomposed[0]) == 1
    assert len(decomposed[0]) == 2
    assert broad_clean(precomposed[0]) == broad_clean(decomposed[0])


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
def test_module_merges_pair_stays_valid(
    module: CompactModule,
    seeds: tuple[str, ...],
) -> None:
    """The shortest pair the module merges but broad cleaning keeps
    apart consists of two valid numbers."""
    pair = find_pair(module, module_merges_only, CATEGORIES['anything'], seeds)
    assert pair is not None
    assert all(module.is_valid(side) for side in pair)


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
def test_module_never_merges_combining_marks(
    module: CompactModule,
    seeds: tuple[str, ...],
) -> None:
    """The module's own compact() never merges a pair because of a
    combining mark."""
    assert find_pair(
        module, module_merges_only, CATEGORIES['combining'], seeds) is None


@pytest.mark.parametrize('module,seeds', MODULE_SEEDS)
def test_module_merges_fullwidth_pair_stays_valid(
    module: CompactModule,
    seeds: tuple[str, ...],
) -> None:
    """The shortest pair with a full-width character that the module
    merges but broad cleaning keeps apart consists of two valid
    numbers, and shrinking keeps it that way."""
    pair = find_pair(
        module, module_merges_only, CATEGORIES['fullwidth'], seeds)
    assert pair is not None
    valid = (module.is_valid(pair[0]), module.is_valid(pair[1]))
    assert valid == (True, True)
    shrunk = shrink_pair(
        module, module_merges_only, CATEGORIES['fullwidth'], pair)
    assert (module.is_valid(shrunk[0]), module.is_valid(shrunk[1])) == valid
