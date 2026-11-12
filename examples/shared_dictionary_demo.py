"""Countable read example for a shared, memory-mapped FlashText dictionary.

Run with::

    python examples/shared_dictionary_demo.py

The script publishes two dictionary versions from one KeywordProcessor
configuration: a small one and one that is more than ten times larger.  Both
versions contain the same queryable prefixes; the extra bulk of the large one
lives in branches that the short title queries never visit.

Each reader memory-maps its published file read-only and counts the compact
trie nodes it actually enters (``SharedKeywordProcessor.node_lookups``).
Because the mapping is shared page cache and no Python trie is rebuilt, the
reader only touches the branches the query walks:

* the lookup count for the same title is identical in both dictionaries even
  though the large file is >10x the size;
* the count changes when a different title walks different branches.
"""

import os
import tempfile

from flashtext import KeywordProcessor, SharedKeywordProcessor


HOT_PREFIXES = [
    ('apple', 'Apple Inc'),
    ('iphone', 'iPhone'),
    ('ipad', 'iPad'),
]
TITLES = [
    'apple unveils the new iphone',
    'ipad sales beat the iphone',
    'zebra migration patterns explained',
]


def build_processor(cold_terms):
    processor = KeywordProcessor(case_sensitive=False)
    for keyword, clean_name in HOT_PREFIXES:
        processor.add_keyword(keyword, clean_name)
    for idx in range(cold_terms):
        processor.add_keyword('zebra{:05d}'.format(idx),
                              'cold-term-{}'.format(idx))
    return processor


def main():
    workdir = tempfile.mkdtemp(prefix='flashtext-shared-')
    small_path = os.path.join(workdir, 'small.ftxd')
    large_path = os.path.join(workdir, 'large.ftxd')

    small = build_processor(cold_terms=200)
    large = build_processor(cold_terms=4000)
    SharedKeywordProcessor.publish(small, small_path)
    SharedKeywordProcessor.publish(large, large_path)

    small_size = os.path.getsize(small_path)
    large_size = os.path.getsize(large_path)
    print('Published dictionaries:')
    print('  small: {:6d} terms, {:7d} bytes ({})'.format(
        len(small), small_size, small_path))
    print('  large: {:6d} terms, {:7d} bytes ({})'.format(
        len(large), large_size, large_path))
    print('  size ratio: {:.1f}x'.format(large_size / float(small_size)))
    print()

    with SharedKeywordProcessor.open(small_path) as small_reader, \
            SharedKeywordProcessor.open(large_path) as large_reader:
        for title in TITLES:
            small_reader.reset_counters()
            large_reader.reset_counters()
            small_hits = small_reader.extract_keywords(title)
            large_hits = large_reader.extract_keywords(title)
            print('title: {!r}'.format(title))
            print('  hits          : {}'.format(small_hits))
            assert small_hits == large_hits
            print('  nodes visited : small={} large={}'.format(
                small_reader.node_lookups, large_reader.node_lookups))
            assert small_reader.node_lookups == large_reader.node_lookups, (
                'a 10x larger dictionary must not add private work for an '
                'unchanged prefix')
            print()

    # Version pinning: a reader keeps the dictionary it opened even after a
    # newer version is published, so an in-flight batch finishes on the old
    # rules (case handling, aliases and word boundaries included).
    v1_path = os.path.join(workdir, 'v1.ftxd')
    v2_path = os.path.join(workdir, 'v2.ftxd')
    v1 = KeywordProcessor()
    v1.add_keyword('apple', 'fruit')
    SharedKeywordProcessor.publish(v1, v1_path)

    old_batch_reader = SharedKeywordProcessor.open(v1_path)
    v2 = KeywordProcessor(case_sensitive=True)
    v2.add_keyword('apple', 'company')
    SharedKeywordProcessor.publish(v2, v2_path)

    print('Version pinning while a new dictionary is published:')
    print('  old batch reader : {}'.format(
        old_batch_reader.extract_keywords('Apple')))
    with SharedKeywordProcessor.open(v2_path) as new_reader:
        print('  new batch reader : {} (case_sensitive={})'.format(
            new_reader.extract_keywords('Apple'),
            new_reader.case_sensitive))
    old_batch_reader.close()


if __name__ == '__main__':
    main()
