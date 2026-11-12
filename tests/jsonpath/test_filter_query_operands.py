"""
Verify that a filter comparison may have a query on its right-hand side.

RFC 9535 section 2.3.5.1 says that both operands of a comparison
may be a literal, a function expression, or a singular query,
and that a singular query may be relative (`@`) or absolute (`$`).
"""

import copy

import pytest

import jsonpath_ng.ext
from jsonpath_ng import Child
from jsonpath_ng import Fields
from jsonpath_ng import Index
from jsonpath_ng import Root
from jsonpath_ng import This
from jsonpath_ng.exceptions import JsonPathParserError
from jsonpath_ng.ext.filter import Expression
from jsonpath_ng.ext.filter import Filter
from tests.helpers import assert_value_equality

REF_DOC = {"ref": {"v": 1}, "items": [{"v": 1}, {"v": 2}, {"v": 1}, {"w": 1}]}
PAIR_DOC = {"items": [{"a": 1, "b": 1}, {"a": 1, "b": 2}, {"a": 3, "b": 2}]}
STRUCT_DOC = {
    "items": [
        {"p": [1, 2], "q": [1, 2]},
        {"p": [1, 2], "q": [2, 1]},
        {"p": {"k": 1}, "q": {"k": 1}},
    ]
}
EQUAL_FIELDS_DOC = {
    "objects": [
        {"some_thing": {"some_field": 1}},
        {"some_other_thing": {"some_field": 1}},
    ]
}
DIFFERENT_FIELDS_DOC = {
    "objects": [
        {"some_thing": {"some_field": 1}},
        {"some_thing": {"some_field": 2}},
    ]
}


@pytest.mark.parametrize(
    "path, datum, expected_values",
    [
        pytest.param("$.items[?(@.a == @.b)]", PAIR_DOC, [{"a": 1, "b": 1}], id="eq"),
        pytest.param(
            "$.items[?(@.a != @.b)]",
            PAIR_DOC,
            [{"a": 1, "b": 2}, {"a": 3, "b": 2}],
            id="ne",
        ),
        pytest.param("$.items[?(@.a < @.b)]", PAIR_DOC, [{"a": 1, "b": 2}], id="lt"),
        pytest.param(
            "$.items[?(@.a <= @.b)]",
            PAIR_DOC,
            [{"a": 1, "b": 1}, {"a": 1, "b": 2}],
            id="le",
        ),
        pytest.param("$.items[?(@.a > @.b)]", PAIR_DOC, [{"a": 3, "b": 2}], id="gt"),
        pytest.param(
            "$.items[?(@.a >= @.b)]",
            PAIR_DOC,
            [{"a": 1, "b": 1}, {"a": 3, "b": 2}],
            id="ge",
        ),
        pytest.param(
            "$.items[?@.a == @.b]", PAIR_DOC, [{"a": 1, "b": 1}], id="no-parentheses"
        ),
        pytest.param(
            "$.items[?(@.p == @.q)]",
            STRUCT_DOC,
            [{"p": [1, 2], "q": [1, 2]}, {"p": {"k": 1}, "q": {"k": 1}}],
            id="array-and-object-equality",
        ),
        pytest.param(
            "$.items[?(@.p != @.q)]",
            STRUCT_DOC,
            [{"p": [1, 2], "q": [2, 1]}],
            id="array-order-matters",
        ),
        pytest.param(
            "$.items[?(@.a == @.b[0])]",
            {"items": [{"a": 1, "b": [1]}, {"a": 1, "b": [2]}]},
            [{"a": 1, "b": [1]}],
            id="index-segment",
        ),
        pytest.param(
            "$.items[?(@.a == @['b'])]",
            {"items": [{"a": 1, "b": 1}, {"a": 1, "b": 2}]},
            [{"a": 1, "b": 1}],
            id="bracketed-name-segment",
        ),
        pytest.param(
            "$.items[?(@ == @)]", {"items": [1, "a", None]}, [1, "a", None], id="this"
        ),
    ],
)
def test_current_node_compared_to_current_node(path, datum, expected_values):
    results = jsonpath_ng.ext.parse(path).find(datum)

    assert_value_equality(results, expected_values)


@pytest.mark.parametrize(
    "path, datum, expected_values",
    [
        pytest.param(
            "$.items[?(@.v == $.ref.v)]",
            REF_DOC,
            [{"v": 1}, {"v": 1}],
            id="eq",
        ),
        pytest.param(
            "$.items[?(@.v != $.ref.v)]",
            REF_DOC,
            [{"v": 2}, {"w": 1}],
            id="ne",
        ),
        pytest.param("$.items[?(@.v > $.ref.v)]", REF_DOC, [{"v": 2}], id="gt"),
        pytest.param(
            "$.items[?@.v == $.ref.v]", REF_DOC, [{"v": 1}, {"v": 1}], id="no-parens"
        ),
        pytest.param(
            "$.items[?($.ref.v == @.v)]",
            REF_DOC,
            [{"v": 1}, {"v": 1}],
            id="root-on-the-left",
        ),
        pytest.param(
            "$.items[?(@.v == $['ref']['v'])]",
            REF_DOC,
            [{"v": 1}, {"v": 1}],
            id="bracketed-names",
        ),
        pytest.param(
            "$.items[?(@.v == $.list[1])]",
            {"list": [5, 2], "items": [{"v": 1}, {"v": 2}]},
            [{"v": 2}],
            id="index-segment",
        ),
        pytest.param(
            "$.items[?(@.v == $.missing)]",
            REF_DOC,
            [{"w": 1}],
            id="missing-root-query-equals-missing-member",
        ),
        pytest.param(
            "$.items[?(@.v == $.ref.v && @.w == $.ref.v)]",
            {"ref": {"v": 1}, "items": [{"v": 1, "w": 1}, {"v": 1, "w": 2}]},
            [{"v": 1, "w": 1}],
            id="combined-with-and",
        ),
        pytest.param(
            "$.objects[1].some_other_thing"
            "[?(@.some_field != $.objects[0].some_thing.some_field)]",
            EQUAL_FIELDS_DOC,
            [1],
            id="nested-root-query-not-equal",
        ),
        pytest.param(
            "$.objects[1].some_other_thing"
            "[?(@.some_field == $.objects[0].some_thing.some_field)]",
            EQUAL_FIELDS_DOC,
            [],
            id="nested-root-query-equal",
        ),
        pytest.param(
            "$.objects[1][?(@.some_field != $.objects[0].some_thing.some_field)]",
            DIFFERENT_FIELDS_DOC,
            [{"some_field": 2}],
            id="nested-root-query-different",
        ),
    ],
)
def test_current_node_compared_to_root_query(path, datum, expected_values):
    results = jsonpath_ng.ext.parse(path).find(datum)

    assert_value_equality(results, expected_values)


@pytest.mark.parametrize(
    "path, datum, expected_values",
    [
        pytest.param(
            "$.objects[?(@.some_other_thing != @.some_thing)]",
            EQUAL_FIELDS_DOC,
            [
                {"some_thing": {"some_field": 1}},
                {"some_other_thing": {"some_field": 1}},
            ],
            id="sibling-members-equal",
        ),
        pytest.param(
            "$.objects[?(@.some_other_thing != @.some_thing)].some_field",
            EQUAL_FIELDS_DOC,
            [],
            id="sibling-members-equal-with-trailing-member",
        ),
        pytest.param(
            "$.objects[?(@.some_thing != @.some_other_thing)]",
            DIFFERENT_FIELDS_DOC,
            [{"some_thing": {"some_field": 1}}, {"some_thing": {"some_field": 2}}],
            id="sibling-members-different",
        ),
    ],
)
def test_sibling_members_compared(path, datum, expected_values):
    """Compare two members of the same object.

    The trailing `.some_field` applies to each selected object,
    and those objects have no `some_field` member,
    so no values are selected.
    """

    results = jsonpath_ng.ext.parse(path).find(datum)

    assert_value_equality(results, expected_values)


@pytest.mark.parametrize(
    "path, datum, expected_values",
    [
        pytest.param(
            "$.items[?(@.x == @.y)]",
            {"items": [{"a": 1}, {"x": 1}]},
            [{"a": 1}],
            id="missing-equals-missing",
        ),
        pytest.param(
            "$.items[?(@.x != @.y)]",
            {"items": [{"a": 1}, {"x": 1}, {"y": 1}, {"x": 1, "y": 1}]},
            [{"x": 1}, {"y": 1}],
            id="missing-differs-from-present",
        ),
        pytest.param(
            "$.items[?(@.x <= @.y)]",
            {"items": [{"a": 1}, {"x": 1}, {"x": 1, "y": 1}]},
            [{"a": 1}, {"x": 1, "y": 1}],
            id="missing-is-less-than-or-equal-to-missing",
        ),
        pytest.param(
            "$.items[?(@.x < @.y)]",
            {"items": [{"a": 1}, {"x": 1, "y": 2}]},
            [{"x": 1, "y": 2}],
            id="missing-is-not-less-than-missing",
        ),
    ],
)
def test_missing_query_operands(path, datum, expected_values):
    """RFC 9535 section 2.3.5.2.2: two empty nodelists are equal.

    An empty nodelist is not equal to any value, even `null`.
    """

    results = jsonpath_ng.ext.parse(path).find(datum)

    assert_value_equality(results, expected_values)


@pytest.mark.parametrize(
    "path, expected_values",
    [
        pytest.param("$.items[?(@.a == $.r)]", [{"a": None}], id="eq"),
        pytest.param("$.items[?(@.a != $.r)]", [{}, {"a": 0}], id="ne"),
        pytest.param("$.items[?(@.a <= $.r)]", [{"a": None}], id="le"),
    ],
)
def test_query_operand_with_null_value_is_not_missing(path, expected_values):
    """RFC 9535 section 2.3.5.2.2: only an *empty nodelist* is Nothing.

    A node whose value is `null` is a node, so it equals `null`,
    and a missing value does not.

    jsonpath-js 0.3.1 disagrees for `$` queries (it treats a `null` result as Nothing),
    which is a bug in jsonpath-js: it gets this right for `@` queries.
    """

    datum = {"r": None, "items": [{"a": None}, {}, {"a": 0}]}

    results = jsonpath_ng.ext.parse(path).find(datum)

    assert_value_equality(results, expected_values)


@pytest.mark.parametrize(
    "path, items, expected_values",
    [
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": 13, "b": "13"}, {"a": 13, "b": 13}],
            [{"a": 13, "b": 13}],
            id="number-is-not-string",
        ),
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": 1, "b": True}, {"a": 0, "b": False}, {"a": True, "b": True}],
            [{"a": True, "b": True}],
            id="number-is-not-boolean",
        ),
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": 1, "b": 1.0}, {"a": 1, "b": 2.0}],
            [{"a": 1, "b": 1.0}],
            id="integer-equals-float",
        ),
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": None, "b": None}, {"a": None, "b": 0}, {"a": None}],
            [{"a": None, "b": None}],
            id="null-equals-null-only",
        ),
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": [1, True], "b": [1, 1]}, {"a": [1, True], "b": [1, True]}],
            [{"a": [1, True], "b": [1, True]}],
            id="array-of-number-is-not-array-of-boolean",
        ),
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": {"k": False}, "b": {"k": 0}}, {"a": {"k": 0}, "b": {"k": 0.0}}],
            [{"a": {"k": 0}, "b": {"k": 0.0}}],
            id="object-of-number-is-not-object-of-boolean",
        ),
        pytest.param(
            "$[?(@.a == @.b)]",
            [{"a": {"x": 1, "y": 2}, "b": {"y": 2, "x": 1}}, {"a": {"x": 1}, "b": {}}],
            [{"a": {"x": 1, "y": 2}, "b": {"y": 2, "x": 1}}],
            id="object-member-order-is-irrelevant",
        ),
    ],
)
def test_query_operand_equality_follows_rfc_9535(path, items, expected_values):
    """RFC 9535 section 2.3.5.2.2 says values are only equal if they have the same type.

    Python considers `True == 1`, `[True] == [1]`, and `{"k": False} == {"k": 0}`,
    and the comparison of a number with a string is coerced for literals.
    """

    results = jsonpath_ng.ext.parse(path).find(items)

    assert_value_equality(results, expected_values)


@pytest.mark.parametrize(
    "path, items, expected_values",
    [
        pytest.param(
            "$[?(@.a < @.b)]",
            [{"a": 1, "b": "x"}, {"a": "x", "b": 1}, {"a": 1, "b": 2}],
            [{"a": 1, "b": 2}],
            id="number-and-string-are-not-ordered",
        ),
        pytest.param(
            "$[?(@.a < @.b)]",
            [{"a": "a", "b": "b"}, {"a": "b", "b": "a"}],
            [{"a": "a", "b": "b"}],
            id="strings-are-ordered",
        ),
        pytest.param(
            "$[?(@.a <= @.b)]",
            [{"a": None, "b": None}, {"a": None, "b": 1}],
            [{"a": None, "b": None}],
            id="null-ordering-reduces-to-equality",
        ),
        pytest.param(
            "$[?(@.a >= @.b)]",
            [{"a": True, "b": True}, {"a": True, "b": False}],
            [{"a": True, "b": True}],
            id="boolean-ordering-reduces-to-equality",
        ),
        pytest.param(
            "$[?(@.a < @.b)]",
            [{"a": [1], "b": [2]}, {"a": {"k": 1}, "b": {"k": 2}}],
            [],
            id="containers-are-not-ordered",
        ),
        pytest.param(
            "$[?(@.a > @.b)]",
            [{"a": True, "b": 0}, {"a": 1, "b": False}],
            [],
            id="boolean-is-not-a-number",
        ),
    ],
)
def test_query_operand_ordering_follows_rfc_9535(path, items, expected_values):
    """RFC 9535 section 2.3.5.2.2: `<` is only true for two numbers or two strings.

    Comparing values of different types is false and must not raise an exception.
    """

    results = jsonpath_ng.ext.parse(path).find(items)

    assert_value_equality(results, expected_values)


def test_query_operand_is_evaluated_for_every_candidate():
    """The right-hand query must be evaluated against each candidate, not the first."""

    items = [{"a": 1, "b": 1}, {"a": 1, "b": 2}, {"a": 2, "b": 2}, {"a": 3, "b": 1}]

    results = jsonpath_ng.ext.parse("$[?(@.a == @.b)]").find(items)

    assert_value_equality(results, [{"a": 1, "b": 1}, {"a": 2, "b": 2}])
    assert [str(result.full_path) for result in results] == ["[0]", "[2]"]


def test_query_operand_in_a_dict():
    """RFC 9535 section 2.3.5.2: a filter on an object selects its member values."""

    datum = {"x": {"a": 1, "b": 1}, "y": {"a": 1, "b": 2}}

    results = jsonpath_ng.ext.parse("$[?(@.a == @.b)]").find(datum)

    assert_value_equality(results, [{"a": 1, "b": 1}])
    assert [str(result.full_path) for result in results] == ["x"]


def test_relative_query_in_a_nested_filter_is_the_inner_candidate():
    """RFC 9535 section 2.3.5.1: `@` is the innermost filter's candidate."""

    datum = {"rows": [{"id": 1, "cells": [{"v": 1}, {"v": 2}]}, {"id": 2, "cells": []}]}

    results = jsonpath_ng.ext.parse("$.rows[?(@.cells[?(@.v == $.rows[0].id)])]").find(
        datum
    )

    assert_value_equality(results, [datum["rows"][0]])


@pytest.mark.parametrize(
    "path, datum, expected",
    [
        pytest.param(
            "$.items[?(@.v == $.ref.v)]",
            REF_DOC,
            {
                "ref": {"v": 1},
                "items": ["X", {"v": 2}, "X", {"w": 1}],
            },
            id="root-query",
        ),
        pytest.param(
            "$.items[?(@.a == @.b)]",
            PAIR_DOC,
            {"items": ["X", {"a": 1, "b": 2}, {"a": 3, "b": 2}]},
            id="current-query",
        ),
        pytest.param(
            "$.items[?(@.a != @.b)]",
            PAIR_DOC,
            {"items": [{"a": 1, "b": 1}, "X", "X"]},
            id="current-query-ne",
        ),
        pytest.param(
            "$.o..[?(@.v == $.k)]",
            {"k": 1, "o": {"p": [{"v": 1}, {"v": 2}], "q": [[{"v": 1}], [{"v": 3}]]}},
            {"k": 1, "o": {"p": ["X", {"v": 2}], "q": [["X"], [{"v": 3}]]}},
            id="root-query-descendants",
        ),
    ],
)
def test_update_with_query_operand(path, datum, expected):
    result = jsonpath_ng.ext.parse(path).update(copy.deepcopy(datum), "X")

    assert result == expected


def test_update_with_callable_and_root_query_operand():
    calls = []

    jsonpath_ng.ext.parse("$.items[?(@.v == $.ref.v)]").update(
        copy.deepcopy(REF_DOC), lambda value, parent, key: calls.append(key)
    )

    assert calls == [0, 2]


@pytest.mark.parametrize(
    "path, datum, expected",
    [
        pytest.param(
            "$.items[?(@.v == $.ref.v)]",
            REF_DOC,
            {"ref": {"v": 1}, "items": [{"v": 2}, {"w": 1}]},
            id="root-query",
        ),
        pytest.param(
            "$.items[?(@.a == @.b)]",
            PAIR_DOC,
            {"items": [{"a": 1, "b": 2}, {"a": 3, "b": 2}]},
            id="current-query",
        ),
        pytest.param(
            "$.o..[?(@.v == $.k)]",
            {"k": 1, "o": {"p": [{"v": 1}, {"v": 2}], "q": [[{"v": 1}], [{"v": 3}]]}},
            {"k": 1, "o": {"p": [{"v": 2}], "q": [[], [{"v": 3}]]}},
            id="root-query-descendants",
        ),
    ],
)
def test_filter_method_with_query_operand(path, datum, expected):
    result = jsonpath_ng.ext.parse(path).filter(
        lambda value: True, copy.deepcopy(datum)
    )

    assert result == expected


def test_root_in_query_operand_is_the_document_root():
    """`$` that is only in an `Expression.value` must still bind to the root."""

    comparison = Expression(
        Child(This(), Fields("v")), "==", Child(Root(), Fields("k"))
    )
    path = Child(Child(Root(), Fields("items")), Filter([comparison]))

    results = path.find({"k": 2, "items": [{"v": 1}, {"v": 2}]})

    assert_value_equality(results, [{"v": 2}])


@pytest.mark.parametrize(
    "path, expected",
    [
        pytest.param("$[?(@.v == $.ref.v)]", "@.v == $.ref.v", id="root"),
        pytest.param("$[?(@.a == @.b)]", "@.a == @.b", id="current"),
        pytest.param("$[?(@.a != @['b'][0])]", "@.a != @.b[0]", id="segments"),
        pytest.param("$[?(@.a == $)]", "@.a == $", id="bare-root"),
    ],
)
def test_query_operand_is_rendered_as_a_query(path, expected):
    expression = jsonpath_ng.ext.parse(path).right.expressions[0]

    assert str(expression) == expected


def test_query_operand_is_parsed_as_a_query():
    expression = jsonpath_ng.ext.parse("$[?(@.a == $.b[0])]").right.expressions[0]

    assert expression == Expression(
        Child(This(), Fields("a")),
        "==",
        Child(Child(Root(), Fields("b")), Index(0)),
    )


def test_quoted_string_on_the_right_is_a_string():
    """Only a right-hand side that starts with `@` or `$` is a query."""

    expression = jsonpath_ng.ext.parse('$[?(@.a == "foo")]').right.expressions[0]

    assert expression.value == "foo"


def test_path_that_does_not_start_with_a_root_or_current_node_is_rejected():
    """RFC 9535 section 2.3.5.1: a query operand starts with `@` or `$`."""

    with pytest.raises(JsonPathParserError):
        jsonpath_ng.ext.parse("$[?(@.a == foo.bar)]")


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("$[?(@.a == @.b[*])]", id="wildcard"),
        pytest.param("$[?(@.a == @.b[0:2])]", id="slice"),
        pytest.param("$[?(@.a == @.*)]", id="dot-wildcard"),
        pytest.param("$[?(@.a == @..b)]", id="descendants"),
        pytest.param("$[?(@.a == @.b[0, 1])]", id="index-union"),
        pytest.param("$[?(@.a == @['b', 'c'])]", id="name-union"),
        pytest.param("$[?(@.a == @.b[?(@.c)])]", id="filter"),
    ],
)
def test_right_hand_side_must_be_a_singular_query(path):
    """RFC 9535 section 2.3.5.1: a comparison operand is a *singular* query."""

    with pytest.raises(JsonPathParserError):
        jsonpath_ng.ext.parse(path)


# The tests below exercise deprecated syntax.
# Delete everything below this comment when v2.0.0 is released.


def test_bare_word_on_the_right_is_still_a_string():
    """A bare word has always been a string, because a field name could be a path."""

    with pytest.warns(DeprecationWarning, match="unquoted word"):
        expression = jsonpath_ng.ext.parse("$[?(@.a == foo)]").right.expressions[0]

    assert expression.value == "foo"
