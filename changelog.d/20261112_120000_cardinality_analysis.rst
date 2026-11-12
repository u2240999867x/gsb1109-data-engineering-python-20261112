Added
-----

*   Add finite-structure cardinality analysis in ``jsonpath_ng.cardinality``.

    The new ``maximum_cardinality()`` computes the greatest number of nodes a
    JSONPath expression can match against documents described by finite
    structural constraints (required and optional object keys, finite value
    enumerations, bounded array lengths with finite alternatives, and explicit
    additional-key handling), and returns a legal sample document that reaches
    that count. Field selection, indices, slices, unions, and filters based on
    enumerated equality are supported; unbounded arrays and unsupported
    extensions honestly report an unknown maximum instead of guessing.
