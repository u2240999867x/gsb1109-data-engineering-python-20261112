# -*- coding: utf-8 -*-
"""FlashText 词典编辑状态对照框架。

三件事：

1. 回放：按编辑序列逐步 add/remove，每一步同时驱动 FlashText 和
   ``ReferenceMatcher``，对每个标题核对每个实体的原文区间与替换串；
2. 分类：把差异分成实体选错（clean/缺失/多余）与区间失真（span 偏移）两类；
3. 最小化：发现差异后，用贪心缩减找到仍会选错同一个实体的最短编辑子序列。

另外提供两类可控故障，用来证明对照确实能区分故障形态：

* :func:`buggy_remove_break_prefix`：删别名时把共享前缀整棵子树删掉
  （删坏前缀），表现为同前缀的短别名一起丢失、实体选错或缺失；
* :class:`OffsetDistortingProcessor`：包装 FlashText 但把 span 末尾统一偏移，
  （偏移失真），规范名仍对、区间原文对不上。
"""

from collections import namedtuple

from flashtext import KeywordProcessor

from state_audit.reference_matcher import ReferenceMatcher


# 一次命中的标准化记录：clean 是替换串，start/end 是原文半开区间，
# matched_text 直接从原标题切片，用来证明区间确实指向那段原文。
HitRecord = namedtuple('HitRecord', ['clean', 'start', 'end', 'matched_text'])

# 单条字段级差异。kind 取值：
# missing_entity / extra_entity / clean_mismatch / span_mismatch
FieldDiff = namedtuple('FieldDiff', ['kind', 'detail'])

# 某个标题在某一步的对照结果。
TitleCheck = namedtuple(
    'TitleCheck',
    ['title_index', 'title', 'reference_hits', 'flashtext_hits',
     'diffs', 'reference_replaced', 'flashtext_replaced', 'replacement_differs']
)

# 某一步编辑后的完整快照。
StepSnapshot = namedtuple(
    'StepSnapshot',
    ['step', 'edit', 'keywords', 'title_checks']
)


def flashtext_hits(processor, title):
    """把 FlashText 的 span 输出标准化成 HitRecord（原文切片取自原标题）。"""
    records = []
    for clean, start, end in processor.extract_keywords(title, span_info=True):
        records.append(HitRecord(clean, start, end, title[start:end]))
    return records


def reference_hits(matcher, title):
    return [
        HitRecord(hit.clean, hit.start, hit.end, hit.matched_text)
        for hit in matcher.find_hits(title)
    ]


def compare_title(title_index, title, matcher, processor):
    """逐字段对照同一标题在参考实现与 FlashText 下的实体区间。"""
    ref_records = reference_hits(matcher, title)
    ft_records = flashtext_hits(processor, title)
    diffs = []

    ref_by_start = dict((record.start, record) for record in ref_records)
    ft_by_start = dict((record.start, record) for record in ft_records)

    for start in sorted(set(ref_by_start) | set(ft_by_start)):
        ref_record = ref_by_start.get(start)
        ft_record = ft_by_start.get(start)
        if ref_record is None:
            diffs.append(FieldDiff(
                'extra_entity',
                '起点 {} FlashText 多选出 {}（参考实现无此实体）'.format(start, ft_record)
            ))
            continue
        if ft_record is None:
            diffs.append(FieldDiff(
                'missing_entity',
                '起点 {} 参考实现选出 {}，FlashText 漏选'.format(start, ref_record)
            ))
            continue
        if ft_record.clean != ref_record.clean:
            diffs.append(FieldDiff(
                'clean_mismatch',
                '起点 {} 实体选错：FlashText={} 参考={}'.format(
                    start, ft_record.clean, ref_record.clean)
            ))
        if ft_record.end != ref_record.end or ft_record.matched_text != ref_record.matched_text:
            diffs.append(FieldDiff(
                'span_mismatch',
                '起点 {} 区间失真：FlashText=[{}:{})={!r} 参考=[{}:{})={!r}'.format(
                    start,
                    ft_record.start, ft_record.end, ft_record.matched_text,
                    ref_record.start, ref_record.end, ref_record.matched_text)
            ))

    ref_replaced = matcher.replace(title)
    ft_replaced = processor.replace_keywords(title)
    return TitleCheck(
        title_index=title_index,
        title=title,
        reference_hits=ref_records,
        flashtext_hits=ft_records,
        diffs=diffs,
        reference_replaced=ref_replaced,
        flashtext_replaced=ft_replaced,
        replacement_differs=ref_replaced != ft_replaced,
    )


def build_reference(edits, case_sensitive=False):
    matcher = ReferenceMatcher(case_sensitive=case_sensitive)
    matcher.apply_edits(edits)
    return matcher


def build_flashtext(edits, case_sensitive=False, remove_fn=None):
    """按编辑序列构建 FlashText；remove_fn 可注入故障版删除逻辑。"""
    processor = KeywordProcessor(case_sensitive=case_sensitive)
    remover = remove_fn or (lambda kp, alias: kp.remove_keyword(alias))
    for action, alias, clean_name in edits:
        if action == 'add':
            processor.add_keyword(alias, clean_name)
        elif action == 'remove':
            remover(processor, alias)
        else:
            raise ValueError('unknown edit action: {}'.format(action))
    return processor


def run_audit(edits, titles, case_sensitive=False, processor_factory=None):
    """逐步回放编辑序列，每一步对全部标题做对照，返回快照列表。

    processor_factory(edits_so_far) -> processor，默认构建普通 FlashText；
    传入故障工厂即可在受控错误下跑同一套对照。
    """
    factory = processor_factory or (
        lambda seq: build_flashtext(seq, case_sensitive=case_sensitive)
    )
    snapshots = []
    for step, edit in enumerate(edits):
        edits_so_far = edits[:step + 1]
        matcher = build_reference(edits_so_far, case_sensitive=case_sensitive)
        processor = factory(edits_so_far)
        checks = [
            compare_title(index, title, matcher, processor)
            for index, title in enumerate(titles)
        ]
        snapshots.append(StepSnapshot(
            step=step,
            edit=edit,
            keywords=matcher.keywords,
            title_checks=checks,
        ))
    return snapshots


def diff_signatures(snapshots):
    """提取报告里所有差异的稳定签名，用于最小化时判断“同一个错误”。"""
    signatures = set()
    for snapshot in snapshots:
        for check in snapshot.title_checks:
            for diff in check.diffs:
                signatures.add((check.title_index, diff.kind, diff.detail))
    return signatures


def first_failing_step(snapshots):
    for snapshot in snapshots:
        if any(check.diffs for check in snapshot.title_checks):
            return snapshot.step
    return None


def minimize_edits(edits, titles, case_sensitive, reproduces, processor_factory=None):
    """贪心缩减：删掉任何一条编辑后只要仍能复现同一错误，就把它丢掉。

    返回 (保留下来的编辑列表, 保留编辑在原序列中的下标列表)。
    reproduces(edits_subset) -> bool 由调用方定义“同一个错误”的判定。
    """
    kept = list(range(len(edits)))
    for index in list(range(len(edits))):
        trial_indices = [i for i in kept if i != index]
        trial_edits = [edits[i] for i in trial_indices]
        snapshots = run_audit(
            trial_edits, titles,
            case_sensitive=case_sensitive,
            processor_factory=processor_factory,
        )
        if reproduces(snapshots):
            kept = trial_indices
    minimal_edits = [edits[i] for i in kept]
    return minimal_edits, kept


# ---------------------------------------------------------------------------
# 可控故障一：删坏前缀
# ---------------------------------------------------------------------------

def buggy_remove_break_prefix(processor, keyword):
    """故障版删除：把别名首字符对应的整棵 trie 子树删掉。

    正常 remove_keyword 只清终止标记并回收孤立路径，共享前缀保留；
    这里直接砍掉首字符节点，所有同前缀别名一起失效，模拟前缀被删坏。
    """
    key = keyword if processor.case_sensitive else keyword.lower()
    first_char = key[0]
    if first_char in processor.keyword_trie_dict:
        processor.keyword_trie_dict.pop(first_char)
        return True
    return False


# ---------------------------------------------------------------------------
# 可控故障二：偏移失真
# ---------------------------------------------------------------------------

class OffsetDistortingProcessor(KeywordProcessor):
    """继承真实 FlashText，只把命中区间末尾整体平移 span_shift 个字符。

    规范名（替换串）不受影响，唯独 span 区间失真，用来验证对照能把
    “区间错”与“实体选错”分开报告。
    """

    def __init__(self, case_sensitive=False, span_shift=1):
        super(OffsetDistortingProcessor, self).__init__(case_sensitive=case_sensitive)
        self.span_shift = span_shift

    def extract_keywords(self, sentence, span_info=False, max_cost=0):
        original = super(OffsetDistortingProcessor, self).extract_keywords(
            sentence, span_info=True, max_cost=max_cost
        )
        distorted = [
            (clean, start, min(end + self.span_shift, len(sentence)))
            for clean, start, end in original
        ]
        if span_info:
            return distorted
        return [item[0] for item in distorted]
