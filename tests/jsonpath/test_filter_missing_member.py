"""
Verify filter comparisons against a literal when the member is missing.

RFC 9535 section 2.3.5.2.2 says that an empty nodelist is not equal to any value,
so `@.a != 5` is true when `@` has no member `a`,
while every other comparison (`==`, `<`, `<=`, `>`, `>=`) is false.
"""

import copy

import pytest

import jsonpath_ng.ext
from tests.helpers import assert_value_equality

DOC = {"i": [{"a": 1}, {"b": 2}, {"a": 5}, {"a": None}]}


@pytest.mark.parametrize(
    "path, expected_values",
    [
        pytest.param(
            "$.i[?@.a != 5]",
            [{"a": 1}, {"b": 2}, {"a": None}],
            id="int",
        ),
        pytest.param(
            "$.i[?@.a != 'x']",
            [{"a": 1}, {"b": 2}, {"a": 5}, {"a": None}],
            id="string",
        ),
        pytest.param(
            "$.i[?@.a != true]",
            [{"a": 1}, {"b": 2}, {"a": 5}, {"a": None}],
            id="bool",
        ),
        pytest.param(
            "$.i[?@.a != null]",
            [{"a": 1}, {"b": 2}, {"a": 5}],
            id="null",
        ),
        pytest.param("$.i[?@.a != 5 && @.b == 2]", [{"b": 2}], id="with-other-clause"),
        pytest.param(
            "$.i[?@.a != 5 && @.b != 2]",
            [{"a": 1}, {"a": None}],
            id="two-ne",
        ),
        pytest.param(
            "$.i[?@.b != 2]",
            [{"a": 1}, {"a": 5}, {"a": None}],
            id="other-member",
        ),
        pytest.param("$.i[?@.a.b != 5]", DOC["i"], id="missing-through-scalar"),
        pytest.param("$.i[?@.z != 5]", DOC["i"], id="missing-everywhere"),
    ],
)
def test_ne_literal_includes_missing_member(path, expected_values):
    matches = jsonpath_ng.ext.parse(path).find(copy.deepcopy(DOC))
    assert_value_equality(matches, expected_values)


@pytest.mark.parametrize("op", ["==", "<", "<=", ">", ">="])
def test_other_comparisons_exclude_missing_member(op):
    # Only the item that lacks `a` is of interest here.
    doc = {"i": [{"b": 2}]}
    assert jsonpath_ng.ext.parse(f"$.i[?@.a {op} 5]").find(doc) == []


def test_ne_literal_on_a_present_member_is_unchanged():
    doc = {"i": [{"a": 1}, {"a": 5}]}
    matches = jsonpath_ng.ext.parse("$.i[?@.a != 5]").find(doc)
    assert_value_equality(matches, [{"a": 1}])


def test_ne_literal_includes_missing_member_in_update():
    doc = copy.deepcopy(DOC)
    result = jsonpath_ng.ext.parse("$.i[?@.a != 5]").update(doc, "hit")
    assert result["i"] == ["hit", "hit", {"a": 5}, "hit"]


def test_ne_literal_includes_missing_member_in_filter():
    doc = copy.deepcopy(DOC)
    result = jsonpath_ng.ext.parse("$.i[?@.a != 5]").filter(lambda _: True, doc)
    assert result["i"] == [{"a": 5}]


@pytest.mark.parametrize(
    "path, expected_values",
    [
        pytest.param("$.i[?@.a != 5]", [{"a": "x"}, {"a": 1}], id="ne"),
        pytest.param("$.i[?@.a == 5]", [{"a": 5}], id="eq"),
        pytest.param("$.i[?@.a < 5]", [{"a": 1}], id="lt"),
        pytest.param("$.i[?@.a >= 5]", [{"a": 5}], id="ge"),
    ],
)
def test_comparing_a_non_numeric_string_with_an_int_literal(path, expected_values):
    """RFC 9535 section 2.3.5.2.2: values of different types are not equal."""

    doc = {"i": [{"a": "x"}, {"a": 1}, {"a": 5}]}
    matches = jsonpath_ng.ext.parse(path).find(doc)
    assert_value_equality(matches, expected_values)


@pytest.mark.parametrize(
    "path, expected_values",
    [
        pytest.param("$.i[?@.a == 5]", [{"a": 5}], id="eq"),
        pytest.param("$.i[?@.a != 5]", [{"a": "5"}, {"a": "x"}, {"a": 1}], id="ne"),
        pytest.param("$.i[?@.a < 5]", [{"a": 1}], id="lt"),
        pytest.param("$.i[?@.a <= 5]", [{"a": 5}, {"a": 1}], id="le"),
        pytest.param("$.i[?@.a > 1]", [{"a": 5}], id="gt"),
        pytest.param("$.i[?@.a >= 5]", [{"a": 5}], id="ge"),
    ],
)
def test_a_numeric_string_is_not_a_number(path, expected_values):
    """RFC 9535 section 2.3.5.2.2: the string "5" is not equal to the number 5."""

    doc = {"i": [{"a": "5"}, {"a": 5}, {"a": "x"}, {"a": 1}]}
    matches = jsonpath_ng.ext.parse(path).find(doc)
    assert_value_equality(matches, expected_values)


@pytest.mark.parametrize("op", ["<", "<=", ">", ">="])
def test_ordering_values_of_different_types_does_not_raise(op):
    doc = {"i": [{"a": None}, {"a": "x"}, {"a": [1]}, {"a": {"k": 1}}, {"a": True}]}
    assert jsonpath_ng.ext.parse(f"$.i[?@.a {op} 5]").find(doc) == []
