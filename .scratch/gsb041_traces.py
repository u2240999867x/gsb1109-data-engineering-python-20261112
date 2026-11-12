"""Verify lazy vs snapshot selection in jsonpath_ng filter updates."""
import sys

sys.path.insert(0, "src")
from jsonpath_ng.ext import parse


def fresh():
    # A, B, C are original rows; A and B are equal-valued twins.
    return {
        "threshold": 2,
        "rows": [{"v": 2}, {"v": 2}, {"v": 1}],  # A, B, C
    }


EXPR = "$.rows[?(@.v >= $.threshold)]"

print("=== 1. Terminal filter update: predicate re-evaluated against LIVE root ===")
data = fresh()
log = []
data_root = data


def fn_raise_threshold(item, data, key):
    # Minimal observation: position (key) + context (live list, live root).
    log.append((key, len(data), id(item), data_root["threshold"]))
    item["hit"] = True
    data_root["threshold"] = 3  # update changes the threshold mid-loop


parse(EXPR).update(data, fn_raise_threshold)
print("final:", data["rows"], "threshold:", data["threshold"])
print("callback saw (key, len, id, threshold):", log)
# A marked (2>=2), then threshold=3, so twin B (2>=3 fails) is skipped.

print()
print("=== 2. Same rule, but find-first (identity fixed up front) ===")
data = fresh()
data_root = data
log2 = []
matches = parse(EXPR).find(data)  # snapshot: A, B both qualify at threshold=2
print("snapshot matches at paths:", [str(m.full_path) for m in matches])
for m in matches:
    log2.append((str(m.full_path), len(data["rows"])))
    m.value["hit"] = True
    data_root["threshold"] = 3
print("final:", data["rows"], "threshold:", data["threshold"])
print("applied to:", log2)

print()
print("=== 3. Non-terminal filter (`...].hit`) also snapshots via find ===")
data = fresh()
for row in data["rows"]:
    row["hit"] = False
data_root = data


def fn_hit_and_raise(old, row, field):
    row["hit"] = True
    data_root["threshold"] = 3  # too late: matches were fixed by find()


parse(EXPR + ".hit").update(data, fn_hit_and_raise)  # Child.update -> left.find() once
print("final:", data["rows"], "threshold:", data["threshold"])

print()
print("=== 4. Two traces, SAME final array, DIFFERENT source rows ===")
EXPR2 = "$.rows[?(@.v >= $.threshold)]"


def fresh2():
    return {"threshold": 2, "rows": [{"v": 2}, {"v": 2}]}  # twins A, B


def fn_mark_and_drop_next(item, data, key):
    item["hit"] = True
    if key + 1 < len(data):
        del data[key + 1]  # update deletes a neighbor


# Trace A: actual entry point, terminal filter, lazy enumerate over live list.
data_a = fresh2()
audit_a = []


def fn_a(item, data, key):
    audit_a.append(("key", key, "len", len(data), "id", id(item)))
    fn_mark_and_drop_next(item, data, key)


parse(EXPR2).update(data_a, fn_a)
print("trace A final:", data_a["rows"], "audit:", audit_a)

# Trace B: business expectation — fix original row identity first.
data_b = fresh2()
audit_b = []
matches = parse(EXPR2).find(data_b)
for m in matches:
    audit_b.append((str(m.full_path), id(m.value)))
    m.value["hit"] = True
    # same neighbor deletion, applied on the live list by current position
    rows = data_b["rows"]
    for i, row in enumerate(rows):
        if row is m.value and i + 1 < len(rows):
            del rows[i + 1]
            break
print("trace B final:", data_b["rows"], "audit:", audit_b)
print("finals identical:", data_a["rows"] == data_b["rows"])
