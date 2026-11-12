#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import operator
import typing

from .. import DatumInContext
from .. import Fields
from .. import Index
from .. import JSONPath
from .. import Root

OPERATOR_MAP = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
}

# Field names used when a computed value is presented as a child of its
# source record; they only appear on synthetic result paths.
RESULT_FIELD_MAP = {
    "+": "sum",
    "-": "difference",
    "*": "product",
    "/": "quotient",
}

# Reasons recorded on isolated rows.
REASON_MISSING = "missing"
REASON_AMBIGUOUS = "ambiguous"
REASON_TYPE_ERROR = "type_error"


class Quarantine:
    """A single input row that strict arithmetic could not evaluate.

    Attributes:
        reason: One of the ``REASON_*`` constants, naming the failure mode.
        detail: A human-readable explanation of the failure.
        path: The full path of the source row, as a string, or ``None``
            when the failure is not tied to a particular source row
            (for example, every operand matched nothing).
        left_path: The full path of the offending left operand match,
            as a string, when applicable.
        right_path: The full path of the offending right operand match,
            as a string, when applicable.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        path: str | None = None,
        left_path: str | None = None,
        right_path: str | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.path = path
        self.left_path = left_path
        self.right_path = right_path

    def as_dict(self) -> dict[str, typing.Any]:
        """Return the quarantine entry as a plain, JSON-serializable dict."""

        return {
            "reason": self.reason,
            "detail": self.detail,
            "path": self.path,
            "left_path": self.left_path,
            "right_path": self.right_path,
        }

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(reason={self.reason!r}, "
            f"detail={self.detail!r}, path={self.path!r})"
        )

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Quarantine)
            and self.reason == other.reason
            and self.detail == other.detail
            and self.path == other.path
            and self.left_path == other.left_path
            and self.right_path == other.right_path
        )


class OperationResult:
    """The outcome of a strict :class:`Operation` evaluation.

    Attributes:
        results: The rows that can be stored, as ``DatumInContext`` objects.
            Each value equals what the original operation produces when run
            against a single, complete source record.
        quarantined: The input rows that were isolated, each with the source
            position and the reason for the isolation.
    """

    def __init__(
        self,
        results: list[DatumInContext],
        quarantined: list[Quarantine],
    ) -> None:
        self.results = results
        self.quarantined = quarantined

    @property
    def values(self) -> list[typing.Any]:
        """The computed values of the rows that can be stored."""

        return [result.value for result in self.results]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(results={self.results!r}, "
            f"quarantined={self.quarantined!r})"
        )

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, OperationResult)
            and self.results == other.results
            and self.quarantined == other.quarantined
        )


def _datum_path(datum: DatumInContext) -> tuple[JSONPath, ...]:
    """Return the root-to-datum chain of path segments for a match."""

    segments: list[JSONPath] = []
    current: DatumInContext | None = datum
    while current is not None:
        segments.append(current.path)
        current = current.context
    segments.reverse()
    return tuple(segments)


def _common_prefix(
    left: tuple[JSONPath, ...], right: tuple[JSONPath, ...]
) -> tuple[JSONPath, ...]:
    prefix: list[JSONPath] = []
    for left_segment, right_segment in zip(left, right):
        if left_segment != right_segment:
            break
        prefix.append(left_segment)
    return tuple(prefix)


def _group_by_parent(
    data: list[DatumInContext], depth: int
) -> dict[tuple[JSONPath, ...], list[DatumInContext]]:
    """Group matches by their first ``depth`` root-to-datum path segments."""

    groups: dict[tuple[JSONPath, ...], list[DatumInContext]] = {}
    for datum in data:
        key = _datum_path(datum)[:depth]
        groups.setdefault(key, []).append(datum)
    return groups


def _ancestor_at_depth(datum: DatumInContext, depth: int) -> DatumInContext:
    """Return the ancestor of ``datum`` whose depth is ``depth``.

    The root has depth 0; ``datum`` itself has depth ``len(_datum_path(datum)) - 1``.
    """

    steps = len(_datum_path(datum)) - depth - 1
    ancestor = datum
    for _ in range(steps):
        assert ancestor.context is not None
        ancestor = ancestor.context
    return ancestor


class Operation(JSONPath):
    def __init__(
        self,
        left: JSONPath | typing.Any,
        op: str,
        right: JSONPath | typing.Any,
        *,
        strict: bool = False,
        allow_scalar_broadcast: bool = False,
    ) -> None:
        self.left = left
        self.op_symbol = op
        self.op = OPERATOR_MAP[op]
        self.right = right
        # Strict mode aligns multi-value operands by their common parent
        # record instead of zipping matches in document order.
        self.strict = strict
        # In strict mode, permit a single scalar value to broadcast across
        # the values of the other operand, but only within one parent record.
        self.allow_scalar_broadcast = allow_scalar_broadcast

    # ------------------------------------------------------------------
    # Strict evaluation
    # ------------------------------------------------------------------

    def find_strict(self, datum: typing.Any) -> OperationResult:
        """Evaluate the operation and separate valid rows from isolated rows.

        Two JSONPath operands are aligned on their common parent record:
        matches that share the same source row are paired, and a column that
        has no value inside a source row is isolated against that row rather
        than silently paired with a value from another row.

        Multiple values for one operand inside a single source row are
        ambiguous and isolate the row by default. Scalar broadcasting only
        happens when explicitly enabled and never crosses parent boundaries.
        """

        if not (
            isinstance(self.left, JSONPath) and isinstance(self.right, JSONPath)
        ):
            return self._find_strict_with_literal(datum)

        left_matches = self.left.find(datum)
        right_matches = self.right.find(datum)
        if not left_matches or not right_matches:
            return OperationResult(
                [],
                [
                    Quarantine(
                        REASON_MISSING,
                        (
                            f"No value matched for "
                            f"{self._missing_side(left_matches, right_matches)} "
                            f"in {self}"
                        ),
                    )
                ],
            )

        left_paths = [_datum_path(item) for item in left_matches]
        right_paths = [_datum_path(item) for item in right_matches]
        parent_depth = max(
            len(_common_prefix(left_path, right_path))
            for left_path in left_paths
            for right_path in right_paths
        )

        left_groups = _group_by_parent(left_matches, parent_depth)
        right_groups = _group_by_parent(right_matches, parent_depth)
        parent_keys = list(left_groups.keys())
        for key in right_groups:
            if key not in left_groups:
                parent_keys.append(key)

        results: list[DatumInContext] = []
        quarantined: list[Quarantine] = []
        for parent_key in parent_keys:
            left_rows = left_groups.get(parent_key, [])
            right_rows = right_groups.get(parent_key, [])
            results.extend(
                self._combine_parent(
                    parent_key, parent_depth, left_rows, right_rows, quarantined
                )
            )
        return OperationResult(results, quarantined)

    def _find_strict_with_literal(self, datum: typing.Any) -> OperationResult:
        """Strict evaluation when one side of the operation is a literal."""

        results: list[DatumInContext] = []
        quarantined: list[Quarantine] = []
        if isinstance(self.left, JSONPath):
            for left_datum in self.left.find(datum):
                self._append_literal_operation(
                    results,
                    quarantined,
                    left_datum.value,
                    self.right,
                    left_datum,
                    is_left=True,
                )
        elif isinstance(self.right, JSONPath):
            for right_datum in self.right.find(datum):
                self._append_literal_operation(
                    results,
                    quarantined,
                    self.left,
                    right_datum.value,
                    right_datum,
                    is_left=False,
                )
        else:
            self._append_operation(
                results, quarantined, self.left, self.right, None, None
            )
        return OperationResult(results, quarantined)

    def _append_literal_operation(
        self,
        results: list[DatumInContext],
        quarantined: list[Quarantine],
        left_value: typing.Any,
        right_value: typing.Any,
        source_datum: DatumInContext,
        *,
        is_left: bool,
    ) -> None:
        try:
            value = self.op(left_value, right_value)
        except TypeError as exc:
            quarantined.append(
                Quarantine(
                    REASON_TYPE_ERROR,
                    str(exc) or f"Unsupported operands for {self.op_symbol}",
                    path=str(source_datum.full_path),
                    left_path=str(source_datum.full_path) if is_left else None,
                    right_path=None if is_left else str(source_datum.full_path),
                )
            )
            return
        # Preserve the source field's provenance so a later operation can
        # align the computed value by the same parent record.
        results.append(
            DatumInContext(
                value,
                path=source_datum.path,
                context=source_datum.context,
            )
        )

    def _combine_parent(
        self,
        parent_key: tuple[JSONPath, ...],
        parent_depth: int,
        left_rows: list[DatumInContext],
        right_rows: list[DatumInContext],
        quarantined: list[Quarantine],
    ) -> list[DatumInContext]:
        parent_path = self._render_key(parent_key)

        if not left_rows:
            quarantined.append(
                Quarantine(
                    REASON_MISSING,
                    f"Left operand {self.left} has no value in source row "
                    f"{parent_path}",
                    path=parent_path,
                    right_path=str(right_rows[0].full_path),
                )
            )
            return []
        if not right_rows:
            quarantined.append(
                Quarantine(
                    REASON_MISSING,
                    f"Right operand {self.right} has no value in source row "
                    f"{parent_path}",
                    path=parent_path,
                    left_path=str(left_rows[0].full_path),
                )
            )
            return []

        parent_datum = _ancestor_at_depth(left_rows[0], parent_depth - 1)
        if len(left_rows) > 1 or len(right_rows) > 1:
            return self._combine_multi_value(
                parent_path,
                parent_datum,
                left_rows,
                right_rows,
                quarantined,
            )

        results: list[DatumInContext] = []
        self._append_operation(
            results,
            quarantined,
            left_rows[0].value,
            right_rows[0].value,
            left_rows[0],
            right_rows[0],
            parent_datum=parent_datum,
        )
        return results

    def _combine_multi_value(
        self,
        parent_path: str,
        parent_datum: DatumInContext,
        left_rows: list[DatumInContext],
        right_rows: list[DatumInContext],
        quarantined: list[Quarantine],
    ) -> list[DatumInContext]:
        if self.allow_scalar_broadcast:
            if len(left_rows) == 1 and len(right_rows) > 1:
                results: list[DatumInContext] = []
                for position, right_datum in enumerate(right_rows):
                    self._append_operation(
                        results,
                        quarantined,
                        left_rows[0].value,
                        right_datum.value,
                        left_rows[0],
                        right_datum,
                        parent_datum=parent_datum,
                        position=position,
                    )
                return results
            if len(right_rows) == 1 and len(left_rows) > 1:
                results = []
                for position, left_datum in enumerate(left_rows):
                    self._append_operation(
                        results,
                        quarantined,
                        left_datum.value,
                        right_rows[0].value,
                        left_datum,
                        right_rows[0],
                        parent_datum=parent_datum,
                        position=position,
                    )
                return results

        quarantined.append(
            Quarantine(
                REASON_AMBIGUOUS,
                f"Operands match {len(left_rows)} left value(s) and "
                f"{len(right_rows)} right value(s) within source row "
                f"{parent_path}; pairing is ambiguous",
                path=parent_path,
                left_path=str(left_rows[0].full_path),
                right_path=str(right_rows[0].full_path),
            )
        )
        return []

    def _append_operation(
        self,
        results: list[DatumInContext],
        quarantined: list[Quarantine],
        left_value: typing.Any,
        right_value: typing.Any,
        left_datum: DatumInContext | None,
        right_datum: DatumInContext | None,
        *,
        parent_datum: DatumInContext | None = None,
        position: int | None = None,
    ) -> None:
        try:
            value = self.op(left_value, right_value)
        except TypeError as exc:
            quarantined.append(
                Quarantine(
                    REASON_TYPE_ERROR,
                    str(exc) or f"Unsupported operands for {self.op_symbol}",
                    path=self._error_path(left_datum, right_datum),
                    left_path=(
                        str(left_datum.full_path)
                        if left_datum is not None
                        else None
                    ),
                    right_path=(
                        str(right_datum.full_path)
                        if right_datum is not None
                        else None
                    ),
                )
            )
            return
        results.append(
            self._wrap_result(value, parent_datum, position)
        )

    def _wrap_result(
        self,
        value: typing.Any,
        parent_datum: DatumInContext | None,
        position: int | None,
    ) -> DatumInContext:
        """Wrap a computed value, preserving its source-row provenance."""

        if parent_datum is None:
            return DatumInContext.wrap(value)
        path: JSONPath
        if position is None:
            # The value belongs to the parent record itself.
            path = Fields._compliant_constructor(self._result_field())
        else:
            # Each repeated hit inside the record keeps its own position.
            path = Index(position)
        return DatumInContext(value, path=path, context=parent_datum)

    def _result_field(self) -> str:
        return RESULT_FIELD_MAP[self.op_symbol]

    @staticmethod
    def _error_path(
        left_datum: DatumInContext | None,
        right_datum: DatumInContext | None,
    ) -> str | None:
        for datum in (left_datum, right_datum):
            if datum is not None:
                return str(datum.full_path)
        return None

    @staticmethod
    def _render_key(parent_key: tuple[JSONPath, ...]) -> str:
        # Match the rendering of `DatumInContext.full_path`: segments are
        # joined with dots and the leading root is omitted (except at the root).
        parts = [
            str(segment)
            for segment in parent_key
            if not isinstance(segment, Root)
        ]
        return ".".join(parts) or "$"

    @staticmethod
    def _missing_side(
        left_matches: list[DatumInContext],
        right_matches: list[DatumInContext],
    ) -> str:
        if not left_matches and not right_matches:
            return "either operand"
        return "the left operand" if not left_matches else "the right operand"

    # ------------------------------------------------------------------
    # Legacy evaluation
    # ------------------------------------------------------------------

    def find(self, datum: typing.Any) -> list[DatumInContext]:
        if self.strict:
            return self.find_strict(datum).results

        result = []
        if isinstance(self.left, JSONPath) and isinstance(self.right, JSONPath):
            left = self.left.find(datum)
            right = self.right.find(datum)
            if left and right and len(left) == len(right):
                for left_datum, right_datum in zip(left, right):
                    try:
                        result.append(self.op(left_datum.value, right_datum.value))
                    except TypeError:
                        return []
            else:
                return []
        elif isinstance(self.left, JSONPath):
            left = self.left.find(datum)
            for left_datum in left:
                try:
                    result.append(self.op(left_datum.value, self.right))
                except TypeError:
                    return []
        elif isinstance(self.right, JSONPath):
            right = self.right.find(datum)
            for right_datum in right:
                try:
                    result.append(self.op(self.left, right_datum.value))
                except TypeError:
                    return []
        else:
            try:
                result.append(self.op(self.left, self.right))
            except TypeError:
                return []
        return [DatumInContext.wrap(r) for r in result]

    def __repr__(self):
        return "{}({!r}{}{!r})".format(
            self.__class__.__name__, self.left, self.op_symbol, self.right
        )

    def __str__(self):
        return f"{self.left} {self.op_symbol} {self.right}"

    def __eq__(self, other):
        return (
            isinstance(other, Operation)
            and self.left == other.left
            and self.op_symbol == other.op_symbol
            and self.right == other.right
        )
