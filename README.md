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

Offline batch merge
-------------------
`jsonmerge` merges RFC 6902 patches that several offline peers produce from
the same JSON baseline. Feed batches to `PatchMerger` in any order, more
than once; each batch carries an `id`, optional `parents`, and operations
with stable `seq` numbers. The merge is deterministic: every receive order,
and two mergers joined with `absorb()`, yield the same document, the same
conflict set and a canonical patch that replays against the common baseline.

Array rows are tracked by identity rather than value, concurrent inserts
into the same gap are ordered by batch id and sequence number, and
conflicting operations are withheld while independent rows still commit.

```python
from jsonmerge import PatchMerger

merger = PatchMerger({'items': [1, 1]})
merger.receive({'id': 'a', 'operations': [
    {'op': 'add', 'path': '/items/1', 'value': 2}]})
merger.receive({'id': 'b', 'operations': [
    {'op': 'add', 'path': '/items/1', 'value': 3}]})
result = merger.merge()
result.document          # {'items': [1, 2, 3, 1]}
result.conflicts         # []
result.canonical_patch   # RFC 6902 patch: baseline -> result.document
```


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
    python jsonmerge_tests.py

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
