# -*- coding: utf-8 -*-
"""端到端演示：词条清单 -> 逐步对照 -> 故障注入 -> 最短子序列最小化。

在仓库根目录运行：

    python -m state_audit.demo
"""

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


LINE = '=' * 72


def print_glossary():
    print(LINE)
    print('一、独立词条清单：规范名 / 词界 / 重叠 / 大小写')
    print(LINE)
    print('-- 规范实体 --')
    for name, note in CANONICAL_NAMES.items():
        print('  {:<12} {}'.format(name, note))
    print('-- 四个维度的规则 --')
    for axis, note in GLOSSARY_NOTES.items():
        print('  [{}] {}'.format(axis, note))
    print('-- 别名逐条解释 --')
    for alias, item in ALIAS_CATALOG.items():
        print('  {!r:<18} -> {:<12} [{}]'.format(
            alias, item['canonical'], '/'.join(item['axes'])))
        print('      {}'.format(item['note']))


def print_step_detail(snapshot, only_titles=None):
    edit = snapshot.edit
    print('  步骤 {:>2} {} {!r:<18} 当前词条数={}'.format(
        snapshot.step, edit[0].upper(), edit[1], len(snapshot.keywords)))
    for check in snapshot.title_checks:
        if only_titles is not None and check.title_index not in only_titles:
            continue
        spans = ['[{0[1]}:{0[2]})={0[0]}<-{0[3]!r}'.format(hit)
                 for hit in check.flashtext_hits]
        print('      标题{} {!r}'.format(check.title_index, check.title))
        print('        命中: {}'.format(' | '.join(spans) if spans else '(无)'))
        print('        替换: {!r}'.format(check.flashtext_replaced))
        if check.diffs:
            for diff in check.diffs:
                print('        !! [{}] {}'.format(diff.kind, diff.detail))


def demo_normal_audit():
    print(LINE)
    print('二、正常编辑序列回放：参考实现 vs FlashText 逐字段对照')
    print(LINE)
    snapshots = run_audit(EDIT_SEQUENCE, TITLES)
    total_diffs = sum(
        len(check.diffs)
        for snapshot in snapshots
        for check in snapshot.title_checks
    )
    print('  共 {} 步编辑 x {} 条标题，字段级差异总数 = {}'.format(
        len(snapshots), len(TITLES), total_diffs))
    assert total_diffs == 0, '正常序列不应出现差异'

    print('\n  -- 场景 A：长别名删掉后短别名重现（步骤 9~11，标题 0/1）--')
    for step in (8, 9, 10, 11):
        print_step_detail(snapshots[step], only_titles=(0, 1))

    print('\n  -- 场景 B：覆盖后多个别名汇到同一实体（步骤 12/13，标题 2/3/4）--')
    for step in (11, 12, 13):
        print_step_detail(snapshots[step], only_titles=(2, 3, 4))
    return snapshots


def demo_broken_prefix():
    print(LINE)
    print('三、可控故障一：删坏前缀（删除时砍掉整棵首字符子树）')
    print(LINE)
    factory = lambda seq: build_flashtext(  # noqa: E731
        seq, remove_fn=buggy_remove_break_prefix
    )
    snapshots = run_audit(EDIT_SEQUENCE, TITLES, processor_factory=factory)
    failing_step = first_failing_step(snapshots)
    print('  首个出错步骤 = {}，编辑 = {!r}'.format(
        failing_step, EDIT_SEQUENCE[failing_step]))
    target = None
    for check in snapshots[failing_step].title_checks:
        for diff in check.diffs:
            print('  标题{} [{}] {}'.format(check.title_index, diff.kind, diff.detail))
            if target is None:
                target = (check.title_index, diff.kind)

    def reproduces(run_snapshots):
        for snapshot in run_snapshots:
            for check in snapshot.title_checks:
                if check.title_index != target[0]:
                    continue
                if any(diff.kind == target[1] for diff in check.diffs):
                    return True
        return False

    minimal_edits, kept_indices = minimize_edits(
        EDIT_SEQUENCE, TITLES, case_sensitive=False,
        reproduces=reproduces, processor_factory=factory,
    )
    print('\n  最小化结果：保留原序列下标 {}，{} 条编辑 -> {} 条'.format(
        kept_indices, len(EDIT_SEQUENCE), len(minimal_edits)))
    for edit in minimal_edits:
        print('    {} {!r} -> {}'.format(edit[0], edit[1], edit[2]))
    verify = run_audit(minimal_edits, TITLES, processor_factory=factory)
    assert reproduces(verify), '最小子序列必须仍能复现同一错误'
    print('  验证：最小子序列仍在标题{} 复现 {}，故障被定位到“先加同前缀别名、再故障删除”。'.format(
        target[0], target[1]))
    return snapshots


def demo_offset_distortion():
    print(LINE)
    print('四、可控故障二：偏移失真（规范名正确，span 末尾整体 +1）')
    print(LINE)
    factory = lambda seq: build_offset_processor(seq)  # noqa: E731
    snapshots = run_audit(EDIT_SEQUENCE, TITLES, processor_factory=factory)
    target = None
    for snapshot in snapshots:
        for check in snapshot.title_checks:
            for diff in check.diffs:
                print('  步骤{} 标题{} [{}] {}'.format(
                    snapshot.step, check.title_index, diff.kind, diff.detail))
                if target is None:
                    target = (check.title_index, diff.kind)
                break
            if target is not None:
                break
        if target is not None:
            break

    # 对照：同一步骤里 clean 全部正确，只有 span 字段出错。
    sample = snapshots[5].title_checks[target[0]]
    wrong_kind = set(diff.kind for diff in sample.diffs)
    print('  该标题差异类型 = {}；命中规范名 = {}（实体本身没选错）'.format(
        wrong_kind, [hit.clean for hit in sample.flashtext_hits]))
    assert wrong_kind == {'span_mismatch'}

    def reproduces(run_snapshots):
        for snapshot in run_snapshots:
            for check in snapshot.title_checks:
                if check.title_index != target[0]:
                    continue
                if any(diff.kind == target[1] for diff in check.diffs):
                    return True
        return False

    minimal_edits, kept_indices = minimize_edits(
        EDIT_SEQUENCE, TITLES, case_sensitive=False,
        reproduces=reproduces, processor_factory=factory,
    )
    print('\n  最小化结果：保留原序列下标 {}，缩到 {} 条编辑'.format(
        kept_indices, len(minimal_edits)))
    for edit in minimal_edits:
        print('    {} {!r} -> {}'.format(edit[0], edit[1], edit[2]))
    print('  含义：偏移失真与编辑历史无关，只要有一条能命中的别名就暴露；')
    print('        而删坏前缀必须依赖 add+remove 组合，两类故障由此被对照分开。')
    return snapshots


def build_offset_processor(edits):
    processor = OffsetDistortingProcessor(span_shift=1)
    for action, alias, clean_name in edits:
        if action == 'add':
            processor.add_keyword(alias, clean_name)
        elif action == 'remove':
            processor.remove_keyword(alias)
    return processor


def demo_case_sensitive():
    print(LINE)
    print('五、大小写模式对照（标题 4：IPHONE / iphone / iPhone）')
    print(LINE)
    title = TITLES[4]
    insensitive = run_audit(
        EDIT_SEQUENCE[:13], [title], case_sensitive=False
    )[-1].title_checks[0]
    sensitive = run_audit(
        EDIT_SEQUENCE[:13], [title], case_sensitive=True
    )[-1].title_checks[0]
    print('  不敏感命中: {}'.format(
        [(h.start, h.end, h.clean) for h in insensitive.flashtext_hits]))
    print('  敏感模式命中: {}'.format(
        [(h.start, h.end, h.clean) for h in sensitive.flashtext_hits]))
    print('  参考实现与 FlashText 在两种模式下差异数: {} / {}'.format(
        len(insensitive.diffs), len(sensitive.diffs)))


def main():
    print_glossary()
    demo_normal_audit()
    demo_broken_prefix()
    demo_offset_distortion()
    demo_case_sensitive()
    print(LINE)
    print('全部演示结束。')
    print(LINE)


if __name__ == '__main__':
    main()
