"""
Fast, in-process release replay against the source (editable) partition.

The wheel build, fresh-interpreter and read-only directory checks live in
``test_wheel_replay.py`` and only run under the ``release_wheel`` marker.
Everything here runs on every test invocation and pins:

* the three outcome classes (loader error, unsupported syntax, successful
  query), including the wrong-column write;
* the exact, documented set of base-vs-extended semantic divergences;
* that the checked-in catalog is current and never mutates during replay.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

RELEASE_DIR = pathlib.Path(__file__).parent


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_module("release_verifier", RELEASE_DIR / "verifier.py")
generator = _load_module("release_generator", RELEASE_DIR / "generate_catalog.py")


@pytest.fixture(scope="module")
def catalog():
    return verifier.load_catalog()


@pytest.fixture(scope="module")
def report(catalog):
    return verifier.replay_catalog(catalog)


def test_catalog_covers_every_required_category(catalog):
    categories = {rule["category"] for rule in catalog["rules"]}
    assert categories == {
        "plain",
        "extended",
        "root_relative",
        "special_key",
        "negative",
    }


def test_source_partition_matches_frozen_catalog(catalog, report):
    failures = verifier.verify_against_catalog(catalog, report)
    assert failures == []


def test_replay_does_not_mutate_the_catalog_document(catalog):
    snapshot = json.dumps(
        catalog["document"], sort_keys=True, ensure_ascii=False
    )
    verifier.replay_catalog(catalog)
    assert (
        json.dumps(catalog["document"], sort_keys=True, ensure_ascii=False)
        == snapshot
    )


def test_unsupported_syntax_is_distinct_from_loader_failure(report):
    # Extended-only grammar must surface as a grammar rejection, not a loader
    # fault, in the base parser.
    observed = report["ext-filter"]["base"]
    assert observed["status"] == verifier.UNSUPPORTED_SYNTAX
    assert observed["error_type"] == "JsonPathLexerError"

    observed = report["bad-malformed"]["ext"]
    assert observed["status"] == verifier.UNSUPPORTED_SYNTAX
    assert observed["error_type"] == "JsonPathParserError"


def test_loader_error_is_classified_in_isolation():
    """A broken PLY table fails at import, before any rule is parsed."""

    run = subprocess.run(
        [
            sys.executable,
            str(RELEASE_DIR / "replay_subprocess.py"),
            "--catalog",
            str(RELEASE_DIR / "rules.json"),
            "--partition",
            "source",
            "--fault",
            "jsonpath_ng._ply_tables.parser_table",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    # Exit code 2 means the package could not be loaded at all.
    assert run.returncode == 2, run.stderr
    payload = json.loads(run.stdout)
    assert "startup_loader_error" in payload
    assert payload["startup_loader_error"]["error_type"] == "RuntimeError"
    assert "report" not in payload


def test_wrong_column_write_is_detected_and_pinned(report):
    """The sort rule reads reordered views but writes the original columns."""

    observed = report["ext-sort"]["ext"]
    assert observed["status"] == verifier.OK
    assert observed["wrong_column"] is True
    assert [target["read_chain"] for target in observed["targets"]] == [
        [0, "name"],
        [1, "name"],
        [2, "name"],
    ]
    assert observed["updated_columns"] == [
        ["rows", 0, "name"],
        ["rows", 1, "name"],
        ["rows", 2, "name"],
    ]
    # The values are nevertheless the expected ones.
    assert [target["old"] for target in observed["targets"]] == ["x1", "x2", "x0"]


def test_only_the_sort_rule_writes_a_wrong_column(report):
    wrong = [
        (rule_id, mode)
        for rule_id, rule_report in report.items()
        for mode in verifier.MODES
        if rule_report[mode]["status"] == verifier.OK
        and rule_report[mode]["wrong_column"]
    ]
    assert wrong == [("ext-sort", "ext")]


def test_old_values_and_sentinel_writes_are_verified(catalog, report):
    observed = report["plain-wildcard"]["ext"]
    assert [target["old"] for target in observed["targets"]] == ["a0", "a1", "a2"]
    for column in observed["updated_columns"]:
        key = json.dumps(column, ensure_ascii=False)
        assert observed["new_values"][key] == catalog["sentinel"]


def test_divergences_match_the_documentation(catalog, report):
    observed = verifier.find_semantic_divergences(catalog, report)
    expected = {
        "ext-filter": {
            "aspect": "status",
            "base": "unsupported_syntax",
            "ext": "ok",
        },
        "ext-regex": {
            "aspect": "status",
            "base": "unsupported_syntax",
            "ext": "ok",
        },
        "ext-sort": {
            "aspect": "status",
            "base": "unsupported_syntax",
            "ext": "ok",
        },
        "at-sign": {
            "aspect": "target_column",
            "base": [["@"]],
            "ext": [[]],
        },
        "key-cjk": {
            "aspect": "status",
            "base": "ok",
            "ext": "unsupported_syntax",
        },
    }
    assert observed == expected


def test_catalog_is_current(catalog):
    """Regenerating the catalog is a no-op unless semantics change on purpose."""

    regenerated = generator.build_catalog()
    assert json.dumps(regenerated, sort_keys=True, ensure_ascii=False) == json.dumps(
        catalog, sort_keys=True, ensure_ascii=False
    )


@pytest.mark.parametrize("optimize", (0, 1, 2))
def test_source_partition_runs_at_every_optimization_level(optimize):
    flags = {0: [], 1: ["-O"], 2: ["-OO"]}[optimize]
    run = subprocess.run(
        [
            sys.executable,
            *flags,
            str(RELEASE_DIR / "replay_subprocess.py"),
            "--catalog",
            str(RELEASE_DIR / "rules.json"),
            "--partition",
            "source",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    # Each optimization level is an isolated test, so a startup failure at one
    # level cannot mask the results of the others.
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["optimize"] == optimize
    assert payload["catalog_failures"] == []
    assert payload["loader_errors"] == []
