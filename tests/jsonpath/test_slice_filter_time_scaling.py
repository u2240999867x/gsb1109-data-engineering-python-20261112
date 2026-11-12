"""
Test that `Slice.filter()` takes time that is linear to the size of the data.

This test compares how the time grows when the data is quadrupled.
When every element is removed, a rescan after each removal would be quadratic.
"""

import timeit

import pytest

import jsonpath_ng
import jsonpath_ng.ext

SMALL = 500
LARGE = SMALL * 4

parsers = pytest.mark.parametrize(
    "parse",
    (jsonpath_ng.parse, jsonpath_ng.ext.parse),
    ids=lambda function: function.__module__,
)


def best_time(path, size):
    def remove_all():
        path.filter(lambda value: True, list(range(size)))

    return min(timeit.repeat(remove_all, number=3, repeat=7))


@pytest.mark.parametrize("query", ("$[*]", "$[:]", "$[::2]", "$[::-1]"))
@parsers
def test_slice_filter_time_is_linear_to_data_size(parse, query):
    path = parse(query)

    ratio = best_time(path, LARGE) / best_time(path, SMALL)

    # Linear growth gives ~4 and quadratic growth gives ~16;
    # 8 leaves margin for timing noise on both sides.
    assert ratio < 8, f"{LARGE / SMALL:.0f}x the data took {ratio:.1f}x as long"
