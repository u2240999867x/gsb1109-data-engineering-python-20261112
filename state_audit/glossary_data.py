# -*- coding: utf-8 -*-
"""小规模商品词典与编辑序列（独立于 FlashText 的词条清单）。

清单沿四个维度组织，每条别名都在 ``ALIAS_CATALOG`` 里给出解释：

* 规范名（canonical）：别名最终汇到的实体名；
* 词界（boundary）：FlashText 只在词界上起匹配，粘连在词内的子串不命中，
  空格、连字符、标点等 ``non_word_boundaries`` 之外的字符都是词界；
* 重叠（overlap）：短别名是长别名的前缀时，同一位置最长别名优先，
  长别名被删后短别名会重新接管该起点；
* 大小写（case）：默认大小写不敏感（统一 lower），可切换成敏感模式对照。
"""

# 规范实体名 -> 含义说明
CANONICAL_NAMES = {
    'RED-APPLE': '红果手机（演示实体 1）',
    'APPLE-PHONE': '苹果手机（演示实体 2）',
    'IPHONE': 'iPhone 旧规范名（覆盖前）',
    'IPHONE-8': 'iPhone 8 旧规范名（覆盖前）',
    'SF-STORE': '旧金山门店',
}

# 连续编辑序列：(动作, 别名, 规范名)；remove 动作规范名填 None。
# 第 9~11 步覆盖“长别名删掉后短别名重现”；
# 第 12~13 步覆盖“覆盖后多个别名汇到同一实体”。
EDIT_SEQUENCE = [
    ('add', 'red', 'RED-APPLE'),                    # 0
    ('add', 'red apple x1', 'RED-APPLE'),           # 1
    ('add', 'red apple x1 pro', 'RED-APPLE'),       # 2 最长前缀别名
    ('add', 'apple', 'APPLE-PHONE'),                # 3
    ('add', 'apple phone', 'APPLE-PHONE'),          # 4
    ('add', 'iphone', 'IPHONE'),                    # 5 旧规范名
    ('add', 'iphone 8', 'IPHONE-8'),                # 6
    ('add', 'a-phone', 'APPLE-PHONE'),              # 7 含连字符词界
    ('add', 'sf', 'SF-STORE'),                      # 8
    ('remove', 'red apple x1 pro', None),           # 9 删最长，回退到 x1
    ('remove', 'red apple x1', None),               # 10 再删，回退到 red
    ('remove', 'red', None),                        # 11 短别名也删掉
    ('add', 'iphone', 'APPLE-PHONE'),               # 12 覆盖：汇到同一实体
    ('add', 'iphone 8', 'APPLE-PHONE'),             # 13 覆盖：汇到同一实体
    ('remove', 'apple', None),                      # 14 删短前缀，长别名仍在
    ('add', 'red apple x1', 'RED-APPLE'),           # 15 前缀不在时重建 trie 路径
    ('remove', 'a-phone', None),                    # 16 连字符别名下线
]

# 每次编辑后都要核对的标题：覆盖词界、重叠、大小写与粘连场景。
TITLES = [
    'red apple x1 and red on sale',       # 0 长别名 + 短别名各一处
    'red apple x1 pro review',             # 1 最长别名，删除后逐级回退
    'a-phone beats iphone 8 in sf',        # 2 连字符 / 数字 / 门店
    'Apple Phone and iPhone at the big apple',  # 3 最长优先 + 裸 apple
    'IPHONE or iphone, pick the iPhone',   # 4 大小写不敏感
    'myiphonex is not an iphone',          # 5 粘连不命中 vs 词界命中
]

# 词条说明：canonical 为规范名，axes 标注该别名涉及的维度，note 给解释。
ALIAS_CATALOG = {
    'red': {
        'canonical': 'RED-APPLE',
        'axes': ('boundary', 'overlap'),
        'note': '最短前缀别名，只在词界起点命中；是两个长别名的前缀，'
                '长别名被删后由它重新接管同一实体区间。',
    },
    'red apple x1': {
        'canonical': 'RED-APPLE',
        'axes': ('boundary', 'overlap'),
        'note': '含空格的多词别名，空格是词界字符但允许出现在别名内部；'
                '相对 red 是更长匹配，相对 pro 是更短匹配。',
    },
    'red apple x1 pro': {
        'canonical': 'RED-APPLE',
        'axes': ('overlap',),
        'note': '该起点上的最长别名；删除后标题里的同一起点回退到 red apple x1。',
    },
    'apple': {
        'canonical': 'APPLE-PHONE',
        'axes': ('boundary', 'overlap'),
        'note': "apple phone 的短前缀；只在词界命中（如 big apple），"
                "不会命中粘连词；长别名存在时同一起点让位于 apple phone。",
    },
    'apple phone': {
        'canonical': 'APPLE-PHONE',
        'axes': ('overlap', 'canonical'),
        'note': '与 apple 同起点时按最长匹配胜出；与 iphone 是同一规范名下的不同别名。',
    },
    'iphone': {
        'canonical': 'IPHONE -> 第 12 步覆盖为 APPLE-PHONE',
        'axes': ('boundary', 'case', 'overlap', 'canonical'),
        'note': '大小写不敏感时 IPHONE/iPhone 都命中；粘连在 myiphonex 内不命中；'
                '是 iphone 8 的前缀；覆盖后与 apple phone 汇到同一实体。',
    },
    'iphone 8': {
        'canonical': 'IPHONE-8 -> 第 13 步覆盖为 APPLE-PHONE',
        'axes': ('boundary', 'overlap', 'canonical'),
        'note': '数字 8 是词内字符、空格是词界；同起点比 iphone 更长所以优先；'
                '覆盖后与 iphone 汇到同一实体。',
    },
    'a-phone': {
        'canonical': 'APPLE-PHONE',
        'axes': ('boundary',),
        'note': "连字符 '-' 是词界字符但允许出现在别名内部，整体作为一个别名命中。",
    },
    'sf': {
        'canonical': 'SF-STORE',
        'axes': ('boundary', 'case'),
        'note': '短别名同样受词界约束，只匹配独立的 sf 片段。',
    },
}

# 四个维度的总说明，供演示输出。
GLOSSARY_NOTES = {
    'canonical': '规范名是替换串：命中区间原文被替换成别名映射的规范名；'
                 '多个别名可映射同一规范名（覆盖/汇聚），add 已存在别名时只改映射不增词条。',
    'boundary': '词界字符 = 不在 non_word_boundaries（默认 [A-Za-z0-9_]）里的字符，'
                '如空格、连字符、逗号、句号；匹配必须起止都落在词界上，粘连在词内的子串不命中。',
    'overlap': '同一起点存在前缀嵌套的多个别名时最长者胜出；删除长别名只清终止标记，'
               '前缀路径保留，短别名随即在同一起点重新命中。',
    'case': '默认 case_sensitive=False，标题与别名统一 lower 后匹配，span 下标仍指向原文；'
            'case_sensitive=True 时只有大小写完全一致的别名才命中。',
}
