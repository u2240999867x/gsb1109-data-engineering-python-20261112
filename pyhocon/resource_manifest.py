"""
Resource manifest support.

A :class:`ResourceManifest` freezes the field mapping used by one import run:
every resource that was considered (loaded, missing or forbidden), every
include relationship between them and the resource version (content digest)
that actually supplied each final column.

The manifest is an in-memory object but it is fully JSON serializable, so it
can be handed to another, offline machine which can rebuild the exact same set
of ingestion columns together with their provenance.
"""

import copy
import hashlib
import json
import os
import posixpath
from collections import OrderedDict
from fnmatch import fnmatch
from urllib.parse import urlsplit, urlunsplit

from pyhocon.config_tree import ConfigList, ConfigTree
from pyhocon.exceptions import (ConfigCircularIncludeException,
                                ConfigForbiddenIncludeException)

# Origin markers for columns that do not come from a real resource.
ORIGIN_MISSING = 'missing'
ORIGIN_FORBIDDEN = 'forbidden'

# Include outcomes recorded in the manifest.
INCLUDE_LOADED = 'loaded'
INCLUDE_GLOB = 'glob'
INCLUDE_MISSING = 'missing'
INCLUDE_MISSING_OPTIONAL = 'missing_optional'
INCLUDE_FORBIDDEN = 'forbidden'
INCLUDE_CYCLE = 'cycle'

ORIGIN_ATTR = '_origin_map'
LIST_ORIGIN_ATTR = '_origin_list'
# Lists get materialized into fresh plain lists on read, which would drop a
# parallel origin list attached to them. Element origins are therefore also
# mirrored onto the owning ConfigTree, keyed by the list column.
LIST_MAP_ATTR = '_list_origins_map'
MANIFEST_ATTR = '_manifest'


def normalize_uri(uri):
    """Return a canonical, machine independent identity for a resource.

    Filesystem paths become ``file://`` URIs with normalized separators so
    that two spellings of the same file collapse to one identity while files
    with the same name in different directories stay distinct.
    """
    if uri is None:
        return None
    uri = str(uri)
    if '://' in uri:
        parts = urlsplit(uri)
        if parts.scheme == 'file':
            path = posixpath.normpath(parts.path)
            return urlunsplit((parts.scheme, '', path, '', ''))
        return uri
    return 'file://' + posixpath.normpath(uri.replace(os.sep, '/'))


def digest_content(content):
    """SHA-256 digest of a resource content, usable as its frozen version."""
    if isinstance(content, str):
        content = content.encode('utf-8')
    return hashlib.sha256(content).hexdigest()


def relative_uri(base, target):
    """Resolve ``target`` from the resource that introduces it (``base``).

    Relative references are interpreted from the introducer's location, not
    from the process working directory.
    """
    if target is None:
        return None
    if '://' in target:
        return normalize_uri(target)
    target = target.replace(os.sep, '/')
    if posixpath.isabs(target):
        return normalize_uri(target)
    if base is None:
        return normalize_uri(target)
    if '://' in base:
        parts = urlsplit(base)
        if parts.scheme == 'file':
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(parts.path), target))
            return urlunsplit(('file', '', resolved, '', ''))
    base_path = base[len('file://'):] if base.startswith('file://') else base
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(base_path), target))
    return 'file://' + resolved


class ResourceManifest(object):
    """Frozen description of every resource involved in one configuration run.

    :param forbidden: iterable of resource URIs (or glob patterns) that must
        never be loaded.
    :param digest: callable mapping raw content to a version string.
    """

    def __init__(self, forbidden=None, digest=digest_content):
        self._resources = {}
        self._aliases = {}
        self._includes = []
        self._forbidden = list(forbidden) if forbidden else []
        self._digest = digest
        # Stack of resource URIs currently being expanded, root first.
        self._active_chain = []
        # Full include chain leading to each resource, as a list of URIs.
        self._chains = {}
        self._overrides = []
        # Frozen ``{column: provenance}`` snapshot, set by freeze_columns().
        self._columns = None
        self.root_uri = None

    # -- forbidden sources ---------------------------------------------------

    def is_forbidden(self, uri):
        for pattern in self._forbidden:
            pattern = normalize_uri(pattern)
            if pattern == uri:
                return True
            if any(ch in pattern for ch in ('*', '?', '[')) and fnmatch(uri, pattern):
                return True
        return False

    def reject_forbidden(self, uri, referrer, required=False):
        """Record and raise when ``uri`` matches a forbidden source."""
        if not self.is_forbidden(uri):
            return False
        self._includes.append({
            'referrer': referrer,
            'target': uri,
            'outcome': INCLUDE_FORBIDDEN,
            'required': required,
        })
        raise ConfigForbiddenIncludeException(uri, referrer)

    # -- resource registry ---------------------------------------------------

    def register(self, uri, content=None, aliases=None, missing=False,
                 required=False, referrer=None, glob_pattern=None):
        """Register a resource and return its stored record.

        The same physical resource may be reached through several aliases:
        every alias spelling is remembered but the record (and its version
        digest) is created only once.
        """
        uri = normalize_uri(uri)
        raw_aliases = list(aliases or [])
        for alias in raw_aliases:
            self._aliases[normalize_uri(alias)] = uri
        record = self._resources.get(uri)
        if record is None:
            record = {
                'uri': uri,
                'version': self._digest(content) if content is not None else None,
                'aliases': sorted(set(raw_aliases)),
                'missing': missing,
                'required': required,
                'referrers': [],
                'glob_patterns': [],
            }
            self._resources[uri] = record
        else:
            if content is not None and record['version'] is None:
                record['version'] = self._digest(content)
            record['missing'] = record['missing'] and missing
            record['required'] = record['required'] or required
            record['aliases'] = sorted(set(record['aliases']) | set(raw_aliases))
        if referrer is not None and referrer not in record['referrers']:
            record['referrers'].append(referrer)
        if glob_pattern is not None and glob_pattern not in record['glob_patterns']:
            record['glob_patterns'].append(glob_pattern)
        return record

    def register_missing(self, uri, required, referrer=None, glob_pattern=None):
        """Record a resource that could not be loaded."""
        return self.register(
            normalize_uri(uri), content=None, missing=True, required=required,
            referrer=referrer, glob_pattern=glob_pattern)

    # -- include expansion ---------------------------------------------------

    def begin_include(self, target, referrer, required=False, glob_pattern=None):
        """Open an include edge, guarding against cycles and forbidden sources.

        Returns the canonical target URI. Raises
        :class:`ConfigCircularIncludeException` carrying the concrete resource
        chain when the target is already being expanded.
        """
        target = normalize_uri(target)
        self.reject_forbidden(target, referrer, required=required)

        parent_chain = self._chains.get(referrer) or []
        # Keep the first path that brought a resource in (diamond includes
        # re-encounter it); that is the chain that actually supplied its columns.
        if target not in self._chains:
            self._chains[target] = parent_chain + [target]

        if target in self._active_chain:
            cycle_start = self._active_chain.index(target)
            actual_chain = self._active_chain[cycle_start:] + [target]
            self._includes.append({
                'referrer': referrer,
                'target': target,
                'outcome': INCLUDE_CYCLE,
                'required': required,
                'glob_pattern': glob_pattern,
                'chain': list(actual_chain),
            })
            raise ConfigCircularIncludeException(actual_chain)

        self._active_chain.append(target)
        if self.root_uri is None:
            self.root_uri = target
        return target

    def end_include(self, target):
        target = normalize_uri(target)
        if target in self._active_chain:
            self._active_chain.remove(target)

    def record_include(self, referrer, target, outcome, required=False,
                       glob_pattern=None, chain=None):
        event = {
            'referrer': normalize_uri(referrer) if referrer else None,
            'target': normalize_uri(target) if target else None,
            'outcome': outcome,
            'required': required,
        }
        if glob_pattern is not None:
            event['glob_pattern'] = glob_pattern
        if chain is not None:
            event['chain'] = list(chain)
        self._includes.append(event)
        return event

    def chain_for(self, uri):
        return list(self._chains.get(normalize_uri(uri), []))

    def record_override(self, column, winner_uri, shadowed_uri, chain=None):
        entry = {
            'column': column,
            'winner': winner_uri,
            'shadowed': shadowed_uri,
            'chain': list(chain) if chain else None,
        }
        if entry not in self._overrides:
            self._overrides.append(entry)

    # -- introspection -------------------------------------------------------

    @property
    def current_uri(self):
        """URI of the resource currently being expanded."""
        return self._active_chain[-1] if self._active_chain else None

    @property
    def resources(self):
        return self._resources

    @property
    def includes(self):
        return self._includes

    @property
    def forbidden_patterns(self):
        return list(self._forbidden)

    def version_of(self, uri):
        record = self._resources.get(normalize_uri(uri))
        return record['version'] if record else None

    def missing_resources(self):
        return [r for r in self._resources.values() if r['missing']]

    def overridden_columns(self):
        return [dict(item) for item in self._overrides]

    # -- frozen columns and offline rebuild verification --------------------

    def freeze_columns(self, config):
        """Snapshot the final columns together with their winning resource."""
        self._columns = origin_view(config, self)
        return copy.deepcopy(self._columns)

    @property
    def columns(self):
        return copy.deepcopy(self._columns) if self._columns is not None else None

    @staticmethod
    def diff_columns(expected, actual):
        """Compare two frozen column maps (e.g. across machines).

        Returns a list of differences; an empty list means the same ingestion
        columns with the same provenance would be rebuilt.
        """
        diffs = []
        for column in sorted(set(expected or {}) | set(actual or {})):
            if column not in expected:
                diffs.append({'column': column, 'kind': 'unexpected',
                              'actual': actual[column]})
            elif column not in actual:
                diffs.append({'column': column, 'kind': 'missing',
                              'expected': expected[column]})
            elif expected[column] != actual[column]:
                diffs.append({'column': column, 'kind': 'mismatch',
                              'expected': expected[column],
                              'actual': actual[column]})
        return diffs

    # -- serialization -------------------------------------------------------

    def to_dict(self):
        return {
            'root': self.root_uri,
            'forbidden': list(self._forbidden),
            'resources': copy.deepcopy(self._resources),
            'aliases': dict(self._aliases),
            'includes': copy.deepcopy(self._includes),
            'overrides': copy.deepcopy(self._overrides),
            'columns': copy.deepcopy(self._columns),
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data):
        manifest = cls(forbidden=data.get('forbidden'))
        manifest.root_uri = data.get('root')
        manifest._resources = copy.deepcopy(data.get('resources', {}))
        manifest._aliases = copy.deepcopy(data.get('aliases', {}))
        manifest._includes = copy.deepcopy(data.get('includes', []))
        manifest._overrides = copy.deepcopy(data.get('overrides', []))
        manifest._columns = copy.deepcopy(data.get('columns'))
        return manifest

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))


# ---------------------------------------------------------------------------
# Origin propagation through the parsed config tree
#
# Every ConfigTree carries a parallel ``{key: origin_uri}`` map (dotted keys
# are handled level by level) and every ConfigList a parallel list of origins.
# The parsers stamp entries as they are built, so merges and overrides leave
# the origin of the resource that actually won each column.
# ---------------------------------------------------------------------------

def get_origin_map(tree):
    return getattr(tree, ORIGIN_ATTR, None)


def get_list_origins(config_list):
    return getattr(config_list, LIST_ORIGIN_ATTR, None)


def _list_origins_map(tree):
    mapping = getattr(tree, LIST_MAP_ATTR, None)
    if mapping is None:
        mapping = {}
        setattr(tree, LIST_MAP_ATTR, mapping)
    return mapping


def mirror_list_origins(tree, key, config_list):
    """Persist a list column's per-element origins on its owning tree.

    Lists are rematerialized as plain lists when read, so origins attached to
    the list object would be lost; the tree survives and keeps them instead.
    """
    origins = getattr(config_list, LIST_ORIGIN_ATTR, None)
    if origins is None:
        origins = [None] * len(config_list)
    _list_origins_map(tree)[key] = list(origins)
    # Also keep the in-memory list attribute for root-level lists and merges.
    setattr(config_list, LIST_ORIGIN_ATTR, list(origins))


def get_list_origins_for(tree, key):
    mapping = getattr(tree, LIST_MAP_ATTR, None)
    if mapping is None:
        return None
    return mapping.get(key)


def _origin_map(tree):
    origin_map = getattr(tree, ORIGIN_ATTR, None)
    if origin_map is None:
        origin_map = {}
        setattr(tree, ORIGIN_ATTR, origin_map)
    return origin_map


def _list_origins(config_list):
    origins = getattr(config_list, LIST_ORIGIN_ATTR, None)
    if origins is None:
        origins = []
        setattr(config_list, LIST_ORIGIN_ATTR, origins)
    while len(origins) < len(config_list):
        origins.append(None)
    return origins


def _is_real_origin(origin):
    return origin not in (None, ORIGIN_MISSING, ORIGIN_FORBIDDEN)


def stamp_local(tree, key, origin, manifest=None, value=None):
    """Stamp a column defined directly by the resource being parsed.

    Nested trees and lists are stamped recursively; the entry is created on
    every level so :func:`origin_view` can resolve the winning resource.
    ``value`` is the raw stored object (ConfigList is not materialized by
    :meth:`ConfigTree.get`).
    """
    if origin is None:
        return
    if value is None:
        value = tree.get(key, None)
    _stamp_value(tree, key, value, origin, manifest)


def stamp_key(tree, key, origin, manifest=None, config_list=None):
    """Shallow stamp: record only the top-level entry for an include token.

    Included subtrees keep their own, finer-grained origin maps, so we never
    recurse into them here.
    """
    if origin is None:
        return
    _origin_map(tree)[key] = origin
    if config_list is not None:
        mirror_list_origins(tree, key, config_list)


def _stamp_value(owner, key, value, origin, manifest):
    if isinstance(value, ConfigTree):
        _origin_map(owner)[key] = origin
        if manifest is not None:
            setattr(value, MANIFEST_ATTR, manifest)
        for child_key, child_value in value.items():
            _stamp_value(value, child_key, child_value, origin, manifest)
    elif isinstance(value, ConfigList):
        _origin_map(owner)[key] = origin
        origins = _list_origins(value)
        for index, element in enumerate(value):
            if isinstance(element, ConfigTree):
                if manifest is not None:
                    setattr(element, MANIFEST_ATTR, manifest)
                for child_key, child_value in element.items():
                    _stamp_value(element, child_key, child_value, origin, manifest)
            elif isinstance(element, ConfigList):
                _stamp_value(owner, key, element, origin, manifest)
            if origins[index] is None:
                origins[index] = origin
        mirror_list_origins(owner, key, value)
    else:
        origin_map = _origin_map(owner)
        existing = origin_map.get(key)
        if _is_real_origin(existing) and _is_real_origin(origin) and existing != origin:
            if manifest is not None:
                manifest.record_override(
                    key, origin, existing, chain=manifest.chain_for(origin))
        origin_map[key] = origin


def merge_tree_origins(target, source, manifest=None, column=''):
    """Carry provenance when ``source`` is merged into ``target``.

    Tree-to-tree merges recurse so nested columns keep their own resource;
    leaf overrides from a different resource are logged as overrides.
    """
    if manifest is not None:
        setattr(target, MANIFEST_ATTR, manifest)
    source_map = getattr(source, ORIGIN_ATTR, None)
    if not source_map:
        return
    target_map = _origin_map(target)
    source_lists = getattr(source, LIST_MAP_ATTR, None) or {}
    for key, source_origin in source_map.items():
        full_column = key if not column else column + '.' + key
        target_value = target.get(key, None)
        source_value = source.get(key, None)
        if isinstance(target_value, ConfigTree) and isinstance(source_value, ConfigTree):
            merge_tree_origins(target_value, source_value, manifest, full_column)
            target_map[key] = source_origin
        elif isinstance(target_value, list) and isinstance(source_value, list):
            if key in source_lists:
                _list_origins_map(target)[key] = list(source_lists[key])
            target_map[key] = source_origin
        else:
            existing = target_map.get(key)
            if _is_real_origin(existing) and _is_real_origin(source_origin) \
                    and existing != source_origin and manifest is not None:
                manifest.record_override(
                    full_column, source_origin, existing,
                    chain=manifest.chain_for(source_origin))
            target_map[key] = source_origin


def merge_list_origins(target, source, manifest=None, owner=None, key=None):
    """Append provenance when an include concatenates ``source`` into lists.

    When ``owner``/``key`` are given, the combined per-element origins are also
    mirrored on the owning tree so they survive list rematerialization.
    """
    if manifest is not None:
        setattr(target, MANIFEST_ATTR, manifest)
    target_origins = _list_origins(target)
    source_origins = getattr(source, LIST_ORIGIN_ATTR, None)
    if source_origins is None:
        source_origins = [None] * len(source)
    for index, origin in enumerate(source_origins):
        target_origins.append(origin)
        element = source[index] if index < len(source) else None
        if isinstance(element, ConfigTree) and origin is not None:
            setattr(element, MANIFEST_ATTR, manifest)
            for child_key, child_value in element.items():
                _stamp_value(element, child_key, child_value, origin, manifest)
    if owner is not None and key is not None:
        mirror_list_origins(owner, key, target)
    return target_origins


def origin_view(config, manifest=None):
    """Plain ``{column: provenance}`` view of a resolved config.

    With a manifest, each provenance entry is a dict exposing the winning
    resource, its frozen version and the include chain. List columns map to a
    per-element provenance list.
    """
    view = {}

    def provenance(origin):
        if not _is_real_origin(origin):
            return origin
        if manifest is None:
            return origin
        record = manifest.resources.get(origin, {})
        return {
            'resource': origin,
            'version': record.get('version'),
            'chain': manifest.chain_for(origin),
            'status': 'missing' if record.get('missing') else 'loaded',
        }

    def walk(node, prefix, owner=None, owner_key=None):
        if isinstance(node, ConfigTree):
            origin_map = get_origin_map(node) or {}
            for key, value in node.items():
                column = prefix + key
                if isinstance(value, ConfigTree):
                    walk(value, column + '.', node, key)
                elif isinstance(value, list):
                    # Prefer the origins mirrored on the owning tree, which
                    # survive the plain-list rematerialization.
                    origins = get_list_origins_for(node, key)
                    if origins is None:
                        stored = OrderedDict.__getitem__(node, key)
                        origins = get_list_origins(stored)
                    if origins is not None:
                        view[column] = [provenance(o) for o in origins]
                    else:
                        view[column] = provenance(origin_map.get(key))
                else:
                    view[column] = provenance(origin_map.get(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, prefix + str(index) + '.')

    walk(config, '')
    return view


__all__ = [
    'ResourceManifest',
    'ORIGIN_MISSING',
    'ORIGIN_FORBIDDEN',
    'INCLUDE_LOADED',
    'INCLUDE_GLOB',
    'INCLUDE_MISSING',
    'INCLUDE_MISSING_OPTIONAL',
    'INCLUDE_FORBIDDEN',
    'INCLUDE_CYCLE',
    'normalize_uri',
    'relative_uri',
    'digest_content',
    'get_origin_map',
    'get_list_origins',
    'get_list_origins_for',
    'mirror_list_origins',
    'stamp_local',
    'stamp_key',
    'merge_tree_origins',
    'merge_list_origins',
    'origin_view',
]
