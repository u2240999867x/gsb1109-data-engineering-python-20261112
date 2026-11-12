import copy
import random

from jsonmerge import Merger
from jsonmerge.exceptions import HeadInstanceError

from parallel_merge_planner import (
    route_business_key, TIER_A, TIER_B, TIER_C,
    fold_partition, combine_summaries, combine_products,
    combine_version, combine_append,
)

SCHEMA = {
    "type": "object", "mergeStrategy": "objectMerge",
    "properties": {
        "name":  {"mergeStrategy": "overwrite"},
        "tags":  {"type": "array", "mergeStrategy": "append"},
        "audit": {"mergeStrategy": "version", "mergeOptions": {"limit": 4}},
        "contacts": {
            "type": "array", "mergeStrategy": "arrayMergeById",
            "mergeOptions": {"idRef": "id"},
            "items": {
                "type": "object", "mergeStrategy": "objectMerge",
                "properties": {
                    "id":     {"mergeStrategy": "overwrite"},
                    "status": {"mergeStrategy": "version"},
                    "emails": {"type": "array", "mergeStrategy": "append"},
                },
            },
        },
    },
}


def serial(schema, events):
    m = Merger(schema)
    base, first_error = None, None
    for i, e in enumerate(events):
        try:
            base = m.merge(base, e)
        except HeadInstanceError as exc:
            first_error = (i, str(exc))
            break
    return base, first_error


def random_stream(rng, n, with_dup_id=False):
    evs = []
    for i in range(n):
        e = {}
        if rng.random() < 0.8:
            e["name"] = rng.choice(["n1", "n2", "n3"])
        if rng.random() < 0.7:
            e["tags"] = [rng.choice(["t1", "t2", "t3"])]
        if rng.random() < 0.8:
            e["audit"] = rng.choice(["a", "a", "b", "c"])  # repeats -> version dedup
        if rng.random() < 0.8:
            cid = rng.choice([1, 1, 2, 3])                 # same id across events
            contact = {"id": cid}
            if rng.random() < 0.8:
                contact["status"] = rng.choice(["new", "new", "active", "suspended"])
            if rng.random() < 0.7:
                contact["emails"] = [rng.choice(["e@x", "f@x"])]
            e["contacts"] = [contact]
        evs.append(e)
    if with_dup_id:
        evs.append({"contacts": [{"id": 9}, {"id": 9}]})   # head error
    return evs


def test_routing():
    tier, tiers = route_business_key(SCHEMA)
    assert tier == TIER_B
    kinds = {p: t[0] for p, t in tiers.items()}
    assert any(t == TIER_B for t in kinds.values())
    assert all(t != TIER_C for t in kinds.values())
    print("routing table:")
    for p, (t, name, opts) in tiers.items():
        print("  %-28s %-18s %s" % ("/".join(p) or "<root>", name or "(default)", t))


def test_parallel_equals_serial(seed, parts, with_dup_id):
    rng = random.Random(seed)
    events = random_stream(rng, 24, with_dup_id=with_dup_id)
    want, want_err = serial(SCHEMA, events)

    bounds = sorted(rng.sample(range(1, len(events)), parts - 1))
    cuts = [0] + bounds + [len(events)]
    partitions = []
    for lo, hi in zip(cuts, cuts[1:]):
        prod, rec = fold_partition(SCHEMA, [(i, events[i]) for i in range(lo, hi)])
        partitions.append((prod, rec))

    got, got_err = combine_summaries(SCHEMA, partitions)
    assert got == want, (seed, got, want)
    assert got_err == want_err, (seed, got_err, want_err)
    return seed, len(events), cuts, bool(want_err)


def test_combine_operators():
    # version: boundary dedup + global limit, identical to serial
    mv = Merger({"mergeStrategy": "version", "mergeOptions": {"limit": 4}})
    heads = ["a", "a", "b", "b", "c", "c", "d", "e"]
    serial_parts, b = [], None
    chunk = 3
    products = []
    for i, h in enumerate(heads):
        b = mv.merge(b, h)
        if (i + 1) % chunk == 0 or i == len(heads) - 1:
            products.append(b)
            b = None
    full, _ = serial({"mergeStrategy": "version",
                      "mergeOptions": {"limit": 4}}, [{"_": h} for h in heads])
    # direct scalar schema serial for comparison:
    full = None
    mm = Merger({"mergeStrategy": "version", "mergeOptions": {"limit": 4}})
    for h in heads:
        full = mm.merge(full, h)
    assert combine_version([copy.deepcopy(p) for p in products], limit=4) == full

    # append: ordered concatenation
    ma = Merger({"type": "array", "mergeStrategy": "append"})
    p1 = ma.merge(None, [1, 2]); p2 = ma.merge(None, [3]); p3 = ma.merge(None, [4, 5])
    full = None
    for h in ([1, 2], [3], [4, 5]):
        full = ma.merge(full, h)
    assert combine_append([p1, p2, p3]) == full
    print("operator checks: version(boundary dedup), append match serial")


# Same shape as SCHEMA but version nodes have no limit -> fully TIER_A:
# partition products alone recombine into the exact serial result, including
# mixed nesting and the same id appearing across multiple partitions.
SCHEMA_NO_LIMIT = copy.deepcopy(SCHEMA)
SCHEMA_NO_LIMIT["properties"]["audit"].pop("mergeOptions")


def test_product_combine_matches_serial(seed, parts):
    rng = random.Random(seed)
    events = random_stream(rng, 24)
    want, _ = serial(SCHEMA_NO_LIMIT, events)

    bounds = sorted(rng.sample(range(1, len(events)), parts - 1))
    cuts = [0] + bounds + [len(events)]
    products = []
    for lo, hi in zip(cuts, cuts[1:]):
        prod, _ = fold_partition(SCHEMA_NO_LIMIT,
                                 [(i, events[i]) for i in range(lo, hi)])
        products.append(prod)
    assert combine_products(SCHEMA_NO_LIMIT, products) == want
    assert route_business_key(SCHEMA_NO_LIMIT)[0] == TIER_A


if __name__ == "__main__":
    test_routing()
    test_combine_operators()
    ok = 0
    for seed in range(300):
        for parts in (2, 3, 4):
            test_product_combine_matches_serial(seed, parts)   # TIER_A path
            ok += 1
    for seed in range(300):
        for parts in (2, 3, 4):
            s, n, cuts, errored = test_parallel_equals_serial(
                seed, parts, with_dup_id=False)                # TIER_B path
            ok += 1
    for seed in range(50):
        s, n, cuts, errored = test_parallel_equals_serial(
            1000 + seed, 3, with_dup_id=True)
        assert errored
        ok += 1
    print("parallel == serial on %d randomized partitionings "
          "(product-combine + summary-replay + first-error)" % ok)
    print("ALL CHECKS PASSED")
