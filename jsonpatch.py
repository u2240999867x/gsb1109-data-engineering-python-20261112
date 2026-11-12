# python-json-patch - An implementation of the JSON Patch format
# https://github.com/stefankoegl/python-json-patch
#
# Copyright (c) 2011 Stefan Kögl <stefan@skoegl.net>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#

""" Apply JSON-Patches (RFC 6902) """

import bisect
import collections
import copy
import functools
import json
from collections.abc import (Mapping, MutableMapping, MutableSequence,
                             Sequence)
from difflib import SequenceMatcher
from types import MappingProxyType

from jsonpointer import JsonPointer, JsonPointerException


_ST_ADD = 0
_ST_REMOVE = 1

# Up to which product of their lengths DiffBuilder matches up all equal items
# of two lists
_EXACT_MATCH_LIMIT = 500 ** 2


# The from_parts of pointer classes that don't override it, see path_join
_JSONPOINTER_FROM_PARTS = JsonPointer.from_parts.__func__


# Will be parsed by setup.py to determine package metadata
__author__ = 'Stefan Kögl <stefan@skoegl.net>'
__version__ = '1.47'
__website__ = 'https://github.com/stefankoegl/python-json-patch'
__license__ = 'BSD-3-Clause'


class JsonPatchException(Exception):
    """Base Json Patch exception"""

    def __init__(self, *args, **kwargs):
        """Initialize a new instance of a JsonPatchException

        :param op: The operation that failed. It is attached to the exception
                   so that callers can inspect (or modify) the failing patch
                   operation. This is either a :class:`PatchOperation`
                   instance or, if the operation could not be constructed,
                   the raw operation object.
        :type op: PatchOperation or dict or None
        """
        self.op = kwargs.pop('op', None)
        super().__init__(*args, **kwargs)


class InvalidJsonPatch(JsonPatchException):
    """ Raised if an invalid JSON Patch is created """


class JsonPatchConflict(JsonPatchException):
    """Raised if patch could not be applied due to conflict situation such as:
    - attempt to add object key when it already exists;
    - attempt to operate with nonexistence object key;
    - attempt to insert value to array at position beyond its size;
    - etc.
    """


class JsonPatchPathNotFound(JsonPatchConflict):
    """Raised if the location an operation has to act on does not exist.

    This is the case for

    - a ``remove`` or ``replace`` whose ``path`` refers to a non-existent
      object member or array element, and
    - a ``move`` or ``copy`` whose ``from`` refers to a non-existent object
      member or array element.

    It is a subclass of :class:`JsonPatchConflict`, so existing code that
    catches :class:`JsonPatchConflict` continues to work. It can be caught on
    its own to tell a missing location apart from other conflicts, for
    example to implement a ``remove`` that ignores members which are already
    absent (see the tutorial).
    """


class JsonPatchTestFailed(JsonPatchException, AssertionError):
    """ A Test operation failed """


class JsonPatchPermissionDenied(JsonPatchException):
    """Raised by the preflight of :class:`GuardedJsonPatch` when an operation
    touches a protected column without a grant for the required permission.

    The whole batch is checked before anything is written, so raising this
    leaves the target document untouched. The offending operation is attached
    like for the other exceptions, and :attr:`index` is its position in the
    batch (the first operation that has to be rejected).
    """

    def __init__(self, message, *, op, index, column, permission,
                 row_key=None):
        super().__init__(message, op=op)
        self.index = index
        self.column = column
        self.permission = permission
        self.row_key = row_key


def multidict(ordered_pairs):
    """Convert duplicate keys values to lists."""
    # read all values into lists
    mdict = collections.defaultdict(list)
    for key, value in ordered_pairs:
        mdict[key].append(value)

    return dict(
        # unpack lists that have only 1 item
        (key, values[0] if len(values) == 1 else values)
        for key, values in mdict.items()
    )


# The "object_pairs_hook" parameter is used to handle duplicate keys when
# loading a JSON object.
_jsonloads = functools.partial(json.loads, object_pairs_hook=multidict)


def apply_patch(doc, patch, in_place=False, pointer_cls=JsonPointer,
                atomic=False):
    """Apply patch operations to the specified JSON document.

    :param doc: JSON document, including arrays and scalar values.
    :type doc: object

    :param patch: Iterable of operation dicts or a raw JSON-encoded string.
    :type patch: iterable or str

    :param in_place: While ``True`` patch will modify target document.
                     By default patch will be applied to document copy.
    :type in_place: bool

    :param pointer_cls: JSON pointer class to use.
    :type pointer_cls: Type[JsonPointer]

    :param atomic: With ``in_place=True``, undo the changes of all preceding
                   operations if an operation fails, so that ``doc`` is
                   either fully patched or left unchanged. See
                   :meth:`JsonPatch.apply`.
    :type atomic: bool

    :return: Resulting document, which may have a different type from ``doc``.
    :rtype: object

    >>> doc = {'foo': 'bar'}
    >>> patch = [{'op': 'add', 'path': '/baz', 'value': 'qux'}]
    >>> other = apply_patch(doc, patch)
    >>> doc is not other
    True
    >>> other == {'foo': 'bar', 'baz': 'qux'}
    True
    >>> patch = [{'op': 'add', 'path': '/baz', 'value': 'qux'}]
    >>> apply_patch(doc, patch, in_place=True) == {'foo': 'bar', 'baz': 'qux'}
    True
    >>> doc == other
    True
    """

    if isinstance(patch, (str, bytes)):
        patch = JsonPatch.from_string(patch, pointer_cls=pointer_cls)
    else:
        patch = JsonPatch(patch, pointer_cls=pointer_cls)
    return patch.apply(doc, in_place, atomic=atomic)


def make_patch(src, dst, pointer_cls=JsonPointer, generate_test_ops=False):
    """Generates patch by comparing two document objects. Actually is
    a proxy to :meth:`JsonPatch.from_diff` method. The resulting patch
    transforms `src` into `dst`, so it has to be applied to `src` (or to a
    document equal to it).

    :param src: Source JSON document, including arrays and scalar values.
    :type src: object

    :param dst: Target JSON document, including arrays and scalar values.
    :type dst: object

    :param pointer_cls: JSON pointer class to use. The paths of the
                        created operations are written with
                        :func:`path_join`, so they use its syntax.
    :type pointer_cls: Type[JsonPointer]

    :param generate_test_ops: Precede each ``remove`` and ``replace``
                              operation by a ``test`` operation that checks
                              the value it removes or replaces, so the patch
                              fails on documents that differ from `src` there.
    :type generate_test_ops: bool

    >>> src = {'foo': 'bar', 'numbers': [1, 3, 4, 8]}
    >>> dst = {'baz': 'qux', 'numbers': [1, 4, 7]}
    >>> patch = make_patch(src, dst)
    >>> new = patch.apply(src)
    >>> new == dst
    True
    """

    return JsonPatch.from_diff(src, dst, pointer_cls=pointer_cls,
                               generate_test_ops=generate_test_ops)


def path_split(path, pointer_cls=JsonPointer):
    """Splits the ``path`` (or ``from``) of an operation into its parts.

    The parts are the unescaped reference tokens of the JSON Pointer: ``~1``
    becomes ``/`` and ``~0`` becomes ``~``. They are always strings, also
    those that refer to array elements, as only the document a path is
    applied to tells whether a part is an object key or an array index.

    This is the reverse of :func:`path_join`.

    :param path: The path, a JSON Pointer such as ``'/foo/0'``.
    :type path: str

    :param pointer_cls: JSON pointer class to parse the path with.
    :type pointer_cls: Type[JsonPointer]

    :return: The unescaped parts of the path; empty for the whole document
             (the path ``''``).
    :rtype: tuple[str, ...]

    :raises JsonPointerException: if ``path`` is not a valid JSON Pointer.

    >>> path_split('/foo/0')
    ('foo', '0')
    >>> path_split('/~1fields~1test/a~0b')
    ('/fields/test', 'a~b')
    >>> path_split('')
    ()
    """
    return tuple(pointer_cls(path).parts)


def path_join(parts, pointer_cls=JsonPointer):
    """Joins unescaped parts into a path, as used in the ``path`` (or
    ``from``) of an operation.

    Each part is converted to a string and escaped: ``~`` becomes ``~0`` and
    ``/`` becomes ``~1``. Integers can be used for array indices.

    This is the reverse of :func:`path_split`. :meth:`JsonPatch.from_diff`
    and :func:`make_patch` use it to write the paths of the operations they
    create, so these use the syntax of their ``pointer_cls``.

    If ``pointer_cls`` overrides ``JsonPointer.from_parts``,
    the path is ``pointer_cls.from_parts(parts).path``, so a pointer class
    with a different path syntax overrides ``from_parts`` to build pointers
    from parts. Otherwise the path is a JSON Pointer as defined by RFC 6901,
    and ``pointer_cls`` is not used, as ``from_parts`` of ``JsonPointer``
    always builds such a pointer.

    :param parts: The object keys and array indices that lead to the
                  location; empty for the whole document.
    :type parts: Iterable[str | int]

    :param pointer_cls: JSON pointer class to build the path with.
    :type pointer_cls: Type[JsonPointer]

    :return: The path, a JSON Pointer.
    :rtype: str

    >>> path_join(['foo', 0])
    '/foo/0'
    >>> path_join(['/fields/test', 'a~b'])
    '/~1fields~1test/a~0b'
    >>> path_join([])
    ''
    """
    from_parts = getattr(pointer_cls, 'from_parts', None)
    if from_parts is None or \
            getattr(from_parts, '__func__', None) is _JSONPOINTER_FROM_PARTS:
        # Also for subclasses that only change how paths are parsed, which
        # JsonPointer.from_parts would parse an RFC 6901 pointer with
        return _to_pointer(parts)

    return from_parts(parts).path


class _NullJournal:
    """Performs the changes of patch operations without recording them."""

    @staticmethod
    def setitem(container, key, value):
        container[key] = value

    @staticmethod
    def delitem(container, key):
        del container[key]

    @staticmethod
    def insert(container, index, value):
        container.insert(index, value)

    @staticmethod
    def append(container, value):
        container.append(value)


_NO_JOURNAL = _NullJournal()


class _Journal:
    """Performs the changes of patch operations and records how to undo them.

    Each undo step refers to the changed container itself rather than to a
    path. As :meth:`rollback` undoes the steps in reverse order, every
    container is in the same state as right after its change when the step is
    undone, so list indices still refer to the same items. Removed and
    replaced values are kept as they are; they are detached from the
    document, so restoring them needs no copy.

    Restoring a removed member adds it at the end of its mapping, so the
    order of the members of each dict that loses one is recorded once, and
    restored after all steps have been undone. This is limited to dicts
    (including subclasses such as OrderedDict or Counter), whose order is the
    order of insertion; moving a member of other mappings, such as a
    ChainMap, to the end can add it to a different underlying mapping.
    """

    def __init__(self):
        self._undo = []
        # id of a mapping -> (mapping, its keys before the first removal)
        self._orders = {}

    def setitem(self, container, key, value):
        if (isinstance(container, MutableMapping) and key not in container or
                # a ChainMap adds the members of its other mappings that are
                # set to its first one
                isinstance(container, collections.ChainMap) and
                key not in container.maps[0]):
            container[key] = value
            self._undo.append((_undo_delitem, container, key))
        else:
            old = container[key]
            container[key] = value
            self._undo.append((_undo_setitem, container, key, old))

    def delitem(self, container, key):
        # reading a missing member can create it, e.g. in a defaultdict
        if isinstance(container, Mapping) and key not in container:
            raise KeyError(key)
        old = container[key]
        if isinstance(container, MutableSequence):
            del container[key]
            self._undo.append((_undo_insert, container, key, old))
        elif isinstance(container, MutableMapping):
            if (isinstance(container, dict) and
                    id(container) not in self._orders):
                self._orders[id(container)] = (container, list(container))
            del container[key]
            self._undo.append((_undo_setitem, container, key, old))
        else:
            del container[key]
            self._undo.append((_undo_setitem, container, key, old))

    def insert(self, container, index, value):
        container.insert(index, value)
        self._undo.append((_undo_delitem, container, index))

    def append(self, container, value):
        if (isinstance(container, collections.deque) and
                container.maxlen is not None and
                len(container) == container.maxlen):
            # A full bounded deque drops its first item to make room
            if container.maxlen:
                dropped = container[0]
                container.append(value)
                self._undo.append((_undo_dropping_append, container, dropped))
            else:
                container.append(value)  # it stays empty
            return
        container.append(value)
        # the list may shrink again before this step is undone, but by then
        # it has the same length as right after the append
        self._undo.append((_undo_delitem, container, len(container) - 1))

    def rollback(self):
        """Undoes all recorded changes, the most recent one first."""
        while self._undo:
            step = self._undo.pop()
            step[0](*step[1:])
        for mapping, keys in self._orders.values():
            _reorder(mapping, keys)
        self._orders.clear()


def _undo_setitem(container, key, value):
    container[key] = value


def _undo_delitem(container, key):
    del container[key]


def _undo_insert(container, index, value):
    container.insert(index, value)


def _undo_dropping_append(deque, dropped):
    del deque[-1]
    deque.appendleft(dropped)


def _reorder(mapping, keys):
    """Restores the order of the members of a mapping after a rollback.

    ``keys`` are its keys before its first removal. They can include
    members that the patch added before, and that the rollback has removed
    again; the original members appear in it in their original order.
    """
    keys = [key for key in keys if key in mapping]
    start = next((index for index, (key, original)
                  in enumerate(zip(mapping, keys)) if key != original),
                 len(keys))
    # Move the members from the first misplaced one on to the end. Unlike
    # update(), which e.g. a Counter uses to add counts, this only uses the
    # operations the rollback itself uses
    for key in keys[start:]:
        mapping[key] = mapping.pop(key)


class PatchOperation:
    """A single operation inside a JSON Patch."""

    def __init__(self, operation, pointer_cls=JsonPointer):
        self.pointer_cls = pointer_cls

        if not operation.__contains__('path'):
            raise InvalidJsonPatch("Operation must have a 'path' member", op=operation)

        if isinstance(operation['path'], self.pointer_cls):
            self.location = operation['path'].path
            self.pointer = operation['path']
        else:
            self.location = operation['path']
            try:
                self.pointer = self.pointer_cls(self.location)
            except TypeError:
                raise InvalidJsonPatch("Invalid 'path'", op=operation)

        # Setters update operation members, even when the input is read-only.
        self.operation = operation if isinstance(operation, dict) else dict(operation)

    def _from_pointer(self):
        """Builds the pointer of the 'from' member like __init__ builds the
        one of 'path'."""
        try:
            from_ptr = self.operation['from']
        except KeyError:
            raise InvalidJsonPatch(
                "The operation does not contain a 'from' member", op=self)

        if isinstance(from_ptr, self.pointer_cls):
            return from_ptr

        try:
            return self.pointer_cls(from_ptr)
        except TypeError:
            raise InvalidJsonPatch("Invalid 'from'", op=self)

    def apply(self, obj):
        """Applies the patch operation to the specified object."""
        return self._apply(obj, _NO_JOURNAL)

    def _apply(self, obj, journal):
        """Applies the operation, making all changes through ``journal``, so
        that :meth:`JsonPatch.apply` can undo them with ``atomic=True``.

        Subclasses either implement this method or override :meth:`apply`.
        """
        raise NotImplementedError('should implement the patch operation.')

    def __hash__(self):
        # Other members can hold arrays or objects, which are unhashable.
        # Operations that compare equal still have equal 'op' and 'path'.
        return hash((self.operation.get('op'), self.operation['path']))

    def __eq__(self, other):
        if not isinstance(other, PatchOperation):
            return False
        # e.g. tests for 1 and for true behave differently
        return _json_equal(self.operation, other.operation)

    @property
    def path(self):
        """The location of the object or array that contains the target of
        the operation, i.e. its ``path`` without the last part, written in
        the syntax of ``pointer_cls`` (see :func:`path_join`).

        For example ``'/a/0'`` for the ``path`` ``'/a/0/b'``, and ``''`` for
        a target at the top level of the document. The last part is
        :attr:`key`.
        """
        return path_join(self.pointer.parts[:-1], self.pointer_cls)

    @property
    def key(self):
        """The last part of ``path``, as returned by :meth:`get_part`.
        Setting it changes the ``path`` member of the operation."""
        return self.get_part(-1)

    @key.setter
    def key(self, value):
        self.set_part(-1, value)

    def get_part(self, index):
        """Returns the part of ``path`` at ``index``, unescaped.

        The part is converted to an int if ``int()`` accepts it, and is a
        str otherwise. Whether it is an array index or an object key is not
        checked, as only the document tells, so numeric object keys are
        converted as well: ``'01'`` and ``'1_0'`` become ``1`` and ``10``.
        To get the exact keys, use :func:`path_split` or ``pointer.parts``.

        >>> op = RemoveOperation({'op': 'remove', 'path': '/a/0/01/b'})
        >>> [op.get_part(i) for i in range(4)]
        ['a', 0, 1, 'b']
        >>> path_split(op.location)
        ('a', '0', '01', 'b')
        """
        try:
            return int(self.pointer.parts[index])
        except ValueError:
            return self.pointer.parts[index]

    def set_part(self, index, value):
        self.pointer.parts[index] = str(value)
        self.location = self.pointer.path
        self.operation['path'] = self.location


class RemoveOperation(PatchOperation):
    """Removes an object property or an array element."""

    def _apply(self, obj, journal):
        subobj, part = self.pointer.to_last(obj)

        if part is None:
            raise JsonPatchConflict("can't remove the whole document", op=self)

        if isinstance(subobj, Sequence) and not isinstance(part, int):
            raise JsonPointerException("invalid array index '{0}'".format(part))

        try:
            journal.delitem(subobj, part)
        except (KeyError, IndexError):
            msg = "can't remove a non-existent object '{0}'".format(part)
            raise JsonPatchPathNotFound(msg, op=self)
        except TypeError as ex:
            raise JsonPatchConflict(str(ex), op=self)

        return obj


class AddOperation(PatchOperation):
    """Adds an object property or an array element."""

    def _apply(self, obj, journal):
        try:
            value = self.operation["value"]
        except KeyError:
            raise InvalidJsonPatch(
                "The operation does not contain a 'value' member", op=self)

        # Insert a copy so the document does not share mutable values with
        # the patch; otherwise later operations would modify the patch itself.
        value = copy.deepcopy(value)
        if journal is _NO_JOURNAL:
            # subclasses may override _add without the journal parameter
            return self._add(obj, value)
        return self._add(obj, value, journal)

    def _add(self, obj, value, journal=_NO_JOURNAL):
        subobj, part = self.pointer.to_last(obj)

        if part is None:
            return value  # we're replacing the root, whatever its type

        if isinstance(subobj, MutableSequence):
            if part == '-':
                journal.append(subobj, value)

            elif part > len(subobj) or part < 0:
                raise JsonPatchConflict("can't insert outside of list", op=self)

            else:
                journal.insert(subobj, part, value)

        elif isinstance(subobj, MutableMapping):
            journal.setitem(subobj, part, value)

        else:
            raise JsonPatchConflict("unable to fully resolve json pointer {0}, part {1}".format(self.location, part), op=self)
        return obj


class ReplaceOperation(PatchOperation):
    """Replaces an object property or an array element by a new value."""

    def _apply(self, obj, journal):
        try:
            value = self.operation["value"]
        except KeyError:
            raise InvalidJsonPatch(
                "The operation does not contain a 'value' member", op=self)

        # copied for the same reason as in AddOperation._apply
        value = copy.deepcopy(value)

        subobj, part = self.pointer.to_last(obj)

        if part is None:
            return value

        if isinstance(subobj, MutableSequence):
            # '-' only refers to the (nonexistent) element after the end of
            # an array; for an object it is an ordinary member name
            if part == "-":
                raise InvalidJsonPatch("'path' with '-' can't be applied to 'replace' operation", op=self)

            if part >= len(subobj) or part < 0:
                raise JsonPatchPathNotFound("can't replace outside of list", op=self)

        elif isinstance(subobj, MutableMapping):
            if part not in subobj:
                msg = "can't replace a non-existent object '{0}'".format(part)
                raise JsonPatchPathNotFound(msg, op=self)
        else:
            raise JsonPatchConflict("unable to fully resolve json pointer {0}, part {1}".format(self.location, part), op=self)

        journal.setitem(subobj, part, value)
        return obj


class MoveOperation(PatchOperation):
    """Moves an object property or an array element to a new location."""

    def _apply(self, obj, journal):
        from_ptr = self._from_pointer()

        # Checked before anything is resolved, as removing an array element
        # shifts its siblings, so the target would resolve to a different
        # location. This also covers moving the whole document ('from' is "").
        if self.pointer != from_ptr and self.pointer.contains(from_ptr):
            raise JsonPatchConflict('Cannot move values into their own children', op=self)

        subobj, part = _to_existing_last(from_ptr, obj)

        # Moving the whole document onto itself is a no-op
        if part is None:
            return obj

        try:
            value = subobj[part]
        except (KeyError, IndexError) as ex:
            raise JsonPatchPathNotFound(str(ex), op=self)

        # If source and target are equal, this is a no-op
        if self.pointer == from_ptr:
            return obj

        try:
            obj = RemoveOperation({
                'op': 'remove',
                'path': self.operation['from']
            }, pointer_cls=self.pointer_cls)._apply(obj, journal)

            # the value has been detached from its old location, so no copy
            # needed
            obj = AddOperation({
                'op': 'add',
                'path': self.location,
            }, pointer_cls=self.pointer_cls)._add(obj, value, journal)
        except JsonPatchException as ex:
            # report the failure against this operation rather than the
            # internal, synthetic remove / add operations
            ex.op = self
            raise

        return obj

    @property
    def from_path(self):
        """Like :attr:`PatchOperation.path`, but of the ``from`` member: the location of
        the object or array that contains the moved value."""
        from_ptr = self._from_pointer()
        return path_join(from_ptr.parts[:-1], self.pointer_cls)

    @property
    def from_key(self):
        """The last part of ``from``, as returned by
        :meth:`get_from_part`. Setting it changes the ``from`` member of the
        operation."""
        return self.get_from_part(-1)

    @from_key.setter
    def from_key(self, value):
        self.set_from_part(-1, value)

    def get_from_part(self, index):
        """Like :meth:`PatchOperation.get_part`, but returns the part of
        ``from`` at ``index``: also numeric object keys become ints."""
        from_ptr = self._from_pointer()
        try:
            return int(from_ptr.parts[index])
        except ValueError:
            return from_ptr.parts[index]

    def set_from_part(self, index, value):
        from_ptr = self._from_pointer()
        from_ptr.parts[index] = str(value)
        self.operation['from'] = from_ptr.path


class TestOperation(PatchOperation):
    """Test value by specified location."""

    def _apply(self, obj, journal):
        try:
            subobj, part = self.pointer.to_last(obj)
            if part is None:
                val = subobj
            else:
                val = self.pointer.walk(subobj, part)
        except JsonPointerException as ex:
            raise JsonPatchTestFailed(str(ex), op=self)

        try:
            value = self.operation['value']
        except KeyError:
            raise InvalidJsonPatch(
                "The operation does not contain a 'value' member", op=self)

        if not _json_equal(val, value):
            msg = '{0} ({1}) is not equal to tested value {2} ({3})'
            raise JsonPatchTestFailed(msg.format(val, type(val),
                                                 value, type(value)), op=self)

        return obj


class CopyOperation(PatchOperation):
    """ Copies an object property or an array element to a new location """

    def _apply(self, obj, journal):
        from_ptr = self._from_pointer()

        subobj, part = _to_existing_last(from_ptr, obj)
        try:
            value = copy.deepcopy(subobj if part is None else subobj[part])
        except (KeyError, IndexError) as ex:
            raise JsonPatchPathNotFound(str(ex), op=self)

        # value is already a deep copy, no need to copy it again
        try:
            obj = AddOperation({
                'op': 'add',
                'path': self.location,
            }, pointer_cls=self.pointer_cls)._add(obj, value, journal)
        except JsonPatchException as ex:
            # report the failure against this operation rather than the
            # internal, synthetic add operation
            ex.op = self
            raise

        return obj


class AppendOperation(PatchOperation):
    """Appends a string suffix to an existing string (not part of RFC 6902)."""

    def _apply(self, obj, journal):
        try:
            value = self.operation['value']
        except KeyError:
            raise InvalidJsonPatch("The operation does not contain a 'value' member", op=self)
        if not isinstance(value, str):
            raise JsonPatchConflict("Cannot append a non-string value", op=self)

        try:
            subobj, part = self.pointer.to_last(obj)
        except JsonPointerException as ex:
            raise JsonPatchPathNotFound(str(ex), op=self)

        if part is None:
            raise JsonPatchConflict("Cannot append to root document", op=self)
        if isinstance(subobj, MutableSequence):
            if not isinstance(part, int) or part < 0 or part >= len(subobj):
                raise JsonPatchPathNotFound("Cannot append outside of list", op=self)
        elif isinstance(subobj, MutableMapping):
            if part not in subobj:
                raise JsonPatchPathNotFound("Cannot append to a non-existent member", op=self)
        else:
            raise JsonPatchConflict("Cannot append to a non-string value", op=self)

        if not isinstance(subobj[part], str):
            raise JsonPatchConflict("Cannot append to a non-string value", op=self)
        journal.setitem(subobj, part, subobj[part] + value)
        return obj


def _restore(obj, saved):
    """Restores the contents of a document from a copy of it.

    Only the items of mappings and sequences are restored, not other state
    that a custom operation can change, such as attributes or the list of
    mappings of a ChainMap.
    """
    if isinstance(obj, collections.ChainMap):
        # restore each mapping, so that none takes over another's members
        for mapping, saved_mapping in zip(obj.maps, saved.maps):
            _restore(mapping, saved_mapping)
    elif isinstance(obj, MutableMapping):
        obj.clear()
        obj.update(saved)
    elif isinstance(obj, MutableSequence):
        # not obj[:] = saved, as e.g. deque doesn't support slices
        obj.clear()
        obj.extend(saved)
    # Immutable containers can't have changed, but their children can
    elif isinstance(obj, Mapping):
        for key in saved:
            _restore(obj[key], saved[key])
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for item, saved_item in zip(obj, saved):
            _restore(item, saved_item)


_JOURNALED_OPERATIONS = (
    RemoveOperation, AddOperation, ReplaceOperation, MoveOperation,
    TestOperation, CopyOperation,
)


def _supports_journal(operation):
    """Whether all changes of ``operation`` go through its journal, which is
    the case for the built-in operations unless a subclass, or the operation
    object itself, changes how they are applied."""
    overridden = getattr(operation, '__dict__', {})
    if any(name in overridden for name in ('apply', '_apply', '_add')):
        return False
    cls = type(operation)
    if getattr(cls, 'apply', None) is not PatchOperation.apply:
        return False
    if not any(getattr(cls, '_apply', None) is base._apply
               for base in _JOURNALED_OPERATIONS):
        return False
    if isinstance(operation, AddOperation) and cls._add is not AddOperation._add:
        return False
    return True


class JsonPatch:
    """A JSON Patch is an iterable of Patch Operations.

    Accepts sequences, iterators, generators, and other iterables of operation
    dictionaries. Non-sequence inputs are materialized before validation so
    the resulting patch can be applied repeatedly.

    >>> patch = JsonPatch([
    ...     {'op': 'add', 'path': '/foo', 'value': 'bar'},
    ...     {'op': 'add', 'path': '/baz', 'value': [1, 2, 3]},
    ...     {'op': 'remove', 'path': '/baz/1'},
    ...     {'op': 'test', 'path': '/baz', 'value': [1, 3]},
    ...     {'op': 'replace', 'path': '/baz/0', 'value': 42},
    ...     {'op': 'remove', 'path': '/baz/1'},
    ... ])
    >>> doc = {}
    >>> result = patch.apply(doc)
    >>> expected = {'foo': 'bar', 'baz': [42]}
    >>> result == expected
    True

    JsonPatch object is iterable, so you can easily access each patch
    statement in a loop:

    >>> lpatch = list(patch)
    >>> expected = {'op': 'add', 'path': '/foo', 'value': 'bar'}
    >>> lpatch[0] == expected
    True
    >>> lpatch == patch.patch
    True

    Also JsonPatch could be converted directly to :class:`bool` if it contains
    any operation statements:

    >>> bool(patch)
    True
    >>> bool(JsonPatch([]))
    False

    This behavior is very handy with :func:`make_patch` to write more readable
    code:

    >>> old = {'foo': 'bar', 'numbers': [1, 3, 4, 8]}
    >>> new = {'baz': 'qux', 'numbers': [1, 4, 7]}
    >>> patch = make_patch(old, new)
    >>> if patch:
    ...     # document have changed, do something useful
    ...     patch.apply(old)    #doctest: +ELLIPSIS
    {...}
    """
    json_dumper = staticmethod(json.dumps)
    json_loader = staticmethod(_jsonloads)

    operations = MappingProxyType({
        'remove': RemoveOperation,
        'add': AddOperation,
        'replace': ReplaceOperation,
        'move': MoveOperation,
        'test': TestOperation,
        'copy': CopyOperation,
    })

    def __init__(self, patch, pointer_cls=JsonPointer):
        self.pointer_cls = pointer_cls

        if isinstance(patch, (str, bytes, Mapping)):
            raise InvalidJsonPatch("Document is expected to be sequence of operations")
        try:
            self.patch = list(patch)
        except TypeError:
            raise InvalidJsonPatch("Document is expected to be sequence of operations")

        # Verify that the structure of the patch document
        # is correct by retrieving each patch element.
        # Much of the validation is done in the initializer
        # though some is delayed until the patch is applied.
        for index, op in enumerate(self.patch):
            # We're only checking for strings in the following check
            # for two reasons:
            #
            # - It should come from JSON, which only allows strings as
            #   dictionary keys, so having a string here unambiguously means
            #   someone used: {"op": ..., ...} instead of [{"op": ..., ...}].
            #
            # - There's no possible false positive: if someone give a sequence
            #   of mappings, this won't raise.
            if isinstance(op, (str, bytes)):
                raise InvalidJsonPatch("Document is expected to be sequence of "
                                       "operations, got a sequence of strings.",
                                       op=op)

            self._get_operation(op)
            # Keep validation errors tied to the original input, but expose
            # concrete, JSON-serializable operation dictionaries on success.
            if not isinstance(op, dict):
                self.patch[index] = dict(op)

    def __str__(self):
        """str(self) -> self.to_string()"""
        return self.to_string()

    def __bool__(self):
        return bool(self.patch)

    def __iter__(self):
        return iter(self.patch)

    def __hash__(self):
        return hash(tuple(self._ops))

    def __eq__(self, other):
        if not isinstance(other, JsonPatch):
            return False
        return self._ops == other._ops

    @classmethod
    def from_string(cls, patch_str, loads=None, pointer_cls=JsonPointer):
        """Creates JsonPatch instance from string source.

        :param patch_str: JSON patch as raw string.
        :type patch_str: str

        :param loads: A function of one argument that loads a serialized
                      JSON string.
        :type loads: Callable

        :param pointer_cls: JSON pointer class to use.
        :type pointer_cls: Type[JsonPointer]

        :return: :class:`JsonPatch` instance.
        """
        json_loader = loads or cls.json_loader
        patch = json_loader(patch_str)
        return cls(patch, pointer_cls=pointer_cls)

    @classmethod
    def from_diff(
            cls, src, dst, optimization=True, dumps=None,
            pointer_cls=JsonPointer, generate_test_ops=False,
    ):
        """Creates JsonPatch instance based on comparison of two document
        objects. Json patch would be created for `src` argument against `dst`
        one. The resulting patch transforms `src` into `dst`, so it has to be
        applied to `src` (or to a document equal to it).

        Values in the patch are copied, so changing either input document
        afterwards does not change the generated patch.

        :param src: Source JSON document, including arrays and scalar values.
        :type src: object

        :param dst: Target JSON document, including arrays and scalar values.
        :type dst: object

        :param optimization: Generate move operations for matching values.
                             Set to False to use only add, remove and replace.
        :type optimization: bool

        :param dumps: A function of one argument that produces a serialized
                      JSON string.
        :type dumps: Callable

        :param pointer_cls: JSON pointer class to use. The paths of the
                            created operations are written with
                            :func:`path_join`, so they use its syntax.
        :type pointer_cls: Type[JsonPointer]

        :param generate_test_ops: Precede each ``remove`` and ``replace``
                                  operation by a ``test`` operation that checks
                                  the value it removes or replaces, so the patch
                                  fails on documents that differ from `src` there.
        :type generate_test_ops: bool

        :return: :class:`JsonPatch` instance.

        >>> src = {'foo': 'bar', 'numbers': [1, 3, 4, 8]}
        >>> dst = {'baz': 'qux', 'numbers': [1, 4, 7]}
        >>> patch = JsonPatch.from_diff(src, dst)
        >>> new = patch.apply(src)
        >>> new == dst
        True
        """
        json_dumper = dumps or cls.json_dumper
        builder = DiffBuilder(src, dst, json_dumper, pointer_cls=pointer_cls,
                              optimization=optimization,
                              generate_test_ops=generate_test_ops)
        builder._compare_values((), None, src, dst)
        ops = copy.deepcopy(list(builder.execute()))
        return cls(ops, pointer_cls=pointer_cls)

    def to_string(self, dumps=None):
        """Returns patch set as JSON string."""
        json_dumper = dumps or self.json_dumper
        return json_dumper(self.patch)

    @property
    def _ops(self):
        return tuple(map(self._get_operation, self.patch))

    def apply(self, obj, in_place=False, atomic=False):
        """Applies the patch to a given object.

        :param obj: JSON document, including arrays and scalar values.
        :type obj: object

        :param in_place: Tweaks the way how patch would be applied - directly to
                         specified `obj` or to its copy.
        :type in_place: bool

        :param atomic: With ``in_place=True``, undo the changes of all
                       preceding operations if an operation fails, so that
                       `obj` is either fully patched or left unchanged. This
                       does not copy the document; it only keeps the values
                       that the patch removes or replaces until it is
                       applied. Patches with custom operations, whose
                       changes can't be undone, copy the document instead.
                       Exact restoration is guaranteed for documents of
                       plain dicts and lists; members that containers such
                       as ``defaultdict`` add when they are only read are
                       not removed again (see the tutorial). Without
                       ``in_place=True``, `obj` is never changed, so this
                       has no effect.
        :type atomic: bool

        :return: Resulting document. Use this return value even with
                 ``in_place=True``, because a root replacement returns a new
                 value rather than modifying the original container.
        """

        if not in_place:
            obj = copy.deepcopy(obj)
            atomic = False

        ops = self._ops

        if not atomic:
            for operation in ops:
                obj = operation.apply(obj)
            return obj

        if not all(map(_supports_journal, ops)):
            # The changes of custom operations can't be undone, so restore
            # the document from a copy instead
            saved = copy.deepcopy(obj)
            original = obj
            try:
                for operation in ops:
                    obj = operation.apply(obj)
            except BaseException:
                _restore(original, saved)
                raise
            return obj

        journal = _Journal()
        try:
            for operation in ops:
                obj = operation._apply(obj, journal)
        except BaseException:
            journal.rollback()
            raise

        return obj

    def _get_operation(self, operation):
        if not isinstance(operation, Mapping):
            raise InvalidJsonPatch("Operation must be an object", op=operation)

        if 'op' not in operation:
            raise InvalidJsonPatch("Operation does not contain 'op' member", op=operation)

        op = operation['op']

        if not isinstance(op, (str, bytes)):
            raise InvalidJsonPatch("Operation's op must be a string", op=operation)

        if op not in self.operations:
            raise InvalidJsonPatch("Unknown operation {0!r}".format(op), op=operation)

        cls = self.operations[op]
        return cls(operation, pointer_cls=self.pointer_cls)


class JsonPatchWithAppend(JsonPatch):
    """A JsonPatch with an optional, non-RFC string append operation.

    Each append operation requires full ``op``, ``path`` and ``value`` members.
    It appends a string suffix to an existing object member or array element.
    The standard JsonPatch class and module-level helpers remain RFC 6902 only.
    """

    operations = MappingProxyType(dict(JsonPatch.operations, append=AppendOperation))

    @classmethod
    def from_diff(cls, src, dst, optimization=True, dumps=None,
                  pointer_cls=JsonPointer, generate_test_ops=False):
        """Creates a patch, using append for string growth in object members.

        Root strings and scalar array elements retain standard diff behavior.
        Options follow JsonPatch.from_diff; optimization=False disables moves,
        while append remains enabled. With generate_test_ops=True, an append
        is preceded by a test of the original string.
        """
        json_dumper = dumps or cls.json_dumper
        builder = _AppendDiffBuilder(src, dst, json_dumper, pointer_cls=pointer_cls,
                                     optimization=optimization,
                                     generate_test_ops=generate_test_ops)
        builder._compare_values((), None, src, dst)
        ops = copy.deepcopy(list(builder.execute()))
        return cls(ops, pointer_cls=pointer_cls)


class DiffBuilder:

    def __init__(self, src_doc, dst_doc, dumps=json.dumps, pointer_cls=JsonPointer,
                 optimization=True, generate_test_ops=False):
        self.dumps = dumps
        self.pointer_cls = pointer_cls
        self.optimization = optimization
        self.generate_test_ops = generate_test_ops
        # the index of the test operation preceding a remove operation, by
        # the id of the remove operation
        self.remove_tests = {}
        self.index_storage = [{}, {}]
        self.__root = root = []
        self.src_doc = src_doc
        self.dst_doc = dst_doc
        # the _ArrayItems of each array, by the location of the array
        self.arrays = {}
        root[:] = [root, root, None]

    def _move_key(self, value):
        """ A key of value, which is equal for values that are equal in JSON,
        so that e.g. [1] is not moved where [true] is added """
        try:
            # Serialized like in _differing_runs
            return self.dumps(_sorted_members(value))
        except (TypeError, ValueError, RecursionError):
            # Values that cannot be serialized, e.g. as they contain
            # themselves, are only moved where they are added unchanged
            return id(value)

    def store_index(self, value, index, st):
        if not self.optimization:
            return
        storage = self.index_storage[st]
        storage.setdefault(self._move_key(value), []).append(index)

    def take_index(self, value, st):
        if not self.optimization:
            return None
        stored = self.index_storage[st].get(self._move_key(value))
        if stored:
            return stored.pop()

    def insert(self, op):
        root = self.__root
        last = root[0]
        last[1] = root[0] = [last, root, op]
        return root[0]

    def remove(self, index):
        link_prev, link_next, _ = index
        link_prev[1] = link_next
        link_next[0] = link_prev
        index[:] = []

    def __iter__(self):
        root = self.__root
        curr = root[1]
        while curr is not root:
            yield curr[2]
            curr = curr[1]

    def execute(self):
        operations = list(self._replay())
        i = 0
        while i < len(operations):
            if i + 1 < len(operations):
                op_first, op_second = operations[i], operations[i + 1]
                if op_first['path'] == op_second['path'] and \
                        op_first['op'] == 'remove' and \
                        op_second['op'] == 'add':
                    yield {
                        'op': 'replace',
                        'path': path_join(op_second['path'],
                                          self.pointer_cls),
                        'value': op_second['value'],
                    }
                    i += 2
                    continue

            operation = operations[i]
            for member in ('from', 'path'):
                if member in operation:
                    operation[member] = path_join(operation[member],
                                                  self.pointer_cls)
            yield operation
            i += 1

    def _replay(self):
        """ Applies the operations to the arrays of the source document, and
        returns them with the array indices at which they apply """
        for items in self.arrays.values():
            items.reset()

        for op in self:
            operation = dict(op)
            # 'from' is removed before 'path' is added
            if 'from' in op:
                operation['from'] = self._parts(op['from'])
                self._set_present(op['from'], False)

            operation['path'] = self._parts(op['path'])
            if op['op'] == 'remove':
                self._set_present(op['path'], False)
            elif op['op'] in ('add', 'move'):
                self._set_present(op['path'], True)

            yield operation

    def _array_items(self, location):
        """ The _ArrayItems of the array at location """
        items = self.arrays.get(location)
        if items is None:
            items = self.arrays[location] = _ArrayItems()
        return items

    def _location(self, parts):
        """ The location of parts in the current document """
        location = ()
        for part in parts:
            if isinstance(part, int):
                part = self._array_items(location).at(part)
            location += (part,)
        return location

    def _new_location(self, path, key):
        """ The location of an item inserted at key of the container at path
        of the current document """
        location = self._location(path)
        if isinstance(key, int):
            key = self._array_items(location).insert(key)
        return _path_join(location, key)

    def _parts(self, location):
        """ The parts of location in the current document. If location is an
        array item that is not in the array, its index is where it would be
        if it was. """
        return tuple(part.index() if isinstance(part, _ArrayItem) else part
                     for part in location)

    def _set_present(self, location, present):
        """ Adds the array item at location to the current document, or
        removes it, if not present """
        item = location[-1] if location else None
        if isinstance(item, _ArrayItem):
            if present:
                item.array.add(item)
            else:
                item.array.remove(item)

    def _item_added(self, path, key, item):
        target = _path_join(path, key)
        index = self.take_index(item, _ST_REMOVE)
        if index is not None:
            removed = index[2]['path']
            # where the removed item is if it is not removed
            source = self._parts(removed)
            # RFC 6902 does not allow moving a value into its own children
            if not _is_inside(target, source):
                # the value is moved rather than removed, so it is not tested
                test_index = self.remove_tests.pop(id(index[2]), None)
                if test_index is not None:
                    self.remove(test_index)
                self.remove(index)
                if source != target:
                    self.insert({'op': 'move', 'from': removed,
                                 'path': self._new_location(path, key)})
                else:
                    # the removed item stays where the added one would be
                    self._set_present(removed, True)
                return

        new_index = self.insert({'op': 'add',
                                 'path': self._new_location(path, key),
                                 'value': item})
        self.store_index(item, new_index, _ST_ADD)

    def _item_removed(self, path, key, item):
        source = _path_join(path, key)
        location = self._location(source)
        index = self.take_index(item, _ST_ADD)
        if index is not None:
            added_location = index[2]['path']
            added = self._parts(added_location)
            # The documents are compared in order, so the added item is in
            # the part compared already: it is in front of the removed one,
            # and neither inside it nor moved by removing it
            self.remove(index)
            if _without_item(source, added) != added:
                self._set_present(location, False)
                self.insert({'op': 'move', 'from': location,
                             'path': added_location})
            else:
                # the removed item stays where the added one would be
                self._set_present(added_location, False)
            return

        test_index = self._insert_test(location, item)
        self._set_present(location, False)
        new_index = self.insert({'op': 'remove', 'path': location})
        if test_index is not None:
            self.remove_tests[id(new_index[2])] = test_index
        self.store_index(item, new_index, _ST_REMOVE)

    def _insert_test(self, location, value):
        """ Inserts a test operation that checks the value at location, if
        test operations are generated """
        if self.generate_test_ops:
            # like all values, it is copied when the patch is created
            return self.insert({'op': 'test', 'path': location,
                                'value': value})

    def _item_replaced(self, path, key, item, old_item):
        location = self._location(_path_join(path, key))
        self._insert_test(location, old_item)
        self.insert({
            'op': 'replace',
            'path': location,
            'value': item,
        })

    def _compare_dicts(self, path, src, dst):
        added_keys = [key for key in dst if key not in src]
        removed_keys = [key for key in src if key not in dst]
        intersection = [key for key in src if key in dst]

        for key in removed_keys:
            self._item_removed(path, str(key), src[key])

        for key in added_keys:
            self._item_added(path, str(key), dst[key])

        for key in intersection:
            self._compare_values(path, str(key), src[key], dst[key])

    def _differing_runs(self, src, dst):
        """ Returns (i1, i2, j1, j2) for each run of items src[i1:i2] that is
        replaced by dst[j1:j2], between runs of items that both lists have.

        Without matching up the items they both have, inserting an item in
        front of arrays or objects would change all of them that follow. """
        try:
            # Serialized like in _compare_values, so e.g. 1 and True differ,
            # but independent of the order of object members
            src_keys = [self.dumps(_sorted_members(item)) for item in src]
            dst_keys = [self.dumps(_sorted_members(item)) for item in dst]
        except (TypeError, ValueError, RecursionError):
            # Items that cannot be serialized are compared by position
            return [(0, len(src), 0, len(dst))]

        # Only what is between the common start and end is matched up
        shorter = min(len(src), len(dst))
        start = 0
        while start < shorter and src_keys[start] == dst_keys[start]:
            start += 1
        common_end = 0
        while common_end < shorter - start and \
                src_keys[-1 - common_end] == dst_keys[-1 - common_end]:
            common_end += 1
        src_end, dst_end = len(src) - common_end, len(dst) - common_end

        # How many operations replacing src[i1:i2] by dst[j1:j2] takes
        def cost(i1, i2, j1, j2):
            return abs((i2 - i1) - (j2 - j1)) + sum(
                _positional_cost(src[i], dst[j])
                for i, j in zip(range(i1, i2), range(j1, j2))
                if src_keys[i] != dst_keys[j])

        by_position = (start, src_end, start, dst_end)
        position_cost = cost(*by_position)

        # Matching up all items can take time proportional to the product of
        # the list lengths. For longer lists SequenceMatcher does not match
        # up items that are frequent in them, unless next to other matches
        exact = (src_end - start) * (dst_end - start) <= _EXACT_MATCH_LIMIT
        if not exact:
            # It still compares each item with the equal items of the other
            # list that are not frequent, which takes quadratic time for long
            # lists of repeated items. Comparing by position takes quadratic
            # time in its operations as well, as the diff turns them into
            # moves, so matching up may only take about as long
            counts = collections.Counter(dst_keys[start:dst_end])
            comparisons = sum(counts[key] for key in src_keys[start:src_end])
            if comparisons > max(_EXACT_MATCH_LIMIT, position_cost ** 2):
                return [by_position]

        matcher = SequenceMatcher(None, src_keys[start:src_end],
                                  dst_keys[start:dst_end], autojunk=not exact)
        runs = [(start + i1, start + i2, start + j1, start + j2)
                for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                if tag != 'equal']

        # Items can be equal by chance, and matching them up can take more
        # operations than comparing by position: for [2, 3, 5] and [3, 3, 4]
        # it removes 2, replaces 5 and adds 4 instead of replacing 2 and 5.
        # It can also pair up arrays or objects that differ in more members.
        if sum(cost(*run) for run in runs) > position_cost:
            return [by_position]

        return runs

    def _compare_lists(self, path, src, dst):
        # The list has become dst[:j1] + src[i1:] when a run is compared, so
        # src[i1] is at index j1
        for i1, i2, j1, j2 in self._differing_runs(src, dst):
            paired = min(i2 - i1, j2 - j1)
            for key in range(j1, j1 + paired):
                old, new = src[i1 + key - j1], dst[key]
                if isinstance(old, MutableMapping) and \
                        isinstance(new, MutableMapping):
                    self._compare_dicts(_path_join(path, key), old, new)

                elif isinstance(old, MutableSequence) and \
                        isinstance(new, MutableSequence):
                    self._compare_lists(_path_join(path, key), old, new)

                # To ensure we catch changes to JSON, we can't rely on a
                # simple old == new, because it would not recognize the
                # difference between 1 and True, among other things.
                elif self.dumps(old) == self.dumps(new):
                    continue

                else:
                    self._item_removed(path, key, old)
                    self._item_added(path, key, new)

            for old in src[i1 + paired:i2]:
                self._item_removed(path, j1 + paired, old)

            for key in range(j1 + paired, j2):
                self._item_added(path, key, dst[key])

    def _compare_values(self, path, key, src, dst):
        if isinstance(src, MutableMapping) and \
                isinstance(dst, MutableMapping):
            self._compare_dicts(_path_join(path, key), src, dst)

        elif isinstance(src, MutableSequence) and \
                isinstance(dst, MutableSequence):
            self._compare_lists(_path_join(path, key), src, dst)

        # To ensure we catch changes to JSON, we can't rely on a simple
        # src == dst, because it would not recognize the difference between
        # 1 and True, among other things. Using json.dumps is the most
        # fool-proof way to ensure we catch type changes that matter to JSON
        # and ignore those that don't. The performance of this could be
        # improved by doing more direct type checks, but we'd need to be
        # careful to accept type changes that don't matter when JSONified.
        elif self.dumps(src) == self.dumps(dst):
            return

        else:
            self._item_replaced(path, key, dst, src)


class _AppendDiffBuilder(DiffBuilder):

    def _item_replaced(self, path, key, item, old_item):
        if (path or key is not None) and isinstance(old_item, str) and \
                isinstance(item, str) and len(item) > len(old_item) and \
                item.startswith(old_item):
            location = self._location(_path_join(path, key))
            self._insert_test(location, old_item)
            self.insert({'op': 'append', 'path': location,
                         'value': item[len(old_item):]})
        else:
            super()._item_replaced(path, key, item, old_item)


def _positional_cost(old, new):
    """ Estimates how many operations change old into new, if the items of
    arrays are compared by position """
    if old == new:
        return 0
    if isinstance(old, MutableMapping) and isinstance(new, MutableMapping):
        return sum(_positional_cost(old[key], new[key]) if key in new else 1
                   for key in old) + sum(key not in old for key in new)
    if isinstance(old, MutableSequence) and isinstance(new, MutableSequence):
        return abs(len(old) - len(new)) + sum(
            _positional_cost(*items) for items in zip(old, new))
    return 1


def _json_equal(first, second):
    """ Compares values the way RFC 6902, 4.6 does: numbers by value, but
    literals like true only with themselves, which Python considers equal to
    1, and arrays and objects member by member """
    if isinstance(first, bool) or isinstance(second, bool):
        return isinstance(first, bool) and isinstance(second, bool) and \
            first == second
    if isinstance(first, Mapping) and isinstance(second, Mapping):
        return len(first) == len(second) and all(
            key in second and _json_equal(first[key], second[key])
            for key in first)
    if isinstance(first, Sequence) and isinstance(second, Sequence) and \
            not isinstance(first, (str, bytes)) and \
            not isinstance(second, (str, bytes)):
        return len(first) == len(second) and all(
            _json_equal(*items) for items in zip(first, second))
    return first == second


def _sorted_members(value):
    """ Copies the arrays and objects in value, with the members of objects
    sorted by key """
    # Most values are scalars, so they are checked for first
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, MutableMapping):
        return {key: _sorted_members(value[key]) for key in sorted(value)}
    if isinstance(value, MutableSequence):
        return [_sorted_members(item) for item in value]
    return value


# The DiffBuilder works with parts, which are tuples of object keys (str) and
# array indices (int), and with locations, in which _ArrayItem objects take
# the place of array indices. Operations keep locations: when a move replaces
# an earlier 'add' or 'remove', the array indices in the operations after it
# change, but their locations do not.


class _ArrayItem:
    """ An item that an array has at some point of the diff """

    __slots__ = ('array', 'label', 'initial')

    def __init__(self, array, label, initial):
        self.array = array
        self.label = label
        # if the item is in the array of the source document
        self.initial = initial

    def index(self):
        """ The index of the item in the array, or the index it would have if
        it was in the array """
        return bisect.bisect_left(self.array.present_labels, self.label)


class _ArrayItems:
    """ All items that an array has at some point of the diff, and the items
    that it has at the moment, both ordered by their labels.

    The array has its items in this order at every point of the diff, also
    when a move later replaces the 'add' or 'remove' of an item. So an item
    inserted at an index is put directly after the item before it in the
    array: inserting at the index of a removed item inserts before it, if a
    move replaces its 'remove' later. """

    # how far apart the labels of items inserted one after the other are
    step = 1 << 16
    # how far apart labels are at the start and at the ends
    gap = 1 << 32

    def __init__(self):
        self.items = []
        self.labels = []
        self.present = []
        self.present_labels = []

    def at(self, index):
        """ The item at index of the array """
        # the items of the source document follow the items that the diff
        # has dealt with, so they are only created when needed
        while len(self.present) <= index:
            label = self.labels[-1] + self.gap if self.labels else 0
            item = _ArrayItem(self, label, True)
            self.items.append(item)
            self.labels.append(label)
            self.present.append(item)
            self.present_labels.append(label)

        return self.present[index]

    def insert(self, index):
        """ Inserts a new item at index of the array, and returns it """
        position = 0
        if index:
            position = bisect.bisect_right(self.labels,
                                           self.at(index - 1).label)

        if position == 0 or position == len(self.labels):
            if not self.labels:
                label = 0
            elif position == 0:
                label = self.labels[0] - self.gap
            else:
                label = self.labels[-1] + self.gap

        else:
            if self.labels[position] - self.labels[position - 1] < 2:
                self._relabel()
            low, high = self.labels[position - 1], self.labels[position]
            label = low + min(self.step, (high - low) // 2)

        item = _ArrayItem(self, label, False)
        self.items.insert(position, item)
        self.labels.insert(position, label)
        self.add(item)
        return item

    def _relabel(self):
        self.labels = [i * self.gap for i in range(len(self.items))]
        for item, label in zip(self.items, self.labels):
            item.label = label
        self.present_labels = [item.label for item in self.present]

    def add(self, item):
        """ Adds item to the array """
        position = bisect.bisect_left(self.present_labels, item.label)
        self.present.insert(position, item)
        self.present_labels.insert(position, item.label)

    def remove(self, item):
        """ Removes item from the array """
        position = bisect.bisect_left(self.present_labels, item.label)
        del self.present[position]
        del self.present_labels[position]

    def reset(self):
        """ Changes the array back to its items in the source document """
        self.present = [item for item in self.items if item.initial]
        self.present_labels = [item.label for item in self.present]


def _path_join(path, key):
    if key is None:
        return path

    return path + (key,)


def _to_pointer(path):
    """ Fast path of path_join for JsonPointer """
    return ''.join('/' + str(part).replace('~', '~0').replace('/', '~1')
                   for part in path)


def _is_prefix(sub_parts, parts):
    return sub_parts == parts[:len(sub_parts)]


def _is_inside(parts, container):
    return len(parts) > len(container) and _is_prefix(container, parts)


def _shift(parts, depth, offset):
    return parts[:depth] + (parts[depth] + offset,) + parts[depth + 1:]


def _without_item(parts, location):
    """ Changes parts, which assumes that there is an item at location, to
    the item missing """
    depth = len(location) - 1
    if isinstance(location[-1], int) and _is_inside(parts, location[:-1]) \
            and parts[depth] > location[-1]:
        return _shift(parts, depth, -1)

    return parts


def _to_existing_last(pointer, doc):
    """Resolve pointer like JsonPointer.to_last, for a location that has to
    exist.

    The '-' of an array refers to the nonexistent element after its last one.
    """
    subobj, part = pointer.to_last(doc)

    if part == '-' and isinstance(subobj, Sequence):
        raise JsonPointerException("invalid array index '{0}'".format(part))

    return subobj, part


# Permissions checked by the guarded patch preflight. Reading a value
# (test/copy/from), changing a column and reordering array rows are granted
# separately, so a grant for one of them never implies another.
PERMISSION_READ = 'read'
PERMISSION_WRITE = 'write'
PERMISSION_REORDER = 'reorder'

_PERMISSIONS = (PERMISSION_READ, PERMISSION_WRITE, PERMISSION_REORDER)

# A row key that could not be resolved, e.g. because the location is not an
# array element or the element has no row key member. Distinct from None,
# which is a valid JSON value and therefore a valid row key.
_UNRESOLVED = object()

# A value that does not exist at the location a pointer resolves to.
_MISSING = object()

# Stands for every business row, used when a grant does not name rows.
_ALL_ROWS = object()


class ColumnPermissions:
    """The protected columns and the grants of a guarded JSON Patch.

    A *column* is the location of a value, given as a JSON Pointer such as
    ``'/rows/0/ssn'``. A column is protected together with all columns below
    it. Permissions are granted per column and per business row; the business
    row of an array element is the value of its ``row_key`` member (``'id'``
    by default).

    The three permissions are independent:

    * ``'read'`` is needed to read a value, namely for ``test`` and for the
      ``from`` of ``move`` and ``copy`` (including every protected column the
      read value contains);
    * ``'write'`` is needed to add, remove, replace or move a value at a
      protected column, including the protected columns overwritten by a
      whole-object replacement;
    * ``'reorder'`` is needed to insert into or remove from an array, which
      shifts the positions of its rows.

    A grant on a column also covers the columns below it. Grants whose rows
    are not restricted (``rows`` omitted) apply to every business row;
    otherwise they apply only to the named row keys.

    :param protected: Column paths that may only be touched with a grant.
    :type protected: Iterable[str | JsonPointer]

    :param grants: Grant rules, each a mapping with the keys ``'permission'``,
                   ``'column'`` and optionally ``'rows'``.
    :type grants: Iterable[Mapping]

    :param row_key: The object member naming the business row key of an array
                    element.
    :type row_key: str

    :param pointer_cls: JSON pointer class used to parse column paths.
    :type pointer_cls: Type[JsonPointer]
    """

    READ = PERMISSION_READ
    WRITE = PERMISSION_WRITE
    REORDER = PERMISSION_REORDER

    def __init__(self, protected=(), grants=(), row_key='id',
                 pointer_cls=JsonPointer):
        self.row_key = row_key
        self.pointer_cls = pointer_cls
        # Sorted so the first protected column a denial reports is
        # deterministic, following document (pointer) order.
        self._protected = tuple(sorted(
            self._parts(column) for column in protected))
        self._grants = {permission: [] for permission in _PERMISSIONS}
        for rule in grants or ():
            self.grant(
                rule['permission'], rule['column'],
                rule.get('rows', _ALL_ROWS),
            )

    def _parts(self, column):
        if isinstance(column, JsonPointer):
            return tuple(column.parts)
        try:
            return tuple(self.pointer_cls(column).parts)
        except (TypeError, JsonPointerException) as ex:
            raise ValueError(
                "Invalid column path {0!r}".format(column)) from ex

    def grant(self, permission, column, rows=_ALL_ROWS):
        """Adds a grant of ``permission`` on ``column``.

        ``rows`` is an iterable of business row keys, or omitted to grant the
        permission for every row.
        """
        if permission not in self._grants:
            raise ValueError(
                "Unknown permission {0!r}; expected one of {1}".format(
                    permission, ', '.join(_PERMISSIONS)))
        allowed = _ALL_ROWS if rows is _ALL_ROWS else tuple(rows)
        self._grants[permission].append((self._parts(column), allowed))

    def is_protected(self, parts):
        """Whether any protected column is the location ``parts`` itself or an
        ancestor of it."""
        parts = tuple(parts)
        return any(parts[:len(column)] == column
                   for column in self._protected)

    def is_reorder_controlled(self, parts):
        """Whether the array at ``parts`` has a protected column at or below
        it, so inserting or removing its elements needs a reorder grant.

        This is independent of :meth:`is_protected`: an open array that holds
        protected rows is order-controlled even though the array location
        itself is not a protected column, so row-order and column-write grants
        stay separate.
        """
        parts = tuple(parts)
        depth = len(parts)
        return any(column[:depth] == parts for column in self._protected)

    def protected_columns_under(self, parts):
        """The protected columns strictly below the location ``parts``."""
        parts = tuple(parts)
        depth = len(parts)
        return [column for column in self._protected
                if len(column) > depth and column[:depth] == parts]

    def permits(self, permission, parts, row_key=_UNRESOLVED):
        """Whether ``permission`` on the location ``parts`` is granted for the
        business row ``row_key``. A grant on the location or one of its
        ancestors, on the row or on every row, is sufficient."""
        parts = tuple(parts)
        for column, allowed in self._grants[permission]:
            if parts[:len(column)] != column:
                continue
            if allowed is _ALL_ROWS or row_key is _ALL_ROWS:
                return True
            if row_key is not _UNRESOLVED and row_key in allowed:
                return True
        return False

    def business_key(self, element):
        """The business row key of an array element, or ``_UNRESOLVED`` if it
        is not an object with a row key member."""
        if isinstance(element, Mapping) and self.row_key in element:
            return element[self.row_key]
        return _UNRESOLVED


class _PatchPreflight:
    """Checks a whole patch against a :class:`ColumnPermissions` policy before
    anything is written.

    The operations are replayed on a private copy of the document, so array
    indices always resolve against the state the real application will reach,
    and the business row behind every index is identified by its row key
    rather than by the index itself. Removing or moving an earlier array
    element therefore cannot transfer a grant to the row that shifts into its
    index.
    """

    def __init__(self, doc, permissions, pointer_cls=JsonPointer):
        self.working = copy.deepcopy(doc)
        self.permissions = permissions
        self.pointer_cls = pointer_cls

    def run(self, operations):
        for index, operation in enumerate(operations):
            self._check(operation, index)
            # Replay the operation so the indices of the operations after it
            # resolve against the same state as in the real application.
            self.working = operation.apply(self.working)

    def _path(self, parts):
        return path_join(parts, self.pointer_cls)

    def _try_get(self, subobj, part):
        """Returns the value at ``part`` of ``subobj``, or ``_MISSING`` if the
        location does not exist. The actual application then raises the
        canonical conflict, so the preflight only has to authorize locations
        that really exist."""
        if part is None:
            # The empty pointer selects the whole document
            return subobj
        try:
            return subobj[part]
        except (KeyError, IndexError, TypeError):
            return _MISSING

    def _array_insertion(self, pointer, removed_from=None):
        """For an insertion into an array, returns the concrete landing parts
        and the business key of the row pushed back.

        The last token of an array pointer can be ``'-'`` or an insertion
        index; it is decoded into the index the inserted element really
        occupies. ``removed_from`` is ``(container parts, index)`` of a value
        that leaves the same array first, as for a ``move``: both the landing
        index and the displaced row are then resolved against the shortened
        array. Returns ``None`` when the target is not an array.
        """
        parts = tuple(pointer.parts)
        subobj, part = pointer.to_last(self.working)
        if not isinstance(subobj, MutableSequence):
            return None

        source_index = None
        if removed_from is not None:
            container_parts, removed_index = removed_from
            if tuple(container_parts) == tuple(parts[:-1]) \
                    and isinstance(removed_index, int):
                source_index = removed_index

        length = len(subobj)
        if part == '-':
            # After a removal the array is one element shorter, so appending
            # lands at its last (rather than one-past-last) index.
            landing = length - (1 if source_index is not None else 0)
            displaced = _UNRESOLVED
        elif isinstance(part, int):
            # The target index is evaluated against the array the add really
            # runs on, which is the shortened one for a move, so the landing
            # index is the token itself.
            landing = part
            # The post-removal element at ``landing`` comes from pre-removal
            # index ``landing`` before the source or ``landing + 1`` after it.
            if source_index is None or landing < source_index:
                displaced_pre = landing
            else:
                displaced_pre = landing + 1
            if 0 <= displaced_pre < length:
                displaced = self.permissions.business_key(
                    subobj[displaced_pre])
            else:
                displaced = _UNRESOLVED
        else:
            # An invalid array index; the real application rejects it
            return parts, _UNRESOLVED

        return parts[:-1] + (str(landing),), displaced

    def _resolve(self, parts):
        if not parts:
            return self.working
        return self.pointer_cls(self._path(parts)).resolve(self.working)

    def _row_key(self, parts):
        """The business row key of the array element located by ``parts``,
        walking up to the nearest array element if it is a member inside one.
        """
        for depth in range(len(parts) - 1, -1, -1):
            token = parts[depth]
            if not isinstance(token, str) or not token.lstrip('-').isdigit():
                continue
            try:
                container = self._resolve(parts[:depth])
            except JsonPointerException:
                return _UNRESOLVED
            if not isinstance(container, MutableSequence):
                continue
            index = int(token)
            if 0 <= index < len(container):
                return self.permissions.business_key(container[index])
            return _UNRESOLVED
        return _UNRESOLVED

    def _denied(self, index, operation, parts, permission, row_key):
        column = self._path(parts)
        message = "{0} permission denied for protected column '{1}'".format(
            permission, column)
        if row_key is not _UNRESOLVED:
            message += " of business row {0!r}".format(row_key)
        message += " (operation {0}: {1})".format(
            index, operation.operation.get('op'))
        raise JsonPatchPermissionDenied(
            message, op=operation, index=index, column=column,
            permission=permission,
            row_key=None if row_key is _UNRESOLVED else row_key)

    def _ensure(self, permission, parts, index, operation,
                row_key=_UNRESOLVED):
        parts = tuple(parts)
        if self.permissions.is_protected(parts) and not \
                self.permissions.permits(permission, parts, row_key):
            self._denied(index, operation, parts, permission, row_key)

    def _ensure_reorder(self, parts, index, operation, row_key):
        parts = tuple(parts)
        if self.permissions.is_reorder_controlled(parts) and not \
                self.permissions.permits(PERMISSION_REORDER, parts, row_key):
            self._denied(index, operation, parts, PERMISSION_REORDER,
                         row_key)

    def _value_reaches(self, value, relative_parts):
        """Whether the column ``relative_parts`` below a replaced or inserted
        value exists within that value."""
        current = value
        for token in relative_parts:
            if isinstance(current, Mapping):
                if token not in current:
                    return False
                current = current[token]
            elif isinstance(current, MutableSequence):
                try:
                    element_index = int(token)
                except ValueError:
                    return False
                if not 0 <= element_index < len(current):
                    return False
                current = current[element_index]
            else:
                return False
        return True

    def _ensure_subtree(self, permission, root_parts, value, present_in,
                        index, operation, row_key, resolve_row_key=True):
        """Grants for the protected columns below a written value.

        ``present_in`` are the values (the old and/or the new one) in which a
        protected column has to actually occur, so a whole-object replacement
        is checked against the columns it really overwrites. When the root is
        a destination that the value is inserted or moved into, its indices
        only become valid after the operation, so ``resolve_row_key`` is set
        to ``False`` and the business key of the written value is used rather
        than resolving the indices against the current document.
        """
        for column in self.permissions.protected_columns_under(root_parts):
            relative = column[len(root_parts):]
            if any(self._value_reaches(candidate, relative)
                   for candidate in present_in):
                column_key = row_key
                if resolve_row_key:
                    resolved = self._row_key(column)
                    if resolved is not _UNRESOLVED:
                        column_key = resolved
                self._ensure(permission, column, index, operation,
                             column_key)

    def _check(self, operation, index):
        if isinstance(operation, TestOperation):
            self._check_read(operation, index, operation.pointer)
        elif isinstance(operation, CopyOperation):
            self._check_copy(operation, index)
        elif isinstance(operation, MoveOperation):
            self._check_move(operation, index)
        elif isinstance(operation, RemoveOperation):
            self._check_remove(operation, index)
        elif isinstance(operation, AddOperation):
            self._check_add(operation, index)
        elif isinstance(operation, ReplaceOperation):
            self._check_replace(operation, index)
        elif isinstance(operation, AppendOperation):
            # It both reads the old string and overwrites the member
            self._check_read(operation, index, operation.pointer)
            self._check_write(operation, index, operation.pointer.parts,
                              self._row_key(operation.pointer.parts))
        else:
            # An operation the preflight has no rule for must not be allowed to
            # bypass the authorization, so fail closed rather than skip it.
            raise InvalidJsonPatch(
                "The guarded preflight cannot authorize operation of type "
                "{0}".format(type(operation).__name__), op=operation)

    def _check_read(self, operation, index, pointer):
        parts = tuple(pointer.parts)
        row_key = self._row_key(parts)
        self._ensure(PERMISSION_READ, parts, index, operation, row_key)
        # Reading a container also exposes every protected column inside it
        subobj, part = pointer.to_last(self.working)
        value = self._try_get(subobj, part)
        if value is not _MISSING:
            self._ensure_subtree(PERMISSION_READ, parts, value, [value],
                                 index, operation, row_key)

    def _check_write(self, operation, index, parts, row_key):
        self._ensure(PERMISSION_WRITE, parts, index, operation, row_key)

    def _check_remove(self, operation, index):
        pointer = operation.pointer
        parts = tuple(pointer.parts)
        row_key = self._row_key(parts)
        self._check_write(operation, index, parts, row_key)

        subobj, part = pointer.to_last(self.working)
        if isinstance(subobj, MutableSequence):
            # Taking an element out shifts every row after it, which is a
            # change of row order rather than a column write.
            self._ensure_reorder(parts[:-1], index, operation, row_key)
        value = self._try_get(subobj, part)
        if value is not _MISSING:
            self._ensure_subtree(PERMISSION_WRITE, parts, value, [value],
                                 index, operation, row_key)

    def _check_add(self, operation, index):
        pointer = operation.pointer
        parts = tuple(pointer.parts)
        value = operation.operation['value']
        insertion = self._array_insertion(pointer)

        present_in = [value]
        if insertion is not None:
            parts, displaced = insertion
            self._ensure_reorder(parts[:-1], index, operation, displaced)
            # The inserted element carries its own business row key
            row_key = self.permissions.business_key(value)
        else:
            row_key = self._row_key(parts)
            # Adding over an existing object member replaces it, so the
            # protected columns of the value that is destroyed are covered too.
            subobj, part = pointer.to_last(self.working)
            old_value = self._try_get(subobj, part)
            if old_value is not _MISSING:
                present_in.append(old_value)

        self._check_write(operation, index, parts, row_key)
        self._ensure_subtree(PERMISSION_WRITE, parts, value, present_in,
                             index, operation, row_key,
                             resolve_row_key=False)

    def _check_replace(self, operation, index):
        pointer = operation.pointer
        parts = tuple(pointer.parts)
        new_value = operation.operation['value']
        row_key = self._row_key(parts)
        self._check_write(operation, index, parts, row_key)

        subobj, part = pointer.to_last(self.working)
        old_value = self._try_get(subobj, part)
        if old_value is _MISSING:
            # The replacement itself is invalid; the real application raises
            # the canonical conflict, so only the new side is authorized here.
            present_in = [new_value]
        else:
            # A whole-object replacement overwrites every protected column
            # that occurs on either side of the replacement.
            present_in = [old_value, new_value]
        self._ensure_subtree(PERMISSION_WRITE, parts, new_value, present_in,
                             index, operation, row_key)

    def _check_destination_overwrite(self, pointer, index, operation):
        """Protected columns of the value destroyed by writing at ``pointer``.

        Only an existing object member is replaced; writing into an array
        inserts and shifts elements instead of overwriting one.
        """
        subobj, part = pointer.to_last(self.working)
        if isinstance(subobj, MutableSequence):
            return
        old_value = self._try_get(subobj, part)
        if old_value is _MISSING:
            return
        parts = tuple(pointer.parts)
        self._ensure_subtree(PERMISSION_WRITE, parts, old_value, [old_value],
                             index, operation, self._row_key(parts))

    def _check_move(self, operation, index):
        from_pointer = operation._from_pointer()
        from_parts = tuple(from_pointer.parts)
        to_pointer = operation.pointer
        to_parts = tuple(to_pointer.parts)
        source_subobj, source_part = _to_existing_last(
            from_pointer, self.working)
        source_key = self._row_key(from_parts)
        value = self._try_get(source_subobj, source_part)
        if value is _MISSING:
            # An invalid move source; let the real application raise the
            # canonical conflict instead of authorizing a missing value.
            return

        # The source has to be read, then detached from its column.
        self._ensure(PERMISSION_READ, from_parts, index, operation, source_key)
        self._ensure_subtree(PERMISSION_READ, from_parts, value, [value],
                             index, operation, source_key)
        self._check_write(operation, index, from_parts, source_key)
        self._ensure_subtree(PERMISSION_WRITE, from_parts, value, [value],
                             index, operation, source_key)
        removed_from = None
        if isinstance(source_subobj, MutableSequence):
            self._ensure_reorder(from_parts[:-1], index, operation,
                                 source_key)
            removed_from = (from_parts[:-1], source_part)

        # The same business row is written at the destination, keeping its
        # row key; the grant must not follow the bare array index.
        insertion = self._array_insertion(to_pointer, removed_from)
        if insertion is not None:
            to_parts, displaced = insertion
            self._ensure_reorder(to_parts[:-1], index, operation, displaced)
        else:
            self._check_destination_overwrite(to_pointer, index, operation)
        self._check_write(operation, index, to_parts, source_key)
        self._ensure_subtree(PERMISSION_WRITE, to_parts, value, [value],
                             index, operation, source_key,
                             resolve_row_key=False)

    def _check_copy(self, operation, index):
        from_pointer = operation._from_pointer()
        from_parts = tuple(from_pointer.parts)
        to_pointer = operation.pointer
        to_parts = tuple(to_pointer.parts)
        source_subobj, source_part = _to_existing_last(
            from_pointer, self.working)
        source_key = self._row_key(from_parts)
        value = self._try_get(source_subobj, source_part)
        if value is _MISSING:
            # An invalid copy source; the real application raises the conflict.
            return

        self._ensure(PERMISSION_READ, from_parts, index, operation, source_key)
        self._ensure_subtree(PERMISSION_READ, from_parts, value, [value],
                             index, operation, source_key)

        insertion = self._array_insertion(to_pointer)
        if insertion is not None:
            to_parts, displaced = insertion
            self._ensure_reorder(to_parts[:-1], index, operation, displaced)
        else:
            self._check_destination_overwrite(to_pointer, index, operation)
        # A copied row carries the business key of its value, if it has one.
        row_key = self.permissions.business_key(value)
        if row_key is _UNRESOLVED:
            row_key = source_key
        self._check_write(operation, index, to_parts, row_key)
        self._ensure_subtree(PERMISSION_WRITE, to_parts, value, [value],
                             index, operation, row_key,
                             resolve_row_key=False)


class GuardedJsonPatch(JsonPatch):
    """A :class:`JsonPatch` whose whole batch is authorized before it is
    applied.

    Every operation is checked against a :class:`ColumnPermissions` policy on
    a private copy of the document first. If any operation is denied,
    :class:`JsonPatchPermissionDenied` is raised for the first offending
    operation and the document passed to :meth:`apply` is left unchanged; no
    operation of the batch is written. The check also replays the operations,
    so indices are decoded against the state each operation actually reaches
    and permissions follow the business row keys, not array positions.

    :param patch: Iterable of operation dicts; a raw JSON-encoded string is
                  handled by :meth:`from_string`.
    :param permissions: The protected columns and their grants.
    :type permissions: ColumnPermissions
    """

    def __init__(self, patch, permissions, pointer_cls=JsonPointer):
        super().__init__(patch, pointer_cls=pointer_cls)
        if not isinstance(permissions, ColumnPermissions):
            raise TypeError(
                "permissions must be a ColumnPermissions instance")
        self.permissions = permissions

    @classmethod
    def from_string(cls, patch_str, loads=None, pointer_cls=JsonPointer,
                    permissions=None):
        """Creates a guarded patch from a raw JSON patch string."""
        json_loader = loads or cls.json_loader
        patch = json_loader(patch_str)
        return cls(patch, permissions, pointer_cls=pointer_cls)

    def apply(self, obj, in_place=False, atomic=False):
        # The preflight either accepts every operation or raises before the
        # document is touched, so application is all-or-nothing regardless of
        # the atomic flag.
        _PatchPreflight(obj, self.permissions, self.pointer_cls).run(
            self._ops)
        return super().apply(obj, in_place=in_place, atomic=atomic)


def apply_guarded_patch(doc, patch, permissions, in_place=False,
                        pointer_cls=JsonPointer):
    """Apply a JSON Patch after a whole-batch permission preflight.

    Like :func:`apply_patch`, but every operation is checked against
    ``permissions`` before the document is changed. A denied operation raises
    :class:`JsonPatchPermissionDenied`, pointing at the first offending
    operation, its protected column and the business row it belongs to, and
    leaves ``doc`` unchanged.

    :param doc: JSON document, including arrays and scalar values.
    :type doc: object

    :param patch: Iterable of operation dicts or a raw JSON-encoded string.
    :type patch: iterable or str

    :param permissions: The protected columns and their grants.
    :type permissions: ColumnPermissions

    :param in_place: While ``True`` patch will modify target document.
    :type in_place: bool

    :param pointer_cls: JSON pointer class to use.
    :type pointer_cls: Type[JsonPointer]

    :return: Resulting document.
    :rtype: object
    """
    if isinstance(patch, (str, bytes)):
        guarded = GuardedJsonPatch.from_string(
            patch, pointer_cls=pointer_cls, permissions=permissions)
    else:
        guarded = GuardedJsonPatch(patch, permissions,
                                   pointer_cls=pointer_cls)
    return guarded.apply(doc, in_place=in_place)
