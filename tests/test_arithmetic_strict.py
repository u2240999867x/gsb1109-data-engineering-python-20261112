"""
Tests for strict, parent-record-aligned arithmetic in the extended parser.

Strict arithmetic never pairs values from two JSONPath operands purely because
they occupy the same position in two result lists. Instead it aligns operands by
their common parent record, isolates rows that are missing a column or contain
ambiguous multiple values, and only broadcasts a scalar inside the parent record
where the scalar was found.
"""

import pytest

from jsonpath_ng.ext import parser
from jsonpath_ng.ext.arithmetic import Operation
from jsonpath_ng.ext.arithmetic import OperationResult
from jsonpath_ng.ext.arithmetic import Quarantine
from jsonpath_ng.ext.arithmetic import REASON_AMBIGUOUS
from jsonpath_ng.ext.arithmetic import REASON_MISSING
from jsonpath_ng.ext.arithmetic import REASON_TYPE_ERROR


INVOICE_DATA = {
    "invoices": [
        {"id": "A", "qty": 2, "price": 5},
        {"id": "B", "qty": 3, "price": 7},
        # A complete column is absent; this source row must be isolated.
        {"id": "C", "qty": 4},
    ]
}


def test_strict_aligns_operands_by_common_parent_record():
    result = parser.parse(
        "$.invoices[*].qty * $.invoices[*].price",
        strict_arithmetic=True,
    ).find_strict(INVOICE_DATA)

    assert result.values == [10, 21]
    assert len(result.quarantined) == 1
    quarantine = result.quarantined[0]
    assert quarantine.reason == REASON_MISSING
    assert quarantine.path == "invoices.[2]"
    assert quarantine.left_path == "invoices.[2].qty"
    assert quarantine.right_path is None
    assert "price" in quarantine.detail


def test_missing_column_is_located_to_the_source_row_not_zipped():
    # The legacy operation silently gives up because the two lists differ in
    # length, even though two rows are perfectly evaluable.
    legacy = parser.parse("$.invoices[*].qty * $.invoices[*].price").find(
        INVOICE_DATA
    )
    assert legacy == []

    strict = parser.parse(
        "$.invoices[*].qty * $.invoices[*].price",
        strict_arithmetic=True,
    ).find_strict(INVOICE_DATA)

    # The two complete rows are stored and only the incomplete row is isolated.
    assert strict.values == [10, 21]
    assert [item.path for item in strict.quarantined] == ["invoices.[2]"]


def test_find_in_strict_mode_returns_only_storable_rows():
    query = parser.parse(
        "$.invoices[*].qty * $.invoices[*].price",
        strict_arithmetic=True,
    )

    assert [datum.value for datum in query.find(INVOICE_DATA)] == [10, 21]


def test_multiple_values_in_one_record_are_ambiguous_by_default():
    data = {
        "rows": [
            {"id": "A", "qty": 2, "prices": [5, 6]},
            {"id": "B", "qty": 3, "prices": [7]},
        ]
    }
    result = parser.parse(
        "$.rows[*].qty * $.rows[*].prices[*]",
        strict_arithmetic=True,
    ).find_strict(data)

    assert result.values == [21]
    assert len(result.quarantined) == 1
    quarantine = result.quarantined[0]
    assert quarantine.reason == REASON_AMBIGUOUS
    assert quarantine.path == "rows.[0]"
    assert quarantine.left_path == "rows.[0].qty"
    assert quarantine.right_path == "rows.[0].prices.[0]"


def test_scalar_broadcast_is_scoped_to_the_parent_record():
    data = {
        "rows": [
            {"id": "A", "qty": 2, "prices": [5, 6]},
            {"id": "B", "qty": 3, "prices": [7]},
        ]
    }
    result = parser.parse(
        "$.rows[*].qty * $.rows[*].prices[*]",
        strict_arithmetic=True,
        allow_scalar_broadcast=True,
    ).find_strict(data)

    # Row A broadcasts its scalar qty over its own two prices; row B is a
    # separate record and is never used to fill row A's missing pair.
    assert result.values == [10, 12, 21]
    assert result.quarantined == []
    assert [str(datum.full_path) for datum in result.results] == [
        "rows.[0].[0]",
        "rows.[0].[1]",
        "rows.[1].product",
    ]


def test_broadcast_never_pairs_two_multi_value_records():
    data = {"rows": [{"a": [1, 2], "b": [3, 4]}]}
    result = parser.parse(
        "$.rows[*].a[*] * $.rows[*].b[*]",
        strict_arithmetic=True,
        allow_scalar_broadcast=True,
    ).find_strict(data)

    assert result.values == []
    assert [item.reason for item in result.quarantined] == [REASON_AMBIGUOUS]


def test_duplicate_values_are_kept_at_distinct_hit_positions():
    data = {"rows": [{"qty": 2, "prices": [5, 5]}]}
    result = parser.parse(
        "$.rows[*].qty * $.rows[*].prices[*]",
        strict_arithmetic=True,
        allow_scalar_broadcast=True,
    ).find_strict(data)

    assert result.values == [10, 10]
    assert [str(datum.full_path) for datum in result.results] == [
        "rows.[0].[0]",
        "rows.[0].[1]",
    ]


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("$.a + $.b", 9),
        ("$.a - $.b", 3),
        ("$.a * $.b", 18),
    ],
)
def test_complete_record_matches_the_original_operation(expression, expected):
    data = {"a": 6, "b": 3}

    legacy_values = [datum.value for datum in parser.parse(expression).find(data)]
    strict_result = parser.parse(
        expression, strict_arithmetic=True
    ).find_strict(data)

    assert legacy_values == [expected]
    assert strict_result.values == legacy_values
    assert strict_result.quarantined == []


def test_division_operation_aligns_records():
    # The `/` character is reserved by the extended syntax for sorting, so a
    # division operation is built directly from two parsed operands.
    data = {"rows": [{"a": 6, "b": 3}, {"a": 10, "b": 4}]}
    operation = Operation(
        parser.parse("$.rows[*].a"),
        "/",
        parser.parse("$.rows[*].b"),
        strict=True,
    )

    assert operation.find_strict(data).values == [2, 2.5]


def test_type_error_isolates_the_offending_row():
    data = {"rows": [{"a": 2, "b": None}, {"a": 3, "b": 4}]}
    result = parser.parse(
        "$.rows[*].a * $.rows[*].b",
        strict_arithmetic=True,
    ).find_strict(data)

    assert result.values == [12]
    quarantine = result.quarantined[0]
    assert quarantine.reason == REASON_TYPE_ERROR
    assert quarantine.path == "rows.[0].a"
    assert quarantine.left_path == "rows.[0].a"
    assert quarantine.right_path == "rows.[0].b"
    assert "NoneType" in quarantine.detail


def test_literal_operand_evaluates_each_match_and_isolates_type_errors():
    data = {"rows": [{"a": 2}, {"a": None}, {"a": 3}]}
    result = parser.parse(
        "$.rows[*].a + 1",
        strict_arithmetic=True,
    ).find_strict(data)

    assert result.values == [3, 4]
    assert [item.reason for item in result.quarantined] == [REASON_TYPE_ERROR]
    assert result.quarantined[0].path == "rows.[1].a"


def test_two_literals_evaluate_without_quarantine():
    result = parser.parse("2 * 3", strict_arithmetic=True).find_strict({})

    assert result.values == [6]
    assert result.quarantined == []


def test_unrelated_arrays_cannot_be_aligned_by_a_record():
    data = {
        "objects": [{"cow": 1}, {"cow": 2}, {"cow": 3}],
        "objects2": [{"cow": 5}],
    }
    result = parser.parse(
        "$.objects[*].cow * $.objects2[*].cow",
        strict_arithmetic=True,
    ).find_strict(data)

    assert result.values == []
    assert len(result.quarantined) == 1
    quarantine = result.quarantined[0]
    assert quarantine.reason == REASON_AMBIGUOUS
    assert quarantine.path == "$"


def test_no_match_at_all_is_reported_as_missing_without_a_row():
    result = parser.parse(
        "$.missing[*].a * $.present[*].b",
        strict_arithmetic=True,
    ).find_strict({"present": [{"b": 1}]})

    assert result.results == []
    assert len(result.quarantined) == 1
    quarantine = result.quarantined[0]
    assert quarantine.reason == REASON_MISSING
    assert quarantine.path is None


def test_nested_operations_stay_aligned_per_record():
    data = {
        "rows": [
            {"a": 2, "b": 3, "c": 4},
            {"a": 5, "b": 6, "c": 7},
            {"a": 8, "c": 9},
        ]
    }
    result = parser.parse(
        "$.rows[*].a * $.rows[*].b + $.rows[*].c",
        strict_arithmetic=True,
    ).find_strict(data)

    # The inner product is computed per record before the literal-free addition.
    assert result.values == [14, 65]
    assert [item.reason for item in result.quarantined] == [REASON_MISSING]
    assert result.quarantined[0].path == "rows.[2]"


def test_quarantine_entries_are_json_serializable():
    result = parser.parse(
        "$.invoices[*].qty * $.invoices[*].price",
        strict_arithmetic=True,
    ).find_strict(INVOICE_DATA)

    serialized = [entry.as_dict() for entry in result.quarantined]
    assert serialized == [
        {
            "reason": REASON_MISSING,
            "detail": result.quarantined[0].detail,
            "path": "invoices.[2]",
            "left_path": "invoices.[2].qty",
            "right_path": None,
        }
    ]
    # Every value is a plain JSON-compatible scalar.
    for entry in serialized:
        assert all(value is None or isinstance(value, str) for value in entry.values())


def test_operation_result_exposes_storable_and_isolated_rows():
    result = parser.parse(
        "$.invoices[*].qty * $.invoices[*].price",
        strict_arithmetic=True,
    ).find_strict(INVOICE_DATA)

    assert isinstance(result, OperationResult)
    assert all(isinstance(entry, Quarantine) for entry in result.quarantined)
    assert result.values == [datum.value for datum in result.results]


def test_direct_operation_constructor_enables_strict_mode():
    left = parser.parse("$.invoices[*].qty")
    right = parser.parse("$.invoices[*].price")
    operation = Operation(left, "*", right, strict=True)

    result = operation.find_strict(INVOICE_DATA)
    assert result.values == [10, 21]
    assert [entry.reason for entry in result.quarantined] == [REASON_MISSING]
    # `find()` keeps the standard interface and drops the quarantine.
    assert [datum.value for datum in operation.find(INVOICE_DATA)] == [10, 21]


def test_default_operation_keeps_legacy_zip_behavior():
    data = {"objects": [{"cow": 1}, {"cow": 2}, {"cow": 3}]}
    expression = "$.objects[*].cow * $.objects[*].cow"

    assert [datum.value for datum in parser.parse(expression).find(data)] == [
        1,
        4,
        9,
    ]
    # The strict flag is opt-in.
    assert parser.parse(expression).find_strict  # method is always available
    strict_query = parser.parse(expression, strict_arithmetic=True)
    assert strict_query.find_strict(data).values == [1, 4, 9]
