"""
Test that `Descendants` handles data nested deeper than the recursion limit.

The nesting is twice the interpreter's recursion limit,
so the tests do not depend on the exact number of stack frames used per level.
Matches are checked by value only: their contexts are linked lists
that are still walked recursively by `DatumInContext.full_path`.
"""

import sys

import pytest

import jsonpath_ng
import jsonpath_ng.ext

DEPTH = sys.getrecursionlimit() * 2

parsers = pytest.mark.parametrize(
    "parse",
    (jsonpath_ng.parse, jsonpath_ng.ext.parse),
    ids=lambda function: function.__module__,
)


def nested_dicts(leaf):
    data = leaf
    for _ in range(DEPTH):
        data = {"a": data}
    return data


def nested_lists(leaf):
    data = leaf
    for _ in range(DEPTH):
        data = [data]
    return data


def innermost(data):
    for _ in range(DEPTH):
        data = data["a"]
    return data


@parsers
def test_find_in_deeply_nested_dicts(parse):
    results = parse("$..a").find(nested_dicts(1))

    assert len(results) == DEPTH
    assert results[-1].value == 1


@parsers
def test_find_in_deeply_nested_lists(parse):
    results = parse("$..[0]").find(nested_lists([]))

    assert len(results) == DEPTH
    assert results[-1].value == []


@parsers
def test_update_deeply_nested_data(parse):
    data = nested_dicts({"b": 1, "c": 2})

    parse("$..b").update(data, 3)

    assert innermost(data) == {"b": 3, "c": 2}


@parsers
def test_filter_deeply_nested_data(parse):
    data = nested_dicts({"b": 1, "c": 2})

    parse("$..b").filter(lambda value: value == 1, data)

    assert innermost(data) == {"c": 2}
