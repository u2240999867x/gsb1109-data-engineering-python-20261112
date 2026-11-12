import json
import multiprocessing
import os
import tempfile
import unittest

from flashtext import KeywordProcessor, SharedKeywordProcessor


def _mp_worker(path, sentence, queue):
    with SharedKeywordProcessor.open(path) as sp:
        queue.put({
            'keywords': sp.extract_keywords(sentence),
            'spans': sp.extract_keywords(sentence, span_info=True),
            'replaced': sp.replace_keywords(sentence),
            'length': len(sp),
            'case_sensitive': sp.case_sensitive,
        })


class TestSharedKeywordProcessor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dict_path = os.path.join(self.tmp.name, 'keywords.ftxd')
        base = KeywordProcessor()
        base.add_keyword('Big Apple', 'New York')
        base.add_keyword('Bay Area')
        base.add_keyword('NYC', 'New York')
        base.add_keyword('san francisco')
        SharedKeywordProcessor.publish(base, self.dict_path)
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_matches_keyword_processor(self):
        sentence = (
            'I love Big Apple and Bay Area, NYC is big. '
            'san francisco too.')
        with SharedKeywordProcessor.open(self.dict_path) as sp:
            self.assertEqual(
                sp.extract_keywords(sentence),
                self.base.extract_keywords(sentence))
            self.assertEqual(
                sp.extract_keywords(sentence, span_info=True),
                self.base.extract_keywords(sentence, span_info=True))

    def test_replace_matches_keyword_processor(self):
        sentence = 'I love Big Apple and bay area. nyc and SAN FRANCISCO.'
        with SharedKeywordProcessor.open(self.dict_path) as sp:
            self.assertEqual(
                sp.replace_keywords(sentence),
                self.base.replace_keywords(sentence))

    def test_aliases_and_containment(self):
        with SharedKeywordProcessor.open(self.dict_path) as sp:
            self.assertEqual(len(sp), len(self.base))
            self.assertEqual(sp.get_all_keywords(),
                             self.base.get_all_keywords())
            self.assertTrue('big apple' in sp)
            self.assertFalse('missing term' in sp)
            self.assertEqual(sp['big apple'], 'New York')
            self.assertEqual(sp.get_keyword('nyc'), 'New York')
            self.assertIsNone(sp.get_keyword('nowhere'))
            self.assertIsNone(sp['nowhere'])

    def test_case_sensitive_setting_travels_with_file(self):
        cs_path = os.path.join(self.tmp.name, 'cs.ftxd')
        kp = KeywordProcessor(case_sensitive=True)
        kp.add_keyword('Big Apple', 'New York')
        SharedKeywordProcessor.publish(kp, cs_path)

        with SharedKeywordProcessor.open(cs_path) as sp:
            self.assertTrue(sp.case_sensitive)
            self.assertEqual(sp.extract_keywords('big Apple'), [])
            self.assertEqual(
                sp.extract_keywords('Big Apple'), ['New York'])
        with self.assertRaises(ValueError):
            SharedKeywordProcessor.open(cs_path, case_sensitive=False)

        with SharedKeywordProcessor.open(self.dict_path) as sp:
            self.assertFalse(sp.case_sensitive)

    def test_non_word_boundaries_travel_with_file(self):
        path = os.path.join(self.tmp.name, 'nwb.ftxd')
        kp = KeywordProcessor()
        kp.add_non_word_boundary('-')
        kp.add_keyword('c++', 'cpp')
        kp.set_non_word_boundaries(kp.non_word_boundaries | set('+'))
        SharedKeywordProcessor.publish(kp, path)

        sentence = 'I write c++ daily.'
        with SharedKeywordProcessor.open(path) as sp:
            self.assertIn('+', sp.non_word_boundaries)
            self.assertEqual(
                sp.extract_keywords(sentence),
                kp.extract_keywords(sentence))
            self.assertEqual(
                sp.replace_keywords(sentence),
                kp.replace_keywords(sentence))

    def test_matches_full_json_corpus(self):
        with open('test/keyword_extractor_test_cases.json') as f:
            cases = json.load(f)
        path = os.path.join(self.tmp.name, 'corpus.ftxd')
        for case_sensitive in (False, True):
            for test_case in cases:
                kp = KeywordProcessor(case_sensitive=case_sensitive)
                for key in test_case['keyword_dict']:
                    kp.add_keywords_from_list(test_case['keyword_dict'][key])
                SharedKeywordProcessor.publish(kp, path)
                with SharedKeywordProcessor.open(path) as sp:
                    sentence = test_case['sentence']
                    self.assertEqual(
                        sp.extract_keywords(sentence),
                        kp.extract_keywords(sentence))
                    self.assertEqual(
                        sp.extract_keywords(sentence, span_info=True),
                        kp.extract_keywords(sentence, span_info=True))
                    self.assertEqual(
                        sp.replace_keywords(sentence),
                        kp.replace_keywords(sentence))

    def test_publish_is_atomic(self):
        kp = KeywordProcessor()
        kp.add_keyword('java', 'java')
        path = os.path.join(self.tmp.name, 'atomic.ftxd')
        SharedKeywordProcessor.publish(kp, path)
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.exists(path + '.tmp'))
        SharedKeywordProcessor.publish(kp, path)
        with SharedKeywordProcessor.open(path) as sp:
            self.assertEqual(sp.extract_keywords('java rocks'), ['java'])

    def test_reader_stays_on_old_version_after_republish(self):
        v1_path = os.path.join(self.tmp.name, 'v1.ftxd')
        v2_path = os.path.join(self.tmp.name, 'v2.ftxd')

        kp_v1 = KeywordProcessor()
        kp_v1.add_keyword('apple', 'fruit')
        SharedKeywordProcessor.publish(kp_v1, v1_path)

        old_reader = SharedKeywordProcessor.open(v1_path)
        try:
            kp_v2 = KeywordProcessor()
            kp_v2.add_keyword('apple', 'company')
            kp_v2.add_keyword('pear', 'fruit')
            SharedKeywordProcessor.publish(kp_v2, v2_path)

            self.assertEqual(
                old_reader.extract_keywords('apple and pear'),
                ['fruit'])
            with SharedKeywordProcessor.open(v2_path) as new_reader:
                self.assertEqual(
                    new_reader.extract_keywords('apple and pear'),
                    ['company', 'fruit'])
        finally:
            old_reader.close()
        self.assertTrue(old_reader.closed)

    def test_republish_over_same_path_keeps_existing_mapping_stable(self):
        path = os.path.join(self.tmp.name, 'same.ftxd')
        kp = KeywordProcessor()
        kp.add_keyword('apple', 'fruit')
        SharedKeywordProcessor.publish(kp, path)
        reader = SharedKeywordProcessor.open(path)
        try:
            kp2 = KeywordProcessor()
            kp2.add_keyword('apple', 'company')
            SharedKeywordProcessor.publish(kp2, path)
            self.assertEqual(reader.extract_keywords('apple'), ['fruit'])
        finally:
            reader.close()

    def test_node_lookups_only_track_visited_branches(self):
        title = 'apple unveils a new iphone today'

        small = KeywordProcessor()
        small.add_keyword('apple', 'Apple Inc')
        small.add_keyword('iphone', 'iPhone')
        small_path = os.path.join(self.tmp.name, 'small.ftxd')
        SharedKeywordProcessor.publish(small, small_path)

        large = KeywordProcessor()
        large.add_keyword('apple', 'Apple Inc')
        large.add_keyword('iphone', 'iPhone')
        for idx in range(2000):
            large.add_keyword('zebra{:04d}'.format(idx), 'cold{}'.format(idx))
        large_path = os.path.join(self.tmp.name, 'large.ftxd')
        SharedKeywordProcessor.publish(large, large_path)

        with SharedKeywordProcessor.open(small_path) as sp_small, \
                SharedKeywordProcessor.open(large_path) as sp_large:
            self.assertGreater(len(sp_large), len(sp_small) * 10)
            self.assertGreater(
                os.path.getsize(large_path),
                os.path.getsize(small_path) * 10)

            sp_small.reset_counters()
            sp_large.reset_counters()
            small_result = sp_small.extract_keywords(title)
            large_result = sp_large.extract_keywords(title)
            small_lookups = sp_small.node_lookups
            large_lookups = sp_large.node_lookups
            self.assertEqual(small_result, large_result)
            self.assertGreater(small_lookups, 0)
            self.assertEqual(large_lookups, small_lookups)

    def test_non_string_clean_name_is_rejected(self):
        kp = KeywordProcessor()
        kp.add_keyword('Taj Mahal', ('Monument', 'Taj Mahal'))
        path = os.path.join(self.tmp.name, 'tuple_alias.ftxd')
        with self.assertRaises(TypeError):
            SharedKeywordProcessor.publish(kp, path)
        self.assertFalse(os.path.exists(path))

    def test_empty_trie(self):
        path = os.path.join(self.tmp.name, 'empty.ftxd')
        SharedKeywordProcessor.publish(KeywordProcessor(), path)
        with SharedKeywordProcessor.open(path) as sp:
            self.assertEqual(len(sp), 0)
            self.assertEqual(sp.extract_keywords('nothing here'), [])
            self.assertEqual(
                sp.replace_keywords('nothing here'), 'nothing here')
            self.assertEqual(sp.get_all_keywords(), {})

    def test_invalid_file_and_unsupported_fuzzy(self):
        bad_path = os.path.join(self.tmp.name, 'bad.ftxd')
        with open(bad_path, 'wb') as f:
            f.write(b'not a flashtext file at all')
        with self.assertRaises(ValueError):
            SharedKeywordProcessor.open(bad_path)

        with SharedKeywordProcessor.open(self.dict_path) as sp:
            with self.assertRaises(ValueError):
                sp.extract_keywords('Big Apple', max_cost=1)
            with self.assertRaises(ValueError):
                sp.replace_keywords('Big Apple', max_cost=1)

    def test_other_process_opens_same_published_file(self):
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        sentence = 'Big Apple and Bay Area with NYC.'
        proc = ctx.Process(
            target=_mp_worker,
            args=(self.dict_path, sentence, queue))
        proc.start()
        proc.join(timeout=60)
        self.assertEqual(proc.exitcode, 0)
        result = queue.get(timeout=10)
        with SharedKeywordProcessor.open(self.dict_path) as sp:
            self.assertEqual(result['keywords'],
                             sp.extract_keywords(sentence))
            self.assertEqual(result['spans'],
                             sp.extract_keywords(sentence, span_info=True))
            self.assertEqual(result['replaced'],
                             sp.replace_keywords(sentence))
            self.assertEqual(result['length'], len(sp))
            self.assertEqual(result['case_sensitive'], sp.case_sensitive)


if __name__ == '__main__':
    unittest.main()
