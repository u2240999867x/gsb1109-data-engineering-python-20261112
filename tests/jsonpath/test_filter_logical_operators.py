"""
Verify RFC 9535 logical operators in extended filter expressions.

RFC 9535 section 2.3.5.1 defines `||` (OR) and `&&` (AND) for filter expressions.
`&&` binds more tightly than `||`, and parentheses group sub-expressions.
The operators are valid only inside a filter selector.
"""

import copy
import warnings

import pytest

import jsonpath_ng
from jsonpath_ng.exceptions import JsonPathParserError
from jsonpath_ng.ext import parse
from jsonpath_ng.ext.filter import Filter

ITEMS = [
    {"id": 1, "a": 1, "b": 1},
    {"id": 2, "a": 1, "b": 2},
    {"id": 3, "a": 2, "b": 1},
    {"id": 4, "a": 2, "b": 2},
    {"id": 5},
]


def ids(query, data):
    return [match.value["id"] for match in parse(query).find(data)]


@pytest.mark.parametrize(
    "query, expected_ids",
    [
        pytest.param("$[?@.a == 1 || @.b == 1]", [1, 2, 3], id="or"),
        pytest.param("$[?@.a == 1 && @.b == 1]", [1], id="and"),
        pytest.param("$[?@.a == 1 || @.zzz == 1]", [1, 2], id="or-one-side-missing"),
        pytest.param("$[?@.zzz == 1 || @.yyy == 1]", [], id="or-no-side-matches"),
        pytest.param("$[?@.a == 1 && @.b == 1 && @.id == 1]", [1], id="and-chain"),
        pytest.param(
            "$[?@.id == 1 || @.id == 3 || @.id == 5]", [1, 3, 5], id="or-chain"
        ),
        pytest.param("$[?@.a==1||@.b==1]", [1, 2, 3], id="or-without-whitespace"),
        pytest.param("$[?@.a==1&&@.b==1]", [1], id="and-without-whitespace"),
        pytest.param("$[?@.a == 1  ||  @.b == 1]", [1, 2, 3], id="or-extra-whitespace"),
        pytest.param(
            "$[?@.id == 5 || @.a]", [1, 2, 3, 4, 5], id="or-with-existence-test"
        ),
        pytest.param("$[?@.a && @.b]", [1, 2, 3, 4], id="and-of-existence-tests"),
        pytest.param("$[?!@.a || @.b == 1]", [1, 3, 5], id="or-with-negation"),
        pytest.param("$[?!@.a && @.id == 5]", [5], id="and-with-negation"),
        pytest.param(
            "$[?@.a == $[0].a || @.b == 2]",
            [1, 2, 4],
            id="or-with-root-reference",
        ),
        pytest.param(
            "$[?@.a == $[0].a && @.b == 2]",
            [2],
            id="and-with-root-reference",
        ),
    ],
)
def test_find(query, expected_ids):
    """A match is reported once, in document order, however many branches select it."""

    assert ids(query, ITEMS) == expected_ids


@pytest.mark.parametrize(
    "query, expected_ids",
    [
        # If `||` and `&&` had equal precedence and associated left-to-right,
        # the first query would be `(a == 2 || a == 1) && b == 1`, which is [1, 3].
        pytest.param(
            "$[?@.a == 2 || @.a == 1 && @.b == 1]",
            [1, 3, 4],
            id="and-binds-tighter-than-or-on-the-right",
        ),
        pytest.param(
            "$[?@.a == 1 && @.b == 1 || @.a == 2]",
            [1, 3, 4],
            id="and-binds-tighter-than-or-on-the-left",
        ),
        pytest.param(
            "$[?@.a == 1 && @.b == 2 || @.a == 2 && @.b == 1]",
            [2, 3],
            id="and-groups-on-both-sides-of-or",
        ),
        pytest.param(
            "$[?(@.a == 2 || @.a == 1) && @.b == 1]",
            [1, 3],
            id="parentheses-override-precedence-on-the-left",
        ),
        pytest.param(
            "$[?@.b == 1 && (@.a == 2 || @.a == 1)]",
            [1, 3],
            id="parentheses-override-precedence-on-the-right",
        ),
        pytest.param(
            "$[?@.a == 2 || (@.a == 1 && @.b == 1)]",
            [1, 3, 4],
            id="parentheses-matching-default-precedence",
        ),
        pytest.param(
            "$[?(@.a == 1 || @.b == 1)]",
            [1, 2, 3],
            id="parenthesized-or",
        ),
        pytest.param(
            "$[?((@.a == 1 || @.b == 1) && @.id != 2)]",
            [1, 3],
            id="nested-parentheses",
        ),
    ],
)
def test_precedence_and_grouping(query, expected_ids):
    assert ids(query, ITEMS) == expected_ids


def test_find_in_nested_data():
    data = {"x": {"y": copy.deepcopy(ITEMS)}}

    assert ids("$.x.y[?@.a == 1 || @.b == 1]", data) == [1, 2, 3]


def test_update_or():
    data = copy.deepcopy(ITEMS)

    updated = parse("$[?@.a == 1 || @.b == 1]").update(data, "X")

    assert updated == ["X", "X", "X", ITEMS[3], ITEMS[4]]


def test_update_and():
    data = copy.deepcopy(ITEMS)

    updated = parse("$[?@.a == 1 && @.b == 1]").update(data, "X")

    assert updated == ["X", *ITEMS[1:]]


def test_update_with_root_reference():
    data = {"ref": {"a": 1}, "items": copy.deepcopy(ITEMS)}

    updated = parse("$.items[?@.a == $.ref.a || @.b == 2]").update(data, "X")

    assert updated["items"] == ["X", "X", ITEMS[2], "X", ITEMS[4]]


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("$.a && $.b", id="and-between-paths"),
        pytest.param("$.a || $.b", id="or-between-paths"),
        pytest.param("$[0] && $[1]", id="and-between-indexes"),
        pytest.param("$.a[?@.b] && $.c", id="and-after-filter"),
        pytest.param("$.a[?@.b] || $.c", id="or-after-filter"),
        pytest.param("$.a && ", id="and-trailing"),
        pytest.param("|| $.a", id="or-leading"),
    ],
)
def test_operators_are_invalid_outside_a_filter(query):
    with pytest.raises(JsonPathParserError):
        parse(query)


@pytest.mark.parametrize("query", ["$.a && $.b", "$.a || $.b", "$.a[0] || $.b"])
def test_operators_are_invalid_outside_a_filter_in_core_parser(query):
    with pytest.raises(JsonPathParserError):
        jsonpath_ng.parse(query)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("$[?@.a ||]", id="or-trailing"),
        pytest.param("$[?@.a &&]", id="and-trailing"),
        pytest.param("$[?|| @.a]", id="or-leading"),
        pytest.param("$[?&& @.a]", id="and-leading"),
        pytest.param("$[?@.a || || @.b]", id="or-repeated"),
        pytest.param("$[?@.a && && @.b]", id="and-repeated"),
        pytest.param("$[?@.a ||| @.b]", id="three-pipes"),
        pytest.param("$[?@.a &&& @.b]", id="three-ampersands"),
        pytest.param("$[?@.a || && @.b]", id="or-then-and"),
        pytest.param("$[?(@.a == 1 || @.b == 1]", id="unbalanced-parentheses"),
    ],
)
def test_malformed_logical_expressions_are_rejected(query):
    with pytest.raises(JsonPathParserError):
        parse(query)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("$[?@.a == 1 | @.b == 1]", id="pipe"),
        pytest.param("$[?(@.a == 1) | (@.b == 1)]", id="pipe-between-parentheses"),
        pytest.param("$[?@.a = 1 | @.b = 1]", id="pipe-single-equals"),
    ],
)
def test_single_pipe_is_not_a_filter_or(query):
    """`|` is not an RFC 9535 operator, so it must not become a filter OR."""

    with pytest.raises(JsonPathParserError):
        parse(query)


def test_ast_represents_precedence():
    from jsonpath_ng.ext.filter import And
    from jsonpath_ng.ext.filter import Expression
    from jsonpath_ng.ext.filter import Or

    (expression,) = parse("$[?@.a == 1 || @.b == 1 && @.id == 1]").right.expressions
    assert isinstance(expression, Or)
    assert [type(operand) for operand in expression.operands] == [Expression, And]

    (expression,) = parse("$[?@.a == 1 && @.b == 1 || @.id == 1]").right.expressions
    assert isinstance(expression, Or)
    assert [type(operand) for operand in expression.operands] == [And, Expression]


def test_ast_equality():
    query = "$[?@.a == 1 && @.b == 1 || @.id == 1]"

    assert parse(query) == parse(query)
    assert parse(query) != parse("$[?@.a == 1 && @.b == 1 || @.id == 2]")
    assert parse("$[?@.a == 1 || @.b == 1]") != parse("$[?@.a == 1 && @.b == 1]")


ROUNDTRIP_QUERIES = [
    pytest.param("$[?@.a == 1 && @.b == 2]", id="and"),
    pytest.param("$[?@.a == 1 || @.b == 2]", id="or"),
    pytest.param("$[?@.a == 1 && @.b == 2 && @.id == 2]", id="and-chain"),
    pytest.param("$[?@.a == 1 || @.b == 2 || @.id == 5]", id="or-chain"),
    pytest.param("$[?@.a == 1 && @.b == 2 || @.id == 5]", id="and-then-or"),
    pytest.param("$[?@.id == 5 || @.a == 1 && @.b == 2]", id="or-then-and"),
    pytest.param(
        "$[?@.a == 1 && @.b == 1 || @.a == 2 && @.b == 2]", id="and-around-or"
    ),
    pytest.param("$[?(@.a == 1 || @.b == 1) && @.id != 2]", id="grouped-or-left"),
    pytest.param("$[?@.id != 2 && (@.a == 1 || @.b == 1)]", id="grouped-or-right"),
    pytest.param("$[?@.a == 1 && (@.b == 2 && @.id == 2)]", id="grouped-and-right"),
    pytest.param("$[?(@.a == 1 && @.b == 2) && @.id == 2]", id="grouped-and-left"),
    pytest.param(
        "$[?@.a == 1 || (@.b == 2 || @.id == 5)]", id="grouped-or-right-in-or"
    ),
    pytest.param("$[?(@.a == 1 || @.b == 2) || @.id == 5]", id="grouped-or-left-in-or"),
    pytest.param("$[?((@.a == 1 || @.b == 1) && @.id != 2)]", id="nested-groups"),
    pytest.param("$[?@.a == 1 && @.b == 2 || @.id == 5 && @.a == 2]", id="two-ands"),
    pytest.param("$[?!@.a || @.b == 1]", id="negation-in-or"),
    pytest.param("$[?!@.a && @.id == 5]", id="negation-in-and"),
    pytest.param("$[?@.a || @.b]", id="existence-tests"),
    pytest.param("$[?@.a == 'x' || @.b == \"y z\"]", id="string-literals"),
    pytest.param("$[?@.a == null || @.b != true && @.id == false]", id="constants"),
    pytest.param("$[?@.a > 1 && @.b <= 2 || @.id < 3]", id="ordering-operators"),
    pytest.param("$[?@.a == $[0].a || @.b == 2]", id="root-reference"),
    pytest.param("$[?@.a == @.b || @.id == $[0].id]", id="query-operands"),
    pytest.param("$[?@['a'] == 1 || @.b[0] == 2]", id="bracketed-targets"),
    pytest.param("$[?@.a == 1 || @.b == 1].id", id="trailing-segment"),
    pytest.param("$.x[?@.a == 1 || @.b == 1]", id="nested-path"),
    pytest.param("$..[?@.a == 1 || @.b == 1]", id="descendants"),
    pytest.param(
        "$[?@.a == 1 || @.b == 1][?@.id > 1 && @.id < 4]", id="chained-filters"
    ),
    pytest.param("$[?@.a==1||@.b==1]", id="no-whitespace"),
    pytest.param("$[?@.a = 1 || @.b = 1]", id="single-equals"),
    pytest.param("$[?@.a =~ '^1' || @.b == 1]", id="regex-operator"),
]


def _roundtrip(query):
    original = parse(query)
    serialized = str(original)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        reparsed = parse(serialized)

    return original, serialized, reparsed


@pytest.mark.parametrize("query", ROUNDTRIP_QUERIES)
def test_roundtrip_is_equal(query):
    original, serialized, reparsed = _roundtrip(query)

    assert reparsed == original, serialized


@pytest.mark.parametrize("query", ROUNDTRIP_QUERIES)
def test_roundtrip_is_functionally_identical(query):
    original, _, reparsed = _roundtrip(query)

    data = {"x": copy.deepcopy(ITEMS), **{str(i): item for i, item in enumerate(ITEMS)}}
    for datum in (ITEMS, data, {"x": ITEMS}):
        expected = [(str(m.full_path), m.value) for m in original.find(datum)]
        actual = [(str(m.full_path), m.value) for m in reparsed.find(datum)]
        assert actual == expected


@pytest.mark.parametrize("query", ROUNDTRIP_QUERIES)
def test_roundtrip_serialization_is_stable(query):
    """Serializing the reparsed query must reproduce the first serialization."""

    _, serialized, reparsed = _roundtrip(query)

    assert str(reparsed) == serialized


@pytest.mark.parametrize(
    "query, expected",
    [
        pytest.param("$[?@.a == 1 && @.b == 2]", None, id="and"),
        pytest.param("$[?@.a == 1 || @.b == 2]", None, id="or"),
        pytest.param("$[?@.a == 1 && @.b == 2 && @.id == 2]", None, id="and-chain"),
        pytest.param("$[?@.a == 1 || @.b == 2 || @.id == 5]", None, id="or-chain"),
        pytest.param("$[?@.a == 1 && @.b == 2 || @.id == 5]", None, id="and-then-or"),
        pytest.param("$[?@.id == 5 || @.a == 1 && @.b == 2]", None, id="or-then-and"),
        pytest.param(
            "$[?(@.a == 1 || @.b == 1) && @.id != 2]",
            None,
            id="parentheses-needed-left",
        ),
        pytest.param(
            "$[?@.id != 2 && (@.a == 1 || @.b == 1)]",
            None,
            id="parentheses-needed-right",
        ),
        pytest.param(
            "$[?(@.a == 1 && @.b == 2) || @.id == 5]",
            "$[?@.a == 1 && @.b == 2 || @.id == 5]",
            id="redundant-parentheses-around-and-are-dropped",
        ),
        pytest.param(
            "$[?(@.a == 1 && @.b == 2)]",
            "$[?@.a == 1 && @.b == 2]",
            id="redundant-outer-parentheses-are-dropped",
        ),
        pytest.param(
            "$[?(@.a == 1 || @.b == 2)]",
            "$[?@.a == 1 || @.b == 2]",
            id="redundant-outer-parentheses-around-or-are-dropped",
        ),
        pytest.param("$[?!@.a || @.b == 1]", None, id="negation"),
        pytest.param("$[?@.a == 'x' || @.b == null]", None, id="literals"),
        pytest.param(
            '$[?@.a == "x" || @.b == 1]',
            "$[?@.a == 'x' || @.b == 1]",
            id="strings-use-single-quotes",
        ),
        pytest.param("$.x[?@.a == 1 || @.b == 1].id", None, id="path-around-filter"),
        pytest.param("$[?@.a == $[0].a && @.b == 2]", None, id="root-reference"),
    ],
)
def test_serialization_is_canonical(query, expected):
    """Canonical queries serialize to themselves; the rest to their canonical form."""

    assert str(parse(query)) == (expected or query)


def test_serialization_of_a_directly_constructed_conjunction():
    """A filter's expression list is a conjunction, and so it serializes as `&&`."""

    expressions = [
        parse("$[?@.a == 1]").right.expressions[0],
        parse("$[?@.b == 2]").right.expressions[0],
    ]

    assert str(Filter(expressions)) == "[?@.a == 1 && @.b == 2]"
    assert parse("$[?@.a == 1 && @.b == 2]").right == Filter(expressions)


# The tests below exercise deprecated syntax.
# Delete everything below this comment when v2.0.0 is released.


@pytest.mark.parametrize(
    "legacy_query, query",
    [
        # Left to right would be `(a == 2 || a == 1) & b == 1`, which is [1, 3].
        pytest.param(
            "$[?@.a == 2 || @.a == 1 & @.b == 1]",
            "$[?@.a == 2 || @.a == 1 && @.b == 1]",
            id="ampersand-after-or",
        ),
        # `||` binding more tightly than `&` would be `a == 1 & (b == 1 || a == 2)`,
        # which is [1]. (Left to right gives the correct result for this order.)
        pytest.param(
            "$[?@.a == 1 & @.b == 1 || @.a == 2]",
            "$[?@.a == 1 && @.b == 1 || @.a == 2]",
            id="ampersand-before-or",
        ),
    ],
)
def test_legacy_ampersand_binds_more_tightly_than_or(legacy_query, query):
    """The deprecated `&` is an alias for `&&`, so it also binds tighter than `||`."""

    with pytest.warns(DeprecationWarning):
        legacy = parse(legacy_query)

    assert [match.value["id"] for match in legacy.find(ITEMS)] == [1, 3, 4]
    assert legacy == parse(query)


def test_legacy_ampersand_serializes_as_double_ampersand():
    """Serialization must not emit deprecated syntax, or re-parsing would warn."""

    with pytest.warns(DeprecationWarning):
        original = parse("$[?@.a == 1 & @.b == 1]")

    reparsed = parse("$[?@.a == 1 && @.b == 1]")

    assert str(original) == "$[?@.a == 1 && @.b == 1]"
    assert reparsed == original
