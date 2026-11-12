# -*- encoding: utf-8 -*-
"""
Regression tests for multi-layer "column mapping" imports.

A historical import stacks several HOCON layers. For a target column each layer
expresses one of three intents, and the tests below pin which one pyhocon can
actually represent:

1. keep the upstream column      -> the layer simply does not mention the key,
                                    or uses an optional substitution that
                                    resolves to nothing (``${?missing}``);
2. explicitly clear the column   -> the layer assigns ``null``. The column
                                    survives in the final row and is sent to the
                                    target row as ``None``;
3. the layer provides no value   -> indistinguishable from (1) at the plain
                                    dictionary level, but the distinction may
                                    still live in the unresolved tree / history
                                    and only shows up when another layer is
                                    stacked on top.

Expected values are always expressed as the *final* row that would be sent to
the target system: the full set of columns, including columns whose value is
``None``.
"""

import copy
from collections import OrderedDict

import pytest

from pyhocon import ConfigFactory, ConfigParser, ConfigTree, ConfigSubstitutionException
from pyhocon.config_tree import NoneValue


def _parse(content, resolve):
    return ConfigFactory.parse_string(content, resolve=resolve)


def _import_layers(layers, resolve_after_merge=True):
    """Stack ``(hocon_text, resolved_before_merge)`` layers into one tree.

    The first parsed tree is kept as the accumulator so that its root flag and
    per-key history are preserved across the merges.
    """
    accumulator = None
    for content, pre_resolved in layers:
        tree = _parse(content, resolve=pre_resolved)
        if accumulator is None:
            accumulator = tree
        else:
            ConfigTree.merge_configs(accumulator, tree)
    if resolve_after_merge:
        ConfigParser.resolve_substitutions(accumulator)
    return accumulator


def _final_row(tree):
    """The plain row (columns + null values) actually sent to the target."""
    return tree.as_plain_ordered_dict()


def _raw_leaf(tree, path):
    current = tree
    for part in path.split('.'):
        current = OrderedDict.__getitem__(current, part) if isinstance(current, ConfigTree) else current
    return current


def _history_types(tree, key):
    return [type(item).__name__ for item in tree.history.get(key, [])]


class TestLayeredColumnMapping(object):

    # ------------------------------------------------------------------
    # Intent 2: explicit null is a value, it never removes the column
    # ------------------------------------------------------------------
    def test_explicit_null_overrides_upstream_but_keeps_column(self):
        config = _import_layers([
            ("a = 1\nb = 2\n", False),
            ("a = null\n", False),
        ])
        assert _final_row(config) == {'a': None, 'b': 2}
        assert 'a' in config
        assert config.get('a') is None

        # null even creates the column when the upstream never had it
        config = _import_layers([
            ("b = 2\n", False),
            ("a = null\n", False),
        ])
        assert _final_row(config) == {'b': 2, 'a': None}
        assert 'a' in config

        # identical visible result whether layers were resolved first or not
        pre_resolved = _import_layers([
            ("a = 1\nb = 2\n", True),
            ("a = null\n", True),
        ], resolve_after_merge=False)
        assert _final_row(pre_resolved) == {'a': None, 'b': 2}

    def test_explicit_null_inside_object_keeps_sibling_columns(self):
        config = _import_layers([
            ("m { x = 1, y = 2 }\n", False),
            ("m { y = null }\n", False),
        ])
        assert _final_row(config) == {'m': {'x': 1, 'y': None}}
        assert 'm.y' in config
        assert config.get('m.y') is None

        # replacing the whole object with null keeps the object key itself
        config = _import_layers([
            ("m { x = 1 }\n", False),
            ("m = null\n", False),
        ])
        assert _final_row(config) == {'m': None}
        assert 'm' in config

    # ------------------------------------------------------------------
    # Intent 1 / 3: an unresolvable optional substitution is "no value"
    # ------------------------------------------------------------------
    def test_optional_missing_keeps_upstream_instead_of_clearing(self):
        # upstream scalar is preserved: the overlay simply provides no value
        config = _import_layers([
            ("a = 1\n", False),
            ("a = ${?missing}\n", False),
        ])
        assert _final_row(config) == {'a': 1}
        assert 'a' in config

        # inside an object the upstream leaf is preserved, siblings untouched
        config = _import_layers([
            ("m { x = 1, y = 2 }\n", False),
            ("m { y = ${?missing} }\n", False),
        ])
        assert _final_row(config) == {'m': {'x': 1, 'y': 2}}

    def test_optional_missing_without_upstream_drops_the_column(self):
        config = _import_layers([
            ("b = 2\n", False),
            ("a = ${?missing}\n", False),
        ])
        assert _final_row(config) == {'b': 2}
        assert 'a' not in config

        # an optional "no value" layer even removes an explicit null predecessor:
        # optional resolution to nothing deletes the key, null cannot protect it
        config = _import_layers([
            ("a = null\n", False),
            ("a = ${?missing}\n", False),
        ])
        assert _final_row(config) == {}
        assert 'a' not in config

    # ------------------------------------------------------------------
    # Same key alternated across null / missing optional / override / self ref
    # ------------------------------------------------------------------
    def test_alternating_null_optional_and_scalar_overrides(self):
        config = _import_layers([
            ("a = 1\n", False),
            ("a = ${?miss1}\n", False),
            ("a = null\n", False),
            ("a = ${?miss2}\n", False),
            ("a = 9\n", False),
        ])
        # the last layer that actually provides a value wins
        assert _final_row(config) == {'a': 9}
        # every layer is still traceable in history, including the null one
        assert _history_types(config, 'a') == [
            'int', 'ConfigValues', 'NoneValue', 'ConfigValues', 'int']

    def test_alternating_object_partial_override_then_null_then_optional(self):
        config = _import_layers([
            ("a = 1\n", False),
            ("a { x = 1 }\n", False),
            ("a { y = 2 }\n", False),
            ("a = null\n", False),
            ("a = ${?missing}\n", False),
        ])
        # the object merges, null replaces it, the final optional layer removes it
        assert _final_row(config) == {}
        assert 'a' not in config

    def test_self_reference_across_layers_merges_objects(self):
        config = _import_layers([
            ("m { x = 1 }\n", False),
            ("m = ${m} { y = 2 }\n", False),
        ])
        assert _final_row(config) == {'m': {'x': 1, 'y': 2}}

    def test_self_reference_over_explicit_null(self):
        # mandatory self reference onto a null column does not raise and keeps
        # the explicit null (current library behaviour, pinned here)
        config = _import_layers([
            ("a = null\n", False),
            ("a = ${a}\n", False),
        ])
        assert _final_row(config) == {'a': None}
        assert 'a' in config

        config = _import_layers([
            ("a = null\n", False),
            ("a = ${?a}\n", False),
        ])
        assert _final_row(config) == {'a': None}
        assert 'a' in config

    # ------------------------------------------------------------------
    # Changing whether each layer is resolved before the merge
    # ------------------------------------------------------------------
    def test_resolve_state_changes_history_but_not_visible_row(self):
        pre_resolved = _import_layers([
            ("b = 2\n", True),
            ("a = ${?missing}\n", True),
        ], resolve_after_merge=False)
        merged_then_resolved = _import_layers([
            ("b = 2\n", False),
            ("a = ${?missing}\n", False),
        ])

        assert _final_row(pre_resolved) == {'b': 2}
        assert _final_row(merged_then_resolved) == {'b': 2}
        assert pre_resolved == merged_then_resolved

        # early resolution already dropped the column, so its history entry is
        # gone; resolving after the merge still records the removed layer
        assert list(pre_resolved.history.keys()) == ['b']
        assert list(merged_then_resolved.history.keys()) == ['b', 'a']
        assert _history_types(merged_then_resolved, 'a') == ['ConfigValues']

    # ------------------------------------------------------------------
    # Pair 1: identical final rows that diverge once another layer is added
    # ------------------------------------------------------------------
    def test_same_row_diverges_tombstone_vs_never_mentioned(self):
        tombstoned = _import_layers([
            ("b = 2\n", False),
            ("a = ${?missing}\n", False),
        ])
        never_mentioned = _import_layers([
            ("b = 2\n", False),
        ])

        # step 0: the two final rows are indistinguishable ...
        assert _final_row(tombstoned) == {'b': 2}
        assert _final_row(never_mentioned) == {'b': 2}
        assert tombstoned == never_mentioned

        # ... but stacking a self-referential layer reveals the tombstone.
        for extra, expected in [
            ("a = ${?a}x\n", {'b': 2, 'a': 'x'}),
            ("a += x\n", {'b': 2, 'a': ' x'}),
        ]:
            broken = copy.deepcopy(tombstoned)
            clean = copy.deepcopy(never_mentioned)
            ConfigTree.merge_configs(broken, _parse(extra, False))
            ConfigTree.merge_configs(clean, _parse(extra, False))

            with pytest.raises(ConfigSubstitutionException, match="cannot be substituted"):
                ConfigParser.resolve_substitutions(broken)
            ConfigParser.resolve_substitutions(clean)
            assert _final_row(clean) == expected

        # the information becomes irreversible once only the plain row survives:
        # rebuilding from the dictionary behaves like the never-mentioned case
        rebuilt = ConfigFactory.from_dict(dict(_final_row(tombstoned)), root=True)
        ConfigTree.merge_configs(rebuilt, _parse("a = ${?a}x\n", False))
        ConfigParser.resolve_substitutions(rebuilt)
        assert _final_row(rebuilt) == {'b': 2, 'a': 'x'}

    def test_same_row_diverges_with_resolve_timing(self):
        extra = "a = ${?a}x\n"

        # each layer resolved before the merge: the tombstone is erased early
        resolved_first = _import_layers([
            ("b = 2\n", True),
            ("a = ${?missing}\n", True),
            (extra, False),
        ], resolve_after_merge=False)
        ConfigParser.resolve_substitutions(resolved_first)
        assert _final_row(resolved_first) == {'b': 2, 'a': 'x'}

        # same textual layers merged unresolved, resolved only at the end:
        # the tombstone survives and the extra self reference is a cycle
        resolved_last = _import_layers([
            ("b = 2\n", False),
            ("a = ${?missing}\n", False),
            (extra, False),
        ], resolve_after_merge=False)
        with pytest.raises(ConfigSubstitutionException, match="cannot be substituted"):
            ConfigParser.resolve_substitutions(resolved_last)

    # ------------------------------------------------------------------
    # Pair 2: identical {'a': None} rows, different internal null representation
    # ------------------------------------------------------------------
    def test_same_null_row_diverges_sentinel_history_vs_plain_none(self):
        # parsed null keeps a NoneValue marker in history even after resolution,
        # while a programmatic plain None only stores NoneType
        parsed_null = _parse("a = null\n", resolve=True)
        plain_none = ConfigFactory.from_dict({'a': None}, root=True)

        assert _final_row(parsed_null) == {'a': None}
        assert _final_row(plain_none) == {'a': None}
        assert parsed_null == plain_none
        assert _history_types(parsed_null, 'a') == ['NoneValue']
        assert _history_types(plain_none, 'a') == ['NoneType']
        assert isinstance(_raw_leaf(parsed_null, 'a'), type(None))

        # adding an optional self-reference layer keeps the column for the
        # NoneValue history, but deletes it for the plain-None history
        parsed_stack = copy.deepcopy(parsed_null)
        plain_stack = copy.deepcopy(plain_none)
        ConfigTree.merge_configs(parsed_stack, _parse("a = ${?a}\n", False))
        ConfigTree.merge_configs(plain_stack, _parse("a = ${?a}\n", False))
        ConfigParser.resolve_substitutions(parsed_stack)
        ConfigParser.resolve_substitutions(plain_stack)

        assert _final_row(parsed_stack) == {'a': None}
        assert 'a' in parsed_stack
        assert _final_row(plain_stack) == {}
        assert 'a' not in plain_stack

        # an unresolved parsed null (still a NoneValue leaf) behaves the same way
        unresolved_null = _import_layers([
            ("a = null\n", False),
            ("a = ${?a}\n", False),
        ])
        assert isinstance(_raw_leaf(_parse("a = null\n", False), 'a'), NoneValue)
        assert _final_row(unresolved_null) == {'a': None}
        assert 'a' in unresolved_null

    # ------------------------------------------------------------------
    # Expressibility limits pinned with minimal input and actual behaviour
    # ------------------------------------------------------------------
    def test_no_syntax_removes_a_column_except_optional_missing(self):
        # explicit null can only clear to None, never remove the key
        config = _import_layers([
            ("a = 1\n", False),
            ("a = null\n", False),
        ])
        assert 'a' in config
        assert _final_row(config) == {'a': None}

    def test_null_string_concatenation_leaks_the_sentinel(self):
        # minimal reproduction: concatenating onto a self-referenced parsed null
        # stringifies the internal NoneValue marker instead of treating it as empty
        config = _import_layers([
            ("a = null\n", False),
            ('a = ${?a} "x"\n', False),
        ])
        leaked = config['a']
        assert isinstance(leaked, str)
        assert leaked.startswith('<pyhocon.config_tree.NoneValue object')
        assert leaked.endswith(' x')

        # the same operation on a plain None stays clean, showing the divergence
        # is caused by the NoneValue sentinel, not by null concatenation itself
        clean = ConfigFactory.from_dict({'a': None}, root=True)
        ConfigTree.merge_configs(clean, _parse('a = ${?a} "x"\n', False))
        ConfigParser.resolve_substitutions(clean)
        assert clean['a'] == 'x'

    def test_nested_null_and_optional_missing_diverge_on_later_reader_layer(self):
        explicit_clear = _import_layers([
            ("m { x = 1, y = 2 }\n", False),
            ("m { y = null }\n", False),
        ])
        no_value = _import_layers([
            ("m { x = 1, y = 2 }\n", False),
            ("m { y = ${?missing} }\n", False),
        ])

        # already different at step 0: None column vs preserved upstream value
        assert _final_row(explicit_clear) == {'m': {'x': 1, 'y': None}}
        assert _final_row(no_value) == {'m': {'x': 1, 'y': 2}}

        # a later layer reading the subkey propagates the difference:
        # mandatory substitution of null removes the reader column,
        # the preserved upstream value flows through
        null_reader = copy.deepcopy(explicit_clear)
        kept_reader = copy.deepcopy(no_value)
        ConfigTree.merge_configs(null_reader, _parse("m { z = ${m.y} }\n", False))
        ConfigTree.merge_configs(kept_reader, _parse("m { z = ${m.y} }\n", False))
        ConfigParser.resolve_substitutions(null_reader)
        ConfigParser.resolve_substitutions(kept_reader)

        assert _final_row(null_reader) == {'m': {'x': 1, 'y': None}}
        assert 'm.z' not in null_reader
        assert _final_row(kept_reader) == {'m': {'x': 1, 'y': 2, 'z': 2}}
