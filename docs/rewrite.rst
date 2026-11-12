Field level rewriting
=====================

.. module:: msgpack

The :mod:`msgpack.rewrite` module edits selected fields of packed MessagePack
messages without unpacking the whole message into Python objects. It is pure
Python and works on messages larger than the available memory.

A *plan* is a collection of instructions addressed by :class:`~msgpack.Path`
objects. Each instruction is one of:

* :class:`~msgpack.Replace` -- overwrite one node with freshly packed bytes.
* :class:`~msgpack.Delete` -- remove one node, including its key when it is a
  map value.
* :class:`~msgpack.Copy` -- copy the original bytes of a subtree onto another
  location. The source may sit before or after the destination.

Bytes that the plan does not address are copied verbatim: untouched map
pairs, extension types, floating point widths (float32 vs float64) and the
ordering of elements all stay byte for byte identical.

Paths
-----

A path is a sequence of steps. An integer step selects an array position; a
string or bytes step selects the first occurrence of a map key. Use
:class:`~msgpack.MapEntry` to select later occurrences of duplicated keys.

.. code-block:: python

    from msgpack import Path, MapEntry

    Path("user", "emails", 2)
    Path(MapEntry("id", 1))  # the second occurrence of the "id" key

One-shot rewrite
----------------

:func:`~msgpack.rewriteb` rewrites a single complete message held in a
``bytes``-like object.

.. code-block:: python

    from msgpack import packb, rewriteb, RewritePlan, Replace, Delete

    message = packb({"a": 1, "b": [10, 20, 30], "c": "x"})
    plan = RewritePlan([Replace(("a",), 999), Delete(("b", 1))])
    rewritten = rewriteb(message, plan)

Copying subtrees
----------------

:class:`~msgpack.Copy` takes a *source* path and a *target* path. It always
reads the original bytes, even if the source subtree is itself deleted or
replaced by another instruction, and even if the source appears later in the
message than the target. Two copies can therefore exchange two subtrees.

.. code-block:: python

    from msgpack import Copy

    plan = RewritePlan([
        Copy(("a",), ("b",)),  # original "a" -> "b"
        Copy(("b",), ("a",)),  # original "b" -> "a"
    ])

Streaming rewrite
-----------------

:class:`~msgpack.StreamRewriter` (or the :func:`~msgpack.rewrite_stream`
helper) processes a stream of back to back messages. Every message is
validated before anything is written: if a message is truncated or a path
cannot be resolved, that message is aborted without touching the destination,
while previously published events remain readable.

.. code-block:: python

    from io import BytesIO
    from msgpack import StreamRewriter, RewritePlan, Replace

    source = BytesIO(packb({"a": 1}) + packb({"a": 2}))
    destination = BytesIO()
    StreamRewriter(
        source,
        destination,
        RewritePlan([Replace(("a",), 0)]),
        window=65536,
    ).run()

The ``window`` argument fixes the I/O transfer size and the ``spill_size``
argument sets the threshold above which internal spools move to reclaimable
temporary files. Working memory grows with the nesting depth, the number of
planned paths and the window -- not with the message size. Changing the
window (and therefore the input chunk boundaries) never changes the result.

Container sizes and conflicts
-----------------------------

Deletions decrement the enclosing array or map size, and the affected headers
are re-emitted using the smallest legal MessagePack format (for example an
array16 header can collapse into a fixarray header).

The whole message is rejected with :class:`~msgpack.PlanError` or
:class:`~msgpack.PathError` before publication when:

* a path does not exist in the message;
* two instructions target the same node;
* a target or copy source runs through an ancestor that is itself replaced or
  deleted;
* a :class:`~msgpack.Copy` target contains another instruction target;
* the resulting container size would leave the valid MessagePack range.

API reference
-------------

.. autoclass:: Path
.. autoclass:: MapEntry
.. autoclass:: Replace
.. autoclass:: Delete
.. autoclass:: Copy
.. autoclass:: RewritePlan
.. autoclass:: StreamRewriter
    :members:
.. autofunction:: rewriteb
.. autofunction:: rewrite_stream
.. autoclass:: RewriteError
.. autoclass:: PathError
.. autoclass:: PlanError
.. autoclass:: TruncatedMessage
