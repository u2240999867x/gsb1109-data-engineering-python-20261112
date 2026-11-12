"""Tests for finite-structure cardinality analysis."""

from __future__ import annotations

import pytest

from jsonpath_ng.cardinality import ANY
from jsonpath_ng.cardinality import ArrayConstraint
from jsonpath_ng.cardinality import CardinalityError
from jsonpath_ng.cardinality import ObjectConstraint
from jsonpath_ng.cardinality import ValueConstraint
from jsonpath_ng.cardinality import matches_constraint
from jsonpath_ng.cardinality import maximum_cardinality
from jsonpath_ng.ext.parser import parse as ext_parse
from jsonpath_ng.jsonpath import Child
from jsonpath_ng.jsonpath import Fields
from jsonpath_ng.jsonpath import Index
from jsonpath_ng.jsonpath import Root
from jsonpath_ng.jsonpath import _Union
from jsonpath_ng.parser import parse as base_parse

STR = ValueConstraint(("a", "b"))
NUM = ValueConstraint((1, 2, 3))
ROLE = ValueConstraint(("admin", "user", "guest"))


def assert_cardinality(expression, constraint, expected):
    """Assert the claimed maximum and that the sample actually reaches it."""

    result = maximum_cardinality(expression, constraint)
    assert result.maximum == expected
    if expected is None:
        assert result.sample is None
        return
    assert result.is_known
    assert matches_constraint(result.sample, constraint)
    assert len(expression.find(result.sample)) == expected


@pytest.mark.parametrize(
    "factory",
    (
        pytest.param(lambda: ValueConstraint(()), id="empty-enum"),
        pytest.param(lambda: ValueConstraint("ab"), id="enum-not-sequence"),
        pytest.param(
            lambda: ObjectConstraint(
                properties={"a": STR}, required=("missing",)
            ),
            id="missing-required-key",
        ),
        pytest.param(
            lambda: ArrayConstraint(element=NUM, min_length=3, max_length=2),
            id="inverted-length-range",
        ),
        pytest.param(
            lambda: ArrayConstraint(element=NUM, min_length=-1),
            id="negative-min-length",
        ),
        pytest.param(lambda: ArrayConstraint(alternatives=()), id="empty-alternatives"),
        pytest.param(
            lambda: ArrayConstraint(
                alternatives=(
                    ArrayConstraint(
                        alternatives=(ArrayConstraint(element=NUM, max_length=1),)
                    ),
                )
            ),
            id="nested-alternatives",
        ),
    ),
)
def test_invalid_constraints_raise(factory):
    with pytest.raises(CardinalityError):
        factory()


def test_rejects_non_constraint():
    with pytest.raises(CardinalityError):
        maximum_cardinality(base_parse("$.a"), {"not": "a constraint"})


def test_value_constraint_membership():
    assert matches_constraint("a", STR)
    assert not matches_constraint("c", STR)
    # Booleans never compare equal to numbers.
    assert not matches_constraint(True, ValueConstraint((1, 2)))


def test_object_constraint_membership():
    constraint = ObjectConstraint(
        properties={"a": STR, "b": NUM}, required=("a",)
    )
    assert matches_constraint({"a": "a"}, constraint)
    assert matches_constraint({"a": "b", "b": 2}, constraint)
    assert not matches_constraint({"b": 1}, constraint)
    assert not matches_constraint({"a": "a", "c": 1}, constraint)

    open_constraint = ObjectConstraint(
        properties={"a": STR}, required=("a",), additional_properties=True
    )
    assert matches_constraint({"a": "a", "anything": [1, 2]}, open_constraint)


def test_array_alternatives_membership():
    constraint = ArrayConstraint(
        alternatives=(
            ArrayConstraint(element=NUM, min_length=0, max_length=1),
            ArrayConstraint(element=NUM, min_length=3, max_length=4),
        )
    )
    assert matches_constraint([], constraint)
    assert matches_constraint([1], constraint)
    assert matches_constraint([1, 2, 3], constraint)
    assert not matches_constraint([1, 2], constraint)
    assert not matches_constraint([1, 2, 3, 4, 5], constraint)


@pytest.mark.parametrize(
    "path_text,expected",
    (
        ("$.a", 1),
        ("$.opt", 1),
        ("$.missing", 0),
        ("$.a,b", 2),
        ("$.a,missing", 1),
        ("$.a,a", 2),
    ),
)
def test_fields(path_text, expected):
    constraint = ObjectConstraint(
        properties={"a": STR, "b": NUM, "opt": STR},
        required=("a", "b"),
    )
    assert_cardinality(base_parse(path_text), constraint, expected)


def test_fields_additional_properties():
    constraint = ObjectConstraint(
        properties={"a": STR}, required=("a",), additional_properties=True
    )
    assert_cardinality(base_parse("$.extra"), constraint, 1)
    assert_cardinality(ext_parse("$[*]"), constraint, None)


def test_wildcard_on_closed_object():
    constraint = ObjectConstraint(
        properties={"a": STR, "b": NUM, "c": STR}, required=()
    )
    assert_cardinality(ext_parse("$[*]"), constraint, 3)


@pytest.mark.parametrize(
    "path_text,expected",
    (
        ("$[0]", 1),
        ("$[9]", 0),
        ("$[-1]", 1),
        ("$[0,2,3]", 3),
        ("$[1,1]", 2),
    ),
)
def test_index(path_text, expected):
    constraint = ArrayConstraint(element=NUM, min_length=1, max_length=4)
    assert_cardinality(base_parse(path_text), constraint, expected)


def test_index_on_non_array_is_zero():
    constraint = ObjectConstraint(properties={"a": STR}, required=("a",))
    assert_cardinality(base_parse("$[0]"), constraint, 0)


def test_index_unbounded_is_unknown():
    constraint = ArrayConstraint(element=NUM, min_length=0, max_length=None)
    assert_cardinality(base_parse("$[0]"), constraint, None)


@pytest.mark.parametrize(
    "path_text,expected",
    (
        ("$[*]", 4),
        ("$[1:3]", 2),
        ("$[::2]", 2),
        ("$[::-1]", 4),
        ("$[2:0:-1]", 2),
        ("$[10:20]", 0),
    ),
)
def test_slices(path_text, expected):
    constraint = ArrayConstraint(element=NUM, min_length=1, max_length=4)
    assert_cardinality(ext_parse(path_text), constraint, expected)


def test_slice_on_object_is_zero():
    constraint = ObjectConstraint(properties={"a": STR}, required=("a",))
    assert_cardinality(ext_parse("$[1:3]"), constraint, 0)


def test_length_alternatives_are_not_summed():
    constraint = ArrayConstraint(
        alternatives=(
            ArrayConstraint(element=NUM, min_length=0, max_length=2),
            ArrayConstraint(element=NUM, min_length=3, max_length=5),
        )
    )
    # The two intervals are mutually exclusive, so the maximum is 5, not 7.
    assert_cardinality(ext_parse("$[*]"), constraint, 5)
    assert_cardinality(base_parse("$[4]"), constraint, 1)
    assert_cardinality(base_parse("$[5]"), constraint, 0)


def test_nested_child_path():
    constraint = ObjectConstraint(
        properties={
            "items": ArrayConstraint(
                element=ObjectConstraint(
                    properties={"k": STR, "v": NUM}, required=("k",)
                ),
                min_length=2,
                max_length=3,
            )
        },
        required=("items",),
    )
    assert_cardinality(ext_parse("$.items[*].k"), constraint, 3)
    assert_cardinality(ext_parse("$.items[1].v"), constraint, 1)
    assert_cardinality(ext_parse("$.missing[*].k"), constraint, 0)


def test_unbounded_descendant_is_unknown():
    constraint = ObjectConstraint(
        properties={
            "items": ArrayConstraint(element=NUM, min_length=0, max_length=None)
        },
        required=("items",),
    )
    assert_cardinality(ext_parse("$.items[*]"), constraint, None)


def test_union_counts_repeated_hits():
    constraint = ObjectConstraint(
        properties={"a": STR, "b": NUM}, required=("a", "b")
    )
    root_a = Child(Root(), Fields("a"))
    assert_cardinality(_Union(root_a, root_a), constraint, 2)
    assert_cardinality(
        _Union(root_a, Child(Root(), Fields("missing"))), constraint, 1
    )


def test_union_on_array_overlap():
    constraint = ArrayConstraint(element=NUM, min_length=2, max_length=4)
    expression = _Union(Child(Root(), Index(0, 1)), Child(Root(), Index(1)))
    assert_cardinality(expression, constraint, 3)


@pytest.fixture
def people():
    return ArrayConstraint(
        element=ObjectConstraint(
            properties={"role": ROLE, "active": ValueConstraint((True, False))},
            required=("role",),
        ),
        min_length=2,
        max_length=4,
    )


def test_enum_equality_filter_all_match(people):
    assert_cardinality(ext_parse("$[?@.role == 'admin']"), people, 4)


def test_enum_equality_filter_no_match(people):
    assert_cardinality(ext_parse("$[?@.role == 'root']"), people, 0)


def test_filter_then_child(people):
    assert_cardinality(
        ext_parse("$[?@.role == 'admin'].active"), people, 4
    )


def test_filter_on_optional_member_then_child():
    # The field that satisfies the filter must survive when the right-hand
    # selection is overlaid onto the filter's candidate objects.
    element = ObjectConstraint(
        properties={
            "role": ValueConstraint(("admin", "user")),
            "n": NUM,
        },
        required=("role",),
    )
    constraint = ArrayConstraint(element=element, min_length=2, max_length=3)
    assert_cardinality(ext_parse("$[?@.n == 2].role"), constraint, 3)
    assert_cardinality(ext_parse("$[?@.role == 'admin'].n"), constraint, 3)


def test_filter_through_nested_object_then_child():
    element = ObjectConstraint(
        properties={
            "role": ROLE,
            "meta": ObjectConstraint(
                properties={"n": NUM}, required=("n",)
            ),
        },
        required=("role",),
    )
    constraint = ArrayConstraint(element=element, min_length=1, max_length=2)
    assert_cardinality(ext_parse("$[?@.meta.n == 2].role"), constraint, 2)
    assert_cardinality(ext_parse("$[?@.role == 'admin'].meta.n"), constraint, 2)


def test_filter_with_optional_member():
    constraint = ArrayConstraint(
        element=ObjectConstraint(
            properties={"role": ValueConstraint(("admin", "user"))}, required=()
        ),
        min_length=1,
        max_length=3,
    )
    assert_cardinality(ext_parse("$[?@.role == 'admin']"), constraint, 3)


def test_filter_over_length_alternatives():
    element = ObjectConstraint(
        properties={"role": ROLE}, required=("role",)
    )
    constraint = ArrayConstraint(
        alternatives=(
            ArrayConstraint(element=element, min_length=0, max_length=2),
            ArrayConstraint(element=element, min_length=3, max_length=5),
        )
    )
    assert_cardinality(ext_parse("$[?@.role == 'admin']"), constraint, 5)


def test_undecidable_alternative_dominated_by_decidable_one():
    decidable = ObjectConstraint(
        properties={"role": ROLE}, required=("role",)
    )
    undecidable = ObjectConstraint(
        properties={"role": ROLE},
        required=("role",),
        additional_properties=True,
    )
    constraint = ArrayConstraint(
        alternatives=(
            # The decidable alternative reaches four passing elements.
            ArrayConstraint(element=decidable, min_length=3, max_length=4),
            # The undecidable alternative can never contribute more than three.
            ArrayConstraint(element=undecidable, min_length=1, max_length=3),
        )
    )
    assert_cardinality(
        ext_parse("$[?@.role == 'admin' || @.weird == 1]"), constraint, 4
    )


def test_filter_on_object_members():
    constraint = ObjectConstraint(
        properties={
            "x": ObjectConstraint(
                properties={"role": ValueConstraint(("admin", "user"))},
                required=("role",),
            ),
            "y": ObjectConstraint(
                properties={"role": ValueConstraint(("guest",))},
                required=("role",),
            ),
        },
        required=("x", "y"),
    )
    assert_cardinality(ext_parse("$[?@.role == 'admin']"), constraint, 1)
    assert_cardinality(ext_parse("$[?@.role == 'root']"), constraint, 0)


def test_filter_on_scalar_root_matches_nothing():
    assert_cardinality(ext_parse("$[?@ == 1]"), NUM, 0)


def test_this_equality_over_scalar_array():
    constraint = ArrayConstraint(element=NUM, min_length=2, max_length=4)
    assert_cardinality(ext_parse("$[?@ == 1]"), constraint, 4)


def test_existence_filters(people):
    assert_cardinality(ext_parse("$[?@.role]"), people, 4)
    assert_cardinality(ext_parse("$[?@.missing]"), people, 0)
    assert_cardinality(ext_parse("$[?!@.missing]"), people, 4)


def test_logical_filters(people):
    assert_cardinality(
        ext_parse("$[?@.role == 'admin' || @.role == 'guest']"), people, 4
    )
    # One member cannot equal two distinct enumerated values at once.
    assert_cardinality(
        ext_parse("$[?@.role == 'admin' && @.role == 'guest']"), people, 0
    )


@pytest.mark.parametrize(
    "path_text",
    (
        "$..role",
        "$[?@.active != true]",
        "$[?@.role =~ 'a.*']",
        "$[?@.role == $.name]",
    ),
)
def test_unsupported_or_unbounded_is_unknown(path_text, people):
    assert_cardinality(ext_parse(path_text), people, None)


def test_any_constraint_is_unknown():
    assert_cardinality(base_parse("$.a"), ANY, None)


def test_single_valued_property():
    constraint = ObjectConstraint(properties={"a": STR}, required=("a",))
    result = maximum_cardinality(base_parse("$.a"), constraint)
    assert result.is_single_valued

    array = ArrayConstraint(element=NUM, min_length=2, max_length=4)
    assert not maximum_cardinality(ext_parse("$[*]"), array).is_single_valued
