#!/usr/bin/env python3
"""Confidence-gated auto-release for encoding detection, with replayable evaluation.

Workflow
--------
1. ``make-sample`` deterministically writes a small labeled sample under
   ``data/auto_release_sample/``: human labels grouped by vendor and month,
   with duplicate exports of the same source file and a few files whose
   labels cover only a sampled window of bytes.
2. ``run`` detects every file with the local chardet, deduplicates exports by
   content hash, splits calibration/evaluation by *source file* (never by
   export row, so copies of one file never straddle the split), calibrates a
   three-zone gate, and evaluates it.

Gate (all thresholds calibrated on the calibration split only)
--------------------------------------------------------------
- confidence >= T_rel and detection is text  -> 放行 (auto-release)
- T_rev <= confidence < T_rel                -> 复核 (manual review)
- confidence < T_rev, or any binary result   -> 弃权 (abstain)

T_rel is the smallest confidence whose false-release-rate *upper bound* on
calibration data is <= TARGET_FALSE_RELEASE.  T_rev is the highest confidence
below which known-wrong releases outnumber known-correct ones.

Window-scoped labels
--------------------
A window label verifies only the first ``window_bytes`` of a file.  A released
file with a window label therefore contributes:
- to the *lower* bound of the false-release rate only if it is wrong even on
  the verified window (known false);
- to the *upper* bound if it is not known false (the unverified tail may hide
  an error).

Everything is deterministic: no RNG, no network, fixed sample content.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import chardet
from chardet.evaluation import is_correct, is_equivalent_detection

SAMPLE_DIR = Path("data/auto_release_sample")

TARGET_FALSE_RELEASE = 0.05
NAIVE_THRESHOLD = 0.80  # common ad-hoc default, compared against the calibrated one
SPLIT_MODULUS = 5  # ~1/5 of each (vendor, month) stratum goes to evaluation

# Cost of a false release, relative per vendor (acme feeds a high-cost pipeline).
COST_WEIGHT = {"acme": 10, "globex": 3, "initech": 1}

# ---------------------------------------------------------------------------
# Sample definition (deterministic; texts are repeated to exceed 512 bytes
# where a window-scoped label needs an unverified tail)
# ---------------------------------------------------------------------------

JA = "日本の四季はそれぞれに美しく、春には桜が咲き、夏には緑が深まり、秋には紅葉が山を彩り、冬には雪が静かに降り積もります。"
KO = "한국의 전통 시장에는 신선한 채소와 생선이 가득하고, 상인들의 활기찬 목소리가 골목마다 울려 퍼집니다."
ZH_CN = "中国传统节日丰富多彩，春节时家家户户贴春联、放鞭炮，中秋节则赏月吃月饼，寄托着人们对团圆的期盼。"
ZH_TW = "台灣的夜市文化聞名遐邇，各式小吃琳瑯滿目，從鹽酥雞到珍珠奶茶，吸引無數遊客前來品嚐。"
RU = "Русская литература подарила миру великих писателей: Пушкина, Толстого, Достоевского и Чехова, чьи произведения читают во всех странах."
FR = "La cuisine française est réputée dans le monde entier pour ses fromages affinés, ses vins régionaux et ses pâtisseries délicates."
DE = "Die deutsche Sprache ist bekannt für ihre langen zusammengesetzten Wörter und ihre präzisen grammatikalischen Regeln."
EN_SMART = "The “smart quotes” in this report—along with the em dash—were typed in Word and saved as Latin-1."
EN_SMART_SHORT = "café — naïve, “quoted”"
RU_CITY = "Санкт-Петербург, город на Неве."
PL_SHORT = "Zażółć gęślą jaźń."
ZH_TW_SHORT = "夜市小吃非常有名。"
JA2 = "東京の地下鉄は世界でも有数の複雑さを誇り、毎日数百万人の通勤者が利用していますが、駅員の案内は実に丁寧です。"


def _repeat(text: str, n: int) -> str:
    return " ".join([text] * n)


@dataclass(frozen=True, slots=True)
class SampleSpec:
    vendor: str
    month: str
    name: str
    label: str  # encoding label, or "binary"
    payload: bytes
    scope: str  # "full" or "window"
    window_bytes: int = 0
    exports: int = 1


def _specs() -> list[SampleSpec]:
    def enc(text: str, encoding: str) -> bytes:
        return text.encode(encoding)

    binary_blob = bytes(range(256)) * 4
    return [
        # acme — high-cost vendor
        SampleSpec("acme", "2026-09", "acme_ja_utf8.txt", "utf-8", enc(_repeat(JA, 6), "utf-8"), "full"),
        SampleSpec("acme", "2026-09", "acme_ja_sjis.txt", "shift_jis", enc(_repeat(JA, 4), "shift_jis"), "full", exports=2),
        SampleSpec("acme", "2026-09", "acme_ru_cp1251.txt", "cp1251", enc(_repeat(RU, 4), "cp1251"), "full"),
        SampleSpec("acme", "2026-09", "acme_en_cp1252.txt", "cp1252", enc(EN_SMART, "cp1252"), "full", exports=2),
        SampleSpec("acme", "2026-09", "acme_export.bin", "binary", binary_blob, "full"),
        SampleSpec("acme", "2026-10", "acme_ko_euckr.txt", "euc-kr", enc(_repeat(KO, 4), "euc-kr"), "full"),
        SampleSpec("acme", "2026-10", "acme_ru_koi8r.txt", "koi8-r", enc(_repeat(RU, 6), "koi8-r"), "window", 512),
        SampleSpec("acme", "2026-10", "acme_utf16_bom.txt", "utf-16", _repeat(DE, 3).encode("utf-16"), "full"),
        SampleSpec("acme", "2026-10", "acme_ru_koi8r_short.txt", "koi8-r", enc(RU_CITY, "koi8-r"), "full"),
        SampleSpec("acme", "2026-10", "acme_en_cp1252_short.txt", "cp1252", enc(EN_SMART_SHORT, "cp1252"), "full"),
        # globex
        SampleSpec("globex", "2026-09", "globex_zh_gb18030.txt", "gb18030", enc(_repeat(ZH_CN, 6), "gb18030"), "window", 512),
        SampleSpec("globex", "2026-09", "globex_zh_big5.txt", "big5", enc(_repeat(ZH_TW, 4), "big5"), "full"),
        SampleSpec("globex", "2026-09", "globex_fr_latin1.txt", "iso-8859-1", enc(_repeat(FR, 4), "iso-8859-1"), "full", exports=2),
        SampleSpec("globex", "2026-09", "globex_ja_cp932.txt", "cp932", enc(_repeat(JA2, 4), "cp932"), "full"),
        SampleSpec("globex", "2026-10", "globex_fr_utf8.txt", "utf-8", enc(_repeat(FR, 4), "utf-8"), "full"),
        SampleSpec("globex", "2026-10", "globex_de_utf8.txt", "utf-8", enc(_repeat(DE, 6), "utf-8"), "window", 512),
        SampleSpec("globex", "2026-10", "globex_pl_cp1250.txt", "cp1250", enc(PL_SHORT, "cp1250"), "full"),
        # initech
        SampleSpec("initech", "2026-09", "initech_en_ascii.txt", "ascii", b"Quarterly report: all systems nominal. Revenue up 3 percent. " * 4, "full", exports=2),
        SampleSpec("initech", "2026-09", "initech_ru_cp1251.txt", "cp1251", enc(_repeat(RU, 6), "cp1251"), "window", 512),
        SampleSpec("initech", "2026-09", "initech_zh_gb18030_short.txt", "gb18030", enc(ZH_CN, "gb18030"), "full"),
        SampleSpec("initech", "2026-10", "initech_mixed_utf8.txt", "utf-8", enc("Status: 正常稼働中. " * 8, "utf-8"), "full"),
        SampleSpec("initech", "2026-10", "initech_ko_euckr_short.txt", "euc-kr", enc(KO, "euc-kr"), "full"),
        SampleSpec("initech", "2026-10", "initech_zh_big5_short.txt", "big5", enc(ZH_TW_SHORT, "big5"), "full"),
    ]


def make_sample(root: Path) -> None:
    rows = []
    for spec in _specs():
        for copy in range(spec.exports):
            name = spec.name if copy == 0 else spec.name.replace(".", f"_reexport{copy}.", 1)
            month = spec.month if copy == 0 else "2026-10" if spec.month == "2026-09" else spec.month
            rel = Path(spec.vendor) / month / name
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(spec.payload)
            rows.append(
                {
                    "relpath": str(rel),
                    "vendor": spec.vendor,
                    "month": month,
                    "label": spec.label,
                    "scope": spec.scope,
                    "window_bytes": spec.window_bytes if spec.scope == "window" else "",
                }
            )
    with (root / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} labeled exports under {root}")


# ---------------------------------------------------------------------------
# Detection and labeling
# ---------------------------------------------------------------------------

STATUS_CORRECT = "correct"  # full-file verified correct
STATUS_WRONG = "wrong"  # known wrong (full label, or wrong even on the window)
STATUS_WINDOW_OK = "window_ok"  # verified on the sampled window only; tail unknown


@dataclass(frozen=True, slots=True)
class Record:
    source_id: str
    relpaths: tuple[str, ...]
    vendor: str
    label: str
    scope: str
    detected: str | None
    confidence: float
    status: str

    @property
    def is_binary_result(self) -> bool:
        return self.detected is None


def _expected(label: str) -> str | None:
    return None if label == "binary" else label


def _judge(data: bytes, label: str, scope: str, window_bytes: int, detected: str | None) -> str:
    expected = _expected(label)
    if expected is None:
        return STATUS_CORRECT if detected is None else STATUS_WRONG
    if detected is None:
        return STATUS_WRONG
    if is_correct(expected, detected):
        return STATUS_CORRECT if scope == "full" else STATUS_WINDOW_OK
    check = data if scope == "full" else data[:window_bytes]
    if is_equivalent_detection(check, expected, detected):
        return STATUS_CORRECT if scope == "full" else STATUS_WINDOW_OK
    return STATUS_WRONG


def detect_all_records(root: Path) -> list[Record]:
    with (root / "labels.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    payloads: dict[str, bytes] = {}
    for row in rows:
        data = (root / row["relpath"]).read_bytes()
        source_id = hashlib.sha256(data).hexdigest()
        by_source[source_id].append(row)
        payloads[source_id] = data
    records = []
    for source_id, group in sorted(by_source.items()):
        data = payloads[source_id]
        result = chardet.detect(data)
        labels = {row["label"] for row in group}
        scopes = {(row["scope"], row["window_bytes"]) for row in group}
        if len(labels) != 1 or len(scopes) != 1:
            msg = f"conflicting labels across duplicate exports of {source_id[:12]}"
            raise ValueError(msg)
        row = group[0]
        window = int(row["window_bytes"]) if row["window_bytes"] else 0
        records.append(
            Record(
                source_id=source_id,
                relpaths=tuple(sorted(r["relpath"] for r in group)),
                vendor=row["vendor"],
                label=row["label"],
                scope=row["scope"],
                detected=result["encoding"],
                confidence=result["confidence"],
                status=_judge(data, row["label"], row["scope"], window, result["encoding"]),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Split: stratified by (vendor, month), unit = source file, deterministic
# ---------------------------------------------------------------------------


def assign_split(records: list[Record]) -> set[str]:
    strata: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for rec in records:
        month = rec.relpaths[0].split("/")[1]
        strata[(rec.vendor, month)].append(rec)
    eval_ids: set[str] = set()
    for members in strata.values():
        members.sort(key=lambda r: hashlib.sha256(b"split|" + r.source_id.encode()).hexdigest())
        eval_ids.update(m.source_id for i, m in enumerate(members) if i % SPLIT_MODULUS == 0)
    return eval_ids


# ---------------------------------------------------------------------------
# Gate and metrics
# ---------------------------------------------------------------------------

ZONE_RELEASE = "release"
ZONE_REVIEW = "review"
ZONE_ABSTAIN = "abstain"


def zone_of(rec: Record, t_rev: float, t_rel: float) -> str:
    if rec.is_binary_result:
        return ZONE_REVIEW  # binary is never auto-released as text
    if rec.confidence >= t_rel:
        return ZONE_RELEASE
    if rec.confidence >= t_rev:
        return ZONE_REVIEW
    return ZONE_ABSTAIN


def fr_bounds(released: list[Record]) -> tuple[float, float]:
    """Determinable [lower, upper] bounds of the false-release rate."""
    if not released:
        return (0.0, 0.0)
    known_false = sum(1 for r in released if r.status == STATUS_WRONG)
    unknown = sum(1 for r in released if r.status == STATUS_WINDOW_OK)
    n = len(released)
    return (known_false / n, (known_false + unknown) / n)


def cost_weighted_fr(released: list[Record]) -> tuple[float, float]:
    if not released:
        return (0.0, 0.0)
    total = sum(COST_WEIGHT[r.vendor] for r in released)
    known = sum(COST_WEIGHT[r.vendor] for r in released if r.status == STATUS_WRONG)
    unknown = sum(COST_WEIGHT[r.vendor] for r in released if r.status == STATUS_WINDOW_OK)
    return (known / total, (known + unknown) / total)


def calibrate(cal: list[Record]) -> tuple[float, float]:
    candidates = sorted({r.confidence for r in cal})
    t_rel = 1.0
    for t in candidates:
        released = [r for r in cal if not r.is_binary_result and r.confidence >= t]
        if released and fr_bounds(released)[1] <= TARGET_FALSE_RELEASE:
            t_rel = t
            break
    t_rev = 0.0
    for t in candidates:
        below = [r for r in cal if r.confidence < t]
        known_correct = sum(1 for r in below if r.status == STATUS_CORRECT)
        known_wrong = sum(1 for r in below if r.status == STATUS_WRONG)
        if known_wrong > known_correct:
            t_rev = max(t_rev, t)
    return (t_rev, t_rel)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt_bounds(bounds: tuple[float, float]) -> str:
    return f"[{bounds[0]:.3f}, {bounds[1]:.3f}]"


def _table(lines: list[str], header: list[str], rows: list[list[object]]) -> None:
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join("---" for _ in header) + "|")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")


def _eval_at(records: list[Record], t_rel: float) -> dict[str, object]:
    released = [r for r in records if not r.is_binary_result and r.confidence >= t_rel]
    correct = sum(1 for r in released if r.status == STATUS_CORRECT)
    return {
        "n_total": len(records),
        "n_released": len(released),
        "overall_accuracy": (correct / len(records)) if records else 0.0,
        "release_accuracy": (correct / len(released)) if released else 0.0,
        "fr": fr_bounds(released),
        "acme_false": sum(1 for r in released if r.vendor == "acme" and r.status == STATUS_WRONG),
        "acme_released": sum(1 for r in released if r.vendor == "acme"),
    }


def report(root: Path, records: list[Record], eval_ids: set[str]) -> str:
    cal = [r for r in records if r.source_id not in eval_ids]
    ev = [r for r in records if r.source_id in eval_ids]
    t_rev, t_rel = calibrate(cal)

    lines: list[str] = []
    lines.append("# Confidence-gate report")
    lines.append("")
    lines.append(f"chardet version: {chardet.__version__} — sample: `{root}`")
    lines.append("")

    n_exports = sum(len(r.relpaths) for r in records)
    dup = sum(1 for r in records if len(r.relpaths) > 1)
    lines.append("## 1. Sample")
    lines.append("")
    lines.append(
        f"{n_exports} labeled exports collapse to {len(records)} source files "
        f"({dup} sources exported more than once); "
        f"{sum(1 for r in records if r.scope == 'window')} sources carry window-scoped labels."
    )
    lines.append("")

    lines.append("## 2. Split (unit = source file, stratified by vendor x month)")
    lines.append("")
    lines.append(
        f"calibration: {len(cal)} sources, evaluation: {len(ev)} sources. "
        "Copies of one source share a content hash and therefore always land on the same side."
    )
    lines.append("")

    lines.append("## 3. Detection results (per source file)")
    lines.append("")
    _table(
        lines,
        ["source", "vendor", "label", "scope", "detected", "conf", "status", "side"],
        [
            [
                r.relpaths[0].split("/")[-1] + (f" (+{len(r.relpaths) - 1} dup)" if len(r.relpaths) > 1 else ""),
                r.vendor,
                r.label,
                r.scope,
                r.detected or "binary",
                f"{r.confidence:.3f}",
                r.status,
                "eval" if r.source_id in eval_ids else "cal",
            ]
            for r in sorted(records, key=lambda r: (r.vendor, r.relpaths[0]))
        ],
    )
    lines.append("")

    lines.append("## 4. Calibrated gate (calibration split only)")
    lines.append("")
    lines.append(f"- 放行 release: `confidence >= {t_rel:.3f}` and result is text")
    lines.append(f"- 复核 review: `{t_rev:.3f} <= confidence < {t_rel:.3f}`, or any binary result")
    lines.append(f"- 弃权 abstain: `confidence < {t_rev:.3f}`")
    cal_rel = [r for r in cal if zone_of(r, t_rev, t_rel) == ZONE_RELEASE]
    lines.append(
        f"- calibration false-release-rate bounds at T_rel: {_fmt_bounds(fr_bounds(cal_rel))} "
        f"(target upper bound <= {TARGET_FALSE_RELEASE})"
    )
    lines.append("")

    lines.append("## 5. Evaluation (held-out sources)")
    lines.append("")
    zones = defaultdict(list)
    for r in ev:
        zones[zone_of(r, t_rev, t_rel)].append(r)
    lines.append(
        f"放行 {len(zones[ZONE_RELEASE])} / 复核 {len(zones[ZONE_REVIEW])} / 弃权 {len(zones[ZONE_ABSTAIN])} "
        f"of {len(ev)} held-out sources."
    )
    lines.append("")
    ev_rel = zones[ZONE_RELEASE]
    lines.append(f"- false-release-rate bounds: {_fmt_bounds(fr_bounds(ev_rel))}")
    lines.append(f"- cost-weighted false-release-rate bounds: {_fmt_bounds(cost_weighted_fr(ev_rel))}")
    lines.append("")
    _table(
        lines,
        ["vendor", "released", "known false", "window-unknown", "FR bounds"],
        [
            [
                v,
                len(rs),
                sum(1 for r in rs if r.status == STATUS_WRONG),
                sum(1 for r in rs if r.status == STATUS_WINDOW_OK),
                _fmt_bounds(fr_bounds(rs)),
            ]
            for v, rs in sorted(_group_by_vendor(ev_rel).items())
        ],
    )
    lines.append("")

    lines.append("## 6. Two thresholds, same accuracy, different high-cost sources")
    lines.append("")
    naive = _eval_at(ev, NAIVE_THRESHOLD)
    gated = _eval_at(ev, t_rel)
    lines.append(
        f"calibrated T={t_rel:.3f}: released {gated['n_released']}/{gated['n_total']}, "
        f"overall accuracy {gated['overall_accuracy']:.3f}, "
        f"acme false releases {gated['acme_false']}/{gated['acme_released']} | "
        f"naive T={NAIVE_THRESHOLD:.2f}: released {naive['n_released']}/{naive['n_total']}, "
        f"overall accuracy {naive['overall_accuracy']:.3f}, "
        f"acme false releases {naive['acme_false']}/{naive['acme_released']}"
    )
    lines.append("")
    pair = _find_tie_pair(ev)
    illustrative = False
    if pair is None:
        pair = _find_tie_pair(records)
        illustrative = pair is not None
    if pair is None:
        lines.append(
            "No pair of thresholds ties on overall accuracy while differing on acme false "
            "releases — neither on the held-out split nor on the full sample."
        )
        lines.append("")
        _labeling_advice(lines, records, ev, t_rel)
    else:
        t1, t2, s1, s2 = pair
        if illustrative:
            lines.append(
                "The held-out split is too small to exhibit the tie; the example below is "
                f"computed on all {len(records)} labeled sources to demonstrate the failure "
                "mode. It illustrates the metric pathology only — threshold selection above "
                "used calibration data exclusively."
            )
            lines.append("")
        lines.append(
            f"T1={t1:.3f} vs T2={t2:.3f}: identical overall accuracy "
            f"{s1['overall_accuracy']:.3f} ({s1['n_released']} resp. {s2['n_released']} releases), "
            f"but acme false releases {s1['acme_false']} vs {s2['acme_false']} "
            f"(of {s1['acme_released']} resp. {s2['acme_released']} acme releases)."
        )
        lo, hi = min(t1, t2), max(t1, t2)
        band = [r for r in records if lo <= r.confidence < hi]
        lines.append("")
        lines.append(
            f"The decision differs only for sources with confidence in [{lo:.3f}, {hi:.3f}). "
            f"{len(band)} sample source(s) sit in this band:"
        )
        for r in band:
            lines.append(
                f"- `{r.relpaths[0]}` vendor={r.vendor} conf={r.confidence:.3f} "
                f"scope={r.scope} status={r.status}"
            )
        lines.append("")
        lines.append(
            "Overall accuracy is blind here because a false release does not lower it — the "
            "wrong file simply joins the released set while the correct count stays flat. "
            "Only a per-vendor, cost-weighted false-release metric separates T1 from T2."
        )
        lines.append("")
        _labeling_advice(lines, records, ev, t_rel, band=(lo, hi))
    lines.append("")
    return "\n".join(lines)


def _labeling_advice(
    lines: list[str],
    records: list[Record],
    ev: list[Record],
    t_rel: float,
    band: tuple[float, float] | None = None,
) -> None:
    lo, hi = band if band else (0.5, t_rel)
    ev_acme = [r for r in ev if r.vendor == "acme"]
    ev_acme_released = [r for r in ev_acme if r.confidence >= t_rel]
    window_acme = [r for r in records if r.vendor == "acme" and r.scope == "window"]
    lines.append("判别力补核建议 (which additional labels would discriminate):")
    lines.append(
        f"- held-out acme sources: {len(ev_acme)}, of which {len(ev_acme_released)} fall "
        f"above T_rel={t_rel:.3f} — too few to rank thresholds on the high-cost vendor; "
        f"label more acme files with confidence in [{lo:.3f}, {hi:.3f}), the band where "
        "the compared thresholds disagree"
    )
    lines.append(
        f"- upgrade window-scoped acme labels to full-file labels first "
        f"({len(window_acme)} in sample: "
        + (", ".join(f"`{r.relpaths[0]}`" for r in window_acme) or "none")
        + ") — they currently feed only the upper bound, never the verified count"
    )
    lines.append(
        "- labels outside the disagreement band cannot separate the two thresholds: "
        "both make identical decisions there"
    )


def _group_by_vendor(records: list[Record]) -> dict[str, list[Record]]:
    grouped: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        grouped[r.vendor].append(r)
    return grouped


def _find_tie_pair(ev: list[Record]) -> tuple[float, float, dict[str, object], dict[str, object]] | None:
    candidates = sorted({r.confidence for r in ev} | {0.5, 0.8, 0.9, 0.95, 0.99})
    stats = {t: _eval_at(ev, t) for t in candidates}
    best = None
    for i, t1 in enumerate(candidates):
        for t2 in candidates[i + 1 :]:
            s1, s2 = stats[t1], stats[t2]
            if not s1["n_released"] or not s2["n_released"]:
                continue
            if s1["overall_accuracy"] != s2["overall_accuracy"]:
                continue
            diff = abs(s1["acme_false"] - s2["acme_false"])
            if diff and (best is None or diff > abs(best[2]["acme_false"] - best[3]["acme_false"])):
                best = (t1, t2, s1, s2)
    return best


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["make-sample", "run", "all"], nargs="?", default="all")
    parser.add_argument("--sample-dir", type=Path, default=SAMPLE_DIR)
    args = parser.parse_args()

    if args.command in {"make-sample", "all"}:
        make_sample(args.sample_dir)
    if args.command in {"run", "all"}:
        if not (args.sample_dir / "labels.csv").exists():
            print("sample missing; run `make-sample` first", file=sys.stderr)
            sys.exit(1)
        records = detect_all_records(args.sample_dir)
        eval_ids = assign_split(records)
        text = report(args.sample_dir, records, eval_ids)
        args.sample_dir.mkdir(parents=True, exist_ok=True)
        (args.sample_dir / "report.md").write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
