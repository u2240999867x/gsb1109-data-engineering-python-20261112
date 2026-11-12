"""
Cardinality analysis for JSONPath expressions against finite input constraints.

The goal is to decide how many nodes a JSONPath expression can match when the
input document is only known through a finite structural constraint:

*   required object keys,
*   a fixed set of optional object keys,
*   finite value enumerations,
*   bounded array lengths (with finite length alternatives), and
*   an explicit flag saying whether an object permits additional keys.

When a finite maximum can be established, :func:`maximum_cardinality` returns it
together with a legal sample document on which the expression actually produces
that many matches. When the constraint (or the expression extension) does not
allow a finite answer, the maximum is reported as ``None`` instead of guessing.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

from .jsonpath import Child
from .jsonpath import Fields
from .jsonpath import Index
from .jsonpath import JSONPath
from .jsonpath import Root
from .jsonpath import Slice
from .jsonpath import This
from .jsonpath import Wildcard
from .jsonpath import _Union

if typing.TYPE_CHECKING:
    from .ext.filter import Expression as _Expression
    from .ext.filter import Filter as _Filter


class CardinalityError(ValueError):
    """Raised when a structural constraint is malformed."""


@dataclass(frozen=True)
class ValueConstraint:
    """A value constrained to a finite enumeration of JSON values."""

    enum: typing.Sequence[typing.Any]

    def __post_init__(self) -> None:
        if isinstance(self.enum, (str, bytes)) or not isinstance(
            self.enum, typing.Sequence
        ):
            raise CardinalityError(
                "ValueConstraint.enum must be a sequence of JSON values"
            )
        if len(self.enum) == 0:
            raise CardinalityError(
                "ValueConstraint.enum must contain at least one value"
            )
        for value in self.enum:
            _validate_json_value(value)


@dataclass(frozen=True)
class ObjectConstraint:
    """
    A constrained JSON object.

    *   ``properties`` maps every known key to its own constraint.
    *   ``required`` is the subset of known keys that must always be present.
    *   the remaining known keys are optional; they may all be present at once.
    *   ``additional_properties`` explicitly states whether other keys are
        allowed. Additional keys are unconstrained, so they make member counts
        unbounded.
    """

    properties: typing.Mapping[str, Constraint]
    required: typing.Collection[str] = frozenset()
    additional_properties: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.properties, typing.Mapping):
            raise CardinalityError("ObjectConstraint.properties must be a mapping")
        known = set(self.properties)
        required = set(self.required)
        if not required <= known:
            raise CardinalityError(
                f"Required keys {sorted(required - known)} are absent from properties"
            )
        for key, constraint in self.properties.items():
            if not isinstance(key, str):
                raise CardinalityError("Object keys must be strings")
            _validate(constraint)


@dataclass(frozen=True)
class ArrayConstraint:
    """
    A constrained JSON array.

    The length is either a single inclusive ``[min_length, max_length]`` interval
    or a finite set of mutually exclusive length ``alternatives``. A
    ``max_length`` of ``None`` means an unbounded array, which forces unknown
    cardinalities.
    """

    element: Constraint | None = None
    min_length: int = 0
    max_length: int | None = None
    alternatives: typing.Sequence[ArrayConstraint] | None = None

    def __post_init__(self) -> None:
        if self.alternatives is not None:
            if not self.alternatives:
                raise CardinalityError(
                    "ArrayConstraint.alternatives must contain at least one alternative"
                )
            for alternative in self.alternatives:
                if not isinstance(alternative, ArrayConstraint):
                    raise CardinalityError(
                        "Array length alternatives must be ArrayConstraint instances"
                    )
                if alternative.alternatives is not None:
                    raise CardinalityError(
                        "Nested array length alternatives are not supported"
                    )
                _validate(alternative)
            return

        if self.element is None:
            raise CardinalityError(
                "ArrayConstraint.element is required without alternatives"
            )
        _validate(self.element)
        if isinstance(self.min_length, bool) or not isinstance(self.min_length, int):
            raise CardinalityError("ArrayConstraint.min_length must be an int")
        if self.min_length < 0:
            raise CardinalityError("ArrayConstraint.min_length must be non-negative")
        if self.max_length is not None:
            if isinstance(self.max_length, bool) or not isinstance(
                self.max_length, int
            ):
                raise CardinalityError(
                    "ArrayConstraint.max_length must be an int or None"
                )
            if self.max_length < self.min_length:
                raise CardinalityError(
                    "ArrayConstraint.max_length must be >= min_length"
                )

    def length_alternatives(self) -> tuple[ArrayConstraint, ...]:
        """Return the mutually exclusive length intervals the array may use."""

        if self.alternatives is not None:
            return tuple(self.alternatives)
        return (self,)


@dataclass(frozen=True)
class _AnyConstraint:
    """An unconstrained value, about which nothing finite can be established."""


ANY: typing.Final = _AnyConstraint()

Constraint: typing.TypeAlias = (
    ValueConstraint | ObjectConstraint | ArrayConstraint | _AnyConstraint
)


@dataclass(frozen=True)
class Cardinality:
    """
    The result of a cardinality analysis.

    *   ``maximum`` is the greatest number of nodes the expression may match,
        counting repeated hits separately, or ``None`` when no finite bound
        follows from the constraint.
    *   ``sample`` is a legal document on which a known maximum is actually
        reached. It is ``None`` whenever the maximum is unknown.
    """

    maximum: int | None
    sample: typing.Any

    @property
    def is_known(self) -> bool:
        return self.maximum is not None

    @property
    def is_single_valued(self) -> bool:
        """Whether the expression is safe to write into a single-value column."""

        return self.maximum == 1


def maximum_cardinality(
    expression: JSONPath, constraint: Constraint
) -> Cardinality:
    """
    Compute the maximum number of nodes ``expression`` may match on documents
    satisfying ``constraint``, together with a document attaining that maximum.

    The claimed maximum is only returned if the accompanying sample is itself
    legal under ``constraint`` and the running expression actually reaches the
    claimed hit count on it. Otherwise the result is honestly reported as
    unknown instead of risking an over-estimate.
    """

    _validate(constraint)
    maximum, sample = _analyze(expression, constraint)
    if maximum is None:
        return Cardinality(None, None)
    if not _matches(sample, constraint):
        return Cardinality(None, None)
    if len(expression.find(sample)) != maximum:
        return Cardinality(None, None)
    return Cardinality(maximum, sample)


def matches_constraint(value: typing.Any, constraint: Constraint) -> bool:
    """Return whether ``value`` is a legal document under ``constraint``."""

    _validate(constraint)
    return _matches(value, constraint)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_json_value(value: typing.Any) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CardinalityError("Enum object keys must be strings")
            _validate_json_value(item)
        return
    raise CardinalityError(f"Value {value!r} is not a JSON-compatible value")


def _validate(constraint: Constraint) -> None:
    if not isinstance(
        constraint,
        (ValueConstraint, ObjectConstraint, ArrayConstraint, _AnyConstraint),
    ):
        raise CardinalityError(
            f"Expected a structure constraint, got {type(constraint).__name__}"
        )
    # The dataclass ``__post_init__`` methods validate nested constraints too.
    if not isinstance(constraint, _AnyConstraint):
        constraint.__post_init__()


def _matches(value: typing.Any, constraint: Constraint) -> bool:
    if isinstance(constraint, _AnyConstraint):
        return True
    if isinstance(constraint, ValueConstraint):
        return any(_json_equal(value, allowed) for allowed in constraint.enum)
    if isinstance(constraint, ObjectConstraint):
        if not isinstance(value, dict):
            return False
        keys = set(value)
        if not set(constraint.required) <= keys:
            return False
        if not constraint.additional_properties and not keys <= set(
            constraint.properties
        ):
            return False
        for key in keys & set(constraint.properties):
            if not _matches(value[key], constraint.properties[key]):
                return False
        return True

    # ArrayConstraint, including length alternatives.
    if not isinstance(value, list):
        return False
    chosen: ArrayConstraint | None = None
    for alternative in constraint.length_alternatives():
        upper = (
            float("inf") if alternative.max_length is None else alternative.max_length
        )
        if alternative.min_length <= len(value) <= upper:
            chosen = alternative
            break
    if chosen is None or chosen.element is None:
        return False
    return all(_matches(item, chosen.element) for item in value)


def _json_equal(left: typing.Any, right: typing.Any) -> bool:
    """Equality that keeps booleans distinct from numbers, as JSON does."""

    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return False
    return left == right


# ---------------------------------------------------------------------------
# Minimal and maximal legal documents
# ---------------------------------------------------------------------------


def _minimal(constraint: Constraint) -> typing.Any:
    if isinstance(constraint, _AnyConstraint):
        return None
    if isinstance(constraint, ValueConstraint):
        return constraint.enum[0]
    if isinstance(constraint, ObjectConstraint):
        return {
            key: _minimal(child)
            for key, child in constraint.properties.items()
            if key in set(constraint.required)
        }
    alternative = constraint.length_alternatives()[0]
    assert alternative.element is not None
    return [_minimal(alternative.element) for _ in range(alternative.min_length)]


def _maximal(constraint: Constraint) -> typing.Any:
    """Build a document filling every finite slot the constraint offers."""

    if isinstance(constraint, (_AnyConstraint, ValueConstraint)):
        return _minimal(constraint)
    if isinstance(constraint, ObjectConstraint):
        return {key: _maximal(child) for key, child in constraint.properties.items()}
    alternative = _longest_alternative(constraint)
    if alternative is None or alternative.max_length is None:
        return _minimal(constraint)
    assert alternative.element is not None
    return [
        _maximal(alternative.element) for _ in range(alternative.max_length)
    ]


def _longest_alternative(
    constraint: ArrayConstraint,
) -> ArrayConstraint | None:
    finite = [
        alternative
        for alternative in constraint.length_alternatives()
        if alternative.max_length is not None
    ]
    if not finite:
        return None
    return max(finite, key=lambda alternative: alternative.max_length)


def _has_container_enum(constraint: ValueConstraint) -> bool:
    return any(isinstance(value, (list, dict)) for value in constraint.enum)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _unknown() -> tuple[int | None, typing.Any]:
    return None, None


def _analyze(
    node: JSONPath, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    if isinstance(node, (This, Root)):
        return 1, _minimal(constraint)

    if isinstance(node, Fields):
        return _analyze_fields(node, constraint)

    if isinstance(node, Wildcard):
        return _analyze_wildcard(constraint)

    if isinstance(node, Index):
        return _analyze_index(node, constraint)

    if isinstance(node, Slice):
        return _analyze_slice(node, constraint)

    if isinstance(node, Child):
        return _analyze_child(node, constraint)

    if isinstance(node, _Union):
        return _analyze_union(node, constraint)

    # Filter lives in the extension subpackage and is imported lazily to avoid
    # any import cycle between the base package and its extensions.
    from .ext.filter import Filter

    if isinstance(node, Filter):
        return _analyze_filter(node, constraint)

    # Descendants, Where/WhereNot, Parent, intersects, and every extension node
    # (Len, Keys, Path, SortedThis, arithmetic/string operations) have no finite
    # interpretation under these constraints, so the answer is honestly unknown.
    return _unknown()


def _analyze_fields(
    node: Fields, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    if node._deprecated_wildcard_use:
        return _unknown()
    if isinstance(constraint, _AnyConstraint):
        return _unknown()
    if isinstance(constraint, ValueConstraint):
        if _has_container_enum(constraint):
            return _unknown()
        return 0, _minimal(constraint)
    if isinstance(constraint, ArrayConstraint):
        return 0, _minimal(constraint)

    assert isinstance(constraint, ObjectConstraint)
    sample = _minimal(constraint)
    maximum = 0
    for field in node.fields:
        if field in constraint.properties:
            maximum += 1
            sample[field] = _maximal(constraint.properties[field])
        elif constraint.additional_properties:
            # Additional keys are unconstrained; each requested name may exist.
            maximum += 1
            sample[field] = None
        # A disallowed key can never be present, so it contributes nothing.
    return maximum, sample


def _analyze_wildcard(
    constraint: Constraint,
) -> tuple[int | None, typing.Any]:
    if isinstance(constraint, ObjectConstraint):
        if constraint.additional_properties:
            return _unknown()
        return len(constraint.properties), _maximal(constraint)
    if isinstance(constraint, ArrayConstraint):
        alternative = _longest_alternative(constraint)
        if alternative is None or alternative.max_length is None:
            return _unknown()
        return alternative.max_length, _maximal(constraint)
    if isinstance(constraint, ValueConstraint):
        if _has_container_enum(constraint):
            return _unknown()
        return 0, _minimal(constraint)
    return _unknown()


def _analyze_index(
    node: Index, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    if isinstance(constraint, ArrayConstraint):
        alternative = _longest_alternative(constraint)
        if alternative is None or alternative.max_length is None:
            return _unknown()
        length = alternative.max_length
        maximum = sum(
            1 for index in node.indices if -length <= index < length
        )
        if maximum == 0:
            return 0, _minimal(constraint)
        assert alternative.element is not None
        sample = [_minimal(alternative.element) for _ in range(length)]
        for index in node.indices:
            if -length <= index < length:
                sample[index % length] = _maximal(alternative.element)
        return maximum, sample
    if isinstance(constraint, (ObjectConstraint, ValueConstraint)):
        if isinstance(constraint, ValueConstraint) and _has_container_enum(
            constraint
        ):
            return _unknown()
        return 0, _minimal(constraint)
    return _unknown()


def _analyze_slice(
    node: Slice, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    if node._deprecated_wildcard_use:
        return _unknown()
    if node.step == 0:
        # A zero step selects nothing.
        return 0, _minimal(constraint)
    if isinstance(constraint, ArrayConstraint):
        alternative = _longest_alternative(constraint)
        if alternative is None or alternative.max_length is None:
            return _unknown()
        length = alternative.max_length
        maximum = _slice_hit_count(node.start, node.end, node.step, length)
        if maximum == 0:
            return 0, _minimal(constraint)
        return maximum, _maximal(constraint)
    if isinstance(constraint, (ObjectConstraint, ValueConstraint)):
        if isinstance(constraint, ValueConstraint) and _has_container_enum(
            constraint
        ):
            return _unknown()
        # Compliant slices select nothing from objects and scalars.
        return 0, _minimal(constraint)
    return _unknown()


def _slice_hit_count(
    start: int | None, end: int | None, step: int | None, length: int
) -> int:
    # Delegate boundary normalization to the same ``slice`` object the runtime
    # uses in ``Slice.find`` so positive and negative steps agree exactly.
    normalized_start, stop, normalized_step = slice(start, end, step).indices(
        length
    )
    if normalized_step > 0:
        if normalized_start >= stop:
            return 0
        return (stop - normalized_start + normalized_step - 1) // normalized_step
    if normalized_start <= stop:
        return 0
    return (normalized_start - stop + (-normalized_step) - 1) // (
        -normalized_step
    )


def _analyze_child(
    node: Child, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    sub_constraints = _sub_constraints(node.left, constraint)
    if sub_constraints is None:
        return _unknown()

    analyses = [_analyze(node.right, sub) for sub in sub_constraints]
    if any(maximum is None for maximum, _ in analyses):
        return _unknown()
    maximum = sum(maximum for maximum, _ in analyses)

    sample = _build(node.left, constraint)
    if not sub_constraints:
        return maximum, sample

    datums = node.left.find(sample)
    if len(datums) != len(analyses):
        # The structural model and the runtime disagree; stay conservative.
        return _unknown()
    for datum, (_, sub_sample) in zip(datums, analyses):
        if sub_sample is not None:
            datum.value = _overlay(datum.value, sub_sample)
    sample = _reattach_root(sample, datums)
    return maximum, sample


def _analyze_union(
    node: _Union, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    left_max, left_sample = _analyze(node.left, constraint)
    right_max, right_sample = _analyze(node.right, constraint)
    if left_max is None or right_max is None:
        return _unknown()
    # Unions concatenate their nodelists, so overlapping hits count repeatedly,
    # and the two sides share one document instead of two separate documents.
    return left_max + right_max, _deep_merge(left_sample, right_sample)


def _reattach_root(sample: typing.Any, datums: list[typing.Any]) -> typing.Any:
    """Recover the root document when a top-level datum was replaced in place."""

    for datum in datums:
        if datum.context is None and datum.value is not sample:
            return datum.value
    return sample


# ---------------------------------------------------------------------------
# Constraint propagation through the supported AST nodes
# ---------------------------------------------------------------------------


def _sub_constraints(
    node: JSONPath, constraint: Constraint
) -> list[Constraint] | None:
    """
    Return the constraints of every node the left-hand side may match.

    Every returned slot is assumed to exist simultaneously when maximizing,
    which is valid because optional object keys may all be present and bounded
    arrays may all run at their maximum length.
    """

    if isinstance(node, (This, Root)):
        return [constraint]

    if isinstance(node, Fields):
        if node._deprecated_wildcard_use:
            return None
        if isinstance(constraint, ObjectConstraint):
            result: list[Constraint] = []
            for field in node.fields:
                if field in constraint.properties:
                    result.append(constraint.properties[field])
                elif constraint.additional_properties:
                    result.append(ANY)
            return result
        if isinstance(constraint, (ArrayConstraint, ValueConstraint)):
            if isinstance(constraint, ValueConstraint) and _has_container_enum(
                constraint
            ):
                return None
            return []
        return None

    if isinstance(node, Wildcard):
        if isinstance(constraint, ObjectConstraint):
            if constraint.additional_properties:
                return None
            return list(constraint.properties.values())
        if isinstance(constraint, ArrayConstraint):
            alternative = _longest_alternative(constraint)
            if alternative is None or alternative.max_length is None:
                return None
            assert alternative.element is not None
            return [alternative.element] * alternative.max_length
        if isinstance(constraint, ValueConstraint):
            if _has_container_enum(constraint):
                return None
            return []
        return None

    if isinstance(node, Index):
        if isinstance(constraint, ArrayConstraint):
            alternative = _longest_alternative(constraint)
            if alternative is None or alternative.max_length is None:
                return None
            length = alternative.max_length
            assert alternative.element is not None
            return [
                alternative.element
                for index in node.indices
                if -length <= index < length
            ]
        if isinstance(constraint, (ObjectConstraint, ValueConstraint)):
            if isinstance(constraint, ValueConstraint) and _has_container_enum(
                constraint
            ):
                return None
            return []
        return None

    if isinstance(node, Slice):
        if node._deprecated_wildcard_use:
            return None
        if node.step == 0:
            return []
        if isinstance(constraint, ArrayConstraint):
            alternative = _longest_alternative(constraint)
            if alternative is None or alternative.max_length is None:
                return None
            length = alternative.max_length
            assert alternative.element is not None
            positions = list(range(length)[node.start : node.end : node.step])
            return [alternative.element] * len(positions)
        if isinstance(constraint, (ObjectConstraint, ValueConstraint)):
            if isinstance(constraint, ValueConstraint) and _has_container_enum(
                constraint
            ):
                return None
            return []
        return None

    if isinstance(node, Child):
        left = _sub_constraints(node.left, constraint)
        if left is None:
            return None
        result: list[Constraint] = []
        for sub in left:
            right = _sub_constraints(node.right, sub)
            if right is None:
                return None
            result.extend(right)
        return result

    if isinstance(node, _Union):
        left = _sub_constraints(node.left, constraint)
        right = _sub_constraints(node.right, constraint)
        if left is None or right is None:
            return None
        return left + right

    from .ext.filter import Filter

    if isinstance(node, Filter):
        return _filter_sub_constraints(node, constraint)

    return None


# ---------------------------------------------------------------------------
# Filters based on enumerated equality
# ---------------------------------------------------------------------------


def _analyze_filter(
    node: _Filter, constraint: Constraint
) -> tuple[int | None, typing.Any]:
    if isinstance(constraint, ArrayConstraint):
        plan = _array_filter_plan(node, constraint)
        if plan is None:
            return _unknown()
        alternative, passes = plan
        return sum(passes), _build_array_filter_sample(
            alternative, passes, node.expressions
        )

    if isinstance(constraint, ObjectConstraint):
        plan = _object_filter_plan(node, constraint)
        if plan is None:
            return _unknown()
        maximum = sum(pass_ for _, _, pass_ in plan)
        return maximum, _build_object_filter_sample(plan, node.expressions)

    if isinstance(constraint, ValueConstraint):
        if _has_container_enum(constraint):
            return _unknown()
        # A filter selects nothing from a scalar.
        return 0, _minimal(constraint)

    return _unknown()


def _array_filter_plan(
    node: _Filter, constraint: ArrayConstraint
) -> tuple[ArrayConstraint, list[int]] | None:
    """
    Choose the length alternative on which the most elements pass, returning
    one pass/fail flag per element at that alternative's maximum length.

    An alternative whose filter outcome cannot be decided can still not pass
    more elements than its ``max_length``. Such an alternative only forces an
    unknown answer when its capacity strictly exceeds the best count that a
    decidable alternative is known to reach; otherwise a decidable alternative
    already dominates it.
    """

    best: tuple[int, ArrayConstraint, list[int]] | None = None
    undecidable_capacity = 0
    for alternative in constraint.length_alternatives():
        if alternative.max_length is None or alternative.element is None:
            return None
        flags: list[int] = []
        decidable = True
        for _ in range(alternative.max_length):
            must_fail = _conjunction_must_fail(
                node.expressions, alternative.element
            )
            if must_fail is None:
                decidable = False
                break
            flags.append(0 if must_fail else 1)
        if not decidable:
            undecidable_capacity = max(
                undecidable_capacity, alternative.max_length
            )
            continue
        count = sum(flags)
        if best is None or count > best[0]:
            best = (count, alternative, flags)

    if best is None or undecidable_capacity > best[0]:
        return None
    return best[1], best[2]


def _object_filter_plan(
    node: _Filter, constraint: ObjectConstraint
) -> list[tuple[str, Constraint, int]] | None:
    if constraint.additional_properties:
        return None
    plan: list[tuple[str, Constraint, int]] = []
    for key, child in constraint.properties.items():
        must_fail = _conjunction_must_fail(node.expressions, child)
        if must_fail is None:
            return None
        plan.append((key, child, 0 if must_fail else 1))
    return plan


def _filter_sub_constraints(
    node: _Filter, constraint: Constraint
) -> list[Constraint] | None:
    if isinstance(constraint, ArrayConstraint):
        plan = _array_filter_plan(node, constraint)
        if plan is None:
            return None
        alternative, passes = plan
        assert alternative.element is not None
        return [alternative.element for pass_ in passes if pass_ == 1]

    if isinstance(constraint, ObjectConstraint):
        plan = _object_filter_plan(node, constraint)
        if plan is None:
            return None
        return [child for _, child, pass_ in plan if pass_ == 1]

    if isinstance(constraint, ValueConstraint):
        if _has_container_enum(constraint):
            return None
        return []

    return None


def _conjunction_must_fail(
    expressions: typing.Sequence[JSONPath], constraint: Constraint
) -> bool | None:
    """
    Decide whether a filter conjunction fails on *every* legal candidate.

    *   ``True``: no legal value can satisfy the conjunction (count it as zero),
    *   ``False``: a legal value can satisfy it (count it as a possible match),
    *   ``None``: the constraint does not decide it.

    Equality conditions on the same member are correlated: asking one member to
    equal two different enumerated values can never hold at once.
    """

    return _must_fail(expressions, constraint)


def _must_fail(
    node: JSONPath | typing.Sequence[JSONPath], constraint: Constraint
) -> bool | None:
    from .ext.filter import And
    from .ext.filter import Expression
    from .ext.filter import Or

    if isinstance(node, (list, tuple)) or isinstance(node, And):
        operands = node if isinstance(node, (list, tuple)) else node.operands
        results = [_must_fail(operand, constraint) for operand in operands]
        if any(result is None for result in results):
            return None
        if any(result is True for result in results):
            return True
        # Every condition is individually satisfiable; a joint equality
        # conflict is the only remaining way the conjunction could always fail.
        conflict = _equality_conflict(operands, constraint)
        # If the conflict cannot be proven, the running engine revalidates the
        # generated sample, so it is safe to assume the conjunction can pass.
        return bool(conflict)

    if isinstance(node, Or):
        results = [_must_fail(operand, constraint) for operand in node.operands]
        if any(result is None for result in results):
            return None
        # A disjunction fails only when every branch fails.
        return all(result is True for result in results)

    if not isinstance(node, Expression):
        return None

    if node.op == "!":
        existence = _decide_existence(node.target, constraint)
        if existence is None:
            return None
        _can_exist, must_exist = existence
        # Negation must fail precisely when the target is guaranteed present.
        return must_exist

    if node.op is None:
        existence = _decide_existence(node.target, constraint)
        if existence is None:
            return None
        can_exist, _must_exist = existence
        return not can_exist

    if isinstance(node.value, JSONPath):
        # Query-to-query comparisons depend on values the constraint may not fix.
        return None

    if node.op not in ("==", "="):
        return None

    return _equality_must_fail(node, constraint)


def _equality_must_fail(
    expression: _Expression, constraint: Constraint
) -> bool | None:
    literal = expression.value

    if isinstance(expression.target, This):
        if isinstance(constraint, ValueConstraint):
            return not any(
                _json_equal(literal, allowed) for allowed in constraint.enum
            )
        if isinstance(literal, (list, dict)):
            return None
        if isinstance(constraint, (ObjectConstraint, ArrayConstraint)):
            # A container never equals a scalar literal.
            return True
        return None

    chain = _singular_field(expression.target)
    if chain is None:
        return None

    classification = _classify_chain(chain, constraint)
    if classification == "absent":
        # A member that can never exist cannot satisfy an equality.
        return True
    if classification == "unknown":
        return None

    target_object, field_name = _walk_chain(chain, constraint)
    if target_object is None:
        return None
    child = target_object.properties.get(field_name)
    if child is None:
        return None

    if isinstance(child, ValueConstraint):
        return not any(
            _json_equal(literal, value) for value in child.enum
        )
    if isinstance(literal, (list, dict)):
        return None
    # An object or array member never equals a scalar literal.
    if isinstance(child, (ObjectConstraint, ArrayConstraint)):
        return True
    return None


def _equality_conflict(
    operands: typing.Sequence[JSONPath], constraint: Constraint
) -> bool | None:
    """
    Detect equalities on one member that no single legal value can satisfy.

    For example ``@.role == 'admin' && @.role == 'guest'`` cannot hold at once
    when ``role`` is a single enumerated member, even though each comparison is
    individually satisfiable.
    """

    requirements: dict[tuple[str, ...], list[typing.Any]] = {}
    if not _collect_equalities(operands, constraint, (), requirements):
        return None
    for chain, literals in requirements.items():
        target_constraint = constraint
        if chain:
            target_object, field_name = _walk_chain(chain, constraint)
            if target_object is None:
                return None
            leaf = target_object.properties.get(field_name)
            if not isinstance(leaf, ValueConstraint):
                continue
            target_constraint = leaf
        if not isinstance(target_constraint, ValueConstraint):
            continue
        if not any(
            all(_json_equal(literal, allowed) for literal in literals)
            for allowed in target_constraint.enum
        ):
            return True
    return False


def _collect_equalities(
    operands: typing.Sequence[JSONPath],
    constraint: Constraint,
    prefix: tuple[str, ...],
    requirements: dict[tuple[str, ...], list[typing.Any]],
) -> bool:
    """Gather decidable equality requirements keyed by their member chain."""

    from .ext.filter import And
    from .ext.filter import Expression
    from .ext.filter import Or

    for operand in operands:
        if isinstance(operand, (list, tuple, And)):
            nested = operand if isinstance(operand, (list, tuple)) else operand.operands
            if not _collect_equalities(nested, constraint, prefix, requirements):
                return False
            continue
        if isinstance(operand, Or):
            # Only one branch needs to hold, so branches do not constrain jointly.
            continue
        if not isinstance(operand, Expression):
            return False
        if isinstance(operand.value, JSONPath) or operand.op not in ("==", "="):
            return False
        if isinstance(operand.target, This):
            requirements.setdefault(prefix, []).append(operand.value)
            continue
        chain = _singular_field(operand.target)
        if chain is None:
            return False
        if _classify_chain(chain, constraint) != "known":
            return False
        requirements.setdefault(prefix + chain, []).append(operand.value)
    return True


def _decide_existence(
    path: JSONPath, constraint: Constraint
) -> tuple[bool, bool] | None:
    """Decide whether a singular query has a node: ``(can_exist, must_exist)``."""

    if isinstance(path, This):
        return True, True
    chain = _singular_field(path)
    if chain is None:
        return None

    current: Constraint = constraint
    must_exist = True
    for field_name in chain:
        if not isinstance(current, ObjectConstraint):
            # The chain cannot resolve through a non-object intermediate.
            return False, False
        child = current.properties.get(field_name)
        if child is None:
            if current.additional_properties:
                # The member may exist with any value, but is not guaranteed.
                return True, False
            # A forbidden member can never exist and is never required.
            return False, False
        if field_name not in set(current.required):
            must_exist = False
        current = child
    return True, must_exist


def _classify_chain(
    chain: typing.Sequence[str], constraint: Constraint
) -> typing.Literal["known", "absent", "unknown"]:
    """
    Classify how a field chain can resolve on a candidate value.

    *   ``known``: every key is in the schema and intermediates are objects,
    *   ``absent``: the chain can never resolve (a forbidden key or a
        non-object intermediate),
    *   ``unknown``: an additional member is needed, so existence is possible
        but the value is unconstrained.
    """

    current: Constraint = constraint
    for field_name in chain:
        if not isinstance(current, ObjectConstraint):
            return "absent"
        child = current.properties.get(field_name)
        if child is None:
            if current.additional_properties:
                return "unknown"
            return "absent"
        current = child
    return "known"


def _walk_chain(
    chain: typing.Sequence[str], constraint: Constraint
) -> tuple[ObjectConstraint | None, str]:
    """
    Return the object constraint holding the final key and the final key.
    Only valid for chains that :func:`_classify_chain` recognizes as ``known``.
    """

    current: Constraint = constraint
    for field_name in chain[:-1]:
        if not isinstance(current, ObjectConstraint):
            return None, ""
        child = current.properties.get(field_name)
        if child is None:
            return None, ""
        current = child
    if not isinstance(current, ObjectConstraint):
        return None, ""
    return current, chain[-1]


def _singular_field(path: JSONPath) -> tuple[str, ...] | None:
    """Return the field chain of a singular, name-only query, if it is one."""

    chain: list[str] = []
    current: JSONPath = path
    while isinstance(current, Child):
        if not isinstance(current.right, Fields) or len(current.right.fields) != 1:
            return None
        if current.right._deprecated_wildcard_use:
            return None
        chain.append(current.right.fields[0])
        current = current.left
    if isinstance(current, This):
        return tuple(reversed(chain))
    return None


# ---------------------------------------------------------------------------
# Concrete sample construction for filters
# ---------------------------------------------------------------------------


def _build_array_filter_sample(
    alternative: ArrayConstraint,
    passes: list[int],
    expressions: typing.Sequence[JSONPath],
) -> list[typing.Any]:
    assert alternative.element is not None
    sample: list[typing.Any] = []
    for pass_ in passes:
        if pass_ == 0:
            sample.append(_failing_value(alternative.element, expressions))
        else:
            sample.append(_satisfying_value(alternative.element, expressions))
    return sample


def _build_object_filter_sample(
    plan: list[tuple[str, Constraint, int]],
    expressions: typing.Sequence[JSONPath],
) -> dict[str, typing.Any]:
    sample: dict[str, typing.Any] = {}
    for key, child, pass_ in plan:
        if pass_ == 1:
            sample[key] = _satisfying_value(child, expressions)
        else:
            sample[key] = _failing_value(child, expressions)
    return sample


def _satisfying_value(
    constraint: Constraint, expressions: typing.Sequence[JSONPath]
) -> typing.Any:
    """Build a value that passes the provable parts of ``expressions``."""

    from .ext.filter import Expression

    if isinstance(constraint, ValueConstraint):
        # A filter over an enumerated scalar only passes via an ``@`` equality.
        for expression in expressions:
            if (
                isinstance(expression, Expression)
                and expression.op in ("==", "=")
                and isinstance(expression.target, This)
                and not isinstance(expression.value, JSONPath)
            ):
                for allowed in constraint.enum:
                    if _json_equal(expression.value, allowed):
                        return allowed
        return _minimal(constraint)

    sample = _minimal(constraint)
    for expression in expressions:
        _apply_satisfying(expression, sample, constraint)
    return sample


def _apply_satisfying(
    expression: JSONPath, sample: typing.Any, constraint: Constraint
) -> bool:
    """Mutate ``sample`` so one expression holds; report whether it applied."""

    from .ext.filter import And
    from .ext.filter import Expression
    from .ext.filter import Or

    if isinstance(expression, And):
        return all(
            _apply_satisfying(operand, sample, constraint)
            for operand in expression.operands
        )

    if isinstance(expression, Or):
        # A disjunction holds when any branch holds, so satisfy the first
        # branch whose assignments the constraint permits.
        return any(
            _apply_satisfying(operand, sample, constraint)
            for operand in expression.operands
        )

    if not isinstance(expression, Expression):
        return False
    if isinstance(expression.value, JSONPath):
        return False
    if expression.op == "!":
        # Passing a negation means the target is absent, which the minimal
        # document already guarantees whenever the outcome is decidable.
        return True
    if expression.op is None:
        chain = _singular_field(expression.target)
        return chain is not None and _ensure_chain(
            sample, constraint, chain
        )
    if expression.op not in ("==", "="):
        return False

    chain = _singular_field(expression.target)
    if chain is not None and isinstance(constraint, ObjectConstraint):
        return _set_chain(sample, constraint, chain, expression.value)
    return False


def _failing_value(
    constraint: Constraint, expressions: typing.Sequence[JSONPath]
) -> typing.Any:
    """Build a value that fails the provable parts of ``expressions``."""

    from .ext.filter import Expression

    if isinstance(constraint, ValueConstraint):
        for expression in expressions:
            if (
                isinstance(expression, Expression)
                and expression.op in ("==", "=")
                and isinstance(expression.target, This)
                and not isinstance(expression.value, JSONPath)
            ):
                for allowed in constraint.enum:
                    if not _json_equal(expression.value, allowed):
                        return allowed
        return _minimal(constraint)

    # Certain failures (forbidden keys, disjoint enums, missing optional
    # members) already hold on the minimal document.
    return _minimal(constraint)


def _ensure_chain(
    sample: dict[str, typing.Any],
    constraint: Constraint,
    chain: typing.Sequence[str],
) -> bool:
    """Ensure every member of ``chain`` exists, filling leaves minimally."""

    current_data: dict[str, typing.Any] = sample
    current_constraint: Constraint = constraint
    for field_name in chain:
        if not isinstance(current_constraint, ObjectConstraint):
            return False
        child = current_constraint.properties.get(field_name)
        if child is None:
            return False
        if field_name not in current_data:
            current_data[field_name] = _minimal(child)
        current_data = current_data[field_name]
        current_constraint = child
    return True


def _set_chain(
    sample: dict[str, typing.Any],
    constraint: ObjectConstraint,
    chain: typing.Sequence[str],
    value: typing.Any,
) -> bool:
    current_data: dict[str, typing.Any] = sample
    current_constraint: Constraint = constraint
    for field_name in chain[:-1]:
        if not isinstance(current_constraint, ObjectConstraint):
            return False
        child = current_constraint.properties.get(field_name)
        if child is None:
            return False
        if field_name not in current_data:
            current_data[field_name] = _minimal(child)
        current_data = current_data[field_name]
        current_constraint = child
    if not isinstance(current_constraint, ObjectConstraint):
        return False
    leaf = current_constraint.properties.get(chain[-1])
    if leaf is None or not _matches(value, leaf):
        # Never install a value the leaf constraint forbids.
        return False
    current_data[chain[-1]] = value
    return True


# ---------------------------------------------------------------------------
# Concrete sample construction for the supported nodes
# ---------------------------------------------------------------------------


def _build(node: JSONPath, constraint: Constraint) -> typing.Any:
    if isinstance(node, (This, Root)):
        return _minimal(constraint)

    if isinstance(node, Fields) and isinstance(constraint, ObjectConstraint):
        sample = _minimal(constraint)
        for field in node.fields:
            if field in constraint.properties:
                sample[field] = _maximal(constraint.properties[field])
            elif constraint.additional_properties:
                sample[field] = None
        return sample

    if isinstance(node, Wildcard):
        return _maximal(constraint)

    if isinstance(node, Index) and isinstance(constraint, ArrayConstraint):
        alternative = _longest_alternative(constraint)
        assert alternative is not None and alternative.max_length is not None
        length = alternative.max_length
        assert alternative.element is not None
        sample = [_minimal(alternative.element) for _ in range(length)]
        for index in node.indices:
            if -length <= index < length:
                sample[index % length] = _maximal(alternative.element)
        return sample

    if isinstance(node, Slice) and isinstance(constraint, ArrayConstraint):
        return _maximal(constraint)

    if isinstance(node, Child):
        sample = _build(node.left, constraint)
        subs = _sub_constraints(node.left, constraint)
        if not subs:
            return sample
        datums = node.left.find(sample)
        if len(datums) != len(subs):
            return sample
        for datum, sub in zip(datums, subs):
            datum.value = _overlay(datum.value, _build(node.right, sub))
        return _reattach_root(sample, datums)

    if isinstance(node, _Union):
        return _deep_merge(
            _build(node.left, constraint), _build(node.right, constraint)
        )

    from .ext.filter import Filter

    if isinstance(node, Filter):
        if isinstance(constraint, ArrayConstraint):
            plan = _array_filter_plan(node, constraint)
            if plan is not None:
                alternative, passes = plan
                return _build_array_filter_sample(
                    alternative, passes, node.expressions
                )
        if isinstance(constraint, ObjectConstraint):
            plan = _object_filter_plan(node, constraint)
            if plan is not None:
                return _build_object_filter_sample(plan, node.expressions)

    return _minimal(constraint)


def _deep_merge(left: typing.Any, right: typing.Any) -> typing.Any:
    """Merge two samples built under the same constraint."""

    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged
    if isinstance(left, list) and isinstance(right, list):
        length = max(len(left), len(right))
        merged: list[typing.Any] = []
        for index in range(length):
            if index < len(left) and index < len(right):
                merged.append(_deep_merge(left[index], right[index]))
            elif index < len(left):
                merged.append(left[index])
            else:
                merged.append(right[index])
        return merged
    return left if left is not None else right


def _overlay(base: typing.Any, addition: typing.Any) -> typing.Any:
    """
    Combine a right-hand sample onto a datum already built for the left side.

    The left datum is already a complete, filter-satisfying value. The right
    side only needs to make sure the members it selects exist and are as large
    as the constraint allows, so:

    *   nested objects merge by filling missing keys, never overwriting a
        scalar the left side tuned to satisfy a filter,
    *   arrays keep the longer of the two (both are legal, and the longer one
        maximizes index and slice hits),
    *   scalars keep the base value, which is already legal.
    """

    if isinstance(base, dict) and isinstance(addition, dict):
        merged = dict(base)
        for key, value in addition.items():
            if key in merged:
                merged[key] = _overlay(merged[key], value)
            else:
                merged[key] = value
        return merged
    if isinstance(base, list) and isinstance(addition, list):
        return addition if len(addition) > len(base) else base
    # ``None`` is only ever a placeholder for an unset additional member, so
    # let the right side populate it; every other scalar keeps its base value.
    return addition if base is None else base
