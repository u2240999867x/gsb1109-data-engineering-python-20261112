"""
A descendant segment may be followed directly by a filter selector.

RFC 9535 section 2.5.2.1 defines the descendant segment as ``".."`` followed by a
``bracketed-selection``, a ``wildcard-selector`` or a ``member-name-shorthand``,
and a ``bracketed-selection`` may contain a ``filter-selector`` (section 2.3.5.1),
so ``$..[?<logical-expr>]`` selects every array element and object member value,
at any depth, for which the logical expression is true.
"""

import pytest

import jsonpath_ng.ext
from jsonpath_ng.exceptions import JsonPathParserError


def paths_and_values(expression, data):
    matches = jsonpath_ng.ext.parse(expression).find(data)
    return [(str(match.full_path), match.value) for match in matches]


@pytest.mark.parametrize(
    "expression",
    (
        pytest.param("$..[?(@.specialKey)]", id="parenthesized"),
        pytest.param("$..[?@.specialKey]", id="unparenthesized"),
    ),
)
def test_existence_test(expression):
    # Issue #46
    data = [
        {"specialKey": 5, "value": 8},
        {"otherKey": 87},
        [{"specialKey": 123, "value": 12}],
        {"olderStuff": [{"specialKey": 124, "value": 88}]},
    ]

    assert [match.value for match in jsonpath_ng.ext.parse(expression).find(data)] == [
        {"specialKey": 5, "value": 8},
        {"specialKey": 123, "value": 12},
        {"specialKey": 124, "value": 88},
    ]


def test_selects_from_objects_and_arrays_at_any_depth():
    # The root and scalar members are never selected, even though a "k" key exists.
    data = {
        "a": {"x": {"k": 1}, "y": {"j": 2}},
        "b": [{"k": 3}, [{"k": 4}]],
        "k": 9,
    }

    assert paths_and_values("$..[?@.k]", data) == [
        ("a.x", {"k": 1}),
        ("b.[0]", {"k": 3}),
        ("b.[1].[0]", {"k": 4}),
    ]


def test_comparison():
    # Issue #162
    data = {
        "students": [
            {"name": "Peter", "gender": "Male"},
            {"name": "Mary", "gender": "Female"},
            {"name": "Susan", "gender": "Female"},
        ],
        "teachers": [
            {"name": "William", "gender": "Male"},
            {"name": "Lucy", "gender": "Female"},
        ],
    }

    assert paths_and_values('$..[?(@.gender == "Female")].name', data) == [
        ("students.[1].name", "Mary"),
        ("students.[2].name", "Susan"),
        ("teachers.[1].name", "Lucy"),
    ]


def test_selects_nothing_when_no_node_matches():
    assert paths_and_values("$..[?@.k]", {"a": [{"j": 1}]}) == []


@pytest.mark.parametrize("data", (5, "abc", None, True))
def test_selects_nothing_from_a_scalar(data):
    assert paths_and_values("$..[?@.k]", data) == []


@pytest.mark.parametrize("expression", ("$.[?@.k]", "[?@.k]"))
def test_filter_is_not_accepted_outside_of_a_segment(expression):
    """RFC 9535 has no ``$.[`` segment and requires the query to start with ``$``."""
    with pytest.raises(JsonPathParserError):
        jsonpath_ng.ext.parse(expression)
