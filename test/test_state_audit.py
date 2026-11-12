# -*- coding: utf-8 -*-
"""状态对照框架的测试。

覆盖：
* 独立参考实现与 FlashText 在整条编辑序列上逐字段一致；
* 长别名删除后短别名重现、覆盖后多别名汇聚两种关键变化；
* 词界、重叠、大小写的清单规则；
* 两类可控故障（删坏前缀 / 偏移失真）被分别检出且分类互不混淆；
* 差异最小化能缩到仍复现同一错误的最短编辑子序列。
"""

import unittest

from state_audit.glossary_data import (
    ALIAS_CATALOG,
    CANONICAL_NAMES,
    EDIT_SEQUENCE,
    GLOSSARY_NOTES,
    TITLES,
)
from state_audit.auditor import (
    build_flashtext,
    buggy_remove_break_prefix,
    first_failing_step,
    minimize_edits,
    run_audit,
    OffsetDistortingProcessor,
)
from state_audit.reference_matcher import (
    ReferenceMatcher,
    boundary_explanation,
)


def all_diffs(snapshots):
    return [
        (snapshot.step, check.title_index, diff)
        for snapshot in snapshots
        for check in snapshot.title_checks
        for diff in check.diffs
    ]


def diff_kinds(snapshots):
    return set(diff.kind for _, _, diff in all_diffs(snapshots))


def snapshot_at(snapshots, step):
    return snapshots[step]


def hits_on_title(snapshot, title_index):
    return snapshot.title_checks[title_index].flashtext_hits


class TestGlossaryCatalog(unittest.TestCase):
    def test_every_edit_alias_is_documented(self):
        documented = set(ALIAS_CATALOG)
        edited = set(alias for _, alias, _ in EDIT_SEQUENCE)
        self.assertEqual(edited, documented)

    def test_canonical_and_axes_valid(self):
        valid_axes = set(GLOSSARY_NOTES)
        for alias, item in ALIAS_CATALOG.items():
            self.assertTrue(item['note'])
            self.assertTrue(set(item['axes']) <= valid_axes)
            self.assertIn(item['canonical'].split(' ')[0], CANONICAL_NAMES)


class TestReferenceAgainstFlashText(unittest.TestCase):
    def test_zero_diffs_across_whole_sequence(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES)
        self.assertEqual(len(snapshots), len(EDIT_SEQUENCE))
        self.assertEqual(all_diffs(snapshots), [])
        for snapshot in snapshots:
            for check in snapshot.title_checks:
                self.assertFalse(check.replacement_differs)
                for hit in check.flashtext_hits:
                    self.assertEqual(hit.matched_text, check.title[hit.start:hit.end])
                self.assertEqual(
                    check.reference_hits, check.flashtext_hits,
                    '步骤{} 标题{} 实体区间不一致'.format(snapshot.step, check.title_index),
                )

    def test_glued_alias_does_not_hit(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES)
        # 标题 5：'myiphonex is not an iphone'，只有词界上的 iphone 命中。
        for snapshot in snapshots[5:12]:
            hits = hits_on_title(snapshot, 5)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].matched_text, 'iphone')
            self.assertEqual(hits[0].start, 20)

    def test_long_alias_removed_short_alias_reappears(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES)

        # 步骤 9：最长别名 pro 被删，标题 1 回退到 'red apple x1'。
        hits = hits_on_title(snapshot_at(snapshots, 9), 1)
        self.assertEqual([(h.matched_text, h.clean) for h in hits],
                         [('red apple x1', 'RED-APPLE')])

        # 步骤 10：x1 也被删，标题 1 同一起点 0 回退到短别名 'red'；
        # 另外 x1 后面的独立词 'apple' 仍按自己的别名命中。
        hits = hits_on_title(snapshot_at(snapshots, 10), 1)
        self.assertEqual([(h.matched_text, h.clean) for h in hits],
                         [('red', 'RED-APPLE'), ('apple', 'APPLE-PHONE')])
        self.assertEqual(hits[0].start, 0)

        # 同一步标题 0：起点 0 和第二个 red 由短别名接管，
        # 中间独立词 apple 照常命中自己的别名。
        hits = hits_on_title(snapshot_at(snapshots, 10), 0)
        self.assertEqual([h.matched_text for h in hits], ['red', 'apple', 'red'])
        self.assertEqual([h.clean for h in hits],
                         ['RED-APPLE', 'APPLE-PHONE', 'RED-APPLE'])

        # 步骤 11：短别名也删掉，red 系列实体整体消失（apple 别名与此无关仍在）。
        for title_index in (0, 1):
            self.assertFalse(
                any(h.clean == 'RED-APPLE'
                    for h in hits_on_title(snapshot_at(snapshots, 11), title_index))
            )

    def test_overwrite_converges_aliases_to_one_entity(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES)

        # 覆盖前（步骤 11）：iphone 8 汇到旧规范名 IPHONE-8。
        before = hits_on_title(snapshot_at(snapshots, 11), 2)
        self.assertIn(('iphone 8', 'IPHONE-8'),
                      [(h.matched_text, h.clean) for h in before])

        # 覆盖后（步骤 13）：iphone 与 iphone 8 都汇到 APPLE-PHONE。
        after = hits_on_title(snapshot_at(snapshots, 13), 2)
        iphone_hits = [(h.matched_text, h.clean) for h in after
                       if h.matched_text in ('iphone', 'iphone 8')]
        self.assertEqual(iphone_hits,
                         [('iphone 8', 'APPLE-PHONE')])
        title3 = hits_on_title(snapshot_at(snapshots, 13), 3)
        self.assertTrue(all(hit.clean == 'APPLE-PHONE'
                            for hit in title3 if 'iphone' in hit.matched_text.lower()
                            or 'apple' in hit.matched_text.lower()))

        # 覆盖不增加词条数：同别名再次 add 只改映射。
        self.assertEqual(
            len(snapshot_at(snapshots, 12).keywords),
            len(snapshot_at(snapshots, 11).keywords),
        )

    def test_overlap_longest_wins(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES)
        # 步骤 8 时 red 系列三个别名都在，标题 1 起点 0 必须取最长的 pro。
        hits = hits_on_title(snapshot_at(snapshots, 8), 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].matched_text, 'red apple x1 pro')

    def test_case_sensitive_mode(self):
        title = TITLES[4]
        insensitive = run_audit(EDIT_SEQUENCE[:13], [title],
                                case_sensitive=False)[-1].title_checks[0]
        sensitive = run_audit(EDIT_SEQUENCE[:13], [title],
                              case_sensitive=True)[-1].title_checks[0]
        self.assertEqual(len(insensitive.flashtext_hits), 3)
        # 敏感模式下清单里只有小写别名，全大写的 IPHONE 不命中。
        self.assertTrue(all(h.matched_text != 'IPHONE'
                            for h in sensitive.flashtext_hits))
        self.assertEqual(insensitive.diffs, [])
        self.assertEqual(sensitive.diffs, [])

    def test_boundary_explanation_helper(self):
        text = 'myiphonex iphone'
        glued = boundary_explanation(text, 2, 8)
        self.assertTrue(any('粘连不命中' in reason for reason in glued))
        anchored = boundary_explanation(text, 10, 16)
        self.assertTrue(any('词界字符' in reason or '句' in reason
                            for reason in anchored))


class TestControlledFaults(unittest.TestCase):
    def _prefix_factory(self):
        return lambda seq: build_flashtext(
            seq, remove_fn=buggy_remove_break_prefix)

    def _offset_factory(self):
        def factory(seq):
            processor = OffsetDistortingProcessor(span_shift=1)
            for action, alias, clean_name in seq:
                if action == 'add':
                    processor.add_keyword(alias, clean_name)
                elif action == 'remove':
                    processor.remove_keyword(alias)
            return processor
        return factory

    def test_broken_prefix_is_detected(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES,
                              processor_factory=self._prefix_factory())
        self.assertIsNotNone(first_failing_step(snapshots))
        kinds = diff_kinds(snapshots)
        # 删坏前缀影响的是“选哪个实体/是否缺失”，不会造成 span 偏移类差异。
        self.assertIn('missing_entity', kinds)
        self.assertNotIn('span_mismatch', kinds)
        self.assertTrue(any(check.replacement_differs
                            for snapshot in snapshots
                            for check in snapshot.title_checks))

    def test_offset_distortion_is_detected(self):
        snapshots = run_audit(EDIT_SEQUENCE, TITLES,
                              processor_factory=self._offset_factory())
        kinds = diff_kinds(snapshots)
        self.assertEqual(kinds, {'span_mismatch'})
        # 替换串由 replace_keywords 产生，未被污染，所以替换结果仍然一致。
        self.assertFalse(any(check.replacement_differs
                             for snapshot in snapshots
                             for check in snapshot.title_checks))
        # 规范名本身没选错，错的只是区间。
        for snapshot in snapshots:
            for check in snapshot.title_checks:
                ref_cleans = [h.clean for h in check.reference_hits]
                ft_cleans = [h.clean for h in check.flashtext_hits]
                self.assertEqual(ref_cleans, ft_cleans)

    def test_two_faults_are_separable(self):
        prefix_kinds = diff_kinds(
            run_audit(EDIT_SEQUENCE, TITLES,
                      processor_factory=self._prefix_factory()))
        offset_kinds = diff_kinds(
            run_audit(EDIT_SEQUENCE, TITLES,
                      processor_factory=self._offset_factory()))
        self.assertEqual(prefix_kinds & offset_kinds, set())

    def test_minimize_broken_prefix_subsequence(self):
        factory = self._prefix_factory()
        snapshots = run_audit(EDIT_SEQUENCE, TITLES, processor_factory=factory)
        step, title_index, first_diff = all_diffs(snapshots)[0]
        target_kind = first_diff.kind

        def reproduces(run_snapshots):
            return any(
                diff.kind == target_kind and check.title_index == title_index
                for snapshot in run_snapshots
                for check in snapshot.title_checks
                for diff in check.diffs
            )

        minimal_edits, kept_indices = minimize_edits(
            EDIT_SEQUENCE, TITLES, case_sensitive=False,
            reproduces=reproduces, processor_factory=factory,
        )
        self.assertLess(len(minimal_edits), len(EDIT_SEQUENCE))
        self.assertTrue(any(edit[0] == 'add' for edit in minimal_edits))
        self.assertTrue(any(edit[0] == 'remove' for edit in minimal_edits))
        verify = run_audit(minimal_edits, TITLES, processor_factory=factory)
        self.assertTrue(reproduces(verify))
        # 最小子序列的下标都能在原序列里找到。
        self.assertEqual([EDIT_SEQUENCE[i] for i in kept_indices], minimal_edits)

    def test_minimize_offset_subsequence_is_single_add(self):
        factory = self._offset_factory()
        snapshots = run_audit(EDIT_SEQUENCE, TITLES, processor_factory=factory)
        _, title_index, first_diff = all_diffs(snapshots)[0]
        target_kind = first_diff.kind

        def reproduces(run_snapshots):
            return any(
                diff.kind == target_kind and check.title_index == title_index
                for snapshot in run_snapshots
                for check in snapshot.title_checks
                for diff in check.diffs
            )

        minimal_edits, _ = minimize_edits(
            EDIT_SEQUENCE, TITLES, case_sensitive=False,
            reproduces=reproduces, processor_factory=factory,
        )
        # 偏移失真与编辑历史无关：一条能命中的 add 就足以暴露。
        self.assertEqual(len(minimal_edits), 1)
        self.assertEqual(minimal_edits[0][0], 'add')
        verify = run_audit(minimal_edits, TITLES, processor_factory=factory)
        self.assertTrue(reproduces(verify))


class TestReferenceMatcherDirect(unittest.TestCase):
    def test_replace_preserves_surrounding_original_text(self):
        matcher = ReferenceMatcher()
        matcher.add('iphone', 'APPLE-PHONE')
        text = 'Buy an iPhone now!'
        self.assertEqual(matcher.replace(text), 'Buy an APPLE-PHONE now!')
        hit = matcher.find_hits(text)[0]
        self.assertEqual((hit.start, hit.end, hit.matched_text), (7, 13, 'iPhone'))

    def test_remove_then_re_add(self):
        matcher = ReferenceMatcher()
        matcher.apply_edits([
            ('add', 'red apple x1', 'RED-APPLE'),
            ('add', 'red', 'RED-APPLE'),
            ('remove', 'red apple x1', None),
        ])
        hits = matcher.find_hits('red apple x1')
        self.assertEqual([(h.matched_text, h.clean) for h in hits],
                         [('red', 'RED-APPLE')])
        matcher.add('red apple x1', 'RED-APPLE')
        hits = matcher.find_hits('red apple x1')
        self.assertEqual(hits[0].matched_text, 'red apple x1')


if __name__ == '__main__':
    unittest.main()
