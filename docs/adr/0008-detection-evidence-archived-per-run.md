# Detection evidence is archived per run, not re-derived on demand

An archive that stores a detected encoding next to a file must later answer
"why may this decode under that name, and does that still hold if the policy
changes?" Two changes are cheap and common: narrowing the candidate
whitelist (`include_encodings`/`exclude_encodings`/`encoding_era`) and
changing output-name policy (`compat_names`, `prefer_superset`). Re-running
detection to answer either is wrong twice: it re-reads bytes that may no
longer be available, and a converged statistical answer is not guaranteed
bit-stable across model or registry versions, so a re-run can silently
replace the archived judgment with a different one rather than validate it.

The decision is a per-run evidence record (`chardet.evidence.
DetectionEvidence`) produced during the **single** detection pass. The
default `detect()` path constructs no recorder and pays nothing;
`detect_with_evidence()` and `UniversalDetector.close_with_evidence()` run
the same pipeline once with an `EvidenceRecorder` attached. The record holds
no input bytes — only names, gate verdicts, scores, ranks, window lengths,
the output-name trace, and the registry universe with its eras — so it is
small, serializable, and cannot be turned back into a second detection.

The record's replay rule (`can_reuse_decision`) turns the question into set
arithmetic over recorded boundaries. A whitelist **addition** of a codec the
run never examined cannot be answered: it has neither a validity verdict nor
a score, so the verdict names the byte-validity stage (or the whitelist gate
for an unknown name) as the evidence to re-gather. A whitelist **removal**
is answerable from the recorded boundary: a candidate dropped at a gate
(validity failure, CJK structural gate, pruning below the scoring contract
floor) never competed for the top, and a fully scored candidate that lost
keeps a score proving it finished below the winner — removing either cannot
move a recorded loser above the recorded winner. Deterministic early exits
(BOM, escape, magic, markup, ASCII, UTF-8, binary) commit before scoring and
are independent of the other whitelist members.

The two `cp1252` answers expose why outcome kind matters more than the name.
An evidence-selected `cp1252` survived validity, was scored, and ranked first;
deleting scored losers or pruned tails is provably safe, deleting the winner
is not. A no-match **fallback** `cp1252` is never checked against the data at
all — it answers because every admitted candidate has a recorded rejection.
Tightening is safe only while every removed name carries such a boundary;
widening toward an un-examined codec must re-gather validity evidence, and
removing the sentinel itself forces a re-run because the record contains no
alternative winner. Same final name, opposite credentials, opposite safe
zones.

Output-name changes split by whether the codec identity moves. An
alias-only rename (`cp1252` ↔ `Windows-1252`) resolves to one codec, so it is
always reusable. A remap that changes codecs (the preferred-superset swap, or
a narrower display codec such as `euc_jis_2004` → `EUC-JP`) needs a window
decode: reusable when the recorded render trace verified it, or when the
record's own validity boundary already shows the target codec fails (the
superset remap is then skipped); otherwise the verdict names the output-render
gate as the stage to re-gather. Turning either transform *off* restores the
pipeline-validated internal name and needs no decode.

Rejected alternatives. (1) Re-running detection on replay: violates the
no-re-read requirement and trades validation for a possibly different
verdict. (2) Storing the input window alongside the result: bloats the
archive, embeds source bytes in a metadata store, and still cannot answer
whitelist additions without re-scoring; the recorded boundaries answer every
tractable case without them. (3) Threading recording through every mypyc
compiled stage: the hot path would carry branches it never uses in default
detection. The recorder instead hangs off the interpreted orchestrator, and
the one non-trivial mirror — the postprocess chain — runs step by step only
on the evidence path, comparing rankings around the same compiled correction
functions so their logic stays defined in one place.
