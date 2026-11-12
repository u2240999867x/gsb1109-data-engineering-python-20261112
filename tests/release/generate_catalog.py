"""
Generate the frozen ingestion-rule catalog consumed by the release tests.

Run from the repository root with the source tree importable::

    python tests/release/generate_catalog.py

The catalog records, for every rule and parser mode:

* ``reported_path`` - the ``full_path`` string the parser prints;
* ``read_chain``    - the concrete context address a datum is read from;
* ``column``        - the true document column ``update()`` writes to;
* ``old``           - the value found before any update.

``read_chain`` and ``column`` are normally identical. When they differ the
query succeeds and returns the expected value while writing to a column whose
address does not match the reported path; the ``ext-sort`` rule is the
documented example. Regenerating the catalog is therefore expected to be a
no-op unless a parser change intentionally alters one of these behaviors.
"""

from __future__ import annotations

import copy
import json

from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.ext.parser import parse as ext_parse
from jsonpath_ng.parser import parse as base_parse

from verifier import CATALOG_PATH
from verifier import context_chain
from verifier import leaf_columns

SENTINEL = "SENT"

# rule id, category, expression, per-mode expectations
# "unsupported" marks a mode whose grammar cannot parse the rule.
RULES = [
    ("plain-child", "plain", "rows[0].name", {}),
    ("plain-wildcard", "plain", "rows[*].alias", {}),
    ("plain-slice", "plain", "rows[0:2].name", {}),
    ("plain-multifield", "plain", "rows[0][name,alias]", {}),
    ("plain-where", "plain", "(rows[*] where active).name", {}),
    ("ext-filter", "extended", "rows[?@.score >= 10].name", {"base": "unsupported"}),
    ("ext-regex", "extended", "rows[?@.name =~ 'x.*'].alias", {"base": "unsupported"}),
    ("ext-sort", "extended", "rows[/score][*].name", {"base": "unsupported"}),
    ("root-dollar", "root_relative", "$.meta.kind", {}),
    ("root-descendants", "root_relative", "$..kind", {}),
    ("root-reset", "root_relative", "meta.$.meta.kind", {}),
    ("this-backtick", "root_relative", "`this`", {"whole": True}),
    ("root-bare", "root_relative", "$", {"whole": True}),
    ("at-sign", "special_key", "@", {}),
    ("at-field", "special_key", "@foo", {}),
    ("key-numeric", "special_key", "1", {}),
    ("key-dotted", "special_key", "'a.c'", {}),
    ("key-star", "special_key", "'*'", {}),
    ("key-true", "special_key", "true", {}),
    ("key-null", "special_key", "null", {}),
    ("key-hyphen", "special_key", "bar-baz", {}),
    ("key-cjk", "special_key", "键", {"ext": "unsupported"}),
    ("bad-malformed", "negative", "foo.", {"both": "unsupported"}),
]

PARSERS = {"base": base_parse, "ext": ext_parse}

DOCUMENT = {
    "rows": [
        {"name": "x0", "alias": "a0", "score": 10, "active": True},
        {"name": "x1", "alias": "a1", "score": 4, "active": False},
        {"name": "x2", "alias": "a2", "score": 7, "active": True},
    ],
    "meta": {"kind": "ingest", "nested": {"kind": "deep"}},
    "@": "at-sign",
    "@foo": "at-field",
    "1": "numeric-key",
    "a.c": "dotted-key",
    "*": "star-key",
    "true": True,
    "null": None,
    "bar-baz": "hyphen-key",
    "键": "cjk-key",
}


def build_catalog() -> dict:
    before = dict(leaf_columns(DOCUMENT))
    catalog = {"sentinel": SENTINEL, "document": DOCUMENT, "rules": []}

    for rule_id, category, expression, flags in RULES:
        entry = {"id": rule_id, "category": category, "expression": expression}
        for mode, parse in PARSERS.items():
            if flags.get(mode) == "unsupported" or flags.get("both") == "unsupported":
                try:
                    parse(expression)
                except JSONPathError as exc:
                    entry[mode] = {
                        "outcome": "unsupported_syntax",
                        "error": type(exc).__name__,
                        "message_contains": (
                            "Unexpected character"
                            if type(exc).__name__ == "JsonPathLexerError"
                            else "Parse error"
                        ),
                    }
                else:  # pragma: no cover - generation-time guard
                    raise SystemExit(f"expected {rule_id}/{mode} to be unsupported")
                continue

            parser = parse(expression)
            datums = parser.find(copy.deepcopy(DOCUMENT))

            updated = copy.deepcopy(DOCUMENT)
            result = parser.update(updated, SENTINEL)
            after = dict(leaf_columns(updated))
            changed = [
                list(column)
                for column in before
                if before[column] != after.get(column)
            ]
            whole = bool(flags.get("whole")) or (not changed and result == SENTINEL)

            # Read-side targets follow ``find()`` order. For view-building
            # selectors (such as ``[/sort]``) this order need not match the
            # document order of the columns written, so the two are recorded
            # independently rather than zipped together.
            targets = []
            for datum in datums:
                target = {
                    "reported_path": str(datum.full_path),
                    "read_chain": context_chain(datum),
                }
                if not whole:
                    target["old"] = datum.value
                targets.append(target)
            if whole:
                updated_columns = [[]]
                wrong_column = False
            else:
                updated_columns = changed
                read_columns = {tuple(target["read_chain"]) for target in targets}
                written_columns = {tuple(column) for column in updated_columns}
                wrong_column = read_columns != written_columns

            entry[mode] = {
                "outcome": "ok",
                "targets": targets,
                "updated_columns": updated_columns,
                "replaces_document": whole,
                "wrong_column": wrong_column,
            }
        catalog["rules"].append(entry)

    return catalog


def main() -> None:
    catalog = build_catalog()
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(catalog['rules'])} rules to {CATALOG_PATH}")


if __name__ == "__main__":
    main()
