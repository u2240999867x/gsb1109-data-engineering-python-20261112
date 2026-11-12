"""Extract a self-contained subset of an unresolved HOCON configuration.

The goal is to export a handful of selected keys ("columns" of a table) together
with every configuration key reachable through substitution references, while
dropping the independent configuration of the other tables.  Override history,
self-referential appends (``+=``) and optional substitutions (``${?var}``) are
preserved so that the extracted document resolves exactly like the original one
in any target environment.
"""

import copy
import os

from pyhocon.config_tree import ConfigTree, ConfigValues, ConfigSubstitution, NoneValue
from pyhocon.config_parser import ConfigParser
from pyhocon.converter import HOCONConverter
from pyhocon.exceptions import ConfigException, ConfigMissingException


class ConfigExtractionError(ConfigException):
    """Raised when a selection cannot be exported independently.

    ``columns`` lists the selected keys that are affected and ``references``
    describes the dependency chain leading to the blocking reference.
    """

    def __init__(self, message, columns=None, references=None):
        super(ConfigExtractionError, self).__init__(message)
        self.columns = list(columns or [])
        self.references = list(references or [])


class ExternalReference(object):
    """A substitution whose variable is not defined inside the configuration.

    Such a reference is resolved against the target environment (or fails when
    it is mandatory).  ``optional`` is ``False`` as soon as one of the
    references to the variable is mandatory, even if other uses are optional.
    """

    def __init__(self, variable, optional, referrers, chains):
        self.variable = variable
        self.optional = optional
        self.referrers = sorted(set(referrers))
        # column -> one representative chain of hops (config paths / variables)
        self.chains = dict(chains)

    @property
    def key(self):
        return self.variable

    def __repr__(self):
        kind = 'optional' if self.optional else 'mandatory'
        return 'ExternalReference({name!r}, {kind}, referrers={refs})'.format(
            name=self.variable, kind=kind, refs=self.referrers)


class ExtractionResult(object):
    """The outcome of :meth:`ConfigExtractor.extract`."""

    def __init__(self, columns, config, hocon, internal_paths, external_references):
        self.columns = list(columns)
        self.config = config
        self.hocon = hocon
        self.internal_paths = list(internal_paths)
        self.external_references = list(external_references)

    @property
    def external_variables(self):
        return [ref.variable for ref in self.external_references]

    def __repr__(self):
        return ('ExtractionResult(columns={cols!r}, internal={n} paths, '
                'external={ext!r})').format(
                    cols=self.columns,
                    n=len(self.internal_paths),
                    ext=self.external_references)


class ConfigExtractor(object):
    """Export selected keys and their transitive substitution dependencies."""

    @classmethod
    def extract(cls, config, columns, strict=False):
        """Extract ``columns`` from an unresolved ``config``.

        :param config: a :class:`ConfigTree` parsed with ``resolve=False`` so
            that substitutions, override chains and ``+=`` appends are intact
        :param columns: iterable of dotted key paths to export
        :param strict: when ``True``, raise :class:`ConfigExtractionError` if a
            mandatory reference cannot be satisfied inside the configuration
            and depends on the target environment; otherwise keep the
            reference and report it in ``external_references``
        :return: :class:`ExtractionResult`
        """
        if not isinstance(config, ConfigTree):
            raise ConfigException(
                'ConfigExtractor expects an unresolved ConfigTree, '
                'parse the source with ConfigFactory.parse_string(..., resolve=False)')

        parsed_columns = [tuple(ConfigTree.parse_key(column)) for column in columns]
        index = cls._index_tree(config)
        defined_paths = cls._collect_defined_paths(config)

        for column, path in zip(columns, parsed_columns):
            if path not in index:
                raise ConfigMissingException(
                    'Cannot extract {column}: key is not defined in the configuration'.format(column=column))

        required = set()
        worklist = []
        for path in parsed_columns:
            cls._add_subtree(required, worklist, index, path)

        external = {}
        while worklist:
            path = worklist.pop()
            node = index[path]
            for substitution in cls._iter_substitutions(node):
                variable = substitution.variable
                target_path = tuple(ConfigTree.parse_key(variable))
                if target_path in index:
                    if target_path not in required:
                        cls._add_subtree(required, worklist, index, target_path)
                elif target_path in defined_paths:
                    # Defined inside an override chain (e.g. object self-reference).
                    # History is rooted at the top-level field, so keep that whole field.
                    owner = (target_path[0],)
                    if owner in index and owner not in required:
                        cls._add_subtree(required, worklist, index, owner)
                elif cls._is_root_self_reference(path, target_path):
                    # Handled by the parser's self-reference fix-up, not the environment.
                    continue
                else:
                    entry = external.setdefault(variable, {
                        'optional': True,
                        'referrers': set(),
                    })
                    entry['optional'] = entry['optional'] and substitution.optional
                    entry['referrers'].add(path)

        pruned = cls._prune(config, required)
        hocon = cls._render(pruned)

        external_references = cls._build_external_references(
            parsed_columns, columns, index, defined_paths, external)

        if strict:
            blocking = [ref for ref in external_references if not ref.optional]
            if blocking:
                affected = sorted({
                    column for ref in blocking for column in ref.chains})
                raise ConfigExtractionError(
                    'The selection cannot be exported independently: {variables} '
                    'must be provided by the target environment'.format(
                        variables=', '.join(ref.variable for ref in blocking)),
                    columns=affected,
                    references=blocking)

        return ExtractionResult(
            columns=columns,
            config=pruned,
            hocon=hocon,
            internal_paths=cls._join_paths(required),
            external_references=external_references)

    @classmethod
    def assert_equivalent(cls, source, extracted_hocon, columns, environments):
        """Resolve the source and the extraction under each environment.

        The selected columns must match in presence, value, type and key order,
        and resolution errors must be of the same kind.  ``source`` may be the
        original HOCON text or an unresolved :class:`ConfigTree`.

        :param environments: iterable of dictionaries used as the process
            environment while resolving substitutions
        :raises AssertionError: on the first discrepancy
        """
        source_tree = _as_unresolved_tree(source)
        extracted_tree = _as_unresolved_tree(extracted_hocon)

        for environment in environments:
            source_outcome = _resolve_in_environment(source_tree, environment)
            extracted_outcome = _resolve_in_environment(extracted_tree, environment)

            source_kind, source_value = source_outcome
            extracted_kind, extracted_value = extracted_outcome
            if source_kind != extracted_kind:
                raise AssertionError(
                    'Resolution differs under environment {env!r}: source {a} but extraction {b}'.format(
                        env=environment,
                        a=_describe_outcome(source_outcome),
                        b=_describe_outcome(extracted_outcome)))

            if source_kind == 'error':
                if type(source_value) is not type(extracted_value):
                    raise AssertionError(
                        'Error type differs under environment {env!r}: {a} vs {b}'.format(
                            env=environment,
                            a=type(source_value).__name__,
                            b=type(extracted_value).__name__))
                continue

            for column in columns:
                source_present = column in source_value
                extracted_present = column in extracted_value
                if source_present != extracted_present:
                    raise AssertionError(
                        'Presence of {column} differs under environment {env!r}: '
                        'source={a} extraction={b}'.format(
                            column=column, env=environment,
                            a=source_present, b=extracted_present))
                if source_present:
                    source_view = _ordered_view(source_value.get(column))
                    extracted_view = _ordered_view(extracted_value.get(column))
                    if source_view != extracted_view:
                        raise AssertionError(
                            'Value of {column} differs under environment {env!r}:\n'
                            '  source:     {a!r}\n  extraction: {b!r}'.format(
                                column=column, env=environment,
                                a=source_view, b=extracted_view))

    @classmethod
    def _index_tree(cls, tree, prefix=()):
        index = {}
        for key, value in tree.items():
            path = prefix + (key,)
            index[path] = value
            if isinstance(value, ConfigTree):
                index.update(cls._index_tree(value, path))
        return index

    @classmethod
    def _collect_defined_paths(cls, tree):
        """All structurally defined paths, including objects inside override chains.

        The current value of an overridden field is a :class:`ConfigValues` whose
        override chain may contain :class:`ConfigTree` literals; those nested
        paths are not visible through the normal tree index but still define
        valid reference targets once the field resolves.
        """
        defined = set()

        def walk(node, prefix):
            if isinstance(node, ConfigTree):
                for key, value in node.items():
                    path = prefix + (key,)
                    defined.add(path)
                    walk(value, path)
            elif isinstance(node, ConfigValues):
                current = node
                while isinstance(current, ConfigValues):
                    for token in current.tokens:
                        walk(token, prefix)
                    current = current.overridden_value
                if current is not None:
                    walk(current, prefix)
            elif isinstance(node, list):
                for element in node:
                    walk(element, prefix)

        walk(tree, ())
        return defined

    @classmethod
    def _iter_substitutions(cls, node):
        """Yield every substitution reachable from ``node``, including chains."""
        if isinstance(node, ConfigSubstitution):
            yield node
        elif isinstance(node, ConfigValues):
            current = node
            while isinstance(current, ConfigValues):
                for token in current.tokens:
                    for substitution in cls._iter_substitutions(token):
                        yield substitution
                current = current.overridden_value
            if current is not None:
                for substitution in cls._iter_substitutions(current):
                    yield substitution
        elif isinstance(node, ConfigTree):
            for value in node.values():
                for substitution in cls._iter_substitutions(value):
                    yield substitution
        elif isinstance(node, list):
            for element in node:
                for substitution in cls._iter_substitutions(element):
                    yield substitution

    @classmethod
    def _is_root_self_reference(cls, owner_path, target_path):
        """Mirror the parser's ``_fixup_self_references`` root-field rule."""
        return len(target_path) > 0 and len(owner_path) > 0 and target_path[0] == owner_path[0]

    @classmethod
    def _add_subtree(cls, required, worklist, index, path):
        for depth in range(1, len(path)):
            ancestor = path[:depth]
            if ancestor in index:
                required.add(ancestor)
        if path in required:
            return
        required.add(path)
        worklist.append(path)
        for other_path in index:
            if other_path[:len(path)] == path and len(other_path) > len(path):
                if other_path not in required:
                    required.add(other_path)
                    worklist.append(other_path)

    @classmethod
    def _prune(cls, config, required):
        pruned = copy.deepcopy(config)

        def prune(node, prefix):
            for key in list(node.keys()):
                path = prefix + (key,)
                if path not in required:
                    del node[key]
                elif isinstance(node[key], ConfigTree):
                    prune(node[key], path)

        prune(pruned, ())
        if getattr(pruned, 'root', False):
            pruned.history = {
                key: value for key, value in pruned.history.items() if key in pruned}
        return pruned

    @classmethod
    def _render(cls, node, level=0):
        indent = '  ' * level
        statements = []
        for key, value in node.items():
            stanzas = []
            if isinstance(value, ConfigValues):
                terminal, layers = cls._split_chain(value)
                if terminal is not None:
                    stanzas.append(cls._render_statement(indent, key, terminal, level))
                for layer in layers:
                    stanzas.append(cls._render_statement(indent, key, layer, level))
            else:
                # Plain scalar (including null) or a literal object block.
                stanzas.append(cls._render_statement(indent, key, value, level))
            statements.append('\n'.join(stanzas))
        return '\n'.join(statements)

    @classmethod
    def _render_statement(cls, indent, key, value, level):
        if isinstance(value, ConfigTree):
            return '{indent}{key} {block}'.format(
                indent=indent, key=key, block=cls._render_block(value, level))
        return '{indent}{key} = {value}'.format(
            indent=indent, key=key,
            value=HOCONConverter.to_hocon(value, level=level + 1))

    @classmethod
    def _render_block(cls, node, level):
        if len(node) == 0:
            return '{}'
        inner = cls._render(node, level + 1)
        closing = '  ' * level
        return '{{\n{inner}\n{closing}}}'.format(inner=inner, closing=closing)

    @classmethod
    def _split_chain(cls, value):
        layers = []
        current = value
        while isinstance(current, ConfigValues):
            layers.append(current)
            current = current.overridden_value
        layers.reverse()
        return current, layers

    @classmethod
    def _build_external_references(cls, parsed_columns, columns, index, defined_paths, external):
        references = []
        for variable, info in external.items():
            chains = {}
            for column, path in zip(columns, parsed_columns):
                chain = cls._find_chain(path, variable, index, defined_paths)
                if chain is not None:
                    chains[column] = chain
            references.append(ExternalReference(
                variable=variable,
                optional=info['optional'],
                referrers=info['referrers'],
                chains=chains))
        references.sort(key=lambda ref: ref.variable)
        return references

    @classmethod
    def _find_chain(cls, start_path, variable, index, defined_paths):
        queue = [(start_path, ['.'.join(start_path)])]
        seen = set()
        while queue:
            path, chain = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            node = index.get(path)
            if node is None:
                continue
            for substitution in cls._iter_substitutions(node):
                hop = chain + ['${' + ('?' if substitution.optional else '') + substitution.variable + '}']
                if substitution.variable == variable:
                    return hop
                target_path = tuple(ConfigTree.parse_key(substitution.variable))
                if target_path in index:
                    queue.append((target_path, hop + ['.'.join(target_path)]))
                elif target_path in defined_paths:
                    queue.append(((target_path[0],), hop + ['.'.join(target_path)]))
        return None

    @classmethod
    def _join_paths(cls, required):
        return ['.'.join(path) for path in sorted(required, key=lambda path: path)]


def _as_unresolved_tree(source):
    if isinstance(source, ConfigTree):
        return source
    return ConfigParser.parse(source, resolve=False)


def _resolve_in_environment(tree, environment):
    saved = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(environment)
        resolved = copy.deepcopy(tree)
        ConfigParser.resolve_substitutions(resolved)
        return 'value', resolved
    except ConfigException as exception:
        return 'error', exception
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _describe_outcome(outcome):
    kind, value = outcome
    if kind == 'error':
        return 'raised {name}: {message}'.format(
            name=type(value).__name__, message=value)
    return 'resolved'


def _ordered_view(value):
    if value is None or isinstance(value, NoneValue):
        return None
    if isinstance(value, ConfigTree):
        return [('tree', [(key, _ordered_view(child)) for key, child in value.items()])]
    if isinstance(value, list):
        return ['list', [_ordered_view(item) for item in value]]
    return value
