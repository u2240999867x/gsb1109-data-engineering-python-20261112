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

""" Merge concurrently received RFC 6902 batches into one JSON document.

Two offline cleaning peers start from the same JSON baseline and each edits
the same nested details. Their patches contain nothing but RFC 6902
operations (``add``, ``remove``, ``replace``, ``move``, ``copy`` and
``test``), a stable batch id, parent batch ids and per-batch operation
sequence numbers; there are no row ids. :class:`PatchMerger` accepts the
batches in any order, possibly more than once, and replays them into one
merged document.

The merge is deterministic: for the same batch graph every receive order,
and every split into two mergers that are joined afterwards, produces the
same document, the same conflict set and the same canonical patch, which is
an ordinary RFC 6902 patch that replays against the common baseline.

Array members are tracked by identity rather than by value: two members of
the baseline array that compare equal are still distinct rows, and a member
inserted by a batch can be moved, copied or referenced again by later
operations of the same batch. Concurrent inserts into the same gap are
ordered by ``(batch id, operation sequence number)``.

A ``copy`` reads the value its author saw. If a concurrent batch changes the
read precondition, or a concurrent ``remove`` deletes an ancestor of the
destination of a ``move``, both sides of the conflict and the affected
follow-up operations are retained in the conflict record, while independent
rows still commit. Concurrent writes to the same value location conflict;
steps that depend on an already conflicting read wait for manual
resolution, whereas unrelated follow-ups apply normally.

Batches with the same id are deduplicated: an identical repeat is ignored,
a repeat with different content raises :class:`DuplicateBatchError`. Causal
order comes from the parent batch ids declared by each batch; parents may
arrive after their children.
"""

import bisect
import copy
from collections import deque
from collections.abc import Mapping, Sequence

from jsonpointer import JsonPointer, JsonPointerException

from jsonpatch import _json_equal, apply_patch, make_patch, path_split


# The labels of baseline array items are multiples of this gap, leaving room
# between every pair of neighbours for inserts without an immediate relabel.
_LABEL_GAP = 1 << 32
# Labels of items inserted one after another into the same gap differ by at
# least this much, again to leave room for further inserts between them.
_LABEL_STEP = 1 << 16


class JsonMergeException(Exception):
    """Base exception of the batch merge engine."""


class DuplicateBatchError(JsonMergeException):
    """Raised when a batch id is received twice with different content."""


class InvalidBatch(JsonMergeException):
    """Raised when a received batch is not a valid merge batch."""


class Conflict:
    """A conflict between operations of different batches.

    Conflicting operations are never applied; their operation records are
    retained together with the follow-up operations that depend on them and
    therefore wait for resolution. Operations of batches unrelated to the
    conflict apply normally.

    :ivar reason: why the operations conflict, one of :data:`Conflict.REASONS`.
    :vartype reason: str
    :ivar operations: the operation records on both sides of the conflict,
                      each a dict with ``batch``, ``sequence`` and ``op``
                      members.
    :vartype operations: list[dict]
    :ivar dependents: operation records that depend on a conflicting read and
                      wait for the conflict to be resolved.
    :vartype dependents: list[dict]
    """

    #: Two operations write the same value location concurrently.
    WRITE = 'write'
    #: A ``copy`` reads a value that a concurrent batch changes.
    STALE_COPY = 'stale_copy'
    #: A ``move`` targets a location whose ancestor a concurrent batch removes.
    MOVED_INTO_REMOVED = 'moved_into_removed'
    #: A ``test`` fails against the merged state.
    TEST_FAILED = 'test_failed'
    #: An operation cannot apply because a location it needs is missing.
    MISSING_LOCATION = 'missing_location'
    #: A batch declares a parent that is not part of the merge graph.
    UNKNOWN_PARENT = 'unknown_parent'
    #: The parent links between batches contain a cycle.
    CAUSAL_CYCLE = 'causal_cycle'

    REASONS = frozenset((
        WRITE, STALE_COPY, MOVED_INTO_REMOVED, TEST_FAILED,
        MISSING_LOCATION, UNKNOWN_PARENT, CAUSAL_CYCLE,
    ))

    def __init__(self, reason, operations, dependents=()):
        if reason not in self.REASONS:
            raise ValueError('unknown conflict reason {0!r}'.format(reason))
        self.reason = reason
        self.operations = list(operations)
        self.dependents = list(dependents)

    def __eq__(self, other):
        if not isinstance(other, Conflict):
            return NotImplemented
        return (self.reason == other.reason
                and self.operations == other.operations
                and self.dependents == other.dependents)

    def __hash__(self):
        return hash((self.reason,
                     tuple(_op_key(record) for record in self.operations),
                     tuple(_op_key(record) for record in self.dependents)))

    def __repr__(self):
        return 'Conflict({0!r}, {1!r}, {2!r})'.format(
            self.reason, self.operations, self.dependents)

    def to_dict(self):
        """A JSON-serializable representation of the conflict."""
        return {
            'reason': self.reason,
            'operations': copy.deepcopy(self.operations),
            'dependents': copy.deepcopy(self.dependents),
        }


def _op_key(record):
    """The stable identity of an operation record, used for sorting."""
    return record['batch'], record['sequence']


def _record(batch_id, sequence, op):
    return {'batch': batch_id, 'sequence': sequence, 'op': copy.deepcopy(op)}


class _Node:
    """A value in the tracked document tree.

    Every node has a stable identity that is regenerated in the same way in
    every replay pass: baseline nodes receive baseline ids in traversal
    order, nodes created by an operation receive the ``(batch, sequence)``
    of their creator. Moves keep the node and hence its identity.
    """

    __slots__ = ('node_id', 'value', 'creator', 'mover', 'writers',
                 'readers')

    def __init__(self, node_id, value, creator=None, mover=None):
        self.node_id = node_id
        self.value = value
        # (batch id, sequence number) of the add/copy that created the node;
        # None for baseline content, and unchanged by moves
        self.creator = creator
        # (batch id, sequence number) of the last move that placed the node
        self.mover = mover
        # Operation records that wrote this node's value location.
        self.writers = []
        # Operation records that read this node (copy / test), oldest first.
        self.readers = []

    @property
    def origin(self):
        """The operation that last introduced the node at its position: its
        creator, or its last move for a moved node."""
        return self.mover if self.mover is not None else self.creator


class _IdFactory:
    """Assigns the stable ids of baseline nodes; created nodes use their
    creator's (batch id, sequence number)."""

    def __init__(self):
        self._counter = 0

    def baseline_id(self):
        node_id = ('base', self._counter)
        self._counter += 1
        return node_id

    @staticmethod
    def created_id(batch_id, sequence):
        return ('new', batch_id, sequence)


class _Array:
    """An array whose members are tracked by identity, not by value.

    Members keep fractional labels, so inserting at an index places a member
    between its neighbours without renaming anyone and without losing the
    distinction between equal baseline members. The array also keeps an event
    history from which each batch's own view can be reconstructed, so a
    concurrent reordering never shifts the indices an author wrote.
    """

    __slots__ = ('nodes', 'labels', 'base', 'history')

    def __init__(self):
        self.nodes = []
        self.labels = []
        # baseline members in their original order
        self.base = []
        # (kind, batch id, sequence, node, predecessor, successor) events;
        # predecessor / successor anchor an insert and are None at the ends
        self.history = []

    @classmethod
    def of(cls, values, ids):
        array = cls()
        for value in values:
            node = _Node(ids.baseline_id(), _wrap(value, ids))
            array.nodes.append(node)
            array.base.append(node)
            array.labels.append(len(array.nodes) * _LABEL_GAP - _LABEL_GAP)
        return array

    def index_of(self, node):
        return self.nodes.index(node)

    def log_insert(self, batch_id, sequence, node, predecessor, successor):
        self.history.append(
            ('ins', batch_id, sequence, node, predecessor, successor))

    def log_remove(self, batch_id, sequence, node):
        self.history.append(('rem', batch_id, sequence, node, None, None))

    def view_for(self, batch_id, ancestors):
        """The members of the array as batch_id sees them: baseline content
        with only its own and its ancestor batches' events applied."""
        visible_batches = ancestors | {batch_id}
        view = list(self.base)
        for event in self.history:
            kind, event_batch, _, node, predecessor, successor = event
            if event_batch not in visible_batches:
                continue
            if kind == 'rem':
                if node in view:
                    view.remove(node)
            else:
                if successor is not None and successor in view:
                    position = view.index(successor)
                else:
                    position = len(view)
                view.insert(position, node)
        return view

    def insert(self, index, node):
        if index == 0:
            label = self.labels[0] - _LABEL_GAP if self.labels else 0
        elif index == len(self.nodes):
            label = self.labels[-1] + _LABEL_GAP
        else:
            low, high = self.labels[index - 1], self.labels[index]
            if high - low <= _LABEL_STEP:
                self._relabel()
                low, high = self.labels[index - 1], self.labels[index]
            label = low + min(_LABEL_STEP, (high - low) // 2)
        self.nodes.insert(index, node)
        self.labels.insert(index, label)

    def remove(self, node):
        index = self.index_of(node)
        del self.nodes[index]
        del self.labels[index]

    def _relabel(self):
        self.labels = [index * _LABEL_GAP for index in range(len(self.nodes))]


def _wrap(value, ids):
    """Wraps the containers of a value into tracked mappings and arrays."""
    if isinstance(value, Mapping):
        return {key: _Node(ids.baseline_id(), _wrap(member, ids))
                for key, member in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _Array.of(value, ids)
    return value


def _unwrap(value):
    """The plain JSON value behind a tracked tree."""
    if isinstance(value, _Array):
        return [_unwrap(node.value) for node in value.nodes]
    if isinstance(value, Mapping):
        return {key: _unwrap(node.value) for key, node in value.items()}
    return value


def _walk_subtree(node):
    """All nodes of the subtree rooted at node, node itself first."""
    yield node
    value = node.value
    if isinstance(value, _Array):
        for child in value.nodes:
            yield from _walk_subtree(child)
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_subtree(child)


class _Engine:
    """Replays scheduled operations onto a tracked tree.

    Three passes keep the result independent of the receive order:

    1. *simulate* applies every operation structurally onto a tracked tree,
       recording per node its writes and reads (``copy`` and ``test``).
       Removed subtrees are detached but kept in a graveyard with their
       histories, and operations that do not resolve are logged instead of
       raising.
    2. *detect* derives the conflicts from those histories, so concurrent
       operations are paired the same way no matter which one was scheduled
       first. The conflicting operations and their dependents are excluded.
    3. *replay* applies only the surviving operations onto a fresh tree and
       evaluates the surviving tests, so the document contains no effect of a
       conflicting operation.
    """

    def __init__(self, baseline, ancestors):
        self._baseline = baseline
        # batch id -> set of all ancestor batch ids (transitive)
        self._ancestors = ancestors
        self._ids = None
        self.root = None
        # (batch id, sequence) -> Conflict that excludes the operation
        self._excluded = {}
        # pointer parts -> Conflict of an operation that failed there
        self._failed_parts = []
        # (batch id, sequence) -> record of the creating/moving operation
        self._origin_record = {}
        # all conflicts, before canonical sorting
        self._conflicts = []

    # -- helpers ---------------------------------------------------------

    def _concurrent(self, batch_id, other_batch):
        """Whether the two batches are unrelated by causality: neither is an
        ancestor of the other. Only concurrent batches can conflict."""
        if batch_id == other_batch:
            return False
        first_ancestors = self._ancestors.get(batch_id, ())
        second_ancestors = self._ancestors.get(other_batch, ())
        return other_batch not in first_ancestors \
            and batch_id not in second_ancestors

    def _view(self, array, batch_id):
        """The array as batch_id sees it, reconstructed from its events."""
        return array.view_for(batch_id,
                              self._ancestors.get(batch_id, frozenset()))

    def _is_visible(self, node, batch_id):
        """Whether batch_id sees node in its own view of the document:
        baseline content, its own nodes and nodes its ancestors created.
        A move never changes who created a node, so moved baseline rows stay
        visible to every batch."""
        if node.creator is None:
            return True
        creator = node.creator[0]
        return creator == batch_id \
            or creator in self._ancestors.get(batch_id, ())

    @staticmethod
    def _key(record):
        return _op_key(record)

    @staticmethod
    def _sorted(records):
        return sorted(records, key=_op_key)

    # -- path resolution -------------------------------------------------

    def _resolve(self, parts, batch_id=None):
        """Resolves parts to ``(node, parent, key)``; node is None when the
        last token addresses a missing member. parent is the containing
        array or mapping (not its node).

        With a batch id, array indices are interpreted against that batch's
        view of the array (baseline, its ancestors and its own earlier
        operations), so members inserted or removed by concurrent batches do
        not shift the indices the author wrote.
        """
        current = self.root
        parent = None
        key = None
        for depth, part in enumerate(parts):
            last = depth == len(parts) - 1
            if isinstance(current.value, _Array):
                array = current.value
                if part == '-':
                    if not last:
                        raise JsonPointerException("can't resolve through '-'")
                    return None, array, '-'
                index = self._array_index(part)
                view = array.nodes if batch_id is None \
                    else self._view(array, batch_id)
                if index < 0 or index >= len(view):
                    if not last:
                        raise JsonPointerException(
                            "array index {0} out of range".format(index))
                    return None, array, index
                chosen = view[index]
                # the view index, not a physical index: a node that a
                # concurrent batch moved out is still in this author's view
                parent, key = array, index
                current = chosen
            elif isinstance(current.value, Mapping):
                mapping = current.value
                if part not in mapping:
                    if not last:
                        raise JsonPointerException(
                            "member '{0}' not found".format(part))
                    return None, mapping, part
                parent, key = mapping, part
                current = mapping[part]
            else:
                raise JsonPointerException(
                    "can't resolve through a scalar value")
        return current, parent, key

    def _resolve_parent(self, parts, batch_id=None):
        """The container node of the last token and that token itself.

        Unlike :meth:`_resolve`, this never validates the final array index
        against the current merged array, whose length differs from the
        author's view when concurrent batches inserted or removed members.
        """
        if not parts:
            return self.root, None
        if len(parts) == 1:
            return self.root, parts[0]
        container, _, _ = self._resolve(parts[:-1], batch_id)
        if container is None:
            raise JsonPointerException(
                "the container of '{0}' does not exist".format(parts[-1]))
        return container, parts[-1]

    @staticmethod
    def _array_index(part):
        if isinstance(part, str):
            if not part or (part[0] == '0' and part != '0') \
                    or not part.isdigit():
                raise JsonPointerException(
                    "invalid array index '{0}'".format(part))
            return int(part)
        if isinstance(part, int) and part >= 0:
            return part
        raise JsonPointerException("invalid array index {0!r}".format(part))

    def _insert_position(self, array, index, record):
        """A concrete position in array.nodes and the anchor nodes for the
        event log, when record inserts at index of its own view.

        The operation addresses a gap between the neighbours it saw in its
        own view (baseline, ancestor batches and its own earlier ops), so
        nodes inserted concurrently into the same gap are ordered by
        ``(batch id, sequence number)`` instead of pushing each other.
        """
        batch_id = record['batch']
        view = self._view(array, batch_id)
        if index > len(view) or index < 0:
            raise JsonPointerException("can't insert outside of array")

        def physical_index(node):
            return array.index_of(node) if node in array.nodes else None

        start = 0
        for anchor in reversed(view[:index]):
            position = physical_index(anchor)
            if position is not None:
                start = position + 1
                break
        end = len(array.nodes)
        for anchor in view[index:]:
            position = physical_index(anchor)
            if position is not None:
                end = position
                break
        predecessor = view[index - 1] if index > 0 else None
        successor = view[index] if index < len(view) else None
        new_key = (_sort_key(batch_id), record['sequence'])
        offset = 0
        for position in range(start, end):
            origin = array.nodes[position].origin
            if origin is not None \
                    and (_sort_key(origin[0]), origin[1]) < new_key:
                offset += 1
        return start + offset, predecessor, successor

    def _array_insert(self, array, index, record, node):
        position, predecessor, successor = self._insert_position(
            array, index, record)
        array.insert(position, node)
        array.log_insert(record['batch'], record['sequence'], node,
                         predecessor, successor)

    def _array_remove(self, array, record, node):
        if node not in array.nodes:
            # visible to the author but moved away by a concurrent batch
            raise JsonPointerException("member is no longer in the array")
        array.remove(node)
        array.log_remove(record['batch'], record['sequence'], node)

    def _node_for_parts(self, parts):
        """The live node at parts, or None if the location is absent."""
        current = self.root
        for part in parts:
            value = current.value
            if isinstance(value, _Array):
                if part == '-':
                    return None
                try:
                    index = self._array_index(part)
                except JsonPointerException:
                    return None
                if index < 0 or index >= len(value.nodes):
                    return None
                current = value.nodes[index]
            elif isinstance(value, Mapping):
                if part not in value:
                    return None
                current = value[part]
            else:
                return None
        return current

    # -- pass 1: structural simulation -----------------------------------

    def simulate(self, records):
        self._ids = _IdFactory()
        self._origin_record = {}
        self.root = _Node(self._ids.baseline_id(),
                          _wrap(self._baseline, self._ids))
        # operations that do not resolve structurally: (record, parts)
        self._structural_failures = []
        # detached subtrees: (remove record, parts, root node)
        self._graveyard = []
        # successful moves: (record, moved node, src parts, dst parts)
        self._move_log = []
        for record in records:
            self._simulate_one(record)

    def _simulate_one(self, record):
        op = record['op']
        name = op['op']
        parts = path_split(op['path'])
        try:
            if name == 'add':
                self._sim_add(record, parts, op['value'])
            elif name == 'remove':
                self._sim_remove(record, parts)
            elif name == 'replace':
                self._sim_replace(record, parts, op['value'])
            elif name == 'move':
                self._sim_move(record, parts, path_split(op['from']))
            elif name == 'copy':
                self._sim_copy(record, parts, path_split(op['from']))
            else:
                self._sim_read(record, parts)
        except (KeyError, JsonPointerException):
            self._structural_failures.append((record, tuple(parts)))

    def _new_node(self, record, value):
        batch_id, sequence = record['batch'], record['sequence']
        node = _Node(_IdFactory.created_id(batch_id, sequence),
                     _wrap(copy.deepcopy(value), self._ids),
                     creator=(batch_id, sequence))
        self._origin_record[(batch_id, sequence)] = record
        node.writers.append(record)
        return node

    def _sim_add(self, record, parts, value):
        batch_id = record['batch']
        if not parts:
            # an add at the document root replaces the whole document
            self.root.value = _wrap(copy.deepcopy(value), self._ids)
            self.root.creator = (batch_id, record['sequence'])
            self.root.mover = None
            self.root.writers.append(record)
            return
        container, key = self._resolve_parent(parts, batch_id)
        target = container.value
        if isinstance(target, _Array):
            index = (len(self._view(target, batch_id))
                     if key == '-' else self._array_index(key))
            self._array_insert(target, index, record,
                               self._new_node(record, value))
            return
        if not isinstance(target, Mapping):
            raise JsonPointerException("unable to resolve add target")
        node = target.get(key)
        new_node = self._new_node(record, value)
        if node is not None:
            # RFC 6902: an add to an existing object member replaces it.
            node.value = new_node.value
            node.creator = new_node.creator
            node.mover = None
            node.writers.append(record)
        else:
            target[key] = new_node

    def _sim_remove(self, record, parts):
        if not parts:
            raise JsonPointerException("can't remove the whole document")
        node, parent, key = self._resolve(parts, record['batch'])
        if node is None:
            raise JsonPointerException("can't remove a missing location")
        if isinstance(parent, _Array):
            self._array_remove(parent, record, node)
        else:
            del parent[key]
        node.writers.append(record)
        self._graveyard.append((record, tuple(parts), node))

    def _sim_replace(self, record, parts, value):
        node, _, _ = self._resolve(parts, record['batch'])
        if node is None:
            raise JsonPointerException("can't replace a missing location")
        node.value = _wrap(copy.deepcopy(value), self._ids)
        node.writers.append(record)

    def _sim_move(self, record, dst_parts, src_parts):
        batch_id = record['batch']
        if not src_parts or not dst_parts:
            raise JsonPointerException(
                "can't move the whole document or move onto it")
        if dst_parts and src_parts[:len(dst_parts)] == dst_parts:
            raise JsonPointerException(
                "cannot move a value into its own child")
        src_node, src_parent, src_key = self._resolve(src_parts, batch_id)
        if src_node is None:
            raise JsonPointerException("can't move from a missing location")
        dst_container, dst_key = self._resolve_parent(dst_parts, batch_id)
        target = dst_container.value
        if isinstance(target, _Array):
            dst_parent, dst_node = target, None
        elif isinstance(target, Mapping):
            dst_parent, dst_node = target, target.get(dst_key)
        else:
            raise JsonPointerException("unable to resolve move target")
        if src_parts == dst_parts:
            src_node.writers.append(record)
            self._move_log.append(
                (record, src_node, tuple(src_parts), tuple(dst_parts)))
            return

        if isinstance(src_parent, _Array) and src_parent is dst_parent:
            # both indices are interpreted in the author's own reconstructed
            # view, so concurrent inserts/removes never move the targets
            view_before = self._view(src_parent, batch_id)
            src_visible = view_before.index(src_node)
            self._array_remove(src_parent, record, src_node)
            if dst_key == '-':
                adjusted = len(view_before) - 1
            else:
                dst_visible = self._array_index(dst_key)
                adjusted = (dst_visible
                            if dst_visible <= src_visible
                            else dst_visible - 1)
            self._array_insert(src_parent, adjusted, record, src_node)
        else:
            if isinstance(src_parent, _Array):
                self._array_remove(src_parent, record, src_node)
            else:
                del src_parent[src_key]
            if isinstance(dst_parent, _Array):
                index = (len(self._view(dst_parent, batch_id))
                         if dst_key == '-'
                         else self._array_index(dst_key))
                self._array_insert(dst_parent, index, record, src_node)
            elif dst_node is not None:
                dst_node.value = src_node.value
                dst_node.creator = src_node.creator
                dst_node.mover = (record['batch'], record['sequence'])
                dst_node.writers.append(record)
            else:
                dst_parent[dst_key] = src_node
        src_node.mover = (record['batch'], record['sequence'])
        self._origin_record[(record['batch'], record['sequence'])] = record
        src_node.writers.append(record)
        self._move_log.append(
            (record, src_node, tuple(src_parts), tuple(dst_parts)))

    def _sim_copy(self, record, dst_parts, src_parts):
        src_node, _, _ = self._resolve(src_parts, record['batch'])
        if src_node is None:
            raise JsonPointerException("can't copy from a missing location")
        src_node.readers.append(record)
        self._sim_add(record, dst_parts, _unwrap(src_node.value))

    def _sim_read(self, record, parts):
        node, _, _ = self._resolve(parts, record['batch'])
        if node is None:
            raise JsonPointerException("can't read a missing location")
        node.readers.append(record)

    # -- pass 2: order-independent conflict detection --------------------

    def detect(self):
        conflicts = []
        excluded = {}
        conflict_index = {}
        # (pointer parts, conflict) for every conflicted value location, so
        # follow-up operations touching it attach as dependents in the replay
        location_failures = []

        def add(reason, sides, dependents=(), location=None):
            sides = self._sorted(sides)
            index_key = (reason, tuple(sorted(_op_key(record)
                                              for record in sides)))
            conflict = conflict_index.get(index_key)
            if conflict is None:
                conflict = Conflict(reason, sides,
                                    self._sorted(dependents))
                conflict_index[index_key] = conflict
                conflicts.append(conflict)
            elif dependents:
                conflict.dependents = self._sorted(
                    conflict.dependents + list(dependents))
            for record in sides + list(dependents):
                excluded.setdefault(_op_key(record), conflict)
            if location is not None:
                location_failures.append((tuple(location), conflict))
            return conflict

        self._detect_writes_and_reads(add)
        self._detect_graveyard(add)
        self._detect_failures(add)
        self._conflicts = conflicts
        self._excluded = excluded
        # seed the replay with the conflicted and failed locations, so their
        # follow-up operations attach as dependents
        self._failed_parts = location_failures + list(self._sim_failed_parts)
        return conflicts

    def _walk_with_parts(self, node, parts=()):
        yield node, tuple(parts)
        value = node.value
        if isinstance(value, _Array):
            for position, child in enumerate(value.nodes):
                yield from self._walk_with_parts(
                    child, tuple(parts) + (position,))
        elif isinstance(value, Mapping):
            for key, child in value.items():
                yield from self._walk_with_parts(
                    child, tuple(parts) + (key,))

    def _detect_writes_and_reads(self, add):
        """Concurrent writers of one location, and reads whose precondition a
        concurrent writer changes."""
        for node, parts in self._walk_with_parts(self.root):
            foreign_writers = self._sorted(
                record for record in node.writers
                if any(self._concurrent(record['batch'], other['batch'])
                       for other in node.writers))
            write_conflict = None
            if foreign_writers:
                # a writer that descends from another concurrent writer does
                # not take its own side: it waits on the ancestor's conflict
                root_sides, causal_dependents = [], []
                for writer in foreign_writers:
                    has_foreign_ancestor = any(
                        other is not writer
                        and other['batch']
                        in self._ancestors.get(writer['batch'], ())
                        for other in foreign_writers)
                    (causal_dependents if has_foreign_ancestor
                     else root_sides).append(writer)
                write_conflict = add(
                    Conflict.WRITE, root_sides,
                    dependents=causal_dependents, location=parts)

            for reader in list(node.readers):
                foreign = [writer for writer in node.writers
                           if self._concurrent(reader['batch'],
                                               writer['batch'])]
                if not foreign:
                    continue
                if write_conflict is not None:
                    # the location is already owned by a write conflict, so a
                    # read that depended on its value waits for resolution
                    add(write_conflict.reason, write_conflict.operations,
                        dependents=[reader])
                else:
                    reason = (Conflict.TEST_FAILED
                              if reader['op']['op'] == 'test'
                              else Conflict.STALE_COPY)
                    add(reason, [reader] + foreign, location=parts)

    def _detect_graveyard(self, add):
        """Pairs removals with concurrent moves that targeted the removed
        subtree and with reads/copies whose source lived inside it."""
        buried_ids = {}
        for remove_record, removed_parts, removed_node in self._graveyard:
            for node in _walk_subtree(removed_node):
                buried_ids[node.node_id] = (remove_record, removed_parts)

        # successful moves whose node now lies in a removed subtree
        for move_record, moved_node, src_parts, dst_parts in self._move_log:
            entry = buried_ids.get(moved_node.node_id)
            if entry is None:
                continue
            remove_record, removed_parts = entry
            if self._concurrent(move_record['batch'],
                                remove_record['batch']):
                add(Conflict.MOVED_INTO_REMOVED,
                    [move_record, remove_record], location=removed_parts)

        # moves that failed structurally because the removal came first
        for failed_record, failed_parts in self._structural_failures:
            if failed_record['op']['op'] != 'move':
                continue
            src_parts = path_split(failed_record['op']['from'])
            for remove_record, removed_parts, _removed_node in self._graveyard:
                prefix = tuple(src_parts[:len(removed_parts)])
                if prefix == removed_parts and self._concurrent(
                        failed_record['batch'], remove_record['batch']):
                    add(Conflict.MOVED_INTO_REMOVED,
                        [failed_record, remove_record],
                        location=removed_parts)
                    break

        # reads whose source now lies in a concurrently removed subtree
        for remove_record, removed_parts, removed_node in self._graveyard:
            for node in _walk_subtree(removed_node):
                for reader in node.readers:
                    if not self._concurrent(reader['batch'],
                                            remove_record['batch']):
                        continue
                    reason = (Conflict.TEST_FAILED
                              if reader['op']['op'] == 'test'
                              else Conflict.STALE_COPY)
                    add(reason, [reader, remove_record],
                        location=removed_parts)
                # writers of a node that the removal detached: writing the
                # same value location concurrently with the removal conflicts
                for writer in node.writers:
                    if writer is remove_record or not self._concurrent(
                            writer['batch'], remove_record['batch']):
                        continue
                    if writer['op']['op'] == 'move':
                        add(Conflict.MOVED_INTO_REMOVED,
                            [writer, remove_record],
                            location=removed_parts)
                    else:
                        add(Conflict.WRITE, [writer, remove_record],
                            location=removed_parts)

    def _detect_failures(self, add):
        """Classifies the operations that never resolved structurally."""
        self._sim_failed_parts = []
        for record, parts in self._structural_failures:
            if _op_key(record) in self._excluded:
                # already paired, e.g. a move matched by _detect_graveyard
                self._sim_failed_parts.append(
                    (parts, self._excluded[_op_key(record)]))
                continue
            owner = self._match_failure(record, parts, add)
            self._sim_failed_parts.append((parts, owner))

    def _match_failure(self, record, parts, add):
        # a concurrent removal of an ancestor location
        for remove_record, removed_parts, _removed_node in self._graveyard:
            prefix = tuple(parts[:len(removed_parts)])
            if prefix == removed_parts and self._concurrent(
                    record['batch'], remove_record['batch']):
                reason = (Conflict.MOVED_INTO_REMOVED
                          if record['op']['op'] == 'move'
                          else Conflict.MISSING_LOCATION)
                return add(reason, [record, remove_record],
                           location=removed_parts)
        # a concurrent writer now sitting at the exact location
        node = self._node_for_parts(parts)
        if node is not None:
            foreign = [writer for writer in node.writers
                       if self._concurrent(record['batch'],
                                           writer['batch'])]
            if foreign:
                return add(Conflict.WRITE, [record] + foreign,
                           location=parts)
        return add(Conflict.MISSING_LOCATION, [record], location=parts)

    # -- pass 3: replay of the survivors ---------------------------------

    def replay(self, records):
        self._ids = _IdFactory()
        self._origin_record = {}
        self.root = _Node(self._ids.baseline_id(),
                          _wrap(self._baseline, self._ids))
        for record in records:
            if _op_key(record) in self._excluded:
                continue
            self._replay_one(record)
        return self._sorted_conflicts()

    def _owner_of_failed_parts(self, parts):
        best = None
        best_length = -1
        for failed_parts, conflict in self._failed_parts:
            if tuple(parts[:len(failed_parts)]) == failed_parts \
                    and len(failed_parts) > best_length:
                best = conflict
                best_length = len(failed_parts)
        return best

    def _replay_one(self, record):
        op = record['op']
        name = op['op']
        parts = path_split(op['path'])
        owner = self._owner_of_failed_parts(parts)
        if owner is not None:
            self._attach_dependent(record, owner, parts)
            return
        if name in ('move', 'copy'):
            source_owner = self._owner_of_failed_parts(
                path_split(op['from']))
            if source_owner is not None:
                self._attach_dependent(record, source_owner, parts)
                return
        try:
            if name == 'add':
                self._replay_add(record, parts, op['value'])
            elif name == 'remove':
                self._replay_remove(record, parts)
            elif name == 'replace':
                self._replay_replace(record, parts, op['value'])
            elif name == 'move':
                self._replay_move(record, parts, path_split(op['from']))
            elif name == 'copy':
                self._replay_copy(record, parts, path_split(op['from']))
            else:
                self._replay_test(record, parts, op['value'])
        except (KeyError, JsonPointerException):
            conflict = self._record_missing(record, parts)
            self._failed_parts.append((tuple(parts), conflict))

    def _attach_dependent(self, record, conflict, failed_parts):
        conflict.dependents = self._sorted(conflict.dependents + [record])
        self._excluded[_op_key(record)] = conflict
        self._failed_parts.append((tuple(failed_parts), conflict))

    def _record_missing(self, record, parts):
        owner = self._owner_of_failed_parts(parts)
        if owner is not None:
            self._attach_dependent(record, owner, parts)
            return owner
        conflict = Conflict(Conflict.MISSING_LOCATION, [record])
        self._conflicts.append(conflict)
        self._excluded[_op_key(record)] = conflict
        return conflict

    def _replay_add(self, record, parts, value):
        batch_id = record['batch']
        if not parts:
            self.root.value = _wrap(copy.deepcopy(value), self._ids)
            self.root.creator = (batch_id, record['sequence'])
            self.root.mover = None
            self.root.writers.append(record)
            return
        container, key = self._resolve_parent(parts, batch_id)
        target = container.value
        if isinstance(target, _Array):
            index = (len(self._view(target, batch_id))
                     if key == '-' else self._array_index(key))
            self._array_insert(target, index, record,
                               self._new_node(record, value))
            return
        if not isinstance(target, Mapping):
            raise JsonPointerException("unable to resolve add target")
        node = target.get(key)
        new_node = self._new_node(record, value)
        if node is not None:
            node.value = new_node.value
            node.creator = new_node.creator
            node.mover = None
            node.writers.append(record)
        else:
            target[key] = new_node

    def _replay_remove(self, record, parts):
        if not parts:
            raise JsonPointerException("can't remove the whole document")
        node, parent, key = self._resolve(parts, record['batch'])
        if node is None:
            raise JsonPointerException("can't remove a missing location")
        if isinstance(parent, _Array):
            self._array_remove(parent, record, node)
        else:
            del parent[key]
        node.writers.append(record)

    def _replay_replace(self, record, parts, value):
        node, _, _ = self._resolve(parts, record['batch'])
        if node is None:
            raise JsonPointerException("can't replace a missing location")
        node.value = _wrap(copy.deepcopy(value), self._ids)
        node.writers.append(record)

    def _replay_move(self, record, dst_parts, src_parts):
        batch_id = record['batch']
        if not src_parts or not dst_parts:
            raise JsonPointerException(
                "can't move the whole document or move onto it")
        src_node, src_parent, src_key = self._resolve(src_parts, batch_id)
        if src_node is None:
            raise JsonPointerException("can't move from a missing location")
        dst_container, dst_key = self._resolve_parent(dst_parts, batch_id)
        target = dst_container.value
        if isinstance(target, _Array):
            dst_parent, dst_node = target, None
        elif isinstance(target, Mapping):
            dst_parent, dst_node = target, target.get(dst_key)
        else:
            raise JsonPointerException("unable to resolve move target")
        if src_parts == dst_parts:
            src_node.writers.append(record)
            return

        if isinstance(src_parent, _Array) and src_parent is dst_parent:
            view_before = self._view(src_parent, batch_id)
            src_visible = view_before.index(src_node)
            self._array_remove(src_parent, record, src_node)
            if dst_key == '-':
                adjusted = len(view_before) - 1
            else:
                dst_visible = self._array_index(dst_key)
                adjusted = (dst_visible
                            if dst_visible <= src_visible
                            else dst_visible - 1)
            self._array_insert(src_parent, adjusted, record, src_node)
        else:
            if isinstance(src_parent, _Array):
                self._array_remove(src_parent, record, src_node)
            else:
                del src_parent[src_key]
            if isinstance(dst_parent, _Array):
                index = (len(self._view(dst_parent, batch_id))
                         if dst_key == '-'
                         else self._array_index(dst_key))
                self._array_insert(dst_parent, index, record, src_node)
            elif dst_node is not None:
                dst_node.value = src_node.value
                dst_node.creator = src_node.creator
                dst_node.mover = (record['batch'], record['sequence'])
                dst_node.writers.append(record)
            else:
                dst_parent[dst_key] = src_node
        src_node.mover = (record['batch'], record['sequence'])
        self._origin_record[(record['batch'], record['sequence'])] = record
        src_node.writers.append(record)

    def _replay_copy(self, record, dst_parts, src_parts):
        src_node, _, _ = self._resolve(src_parts, record['batch'])
        if src_node is None:
            raise JsonPointerException("can't copy from a missing location")
        self._replay_add(record, dst_parts, _unwrap(src_node.value))

    def _replay_test(self, record, parts, expected):
        node, _, _ = self._resolve(parts, record['batch'])
        actual = None if node is None else _unwrap(node.value)
        if not _json_equal(actual, expected):
            conflict = Conflict(Conflict.TEST_FAILED, [record])
            self._conflicts.append(conflict)
            self._excluded[_op_key(record)] = conflict
            self._failed_parts.append((tuple(parts), conflict))

    def _sorted_conflicts(self):
        def key(conflict):
            sides = tuple(sorted(_op_key(record)
                                 for record in conflict.operations))
            dependents = tuple(sorted(_op_key(record)
                                      for record in conflict.dependents))
            return sides, conflict.reason, dependents

        return sorted(self._conflicts, key=key)

class PatchMerger:
    """Merges RFC 6902 batches produced offline from one JSON baseline.

    A batch is a JSON object with the members:

    ``id``
        A stable, hashable batch identifier.
    ``operations``
        A list of RFC 6902 operation objects. Each operation may carry an
        integer ``seq`` member; when it is missing, the position of the
        operation in the list is used as its sequence number. Sequence
        numbers must be unique within a batch.
    ``parents``
        The ids of the batches whose effects this batch builds on. Parents
        may arrive later than the batch. Optional, defaults to no parents.

    Batches can be fed to :meth:`receive` one at a time, in any order and
    more than once. :meth:`merge` replays every batch whose whole parent
    chain is known into the baseline; call it again after more batches have
    arrived. The result only depends on the set of batches, never on the
    receive order: two mergers that receive the same batches, or one merger
    fed the batches in two groups that are joined with :meth:`absorb`, end
    up with identical documents, conflicts and canonical patches.

    >>> baseline = {'items': [1, 1], 'name': 'base'}
    >>> merger = PatchMerger(baseline)
    >>> merger.receive({'id': 'a', 'operations': [
    ...     {'op': 'add', 'path': '/items/1', 'value': 2, 'seq': 0}]})
    >>> merger.receive({'id': 'b', 'operations': [
    ...     {'op': 'add', 'path': '/items/1', 'value': 3, 'seq': 0}]})
    >>> result = merger.merge()
    >>> result.document
    {'items': [1, 2, 3, 1], 'name': 'base'}
    """

    def __init__(self, baseline, pointer_cls=JsonPointer):
        self.pointer_cls = pointer_cls
        self._baseline = copy.deepcopy(baseline)
        # batch id -> raw batch dict
        self._batches = {}
        # batch id -> canonical fingerprint, for duplicate detection
        self._fingerprints = {}
        # batch id -> list of operation records
        self._operations = {}
        # batch id -> tuple of parent ids
        self._parents = {}
        self._reset_result()

    def _reset_result(self):
        self._merged = False
        self._document = None
        self._conflicts = []
        self._canonical = None
        self._order = []

    # -- receiving -------------------------------------------------------

    def receive(self, batch):
        """Adds one batch, storing it for the next :meth:`merge`.

        :raises InvalidBatch: if the batch lacks an id or valid operations.
        :raises DuplicateBatchError: if its id was received before with
            different content.
        """
        if not isinstance(batch, Mapping) or 'id' not in batch:
            raise InvalidBatch("a batch must be an object with an 'id' member")
        batch_id = batch['id']
        try:
            hash(batch_id)
        except TypeError:
            raise InvalidBatch("batch ids must be hashable")
        if 'operations' not in batch \
                or isinstance(batch['operations'], (str, bytes)):
            raise InvalidBatch(
                "batch {0!r} has no 'operations' list".format(batch_id))
        try:
            raw_ops = list(batch['operations'])
        except TypeError:
            raise InvalidBatch(
                "batch {0!r}: 'operations' must be a list".format(batch_id))
        parents_raw = batch.get('parents', ())
        if isinstance(parents_raw, (str, bytes)) \
                or not isinstance(parents_raw, Sequence):
            raise InvalidBatch(
                "batch {0!r}: 'parents' must be a list of ids".format(batch_id))
        parents = tuple(dict.fromkeys(parents_raw))

        records = []
        seen_sequences = set()
        for position, op in enumerate(raw_ops):
            if not isinstance(op, Mapping) or 'op' not in op \
                    or 'path' not in op:
                raise InvalidBatch(
                    "batch {0!r}: operation {1} is not a valid RFC 6902 "
                    "operation".format(batch_id, position))
            if op['op'] not in ('add', 'remove', 'replace', 'move',
                                'copy', 'test'):
                raise InvalidBatch(
                    "batch {0!r}: unknown operation {1!r}"
                    .format(batch_id, op['op']))
            sequence = op.get('seq', position)
            if not isinstance(sequence, int) or isinstance(sequence, bool):
                raise InvalidBatch(
                    "batch {0!r}: sequence numbers must be integers"
                    .format(batch_id))
            if sequence in seen_sequences:
                raise InvalidBatch(
                    "batch {0!r}: duplicate sequence number {1}"
                    .format(batch_id, sequence))
            seen_sequences.add(sequence)
            records.append(_record(batch_id, sequence, dict(op)))

        fingerprint = repr((
            sorted((record['sequence'],
                    _canonical_op(record['op'])) for record in records),
            sorted(parents, key=_sort_key),
        ))
        if batch_id in self._batches:
            if self._fingerprints[batch_id] != fingerprint:
                raise DuplicateBatchError(
                    "batch {0!r} was received again with different content"
                    .format(batch_id))
            return
        self._batches[batch_id] = copy.deepcopy(dict(batch))
        self._fingerprints[batch_id] = fingerprint
        self._operations[batch_id] = records
        self._parents[batch_id] = parents
        self._reset_result()

    def receive_many(self, batches):
        """Adds several batches, see :meth:`receive`."""
        for batch in batches:
            self.receive(batch)

    # -- scheduling ------------------------------------------------------

    def _schedulable(self):
        """The batches whose whole parent chain is part of the graph."""
        known = set(self._batches)
        schedulable = set()
        for batch_id in known:
            pending = [batch_id]
            seen = set()
            ok = True
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                if current not in known:
                    ok = False
                    break
                pending.extend(self._parents[current])
            if ok:
                schedulable.add(batch_id)
        return schedulable

    def _topological_order(self, batch_ids):
        """Parents first, ties broken by batch id. Cycles are returned as a
        causal-cycle conflict instead of raising."""
        indegree = {batch_id: 0 for batch_id in batch_ids}
        children = {batch_id: [] for batch_id in batch_ids}
        for batch_id in batch_ids:
            for parent in self._parents[batch_id]:
                if parent in indegree:
                    indegree[batch_id] += 1
                    children[parent].append(batch_id)
        ready = deque(sorted(
            (batch_id for batch_id, degree in indegree.items()
             if degree == 0), key=_sort_key))
        order = []
        while ready:
            batch_id = ready.popleft()
            order.append(batch_id)
            for child in sorted(children[batch_id], key=_sort_key):
                indegree[child] -= 1
                if indegree[child] == 0:
                    bisect.insort(ready, child, key=_sort_key)
        conflicts = []
        if len(order) != len(batch_ids):
            stuck = sorted(
                (batch_id for batch_id in batch_ids if batch_id not in order),
                key=_sort_key)
            conflicts.append(Conflict(
                Conflict.CAUSAL_CYCLE,
                [{'batch': batch_id, 'sequence': None, 'op': None}
                 for batch_id in stuck]))
        return order, conflicts

    def _ancestor_map(self, order):
        ancestors = {}
        for batch_id in order:
            chain = set()
            for parent in self._parents[batch_id]:
                chain.add(parent)
                chain.update(ancestors.get(parent, ()))
            ancestors[batch_id] = chain
        return ancestors

    # -- merging ---------------------------------------------------------

    def merge(self):
        """Replays all known, causally complete batches into the baseline.

        Re-running it after more batches arrived recomputes the result from
        the whole batch graph. Returns this merger, so callers can read
        :attr:`document`, :attr:`conflicts` and :attr:`canonical_patch`.
        """
        if self._merged:
            return self

        schedulable = self._schedulable()
        graph_conflicts = []
        for batch_id in sorted(set(self._batches) - schedulable,
                               key=_sort_key):
            missing = sorted(
                (parent for parent in self._parents[batch_id]
                 if parent not in self._batches), key=_sort_key)
            graph_conflicts.append(Conflict(
                Conflict.UNKNOWN_PARENT,
                [{'batch': batch_id, 'sequence': None, 'op': None}],
                [{'batch': parent, 'sequence': None, 'op': None}
                 for parent in missing]))

        order, cycle_conflicts = self._topological_order(schedulable)
        graph_conflicts.extend(cycle_conflicts)
        self._order = order

        scheduled = []
        ranks = {batch_id: rank for rank, batch_id in enumerate(order)}
        for batch_id in order:
            scheduled.extend(self._operations[batch_id])
        scheduled.sort(key=lambda record: (
            ranks.get(record['batch'], len(ranks)),
            _sort_key(record['batch']),
            record['sequence']))

        engine = _Engine(self._baseline, self._ancestor_map(order))
        engine.simulate(scheduled)
        engine.detect()
        op_conflicts = engine.replay(scheduled)

        self._document = _unwrap(engine.root.value)
        self._canonical = list(make_patch(
            self._baseline, self._document,
            pointer_cls=self.pointer_cls))
        self._conflicts = graph_conflicts + op_conflicts
        self._merged = True
        return self

    # -- joining two mergers ---------------------------------------------

    def absorb(self, other):
        """Folds another merger's batches into this one.

        Both mergers must have been started from an equal baseline.
        Afterwards this merger contains the union of both batch graphs, so
        merging it converges with a single merger that received every batch.
        """
        if not _json_equal(self._baseline, other._baseline):
            raise InvalidBatch("can only absorb mergers of the same baseline")
        for batch_id in sorted(other._batches, key=_sort_key):
            if batch_id in self._batches:
                if self._fingerprints[batch_id] \
                        != other._fingerprints[batch_id]:
                    raise DuplicateBatchError(
                        "batch {0!r} differs between the two mergers"
                        .format(batch_id))
                continue
            self._batches[batch_id] = other._batches[batch_id]
            self._fingerprints[batch_id] = other._fingerprints[batch_id]
            self._operations[batch_id] = copy.deepcopy(
                other._operations[batch_id])
            self._parents[batch_id] = other._parents[batch_id]
        self._reset_result()
        return self

    # -- results ---------------------------------------------------------

    @property
    def document(self):
        """The merged document of all non-conflicting operations."""
        if not self._merged:
            self.merge()
        return self._document

    @property
    def conflicts(self):
        """The conflicts of the last :meth:`merge`, in canonical order."""
        if not self._merged:
            self.merge()
        return list(self._conflicts)

    @property
    def canonical_patch(self):
        """An RFC 6902 patch turning the baseline into :attr:`document`.

        It is canonical: every receive order of the same batch graph yields
        the same operations, and it replays cleanly against the common
        baseline.
        """
        if not self._merged:
            self.merge()
        return list(self._canonical)

    @property
    def batch_order(self):
        """The batch ids in the causal order used for replay."""
        if not self._merged:
            self.merge()
        return list(self._order)

    def replay_canonical(self):
        """Applies :attr:`canonical_patch` to a fresh copy of the baseline."""
        return apply_patch(self._baseline, self.canonical_patch,
                           pointer_cls=self.pointer_cls)


def _canonical_op(op):
    """Renders one operation with sorted members, for batch fingerprints."""
    return tuple(sorted(
        (key, _canonical_value(value)) for key, value in op.items()))


def _canonical_value(value):
    if isinstance(value, Mapping):
        return tuple(sorted(
            (_canonical_value(key), _canonical_value(member))
            for key, member in value.items()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_canonical_value(member) for member in value)
    return value


def _sort_key(value):
    """A total ordering over JSON-ish batch ids, so ties never depend on
    incomparable types of heterogeneous ids."""
    return _sortable(value)


def _sortable(value):
    if isinstance(value, Mapping):
        return ('dict', tuple(sorted(
            (_sortable(key), _sortable(member))
            for key, member in value.items())))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ('list', tuple(_sortable(member) for member in value))
    return (type(value).__name__, value)


def merge_batches(baseline, batches, pointer_cls=JsonPointer):
    """Convenience wrapper: build a :class:`PatchMerger`, receive all batches
    and merge once.

    >>> result = merge_batches([1], [
    ...     {'id': 'a', 'operations': [
    ...         {'op': 'add', 'path': '/-', 'value': 2}]},
    ...     {'id': 'b', 'operations': [
    ...         {'op': 'add', 'path': '/-', 'value': 3}]}])
    >>> result.document
    [1, 2, 3]
    """
    merger = PatchMerger(baseline, pointer_cls=pointer_cls)
    merger.receive_many(batches)
    return merger.merge()
