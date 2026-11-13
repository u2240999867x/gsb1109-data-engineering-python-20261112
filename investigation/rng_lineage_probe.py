"""RNG lineage probe: locate why re-run Mimesis sample tables diverge.

Reproduces two failure trajectories that look identical when only the
exported rows are inspected, and separates them with two extra
observations per attempt:

    1. the identifier of the random source that produced each value
    2. the attempt number (including skipped attempts) and draw count

Trajectory A: two tables share one ``Field`` instance; sequential
generation is replaced by interleaved generation.
Trajectory B: each table has its own ``Field``, but the schema callback
skips rows (returns ``None``) and a key function draws extra randoms.

Run with:  .venv/bin/python investigation/rng_lineage_probe.py
"""

import collections
import contextlib
import json
from pathlib import Path

from mimesis import Field, Schema
from mimesis.keys import maybe
from mimesis.providers.base import BaseProvider
from mimesis.random import Random

OUT_DIR = Path(__file__).parent / "out"

ORDERS_SEED = 42
CUSTOMERS_SEED = 43
SHARED_SEED = 42
ROWS_PER_TABLE = 6


class DrawTracer:
    """Counts primitive RNG draws and attributes them to attempts.

    Patches ``Random.random`` and ``Random.getrandbits`` (the two
    primitive outputs every provider method funnels into) for the
    duration of a ``with`` block.
    """

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.totals: collections.Counter = collections.Counter()
        self._rng_labels: dict[int, str] = {}
        self._fields: list[tuple[str, Field]] = []
        self._current: dict | None = None

    def register_field(self, label: str, field: Field) -> None:
        self._fields.append((label, field))

    def _label_for(self, rng: Random) -> str:
        rid = id(rng)
        if rid not in self._rng_labels:
            label = f"unknown#{rid % 10000}"
            for field_label, field in self._fields:
                generic = field._generic
                if generic.random is rng:
                    label = f"{field_label}/generic"
                    break
                for attr, value in vars(generic).items():
                    if isinstance(value, BaseProvider) and value.random is rng:
                        label = f"{field_label}/{attr.lstrip('_')}"
                        break
                else:
                    continue
                break
            self._rng_labels[rid] = label
        return self._rng_labels[rid]

    def _note(self, rng: Random) -> None:
        if self._current is not None:
            self._current["draws"] += 1
            self._current["sources"].add(self._label_for(rng))
            self.totals[self._current["table"]] += 1

    def __enter__(self) -> "DrawTracer":
        orig_random = Random.random
        orig_getrandbits = Random.getrandbits
        tracer = self

        def traced_random(self_rng: Random) -> float:
            tracer._note(self_rng)
            return orig_random(self_rng)

        def traced_getrandbits(self_rng: Random, k: int) -> int:
            tracer._note(self_rng)
            return orig_getrandbits(self_rng, k)

        Random.random = traced_random  # type: ignore[method-assign]
        Random.getrandbits = traced_getrandbits  # type: ignore[method-assign]
        self._restore = (orig_random, orig_getrandbits)
        return self

    def __exit__(self, *exc: object) -> None:
        Random.random, Random.getrandbits = self._restore  # type: ignore[method-assign]

    @contextlib.contextmanager
    def attempt(self, table: str, attempt_no: int):
        rec = {
            "table": table,
            "attempt": attempt_no,
            "pk": None,
            "draws": 0,
            "sources": set(),
            "outcome": "emitted",
        }
        self._current = rec
        try:
            yield rec
        finally:
            rec["sources"] = sorted(rec["sources"])
            self.records.append(rec)
            self._current = None


def make_schema_fn(tracer: DrawTracer, table: str, pk_prefix: str, pk_start: int, body):
    """Wrap a row body so every attempt is traced.

    The business pk is assigned per *emitted* row (as an upstream
    sequence would be), while the attempt number counts every call,
    including skipped ones.
    """
    state = {"attempt": 0, "emitted": 0}

    def schema_fn():
        attempt_no = state["attempt"]
        state["attempt"] += 1
        with tracer.attempt(table, attempt_no) as rec:
            row = body()
            if row is None:
                rec["outcome"] = "skipped"
                return None
            pk = f"{pk_prefix}-{pk_start + state['emitted']}"
            state["emitted"] += 1
            rec["pk"] = pk
            row["pk"] = pk
            return row

    return schema_fn


def interleave(schema_fns: dict[str, callable], rows_per_table: int) -> dict[str, list[dict]]:
    """Generate one row per table in round-robin order."""
    out: dict[str, list[dict]] = {name: [] for name in schema_fns}
    schemas = {name: Schema(schema=fn, iterations=1) for name, fn in schema_fns.items()}
    for _ in range(rows_per_table):
        for name, schema in schemas.items():
            out[name].extend(schema.create())
    return out


def sequential(schema_fns: dict[str, callable], rows_per_table: int) -> dict[str, list[dict]]:
    """Generate each table fully before starting the next."""
    out = {}
    for name, fn in schema_fns.items():
        out[name] = Schema(schema=fn, iterations=rows_per_table).create()
    return out


# ---------------------------------------------------------------- row bodies

def orders_body(field: Field):
    return {
        "customer": field("full_name"),
        "amount": field("integer_number", start=1, end=500),
    }


def customers_body(field: Field):
    return {
        "name": field("full_name"),
        "score": field("integer_number", start=1, end=100),
    }


def make_orders_body_flaky(field: Field, gate_from_attempt: int):
    """Same columns as ``orders_body`` plus a mid-stream quality gate.

    From ``gate_from_attempt`` on, each attempt first draws a gate
    value; ~20% of attempts are skipped (return ``None``) *after*
    consuming draws, and the amount column applies a key function that
    draws again from the field's generic random. Models a validation
    rule introduced part-way through a batch.
    """
    state = {"attempt": 0}

    def body():
        attempt_no = state["attempt"]
        state["attempt"] += 1
        if attempt_no >= gate_from_attempt:
            gate = field("integer_number", start=1, end=10)
            if gate <= 2:
                return None
            amount = field("integer_number", start=1, end=500, key=maybe("N/A", 0.3))
        else:
            amount = field("integer_number", start=1, end=500)
        return {
            "customer": field("full_name"),
            "amount": amount,
        }

    return body


# ---------------------------------------------------------------- reporting

def diff_summary(base: dict[str, list[dict]], new: dict[str, list[dict]]) -> dict[str, str]:
    summary = {}
    for table in base:
        b, n = base[table], new[table]
        matching = sum(1 for rb, rn in zip(b, n) if rb == rn)
        first_diff = next((i for i, (rb, rn) in enumerate(zip(b, n)) if rb != rn), None)
        summary[table] = (
            f"rows {len(b)} vs {len(n)}, identical {matching}/{len(b)}"
            + (f", first divergence at row {first_diff}" if first_diff is not None else "")
        )
    return summary


def print_export_view(title: str, summary: dict[str, str]) -> None:
    print(f"  [{title}] exported rows only:")
    for table, line in summary.items():
        print(f"    {table:10s} {line}")


def print_trace_view(tracer: DrawTracer, tables: tuple[str, ...]) -> None:
    print("  trace view (per attempt):")
    for table in tables:
        recs = [r for r in tracer.records if r["table"] == table]
        skipped = [r["attempt"] for r in recs if r["outcome"] == "skipped"]
        draws = [r["draws"] for r in recs]
        sources = sorted({s for r in recs for s in r["sources"]})
        print(f"    {table:10s} total draws={tracer.totals[table]:3d} "
              f"draws/attempt={draws} skipped attempts={skipped or 'none'}")
        print(f"    {'':10s} rng sources: {sources}")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    all_traces: dict[str, list[dict]] = {}

    print("=" * 78)
    print("TRAJECTORY A: one shared Field, sequential vs interleaved generation")
    print("=" * 78)

    with DrawTracer() as tracer_a1:
        shared = Field(seed=SHARED_SEED)
        tracer_a1.register_field("F-shared", shared)
        fns = {
            "orders": make_schema_fn(tracer_a1, "orders", "ORD", 1001,
                                     lambda: orders_body(shared)),
            "customers": make_schema_fn(tracer_a1, "customers", "CUST", 2001,
                                        lambda: customers_body(shared)),
        }
        a_seq = sequential(fns, ROWS_PER_TABLE)
    all_traces["A1_sequential"] = tracer_a1.records

    with DrawTracer() as tracer_a2:
        shared = Field(seed=SHARED_SEED)
        tracer_a2.register_field("F-shared", shared)
        fns = {
            "orders": make_schema_fn(tracer_a2, "orders", "ORD", 1001,
                                     lambda: orders_body(shared)),
            "customers": make_schema_fn(tracer_a2, "customers", "CUST", 2001,
                                        lambda: customers_body(shared)),
        }
        a_alt = interleave(fns, ROWS_PER_TABLE)
    all_traces["A2_interleaved"] = tracer_a2.records

    print_export_view("A", diff_summary(a_seq, a_alt))
    print("  -> same seed, same row counts, prefix matches, then values shift")
    print_trace_view(tracer_a1, ("orders", "customers"))
    print_trace_view(tracer_a2, ("orders", "customers"))
    conserved = (tracer_a1.totals["orders"] + tracer_a1.totals["customers"]) == (
        tracer_a2.totals["orders"] + tracer_a2.totals["customers"])
    print(f"  -> cross-table draw total conserved between runs: {conserved}")
    print(f"  -> both tables draw from the SAME rng sources: "
          f"{sorted({s for r in tracer_a2.records for s in r['sources']})}")

    print()
    print("=" * 78)
    print("TRAJECTORY B: per-table Fields, callback skips rows / draws extra")
    print("=" * 78)

    with DrawTracer() as tracer_b1:
        o_field, c_field = Field(seed=ORDERS_SEED), Field(seed=CUSTOMERS_SEED)
        tracer_b1.register_field("F-orders", o_field)
        tracer_b1.register_field("F-customers", c_field)
        fns = {
            "orders": make_schema_fn(tracer_b1, "orders", "ORD", 1001,
                                     lambda: orders_body(o_field)),
            "customers": make_schema_fn(tracer_b1, "customers", "CUST", 2001,
                                        lambda: customers_body(c_field)),
        }
        b_clean = sequential(fns, ROWS_PER_TABLE)
    all_traces["B1_clean"] = tracer_b1.records

    with DrawTracer() as tracer_b2:
        o_field, c_field = Field(seed=ORDERS_SEED), Field(seed=CUSTOMERS_SEED)
        tracer_b2.register_field("F-orders", o_field)
        tracer_b2.register_field("F-customers", c_field)
        flaky_orders = make_orders_body_flaky(o_field, gate_from_attempt=2)
        fns = {
            "orders": make_schema_fn(tracer_b2, "orders", "ORD", 1001, flaky_orders),
            "customers": make_schema_fn(tracer_b2, "customers", "CUST", 2001,
                                        lambda: customers_body(c_field)),
        }
        b_flaky = sequential(fns, ROWS_PER_TABLE)
    all_traces["B2_flaky"] = tracer_b2.records

    print_export_view("B", diff_summary(b_clean, b_flaky))
    print("  -> same seed, same row counts, prefix matches, then values shift")
    print("     (indistinguishable from trajectory A by exported rows alone)")
    print_trace_view(tracer_b1, ("orders", "customers"))
    print_trace_view(tracer_b2, ("orders", "customers"))
    print(f"  -> orders draw total grows ({tracer_b1.totals['orders']} -> "
          f"{tracer_b2.totals['orders']}), customers untouched; "
          f"rng sources stay per-table disjoint")

    print()
    print("=" * 78)
    print("SEPARATING THE TWO WITH MINIMAL EXTRA OBSERVATIONS")
    print("=" * 78)
    a2_sources = {t: sorted({s for r in tracer_a2.records if r["table"] == t for s in r["sources"]})
                  for t in ("orders", "customers")}
    b2_sources = {t: sorted({s for r in tracer_b2.records if r["table"] == t for s in r["sources"]})
                  for t in ("orders", "customers")}
    print(f"  observation 1 (rng source id per row): A shares sources across tables "
          f"-> {a2_sources['orders'] == a2_sources['customers']}; "
          f"B keeps them disjoint -> {b2_sources['orders'] != b2_sources['customers']}")
    b2_skips = [r["attempt"] for r in tracer_b2.records
                if r["table"] == "orders" and r["outcome"] == "skipped"]
    print(f"  observation 2 (attempt number + skipped flag): A has dense attempts "
          f"with no skips; B has skipped attempts {b2_skips} and per-table draw "
          f"totals that are stable for the untouched table "
          f"({tracer_b1.totals['customers']} == {tracer_b2.totals['customers']}) "
          f"but inflated for orders "
          f"({tracer_b1.totals['orders']} -> {tracer_b2.totals['orders']})")

    print()
    print("=" * 78)
    print("REPLAY BOUNDARY: per-table independent backfill")
    print("=" * 78)

    # Backfill orders rows 2..4 of the clean baseline (B1) from scratch.
    replay_field = Field(seed=ORDERS_SEED)
    replayed = []
    for attempt_no in range(5):
        row = orders_body(replay_field)
        if attempt_no >= 2:
            row["pk"] = f"ORD-{1001 + attempt_no}"
            replayed.append(row)
    expected = b_clean["orders"][2:5]
    print(f"  replay attempts 0..4, emit 2..4, dedicated Field(seed={ORDERS_SEED}): "
          f"matches baseline -> {replayed == expected}")

    # Replaying B2 by emitted-row index (ignoring skips) fails.
    naive_field = Field(seed=ORDERS_SEED)
    naive = Schema(schema=lambda: orders_body(naive_field), iterations=ROWS_PER_TABLE).create()
    strip = lambda rows: [{k: v for k, v in r.items() if k != "pk"} for r in rows]
    print(f"  naive replay of B2 (skip-unaware, row-index based): matches B2 -> "
          f"{strip(naive) == strip(b_flaky['orders'])}")

    # Replaying B2 by attempt index with the same code version works.
    attempt_field = Field(seed=ORDERS_SEED)
    attempt_body = make_orders_body_flaky(attempt_field, gate_from_attempt=2)
    attempt_rows = []
    for _ in range(20):
        row = attempt_body()
        if row is not None:
            attempt_rows.append(row)
        if len(attempt_rows) == ROWS_PER_TABLE:
            break
    print(f"  attempt-aware replay of B2 (same code, skip pattern re-derived): "
          f"matches B2 -> {strip(attempt_rows) == strip(b_flaky['orders'])}")

    # Schema(seed=...) is not a replay mechanism.
    s_field = Field(seed=ORDERS_SEED)
    schema = Schema(schema=lambda: orders_body(s_field), iterations=3, seed=ORDERS_SEED)
    run1 = schema.create()
    run2 = schema.create()
    print(f"  Schema(seed=...) re-run without Field.reseed: identical -> {run1 == run2} "
          f"(seed only lands in SchemaContext, never reseeds the Field)")

    # MissingSeed is fundamentally unrecoverable.
    m1 = Schema(schema=lambda: orders_body(Field()), iterations=2).create()
    m2 = Schema(schema=lambda: orders_body(Field()), iterations=2).create()
    print(f"  MissingSeed batch re-run: identical -> {m1 == m2} (system entropy, unrecoverable)")

    trace_path = OUT_DIR / "traces.json"
    trace_path.write_text(json.dumps(all_traces, indent=2), encoding="utf-8")
    print(f"\nfull attempt-level traces written to {trace_path}")


if __name__ == "__main__":
    main()
