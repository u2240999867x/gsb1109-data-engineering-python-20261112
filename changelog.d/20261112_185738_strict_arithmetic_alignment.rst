Added
-----

*   Add strict, parent-record-aligned arithmetic for the extended parser.

    Operations built with ``strict_arithmetic=True`` align two JSONPath
    operands by their common parent record instead of pairing matches in
    document order. A column missing inside a source row is isolated against
    that row, multiple values within one record are reported as ambiguous by
    default, and scalar broadcasting (``allow_scalar_broadcast=True``) stays
    inside the declared parent record. ``Operation.find_strict()`` returns both
    the rows that can be stored and the isolated rows with their positions and
    reasons, while stored values equal the original operation on a single
    complete record.
