#!/usr/bin/env python

import copy
import itertools
import json
import unittest

from jsonmerge import (Conflict, DuplicateBatchError, InvalidBatch,
                       PatchMerger, merge_batches)


def conflicts_dict(merger):
    return [conflict.to_dict() for conflict in merger.conflicts]


def signature(merger):
    return (
        json.dumps(merger.document, sort_keys=True),
        json.dumps(conflicts_dict(merger), sort_keys=True),
        json.dumps(merger.canonical_patch, sort_keys=True),
    )


class ArrayTrackingTests(unittest.TestCase):

    def test_equal_baseline_members_are_distinct_rows(self):
        merger = PatchMerger([1, 1, 1])
        merger.receive({'id': 'a', 'operations': [
            {'op': 'remove', 'path': '/1'}]})
        self.assertEqual(merger.document, [1, 1])

    def test_concurrent_inserts_into_same_gap_order_by_batch_then_seq(self):
        merger = PatchMerger({'rows': ['x', 'y']})
        merger.receive({'id': 'p3', 'operations': [
            {'op': 'add', 'path': '/rows/1', 'value': 'c', 'seq': 7}]})
        merger.receive({'id': 'p1', 'operations': [
            {'op': 'add', 'path': '/rows/1', 'value': 'a', 'seq': 5}]})
        merger.receive({'id': 'p2', 'operations': [
            {'op': 'add', 'path': '/rows/1', 'value': 'b', 'seq': 2}]})
        self.assertEqual(merger.document,
                         {'rows': ['x', 'a', 'b', 'c', 'y']})

    def test_sequence_orders_inserts_within_one_batch(self):
        merger = PatchMerger([0])
        merger.receive({'id': 'a', 'operations': [
            {'op': 'add', 'path': '/0', 'value': 1, 'seq': 0},
            {'op': 'add', 'path': '/0', 'value': 2, 'seq': 1},
            {'op': 'add', 'path': '/0', 'value': 3, 'seq': 2}]})
        self.assertEqual(merger.document, [3, 2, 1, 0])

    def test_new_member_can_be_moved_copied_and_referenced(self):
        merger = PatchMerger({'items': [1]})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'add', 'path': '/items/-', 'value': 9, 'seq': 0},
            {'op': 'move', 'from': '/items/1', 'path': '/items/0', 'seq': 1},
            {'op': 'copy', 'from': '/items/0', 'path': '/items/-', 'seq': 2}]})
        self.assertEqual(merger.document, {'items': [9, 1, 9]})

    def test_indices_use_author_view_under_concurrent_shifts(self):
        batches = (
            {'id': 'a', 'operations': [{'op': 'remove', 'path': '/0'}]},
            {'id': 'b', 'operations': [
                {'op': 'replace', 'path': '/1', 'value': 99}]},
        )
        docs = set()
        for order in itertools.permutations(batches):
            merger = PatchMerger([10, 20, 30])
            merger.receive_many(order)
            docs.add(json.dumps(merger.document))
        # b's index 1 resolves against its own baseline view to the original
        # second member even though a concurrently removed the first one
        self.assertEqual(docs, {'[99, 30]'})


class ConflictTests(unittest.TestCase):

    def test_concurrent_write_conflicts_but_independent_row_commits(self):
        merger = PatchMerger({'a': 1, 'b': 2})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'replace', 'path': '/a', 'value': 10}]})
        merger.receive({'id': 'b', 'operations': [
            {'op': 'replace', 'path': '/a', 'value': 20},
            {'op': 'replace', 'path': '/b', 'value': 20}]})
        self.assertEqual(merger.document, {'a': 1, 'b': 20})
        self.assertEqual(len(merger.conflicts), 1)
        self.assertEqual(merger.conflicts[0].reason, Conflict.WRITE)
        self.assertEqual(
            sorted(op['batch'] for op in merger.conflicts[0].operations),
            ['a', 'b'])

    def test_stale_copy_is_detected_in_either_receive_order(self):
        results = set()
        for order in itertools.permutations(['a', 'b']):
            merger = PatchMerger({'x': 1})
            for batch_id in order:
                if batch_id == 'a':
                    merger.receive({'id': 'a', 'operations': [
                        {'op': 'copy', 'from': '/x', 'path': '/y'}]})
                else:
                    merger.receive({'id': 'b', 'operations': [
                        {'op': 'replace', 'path': '/x', 'value': 2}]})
            results.add(signature(merger))
        self.assertEqual(len(results), 1)
        conflict = merger.conflicts[0]
        self.assertEqual(conflict.reason, Conflict.STALE_COPY)
        self.assertEqual(
            sorted(op['batch'] for op in conflict.operations), ['a', 'b'])
        # both sides are withheld, so the document stays at the baseline
        self.assertEqual(merger.document, {'x': 1})

    def test_move_into_removed_ancestor_keeps_both_sides(self):
        results = set()
        for order in itertools.permutations(['a', 'b']):
            merger = PatchMerger({'dir': {'sub': {'f': 1}}})
            for batch_id in order:
                if batch_id == 'a':
                    merger.receive({'id': 'a', 'operations': [
                        {'op': 'move', 'from': '/dir/sub/f',
                         'path': '/dir/sub/g'}]})
                else:
                    merger.receive({'id': 'b', 'operations': [
                        {'op': 'remove', 'path': '/dir/sub'}]})
            results.add(signature(merger))
        self.assertEqual(len(results), 1)
        self.assertEqual(merger.document, {'dir': {'sub': {'f': 1}}})
        self.assertEqual(merger.conflicts[0].reason,
                         Conflict.MOVED_INTO_REMOVED)

    def test_dependent_waits_while_unrelated_follow_up_applies(self):
        merger = PatchMerger({'n': 1})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'replace', 'path': '/n', 'value': 99}]})
        merger.receive({'id': 'b', 'operations': [
            {'op': 'replace', 'path': '/n', 'value': 2}]})
        merger.receive({'id': 'c', 'parents': ['b'], 'operations': [
            {'op': 'copy', 'from': '/n', 'path': '/m', 'seq': 0},
            {'op': 'add', 'path': '/q', 'value': 7, 'seq': 1}]})
        self.assertEqual(merger.document, {'n': 1, 'q': 7})
        conflict = merger.conflicts[0]
        self.assertEqual(conflict.reason, Conflict.WRITE)
        self.assertEqual(
            [(d['batch'], d['sequence']) for d in conflict.dependents],
            [('c', 0)])

    def test_failed_test_is_a_conflict(self):
        merger = PatchMerger({'n': 1})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'test', 'path': '/n', 'value': 2}]})
        self.assertEqual(merger.document, {'n': 1})
        self.assertEqual(merger.conflicts[0].reason, Conflict.TEST_FAILED)

    def test_passing_test_allows_the_rest_of_the_batch(self):
        merger = PatchMerger({'n': 1})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'test', 'path': '/n', 'value': 1},
            {'op': 'replace', 'path': '/n', 'value': 5}]})
        self.assertEqual(merger.document, {'n': 5})
        self.assertEqual(merger.conflicts, [])

    def test_missing_location_is_reported(self):
        merger = PatchMerger({'a': 1})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'remove', 'path': '/b'}]})
        self.assertEqual(merger.conflicts[0].reason,
                         Conflict.MISSING_LOCATION)

    def test_write_and_remove_of_same_row_conflict_in_either_order(self):
        results = set()
        for order in itertools.permutations(['a', 'b']):
            merger = PatchMerger([10, 20, 30])
            for batch_id in order:
                if batch_id == 'a':
                    merger.receive({'id': 'a', 'operations': [
                        {'op': 'replace', 'path': '/2', 'value': 99}]})
                else:
                    merger.receive({'id': 'b', 'operations': [
                        {'op': 'remove', 'path': '/2'}]})
            results.add(signature(merger))
        self.assertEqual(len(results), 1)
        self.assertEqual(merger.document, [10, 20, 30])
        self.assertEqual(merger.conflicts[0].reason, Conflict.WRITE)

    def test_copy_of_source_inside_removed_subtree_conflicts(self):
        results = set()
        for order in itertools.permutations(['a', 'b']):
            merger = PatchMerger({'d': {'x': 1}})
            for batch_id in order:
                if batch_id == 'a':
                    merger.receive({'id': 'a', 'operations': [
                        {'op': 'copy', 'from': '/d/x', 'path': '/y'}]})
                else:
                    merger.receive({'id': 'b', 'operations': [
                        {'op': 'remove', 'path': '/d'}]})
            results.add(signature(merger))
        self.assertEqual(len(results), 1)
        self.assertEqual(merger.document, {'d': {'x': 1}})
        self.assertEqual(merger.conflicts[0].reason, Conflict.STALE_COPY)

    def test_concurrent_reorder_does_not_shift_author_indices(self):
        results = set()
        for order in itertools.permutations(['a', 'b']):
            merger = PatchMerger([10, 20, 30])
            for batch_id in order:
                if batch_id == 'a':
                    merger.receive({'id': 'a', 'operations': [
                        {'op': 'move', 'from': '/2', 'path': '/0'}]})
                else:
                    merger.receive({'id': 'b', 'operations': [
                        {'op': 'replace', 'path': '/0', 'value': 99}]})
            results.add(signature(merger))
        self.assertEqual(len(results), 1)

    def test_root_replace_is_supported(self):
        merger = PatchMerger({'a': 1})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'replace', 'path': '', 'value': [1, 2]}]})
        self.assertEqual(merger.document, [1, 2])
        self.assertEqual(merger.conflicts, [])
        self.assertEqual(merger.replay_canonical(), [1, 2])

    def test_descendant_of_a_conflicting_writer_waits_as_dependent(self):
        merger = PatchMerger({'v': 0})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'replace', 'path': '/v', 'value': 1}]})
        merger.receive({'id': 'b', 'operations': [
            {'op': 'replace', 'path': '/v', 'value': 2}]})
        merger.receive({'id': 'c', 'parents': ['a'], 'operations': [
            {'op': 'replace', 'path': '/v', 'value': 3}]})
        conflict = merger.conflicts[0]
        self.assertEqual(
            sorted(op['batch'] for op in conflict.operations), ['a', 'b'])
        self.assertEqual([d['batch'] for d in conflict.dependents], ['c'])
        self.assertEqual(merger.document, {'v': 0})


class BatchGraphTests(unittest.TestCase):

    def test_duplicate_batch_is_deduplicated(self):
        merger = PatchMerger({})
        batch = {'id': 'a', 'operations': [
            {'op': 'add', 'path': '/k', 'value': 1}]}
        merger.receive(batch)
        merger.receive(copy.deepcopy(batch))
        merger.merge()
        self.assertEqual(merger.document, {'k': 1})

    def test_same_id_with_different_content_raises(self):
        merger = PatchMerger({})
        merger.receive({'id': 'a', 'operations': [
            {'op': 'add', 'path': '/k', 'value': 1}]})
        with self.assertRaises(DuplicateBatchError):
            merger.receive({'id': 'a', 'operations': [
                {'op': 'add', 'path': '/k', 'value': 2}]})

    def test_parent_may_arrive_later(self):
        merger = PatchMerger({'v': 0})
        merger.receive({'id': 'b', 'parents': ['a'], 'operations': [
            {'op': 'replace', 'path': '/v', 'value': 2}]})
        self.assertTrue(any(c.reason == Conflict.UNKNOWN_PARENT
                            for c in merger.merge().conflicts))
        merger.receive({'id': 'a', 'operations': [
            {'op': 'replace', 'path': '/v', 'value': 1}]})
        result = merger.merge()
        self.assertEqual(result.document, {'v': 2})
        self.assertEqual(result.conflicts, [])
        self.assertEqual(result.batch_order, ['a', 'b'])

    def test_causal_chain_applies_in_order(self):
        batches = [
            {'id': 'c', 'parents': ['b'], 'operations': [
                {'op': 'replace', 'path': '/v', 'value': 3}]},
            {'id': 'b', 'parents': ['a'], 'operations': [
                {'op': 'replace', 'path': '/v', 'value': 2}]},
            {'id': 'a', 'operations': [
                {'op': 'replace', 'path': '/v', 'value': 1}]},
        ]
        signatures = set()
        for order in itertools.permutations(batches):
            merger = PatchMerger({'v': 0})
            merger.receive_many(order)
            signatures.add(signature(merger.merge()))
        self.assertEqual(len(signatures), 1)
        self.assertEqual(merger.document, {'v': 3})

    def test_causal_cycle_is_reported(self):
        merger = PatchMerger({})
        merger.receive({'id': 'a', 'parents': ['b'], 'operations': [
            {'op': 'add', 'path': '/x', 'value': 1}]})
        merger.receive({'id': 'b', 'parents': ['a'], 'operations': [
            {'op': 'add', 'path': '/y', 'value': 2}]})
        self.assertTrue(any(c.reason == Conflict.CAUSAL_CYCLE
                            for c in merger.conflicts))

    def test_invalid_batches_are_rejected(self):
        merger = PatchMerger({})
        with self.assertRaises(InvalidBatch):
            merger.receive({'operations': []})
        with self.assertRaises(InvalidBatch):
            merger.receive({'id': 'a', 'operations': [
                {'op': 'frobnicate', 'path': '/x'}]})
        with self.assertRaises(InvalidBatch):
            merger.receive({'id': 'a', 'operations': [
                {'op': 'add', 'path': '/x', 'value': 1, 'seq': 0},
                {'op': 'add', 'path': '/y', 'value': 2, 'seq': 0}]})


class ConvergenceTests(unittest.TestCase):

    def _batches(self):
        return [
            {'id': 'a', 'operations': [
                {'op': 'replace', 'path': '/v', 'value': 1}]},
            {'id': 'b', 'operations': [
                {'op': 'add', 'path': '/w', 'value': 2}]},
            {'id': 'c', 'operations': [
                {'op': 'replace', 'path': '/v', 'value': 3}]},
        ]

    def test_every_receive_order_converges(self):
        signatures = set()
        for order in itertools.permutations(self._batches()):
            merger = PatchMerger({'v': 0})
            merger.receive_many(order)
            merger.merge()
            signatures.add(signature(merger))
        self.assertEqual(len(signatures), 1)

    def test_canonical_patch_replays_against_baseline(self):
        merger = PatchMerger({'v': 0})
        merger.receive_many(self._batches())
        merger.merge()
        self.assertEqual(merger.replay_canonical(), merger.document)

    def test_separate_mergers_joined_with_absorb_converge(self):
        batches = self._batches()
        left = PatchMerger({'v': 0})
        left.receive_many(batches[:2])
        left.merge()
        right = PatchMerger({'v': 0})
        right.receive_many(batches[1:])
        right.merge()
        left.absorb(right)
        joined = left.merge()

        single = PatchMerger({'v': 0})
        single.receive_many(batches)
        single.merge()
        self.assertEqual(signature(joined), signature(single))

    def test_absorb_rejects_different_baselines_and_duplicate_ids(self):
        left = PatchMerger({'v': 0})
        with self.assertRaises(InvalidBatch):
            left.absorb(PatchMerger({'v': 1}))

        first = PatchMerger({'v': 0})
        first.receive({'id': 'b', 'operations': [
            {'op': 'add', 'path': '/w', 'value': 2}]})
        other = PatchMerger({'v': 0})
        other.receive({'id': 'b', 'operations': [
            {'op': 'add', 'path': '/w', 'value': 9}]})
        with self.assertRaises(DuplicateBatchError):
            first.absorb(other)

    def test_merge_batches_helper(self):
        result = merge_batches([1], [
            {'id': 'a', 'operations': [
                {'op': 'add', 'path': '/-', 'value': 2}]},
            {'id': 'b', 'operations': [
                {'op': 'add', 'path': '/-', 'value': 3}]}])
        self.assertEqual(result.document, [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
