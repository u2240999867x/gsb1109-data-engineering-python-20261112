# Quality-gate regression corpus

This directory holds the hand-labeled, paired records used by
`tests/test_quality_gate.py` to test `ftfy.quality_gate`.

The corpus models a mixed corpus where the same string can be two different
things:

- a product title that ftfy is expected to repair (`purpose: "should_fix"`),
- documentation that deliberately displays the broken text and must not be
  touched (`purpose: "should_keep"`).

## Record fields

- `record_id`: stable identity of the business record. Repeated exports of the
  same document reuse the same id and are counted once, so exporting a document
  ten times cannot change a purpose's false-change proportion.
- `document_id`: the document or export the row came from, for diagnostics.
- `purpose`: `"should_fix"` or `"should_keep"`.
- `original`: the string fed to ftfy.
- `expected`: the output the human labeler expects. For `should_keep` records
  this must equal `original`.
- `label`: human-readable description of the case.

## What the corpus exercises

- The same string appears as both a fixable title and intentionally displayed
  mojibake (`R-002` vs `R-101`, and similar pairs for curly quotes and HTML
  entities).
- `R-101` is exported ten times and `R-001` twice, exercising per-record
  deduplication.
- `R-006` mixes three regions in one string: one fixed correctly, one
  intended change that is retained, and one fragment ftfy does not recognize,
  so the outcome contains both retained and introduced change intervals.
- `R-007` is a false retain (ftfy changes nothing).
- `R-102` is ftfy's known synthetic false positive, whose minimal suppressing
  combination is the single switch `fix_encoding`.
- Several records reroute through a second fixer when only one switch is off,
  requiring multi-switch minimal combinations (for example `R-104` needs both
  `fix_encoding` and `fix_c1_controls` disabled).
