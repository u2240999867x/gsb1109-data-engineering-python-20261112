"""
Test that `Descendants` visits data depth-first, in pre-order, in document order.

The visiting order is observable through callbacks passed to `update()` and `filter()`,
and through the order of the matches returned by `find()`.
"""

import pytest

import jsonpath_ng
import jsonpath_ng.ext

parsers = pytest.mark.parametrize(
    "parse",
    (jsonpath_ng.parse, jsonpath_ng.ext.parse),
    ids=lambda function: function.__module__,
)

DATA = {
    "a": 1,
    "b": [{"a": 2}, {"c": {"a": 3}}, 4],
    "c": {"a": 5, "d": [{"a": 6}]},
}


@parsers
def test_descendants_find_order(parse):
    results = parse("$..a").find(DATA)

    assert [result.value for result in results] == [1, 2, 3, 5, 6]


@parsers
def test_descendants_update_order(parse):
    visited = []

    def record(value, parent, field):
        visited.append(value)
        return value

    parse("$..a").update(DATA, record)

    assert visited == [1, 2, 3, 5, 6]


@parsers
def test_descendants_filter_order(parse):
    visited = []

    def record(value):
        visited.append(value)
        return False

    parse("$..a").filter(record, DATA)

    assert visited == [1, 2, 3, 5, 6]


@parsers
def test_descendants_update_and_filter_with_multiple_left_matches_keep_order(parse):
    data = [{"x": {"a": 1, "y": {"a": 2}}}, {"x": {"a": 3}}]
    visited = []

    parse("[*].x..a").update(
        data, lambda value, parent, field: visited.append(value) or value
    )
    assert visited == [1, 2, 3]

    visited.clear()
    parse("[*].x..a").filter(lambda value: visited.append(value), data)
    assert visited == [1, 2, 3]
