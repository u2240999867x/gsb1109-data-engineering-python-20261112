# chardet-rewrite Performance Comparison

Benchmarked on 2026-03-02 against 2179 test files using
`scripts/compare_detectors.py`. All detectors run in isolated temporary
venvs created with ``uv`` for fair, consistent measurement. All detectors
are evaluated with the same equivalence rules: directional superset and
bidirectional groups, plus decoded-output equivalence (base-letter
matching after NFKD normalization and currency/euro symbol equivalence).
Directional supersets include ISO-to-Windows mappings (e.g., detecting
windows-1252 when iso-8859-1 is expected counts as correct).

The chardet-rewrite numbers below are pure Python (CPython 3.12). See
"mypyc Compilation" section below for compiled performance.

## Overall Accuracy

| Detector | Correct | Accuracy | Detection Time |
|---|---|---|---|
| **chardet-rewrite** | **2110/2179** | **96.8%** | 6.51s |
| chardet 6.0.0 | 2060/2179 | 94.5% | 181.30s |
| charset-normalizer | 1942/2179 | 89.1% | 33.08s |
| charset-normalizer (pure) | 1942/2179 | 89.1% | 50.02s |
| chardet 5.2.0 | 1485/2179 | 68.2% | 44.15s |
| cchardet | 1245/2179 | 57.1% | 1.21s |

The rewrite leads all detectors on accuracy: **+2.3pp** vs chardet 6.0.0,
**+7.7pp** vs charset-normalizer, **+28.6pp** vs chardet 5.2.0, and
**+39.7pp** vs cchardet. Speed-wise, the rewrite is **28x faster** than
chardet 6.0.0, **5.1x faster** than charset-normalizer, and **6.8x faster**
than chardet 5.2.0.

cchardet (C/C++ uchardet engine via faust-cchardet) is the fastest at 1.21s
but has the lowest accuracy at 57.1% -- over 39pp behind the rewrite. It
lacks support for many encodings the rewrite handles (EBCDIC, Mac encodings,
Baltic, etc.).

The two charset-normalizer variants produce identical accuracy (1942/2179).
The pure-Python build is 1.5x slower (50.02s vs 33.08s).

## Language Detection Accuracy

The rewrite returns a `language` field alongside the detected encoding.
Language detection uses a three-tier approach:

1. **Tier 1**: Hardcoded mapping for 41 single-language encodings
   (e.g. Big5->Chinese, EUC-JP->Japanese, ISO-8859-7->Greek)
2. **Tier 2**: Statistical bigram scoring for multi-language encodings
   (e.g. windows-1252, ISO-8859-5) -- reuses the encoding's language
   model variants
3. **Tier 3**: Decode to UTF-8 and score against 48 UTF-8 byte bigram
   language models -- universal fallback for all encodings including
   UTF-8, UTF-16, and UTF-32

| Detector | Correct | Accuracy |
|---|---|---|
| **chardet-rewrite** | **1964/2171** | **90.5%** |
| chardet 6.0.0 | 1016/2171 | 46.8% |
| chardet 5.2.0 | 411/2171 | 18.9% |
| charset-normalizer | 0/2171 | 0.0% |
| cchardet | 0/2171 | 0.0% |

The rewrite leads on language detection by a wide margin: **+43.7pp** vs
chardet 6.0.0, **+71.6pp** vs chardet 5.2.0. charset-normalizer and
cchardet do not report language at all.

Every detected file now receives a language. The 207 wrong-language
cases are primarily confusable language pairs within the same script
(e.g. Danish/Norwegian, French/Spanish for English text, Belarusian/
Bulgarian for Cyrillic). The UTF-8 language scoring uses the first
2 KB of data to keep overhead low while maintaining discrimination
across all 48 languages.

## Detection Runtime Distribution

| Detector | Total | Mean | Median | p90 | p95 |
|---|---|---|---|---|---|
| cchardet | 1,203ms | 0.55ms | 0.07ms | 0.65ms | 0.92ms |
| chardet-rewrite | 6,493ms | 2.98ms | 1.13ms | 5.32ms | 6.15ms |
| chardet 5.2.0 | 44,137ms | 20.26ms | 2.85ms | 12.21ms | 23.02ms |
| charset-normalizer | 33,061ms | 15.17ms | 4.67ms | 49.31ms | 70.71ms |
| charset-normalizer (pure) | 49,998ms | 22.95ms | 7.15ms | 75.53ms | 107.11ms |
| chardet 6.0.0 | 181,270ms | 83.19ms | 16.32ms | 122.32ms | 319.77ms |

The rewrite has the lowest median latency (1.13ms) among all pure-Python
detectors, matching cchardet's order of magnitude despite being pure Python.
Its p95 (6.15ms) is well below charset-normalizer's (71ms) and chardet
6.0.0's (320ms).

## Startup & Memory

| Detector | Import Time | Traced Import | Traced Peak | RSS After |
|---|---|---|---|---|
| cchardet | 0.340s | 23.6 KiB | 27.2 KiB | 62.8 MiB |
| chardet-rewrite | 0.000s | 96 B | 22.5 MiB | 96.1 MiB |
| chardet 6.0.0 | 0.000s | 96 B | 16.4 MiB | 101.8 MiB |
| chardet 5.2.0 | 0.000s | 51 B | 60.9 MiB | 157.6 MiB |
| charset-normalizer | 0.640s | 1.3 MiB | 101.8 MiB | 265.7 MiB |
| charset-normalizer (pure) | 0.026s | 1.3 MiB | 101.9 MiB | 264.5 MiB |

Among pure-Python detectors, the rewrite has the best memory profile:

- **negligible import memory** (96 B traced import)
- **4.5x less peak memory** than charset-normalizer (22.5 MiB vs 101.8 MiB)
- **2.7x less peak memory** than chardet 5.2.0 (22.5 MiB vs 60.9 MiB)
- **2.8x less RSS** than charset-normalizer (96.1 MiB vs 265.7 MiB)

cchardet has near-zero Python-level memory (C extension) but these numbers
reflect only tracemalloc-visible allocations. Its RSS advantage (62.8 MiB)
is modest, as the baseline interpreter itself accounts for ~60 MiB.

## Rewrite vs chardet 6.0.0 (pairwise)

### Rewrite wins (21 encodings)

Biggest leads:

- cp424 (+100pp), iso-8859-7 (+41.2pp, 17 files)
- cp500 (+43.5pp, 46 files)
- cp1026/cp720/macturkish/windows-1253 (+33.3pp each)
- cp857/windows-1254 (+25pp), windows-1251 (+24.5pp, 53 files)
- iso-8859-9 (+22.2pp), windows-1250 (+14.3pp, 28 files)
- iso-8859-3 (+11.1pp), iso-8859-2 (+7.0pp, 43 files)

### chardet 6.0.0 wins (9 encodings)

Biggest leads:

- cp852 (+15pp)
- cp932 (+14.3pp), macroman (+11.9pp, 42 files)
- maclatin2 (+6.7pp), cp850/cp858 (+6.5pp each, 46 files)
- cp437 (+6.1pp, 33 files), gb2312 (+4.3pp, 23 files)
- cp037 (+2.1pp, 47 files)

### Tied (53 encodings)

utf-16, utf-32, iso-8859-5, iso-8859-6, koi8-r, koi8-t, shift_jis, euc-jp,
euc-kr, gb18030, etc.

## Rewrite vs charset-normalizer (pairwise)

### Rewrite wins (34 encodings)

Strongest in legacy Western European (iso-8859-1/15, windows-1252), DOS
codepages (cp437/cp850/cp858), Mac encodings (macroman, maciceland), and
Baltic/Central European (iso-8859-2/4/13, windows-1257). Also leads on
EBCDIC (cp500 +41.3pp, cp1026 +100pp), escape-based encodings
(hz-gb-2312, iso-2022-jp), and UTF-8-sig (+100pp).

### charset-normalizer wins (3 encodings)

cp932 (+14.3pp), maclatin2 (+6.7pp), and utf-8 (+0.6pp).

### Tied (46 encodings)

## Rewrite vs cchardet (pairwise)

### Rewrite wins (50 encodings)

The rewrite dominates on encoding breadth: 27 encodings at +100pp where
cchardet returns 0%. These include EBCDIC (cp037/cp500/cp875), Mac
encodings, Baltic (cp775), UTF-16/32 BOM-less variants, johab, and many
legacy single-byte encodings that cchardet's uchardet engine simply
doesn't support.

### cchardet wins (3 encodings)

cchardet is stronger on cp866 (+6.9pp), cp852 (+5.0pp), and gb2312
(+4.3pp).

### Tied (30 encodings)

## Rewrite vs chardet 5.2.0 (pairwise)

Massive improvement: rewrite wins on 54 encodings vs 5.2.0 winning on only
6. The rewrite gained all the 6.0 encoding-era support (EBCDIC, Baltic,
Central European, etc.) that 5.2.0 completely lacked. The 5.2.0 wins are
in traditional chardet strengths (cp932, cp866, gb2312, iso-8859-15,
windows-1252, iso-8859-1) where the older statistical models had a small
edge.

## charset-normalizer: mypyc vs pure Python

The two charset-normalizer variants (mypyc default, pure-Python venv) produce
**identical accuracy** (1942/2179 = 89.1%). The mypyc build is 1.5x faster
(33.08s vs 50.02s). Both have identical peak memory (~101.8 MiB traced,
~265 MiB RSS).

## mypyc Compilation

The rewrite supports optional mypyc compilation on CPython. When built with
`HATCH_BUILD_HOOK_ENABLE_MYPYC=true`, hot-path modules are compiled
to C extensions: `models/__init__.py` (bigram profiling and scoring),
`pipeline/structural.py` (CJK byte-scanning), `pipeline/validity.py`
(decode filtering), and `pipeline/statistical.py` (scoring orchestration).

In-process timing (2179 files, `encoding_era=ALL`, pre-loaded into memory):

| Build | Total | Per-file mean | Speedup |
|---|---|---|---|
| Pure Python | 6,566ms | 3.01ms | baseline |
| mypyc compiled | 4,416ms | 2.03ms | **1.49x** |

The mypyc speedup is 1.49x. The three-tier `_fill_language`
post-processing adds ~800ms of BigramProfile construction and cosine
scoring in pure Python -- exactly the kind of tight-loop integer
arithmetic that mypyc compiles to fast C code. The compiled build
(4,416ms) is faster than the pre-language pure Python baseline (~5,015ms).

Pure Python wheels are published alongside mypyc wheels for PyPy and
platforms without prebuilt binaries. No runtime dependencies are added.

## Performance Across Python Versions

Benchmarked chardet 7.0.0rc4 from PyPI across all supported Python
versions (macOS aarch64, 2179 files, `encoding_era=ALL`). CPython
versions install mypyc-compiled wheels automatically; PyPy receives
the pure-Python wheel.

| Python | Wheel | Import | Total | Files/s | Mean | Median | p90 | p95 |
|---|---|---|---|---|---|---|---|---|
| CPython 3.10 | mypyc | 2.509s | 4,257ms | 512 | 1.95ms | 0.60ms | 3.72ms | 4.28ms |
| CPython 3.10 | pure | 0.038s | 8,172ms | 267 | 3.75ms | 1.41ms | 6.89ms | 7.79ms |
| CPython 3.11 | mypyc | 2.736s | 3,815ms | **571** | **1.75ms** | 0.52ms | **3.41ms** | **3.89ms** |
| CPython 3.11 | pure | 0.040s | 6,345ms | 343 | 2.91ms | 1.09ms | 5.34ms | 6.20ms |
| CPython 3.12 | mypyc | 2.930s | 4,455ms | 489 | 2.04ms | 0.62ms | 3.87ms | 4.44ms |
| CPython 3.12 | pure | 0.018s | 6,567ms | 332 | 3.01ms | 1.13ms | 5.35ms | 6.18ms |
| CPython 3.13 | mypyc | 2.755s | 4,678ms | 466 | 2.15ms | 0.63ms | 4.07ms | 4.71ms |
| CPython 3.13 | pure | 0.054s | 8,666ms | 251 | 3.98ms | 1.46ms | 7.01ms | 7.91ms |
| CPython 3.14 | mypyc | 2.666s | 4,656ms | 468 | 2.14ms | 0.64ms | 4.07ms | 4.75ms |
| CPython 3.14 | pure | 0.013s | 6,525ms | 334 | 2.99ms | 1.12ms | 5.43ms | 6.24ms |
| PyPy 3.10 | pure | 0.031s | 5,392ms | 404 | 2.47ms | **0.31ms** | 4.97ms | 5.52ms |
| PyPy 3.11 | pure | 0.138s | 5,409ms | 403 | 2.48ms | 0.30ms | 4.98ms | 5.52ms |

**mypyc speedup by version:** 1.92x (3.10), 1.66x (3.11), 1.47x (3.12),
1.85x (3.13), 1.40x (3.14). The speedup varies because pure-Python
performance differs across CPython versions -- 3.11 and 3.12 have the
fastest pure-Python interpreters, leaving less room for mypyc to improve.

**CPython 3.11 + mypyc is the fastest combination** at 571 files/s,
~10% ahead of other mypyc builds.

**PyPy's JIT is competitive with mypyc**: pure Python on PyPy
(404 files/s) beats every pure CPython version and reaches 70--85% of
mypyc-compiled CPython throughput. PyPy also has the lowest median
latency (0.30--0.31ms), indicating the JIT optimizes the fast-path
pipeline stages (BOM, ASCII, UTF-8) extremely well.

**Best pure CPython versions:** 3.11, 3.12, and 3.14 cluster at
332--343 files/s; 3.10 and 3.13 are ~20% slower in pure mode.

**Import tradeoff:** mypyc builds pay ~2.5--2.9s upfront for import
(loading compiled `.so` extensions and lazy model initialization) vs
13--54ms for pure-Python builds. This is a one-time cost amortized
over all subsequent `detect()` calls.

## Thread Safety

The rewrite is fully thread-safe for concurrent `detect()` and
`detect_all()` calls. Each call creates its own `PipelineContext`
carrying per-run state (analysis cache, non-ASCII count, multi-byte
scores), eliminating shared mutable state between threads.

Five load-once global caches (model data, encoding index, model norms,
confusion maps, candidate lists) use double-checked locking: the fast
path (`if cache is not None: return cache`) has zero synchronization
overhead after the first call. This design is compatible with
mypyc-compiled modules.

Individual `UniversalDetector` instances are NOT thread-safe due to
mutable internal buffers. Create one instance per thread when using the
streaming API.

### Free-Threaded Python

CI tests run on Python 3.13t and 3.14t with the GIL disabled. The test
suite includes cold-cache race conditions (6 workers racing to
initialize all 5 caches simultaneously) and high-concurrency stress
tests (8 workers x 10 iterations). All pass under free-threading.

### Performance Impact

Thread safety overhead (single-threaded, in-process, 2179 files):

| Build | Before | After | Delta |
|---|---|---|---|
| Pure Python (CPython 3.12) | 6,027ms | 6,033ms | +0.1% |
| mypyc compiled (CPython 3.12) | 4,078ms | 4,070ms | -0.2% |

The thread safety mechanisms (PipelineContext allocation, lock objects on
load-once caches) add no measurable overhead. Both deltas are within
run-to-run noise.

Free-threaded Python scaling (3.13t, GIL disabled, 2179 files,
8-core Apple Silicon):

| Threads | Time | Speedup vs 1 thread |
|---|---|---|
| 1 | 4,361ms | baseline |
| 2 | 2,337ms | 1.9x |
| 4 | 1,930ms | 2.3x |
| 8 | 2,357ms | 1.9x |

With the GIL disabled, detection scales well up to 4 threads (2.3x
speedup), with diminishing returns at 8 threads due to memory bandwidth
contention. For comparison, CPython 3.12 (GIL enabled) shows no scaling
beyond 1 thread (~2,400ms regardless of thread count). Note that 3.13t
single-threaded performance is ~1.6x slower than CPython 3.12 due to
free-threading interpreter overhead, but multi-threading more than
compensates: 4-thread 3.13t (1,930ms) beats single-threaded CPython 3.12
(2,431ms).

## Key Takeaways

1. **The rewrite achieves 96.8% accuracy with dramatic speed/memory gains**
   vs chardet 6.0.0. It detects in ~6.5s what 6.0.0 takes 181s for, imports
   instantly, and uses negligible import memory (96 B) -- while being +2.3pp
   more accurate.

2. **Remaining accuracy gaps vs chardet 6.0.0**: DOS codepages where
   6.0.0 has a slight edge (cp437/cp850/cp858), cp852, macroman, and a
   handful of cp037 EBCDIC confusions. These are niche encodings that need
   either model retraining with more data or encoding-specific structural
   probes.

3. **The rewrite beats all competitors** on accuracy among pure-Python
   detectors. It leads chardet 6.0.0 by +2.3pp, charset-normalizer by
   +7.7pp, and chardet 5.2.0 by +28.6pp.

4. **charset-normalizer is worst on peak memory** (101.8 MiB traced peak,
   265 MiB RSS) despite moderate accuracy. The rewrite beats it on both
   fronts.

5. **cchardet is unbeatable on raw speed** (1.21s) but pays for it with the
   lowest accuracy (57.1%) and zero support for many encoding families. It's
   a poor choice when encoding breadth matters.

6. **mypyc compilation provides 1.49x speedup**. The BigramProfile
   construction and scoring in `_fill_language` is the kind of tight-loop
   work that mypyc optimizes well, bringing the compiled total (4,416ms)
   below the pre-language pure Python baseline (~5,015ms).

7. **Thread-safe and free-threading ready** -- `detect()` and
   `detect_all()` are safe to call concurrently from any number of
   threads, with negligible single-threaded overhead (+0.1%). On
   free-threaded Python 3.13t (GIL disabled), detection scales to 2.3x
   speedup at 4 threads, with 4-thread 3.13t outperforming single-threaded
   CPython 3.12.

8. **Language detection is a clear differentiator** -- the rewrite achieves
   90.5% language accuracy, nearly double chardet 6.0.0's 46.8% and far
   ahead of chardet 5.2.0's 18.9%. charset-normalizer and cchardet do not
   report language at all.
