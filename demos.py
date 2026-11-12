"""Demonstrations backing the planning rules. Run: .venv/bin/python demos.py"""
import copy
import random

from jsonmerge import Merger
from parallel_merge_planner import (
    combine_products, combine_version, fold_partition,
    combine_summaries, route_business_key, TIER_A, TIER_B,
)
from test_parallel_merge import serial, SCHEMA_NO_LIMIT, random_stream

print("=" * 70)
print("LOSS 1: same multiset of partial products, different serial history")
print("=" * 70)
ma = Merger({"type": "array", "mergeStrategy": "append"})
x, y = [{"id": 1, "tag": "x"}], [{"id": 1, "tag": "y"}]
# Two partitionings of the same two events. The set of partial products is
# identical ({[x],[y]}); only the partition boundaries differ.
alpha = (ma.merge(None, x), ma.merge(None, y))     # x in partition 1
beta = (ma.merge(None, y), ma.merge(None, x))      # y in partition 1
print("partitioning alpha products:", alpha)
print("partitioning beta  products:", beta)
print("same partial products ?", sorted(map(str, alpha)) == sorted(map(str, beta)))
serial_alpha = ma.merge(copy.deepcopy(alpha[0]), y)
serial_beta = ma.merge(copy.deepcopy(beta[0]), x)
print("serial result alpha:", serial_alpha)
print("serial result beta :", serial_beta)
print("equal ?", serial_alpha == serial_beta,
      "-> the product stores no global seq; order lives only in partition placement")

print()
print("=" * 70)
print("LOSS 2: identical partial product, but one stream had a head error")
print("=" * 70)
from jsonmerge.exceptions import HeadInstanceError
id_schema = {
    "type": "array", "mergeStrategy": "arrayMergeById",
    "mergeOptions": {"idRef": "id"},
    "items": {"type": "object", "mergeStrategy": "objectMerge",
              "properties": {"id": {}, "v": {}}},
}
mid = Merger(id_schema)
# Stream A: second head repeats an id -> HeadInstanceError, fold aborts,
# leaving the same partial product that the healthy stream B ends with.
try:
    mid.merge(mid.merge(None, [{"id": 1, "v": "a"}]),
              [{"id": 1, "v": "b"}, {"id": 1, "v": "c"}])
except HeadInstanceError as exc:
    print("stream A aborts:", exc)
product_a = mid.merge(None, [{"id": 1, "v": "a"}])
product_b = mid.merge(None, [{"id": 1, "v": "a"}])
print("partial product A:", product_a)
print("partial product B:", product_b, " identical?", product_a == product_b)
print("serial outcome A: abort at the duplicate-id head; B: keeps merging")
print("-> the product alone cannot reconstruct the FIRST ERROR; each partition")
print("   must ship (first_error_seq, error) alongside its product")

print()
print("=" * 70)
print("LOSS 3: identical version products hide multiplicity and eviction")
print("=" * 70)
mv = Merger({"mergeStrategy": "version"})
r1 = mv.merge(None, "x")
r2 = mv.merge(mv.merge(None, "x"), "x")
print("fold [x]    ->", r1)
print("fold [x, x] ->", r2, " identical?", r1 == r2,
      "-> suppressed duplicate count is unrecoverable (ignoreDups=True)")

mvl = Merger({"mergeStrategy": "version", "mergeOptions": {"limit": 2}})
a = mvl.merge(mvl.merge(mvl.merge(mvl.merge(None, 9), 2), 3), 4)
b = mvl.merge(mvl.merge(None, 3), 4)
print("limit=2: [9,2,3,4] ->", a)
print("limit=2: [3,4]     ->", b, " identical?", a == b,
      "-> evicted heads 9, 2 cannot be rebuilt for full audit history")

print()
print("=" * 70)
print("LINE-BY-LINE RECONCILIATION: partition products -> full serial history")
print("=" * 70)
rng = random.Random(7)
events = random_stream(rng, 12)
want, _ = serial(SCHEMA_NO_LIMIT, events)
cut = 6
p1, s1 = fold_partition(SCHEMA_NO_LIMIT, [(i, events[i]) for i in range(cut)])
p2, s2 = fold_partition(SCHEMA_NO_LIMIT, [(i, events[i]) for i in range(cut, 12)])
print("partition seq ranges: P1 =", [s for s, _ in s1.raw_events],
      " P2 =", [s for s, _ in s2.raw_events])

# append node: concat in partition order, one line per element
print("\nappend /tags  P1 =", p1["tags"])
print("append /tags  P2 =", p2["tags"])
print("append /tags combined =", combine_products(
    SCHEMA_NO_LIMIT["properties"]["tags"], [p1["tags"], p2["tags"]]))

# version node: concat entries, only boundary-adjacent dup is re-checked
print("\nversion /audit P1 =", [e["value"] for e in p1["audit"]])
print("version /audit P2 =", [e["value"] for e in p2["audit"]])
merged_audit = combine_version([p1["audit"], p2["audit"]])
print("version /audit combined (boundary dedup only) =",
      [e["value"] for e in merged_audit])

# arrayMergeById: same id across partitions is an ordered update, not an error
print("\narrayMergeById /contacts P1 ids =", [c["id"] for c in p1["contacts"]])
print("arrayMergeById /contacts P2 ids =", [c["id"] for c in p2["contacts"]])
full = combine_products(SCHEMA_NO_LIMIT, [p1, p2])
print("combined contact order (global first-seen):",
      [c["id"] for c in full["contacts"]])
for c in full["contacts"]:
    print("  id=%s status history=%s emails=%s" % (
        c["id"], [e["value"] for e in c["status"]], c["emails"]))
print("\nfull recombined document == serial ground truth ?", full == want)

print()
print("=" * 70)
print("SORT + METADATA: product combiner matches library semantics")
print("=" * 70)
sort_schema = {
    "type": "array", "mergeStrategy": "append",
    "mergeOptions": {"sortByRef": "name"},
}
ms = Merger(sort_schema)
sp1 = ms.merge(None, [{"name": "b"}, {"name": "a"}])
sp2 = ms.merge(None, [{"name": "c"}])
lib = ms.merge(copy.deepcopy(sp1), sp2)
ours = combine_products(sort_schema, [sp1, sp2])
print("append+sortByRef library =", lib)
print("append+sortByRef combiner=", ours, " equal?", lib == ours)

meta_schema = {"mergeStrategy": "version",
               "mergeOptions": {"metadata": {"rev": "n"}}}
mm = Merger(meta_schema)
mp1 = mm.merge(mm.merge(None, "a"), "b")
mp2 = mm.merge(None, "c")
# Ground truth is the serial fold of the raw heads, NOT re-merging products
# (re-merging would nest mp2's version array inside a new entry).
truth = None
for h in ("a", "b", "c"):
    truth = mm.merge(truth, h)
ours = combine_products(meta_schema, [mp1, mp2])
print("version+metadata serial truth =", truth)
print("version+metadata combiner      =", ours, " equal?", truth == ours)

print()
print("=" * 70)
print("LIMITED version forces TIER_B and is rebuilt via event summary")
print("=" * 70)
from test_parallel_merge import SCHEMA
tier, _ = route_business_key(SCHEMA)
print("routing of schema containing version(limit=4):", tier)
lim_evs = random_stream(random.Random(11), 20)
want, want_err = serial(SCHEMA, lim_evs)
parts = []
for lo, hi in ((0, 7), (7, 14), (14, 20)):
    parts.append(fold_partition(SCHEMA, [(i, lim_evs[i]) for i in range(lo, hi)]))
got, err = combine_summaries(SCHEMA, parts)
print("summary replay == serial ?", got == want, " first error aligned ?", err == want_err)
