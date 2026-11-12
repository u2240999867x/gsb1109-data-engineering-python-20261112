"""GSB1109-20261112-DE-PY-006 — 定制邮编三种失效解释的最小复现批次。

同一份合成客户数据里，定制邮编（新格式 ``CN-######``）只在部分行生效。
本脚本用 Faker 的注册入口（``Factory.create(..., includes=[...])`` 与
``Generator.add_provider``）重建三种解释，逐行记录：
  - 请求的 locale
  - 实际供数的实现（provider 类、其 locale、对象身份）
  - 是否命中新邮编格式

并对“单靠输出字符串不能分辨”的两种情况（B 代理沿用旧候选集合 /
C 字段定义持有旧绑定方法）做一次额外观测（现役绑定对象的身份比对）把它们分开。

运行：python3 postcode_probe.py   （从本目录运行，脚本可重复执行，结果确定）
"""

import re

from collections import Counter

from faker import Faker

from custom_postcode_provider import Provider as CustomPostcodeProvider

NEW_FORMAT = re.compile(r"^CN-\d{6}$")
ROWS_PER_SCENARIO = 4


def serve_info(fn):
    """从供数的绑定方法上取“实际供数的实现”。"""
    provider = fn.__self__
    return {
        "impl": f"{type(provider).__module__}.{type(provider).__name__}",
        "lang": getattr(provider, "__lang__", None),
        "provider_id": id(provider),
        "generator_id": id(provider.generator),
    }


def draw_rows(scenario, requested_locale, make_fn, n=ROWS_PER_SCENARIO):
    """make_fn() 每次返回“本行实际使用的绑定方法”，模拟管线逐行取数。"""
    rows = []
    for i in range(1, n + 1):
        fn = make_fn()
        value = fn()
        rows.append(
            {
                "row": f"{scenario}-{i}",
                "requested_locale": requested_locale,
                "postcode": value,
                "hit_new_format": bool(NEW_FORMAT.match(value)),
                "served_by": serve_info(fn),
            }
        )
    return rows


def print_rows(title, rows):
    print(f"\n== {title} ==")
    for r in rows:
        s = r["served_by"]
        print(
            f"  {r['row']}  req_locale={r['requested_locale']:<16} "
            f"postcode={r['postcode']:<10} hit_new={str(r['hit_new_format']):<5} "
            f"served_by={s['impl']} lang={s['lang']} "
            f"provider#{s['provider_id']} generator#{s['generator_id']}"
        )


# ---------------------------------------------------------------------------
# 解释 A：工厂没收到 provider —— 注册动作落在了另一个 Faker 实例上，
#          实际供数的工厂从未见过定制 provider。
# ---------------------------------------------------------------------------
fake_a = Faker("en_US")  # 实际供数的工厂
wrong_instance = Faker("en_US")
wrong_instance.add_provider(CustomPostcodeProvider)  # 注册到了别的实例
fake_a.seed_instance(20261001)
rows_a = draw_rows("A", "en_US", lambda: fake_a.postcode)

# ---------------------------------------------------------------------------
# 解释 B：代理沿用旧的候选集合 —— 多 locale 代理第一次取 postcode 时缓存了
#          _cached_postcode_mapping；之后虽用注册入口重建了 en_US 工厂并塞进
#          _factory_map，但代理的 _factories / 缓存映射仍指向旧对象。
# ---------------------------------------------------------------------------
proxy = Faker(["en_US", "en_GB"])
proxy.seed_instance(20261002)
_ = proxy.postcode()  # 首次访问，暖化 _cached_postcode_mapping（旧候选集合）
rebuilt = Faker("en_US", includes=["custom_postcode_provider"])  # 注册入口重建
proxy._factory_map["en_US"] = rebuilt  # 热替换：只换了 map，没换候选集合
rows_b = draw_rows("B", "en_US|en_GB", lambda: proxy.postcode)

# ---------------------------------------------------------------------------
# 解释 C：字段定义持有之前取出的绑定方法 —— 字段定义在注册前执行了
#          fn = fake.postcode，add_provider 之后生成器上的 formatter 已换，
#          但字段定义仍调用旧的绑定方法。
# ---------------------------------------------------------------------------
fake_c = Faker("en_US")
fake_c.seed_instance(20261003)
field_fn = fake_c.postcode  # 字段定义：注册“之前”取出的绑定方法
fake_c.add_provider(CustomPostcodeProvider)  # 注册入口：add_provider
rows_c = draw_rows("C", "en_US", lambda: field_fn)

print_rows("A 工厂没收到 provider（注册到了别的实例）", rows_a)
print_rows("B 代理沿用旧的候选集合（_cached_postcode_mapping / _factories 未失效）", rows_b)
print_rows("C 字段定义持有之前取出的绑定方法", rows_c)

# ---------------------------------------------------------------------------
# 额外观测：B 和 C 的行输出都是旧格式、且定制 provider 都“注册过”，
# 单靠输出字符串分不开。对现役链路做一次身份比对即可分开：
#   - B：行供数工厂不在代理现役候选集合里（代理层陈旧）
#   - C：行供数工厂就是现役生成器，但绑定方法属于旧 provider 实例（字段层陈旧）
# ---------------------------------------------------------------------------
print("\n== 额外观测：现役绑定对象的身份比对 ==")
cached_factories = proxy._cached_postcode_mapping[0]
print(
    f"  B: proxy 现役候选集合 factories ids = {[id(f) for f in proxy.factories]}\n"
    f"     _cached_postcode_mapping ids     = {[id(f) for f in cached_factories]}\n"
    f"     重建工厂 id = {id(rebuilt)}，在候选集合中？ {rebuilt in proxy.factories}\n"
    f"     但 proxy._factory_map['en_US'] is 重建工厂？ {proxy._factory_map['en_US'] is rebuilt}"
)
probe_top = serve_info(proxy.postcode)
probe_sub = serve_info(proxy["en_US"].postcode)
print(
    f"  B: 现役探测 顶层 proxy.postcode       -> {probe_top['impl']} lang={probe_top['lang']}（旧）\n"
    f"            proxy['en_US'].postcode   -> {probe_sub['impl']} lang={probe_sub['lang']}（新）\n"
    f"     => 供数工厂不在代理候选集合中：解释 B（代理沿用旧候选集合）"
)
live_fn = fake_c.postcode
field_impl = f"{type(field_fn.__self__).__module__}.{type(field_fn.__self__).__name__}"
live_impl = f"{type(live_fn.__self__).__module__}.{type(live_fn.__self__).__name__}"
print(
    f"  C: 字段定义持有的绑定方法 provider#{id(field_fn.__self__)} ({field_impl})\n"
    f"     现役 fake_c.postcode   provider#{id(live_fn.__self__)} ({live_impl})\n"
    f"     同一生成器？ {field_fn.__self__.generator is live_fn.__self__.generator}\n"
    f"     => 同一生成器、不同 provider 实例：解释 C（字段定义持有旧绑定方法）"
)

# ---------------------------------------------------------------------------
# 判定：有来源标记的行 -> 按解释补造；只剩邮编的行 -> 旧格式无法判定
# ---------------------------------------------------------------------------
print("\n== 判定：有来源标记的行 ==")
fixes = {
    "A": "补造：把定制 provider 注册到实际供数的工厂（includes=/add_provider 用对实例）",
    "B": "补造：重建代理（或使 _cached_*_mapping/_factories 失效）后再取数",
    "C": "补造：注册之后重新取绑定方法（field = fake.postcode）再生成",
}
for scenario, rows in (("A", rows_a), ("B", rows_b), ("C", rows_c)):
    stale = [r["row"] for r in rows if not r["hit_new_format"]]
    print(f"  {scenario}: 旧格式行 {stale} -> {fixes[scenario]}")

print("\n== 判定：只剩邮编、无来源标记的行 ==")
untagged = ["90210", "CN-483920", "SW1A 1AA", "CN-007331", "10001-1234"]
for code in untagged:
    if NEW_FORMAT.match(code):
        verdict = "可信：新格式只可能来自正确链路，无需补造"
    else:
        verdict = "无法判定：旧格式与 A/B/C 三种缺陷及迁移前旧数据均相容，需按可疑处理"
    print(f"  {code:<12} -> {verdict}")

# ---------------------------------------------------------------------------
# 保留旧分片可复现性的切换次序
# ---------------------------------------------------------------------------
print(
    """
== 切换次序（保留旧分片可复现性） ==
  1. 冻结旧分片：记录其 seed、locale 列表、provider 清单与 Faker 版本，旧管线不再改动。
  2. 新分片在 Factory.create(..., includes=[...]) 时注册定制 provider；
     若用 add_provider，必须在首次取数（任何 postcode 访问）之前完成，
     避免代理暖化出旧的 _cached_*_mapping。
  3. 重建 Faker 代理（新的 _factories、空缓存），不要热替换 _factory_map。
  4. 字段定义在注册“之后”重新取绑定方法（field = fake.postcode）。
  5. 结构就绪后才 seed_instance(新 seed) 并生成新分片；
     旧分片重跑仍用原 seed 与原工厂，两边互不影响
     （用 seed_instance 而非类方法 Faker.seed，避免共享 random 被改写）。
"""
)

# 汇总计数，便于回归比对
summary = Counter(
    (scenario, r["hit_new_format"])
    for scenario, rows in (("A", rows_a), ("B", rows_b), ("C", rows_c))
    for r in rows
)
print("汇总 (scenario, hit_new_format) -> 行数:", dict(sorted(summary.items())))
