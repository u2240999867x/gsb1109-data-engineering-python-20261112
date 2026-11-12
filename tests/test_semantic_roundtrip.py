"""
Semantic round-trip tests for saved JSONPath rules.

The syntactic round-trip tests in ``test_roundtrip.py`` only prove that a parsed
rule and its re-parsed text form are the same AST. Two different rules can read
the *same values* while pointing at different locations in a document, so these
tests compare semantics instead:

*   the exact locations hit (``DatumInContext.full_path``),
*   the order and the number of repetitions of those locations,
*   whether every locator can be re-parsed at the root and re-anchor at the
    node it was produced from,
*   the locations that actually change when the matches are write targets.

The ordinary parser and the extended parser get separate guarantees. Rules that
cannot be textualized reliably (for example a bare ``SortedThis`` whose
``__str__`` raises), or whose matches do not identify document positions, are
exercised as boundary tests instead. Where a rule and its re-parse can diverge,
property-based tests use Hypothesis shrinking so the smallest expression and
document pair that still exhibits the divergence is reported.
"""

from __future__ import annotations

import copy
import typing
import warnings

import pytest
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from jsonpath_ng import Child
from jsonpath_ng import Descendants
from jsonpath_ng import Fields
from jsonpath_ng import Index
from jsonpath_ng import JSONPath
from jsonpath_ng import Root
from jsonpath_ng import Slice
from jsonpath_ng import This
from jsonpath_ng import Where
from jsonpath_ng import WhereNot
from jsonpath_ng import Wildcard
from jsonpath_ng.ext.parser import ExtendedJsonPathParser
from jsonpath_ng.parser import JsonPathParser

# Reusing one parser per module avoids paying the PLY table loading cost on
# every property-test example.
CORE_PARSER = JsonPathParser()
EXT_PARSER = ExtendedJsonPathParser()

SENTINEL = "__roundtrip_sentinel__"

# Keys deliberately mix ordinary names, dotted literal names, and duplicates.
KEYS = ("a", "a.b", "b", "dup", "x", "v", "items", "target")


# ---------------------------------------------------------------------------
# Shared documents and comparison helpers
# ---------------------------------------------------------------------------

# A document with equal values at distinct positions, dotted literal keys, and
# repeated array members so that order and repetition are observable.
SEMANTIC_DOC = {
    "a.b": {"v": 1, "dup": 1},
    "a": {
        "b": {"v": 1, "dup": 1},
        "items": [
            {"k": 1, "v": "same"},
            {"k": 1, "v": "same"},
            {"k": 2, "v": "other"},
        ],
    },
    "dup": 1,
    "other": {"dup": 1, "x": 1},
    "x": 1,
    "target": 1,
}

DOCUMENTS = (
    SEMANTIC_DOC,
    {
        "a.b": 1,
        "a": {"b": 1, "z": {"x": 1}},
        "items": [
            {"k": 1, "v": "a"},
            {"k": 1, "v": "b"},
            {"k": 2, "v": "c"},
        ],
        "target": 1,
    },
    {
        "a": [{"b": {"x": 1}}, {"b": {"x": 1, "m": 0}}],
        "b": [{"x": 1}, {"c": 2}],
        "x": [1, 2, 1],
    },
    [{"x": 1}, {"x": 1}, {"a": 1}],
)


def field(*names: str) -> Fields:
    """Build a `Fields` node without triggering wildcard deprecations."""

    return Fields._compliant_constructor(*names)


def locators(rule: JSONPath, doc: typing.Any) -> list[str]:
    """Return the ordered, repetition-preserving list of match locators."""

    return [str(match.full_path) for match in rule.find(doc)]


def values(rule: JSONPath, doc: typing.Any) -> list[typing.Any]:
    return [match.value for match in rule.find(doc)]


def changed_locations(
    before: typing.Any, after: typing.Any, trail: tuple = ()
) -> set[tuple]:
    """Locate every leaf whose value differs between two documents."""

    changed: set[tuple] = set()
    if type(before) is not type(after):
        return {trail}
    if isinstance(before, dict):
        for key in before.keys() | after.keys():
            if key not in before or key not in after:
                changed.add(trail + (key,))
            else:
                changed |= changed_locations(before[key], after[key], trail + (key,))
    elif isinstance(before, list):
        for index in range(max(len(before), len(after))):
            if index >= len(before) or index >= len(after):
                changed.add(trail + (index,))
            else:
                changed |= changed_locations(
                    before[index], after[index], trail + (index,)
                )
    elif before != after:
        changed.add(trail)
    return changed


def write_targets(rule: JSONPath, doc: typing.Any) -> set[tuple]:
    """Apply the rule as a write target and report every changed location."""

    writable = copy.deepcopy(doc)
    rule.update(writable, SENTINEL)
    return changed_locations(doc, writable)


def assert_locators_re_anchor(
    parser: JsonPathParser, rule: JSONPath, doc: typing.Any
) -> None:
    """Each locator must, when parsed at the root, find its node again."""

    for match in rule.find(doc):
        locator = str(match.full_path)
        re_anchored = parser.parse(locator).find(doc)
        assert len(re_anchored) == 1, locator
        assert re_anchored[0].value == match.value, locator


def assert_semantic_roundtrip(
    parser: JsonPathParser, rule: JSONPath, doc: typing.Any
) -> None:
    """Textualize, re-parse, then compare locations, repetition, and writes."""

    reparsed = parser.parse(str(rule))

    expected_locators = locators(rule, doc)
    # Ordered equality covers both sequence and number of repetitions.
    assert locators(reparsed, doc) == expected_locators
    assert values(reparsed, doc) == values(rule, doc)
    assert write_targets(reparsed, doc) == write_targets(rule, doc)

    if expected_locators:
        assert_locators_re_anchor(parser, rule, doc)
        assert_locators_re_anchor(parser, reparsed, doc)


# ---------------------------------------------------------------------------
# Deterministic guarantees: ordinary parser
# ---------------------------------------------------------------------------

CORE_QUERIES = (
    "$",
    "`this`",
    "a",
    "$.a.b",
    "$['a.b']",
    "$['a.b'].v",
    "$.a.items[*].v",
    "$.a.items[0,1,0].k",
    "$.a.items[-1].v",
    "$.a.items[:2].v",
    "$.a.items[::2].v",
    "$..dup",
    "$.a..x",
    "a,dup",
    "a.b where v",
    "a.b wherenot missing",
    "($..x)",
)


@pytest.mark.parametrize("query", CORE_QUERIES)
@pytest.mark.parametrize("doc", DOCUMENTS)
def test_core_guaranteed_subset_roundtrips_semantically(query, doc):
    rule = CORE_PARSER.parse(query)

    assert_semantic_roundtrip(CORE_PARSER, rule, doc)


@pytest.mark.parametrize("query", CORE_QUERIES)
def test_core_subset_is_also_guaranteed_by_the_extended_parser(query):
    # Every ordinary-parser rule is accepted by the extended parser and keeps
    # the same semantics after textualization there.
    rule = CORE_PARSER.parse(query)
    ext_rule = EXT_PARSER.parse(str(rule))

    for doc in DOCUMENTS:
        assert locators(ext_rule, doc) == locators(rule, doc)
        assert write_targets(ext_rule, doc) == write_targets(rule, doc)


# ---------------------------------------------------------------------------
# Deterministic guarantees: extended parser
# ---------------------------------------------------------------------------

EXT_QUERIES = (
    "@",
    "a.items[?@.k == 1].v",
    "a.items[?@.k].v",
    "a.items[?@.k == $.target].k",
    "a.items[?@.k == 1 && @.v == 'same'].v",
    "a.items[?@.k == 2 || @.v == 'same'].v",
    "a.items[?!@.m].k",
    "a.items[?@.v =~ 'sa.*'].v",
    "a.items[?@.k != null].v",
    "a.items[?@['a.b'] == 1].v",
    "$.a.items[?@.k == 1][0].v",
)


@pytest.mark.parametrize("query", EXT_QUERIES)
@pytest.mark.parametrize("doc", DOCUMENTS)
def test_ext_guaranteed_subset_roundtrips_semantically(query, doc):
    rule = EXT_PARSER.parse(query)

    assert_semantic_roundtrip(EXT_PARSER, rule, doc)


# ---------------------------------------------------------------------------
# Discrimination: equal read values must not imply equal write targets
# ---------------------------------------------------------------------------

def test_dotted_literal_and_nested_path_read_equal_but_write_elsewhere():
    dotted = CORE_PARSER.parse("$['a.b']")
    nested = CORE_PARSER.parse("$.a.b")
    doc = {"a.b": 1, "a": {"b": 1}}

    # The two rules read the same value ...
    assert values(dotted, doc) == values(nested, doc) == [1]
    assert locators(dotted, doc) == ["'a.b'"]
    assert locators(nested, doc) == ["a.b"]

    # ... but writing through them changes two different lines.
    dotted_doc = copy.deepcopy(doc)
    nested_doc = copy.deepcopy(doc)
    dotted.update(dotted_doc, SENTINEL)
    nested.update(nested_doc, SENTINEL)

    assert dotted_doc["a.b"] == SENTINEL and dotted_doc["a"] == {"b": 1}
    assert nested_doc["a"] == {"b": SENTINEL} and nested_doc["a.b"] == 1
    assert changed_locations(doc, dotted_doc) == {("a.b",)}
    assert changed_locations(doc, nested_doc) == {("a", "b")}


def test_descendant_order_and_duplicates_are_stable_across_reparse():
    rule = CORE_PARSER.parse("$..dup")
    reparsed = CORE_PARSER.parse(str(rule))

    # Document order: the root member precedes the depth-first descendants.
    expected = ["dup", "'a.b'.dup", "a.b.dup", "other.dup"]
    assert locators(rule, SEMANTIC_DOC) == expected
    assert locators(reparsed, SEMANTIC_DOC) == expected
    # All four leaves hold the value 1, so value-only assertions would hide
    # any swap between them.
    assert values(rule, SEMANTIC_DOC) == [1, 1, 1, 1]
    assert write_targets(reparsed, SEMANTIC_DOC) == write_targets(rule, SEMANTIC_DOC)


def test_repeated_index_keeps_duplicate_matches_but_distinct_write_lines():
    rule = CORE_PARSER.parse("a.items[0,1,0].k")
    reparsed = CORE_PARSER.parse(str(rule))

    # Index 0 is reported twice and in its original position each time.
    assert locators(rule, SEMANTIC_DOC) == [
        "a.items.[0].k",
        "a.items.[1].k",
        "a.items.[0].k",
    ]
    assert locators(reparsed, SEMANTIC_DOC) == locators(rule, SEMANTIC_DOC)

    # Writing collapses the repetition onto only the two distinct cells.
    assert write_targets(rule, SEMANTIC_DOC) == {
        ("a", "items", 0, "k"),
        ("a", "items", 1, "k"),
    }
    assert write_targets(reparsed, SEMANTIC_DOC) == write_targets(rule, SEMANTIC_DOC)


def test_repeated_field_matches_twice_but_writes_one_line():
    rule = CORE_PARSER.parse("['dup','dup']")
    reparsed = CORE_PARSER.parse(str(rule))
    doc = {"dup": 1}

    assert locators(rule, doc) == ["dup", "dup"]
    assert locators(reparsed, doc) == ["dup", "dup"]
    assert write_targets(rule, doc) == {("dup",)}
    assert write_targets(reparsed, doc) == {("dup",)}


def test_root_and_current_node_read_the_same_but_never_rewrite_the_document():
    doc = {"a": 1}
    root = CORE_PARSER.parse("$")
    current = EXT_PARSER.parse("@")

    assert values(root, doc) == values(current, doc) == [doc]
    # Root/current replace the *result* of ``update`` rather than mutating the
    # document, so neither has a write line inside it.
    assert write_targets(root, doc) == set()
    assert write_targets(current, doc) == set()


def test_equal_values_in_filter_hits_keep_distinct_positions_and_writes():
    rule = EXT_PARSER.parse("a.items[?@.k == 1].v")
    reparsed = EXT_PARSER.parse(str(rule))

    assert values(rule, SEMANTIC_DOC) == ["same", "same"]
    assert locators(rule, SEMANTIC_DOC) == [
        "a.items.[0].v",
        "a.items.[1].v",
    ]
    assert locators(reparsed, SEMANTIC_DOC) == locators(rule, SEMANTIC_DOC)
    assert write_targets(reparsed, SEMANTIC_DOC) == write_targets(rule, SEMANTIC_DOC)


# ---------------------------------------------------------------------------
# Boundaries: rules outside the guaranteed, textually-locatable subset
# ---------------------------------------------------------------------------

def test_bare_union_locations_roundtrip_but_update_is_unsupported():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        rule = CORE_PARSER.parse("a | b")
        reparsed = CORE_PARSER.parse(str(rule))

    doc = {"a": 1, "b": 2}
    # Reading still has stable locations ...
    assert locators(reparsed, doc) == locators(rule, doc) == ["a", "b"]

    # ... but the union itself cannot be used as a write target.
    with pytest.raises(NotImplementedError):
        rule.update(copy.deepcopy(doc), SENTINEL)
    with pytest.raises(NotImplementedError):
        reparsed.update(copy.deepcopy(doc), SENTINEL)


def test_intersection_is_outside_the_semantic_subset():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        rule = CORE_PARSER.parse("a & b")

    with pytest.raises(NotImplementedError):
        rule.find({"a": 1, "b": 2})


def test_parent_without_context_is_outside_the_semantic_subset():
    rule = CORE_PARSER.parse("`parent`")
    with pytest.raises(AssertionError):
        rule.find({"a": 1})


def test_bare_sorted_ast_cannot_be_textualized():
    # ``objects.`sorted``` parses, but the resulting ``SortedThis`` carries no
    # sort expressions and its ``__str__`` cannot render them. This is why
    # sorting is excluded from the textually-guaranteed subset.
    rule = EXT_PARSER.parse("objects.`sorted`")
    with pytest.raises(TypeError):
        str(rule)


def test_sorted_index_locator_cannot_re_anchor_even_though_the_write_is_correct():
    doc = {"a": [{"x": 2, "v": "first"}, {"x": 1, "v": "second"}, {"x": 3}]}
    rule = EXT_PARSER.parse("a[/x][0].v")

    match = rule.find(doc)[0]
    read_index = 1
    # The read value comes from the second array element after sorting ...
    assert match.value == "second"
    assert doc["a"][read_index]["v"] == "second"
    # ... but the recorded locator is relative to the sorted copy, not the
    # document, so it cannot re-anchor at the read node from the root.
    locator = str(match.full_path)
    assert [m.value for m in EXT_PARSER.parse(locator).find(doc)] == []

    written = copy.deepcopy(doc)
    rule.update(written, SENTINEL)
    # Writing through the rule still reaches the actual read node by identity,
    # which is precisely why the un-re-anchorable locator cannot be trusted as
    # a saved write target.
    assert written["a"][read_index]["v"] == SENTINEL
    assert written["a"][0]["v"] == "first"


def test_len_and_keys_matches_do_not_identify_document_positions():
    doc = {"a": {"k1": 1, "k2": 2}}
    keys = EXT_PARSER.parse("a.`keys`")
    length = EXT_PARSER.parse("a.`len`")

    # Both locators describe the computation, not the members they returned.
    assert locators(keys, doc) == ["`keys`", "`keys`"]
    assert locators(length, doc) == ["`len`"]
    with pytest.raises(NotImplementedError):
        length.update(copy.deepcopy(doc), SENTINEL)
    with pytest.raises(NotImplementedError):
        keys.update(copy.deepcopy(doc), SENTINEL)


def test_arithmetic_rules_have_no_write_semantics():
    rule = EXT_PARSER.parse("a.items[0].k + 1")
    with pytest.raises(NotImplementedError):
        rule.update(copy.deepcopy(SEMANTIC_DOC), SENTINEL)


def test_string_operators_roundtrip_but_their_writes_change_nothing():
    doc = {"a": "ooo"}
    for query in ("a.`str()`", "a.`sub(/o/, 0)`", "a.`split('o', *, -1)`"):
        rule = EXT_PARSER.parse(query)
        reparsed = EXT_PARSER.parse(str(rule))

        assert reparsed == rule
        assert values(reparsed, doc) == values(rule, doc)
        assert write_targets(rule, doc) == set()


def test_where_serialization_normalizes_ast_but_preserves_semantics():
    # A hand-built ``(a.b) where c`` textualizes without parentheses, which
    # re-parses as ``a.(b where c)``: different AST, identical semantics.
    hand_built = Where(Child(field("a"), field("b")), field("c"))
    reparsed = CORE_PARSER.parse(str(hand_built))

    assert reparsed != hand_built
    # The parenthesized text is the faithful rendering of the original AST.
    assert CORE_PARSER.parse("(a.b) where c") == hand_built

    for doc in DOCUMENTS:
        assert locators(reparsed, doc) == locators(hand_built, doc)
        assert write_targets(reparsed, doc) == write_targets(hand_built, doc)


# ---------------------------------------------------------------------------
# Property tests and Hypothesis-driven shrinking
# ---------------------------------------------------------------------------

_FIELD_NAMES = st.sampled_from(KEYS)
_INDEX = st.integers(min_value=-2, max_value=3)


def core_ast_strategy() -> st.SearchStrategy:
    base = st.one_of(
        st.just(Root()),
        st.just(This()),
        st.builds(field, _FIELD_NAMES),
        st.builds(Fields._compliant_constructor, _FIELD_NAMES, _FIELD_NAMES),
        st.builds(Index, _INDEX),
        st.builds(Index, _INDEX, _INDEX, _INDEX),
        st.builds(Wildcard),
        st.builds(
            Slice._compliant_constructor,
            st.one_of(st.none(), st.integers(0, 2)),
            st.one_of(st.none(), st.integers(1, 4)),
            st.one_of(st.none(), st.integers(1, 3)),
        ),
        st.builds(Descendants, st.just(Root()), st.builds(field, st.just("x"))),
        st.builds(
            Where,
            st.builds(field, st.just("a")),
            st.builds(field, st.just("b")),
        ),
        st.builds(
            WhereNot, st.builds(field, st.just("a")), st.builds(field, st.just("x"))
        ),
    )
    extenders = st.one_of(
        st.builds(field, _FIELD_NAMES),
        st.builds(Index, _INDEX),
        st.just(Wildcard()),
        st.builds(Slice._compliant_constructor, st.none(), st.integers(1, 4)),
    )
    return st.recursive(
        base, lambda node: st.builds(Child, node, extenders), max_leaves=8
    )


@given(rule=core_ast_strategy())
@settings(max_examples=60, deadline=None)
def test_core_ast_property_roundtrips_on_fixed_documents(rule):
    rendered = str(rule)
    reparsed = CORE_PARSER.parse(rendered)

    # The guaranteed subset textualizes back to an equal AST ...
    assert reparsed == rule
    for doc in DOCUMENTS:
        assert_semantic_roundtrip(CORE_PARSER, rule, doc)


_EXT_FILTER_BASES = (
    "a[?@.k == 1]",
    "a[?@.k]",
    "a[?@.k == $.target]",
    "a[?@.k == 1 && @.m]",
    "a[?@.k == 1 || @.m == 2]",
    "a[?!@.m]",
    "a[?@.v =~ 'sa.*']",
)


def ext_ast_strategy() -> st.SearchStrategy:
    base = st.sampled_from([EXT_PARSER.parse(q) for q in _EXT_FILTER_BASES])
    extenders = st.one_of(
        st.builds(field, st.sampled_from(("v", "k", "a.b"))),
        st.builds(Index, _INDEX),
        st.just(Wildcard()),
        st.builds(Slice._compliant_constructor, st.none(), st.integers(1, 4)),
    )
    return st.recursive(
        base, lambda node: st.builds(Child, node, extenders), max_leaves=6
    )


@given(rule=ext_ast_strategy())
@settings(max_examples=50, deadline=None)
def test_ext_ast_property_roundtrips_on_fixed_documents(rule):
    rendered = str(rule)
    reparsed = EXT_PARSER.parse(rendered)

    assert reparsed == rule
    for doc in DOCUMENTS:
        assert_semantic_roundtrip(EXT_PARSER, rule, doc)


def ambiguous_dotted_documents() -> st.SearchStrategy:
    """Always contain equal values at ``a.b`` and ``a`` -> ``b``."""

    shared = st.sampled_from((1, "same", None))
    return shared.flatmap(
        lambda value: st.fixed_dictionaries(
            {
                "a.b": st.just(value),
                "a": st.fixed_dictionaries({"b": st.just(value)}),
            }
        )
    )


def multi_x_documents() -> st.SearchStrategy:
    """Always hold the same value at two distinct ``x`` positions."""

    leaf = st.one_of(st.integers(0, 2), st.sampled_from(("same", "other", None)))
    return leaf.flatmap(
        lambda value: st.fixed_dictionaries(
            {
                "x": st.just(value),
                "other": st.fixed_dictionaries({"x": st.just(value)}),
            }
        )
    )


def sortable_divergent_documents() -> st.SearchStrategy:
    """A list whose minimum element is guaranteed outside array slot zero."""

    value = st.sampled_from(("P", "Q", "R"))
    head = st.fixed_dictionaries({"x": st.just(3), "v": value})
    tail = st.lists(
        st.fixed_dictionaries({"x": st.integers(0, 2), "v": value}),
        min_size=1,
        max_size=3,
    )
    return st.builds(lambda first, rest: {"a": [first, *rest]}, head, tail)


@given(doc=ambiguous_dotted_documents())
@settings(max_examples=60, deadline=None)
def test_dotted_vs_nested_shrinks_to_a_distinct_write_pair(doc):
    dotted = CORE_PARSER.parse("$['a.b']")
    nested = CORE_PARSER.parse("$.a.b")

    # Both rules read the same single value by construction; Hypothesis shrinks
    # the surrounding document to ``{'a.b': 1, 'a': {'b': 1}}``.
    assert len(dotted.find(doc)) == len(nested.find(doc)) == 1
    assert values(dotted, doc) == values(nested, doc)

    dotted_targets = write_targets(dotted, doc)
    nested_targets = write_targets(nested, doc)

    # Equal read values never imply equal write lines.
    assert dotted_targets == {("a.b",)}
    assert nested_targets == {("a", "b")}
    assert dotted_targets != nested_targets


@given(doc=multi_x_documents())
@settings(max_examples=60, deadline=None)
def test_equal_descendant_values_shrink_to_ordered_distinct_locations(doc):
    rule = CORE_PARSER.parse("$..x")
    reparsed = CORE_PARSER.parse(str(rule))

    # Two equal hits at distinct positions: a value-based comparison would be
    # blind to their order, so locators and write lines carry the distinction.
    assert values(rule, doc) == [doc["x"], doc["x"]]
    assert locators(rule, doc) == ["x", "other.x"]
    assert locators(reparsed, doc) == locators(rule, doc)
    assert write_targets(reparsed, doc) == write_targets(rule, doc)
    assert write_targets(rule, doc) == {("x",), ("other", "x")}


@given(doc=sortable_divergent_documents())
@settings(max_examples=60, deadline=None)
def test_sorted_index_shrinks_to_an_unreanchorable_locator(doc):
    rule = EXT_PARSER.parse("a[/x][0].v")
    matches = rule.find(doc)
    assert matches

    # The minimum is structurally guaranteed to live outside array slot zero;
    # Hypothesis shrinks the document to two elements.
    minimum = min(item["x"] for item in doc["a"])
    read_index = next(
        index
        for index, item in enumerate(doc["a"])
        if item["x"] == minimum
    )
    assert read_index != 0

    # The locator is relative to the sorted copy and cannot re-anchor at the
    # read node at the document root (the root is an object, so an array-index
    # locator matches nothing there).
    locator = str(matches[0].full_path)
    assert EXT_PARSER.parse(locator).find(doc) == []

    # The write still reaches the read node through its object identity, so the
    # un-re-anchorable locator is the only thing that misidentifies the line.
    written = copy.deepcopy(doc)
    rule.update(written, SENTINEL)
    assert written["a"][read_index]["v"] == SENTINEL
    assert written["a"][0]["v"] == doc["a"][0]["v"]
