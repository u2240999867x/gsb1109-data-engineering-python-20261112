# vim:ts=4 sw=4 expandtab softtabstop=4
import copy

from jsonmerge.exceptions import HeadInstanceError, \
                                 BaseInstanceError, \
                                 SchemaError, \
                                 PendingIdError
from jsonmerge.jsonvalue import JSONValue
import jsonschema
import re

class Strategy(object):
    """Base class for merge strategies.
    """

    def merge(self, walk, base, head, schema, **kwargs):
        """Merge head instance into base.

        walk -- WalkInstance object for the current context.
        base -- JSONValue being merged into.
        head -- JSONValue being merged.
        schema -- Schema used for merging (also JSONValue)
        kwargs -- Dict with any extra options given in the 'mergeOptions'
        keyword

        Specific merge strategies should override this method to implement
        their behavior.

        The function should return the object resulting from the merge.

        Recursion into the next level, if necessary, is achieved by calling
        walk.descend() method.
        """
        raise NotImplemented

    def get_schema(self, walk, schema, **kwargs):
        """Return the schema for the merged document.

        walk -- WalkSchema object for the current context.
        schema -- Original document schema.
        kwargs -- Dict with any extra options given in the 'mergeOptions'
        keyword.

        Specific merge strategies should override this method to modify the
        document schema depending on the behavior of the merge() method.

        The function should return the schema for the object resulting from the
        merge.

        Recursion into the next level, if necessary, is achieved by calling
        walk.descend() method.
        """
        raise NotImplemented

    def _resolve_ref(self, walk, item, ref):
        if walk.is_type(JSONValue(ref), 'array'):
            resolved = [ walk.resolver.resolve_fragment(item.val, i) for i in ref ]
        else:
            resolved = walk.resolver.resolve_fragment(item.val, ref)

        return resolved

class Overwrite(Strategy):
    def merge(self, walk, base, head, schema, **kwargs):
        return head

    def get_schema(self, walk, schema, **kwargs):
        return schema

class Discard(Strategy):
    def merge(self, walk, base, head, schema, keepIfUndef=False, **kwargs):
        if base.is_undef() and keepIfUndef:
            return head
        else:
            return base

    def get_schema(self, walk, schema, **kwargs):
        return schema

class Version(Strategy):

    def add_metadata(self, head, metadata):
        if metadata is None:
            rv = dict()
        else:
            rv = dict(metadata)

        rv['value'] = head
        return rv

    def merge(self, walk, base, head, schema, limit=None, unique=None, ignoreDups=True, metadata=None, **kwargs):

        # backwards compatibility
        if unique is False:
            ignoreDups = False

        if metadata is not None:
            if not walk.is_type(JSONValue(val=metadata), "object"):
                raise SchemaError("'metadata' option does not contain an object")

        if base.is_undef():
            base = JSONValue(val=[], ref=base.ref)
            last_entry = JSONValue(undef=True)
        else:
            if not walk.is_type(base, "array"):
                raise BaseInstanceError("Base is not an array. "
                        "Base not previously generated with this strategy?", base)

            base = JSONValue(list(base.val), base.ref)

            if base.val:
                last_entry = base[-1]

                if not walk.is_type(last_entry, "object"):
                    raise BaseInstanceError("Last entry in the versioned array is not an object. "
                            "Base not previously generated with this strategy?", last_entry)

                if 'value' not in last_entry.val:
                    raise BaseInstanceError("Last entry in the versioned array has no 'value' property. "
                            "Base not previously generated with this strategy?", last_entry)
            else:
                last_entry = JSONValue(undef=True)

        if not ignoreDups or last_entry.is_undef() or last_entry['value'].val != head.val:
            base.val.append(self.add_metadata(head.val, metadata))
            if limit is not None:
                base.val = base.val[-limit:]

        return base

    def get_schema(self, walk, schema, limit=None, metadataSchema=None, **kwargs):

        if metadataSchema is not None:
            item = dict(walk.resolve_subschema_option_refs(metadataSchema))
        else:
            item = {}

        if 'properties' not in item:
            item['properties'] = {}
        else:
            item['properties'] = dict(item['properties'])

        item['properties']['value'] = schema.val

        rv = {  "type": "array",
                "items": item }

        if limit is not None:
            rv['maxItems'] = limit

        return JSONValue(rv, schema.ref)

class ArrayStrategy(Strategy):
    def merge(self, walk, base, head, schema, **kwargs):
        if not walk.is_type(head, "array"):
            raise HeadInstanceError("Head is not an array", head)

        if base.is_undef():
            base = JSONValue([], base.ref)
        else:
            if not walk.is_type(base, "array"):
                raise BaseInstanceError("Base is not an array", base)

            base = JSONValue(list(base.val), base.ref)

        return self._merge(walk, base, head, schema, **kwargs)

    def default_key(self):
        # This object always sorts after other items
        class UnknownKey:
            def __lt__(self, other):
                return False

            def __gt__(self, other):
                if isinstance(other, UnknownKey):
                    return False
                else:
                    return True

        return UnknownKey()

    def sort_array(self, walk, base, sortByRef, sortReverse):
        assert walk.is_type(base, "array")

        if sortByRef is None:
            return

        def key(item):
            try:
                return self._resolve_ref(walk, item, sortByRef)
            except jsonschema.RefResolutionError:
                return self.default_key()

        base.sort(key=key, reverse=bool(sortReverse))

class Append(ArrayStrategy):
    def _merge(self, walk, base, head, schema, sortByRef=None, sortReverse=None, **kwargs):
        base.val += head.val

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_schema(self, walk, schema, **kwargs):
        schema.val.pop('maxItems', None)
        schema.val.pop('uniqueItems', None)

        return schema

class ArrayMergeById(ArrayStrategy):

    def get_key(self, walk, item, idRef):
        return self._resolve_ref(walk, item, idRef)

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                continue

            yield i, key, item

    # Return True if a resolved key is not a usable ID. The base strategy
    # treats every value the idRef pointer resolves to (including JSON null)
    # as a real key, so this is False by default. The patching subclass
    # overrides it to treat an explicit null as "no ID yet".
    def _key_unresolvable(self, key):
        return False

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None, sortByRef=None, sortReverse=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        for i, key_1, item_1 in self.iter_index_key_item(walk, head, idRef):
            for j, key_2, item_2 in self.iter_index_key_item(walk, head, idRef):
                if j < i:
                    if key_1 == key_2:
                        raise HeadInstanceError("Id '%s' was not unique in head" % (key_1,), item_1)
                else:
                    break

        for i, head_key, head_item in self.iter_index_key_item(walk, head, idRef):

            if head_key == ignoreId:
                continue

            matching_j = []
            for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):

                if base_key == head_key:
                    matching_j.append(j)
                    matched_item = base_item

            if len(matching_j) == 1:
                # If there was exactly one match, we replace it with a merged item
                j = matching_j[0]
                base[j] = walk.descend(subschema, matched_item, head_item)
            elif len(matching_j) == 0:
                # If there wasn't a match, we append a new object
                base.append(walk.descend(subschema, JSONValue(undef=True), head_item))
            else:
                j = matching_j[1]
                raise BaseInstanceError("Id '%s' was not unique in base" % (base_key,), base[j])

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base

    def get_schema(self, walk, schema, **kwargs):
        subschema = schema.get('items')
        if not subschema.is_undef():
            schema['items'] = walk.descend(subschema)

        return schema


class PendingMergeState(object):
    """Bookkeeping for one array using the patching strategy.

    The state lives on the Merger (not on the per-merge Walk) so it survives
    across a series of merge() calls and the later backfill() calls. It is
    keyed by the schema ref of the array, so distinct arrays never share a
    journal.

    Every head row that carries an ID is recorded as a merge event in its
    original source order. Rows without an ID are recorded as pending events
    and quarantined. Backfill resolves pending events and rebuilds the array
    by replaying the whole event log from the original base, which preserves
    the original event sequence (and hence version numbers) without rewriting
    any unrelated entity.
    """

    def __init__(self):
        self.batch = 0
        self.counter = 0
        self.instance_ref = '#'
        self.schema = None
        self.initial_base = None
        self.events = []
        self.quarantine = []
        self.digested = set()
        self.idRef = "id"
        self.ignoreId = None
        self.sortByRef = None
        self.sortReverse = None


class PendingRef(object):
    """Handle to a quarantined row, handed out in the quarantine journal."""

    def __init__(self, token, batch=None, row=None, reason=None):
        self.token = token
        self.batch = batch
        self.row = row
        self.reason = reason

    def __repr__(self):
        return "PendingRef(%r)" % (self.token,)

    def __eq__(self, other):
        return isinstance(other, PendingRef) and other.token == self.token

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.token)


class PendingArrayMergeById(ArrayMergeById):
    """arrayMergeById variant that quarantines head items without a usable ID.

    Head items whose ID is missing (the idRef pointer cannot be resolved) or
    explicitly null are not silently dropped. They are stored in a quarantine
    journal together with the batch and the source row they arrived in, the
    reason they could not be matched and their ordinal position in the event
    sequence. All rows (keyed and pending) are recorded in source order, so
    once the missing ID is supplied the row can be replayed at its original
    position via IdPatchMerger.
    """

    MISSING = "missing-id"
    NULL = "null-id"

    def _key_unresolvable(self, key):
        # An explicit JSON null means "ID not known yet", so the row is
        # quarantined instead of being merged under the null key.
        return key is None

    def _states(self, walk):
        merger = walk.merger
        states = getattr(merger, '_pending_states', None)
        if states is None:
            states = {}
            merger._pending_states = states
        return states

    def _state_for(self, walk, schema):
        states = self._states(walk)
        state = states.get(schema.ref)
        if state is None:
            state = PendingMergeState()
            states[schema.ref] = state
        return state

    def _new_ref(self, state, index, reason):
        state.counter += 1
        token = "%s/pending-%d" % (state.schema.ref, state.counter)
        return PendingRef(token, batch=state.batch, row=index, reason=reason)

    def _record_pending(self, state, index, item, reason):
        ref = self._new_ref(state, index, reason)
        event = {
            'kind': 'pending',
            'ref': ref,
            'batch': state.batch,
            'row': index,
            'reason': reason,
            'event': len(state.events),
            'item': copy.deepcopy(item.val),
        }
        state.events.append(event)
        state.quarantine.append(event)
        return event

    def _merge(self, walk, base, head, schema, idRef="id", ignoreId=None,
            sortByRef=None, sortReverse=None, **kwargs):
        subschema = schema.get('items')

        if walk.is_type(subschema, "array"):
            raise SchemaError("This strategy is not supported when 'items' is an array", subschema)

        state = self._state_for(walk, schema)
        state.batch += 1
        state.schema = schema
        state.instance_ref = head.ref
        state.idRef = idRef
        state.ignoreId = ignoreId
        state.sortByRef = sortByRef
        state.sortReverse = sortReverse

        if state.initial_base is None:
            state.initial_base = copy.deepcopy(base.val)

        # Record every head row in source order: keyed rows become merge
        # events, rows without a usable ID are quarantined.
        batch_events = []
        for index, item in enumerate(head):
            try:
                key = self.get_key(walk, item, idRef)
            except jsonschema.RefResolutionError:
                self._record_pending(state, index, item, self.MISSING)
                continue

            if self._key_unresolvable(key):
                self._record_pending(state, index, item, self.NULL)
                continue

            if key == ignoreId:
                continue

            event = {
                'kind': 'merge',
                'batch': state.batch,
                'row': index,
                'event': len(state.events),
                'key': key,
                'item': copy.deepcopy(item.val),
            }
            state.events.append(event)
            batch_events.append(event)

        # Head uniqueness check within this batch, mirroring arrayMergeById.
        for i, event_1 in enumerate(batch_events):
            for event_2 in batch_events[:i]:
                if event_1['key'] == event_2['key']:
                    item_1 = JSONValue(event_1['item'],
                            '%s/%d' % (head.ref, event_1['row']))
                    raise HeadInstanceError(
                            "Id '%s' was not unique in head" % (event_1['key'],),
                            item_1)

        # Apply the keyed events immediately, exactly like arrayMergeById.
        for event in batch_events:
            # Use a view into the live head document so that the walk's
            # resolver identity assertions hold. The stored event copy is
            # only used for later rebuilds.
            head_item = head[event['row']]
            head_key = event['key']

            matching_j = []
            for j, base_key, base_item in self.iter_index_key_item(walk, base, idRef):

                if self._key_unresolvable(base_key):
                    continue

                if base_key == head_key:
                    matching_j.append(j)
                    matched_item = base_item

            if len(matching_j) == 1:
                j = matching_j[0]
                base[j] = walk.descend(subschema, matched_item, head_item)
            elif len(matching_j) == 0:
                base.append(walk.descend(subschema, JSONValue(undef=True), head_item))
            else:
                j = matching_j[1]
                raise BaseInstanceError("Id '%s' was not unique in base" % (base_key,), base[j])

        self.sort_array(walk, base, sortByRef, sortReverse)

        return base


class IdPatchMerger(object):
    """Backfill entry point for rows quarantined by PendingArrayMergeById.

    A patch supplies the ID that was missing when a supplier row first
    arrived. The resolved row is converted into a merge event at its original
    position in the event log, after which the affected array is rebuilt by
    replaying every event from the original base. Rows that are still
    quarantined are skipped. Because replay follows the original event order,
    version numbers of unrelated entities come out identical and are never
    rewritten, and the rebuilt document matches a one-shot merge performed
    with complete IDs.
    """

    def __init__(self, merger):
        self.merger = merger
        self._strategy = self.merger.strategies['patchingArrayMergeById']
        self.states = getattr(merger, '_pending_states', None)
        if not self.states:
            raise PendingIdError(
                    "Merger schema does not use the 'patchingArrayMergeById' "
                    "merge strategy, so there is nothing to backfill")

    def quarantine(self):
        """Return a copy of every open quarantine journal, grouped by array."""
        return {
            state.instance_ref: [dict(entry) for entry in state.quarantine]
            for state in self.states.values()
        }

    def pending_refs(self):
        """Return all open pending references, in arrival order."""
        refs = []
        for state in self.states.values():
            refs.extend(entry['ref'] for entry in state.quarantine)
        return refs

    def _find_pending(self, ref):
        for state in self.states.values():
            for entry in state.quarantine:
                if entry['ref'] == ref:
                    return state, entry
        return None, None

    def backfill(self, base, patches):
        """Replay quarantined rows once their IDs are known.

        base -- document most recently returned by Merger.merge().
        patches -- mapping of PendingRef to the backfilled ID, or an iterable
                   of (ref, id) pairs.

        Each pending reference is digested at most once: resubmitting the
        same patch is reported as a duplicate and changes nothing. When a
        backfilled ID collides with more than one existing row, nothing is
        merged and the colliding original rows are listed in the report
        instead of raising an exception; the reference stays open and can be
        patched again. IDs resolving to a single row (or no row) are merged
        or appended following the ordinary arrayMergeById rules.

        Returns a (base, report) tuple.
        """

        if isinstance(patches, dict):
            patches = list(patches.items())
        else:
            patches = list(patches)

        report = []
        # state -> list of validated (entry, new_id, item, status, collisions)
        planned = {}

        for ref, new_id in patches:
            if not isinstance(ref, PendingRef):
                raise PendingIdError("Patch key %r is not a pending reference" % (ref,))

            state, entry = self._find_pending(ref)

            if entry is None:
                if any(ref.token in state.digested for state in self.states.values()):
                    report.append(self._report_entry(ref, new_id, None, 'duplicate', []))
                    continue
                raise PendingIdError("Unknown pending reference %r" % (ref,))

            if ref.token in state.digested:
                report.append(self._report_entry(ref, new_id, entry, 'duplicate', []))
                continue

            matches = self._current_matches(state, base, new_id)
            collisions = [copy.deepcopy(match) for match in matches]

            if len(matches) > 1:
                # Ambiguous ID: keep the row quarantined and list the
                # original rows that collide.
                report.append(self._report_entry(
                    ref, new_id, entry, 'ambiguous', collisions))
                continue

            item = copy.deepcopy(entry['item'])
            item = self._set_id(state, item, new_id)
            status = 'merged' if len(matches) == 1 else 'appended'

            planned.setdefault(state, []).append(
                    (entry, new_id, item, status, collisions, ref))

        # Validate source-batch uniqueness before mutating any state, so a
        # rejected patch leaves the merger and document untouched.
        for state, changes in planned.items():
            keys_by_batch = {}
            for event in state.events:
                if event['kind'] == 'merge':
                    keys_by_batch.setdefault(event['batch'], []).append(event['key'])
            for entry, new_id, item, _, _, _ in changes:
                keys_by_batch.setdefault(entry['batch'], []).append(new_id)

            for keys in keys_by_batch.values():
                if len(keys) != len(set(keys)):
                    offending = next(
                            change for change in changes
                            if keys.count(change[1]) > 1)
                    entry, offending_id, item, _, _, _ = offending
                    item_jv = JSONValue(item,
                            '%s/%d' % (state.instance_ref, entry['row']))
                    raise HeadInstanceError(
                            "Id '%s' was not unique in head" % (offending_id,),
                            item_jv)

        # Commit the validated patches and rebuild each affected array once.
        rebuild_states = set()
        for state, changes in planned.items():
            for entry, new_id, item, status, collisions, ref in changes:
                entry['kind'] = 'merge'
                entry['key'] = new_id
                entry['item'] = item
                entry.pop('reason', None)

                state.digested.add(ref.token)
                position = next(
                        i for i, quarantined in enumerate(state.quarantine)
                        if quarantined['ref'] == ref)
                del state.quarantine[position]

                report.append(self._report_entry(
                    ref, new_id, entry, status, collisions))
            rebuild_states.add(state)

        for state in rebuild_states:
            base = self._rebuild(state, base)

        return base, report

    def _report_entry(self, ref, new_id, entry, status, collisions):
        if entry is None:
            return {
                'ref': ref,
                'id': new_id,
                'status': status,
                'batch': None,
                'row': None,
                'reason': None,
                'event': None,
                'collisions': collisions,
            }
        return {
            'ref': ref,
            'id': new_id,
            'status': status,
            'batch': entry.get('batch'),
            'row': entry.get('row'),
            'reason': entry.get('reason'),
            'event': entry.get('event'),
            'collisions': collisions,
        }

    def _set_id(self, state, item, new_id):
        id_ref = state.idRef
        if isinstance(id_ref, list):
            raise PendingIdError(
                    "Backfill is not supported for compound idRef %r" % (id_ref,))

        pointer = id_ref.lstrip('/')
        if pointer == '':
            # Root pointer: the item itself is the ID (arrays of scalars).
            if isinstance(item, (dict, list)):
                raise PendingIdError(
                        "Cannot backfill an ID on a composite item with root idRef '/'")
            return new_id

        target = item
        parts = [part.replace('~1', '/').replace('~0', '~')
                for part in pointer.split('/')]
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        target[parts[-1]] = new_id
        return item

    def _navigate(self, jv, ref):
        pointer = ref.lstrip('#')
        current = jv
        for part in pointer.split('/'):
            if not part:
                continue
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(current.val, list):
                part = int(part)
            current = current[part]
        return current

    def _current_matches(self, state, base_doc, new_id):
        from jsonmerge import WalkInstance

        try:
            array = self._navigate(JSONValue(base_doc), state.instance_ref)
        except (KeyError, IndexError):
            return []

        walk = WalkInstance(self.merger,
                JSONValue(base_doc), JSONValue(base_doc), {})

        matches = []
        for j, key, item in self._strategy.iter_index_key_item(
                walk, array, state.idRef):
            if self._strategy._key_unresolvable(key):
                continue
            if key == new_id:
                matches.append(item.val)
        return matches

    def _rebuild(self, state, base_doc):
        from jsonmerge import WalkInstance

        schema = state.schema
        subschema = schema.get('items')

        merge_events = [event for event in state.events
                if event['kind'] == 'merge']
        head_items = [event['item'] for event in merge_events]

        # Root the walk at the target array (not the whole document), exactly
        # like a top-level array merge. The merger's schema resolver still
        # resolves item subschemas, including external $ref, via the schema.
        # The rebuilt array and the walk's base resolver must share the same
        # list object so items appended during replay remain resolvable.
        initial = copy.deepcopy(state.initial_base)
        walk = WalkInstance(self.merger,
                JSONValue(initial), JSONValue(head_items), {})

        rebuilt = JSONValue(initial)
        head_array = JSONValue(head_items)

        # Per-batch head uniqueness, mirroring arrayMergeById semantics.
        event_position = {id(event): pos
                for pos, event in enumerate(merge_events)}
        batches = {}
        for event in merge_events:
            batches.setdefault(event['batch'], []).append(event)
        for batch_events in batches.values():
            for i, event_1 in enumerate(batch_events):
                for event_2 in batch_events[:i]:
                    if event_1['key'] == event_2['key']:
                        item_1 = head_array[event_position[id(event_1)]]
                        raise HeadInstanceError(
                                "Id '%s' was not unique in head" % (event_1['key'],),
                                item_1)

        for position, event in enumerate(merge_events):
            # View into the live head array so resolver identity holds.
            head_item = head_array[position]
            head_key = event['key']

            matching_j = []
            for j, base_key, base_item in \
                    self._strategy.iter_index_key_item(walk, rebuilt, state.idRef):

                if self._strategy._key_unresolvable(base_key):
                    continue

                if base_key == head_key:
                    matching_j.append(j)
                    matched_item = base_item

            if len(matching_j) == 1:
                j = matching_j[0]
                rebuilt[j] = walk.descend(subschema, matched_item, head_item)
            elif len(matching_j) == 0:
                rebuilt.append(walk.descend(subschema, JSONValue(undef=True), head_item))
            else:
                j = matching_j[1]
                raise BaseInstanceError(
                        "Id '%s' was not unique in base" % (base_key,), rebuilt[j])

        self._strategy.sort_array(walk, rebuilt,
                state.sortByRef, state.sortReverse)

        if state.instance_ref == '#':
            return rebuilt.val

        parent_ref, _, last = state.instance_ref.rpartition('/')
        parent = self._navigate(JSONValue(base_doc), parent_ref or '#')
        last = last.replace('~1', '/').replace('~0', '~')
        if isinstance(parent.val, list):
            last = int(last)
        parent.val[last] = rebuilt.val
        return base_doc


class ArrayMergeByIndex(ArrayMergeById):

    def iter_index_key_item(self, walk, jv, idRef):
        for i, item in enumerate(jv):
            yield i, i, item


class ObjectMerge(Strategy):
    """A Strategy for merging objects.

    Resulting objects have properties from both base and head. Any
    properties that are present both in base and head are merged based
    on the strategy specified further down in the hierarchy (e.g. in
    properties, patternProperties or additionalProperties schema
    keywords). 

    walk -- WalkInstance object for the current context.
    base -- JSONValue being merged into.
    head -- JSONValue being merged.
    schema -- Schema used for merging (also JSONValue)
    objclass_menu -- A dictionary of classes to use as a JSON object.
    kwargs -- Any extra options given in the 'mergeOptions' keyword.

    objclass_menu should be a dictionary that maps a string name to a function
    or class that will return an empty dictionary-like object to use as a JSON
    object.  The function must accept either no arguments or a dictionary-like
    object.  The name '_default' represents the default object to use if not
    overridden by the objClass option.

    One mergeOption is supported:

    objClass -- a name for the class to use as a JSON object in the output.
    """
    def merge(self, walk, base, head, schema, objclass_menu=None, objClass='_default', **kwargs):
        if not walk.is_type(head, "object"):
            raise HeadInstanceError("Head is not an object", head)

        if objclass_menu is None:
            objclass_menu = { '_default': dict }

        objcls = objclass_menu.get(objClass)
        if objcls is None:
            raise SchemaError("objClass '%s' not recognized" % objClass, schema)

        if base.is_undef():
            base = JSONValue(objcls(), base.ref)
        else:
            if not walk.is_type(base, "object"):
                raise BaseInstanceError("Base is not an object", base)

            base = JSONValue(objcls(base.val), base.ref)

        for k, v in head.items():

            subschema = JSONValue(undef=True)

            # get subschema for this element
            if not schema.is_undef():
                p = schema.get('properties')
                if not p.is_undef():
                    subschema = p.get(k)

                if subschema.is_undef():
                    p = schema.get('patternProperties')
                    if not p.is_undef():
                        for pattern, s in p.items():
                            if re.search(pattern, k):
                                subschema = s

                if subschema.is_undef():
                    p = schema.get('additionalProperties')
                    # additionalProperties can be boolean in draft 4
                    if not p.is_undef() and walk.is_type(p, "object"):
                        subschema = p

            base[k] = walk.descend(subschema, base.get(k), v)

        return base

    def get_schema(self, walk, schema, **kwargs):
        schema2 = JSONValue(dict(schema.val), schema.ref)

        def descend_keyword(keyword):
            p = schema.get(keyword)
            if not p.is_undef():
                for k, v in p.items():
                    schema2[keyword][k] = walk.descend(v)

        descend_keyword("properties")
        descend_keyword("patternProperties")

       # additionalProperties can be boolean in draft 4
        p = schema.get("additionalProperties")
        if not p.is_undef() and walk.is_type(p, "object"):
            schema2["additionalProperties"] = walk.descend(p)

        return schema2
