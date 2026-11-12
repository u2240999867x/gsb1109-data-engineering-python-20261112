# Release replay: known base-vs-extended divergences

This is the minimal, authoritative list of semantic differences between the
base parser (`jsonpath_ng.parser.parse`) and the extended parser
(`jsonpath_ng.ext.parser.parse`). The release replay asserts that the set of
observed divergences never grows beyond this list; a new divergence must first
be added here as a single mapping entry with its minimal explanation.

| Rule id | Aspect | Base | Extended | Notes |
| --- | --- | --- | --- | --- |
| `ext-filter` | `status` | `unsupported_syntax` | `ok` | The `[? ... ]` filter grammar only exists in the extended parser. |
| `ext-regex` | `status` | `unsupported_syntax` | `ok` | The `=~` filter operator only exists in the extended parser. |
| `ext-sort` | `status` | `unsupported_syntax` | `ok` | The `[/field]` sort grammar only exists in the extended parser. It also reports `[0].name` while writing `rows[0].name`; see below. |
| `at-sign` | `target_column` | column `["@"]` (a field named `@`) | document replacement (the `This` operator) | Bare `@` is a field name in the base parser but the current-datum operator in the extended parser. |
| `key-cjk` | `status` | `ok` | `unsupported_syntax` | The base lexer accepts CJK identifiers; the extended lexer's ID rule does not include them. |

## Documented wrong-column write

`ext-sort` (`rows[/score][*].name`) is the one rule where the query succeeds
and returns the correct values while the rendered `full_path` does not match
the column actually written:

* reported paths: `[0].name`, `[1].name`, `[2].name`
* columns written: `rows[0].name`, `rows[1].name`, `rows[2].name`

The sort selector synthesizes a reordered view, so its datum context chains
are rooted at the view rather than at `rows`. `update()` still writes through
to the original `rows` containers. The catalog records both `read_chain` and
`updated_columns` separately so this behavior is pinned rather than hidden.
