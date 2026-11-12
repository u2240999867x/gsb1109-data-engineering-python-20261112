"""
Verify the shape of the trees that `&&` and `||` expressions are parsed into.

The parser builds the `Or` node for `a || b || c || ...` by extending the node that
it created for the previous term, instead of creating a new node for every term.
That is only correct if the node being extended is referenced by nothing else,
so these tests check that parentheses, `&&`, and repeated parses
never cause a node to be changed after it has been used somewhere else.

The property-based tests compare the parser to an independent evaluator,
so that a tree which is corrupted but still plausible is caught.
"""

import copy
import itertools

import pytest
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from jsonpath_ng import Child
from jsonpath_ng.ext import parse
from jsonpath_ng.ext.filter import And
from jsonpath_ng.ext.filter import Expression
from jsonpath_ng.ext.filter import Filter
from jsonpath_ng.ext.filter import Or


def expression(name):
    """Create the `Expression` that `@.<name> == 1` parses to."""

    return parse(f"$[?@.{name} == 1]").right.expressions[0]


def top(query):
    """Parse a filter and return the one expression in its expression list."""

    (result,) = parse(f"$[?{query}]").right.expressions
    return result


def a_to_e():
    return [expression(name) for name in "abcde"]


def test_parenthesized_or_on_the_left_is_extended():
    a, b, c, *_ = a_to_e()

    assert top("(@.a == 1 || @.b == 1) || @.c == 1") == Or([a, b, c])


def test_nested_parentheses_on_the_left_are_extended():
    a, b, c, *_ = a_to_e()

    assert top("((@.a == 1 || @.b == 1)) || @.c == 1") == Or([a, b, c])


def test_parenthesized_or_on_the_right_is_spliced_in():
    a, b, c, *_ = a_to_e()

    assert top("@.a == 1 || (@.b == 1 || @.c == 1)") == Or([a, b, c])


def test_parenthesized_ors_on_both_sides_are_flattened():
    a, b, c, d, _ = a_to_e()

    query = top("(@.a == 1 || @.b == 1) || (@.c == 1 || @.d == 1)")

    assert query == Or([a, b, c, d])


def test_or_after_and_in_the_left_group_is_not_extended():
    """`(a || b) && c || d || e`: the inner `Or` is an operand of an `And`."""

    a, b, c, d, e = a_to_e()

    outer = top("(@.a == 1 || @.b == 1) && @.c == 1 || @.d == 1 || @.e == 1")

    assert outer == Or([And([Or([a, b]), c]), d, e])
    assert outer.operands[0].operands[0].operands == [a, b]


def test_or_before_and_in_the_left_group_is_not_extended():
    """`x && (a || b) || c || d`: the left group has two elements."""

    a, b, c, d, e = a_to_e()

    outer = top("@.e == 1 && (@.a == 1 || @.b == 1) || @.c == 1 || @.d == 1")

    assert outer == Or([And([e, Or([a, b])]), c, d])
    assert outer.operands[0].operands[1].operands == [a, b]


def test_or_in_the_right_hand_and_is_not_extended():
    a, b, c, d, e = a_to_e()

    outer = top("@.a == 1 || @.b == 1 && (@.c == 1 || @.d == 1) || @.e == 1")

    assert outer == Or([a, And([b, Or([c, d])]), e])
    assert outer.operands[1].operands[1].operands == [c, d]


def test_group_in_a_longer_conjunction_is_not_extended():
    """`a && (b || c) && d || e`: the group is one element of a longer list."""

    a, b, c, d, e = a_to_e()

    outer = top("@.a == 1 && (@.b == 1 || @.c == 1) && @.d == 1 || @.e == 1")

    assert outer == Or([And([a, Or([b, c]), d]), e])
    assert outer.operands[0].operands[1].operands == [b, c]


def test_or_of_conjunctions_extends_only_the_or():
    a, b, c, d, e = a_to_e()

    outer = top("@.a == 1 && @.b == 1 || @.c == 1 && @.d == 1 || @.e == 1")

    assert outer == Or([And([a, b]), And([c, d]), e])


def test_filter_with_a_parenthesized_or_and_a_conjunct_is_unchanged():
    """`(a || b) && c` has no `||` at its top, so nothing may be extended."""

    a, b, c, *_ = a_to_e()

    first, second = parse("$[?(@.a == 1 || @.b == 1) && @.c == 1]").right.expressions

    assert first == Or([a, b])
    assert second == c


def test_each_parse_creates_new_nodes():
    query = "$[?@.a == 1 || @.b == 1 || @.c == 1]"

    first = parse(query).right.expressions[0]
    second = parse(query).right.expressions[0]
    assert first == second
    assert first is not second
    assert first.operands is not second.operands

    first.operands.append(expression("d"))
    assert len(second.operands) == 3
    assert parse(query).right.expressions[0] == second


def test_separate_parses_share_no_nodes():
    query = "$[?@.a == 1 && @.b == 1 || @.a == 1 && @.b == 1 || @.c == 1]"

    # Every tree must stay alive while the ids are compared.
    # Python reuses the memory of an object that has been freed,
    # so ids taken from a tree that was already discarded can collide
    # with the ids of the nodes of the next tree.
    trees = [parse(query) for _ in range(20)]
    nodes = [node for tree in trees for node in walk(tree)]

    assert len({id(node) for node in nodes}) == len(nodes)


def test_the_same_term_written_twice_is_two_nodes():
    tree = parse("$[?@.a == 1 || @.a == 1 || (@.a == 1 || @.a == 1)]")

    (outer,) = tree.right.expressions
    assert len(outer.operands) == 4
    assert len({id(operand) for operand in outer.operands}) == 4


def test_the_parser_instance_does_not_keep_nodes_between_parses():
    from jsonpath_ng.ext.parser import ExtendedJsonPathParser

    parser = ExtendedJsonPathParser()
    first = parser.parse("$[?(@.a == 1 || @.b == 1) || @.c == 1]")
    snapshot = copy.deepcopy(first)

    parser.parse("$[?(@.a == 1 || @.b == 1) || @.d == 1 || @.e == 1]")
    parser.parse("$[?@.x == 1 || @.y == 1]")

    assert first == snapshot


@pytest.mark.parametrize("cls", [And, Or])
def test_constructor_flattens_the_same_operator(cls):
    a, b, c, d, _ = a_to_e()

    assert cls([cls([a, b]), c]) == cls([a, b, c])
    assert cls([a, cls([b, c])]) == cls([a, b, c])
    assert cls([cls([a, b]), cls([c, d])]) == cls([a, b, c, d])


def test_constructor_does_not_flatten_the_other_operator():
    a, b, c, *_ = a_to_e()

    assert Or([And([a, b]), c]).operands == [And([a, b]), c]
    assert And([Or([a, b]), c]).operands == [Or([a, b]), c]


@pytest.mark.parametrize("cls", [And, Or])
def test_constructor_does_not_keep_the_callers_list(cls):
    a, b, c, *_ = a_to_e()
    operands = [a, b]

    node = cls(operands)
    operands.append(c)
    node.operands.append(c)

    assert operands == [a, b, c]
    assert node.operands == [a, b, c]
    assert node.operands is not operands

    inner = cls([a, b])
    outer = cls([inner, c])
    outer.operands.append(a)
    assert inner.operands == [a, b]


# ----------------------------------------------------------------------------
# Property-based tests
# ----------------------------------------------------------------------------

ATOMS = 4


def children(node):
    if isinstance(node, Child):
        return [node.left, node.right]
    if isinstance(node, Filter):
        return list(node.expressions)
    if isinstance(node, (And, Or)):
        return list(node.operands)
    if isinstance(node, Expression):
        return [node.target] + ([node.value] if hasattr(node.value, "find") else [])
    return []


def walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(children(node))


# A generated expression is `("atom", index, parenthesized)`,
# or `("and" | "or", [children], parenthesized)`.
expressions = st.recursive(
    st.tuples(st.just("atom"), st.integers(0, ATOMS - 1), st.booleans()),
    lambda inner: st.tuples(
        st.sampled_from(["and", "or"]),
        st.lists(inner, min_size=2, max_size=4),
        st.booleans(),
    ),
    max_leaves=14,
)

ITEMS = [
    {"id": number, **{f"p{i}": (number >> i) & 1 for i in range(ATOMS)}}
    for number in range(2**ATOMS)
]


def render(node):
    """Write a generated expression as a query, parenthesizing where required."""

    kind, content, parenthesized = node
    if kind == "atom":
        text = f"@.p{content} == 1"
    else:
        text = f" {'&&' if kind == 'and' else '||'} ".join(
            _render_child(kind, child) for child in content
        )
    return f"({text})" if parenthesized else text


def _render_child(parent, child):
    text = render(child)
    needs_parentheses = parent == "and" and child[0] == "or" and not child[2]
    return f"({text})" if needs_parentheses else text


def evaluate(node, item):
    kind, content, _ = node
    if kind == "atom":
        return item[f"p{content}"] == 1
    results = (evaluate(child, item) for child in content)
    return all(results) if kind == "and" else any(results)


@settings(max_examples=400, deadline=None)
@given(expressions)
def test_parsed_results_match_an_independent_evaluator(generated):
    query = f"$[?{render(generated)}]"
    expected = [item["id"] for item in ITEMS if evaluate(generated, item)]

    tree = parse(query)

    assert [match.value["id"] for match in tree.find(ITEMS)] == expected
    assert [match.value["id"] for match in parse(str(tree)).find(ITEMS)] == expected
    assert parse(str(tree)) == tree


def test_walk_visits_the_nodes_of_a_parsed_filter():
    tree = parse("$[?@.a == 1 && @.b == 1 || @.c == 1]")

    kinds = {type(node) for node in walk(tree)}

    assert {Filter, Or, And, Expression} <= kinds
    assert sum(isinstance(node, Expression) for node in walk(tree)) == 3


@settings(max_examples=400, deadline=None)
@given(expressions)
def test_no_node_is_referenced_twice(generated):
    tree = parse(f"$[?{render(generated)}]")

    nodes = list(walk(tree))
    assert len({id(node) for node in nodes}) == len(nodes)

    lists = [node.operands for node in nodes if isinstance(node, (And, Or))]
    assert len({id(operands) for operands in lists}) == len(lists)


@settings(max_examples=400, deadline=None)
@given(expressions)
def test_trees_are_flat_and_every_operator_has_operands(generated):
    tree = parse(f"$[?{render(generated)}]")

    for node in walk(tree):
        if isinstance(node, (And, Or)):
            assert len(node.operands) >= 2
            assert not any(type(operand) is type(node) for operand in node.operands)


@settings(max_examples=200, deadline=None)
@given(expressions)
def test_parsing_does_not_change_an_earlier_tree(generated):
    query = f"$[?{render(generated)}]"
    first = parse(query)
    snapshot = copy.deepcopy(first)

    for other in (
        "$[?@.p0 == 1 || @.p1 == 1 || @.p2 == 1]",
        f"$[?{render(generated)}]",
    ):
        parse(other)

    assert first == snapshot


def test_every_combination_of_two_operators_matches_the_evaluator():
    """An exhaustive version of the property tests, for three terms."""

    atoms = [("atom", i, False) for i in range(3)]
    for kinds in itertools.product(["and", "or"], repeat=2):
        for grouping in ("left", "right", "none"):
            first = (kinds[0], atoms[:2], grouping == "left")
            if grouping == "right":
                second = (kinds[1], [atoms[0], (kinds[0], atoms[1:], True)], False)
            else:
                second = (kinds[1], [first, atoms[2]], False)
            query = f"$[?{render(second)}]"
            expected = [item["id"] for item in ITEMS if evaluate(second, item)]

            actual = [m.value["id"] for m in parse(query).find(ITEMS)]

            assert actual == expected, query
