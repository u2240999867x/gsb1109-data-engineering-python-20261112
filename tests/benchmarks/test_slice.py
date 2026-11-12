import pytest

import jsonpath_ng.parser

benchmark_group = pytest.mark.benchmark(group="slice")

SIZE = 1000


def filter_each_round(benchmark, query, build_data, predicate):
    """Benchmark `.filter()`, giving each round fresh data to remove from.

    `.filter()` mutates its data, so the data can't be reused between rounds.
    Building it in `setup` keeps that work out of the measurement.
    """

    path = jsonpath_ng.parser.JsonPathParser().parse(query)
    return benchmark.pedantic(
        lambda data: path.filter(predicate, data),
        setup=lambda: ((build_data(),), {}),
    )


@benchmark_group
def test_slice_filter_remove_all(benchmark):
    # Every element is removed.
    result = filter_each_round(
        benchmark, "$[*]", lambda: list(range(SIZE)), lambda value: True
    )

    assert result == []


@benchmark_group
def test_slice_filter_remove_half(benchmark):
    # Every other element is removed.
    result = filter_each_round(
        benchmark, "$[*]", lambda: list(range(SIZE)), lambda value: value % 2 == 0
    )

    assert result == list(range(1, SIZE, 2))


@benchmark_group
def test_slice_filter_remove_none(benchmark):
    # No element is removed. This is the baseline for the removal benchmarks.
    result = filter_each_round(
        benchmark, "$[*]", lambda: list(range(SIZE)), lambda value: False
    )

    assert result == list(range(SIZE))


@benchmark_group
def test_slice_filter_wildcard_remove_all_object_members(benchmark):
    result = filter_each_round(
        benchmark,
        "$[*]",
        lambda: {str(i): i for i in range(SIZE)},
        lambda value: True,
    )

    assert result == {}
