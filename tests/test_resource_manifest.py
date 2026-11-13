# -*- encoding: utf-8 -*-
import json

import pytest

from pyhocon import (ConfigCircularIncludeException,
                    ConfigForbiddenIncludeException, ConfigFactory,
                    ResourceManifest, origin_view)
from pyhocon.resource_manifest import (INCLUDE_FORBIDDEN,
                                       INCLUDE_MISSING_OPTIONAL,
                                       normalize_uri, relative_uri)


def write(path, content):
    with open(str(path), 'w') as fd:
        fd.write(content)


def uri(path):
    return normalize_uri(str(path))


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'shared').mkdir()
    (tmp_path / 'dir_a').mkdir()
    (tmp_path / 'dir_b').mkdir()
    write(tmp_path / 'shared' / 'base.conf',
          'col_x: from_base\ncol_shared: 1\n')
    write(tmp_path / 'dir_a' / 'mapping.conf',
          'col_a: alpha\ncol_x: from_a\nnested { inner: 1 }\n')
    write(tmp_path / 'dir_b' / 'mapping.conf',
          'col_b: beta\n')
    write(tmp_path / 'root.conf',
          'include "shared/base.conf"\n'
          'include "dir_a/mapping.conf"\n'
          'include "dir_b/mapping.conf"\n'
          'include "optional_missing.conf"\n'
          'col_local: 42\n')
    return tmp_path


class TestResourceManifest(object):
    def test_columns_carry_winning_resource_and_version(self, project):
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))

        assert config['col_x'] == 'from_a'
        view = origin_view(config, manifest)

        a_uri = uri(project / 'dir_a' / 'mapping.conf')
        b_uri = uri(project / 'dir_b' / 'mapping.conf')
        base_uri = uri(project / 'shared' / 'base.conf')
        root_uri = uri(project / 'root.conf')

        assert view['col_a']['resource'] == a_uri
        assert view['col_b']['resource'] == b_uri
        assert view['col_local']['resource'] == root_uri
        assert view['col_shared']['resource'] == base_uri
        assert view['nested.inner']['resource'] == a_uri

        # Every real column points at a resource with a frozen version.
        for column, provenance in view.items():
            assert isinstance(provenance, dict)
            assert len(provenance['version']) == 64
            assert provenance['status'] == 'loaded'

        # The winning resource for an overridden column is the override.
        assert view['col_x']['resource'] == a_uri

    def test_include_chain_is_recorded_per_column(self, project):
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))
        chain = origin_view(config, manifest)['col_a']['chain']
        assert chain == [uri(project / 'root.conf'),
                         uri(project / 'dir_a' / 'mapping.conf')]

    def test_relative_references_resolve_from_introducer(self, tmp_path):
        deep = tmp_path / 'deep'
        deep.mkdir()
        write(deep / 'leaf.conf', 'leaf_col: true\n')
        write(deep / 'mid.conf', 'mid_col: 5\ninclude "leaf.conf"\n')
        write(tmp_path / 'entry.conf', 'include "deep/mid.conf"\n')

        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(tmp_path / 'entry.conf'))
        chain = origin_view(config, manifest)['leaf_col']['chain']
        assert chain == [uri(tmp_path / 'entry.conf'),
                         uri(deep / 'mid.conf'),
                         uri(deep / 'leaf.conf')]

        assert relative_uri(uri(tmp_path / 'entry.conf'), 'deep/mid.conf') == \
            uri(deep / 'mid.conf')

    def test_diamond_include_keeps_single_resource_and_chain(self, tmp_path):
        write(tmp_path / 'shared.conf', 'shared_col: 1\n')
        write(tmp_path / 'left.conf', 'include "shared.conf"\nleft_col: 2\n')
        write(tmp_path / 'right.conf', 'include "shared.conf"\nright_col: 3\n')
        write(tmp_path / 'top.conf',
              'include "left.conf"\ninclude "right.conf"\n')

        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(tmp_path / 'top.conf'))
        shared = uri(tmp_path / 'shared.conf')
        # One physical resource reached through two include paths.
        assert manifest.resources[shared]['version'] is not None
        chain = origin_view(config, manifest)['shared_col']['chain']
        assert chain[0] == uri(tmp_path / 'top.conf')
        assert chain[-1] == shared
        assert len(chain) == 3


    def test_overridden_definitions_are_distinguished(self, project):
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))
        overrides = manifest.overridden_columns()
        assert len(overrides) == 1
        override = overrides[0]
        assert override['column'] == 'col_x'
        assert override['winner'] == uri(project / 'dir_a' / 'mapping.conf')
        assert override['shadowed'] == uri(project / 'shared' / 'base.conf')

    def test_missing_optional_vs_required(self, project):
        _, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))
        missing = manifest.missing_resources()
        assert len(missing) == 1
        assert missing[0]['uri'] == uri(project / 'optional_missing.conf')
        assert missing[0]['required'] is False
        outcomes = [event['outcome'] for event in manifest.includes]
        assert INCLUDE_MISSING_OPTIONAL in outcomes

        write(project / 'req.conf', 'include required("nope.conf")\n')
        with pytest.raises(IOError):
            ConfigFactory.parse_file_with_manifest(str(project / 'req.conf'))

    def test_forbidden_source_is_rejected_and_recorded(self, project):
        forbidden = uri(project / 'dir_a' / 'mapping.conf')
        manifest = ResourceManifest(forbidden=[forbidden])
        with pytest.raises(ConfigForbiddenIncludeException) as exc_info:
            ConfigFactory.parse_file(
                str(project / 'root.conf'), manifest=manifest)
        assert exc_info.value.target == forbidden
        # The forbidden edge is recorded before the exception propagates so it
        # stays distinguishable from missing resources.
        assert any(event['outcome'] == INCLUDE_FORBIDDEN
                   and event['target'] == forbidden
                   for event in manifest.includes)
        # A forbidden source is never loaded, so it carries no content version.
        assert manifest.version_of(forbidden) is None
        assert manifest.missing_resources() == []

    def test_forbidden_glob_pattern_rejects_matches(self, tmp_path):
        secret = tmp_path / 'secret.conf'
        write(secret, 'secret_col: 1\n')
        write(tmp_path / 'main.conf', 'include "secret.conf"\n')
        pattern = uri(tmp_path / 'secret*.conf')
        manifest = ResourceManifest(forbidden=[pattern])
        with pytest.raises(ConfigForbiddenIncludeException) as exc_info:
            ConfigFactory.parse_file(
                str(tmp_path / 'main.conf'), manifest=manifest)
        assert exc_info.value.target == uri(secret)

    def test_same_filename_in_different_dirs_are_distinct(self, project):
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))
        a_uri = uri(project / 'dir_a' / 'mapping.conf')
        b_uri = uri(project / 'dir_b' / 'mapping.conf')
        assert a_uri != b_uri
        assert manifest.version_of(a_uri) != manifest.version_of(b_uri)
        view = origin_view(config, manifest)
        assert view['col_a']['resource'] == a_uri
        assert view['col_b']['resource'] == b_uri

    def test_same_resource_via_two_aliases(self, project):
        write(project / 'alias.conf',
              'include "dir_a/mapping.conf"\n'
              'include "./dir_a/./mapping.conf"\n')
        _, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'alias.conf'))
        target = uri(project / 'dir_a' / 'mapping.conf')
        record = manifest.resources[target]
        # One physical resource, one version, but both alias spellings kept.
        assert len(record['aliases']) == 2

        # The same physical file is reached twice but registered once.
        included = [r for r in manifest.resources.values()
                    if r['uri'] != manifest.root_uri and not r['missing']]
        assert len(included) == 1
        assert included[0]['uri'] == target

    def test_cycle_reports_actual_resource_chain(self, project):
        write(project / 'dir_a' / 'cyc1.conf',
              'include "../dir_b/cyc2.conf"\na: 1\n')
        write(project / 'dir_b' / 'cyc2.conf',
              'include "../dir_a/cyc1.conf"\nb: 2\n')
        with pytest.raises(ConfigCircularIncludeException) as exc_info:
            ConfigFactory.parse_file_with_manifest(
                str(project / 'dir_a' / 'cyc1.conf'))
        chain = exc_info.value.chain
        assert chain[0] == uri(project / 'dir_a' / 'cyc1.conf')
        assert chain[1] == uri(project / 'dir_b' / 'cyc2.conf')
        assert chain[-1] == uri(project / 'dir_a' / 'cyc1.conf')

    def test_glob_include_tracks_each_matched_resource(self, tmp_path):
        parts = tmp_path / 'parts'
        parts.mkdir()
        write(parts / 'one.conf', 'one: 1\nshared { a: 1 }\n')
        write(parts / 'two.conf', 'two: 2\nshared { b: 2 }\n')
        write(tmp_path / 'glob.conf', 'include "parts/*.conf"\n')

        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(tmp_path / 'glob.conf'))
        view = origin_view(config, manifest)
        assert view['one']['resource'] == uri(parts / 'one.conf')
        assert view['two']['resource'] == uri(parts / 'two.conf')
        assert view['shared.a']['resource'] == uri(parts / 'one.conf')
        assert view['shared.b']['resource'] == uri(parts / 'two.conf')

    def test_list_include_tracks_per_element_origins(self, tmp_path):
        write(tmp_path / 'vals.conf', '[10, 20]\n')
        write(tmp_path / 'list.conf',
              'nums: [ include "vals.conf", 30 ]\n')
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(tmp_path / 'list.conf'))
        assert config['nums'] == [10, 20, 30]
        nums = origin_view(config, manifest)['nums']
        vals_uri = uri(tmp_path / 'vals.conf')
        root_uri = uri(tmp_path / 'list.conf')
        assert [item['resource'] for item in nums] == \
            [vals_uri, vals_uri, root_uri]

    def test_manifest_json_roundtrip_and_offline_rebuild(self, project):
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))
        frozen = manifest.freeze_columns(config)

        # The single in-memory artifact is fully JSON serializable.
        restored = ResourceManifest.from_json(manifest.to_json())
        assert restored.to_dict() == manifest.to_dict()
        assert ResourceManifest.diff_columns(frozen, restored.columns) == []

        # Another (offline) machine can reconstruct columns and their sources
        # from the serialized manifest alone.
        payload = json.loads(manifest.to_json())
        assert set(payload['columns']) == set(frozen)
        for column, provenance in payload['columns'].items():
            assert provenance['version'] is not None
            assert provenance['chain']

    def test_diff_detects_drift(self, project):
        config, manifest = ConfigFactory.parse_file_with_manifest(
            str(project / 'root.conf'))
        frozen = manifest.freeze_columns(config)
        changed = dict(frozen)
        changed['col_a'] = dict(changed['col_a'], version='other-version')
        diffs = ResourceManifest.diff_columns(frozen, changed)
        assert len(diffs) == 1
        assert diffs[0]['kind'] == 'mismatch'
        assert diffs[0]['column'] == 'col_a'

    def test_parse_string_with_manifest(self, tmp_path):
        write(tmp_path / 'extra.conf', 'extra_col: 9\n')
        source = tmp_path / 'inline.conf'
        config, manifest = ConfigFactory.parse_string_with_manifest(
            'include "extra.conf"\nlocal: 1\n',
            source_uri=str(source), basedir=str(tmp_path))
        view = origin_view(config, manifest)
        assert view['extra_col']['resource'] == uri(tmp_path / 'extra.conf')
        assert view['local']['resource'] == uri(source)

    def test_legacy_parse_without_manifest_unchanged(self, project):
        config = ConfigFactory.parse_file(str(project / 'root.conf'))
        assert config['col_local'] == 42
        assert config['col_x'] == 'from_a'
        assert not hasattr(config, '_origin_map')
