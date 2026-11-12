"""
Test that `Descendants.find()` takes time that is linear to the size of the data.

This test compares how the time grows when the depth is quadrupled.
As the data depth increases, `.find()` time is expected to increase linearly.
"""

import timeit

import pytest

import jsonpath_ng
import jsonpath_ng.ext

SMALL = 250
LARGE = SMALL * 4

parsers = pytest.mark.parametrize(
    "parse",
    (jsonpath_ng.parse, jsonpath_ng.ext.parse),
    ids=lambda function: function.__module__,
)


def chain_with_match_at_every_level(depth):
    data = {"needle": 1}
    for _ in range(depth):
        data = {"a": data, "needle": 1}
    return data


def best_time(path, data):
    return min(timeit.repeat(lambda: path.find(data), number=5, repeat=7))


@parsers
def test_descendants_find_time_is_linear_to_data_depth(parse):
    path = parse("$..needle")
    small = chain_with_match_at_every_level(SMALL)
    large = chain_with_match_at_every_level(LARGE)
    assert len(path.find(large)) == LARGE + 1

    ratio = best_time(path, large) / best_time(path, small)

    # Linear growth gives ~4 and quadratic growth gives ~6-16, depending on the
    # fixed per-match costs; 5.5 leaves margin for timing noise on both sides.
    assert ratio < 5.5, f"{LARGE / SMALL:.0f}x the depth took {ratio:.1f}x as long"
