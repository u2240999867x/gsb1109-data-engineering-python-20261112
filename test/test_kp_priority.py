from flashtext import KeywordProcessor
import logging
import unittest

logger = logging.getLogger(__name__)


class TestKPPriority(unittest.TestCase):
    def setUp(self):
        logger.info("Starting...")

    def tearDown(self):
        logger.info("Ending.")

    def test_weight_beats_longest_match(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('new york', 'New York City')  # default weight 1.0
        keyword_processor.add_keyword('new', weight=2.0)
        keyword_processor.add_keyword('york', weight=2.0)
        # old interface keeps the original longest-match behaviour
        self.assertEqual(keyword_processor.extract_keywords('new york'), ['New York City'])
        # priority mode: 2.0 + 2.0 beats 1.0
        self.assertEqual(keyword_processor.extract_keywords_with_priority('new york'), ['new', 'york'])

    def test_long_entry_wins_with_higher_weight(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('new york', 'New York City', weight=5.0)
        keyword_processor.add_keyword('new', weight=2.0)
        keyword_processor.add_keyword('york', weight=2.0)
        self.assertEqual(keyword_processor.extract_keywords_with_priority('new york'), ['New York City'])

    def test_total_weight_maximized_over_sentence(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('alpha beta', weight=2.0)
        keyword_processor.add_keyword('alpha', weight=1.5)
        keyword_processor.add_keyword('beta', weight=1.5)
        # 1.5 + 1.5 > 2.0, so the two short hits win together
        self.assertEqual(keyword_processor.extract_keywords_with_priority('alpha beta'), ['alpha', 'beta'])

    def test_tie_break_prefers_larger_coverage(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('ab cd', weight=1.0)
        keyword_processor.add_keyword('ab', weight=0.5)
        keyword_processor.add_keyword('cd', weight=0.5)
        # equal total weight (1.0), coverage 5 beats coverage 4
        self.assertEqual(keyword_processor.extract_keywords_with_priority('ab cd'), ['ab cd'])

    def test_tie_break_prefers_earlier_and_longer_spans(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('ab', weight=1.0)
        keyword_processor.add_keyword('a', weight=0.5)
        keyword_processor.add_keyword('b', weight=0.5)
        # equal weight and coverage, starts tuple (0,) beats (0, 1)
        self.assertEqual(keyword_processor.extract_keywords_with_priority('ab'), ['ab'])

    def test_return_conflicts(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('new york', 'NY', weight=5.0)
        keyword_processor.add_keyword('new', weight=1.0)
        keyword_processor.add_keyword('york', weight=1.0)
        selected, conflicts = keyword_processor.extract_keywords_with_priority(
            'new york', return_conflicts=True)
        self.assertEqual(selected, ['NY'])
        self.assertEqual(conflicts, [('new', 0, 3), ('york', 4, 8)])

    def test_conflicts_exclude_non_overlapping_hits(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('new york', 'NY', weight=5.0)
        keyword_processor.add_keyword('new', weight=1.0)
        keyword_processor.add_keyword('paris', weight=1.0)
        selected, conflicts = keyword_processor.extract_keywords_with_priority(
            'new york and paris', return_conflicts=True)
        self.assertEqual(selected, ['NY', 'paris'])
        self.assertEqual(conflicts, [('new', 0, 3)])

    def test_span_info_matches_original_sentence(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('Big Apple', 'New York', weight=3.0)
        keyword_processor.add_keyword('Apple', weight=1.0)
        sentence = 'I love Big Apple very much'
        selected = keyword_processor.extract_keywords_with_priority(sentence, span_info=True)
        self.assertEqual(selected, [('New York', 7, 16)])
        self.assertEqual(sentence[7:16], 'Big Apple')

    def test_word_boundaries_respected(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('he', weight=10.0)
        self.assertEqual(keyword_processor.extract_keywords_with_priority('the heat'), [])
        self.assertEqual(keyword_processor.extract_keywords_with_priority('he said'), ['he'])

    def test_zero_weight_hit_still_selected_when_uncontested(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('solo', weight=0.0)
        self.assertEqual(keyword_processor.extract_keywords_with_priority('a solo hit'), ['solo'])

    def test_negative_weight_rejected(self):
        keyword_processor = KeywordProcessor()
        with self.assertRaises(ValueError):
            keyword_processor.add_keyword('bad', weight=-1)
        with self.assertRaises(ValueError):
            keyword_processor.add_keyword('bad', weight='heavy')
        with self.assertRaises(ValueError):
            keyword_processor.set_keyword_weight('bad', -0.5)

    def test_set_and_get_keyword_weight(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('NYC', 'New York')
        self.assertEqual(keyword_processor.get_keyword_weight('nyc'), 1.0)
        self.assertTrue(keyword_processor.set_keyword_weight('nyc', 2.5))
        self.assertEqual(keyword_processor.get_keyword_weight('NYC'), 2.5)
        self.assertFalse(keyword_processor.set_keyword_weight('missing', 1.0))

    def test_remove_keyword_drops_weight(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('nyc', weight=4.0)
        self.assertEqual(keyword_processor.get_keyword_weight('nyc'), 4.0)
        keyword_processor.remove_keyword('nyc')
        self.assertEqual(keyword_processor.get_keyword_weight('nyc'), 1.0)

    def test_case_sensitive_weights(self):
        keyword_processor = KeywordProcessor(case_sensitive=True)
        keyword_processor.add_keyword('Apple', weight=3.0)
        keyword_processor.add_keyword('apple', weight=1.0)
        self.assertEqual(keyword_processor.get_keyword_weight('Apple'), 3.0)
        self.assertEqual(keyword_processor.get_keyword_weight('apple'), 1.0)
        self.assertEqual(
            keyword_processor.extract_keywords_with_priority('Apple apple'), ['Apple', 'apple'])

    def test_empty_sentence(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('a', weight=1.0)
        self.assertEqual(keyword_processor.extract_keywords_with_priority(''), [])
        self.assertEqual(keyword_processor.extract_keywords_with_priority('', return_conflicts=True), ([], []))

    def test_old_interface_ignores_weights(self):
        keyword_processor = KeywordProcessor()
        keyword_processor.add_keyword('new york', 'NY', weight=0.1)
        keyword_processor.add_keyword('new', weight=10.0)
        # longest match still wins in the legacy interfaces
        self.assertEqual(keyword_processor.extract_keywords('new york'), ['NY'])
        self.assertEqual(keyword_processor.replace_keywords('new york'), 'NY')


if __name__ == '__main__':
    unittest.main()
