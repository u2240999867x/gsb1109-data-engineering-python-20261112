import pytest

import jsonpath_ng.parser

benchmark_group = pytest.mark.benchmark(group="descendants")


def build_haystack(depth: int):
    """Build a tree of dicts, lists, and scalars that is `depth` levels deep.

    Each level shares its child between two branches,
    so the number of nodes visited roughly doubles with each level.
    """

    if not depth:
        return {"needle": "present", "scalar": False}
    child = build_haystack(depth - 1)
    return {
        "needle": depth,
        "a": child,
        "b": [child, 1, None, {"needle-adjacent": "red-herring"}],
        "c": {},
    }


def build_chain(depth: int):
    """Build `depth` nested dicts, each with a needle."""

    haystack = {"needle": "present"}
    for _ in range(depth):
        haystack = {"a": haystack, "needle": "present"}
    return haystack


@benchmark_group
def test_descendants_find_wide(benchmark):
    depth = 10
    path = jsonpath_ng.parser.JsonPathParser().parse("$..needle")
    haystack = build_haystack(depth)

    result = benchmark(lambda: path.find(haystack))

    assert len(result) == 2 ** (depth + 1) - 1
    assert str(result[-1].full_path) == "b.[0]." * depth + "needle"


@benchmark_group
def test_descendants_find_deep(benchmark):
    path = jsonpath_ng.parser.JsonPathParser().parse("$..needle")
    haystack = build_chain(500)

    result = benchmark(lambda: path.find(haystack))

    assert len(result) == 501


@benchmark_group
def test_descendants_update_wide(benchmark):
    path = jsonpath_ng.parser.JsonPathParser().parse("$..needle")
    haystack = build_haystack(10)

    # Updating is idempotent, so the same haystack can be reused every round.
    benchmark(lambda: path.update(haystack, "updated"))

    assert {match.value for match in path.find(haystack)} == {"updated"}


@benchmark_group
def test_descendants_update_deep(benchmark):
    path = jsonpath_ng.parser.JsonPathParser().parse("$..needle")
    haystack = build_chain(500)

    # Updating is idempotent, so the same haystack can be reused every round.
    benchmark(lambda: path.update(haystack, "updated"))

    assert {match.value for match in path.find(haystack)} == {"updated"}


@benchmark_group
def test_descendants_filter_wide(benchmark):
    path = jsonpath_ng.parser.JsonPathParser().parse("$..needle")
    haystack = build_haystack(10)

    # The predicate never matches, so the haystack never changes
    # and can be reused every round.
    benchmark(lambda: path.filter(lambda value: False, haystack))

    assert len(path.find(haystack)) == 2**11 - 1


@benchmark_group
def test_descendants_filter_deep(benchmark):
    path = jsonpath_ng.parser.JsonPathParser().parse("$..needle")
    haystack = build_chain(500)

    # The predicate never matches, so the haystack never changes
    # and can be reused every round.
    benchmark(lambda: path.filter(lambda value: False, haystack))

    assert len(path.find(haystack)) == 501
