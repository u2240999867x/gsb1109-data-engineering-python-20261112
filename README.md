python-json-patch
=================

[![PyPI version](https://img.shields.io/pypi/v/jsonpatch.svg)](https://pypi.org/project/jsonpatch/)
[![Supported Python versions](https://img.shields.io/pypi/pyversions/jsonpatch.svg)](https://pypi.org/project/jsonpatch/)
[![Build Status](https://github.com/stefankoegl/python-json-patch/actions/workflows/test.yaml/badge.svg?branch=master)](https://github.com/stefankoegl/python-json-patch/actions/workflows/test.yaml)
[![Coverage Status](https://coveralls.io/repos/github/stefankoegl/python-json-patch/badge.svg?branch=master)](https://coveralls.io/github/stefankoegl/python-json-patch?branch=master)

Applying JSON Patches in Python
-------------------------------

Library to apply JSON Patches according to
[RFC 6902](https://datatracker.ietf.org/doc/html/rfc6902)

See the [tutorial](https://python-json-patch.readthedocs.io/en/latest/tutorial.html)
for examples.

* Website: https://github.com/stefankoegl/python-json-patch
* Repository: https://github.com/stefankoegl/python-json-patch.git
* Documentation: https://python-json-patch.readthedocs.io/
* PyPI: https://pypi.org/project/jsonpatch/
* CI: https://github.com/stefankoegl/python-json-patch/actions
* Coveralls: https://coveralls.io/github/stefankoegl/python-json-patch

String append extension
-----------------------
`JsonPatchWithAppend` is a drop-in replacement for `JsonPatch` with a
non-RFC string `append` operation. Its `from_diff()` method sends only the
new suffix when an object member's string grows. `JsonPatch`, `make_patch()`
and `apply_patch()` retain their standard RFC 6902 behavior.

See the [tutorial](https://python-json-patch.readthedocs.io/en/latest/tutorial.html#string-append-extension)
for examples.
Both sender and receiver must support the extension.

Byte-cost diffs
---------------
`JsonPatch.from_diff()` normally minimizes the number of operations. When a
subscriber bills each patch by its UTF-8 wire length instead, pass
`cost='bytes'` to minimize the number of bytes on the wire.

At every object the builder chooses between replacing the whole node and
fixing its members separately, whichever serializes shorter. For arrays it
reuses equal, contiguous runs of items and combines adds, removes and
replacements between them, searching every alignment within a bounded window
(`array_window`, default 8); longer runs fall back to a positional diff. On
documents made only of objects and scalars the result is the exact minimum.

`restricted` takes JSON pointers of columns that are not authorized in the
patch payload. A restricted value is never sent, not even inside an ancestor
replacement; if the target cannot be reached without it,
`RestrictedValueError` is raised.

    >>> patch = JsonPatch.from_diff(src, dst, cost='bytes',
    ...                             restricted=['/ssn'])
    >>> patch.apply(src) == dst
    True

`patch_wire_size()` and `patch_wire_sizes()` report the UTF-8 bytes a patch
uses overall and per operation, so the transmission volume can be reviewed
after replaying the patch.

Type hints
----------
jsonpatch ships type stubs (in the `jsonpatch-stubs` directory, as
jsonpatch.py is a single-file module), so type checkers such as mypy and
pyright work with it without further setup. JSON documents are typed as `Any`.

When changing the public API of jsonpatch.py, update
`jsonpatch-stubs/__init__.pyi` as well. These check that both agree, and that
typical usage in `typetests/usage.py` type-checks:

    pip install mypy .
    python -m mypy.stubtest --mypy-config-file pyproject.toml jsonpatch
    cd typetests && mypy --config-file ../pyproject.toml usage.py

Running tests
-------------
    pip install -r requirements.txt -r requirements-dev.txt
    python tests.py

Running external tests
----------------------
To run external tests (such as those from https://github.com/json-patch/json-patch-tests) use ext_tests.py

    ./ext_tests.py ../json-patch-tests/tests.json

Running property-based tests
----------------------------
Property-based tests using [Hypothesis](https://hypothesis.readthedocs.io/)
are in property_tests.py.

    pip install hypothesis
    python property_tests.py

Set `HYPOTHESIS_PROFILE=thorough` to run many more examples.
