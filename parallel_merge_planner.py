"""
Parallel-merge planning rules for jsonmerge.

Three routing tiers per business key / schema path:

  TIER_A  partition products can be combined directly (no extra summary)
  TIER_B  products alone lose information; an event-level summary must be
          retained and combined with the rules below
  TIER_C  not associatively mergeable; the business key must stay in one
          partition and be folded serially

Run:  .venv/bin/python parallel_merge_planner.py
"""
import copy
from jsonmerge import Merger
from jsonmerge.exceptions import HeadInstanceError, BaseInstanceError

TIER_A, TIER_B, TIER_C = "A_direct", "B_summary", "C_serial"


def classify(schema_node, path=()):
    """Return {json-path: tier} for every strategy node in the schema."""
    out = {}
    node = schema_node or {}
    name = node.get("mergeStrategy")
    opts = node.get("mergeOptions", {})

    # Runtime descenders pick the effective schema from the data, so no fixed
    # combine algebra exists at planning time -> the key stays serial.
    if any(k in node for k in ("oneOf", "anyOf", "allOf", "$ref")):
        out[path] = (TIER_C, name or "descender", opts)
        return out

    if name in (None, "objectMerge"):
        tier = TIER_A
    elif name in ("overwrite", "discard", "append", "arrayMergeByIndex"):
        tier = TIER_A
    elif name == "version":
        # Without a limit every retained entry survives in the product and the
        # only cross-partition work is boundary dedup, which the products'
        # boundary values decide -> products suffice (TIER_A).
        # With a limit, each partition truncates on its own and the evicted
        # raw heads are gone from every product -> an event-level summary is
        # required to reproduce the globally-trimmed serial history.
        tier = TIER_B if opts.get("limit") is not None else TIER_A
    elif name == "arrayMergeById":
        # Same id across partitions is a legal ordered update; a schema-driven
        # product combiner handles it. Head-local uniqueness errors are caught
        # while folding each partition, so this stays TIER_A for the value.
        # It drops to TIER_B only if a child node is itself TIER_B.
        tier = TIER_A
    else:
        # Custom / unknown strategy, oneOf/anyOf/allOf-driven branches,
        # unresolvable $ref at planning time -> no algebraic combine rule.
        tier = TIER_C

    out[path] = (tier, name, opts)

    child_tiers = []
    if isinstance(node.get("items"), dict):
        sub_out = classify(node["items"], path + ("[]",))
        out.update(sub_out)
        child_tiers.extend(t[0] for t in sub_out.values())
    for keyword in ("properties", "patternProperties"):
        for key, sub in node.get(keyword, {}).items():
            sub_out = classify(sub, path + (key,))
            out.update(sub_out)
            child_tiers.extend(t[0] for t in sub_out.values())
    ap = node.get("additionalProperties")
    if isinstance(ap, dict):
        sub_out = classify(ap, path + ("*",))
        out.update(sub_out)
        child_tiers.extend(t[0] for t in sub_out.values())

    # A node inherits the weakest tier required anywhere in its subtree.
    if name == "arrayMergeById" and TIER_B in child_tiers:
        out[path] = (TIER_B, name, opts)
    return out


def _subschemas(node):
    if "items" in node and isinstance(node["items"], dict):
        yield node["items"]
    for s in node.get("properties", {}).values():
        yield s
    for s in node.get("patternProperties", {}).values():
        yield s
    ap = node.get("additionalProperties")
    if isinstance(ap, dict):
        yield ap


def route_business_key(schema):
    """Whole-key routing: the weakest tier on any path wins."""
    tiers = classify(schema)
    order = {TIER_A: 0, TIER_B: 1, TIER_C: 2}
    worst = max(tiers.values(), key=lambda t: order[t[0]])
    return worst[0], tiers


# --------------------------------------------------------------------------
# Partition folding and the TIER_B summary.
#
# The summary is deliberately the partition's *event slice with global
# sequence numbers* plus its first head-local error. It is the only summary
# that can provably rebuild a serial fold once a product has destroyed
# information (e.g. version `limit` eviction). Compact algebraic summaries
# are exactly the TIER_A product combiner below; do not invent a lossy
# compact summary for a TIER_B node.
# --------------------------------------------------------------------------

class PartitionSummary:
    def __init__(self):
        self.raw_events = []          # [(global_seq, event)] actually merged
        self.first_error = None       # (global_seq, str(error)) or None
        self.event_count = 0          # events offered before any abort


def fold_partition(schema, events_with_seq):
    """Fold one partition serially. Returns (product, PartitionSummary)."""
    summary = PartitionSummary()
    merger = Merger(schema)
    base = None
    for seq, event in events_with_seq:
        summary.event_count += 1
        try:
            base = merger.merge(base, event)
        except (HeadInstanceError, BaseInstanceError) as exc:
            # Deterministic at (seq, json ref); serial fold aborts here too.
            summary.first_error = (seq, str(exc))
            break
        summary.raw_events.append((seq, copy.deepcopy(event)))
    return base, summary


def combine_summaries(schema, partitions):
    """partitions: [(product, PartitionSummary)] in partition (seq) order.

    Mirrors the serial fold's abort semantics: returns the product folded
    from every event before the first error, together with that first error
    (smallest global seq). Returns (product, None) when no partition failed.
    """
    errors = [s.first_error for _, s in partitions if s.first_error]
    first_error = min(errors, key=lambda e: e[0]) if errors else None
    cutoff = first_error[0] if first_error else float("inf")

    ordered = sorted(
        (row for _, s in partitions for row in s.raw_events
         if row[0] < cutoff),
        key=lambda row: row[0],
    )
    merger = Merger(schema)
    base = None
    for _, event in ordered:
        base = merger.merge(base, event)
    return base, first_error


# --------------------------------------------------------------------------
# Schema-driven product combiner (TIER_A). One combine rule per jsonmerge
# strategy; recursion makes mixed nesting (append/version/arrayMergeById/
# overwrite inside one object) combine exactly like the serial fold.
# --------------------------------------------------------------------------

def combine_products(schema, products):
    """Combine partition *products* (not raw events) in partition order."""
    products = [p for p in products if p is not None]
    if not products:
        return None
    name = (schema or {}).get("mergeStrategy")
    opts = (schema or {}).get("mergeOptions", {})

    if name in (None, "objectMerge"):
        return _combine_object(schema, products)
    if name == "overwrite":
        return copy.deepcopy(products[-1])          # last writer wins
    if name == "discard":
        return copy.deepcopy(products[0])           # first writer wins
    if name == "append":
        return combine_append(products, opts.get("sortByRef"),
                              opts.get("sortReverse"))
    if name == "arrayMergeByIndex":
        return _combine_by_index(schema, products)
    if name == "arrayMergeById":
        out = _combine_by_id(schema, products, opts.get("idRef", "id"))
        return _maybe_sort(out, opts)
    if name == "version":
        return combine_version(products, opts.get("limit"),
                               opts.get("ignoreDups", True),
                               opts.get("unique"), opts.get("ignoreDups"))
    raise ValueError("TIER_C node cannot combine products: %r" % name)


def _combine_object(schema, products):
    subs = (schema or {}).get("properties", {})
    keys = []
    for p in products:
        for k in p:
            if k not in keys:
                keys.append(k)                      # first-seen property order
    out = {}
    for k in keys:
        sub_products = [p[k] for p in products if k in p]
        out[k] = combine_products(subs.get(k, {}), sub_products)
    return out


def _combine_by_index(schema, products):
    out = []
    for p in products:
        if len(out) < len(p):
            out.extend([None] * (len(p) - len(out)))
        for i, item in enumerate(p):
            out[i] = item if out[i] is None else combine_products(
                schema.get("items", {}), [out[i], item])
    return out


def _combine_by_id(schema, products, id_ref):
    item_schema = schema.get("items", {})
    out, pos = [], {}
    for product in products:                        # partition (global) order
        for item in product:                        # head order inside partition
            key = item.get(id_ref)
            if key in pos:                          # same id across partitions
                out[pos[key]] = combine_products(
                    item_schema, [out[pos[key]], copy.deepcopy(item)])
            else:
                pos[key] = len(out)                 # global first-seen order
                out.append(copy.deepcopy(item))
    return out


def combine_version(entry_arrays, limit=None, ignore_dups=True,
                    unique=None, ignore_dups_kw=None):
    """Concatenate entry arrays and redo ONLY the cross-partition boundary
    dedup (comparing adjacent entries), then apply the global limit. This is
    exact when no per-partition limit was used."""
    if unique is False:
        ignore_dups = False
    out = []
    for arr in entry_arrays:
        for entry in arr:
            if ignore_dups and out and out[-1].get("value") == entry.get("value"):
                continue
            out.append(copy.deepcopy(entry))
    return out[-limit:] if limit is not None else out


def combine_append(products, sort_by_ref=None, sort_reverse=False):
    out = []
    for p in products:
        out.extend(copy.deepcopy(p))
    return _maybe_sort(out, {"sortByRef": sort_by_ref,
                             "sortReverse": sort_reverse})


class _UnknownKey:
    """Sorts after every real key, mirroring ArrayStrategy.default_key()."""
    def __lt__(self, other):
        return False

    def __gt__(self, other):
        return not isinstance(other, _UnknownKey)


def _resolve_ref(item, ref):
    if ref == "/":
        return item
    cur = item
    for part in ref.lstrip("/").split("/"):
        cur = cur[part]
    return cur


def _maybe_sort(out, opts):
    ref = opts.get("sortByRef")
    if ref is None:
        return out

    def key(item):
        try:
            return _resolve_ref(item, ref)
        except (KeyError, TypeError):
            return _UnknownKey()

    out.sort(key=key, reverse=bool(opts.get("sortReverse")))
    return out


def _deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        r = dict(a)
        for k, v in b.items():
            r[k] = _deep_merge(r[k], v) if k in r else v
        return r
    return b
