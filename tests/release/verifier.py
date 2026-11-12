"""
Replay engine for the JSONPath ingestion-rule release regression.

The engine is deliberately free of pytest so the same code can run:

* in-process against the source tree, and
* in a fresh interpreter that only has the built wheel installed.

Every rule is replayed through both parser modes (base and extended) and each
outcome is reduced to one of three classifications:

* ``loader_error``       - the PLY table machinery failed before any
                           rule-specific parsing happened;
* ``unsupported_syntax`` - the rule could not be parsed in this mode, which
                           is a property of the grammar, not of the loader;
* ``ok``                 - the rule parsed, and both the read targets and the
                           columns written by ``update()`` are reported.

For ``ok`` rules three addresses are kept separate:

* ``reported_path`` - the ``full_path`` string the parser prints;
* ``read_chain``    - the concrete context address a datum was read from;
* ``updated_columns`` - the true document columns ``update()`` wrote to,
                        measured by diffing the document's scalar leaves.

They normally agree. When ``read_chain`` and ``updated_columns`` differ the
query succeeds and returns the right value while writing to a column whose
address does not match the reported path (the ``rows[/score]`` sort rule is
the documented example).
"""

from __future__ import annotations

import copy
import json
import pathlib
import typing

from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.ext.parser import parse as ext_parse
from jsonpath_ng.jsonpath import Fields
from jsonpath_ng.jsonpath import Index
from jsonpath_ng.jsonpath import Root
from jsonpath_ng.jsonpath import This
from jsonpath_ng.parser import parse as base_parse

if typing.TYPE_CHECKING:
    from jsonpath_ng.jsonpath import DatumInContext
    from jsonpath_ng.jsonpath import JSONPath

MODES = ("base", "ext")
PARSERS = {"base": base_parse, "ext": ext_parse}

LOADER_ERROR = "loader_error"
UNSUPPORTED_SYNTAX = "unsupported_syntax"
QUERY_ERROR = "query_error"
OK = "ok"

CATALOG_PATH = pathlib.Path(__file__).parent / "rules.json"


def load_catalog(path: pathlib.Path | str | None = None) -> dict:
    with open(path or CATALOG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def leaf_columns(
    document: typing.Any, prefix: tuple = ()
) -> typing.Iterator[tuple[tuple, typing.Any]]:
    """Yield ``(column, value)`` pairs for every scalar leaf of a document."""

    if isinstance(document, dict):
        for key, value in document.items():
            yield from leaf_columns(value, prefix + (key,))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from leaf_columns(value, prefix + (index,))
    else:
        yield prefix, document


def context_chain(datum: DatumInContext) -> list:
    """The concrete dict/list address a datum was read from.

    The chain follows ``DatumInContext.context`` rather than ``full_path`` so
    that selectors which synthesize a view (such as ``[/sort]``) still expose
    the structural address instead of the rendered path string.
    """

    chain: list = []
    current: DatumInContext | None = datum
    while current is not None and not isinstance(current.path, (Root, This)):
        path = current.path
        if isinstance(path, Fields):
            chain.append(path.fields[0])
        elif isinstance(path, Index):
            chain.append(path.indices[0])
        else:
            chain.append(f"<{type(path).__name__}>")
        current = current.context
    chain.reverse()
    return chain


def _failure(status: str, exc: Exception) -> dict:
    return {
        "status": status,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }


def replay_rule(
    parser: JSONPath,
    document: typing.Any,
    sentinel: typing.Any,
) -> dict:
    """Replay one rule through one already-constructed parser object."""

    read_document = copy.deepcopy(document)
    try:
        datums = parser.find(read_document)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return _failure(QUERY_ERROR, exc)

    targets = [
        {
            "reported_path": str(datum.full_path),
            "read_chain": context_chain(datum),
            "old": datum.value,
        }
        for datum in datums
    ]

    before = dict(leaf_columns(document))
    updated_document = copy.deepcopy(document)
    result = parser.update(updated_document, sentinel)
    after = dict(leaf_columns(updated_document))
    # Keep the full post-update leaf map so the verifier can prove that every
    # non-target column retains its original value (no collateral writes).
    all_leaf_values = {
        json.dumps(list(column), ensure_ascii=False): value
        for column, value in after.items()
    }
    changed_columns = [
        list(column) for column in before if before[column] != after.get(column)
    ]
    replaces_document = not changed_columns and result == sentinel

    if replaces_document:
        updated_columns = [[]]
        new_values = {"[]": sentinel}
    else:
        updated_columns = changed_columns
        new_values = {
            json.dumps(column, ensure_ascii=False): after.get(tuple(column))
            for column in updated_columns
        }

    # A successful query writes the wrong column when the structural address
    # a datum was read from does not match the address actually updated.
    # Tuples are compared, not path strings, so quoting conventions such as
    # ``'@'`` versus ``@`` do not register as a mismatch.
    read_columns = {tuple(target["read_chain"]) for target in targets}
    written_columns = {tuple(column) for column in updated_columns}
    wrong_column = not replaces_document and read_columns != written_columns

    return {
        "status": OK,
        "targets": targets,
        "updated_columns": updated_columns,
        "replaces_document": replaces_document,
        "wrong_column": wrong_column,
        "new_values": new_values,
        "all_leaf_values": all_leaf_values,
    }


def construct_and_replay(
    mode: str,
    expression: str,
    document: typing.Any,
    sentinel: typing.Any,
    parse: typing.Callable[[str], JSONPath] | None = None,
) -> dict:
    """Construct the parser for a rule and replay it, classifying failures."""

    parse = parse or PARSERS[mode]
    try:
        parser = parse(expression)
    except JSONPathError as exc:
        return _failure(UNSUPPORTED_SYNTAX, exc)
    except Exception as exc:
        # A broken/missing PLY table (or any other loader-level fault) raises
        # something other than a JSONPath error, which is the signal that the
        # installation rather than the rule is at fault.
        return _failure(LOADER_ERROR, exc)
    return replay_rule(parser, document, sentinel)


def replay_catalog(
    catalog: dict,
    modes: typing.Iterable[str] = MODES,
    parsers: dict[str, typing.Callable[[str], JSONPath]] | None = None,
) -> dict:
    """Replay every catalog rule. Returns a JSON-serialisable report."""

    parsers = parsers or PARSERS
    document = catalog["document"]
    sentinel = catalog["sentinel"]
    report: dict = {}
    for rule in catalog["rules"]:
        rule_report: dict = {
            "category": rule["category"],
            "expression": rule["expression"],
        }
        for mode in modes:
            rule_report[mode] = construct_and_replay(
                mode,
                rule["expression"],
                document,
                sentinel,
                parse=parsers.get(mode),
            )
        report[rule["id"]] = rule_report
    return report


def _catalog_status(expectation: dict) -> str:
    return OK if expectation["outcome"] == OK else UNSUPPORTED_SYNTAX


def verify_against_catalog(catalog: dict, report: dict) -> list[str]:
    """Check a replay report against the frozen expectations.

    Returns a list of human-readable failure strings; an empty list means the
    release behaves exactly as recorded.
    """

    document = catalog["document"]
    before = dict(leaf_columns(document))
    failures: list[str] = []

    for rule in catalog["rules"]:
        rule_id = rule["id"]
        observed_rule = report[rule_id]
        for mode in MODES:
            label = f"{rule_id}/{mode}"
            expected = rule[mode]
            observed = observed_rule[mode]

            expected_status = _catalog_status(expected)
            if observed["status"] != expected_status:
                failures.append(
                    f"{label}: status {observed['status']!r} != "
                    f"expected {expected_status!r}"
                )
                continue

            if observed["status"] == UNSUPPORTED_SYNTAX:
                if observed["error_type"] != expected["error"]:
                    failures.append(
                        f"{label}: error type {observed['error_type']!r} != "
                        f"expected {expected['error']!r}"
                    )
                if expected["message_contains"] not in observed["error_message"]:
                    failures.append(
                        f"{label}: message {observed['error_message']!r} does not "
                        f"contain {expected['message_contains']!r}"
                    )
                continue

            if observed["status"] != OK:
                # A loader error (or any other non-grammar failure) is never an
                # expected outcome for a shipped rule: the release is broken.
                failures.append(
                    f"{label}: unexpected {observed['status']} "
                    f"({observed.get('error_type')}: {observed.get('error_message')})"
                )
                continue

            expected_targets = expected["targets"]
            for field in ("reported_path", "read_chain"):
                observed_values = [target[field] for target in observed["targets"]]
                expected_values = [target[field] for target in expected_targets]
                if observed_values != expected_values:
                    failures.append(
                        f"{label}: {field} {observed_values!r} != {expected_values!r}"
                    )

            if not expected["replaces_document"]:
                observed_old = [target["old"] for target in observed["targets"]]
                expected_old = [target["old"] for target in expected_targets]
                if observed_old != expected_old:
                    failures.append(
                        f"{label}: old values {observed_old!r} != {expected_old!r}"
                    )

            if observed["updated_columns"] != expected["updated_columns"]:
                failures.append(
                    f"{label}: wrote to {observed['updated_columns']!r}, "
                    f"expected {expected['updated_columns']!r}"
                )

            if observed["wrong_column"] != expected["wrong_column"]:
                failures.append(
                    f"{label}: wrong_column {observed['wrong_column']!r} != "
                    f"expected {expected['wrong_column']!r}"
                )

            if observed["replaces_document"] != expected["replaces_document"]:
                failures.append(f"{label}: document-replacement flag mismatch")

            # For column updates every written column must hold the sentinel
            # and every other scalar leaf retain its original value, proving
            # no collateral write. Whole-document replacement is total by
            # nature, so it only needs the value check above.
            if observed["replaces_document"]:
                continue

            written = {tuple(column) for column in observed["updated_columns"]}
            for column, original in before.items():
                key = json.dumps(list(column), ensure_ascii=False)
                if column in written:
                    if observed["new_values"][key] != catalog["sentinel"]:
                        failures.append(
                            f"{label}: column {list(column)!r} holds "
                            f"{observed['new_values'][key]!r}, not the sentinel "
                            f"{catalog['sentinel']!r}"
                        )
                elif observed["all_leaf_values"].get(key) != original:
                    failures.append(
                        f"{label}: untouched column {list(column)!r} changed"
                    )

    return failures


def find_semantic_divergences(catalog: dict, report: dict) -> dict[str, dict]:
    """Reduce base-vs-ext differences to one entry per rule.

    Only the first differing aspect of a rule is recorded, so a newly
    introduced semantic divergence shows up as a single mapping plus its
    minimal documentation rather than as scattered failures.
    """

    divergences: dict[str, dict] = {}
    for rule in catalog["rules"]:
        rule_id = rule["id"]
        base_result = report[rule_id]["base"]
        ext_result = report[rule_id]["ext"]

        if base_result["status"] != ext_result["status"]:
            divergences[rule_id] = {
                "aspect": "status",
                "base": base_result["status"],
                "ext": ext_result["status"],
            }
            continue
        if base_result["status"] != OK:
            continue

        base_chains = [target["read_chain"] for target in base_result["targets"]]
        ext_chains = [target["read_chain"] for target in ext_result["targets"]]
        if base_chains != ext_chains:
            divergences[rule_id] = {
                "aspect": "target_column",
                "base": base_chains,
                "ext": ext_chains,
            }
            continue

        if base_result["wrong_column"] != ext_result["wrong_column"]:
            divergences[rule_id] = {
                "aspect": "wrong_column_flag",
                "base": base_result["wrong_column"],
                "ext": ext_result["wrong_column"],
            }
    return divergences
