import unittest

from flashtext import KeywordProcessor
from flashtext.damerau import (
    DamerauTrieMatcher,
    damerau_levenshtein_distance,
)


class TestDamerauLevenshteinDistance(unittest.TestCase):
    def test_basic_edits(self):
        self.assertEqual(damerau_levenshtein_distance('', 'abc'), 3)
        self.assertEqual(damerau_levenshtein_distance('abc', ''), 3)
        self.assertEqual(damerau_levenshtein_distance('abc', 'abc'), 0)
        self.assertEqual(damerau_levenshtein_distance('skpe', 'skype'), 1)
        self.assertEqual(damerau_levenshtein_distance('kitten', 'sitting'), 3)

    def test_adjacent_transposition_is_one_edit(self):
        # Plain Levenshtein (and the restricted OSA variant) would charge 2.
        self.assertEqual(damerau_levenshtein_distance('ab', 'ba'), 1)
        self.assertEqual(damerau_levenshtein_distance('nkie', 'nike'), 1)
        self.assertEqual(damerau_levenshtein_distance('abc', 'bca'), 2)

    def test_unrestricted_consecutive_edits(self):
        # CA -> AC (transpose) -> ABC (insert): the same character A is
        # involved in both edits, which only the unrestricted recurrence allows.
        self.assertEqual(damerau_levenshtein_distance('CA', 'ABC'), 2)
        # Restricted OSA cannot express this and returns 3.
        self.assertGreater(self._restricted_osa('CA', 'ABC'), 2)

    @staticmethod
    def _restricted_osa(left, right):
        rows = [list(range(len(right) + 1))]
        for i, left_char in enumerate(left, start=1):
            new_row = [i]
            for j, right_char in enumerate(right, start=1):
                cost = int(left_char != right_char)
                value = min(
                    rows[-1][j] + 1,
                    new_row[j - 1] + 1,
                    rows[-1][j - 1] + cost,
                )
                if (
                    i > 1 and j > 1
                    and left_char == right[j - 2]
                    and left[i - 2] == right_char
                ):
                    value = min(value, rows[-2][j - 2] + 1)
                new_row.append(value)
            rows.append(new_row)
        return rows[-1][-1]


class TestExtractDamerau(unittest.TestCase):
    def _processor(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike', 'Nike Inc')
        keyword_proc.add_keyword('adidas', 'Adidas AG')
        keyword_proc.add_keyword('puma', 'Puma SE')
        return keyword_proc

    def test_transposition_extracted_within_budget_one(self):
        keyword_proc = self._processor()
        matches = keyword_proc.extract_fuzzy_entities(
            'i love nkie shoes', max_cost=1, span_info=True,
        )
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match['entity'], 'Nike Inc')
        self.assertEqual(match['aliases'], ['nike'])
        self.assertEqual(match['distance'], 1)
        self.assertFalse(match['ambiguous'])
        self.assertEqual(match['span'], (7, 11))
        self.assertEqual('i love nkie shoes'[7:11], 'nkie')

    def test_ca_to_abc_budget_two(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('abc', 'ABC Brand')
        matches = keyword_proc.extract_fuzzy_entities(
            'ca', max_cost=2, span_info=True,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['entity'], 'ABC Brand')
        self.assertEqual(matches[0]['distance'], 2)

    def test_budget_rejects_too_distant_match(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('abc', 'ABC Brand')
        # CA -> ABC needs 2 unrestricted edits, so budget 1 must reject it.
        self.assertEqual(
            keyword_proc.extract_fuzzy_entities('ca', max_cost=1), [],
        )

    def test_returns_original_span_and_aliases(self):
        keyword_proc = self._processor()
        sentence = 'wearing addidas and pumaa today'
        matches = keyword_proc.extract_fuzzy_entities(
            sentence, max_cost=1, span_info=True,
        )
        self.assertEqual(
            [(m['entity'], m['span']) for m in matches],
            [('Adidas AG', (8, 15)), ('Puma SE', (20, 25))],
        )
        for match in matches:
            start, end = match['span']
            self.assertTrue(sentence[start:end].isalpha())

    def test_aliases_of_same_entity_are_merged(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('ab', 'Entity A')
        keyword_proc.add_keyword('ac', 'Entity A')
        matches = keyword_proc.extract_fuzzy_entities(
            'ad', max_cost=1, span_info=True,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['entity'], 'Entity A')
        self.assertEqual(matches[0]['aliases'], ['ab', 'ac'])
        self.assertFalse(matches[0]['ambiguous'])
        self.assertEqual(
            keyword_proc.extract_fuzzy_entities('ad', max_cost=1),
            ['Entity A'],
        )

    def test_equal_distance_different_entities_go_to_human(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('ab', 'Entity A')
        keyword_proc.add_keyword('cb', 'Entity B')
        matches = keyword_proc.extract_fuzzy_entities(
            'xb', max_cost=1, span_info=True,
        )
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertTrue(match['ambiguous'])
        self.assertIsNone(match['entity'])
        self.assertEqual(match['distance'], 1)
        self.assertEqual(
            sorted(c['entity'] for c in match['candidates']),
            ['Entity A', 'Entity B'],
        )
        # Flat form surfaces both candidate entities instead of guessing.
        self.assertEqual(
            keyword_proc.extract_fuzzy_entities('xb', max_cost=1),
            [['Entity A', 'Entity B']],
        )

    def test_exact_match_wins_over_equidistant_typos(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike', 'Nike Inc')
        keyword_proc.add_keyword('noke', 'Noke Co')
        matches = keyword_proc.extract_fuzzy_entities(
            'nike', max_cost=1, span_info=True,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['entity'], 'Nike Inc')
        self.assertEqual(matches[0]['distance'], 0)
        self.assertFalse(matches[0]['ambiguous'])

    def test_case_insensitive_by_default(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike', 'Nike Inc')
        matches = keyword_proc.extract_fuzzy_entities(
            'NKIE', max_cost=1, span_info=True,
        )
        self.assertEqual(matches[0]['entity'], 'Nike Inc')
        self.assertEqual(matches[0]['span'], (0, 4))

    def test_invalid_budget(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike')
        with self.assertRaises(ValueError):
            keyword_proc.extract_fuzzy_entities('nike', max_cost=-1)

    def test_empty_sentence(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike')
        self.assertEqual(
            keyword_proc.extract_fuzzy_entities('', max_cost=2), [],
        )


class TestTriePruning(unittest.TestCase):
    def _decoy_leaf_ids(self, keyword_proc, decoys):
        leaf_ids = set()
        for decoy in decoys:
            node = keyword_proc.keyword_trie_dict
            for char in decoy:
                node = node[char]
            leaf_ids.add(id(node))
        return leaf_ids

    def test_unrelated_branches_are_never_walked_to_leaves(self):
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike', 'Nike Inc')
        decoys = ['z' * 10, 'q' * 10, 'x' * 10, 'v' * 10, 'w' * 10]
        for decoy in decoys:
            keyword_proc.add_keyword(decoy, 'Decoy ' + decoy)
        leaf_ids = self._decoy_leaf_ids(keyword_proc, decoys)

        matcher = DamerauTrieMatcher(
            keyword_proc.keyword_trie_dict,
            keyword_proc._keyword,
            max_cost=1,
            non_word_boundaries=keyword_proc.non_word_boundaries,
        )
        candidates = matcher.search('nike shoes', 0, 5)

        self.assertEqual([c.entity for c in candidates], ['Nike Inc'])
        # No decoy branch may be walked to its terminal leaf.
        self.assertEqual(matcher.visited_nodes & leaf_ids, set())
        # Only the first max_cost+1 levels of an impossible branch are probed.
        self.assertLessEqual(matcher.access_count, 20)

    def test_adding_more_impossible_branches_keeps_leaves_untouched(self):
        def build(depth):
            keyword_proc = KeywordProcessor()
            keyword_proc.add_keyword('nike', 'Nike Inc')
            decoys = []
            for index in range(40):
                decoy = chr(0x4e00 + index) * depth
                decoys.append(decoy)
                keyword_proc.add_keyword(decoy, 'Decoy {}'.format(index))
            return keyword_proc, decoys

        shallow, shallow_decoys = build(12)
        deep, deep_decoys = build(60)

        shallow_matches = shallow.extract_fuzzy_entities(
            'nike', max_cost=1, span_info=True,
        )
        deep_matches = deep.extract_fuzzy_entities(
            'nike', max_cost=1, span_info=True,
        )

        self.assertEqual(
            [m['entity'] for m in shallow_matches], ['Nike Inc'],
        )
        self.assertEqual(
            [m['entity'] for m in deep_matches], ['Nike Inc'],
        )
        # The deep branches are five times longer but never reach their
        # leaves, and the number of node accesses is identical: the cost of a
        # wholly impossible branch is bounded by the budget, not its depth.
        self.assertEqual(
            deep._damerau_matcher.visited_nodes
            & self._decoy_leaf_ids(deep, deep_decoys),
            set(),
        )
        self.assertEqual(
            shallow.damerau_access_count, deep.damerau_access_count,
        )

    def test_related_prefix_branch_still_reachable(self):
        # Pruning must not kill a sibling branch that shares the text prefix.
        keyword_proc = KeywordProcessor()
        keyword_proc.add_keyword('nike', 'Nike Inc')
        keyword_proc.add_keyword('nokia', 'Nokia Corp')
        matches = keyword_proc.extract_fuzzy_entities(
            'nakia phones', max_cost=1, span_info=True,
        )
        self.assertEqual(matches[0]['entity'], 'Nokia Corp')
        self.assertEqual(matches[0]['distance'], 1)


if __name__ == '__main__':
    unittest.main()
