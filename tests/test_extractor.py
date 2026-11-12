# -*- encoding: utf-8 -*-
import os

import pytest

from pyhocon import ConfigFactory, ConfigTree, ConfigMissingException
from pyhocon.config_parser import ConfigParser
from pyhocon.exceptions import ConfigException
from pyhocon.extractor import ConfigExtractor, ConfigExtractionError


TWO_TABLES = """
base {
  prefix = "pre"
}
tables {
  users {
    id = 1
    label = base
    label = ${tables.users.label}/x
    derived = ${base.prefix}-d
    tags = ["a"]
    tags = ${tables.users.tags} ["b"]
    maybe = ${?OPT_VAR}
    need = ${MAND_VAR}
  }
  orders {
    oid = 9
    total = ${tables.users.id}
  }
}
unrelated {
  noise = 42
}
"""

USER_COLUMNS = [
    "tables.users.id",
    "tables.users.label",
    "tables.users.derived",
    "tables.users.tags",
    "tables.users.maybe",
    "tables.users.need",
]


def unresolved(text=TWO_TABLES):
    return ConfigFactory.parse_string(text, resolve=False)


class TestDependencyClosure(object):
    def test_keeps_transitive_dependencies_and_prunes_other_tables(self):
        result = ConfigExtractor.extract(unresolved(), USER_COLUMNS)
        reparsed = ConfigFactory.parse_string(result.hocon, resolve=False)

        assert "unrelated" not in reparsed
        assert "orders" not in reparsed["tables"]
        assert "noise" not in result.hocon
        assert "base" in reparsed
        assert reparsed["base"]["prefix"] == "pre"

    def test_internal_paths_include_ancestors_and_dependencies(self):
        result = ConfigExtractor.extract(unresolved(), USER_COLUMNS)
        assert "base.prefix" in result.internal_paths
        assert "tables.users" in result.internal_paths
        assert "tables.orders.oid" not in result.internal_paths
        assert "unrelated.noise" not in result.internal_paths

    def test_selecting_subtree_preserves_key_order(self):
        result = ConfigExtractor.extract(unresolved(), ["tables.users"])
        ConfigExtractor.assert_equivalent(
            TWO_TABLES, result.hocon, ["tables.users"],
            [{"MAND_VAR": "mv"}, {"MAND_VAR": "mv", "OPT_VAR": "ov"}])
        source = ConfigFactory.parse_string(
            TWO_TABLES, resolve=False)
        assert list(result.config["tables"]["users"].keys()) == \
            list(source["tables"]["users"].keys())

    def test_missing_column_raises(self):
        with pytest.raises(ConfigMissingException):
            ConfigExtractor.extract(unresolved(), ["tables.users.ghost"])

    def test_requires_config_tree(self):
        with pytest.raises(ConfigException):
            ConfigExtractor.extract({"a": 1}, ["a"])


class TestOverrideAndSelfReference(object):
    def test_override_history_is_preserved(self):
        result = ConfigExtractor.extract(unresolved(), ["tables.users.label"])
        config = ConfigFactory.parse_string(result.hocon)
        assert config["tables.users.label"] == "base/x"

    def test_string_and_list_self_append(self):
        text = """
        x = abc
        x += def
        arr = [1]
        arr += [2, 3]
        obj = {a: 1}
        obj += {b: 2}
        nested {
          tags = ["a"]
          tags = ${nested.tags} ["b"]
          label = first
          label = ${nested.label}-second
        }
        """
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False),
            ["x", "arr", "obj", "nested"])
        config = ConfigFactory.parse_string(result.hocon)
        assert config["x"] == "abc def"
        assert config["arr"] == [1, 2, 3]
        assert config["obj"] == {"a": 1, "b": 2}
        assert config["nested"]["tags"] == ["a", "b"]
        assert config["nested"]["label"] == "first-second"

    def test_object_self_reference_is_internal_not_external(self):
        text = """
        foo : { a : { c : 1 } }
        foo : ${foo.a}
        foo : { a : 2 }
        """
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False), ["foo"], strict=True)
        assert result.external_references == []
        config = ConfigFactory.parse_string(result.hocon)
        assert config["foo"] == {"a": 2, "c": 1}


class TestOptionalAndEnvironment(object):
    def test_optional_reference_omitted_then_provided(self):
        text = """
        b = ${?OPT_VAR}
        d = ${?OPT_VAR} 4
        g = ${?O1} ${?O2}
        h = ${?O1} ${?O2} 1
        """
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False), ["b", "d", "g", "h"])
        external = {ref.variable: ref for ref in result.external_references}
        assert set(external) == {"OPT_VAR", "O1", "O2"}
        assert all(ref.optional for ref in external.values())

        empty = ConfigFactory.parse_string(result.hocon)
        assert "b" not in empty
        assert "g" not in empty
        assert empty["d"] == 4
        assert empty["h"] == 1

        provided = ConfigFactory.parse_string(
            result.hocon, resolve=False)
        saved = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update({"OPT_VAR": "zzz", "O1": "p", "O2": "q"})
            ConfigParser.resolve_substitutions(provided)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        assert provided["b"] == "zzz"
        assert provided["d"] == "zzz 4"
        assert provided["g"] == "p q"
        assert provided["h"] == "p q 1"

    def test_mandatory_external_reference_is_reported(self):
        result = ConfigExtractor.extract(unresolved(), USER_COLUMNS)
        external = {ref.variable: ref for ref in result.external_references}
        assert external["MAND_VAR"].optional is False
        assert external["OPT_VAR"].optional is True
        chain = external["MAND_VAR"].chains["tables.users.need"]
        assert chain[0] == "tables.users.need"
        assert any("MAND_VAR" in hop for hop in chain)

    def test_strict_mode_reports_blocking_columns_and_chain(self):
        with pytest.raises(ConfigExtractionError) as excinfo:
            ConfigExtractor.extract(unresolved(), USER_COLUMNS, strict=True)
        error = excinfo.value
        assert error.columns == ["tables.users.need"]
        assert [ref.variable for ref in error.references] == ["MAND_VAR"]
        chain = error.references[0].chains["tables.users.need"]
        assert "tables.users.need" in chain[0]

    def test_transitive_chain_to_environment_variable(self):
        text = """
        a = ${b}
        b = ${?ENV_B}
        """
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False), ["a"])
        external = {ref.variable: ref for ref in result.external_references}
        assert "ENV_B" in external
        chain = external["ENV_B"].chains["a"]
        assert chain == ["a", "${b}", "b", "${?ENV_B}"]

    def test_nested_bare_self_reference_is_environment_dependency(self):
        # A bare ${label} inside a nested scope does not point at the nested
        # field in pyhocon; it resolves against the environment (and fails when
        # absent). The extraction must reproduce that error identically.
        text = """
        tables {
          users {
            label = first
            label = ${label}-second
          }
        }
        """
        unresolved_tree = ConfigFactory.parse_string(text, resolve=False)
        result = ConfigExtractor.extract(unresolved_tree, ["tables.users.label"])
        external = {ref.variable: ref for ref in result.external_references}
        assert external["label"].optional is False
        ConfigExtractor.assert_equivalent(
            text, result.hocon, ["tables.users.label"], [{}])
        ConfigExtractor.assert_equivalent(
            text, result.hocon, ["tables.users.label"], [{"label": "env"}])

    def test_strict_follows_transitive_mandatory_chain(self):
        text = """
        a = ${b}
        b = ${ENV_B}
        """
        with pytest.raises(ConfigExtractionError) as excinfo:
            ConfigExtractor.extract(
                ConfigFactory.parse_string(text, resolve=False), ["a"], strict=True)
        error = excinfo.value
        assert error.columns == ["a"]
        assert [ref.variable for ref in error.references] == ["ENV_B"]
        assert error.references[0].chains["a"] == ["a", "${b}", "b", "${ENV_B}"]


class TestEquivalenceAcrossEnvironments(object):
    def test_values_presence_and_errors_match(self):
        result = ConfigExtractor.extract(unresolved(), USER_COLUMNS)
        environments = [
            {},
            {"MAND_VAR": "mv"},
            {"MAND_VAR": "mv", "OPT_VAR": "ov"},
        ]
        ConfigExtractor.assert_equivalent(
            TWO_TABLES, result.hocon, USER_COLUMNS, environments)

    def test_mandatory_missing_is_an_error_in_both(self):
        text = "a = ${ONLY_IN_ENV}\n"
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False), ["a"])
        # Empty environment: both must raise the same kind of resolution error.
        ConfigExtractor.assert_equivalent(text, result.hocon, ["a"], [{}])
        # Providing the variable: both resolve identically.
        ConfigExtractor.assert_equivalent(
            text, result.hocon, ["a"], [{"ONLY_IN_ENV": "value"}])

    def test_rendered_hocon_round_trips(self):
        result = ConfigExtractor.extract(unresolved(), USER_COLUMNS)
        reparsed = ConfigFactory.parse_string(result.hocon, resolve=False)
        assert isinstance(reparsed, ConfigTree)
        assert reparsed.root is True

    def test_null_values_round_trip(self):
        text = """
        tables {
          users {
            a = null
            b = { c = null }
            d = 5
          }
        }
        """
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False), ["tables.users"])
        assert "a = null" in result.hocon
        assert "c = null" in result.hocon
        ConfigExtractor.assert_equivalent(
            text, result.hocon, ["tables.users"], [{}])

    def test_quoted_dotted_keys_and_list_substitutions(self):
        text = """
        "weird.key" {
          "dot.field" = ${base.p}
        }
        base {
          p = 1
        }
        lst = [${base.p}, 2, ${?OPT}]
        """
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False),
            ['"weird.key"', "lst"])
        reparsed = ConfigFactory.parse_string(result.hocon, resolve=False)
        assert '"weird.key"' in reparsed
        assert "base" in reparsed
        ConfigExtractor.assert_equivalent(
            text, result.hocon, ['"weird.key"', "lst"], [{}, {"OPT": "z"}])

    def test_optional_column_presence_matches_across_environments(self):
        text = "g = ${?G}\n"
        result = ConfigExtractor.extract(
            ConfigFactory.parse_string(text, resolve=False), ["g"])
        assert result.external_references[0].optional is True
        # Absent variable: the column is missing in both. Present: equal in both.
        ConfigExtractor.assert_equivalent(text, result.hocon, ["g"], [{}])
        ConfigExtractor.assert_equivalent(text, result.hocon, ["g"], [{"G": "v"}])
