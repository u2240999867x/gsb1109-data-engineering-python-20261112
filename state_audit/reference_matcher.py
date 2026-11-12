# -*- coding: utf-8 -*-
"""独立于 FlashText 的参考匹配器。

这份实现刻意不导入 ``flashtext``，只用最直白的 token 切分与前缀比较，
作为规范（spec）存在：审计时 FlashText 的输出与它逐字段对照，
任何不一致都算作差异，而不是让 FlashText 自己证明自己。

规范要点（与 FlashText 文档化行为一致，但独立实现）：

1. 词界：``non_word_boundaries`` 之外的字符都是词界；匹配必须从词界起点开始、
   在词界（或句尾）结束，所以粘连在词内的子串不命中。
2. 重叠：同一起点有多个前缀嵌套别名时，取结束位置最远的最长别名。
3. 大小写：大小写不敏感时别名与标题统一 lower，span 下标仍按原文给出。
4. 替换：命中区间的原文整体换成规范名，其余原文（含大小写、标点）原样保留。
"""

from collections import namedtuple
import string


DEFAULT_NON_WORD_BOUNDARIES = frozenset(
    string.digits + string.ascii_letters + '_'
)

# clean：规范名/替换串；start、end：原文半开区间；matched_text：区间原文。
EntityHit = namedtuple('EntityHit', ['clean', 'start', 'end', 'matched_text'])


def is_word_boundary(char, non_word_boundaries):
    return char not in non_word_boundaries


def tokenize(text, non_word_boundaries):
    """把文本切成词内 token，记录每个 token 的原文半开区间。

    返回 [(token_text, start, end), ...]，token 之间的词界字符不进列表，
    需要时用 ``text[prev_end:next_start]`` 取回词界原文。
    """
    tokens = []
    start = None
    for idx, char in enumerate(text):
        if char in non_word_boundaries:
            if start is None:
                start = idx
        elif start is not None:
            tokens.append((text[start:idx], start, idx))
            start = None
    if start is not None:
        tokens.append((text[start:len(text)], start, len(text)))
    return tokens


def split_alias(alias, non_word_boundaries):
    """别名切成 (token 列表, 词界原文列表)；词界列表长度 = token 数 - 1。"""
    tokens = []
    separators = []
    buf = []
    sep = []
    for char in alias:
        if char in non_word_boundaries:
            if sep:
                separators.append(''.join(sep))
                sep = []
            buf.append(char)
        else:
            if buf:
                tokens.append(''.join(buf))
                buf = []
            sep.append(char)
    if buf:
        tokens.append(''.join(buf))
    if sep:
        separators.append(''.join(sep))
    return tokens, separators


class ReferenceMatcher(object):
    """参考词典：维护 别名 -> 规范名 映射，提供匹配与替换。"""

    def __init__(self, case_sensitive=False, non_word_boundaries=None):
        self.case_sensitive = case_sensitive
        self.non_word_boundaries = (
            frozenset(non_word_boundaries)
            if non_word_boundaries is not None
            else DEFAULT_NON_WORD_BOUNDARIES
        )
        # 统一存 lower 后的别名键（大小写不敏感模式）。
        self._mapping = {}
        # 预编译：首 token(lower) -> [(alias token 列表, 词界列表, clean, 原别名)]
        self._by_first_token = {}

    def _norm(self, text):
        return text if self.case_sensitive else text.lower()

    def add(self, alias, clean_name):
        key = self._norm(alias)
        self._mapping[key] = clean_name
        self._rebuild()

    def remove(self, alias):
        self._mapping.pop(self._norm(alias), None)
        self._rebuild()

    def apply_edits(self, edits):
        for action, alias, clean_name in edits:
            if action == 'add':
                self.add(alias, clean_name)
            elif action == 'remove':
                self.remove(alias)
            else:
                raise ValueError('unknown edit action: {}'.format(action))

    @property
    def keywords(self):
        return dict(self._mapping)

    def _rebuild(self):
        self._by_first_token = {}
        for alias, clean_name in self._mapping.items():
            tokens, separators = split_alias(alias, self.non_word_boundaries)
            if not tokens:
                continue
            self._by_first_token.setdefault(tokens[0], []).append(
                (tokens, separators, clean_name, alias)
            )

    def find_hits(self, text):
        """返回文本中全部命中：左起最长、互不重叠的 EntityHit 列表。"""
        if not text:
            return []
        tokens = tokenize(text, self.non_word_boundaries)
        hits = []
        i = 0
        while i < len(tokens):
            first_text, first_start, _ = tokens[i]
            candidates = self._by_first_token.get(self._norm(first_text))
            best = None
            if candidates:
                for alias_tokens, separators, clean_name, _ in candidates:
                    hit = self._match_candidate(text, tokens, i,
                                                alias_tokens, separators, clean_name)
                    if hit is not None and (best is None or hit.end > best.end):
                        best = hit
            if best is not None:
                hits.append(best)
                # 跳过被这个最长别名吃掉的 token。
                consumed = 1
                while i + consumed < len(tokens) and tokens[i + consumed][1] < best.end:
                    consumed += 1
                i += consumed
            else:
                i += 1
        return hits

    def _match_candidate(self, text, tokens, start_idx,
                         alias_tokens, separators, clean_name):
        if start_idx + len(alias_tokens) > len(tokens):
            return None
        last_idx = start_idx
        for offset in range(len(alias_tokens)):
            token_text, token_start, token_end = tokens[start_idx + offset]
            if self._norm(token_text) != alias_tokens[offset]:
                return None
            if offset < len(alias_tokens) - 1:
                # 别名内部要求词界原文逐字符一致（复刻 FlashText 的单字符跳转）。
                gap = text[token_end:tokens[start_idx + offset + 1][1]]
                if gap != separators[offset]:
                    return None
            last_idx = start_idx + offset
        start = tokens[start_idx][1]
        end = tokens[last_idx][2]
        if not self._anchored_at_boundaries(text, start, end):
            return None
        return EntityHit(clean_name, start, end, text[start:end])

    def _anchored_at_boundaries(self, text, start, end):
        left_ok = start == 0 or is_word_boundary(text[start - 1], self.non_word_boundaries)
        right_ok = end == len(text) or is_word_boundary(text[end], self.non_word_boundaries)
        return left_ok and right_ok

    def replace(self, text):
        """按命中区间做切片替换，未命中部分保留原文（含大小写与标点）。"""
        hits = self.find_hits(text)
        if not hits:
            return text
        parts = []
        cursor = 0
        for hit in hits:
            parts.append(text[cursor:hit.start])
            parts.append(hit.clean)
            cursor = hit.end
        parts.append(text[cursor:])
        return ''.join(parts)


def boundary_explanation(text, start, end, non_word_boundaries=None):
    """解释某个区间为什么（不）满足词界约束，供清单演示与诊断使用。"""
    nwb = DEFAULT_NON_WORD_BOUNDARIES if non_word_boundaries is None else frozenset(non_word_boundaries)
    reasons = []
    if start == 0:
        reasons.append('左侧是句首，天然是词界')
    elif text[start - 1] in nwb:
        reasons.append("左侧字符 '{}' 是词内字符，粘连不命中".format(text[start - 1]))
    else:
        reasons.append("左侧字符 '{}' 是词界字符".format(text[start - 1]))
    if end == len(text):
        reasons.append('右侧是句尾，天然是词界')
    elif text[end] in nwb:
        reasons.append("右侧字符 '{}' 是词内字符，粘连不命中".format(text[end]))
    else:
        reasons.append("右侧字符 '{}' 是词界字符".format(text[end]))
    return reasons
