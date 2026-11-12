Advanced usage
===============

Packer
------

autoreset
~~~~~~~~~

When you used ``autoreset=False`` option of :class:`~msgpack.Packer`, 
``pack()`` method doesn't return packed ``bytes``.

You can use :meth:`~msgpack.Packer.bytes` or :meth:`~msgpack.Packer.getbuffer` to
get packed data.

``bytes()`` returns ``bytes`` object.  ``getbuffer()`` returns some bytes-like
object.  It's concrete type is implement detail and it will be changed in future
versions.

You can reduce temporary bytes object by using ``Unpacker.getbuffer()``.

.. code-block:: python

    packer = Packer(use_bin_type=True, autoreset=False)

    packer.pack([1, 2])
    packer.pack([3, 4])

    with open('data.bin', 'wb') as f:
        f.write(packer.getbuffer())

    packer.reset()  # reset internal buffer

keep_packed_prefix
~~~~~~~~~~~~~~~~~~

The pure Python :class:`~msgpack.Packer` supports ``keep_packed_prefix=True``
together with ``autoreset=False``.  When a ``pack()`` call fails, the bytes of
the messages packed earlier are kept instead of clearing the whole buffer; only
the partial, failed message is discarded.  The packer stays usable afterwards.

Caller-owned payloads such as large binary columns are staged by reference while
a message is built, so a failed pack neither copies those bytes nor the batch
already accumulated.  Reentrant packing from inside the ``default`` callback is
rejected with ``RuntimeError``.

.. code-block:: python

    packer = Packer(autoreset=False, keep_packed_prefix=True)

    packer.pack(b"committed")
    try:
        packer.pack(object())  # unsupported type
    except TypeError:
        pass
    assert packer.bytes() == packb(b"committed")  # prefix survives
    packer.pack(b"next")  # packing continues

