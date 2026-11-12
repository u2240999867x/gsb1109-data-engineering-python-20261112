# -*- encoding: utf-8 -*-
"""
Composition regression suite for HOCON-based ingestion (sink) column mappings.

An optional fragment (``${?patch}``) can silently disappear while a column
mapping is being assembled. Depending on where the fragment sits the result is
different:

* list/object concat position  -> the fragment vanishes without a trace
* element position inside a list -> a ``None`` slot keeps the position alive
* object value position        -> the whole column key is removed
* wrong-typed fragment         -> the column silently loses its type, or a
                                  ``ConfigWrongTypeException`` is raised at the
                                  concat boundary

The suite drives three representations of the same logical mapping:

1. a typed column list      ``[{name, type}, ...]``
2. a nested object schema   ``schema { col { type } }`` plus a text order list
3. plain column-name text   bare ``[id, amount, note]`` strings and scalar
                            fragments that carry no type

For every representation the composed target columns, their order and the
missing entries are extracted independently. The same logical mapping is then
expressed in three writing styles:

* inline            - every column is written literally in one place
* relative reference- the fragment lives in a nested object in the same file
* cross-layer concat- the fragment lives in an included file

Equivalence is only asserted while the semantic precondition holds: every
fragment involved in a concat must be defined with the expected container type
(``ConfigList`` / ``ConfigTree``). Failing combinations must be localised to the
earliest fragment that lost its type, and a shortened version of a failing
configuration must still reproduce the same column's type divergence rather
than degenerating into an ordinary syntax error.

Templates use ``@PLACEHOLDER@`` tokens plus ``str.replace`` so that HOCON's own
braces never have to be escaped.
"""

import logging

import pytest
from pyparsing import ParseBaseException

from pyhocon import ConfigFactory, ConfigTree
from pyhocon.exceptions import (ConfigException, ConfigMissingException,
                                ConfigWrongTypeException)


# ---------------------------------------------------------------------------
# Shared fixture data: the canonical ingestion mapping used across the suite.
# ---------------------------------------------------------------------------

BASE_COLUMNS = [("id", "int")]
PATCH_COLUMNS = [("amount", "decimal")]
TAIL_COLUMNS = [("note", "string")]
FULL_COLUMNS = BASE_COLUMNS + PATCH_COLUMNS + TAIL_COLUMNS

BAD_TEXT_FRAGMENT = "amount,decimal"


def render(template, **placeholders):
    for key, value in placeholders.items():
        template = template.replace("@" + key + "@", value)
    return template


def slot(name, coltype):
    return "{ name = %s, type = %s }" % (name, coltype)


def typed_list(columns):
    return "[" + ", ".join(slot(name, coltype) for name, coltype in columns) + "]"


def names(columns):
    return [name for name, _ in columns]


def analyze_name_list(conf, path="names"):
    """Analyse a plain column-name text list (no type information).

    :return: ``(column_names, null_slots, non_text)`` where
             - ``column_names`` is the ordered list of surviving name strings
             - ``null_slots`` lists positions where an optional fragment vanished
             - ``non_text`` lists ``(position, kind, value)`` entries that are
               not plain strings (the text input cannot flag a scalar fragment
               as mistyped, so only containers stand out)
    """
    column_names = []
    null_slots = []
    non_text = []
    for position, value in enumerate(conf[path]):
        if value is None:
            null_slots.append(position)
        elif isinstance(value, str):
            column_names.append(value)
        else:
            non_text.append((position, type(value).__name__, value))
    return column_names, null_slots, non_text


# ---------------------------------------------------------------------------
# Independent extraction of target columns, order and missing entries.
# ---------------------------------------------------------------------------

def analyze_typed_list(conf, path="sink.cols"):
    """Analyse a typed column list.

    :return: ``(columns, missing, untyped)`` where
             - ``columns`` is the ordered list of ``(name, declared_type)``
             - ``missing`` lists ``(position, reason)`` for null slots
             - ``untyped`` lists ``(position, kind, value)`` for slots that
               lost the column-object type (scalars or nested containers)
    """
    columns = []
    missing = []
    untyped = []
    for position, value in enumerate(conf[path]):
        if value is None:
            missing.append((position, "optional fragment resolved to null"))
        elif isinstance(value, ConfigTree):
            columns.append((value["name"], value["type"]))
        else:
            untyped.append((position, type(value).__name__, value))
    return columns, missing, untyped


def analyze_nested_schema(conf, schema_path="schema", order_path="order"):
    """Project a text order list onto a nested typed schema object.

    :return: ``(columns, missing, downgraded)`` where
             - ``columns`` is the projection in the order of the name text
             - ``missing`` names the text entries absent from the schema
             - ``downgraded`` lists ``(name, kind, value)`` for columns whose
               schema value is no longer a typed object
    """
    schema = conf[schema_path]
    columns = []
    missing = []
    downgraded = []
    for name in conf[order_path]:
        try:
            value = schema[name]
        except (ConfigMissingException, KeyError):
            missing.append(name)
            continue
        if isinstance(value, ConfigTree):
            columns.append((name, value["type"]))
        else:
            downgraded.append((name, type(value).__name__, value))
    return columns, missing, downgraded


# ---------------------------------------------------------------------------
# HOCON renderings of the list-form mapping in the three writing styles.
# ---------------------------------------------------------------------------

INLINE_LIST_CONF = render("""
sink {
  cols = @FULL_LIST@
}
""", FULL_LIST=typed_list(FULL_COLUMNS))


RELATIVE_LIST_CONF = render("""
sink {
  base = @BASE_LIST@
  patch = @PATCH_LIST@
  tail = @TAIL_LIST@
  cols = ${sink.base} ${?sink.patch} ${sink.tail}
}
""",
                            BASE_LIST=typed_list(BASE_COLUMNS),
                            PATCH_LIST=typed_list(PATCH_COLUMNS),
                            TAIL_LIST=typed_list(TAIL_COLUMNS))


def write_cross_layer_list(tmp_path, patch_defined=True):
    """Create an included fragment file and return the main config path."""
    if patch_defined:
        (tmp_path / "fragments.conf").write_text(
            render("patch = @PATCH_LIST@\n", PATCH_LIST=typed_list(PATCH_COLUMNS))
        )
    main_conf = render("""
include "fragments.conf"
sink {
  base = @BASE_LIST@
  tail = @TAIL_LIST@
  cols = ${sink.base} ${?patch} ${sink.tail}
}
""",
                       BASE_LIST=typed_list(BASE_COLUMNS),
                       TAIL_LIST=typed_list(TAIL_COLUMNS))
    mapping_path = tmp_path / "mapping.conf"
    mapping_path.write_text(main_conf)
    return mapping_path


# ---------------------------------------------------------------------------
# HOCON renderings of the nested-object mapping in the three writing styles.
# ---------------------------------------------------------------------------

INLINE_OBJECT_CONF = """
schema {
  id { type = int }
  amount { type = decimal }
  note { type = string }
}
order = [id, amount, note]
"""

RELATIVE_OBJECT_CONF = """
base = {
  id { type = int }
  note { type = string }
}
sink {
  patch = {
    amount { type = decimal }
  }
}
schema = ${base} ${?sink.patch}
order = [id, amount, note]
"""


def write_cross_layer_object(tmp_path, patch_defined=True):
    if patch_defined:
        (tmp_path / "schema_fragment.conf").write_text(
            "patch {\n  amount { type = decimal }\n}\n"
        )
    main_conf = """
include "schema_fragment.conf"
base = {
  id { type = int }
  note { type = string }
}
schema = ${base} ${?patch}
order = [id, amount, note]
"""
    mapping_path = tmp_path / "schema_mapping.conf"
    mapping_path.write_text(main_conf)
    return mapping_path


class TestTypedListComposition:
    """Input form 1: the typed column list."""

    def test_inline_list_composes_all_columns_in_order(self):
        conf = ConfigFactory.parse_string(INLINE_LIST_CONF)
        columns, missing, untyped = analyze_typed_list(conf)
        assert columns == FULL_COLUMNS
        assert names(columns) == ["id", "amount", "note"]
        assert missing == []
        assert untyped == []

    def test_optional_fragment_present_in_concat_position(self):
        conf = ConfigFactory.parse_string(RELATIVE_LIST_CONF)
        columns, missing, untyped = analyze_typed_list(conf)
        assert columns == FULL_COLUMNS
        assert missing == []
        assert untyped == []

    def test_optional_fragment_absent_in_concat_drops_column_cleanly(self):
        conf = ConfigFactory.parse_string(render("""
sink {
  base = @BASE_LIST@
  tail = @TAIL_LIST@
  cols = ${sink.base} ${?sink.patch} ${sink.tail}
}
""",
                                                 BASE_LIST=typed_list(BASE_COLUMNS),
                                                 TAIL_LIST=typed_list(TAIL_COLUMNS)))
        columns, missing, untyped = analyze_typed_list(conf)
        # concat position: no null slot, the patch column simply disappears and
        # the surviving base + tail columns keep their order.
        assert columns == BASE_COLUMNS + TAIL_COLUMNS
        assert names(columns) == ["id", "note"]
        assert missing == []
        assert untyped == []

    def test_optional_fragment_absent_in_element_position_leaves_null_slot(self):
        conf = ConfigFactory.parse_string(render("""
sink {
  cols = [
    @SLOT_ID@,
    ${?sink.patch},
    @SLOT_NOTE@
  ]
}
""",
                                                 SLOT_ID=slot("id", "int"),
                                                 SLOT_NOTE=slot("note", "string")))
        columns, missing, untyped = analyze_typed_list(conf)
        # element position: the position survives as an explicit null slot.
        assert columns == [("id", "int"), ("note", "string")]
        assert missing == [(1, "optional fragment resolved to null")]
        assert untyped == []
        assert conf["sink.cols"][1] is None

    def test_scalar_fragment_in_element_position_silently_downgrades_slot(self):
        conf = ConfigFactory.parse_string(render("""
sink {
  patch = "@BAD@"
  cols = [
    @SLOT_ID@,
    ${?sink.patch}
  ]
}
""",
                                                 BAD=BAD_TEXT_FRAGMENT,
                                                 SLOT_ID=slot("id", "int")))
        columns, missing, untyped = analyze_typed_list(conf)
        assert columns == [("id", "int")]
        assert missing == []
        # the would-be column is now plain text with no type information.
        assert untyped == [(1, "str", BAD_TEXT_FRAGMENT)]

    def test_list_fragment_in_element_position_nests_instead_of_splicing(self):
        conf = ConfigFactory.parse_string(render("""
sink {
  patch = @PATCH_LIST@
  cols = [
    @SLOT_ID@,
    ${?sink.patch},
    @SLOT_NOTE@
  ]
}
""",
                                                 PATCH_LIST=typed_list(PATCH_COLUMNS),
                                                 SLOT_ID=slot("id", "int"),
                                                 SLOT_NOTE=slot("note", "string")))
        columns, missing, untyped = analyze_typed_list(conf)
        # element position does not splice: the patch list becomes a nested
        # list slot rather than the 'amount' column.
        assert columns == [("id", "int"), ("note", "string")]
        assert len(untyped) == 1
        position, kind, value = untyped[0]
        assert position == 1
        # substitution deep-copies the fragment into a plain list slot.
        assert kind == "list"
        assert value[0]["name"] == "amount"


class TestNestedSchemaComposition:
    """Input form 2: nested typed object plus text column names."""

    def test_order_text_is_project_onto_typed_schema(self):
        conf = ConfigFactory.parse_string(INLINE_OBJECT_CONF)
        columns, missing, downgraded = analyze_nested_schema(conf)
        assert columns == FULL_COLUMNS
        assert names(columns) == ["id", "amount", "note"]
        assert missing == []
        assert downgraded == []
        # the order input really is plain column-name text.
        assert all(isinstance(name, str) for name in conf["order"])

    def test_name_absent_from_schema_is_reported_as_missing(self):
        conf = ConfigFactory.parse_string("""
schema {
  id { type = int }
  note { type = string }
}
order = [id, amount, note]
""")
        columns, missing, downgraded = analyze_nested_schema(conf)
        assert columns == [("id", "int"), ("note", "string")]
        assert missing == ["amount"]
        assert downgraded == []

    def test_optional_column_absent_removes_key_but_text_order_keeps_name(self):
        conf = ConfigFactory.parse_string("""
schema {
  id { type = int }
  amount = ${?sink.patch}
  note { type = string }
}
order = [id, amount, note]
""")
        assert "amount" not in conf["schema"]
        columns, missing, downgraded = analyze_nested_schema(conf)
        assert columns == [("id", "int"), ("note", "string")]
        assert missing == ["amount"]
        assert downgraded == []

    def test_scalar_fragment_downgrades_object_column_to_text(self):
        conf = ConfigFactory.parse_string(render("""
sink {
  patch = "@BAD@"
}
schema {
  id { type = int }
  amount = ${?sink.patch}
  note { type = string }
}
order = [id, amount, note]
""", BAD=BAD_TEXT_FRAGMENT))
        columns, missing, downgraded = analyze_nested_schema(conf)
        assert columns == [("id", "int"), ("note", "string")]
        assert missing == []
        assert downgraded == [("amount", "str", BAD_TEXT_FRAGMENT)]
        # the typed accessor confirms the column object type was lost.
        with pytest.raises(ConfigException, match="amount has type 'str' rather than 'config'"):
            conf["schema"].get_config("amount")

    def test_duplicate_column_name_in_object_keeps_last_definition(self):
        conf = ConfigFactory.parse_string("""
schema {
  id { type = int }
  id { type = string }
}
order = [id]
""")
        columns, _, _ = analyze_nested_schema(conf)
        # object form deduplicates by name; the list form would keep both.
        assert columns == [("id", "string")]


class TestColumnNameTextComposition:
    """Input form 3: plain column-name text with no type field.

    This representation cannot distinguish a genuine column name from a text
    fragment, so type divergence is invisible here; the suite pins down exactly
    which guarantees still hold (order, null slots, missing names).
    """

    def test_concat_present_and_absent(self):
        present = ConfigFactory.parse_string("""
base = [id]
patch = [amount]
names = ${base} ${?patch} [note]
""")
        absent = ConfigFactory.parse_string("""
base = [id]
names = ${base} ${?patch} [note]
""")
        present_names, present_nulls, present_bad = analyze_name_list(present)
        absent_names, absent_nulls, absent_bad = analyze_name_list(absent)
        assert present_names == ["id", "amount", "note"]
        # the absent fragment drops 'amount' without leaving a null slot.
        assert absent_names == ["id", "note"]
        assert present_nulls == absent_nulls == []
        assert present_bad == absent_bad == []

    def test_element_absent_leaves_null_slot(self):
        conf = ConfigFactory.parse_string("names = [id, ${?patch}, note]")
        column_names, null_slots, non_text = analyze_name_list(conf)
        assert column_names == ["id", "note"]
        assert null_slots == [1]
        assert non_text == []
        assert conf["names"][1] is None

    def test_text_fragment_is_indistinguishable_from_real_name(self):
        # a scalar text fragment occupies the slot and reads exactly like a
        # real column name; the text form has no way to report it as untyped.
        conf = ConfigFactory.parse_string("""
patch = amount
names = [id, ${?patch}, note]
""")
        column_names, null_slots, non_text = analyze_name_list(conf)
        assert column_names == ["id", "amount", "note"]
        assert null_slots == []
        assert non_text == []

    def test_container_fragment_in_name_list_still_stands_out(self):
        conf = ConfigFactory.parse_string("""
patch = [amount, amount_extra]
names = [id, ${?patch}, note]
""")
        column_names, null_slots, non_text = analyze_name_list(conf)
        assert column_names == ["id", "note"]
        assert null_slots == []
        # only a non-string container is detectable in the text representation.
        assert non_text[0][0] == 1
        assert non_text[0][1] == "list"


class TestThreeWritingStyles:
    """The same logical mapping written inline, relatively and cross-layer."""

    def test_list_styles_are_equivalent_while_precondition_holds(self, tmp_path):
        inline = ConfigFactory.parse_string(INLINE_LIST_CONF)
        relative = ConfigFactory.parse_string(RELATIVE_LIST_CONF)
        cross = ConfigFactory.parse_file(str(write_cross_layer_list(tmp_path, True)))

        expected = (FULL_COLUMNS, [], [])
        assert analyze_typed_list(inline) == expected
        assert analyze_typed_list(relative) == expected
        assert analyze_typed_list(cross) == expected

    def test_object_styles_are_equivalent_while_precondition_holds(self, tmp_path):
        inline = ConfigFactory.parse_string(INLINE_OBJECT_CONF)
        relative = ConfigFactory.parse_string(RELATIVE_OBJECT_CONF)
        cross = ConfigFactory.parse_file(str(write_cross_layer_object(tmp_path, True)))

        expected = (FULL_COLUMNS, [], [])
        assert analyze_nested_schema(inline) == expected
        assert analyze_nested_schema(relative) == expected
        assert analyze_nested_schema(cross) == expected

    def test_equivalence_breaks_when_optional_fragment_is_undefined(self, tmp_path):
        # semantic precondition (a defined, correctly typed patch) no longer
        # holds: the inline mapping still has three columns while the relative
        # and cross-layer mappings silently drop 'amount'.
        inline = analyze_typed_list(ConfigFactory.parse_string(INLINE_LIST_CONF))
        relative = ConfigFactory.parse_string(render("""
sink {
  base = @BASE_LIST@
  tail = @TAIL_LIST@
  cols = ${sink.base} ${?sink.patch} ${sink.tail}
}
""",
                                                      BASE_LIST=typed_list(BASE_COLUMNS),
                                                      TAIL_LIST=typed_list(TAIL_COLUMNS)))
        cross = ConfigFactory.parse_file(str(write_cross_layer_list(tmp_path, False)))

        assert names(inline[0]) == ["id", "amount", "note"]
        for conf in (relative, cross):
            columns, missing, untyped = analyze_typed_list(conf)
            assert names(columns) == ["id", "note"]
            assert "amount" not in names(columns)
            assert missing == []
            assert untyped == []

    def test_cross_layer_missing_include_matches_relative_absence(self, tmp_path, caplog):
        mapping_path = write_cross_layer_list(tmp_path, False)
        caplog.set_level(logging.WARNING, logger="pyhocon.config_parser")
        conf = ConfigFactory.parse_file(str(mapping_path))

        assert any("Cannot include file" in record.getMessage()
                   for record in caplog.records)
        columns, missing, untyped = analyze_typed_list(conf)
        assert columns == BASE_COLUMNS + TAIL_COLUMNS
        assert missing == []
        assert untyped == []

    def test_cross_layer_bad_fragment_propagates_same_type_error(self, tmp_path):
        (tmp_path / "fragments.conf").write_text(
            render('patch = "@BAD@"\n', BAD=BAD_TEXT_FRAGMENT)
        )
        (tmp_path / "mapping.conf").write_text(render("""
include "fragments.conf"
sink {
  base = @BASE_LIST@
  cols = ${sink.base} ${?patch}
}
""", BASE_LIST=typed_list(BASE_COLUMNS)))
        with pytest.raises(ConfigWrongTypeException,
                           match=r"Token 'amount,decimal.*index 1.*ConfigList"):
            ConfigFactory.parse_file(str(tmp_path / "mapping.conf"))


class TestErrorLocalization:
    """Failing combinations point at the earliest fragment that lost type."""

    def test_list_concat_locates_first_bad_token_index(self):
        conf = render("""
sink {
  base = @BASE_LIST@
  bad = "@BAD@"
  tail = @TAIL_LIST@
  cols = ${sink.base} ${sink.bad} ${sink.tail}
}
""",
                      BASE_LIST=typed_list(BASE_COLUMNS),
                      BAD=BAD_TEXT_FRAGMENT,
                      TAIL_LIST=typed_list(TAIL_COLUMNS))
        with pytest.raises(ConfigWrongTypeException,
                           match=r"Token 'amount,decimal.*\(index 1\).*ConfigList"):
            ConfigFactory.parse_string(conf)

    def test_list_concat_skips_good_fragment_and_locates_later_bad_token(self):
        conf = render("""
sink {
  base = @BASE_LIST@
  good = @PATCH_LIST@
  bad = "@BAD@"
  cols = ${sink.base} ${sink.good} ${sink.bad}
}
""",
                      BASE_LIST=typed_list(BASE_COLUMNS),
                      PATCH_LIST=typed_list(PATCH_COLUMNS),
                      BAD=BAD_TEXT_FRAGMENT)
        with pytest.raises(ConfigWrongTypeException,
                           match=r"Token 'amount,decimal.*\(index 2\).*ConfigList"):
            ConfigFactory.parse_string(conf)

    def test_object_concat_reports_config_tree_requirement(self):
        conf = render("""
base = {
  id { type = int }
}
bad = "@BAD@"
schema = ${base} ${?bad}
""", BAD=BAD_TEXT_FRAGMENT)
        with pytest.raises(ConfigWrongTypeException,
                           match=r"Token 'amount,decimal.*\(index 1\).*ConfigTree"):
            ConfigFactory.parse_string(conf)

    def test_element_position_failure_is_localized_by_slot_scan(self):
        # element-position type loss does not raise; the analyser must point
        # at the earliest slot that is no longer a column object.
        conf = ConfigFactory.parse_string(render("""
sink {
  patch = "@BAD@"
  cols = [@SLOT_ID@, ${?sink.patch}, @SLOT_NOTE@]
}
""",
                                                 BAD=BAD_TEXT_FRAGMENT,
                                                 SLOT_ID=slot("id", "int"),
                                                 SLOT_NOTE=slot("note", "string")))
        columns, missing, untyped = analyze_typed_list(conf)
        assert columns == [("id", "int"), ("note", "string")]
        assert untyped[0][0] == 1
        assert untyped[0][1] == "str"

    def test_nested_object_failure_is_localized_by_column_name(self):
        conf = ConfigFactory.parse_string(render("""
sink {
  patch = "@BAD@"
}
schema {
  id { type = int }
  amount = ${?sink.patch}
}
""", BAD=BAD_TEXT_FRAGMENT))
        with pytest.raises(ConfigException) as excinfo:
            conf["schema"].get_config("amount")
        assert "amount" in str(excinfo.value)
        assert "'str'" in str(excinfo.value)

    def test_mandatory_missing_fragment_is_distinct_from_type_failure(self):
        with pytest.raises(ConfigException) as excinfo:
            ConfigFactory.parse_string(render("""
sink {
  cols = [@SLOT_ID@, ${sink.patch}]
}
""", SLOT_ID=slot("id", "int")))
        # a missing mandatory substitution is a resolution error, not a
        # wrong-type error and not a grammar error.
        assert not isinstance(excinfo.value, ConfigWrongTypeException)
        assert not isinstance(excinfo.value, ParseBaseException)


class TestMinimizedFailureReproducesDivergence:
    """Shortening a failing config keeps the same column's type divergence."""

    FULL_FAILING_CONCAT = render("""
sink {
  base = @BASE_LIST@
  patch = "@BAD@"
  tail = @TAIL_LIST@
  unrelated { nested { keep = true } }
  cols = ${sink.base} ${?sink.patch} ${sink.tail}
}
""",
                                 BASE_LIST=typed_list(BASE_COLUMNS),
                                 BAD=BAD_TEXT_FRAGMENT,
                                 TAIL_LIST=typed_list(TAIL_COLUMNS))

    # A single optional substitution just yields the scalar verbatim, so the
    # minimized config keeps one typed list anchor: only then does the concat
    # boundary enforce the ConfigList type and expose the divergence.
    MINIMIZED_FAILING_CONCAT = render("""
sink {
  anchor = []
  patch = "@BAD@"
  cols = ${sink.anchor} ${?sink.patch}
}
""", BAD=BAD_TEXT_FRAGMENT)

    def test_full_and_minimized_concat_fail_on_same_fragment(self):
        with pytest.raises(ConfigWrongTypeException) as full_exc:
            ConfigFactory.parse_string(self.FULL_FAILING_CONCAT)
        with pytest.raises(ConfigWrongTypeException) as short_exc:
            ConfigFactory.parse_string(self.MINIMIZED_FAILING_CONCAT)

        for exc in (full_exc, short_exc):
            message = str(exc.value)
            assert BAD_TEXT_FRAGMENT in message
            assert "index 1" in message
            assert "ConfigList" in message
            assert not isinstance(exc.value, ParseBaseException)

    def test_minimized_concat_failure_is_semantic_not_syntactic(self):
        # control case: genuinely malformed HOCON fails at the grammar level.
        with pytest.raises(ParseBaseException):
            ConfigFactory.parse_string("sink { cols = [ {name = id} ")
        # the minimized mapping parses grammatically and only fails while
        # resolving types, so the divergence must not be mistaken for syntax.
        unresolved = ConfigFactory.parse_string(
            self.MINIMIZED_FAILING_CONCAT, resolve=False
        )
        assert unresolved is not None
        with pytest.raises(ConfigWrongTypeException):
            ConfigFactory.parse_string(self.MINIMIZED_FAILING_CONCAT)

    def test_full_and_minimized_element_divergence_keep_same_slot_type(self):
        full = ConfigFactory.parse_string(render("""
sink {
  patch = "@BAD@"
  cols = [@SLOT_ID@, ${?sink.patch}, @SLOT_NOTE@]
}
""",
                                                 BAD=BAD_TEXT_FRAGMENT,
                                                 SLOT_ID=slot("id", "int"),
                                                 SLOT_NOTE=slot("note", "string")))
        minimized = ConfigFactory.parse_string(render("""
sink {
  patch = "@BAD@"
  cols = [${?sink.patch}]
}
""", BAD=BAD_TEXT_FRAGMENT))
        full_columns, _, full_untyped = analyze_typed_list(full)
        short_columns, _, short_untyped = analyze_typed_list(minimized)
        # both configurations reproduce the same text-typed slot kind.
        assert full_untyped == [(1, "str", BAD_TEXT_FRAGMENT)]
        assert short_untyped == [(0, "str", BAD_TEXT_FRAGMENT)]
        assert full_columns == [("id", "int"), ("note", "string")]
        assert short_columns == []

    def test_minimized_cross_layer_failure_still_names_same_column(self, tmp_path):
        (tmp_path / "fragments.conf").write_text(
            render('patch = "@BAD@"\n', BAD=BAD_TEXT_FRAGMENT)
        )
        full_main = tmp_path / "full.conf"
        full_main.write_text(render("""
include "fragments.conf"
sink {
  base = @BASE_LIST@
  tail = @TAIL_LIST@
  cols = ${sink.base} ${?patch} ${sink.tail}
}
""",
                                    BASE_LIST=typed_list(BASE_COLUMNS),
                                    TAIL_LIST=typed_list(TAIL_COLUMNS)))
        short_main = tmp_path / "short.conf"
        short_main.write_text(
            'include "fragments.conf"\n'
            'anchor = []\n'
            'cols = ${anchor} ${?patch}\n'
        )
        for path in (full_main, short_main):
            with pytest.raises(ConfigWrongTypeException, match="amount,decimal"):
                ConfigFactory.parse_file(str(path))
