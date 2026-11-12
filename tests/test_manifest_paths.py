# ruff: noqa: SLF001
"""Tests for manifest-aware PATH sorting (roots, relative paths, sources)."""

from __future__ import annotations

import pytest

from natsort import (
    index_natsorted,
    natsort_keygen,
    natsorted,
    ns,
    order_by_index,
    utils,
)


def test_classifies_unc_share_separately() -> None:
    resolved = utils._classify_manifest_path(r"\\server\share\dir\file.txt")
    assert resolved.kind == utils.PATH_KIND_UNC
    assert resolved.anchor == r"\\server\share"
    assert resolved.parts == ("dir", "file.txt")


def test_classifies_drive_absolute_and_relative_distinctly() -> None:
    absolute = utils._classify_manifest_path(r"C:\Users\docs")
    assert absolute.kind == utils.PATH_KIND_DRIVE
    assert absolute.anchor == "C:\\"
    assert absolute.parts == ("Users", "docs")

    relative = utils._classify_manifest_path(r"C:docs\rel")
    assert relative.kind == utils.PATH_KIND_DRIVE_RELATIVE
    assert relative.anchor == "C:"
    assert relative.parts == ("docs", "rel")

    assert absolute.kind != relative.kind


def test_classifies_drive_accepting_either_separator() -> None:
    resolved = utils._classify_manifest_path("C:/Users/docs")
    assert resolved.kind == utils.PATH_KIND_DRIVE
    assert resolved.anchor == "C:\\"
    assert resolved.parts == ("Users", "docs")


def test_classifies_posix_root() -> None:
    resolved = utils._classify_manifest_path("/srv/collection/a")
    assert resolved.kind == utils.PATH_KIND_POSIX
    assert resolved.anchor == "/"
    assert resolved.parts == ("srv", "collection", "a")


def test_literal_backslash_is_a_single_component() -> None:
    resolved = utils._classify_manifest_path(r"name\with\slashes")
    assert resolved.kind == utils.PATH_KIND_LITERAL_BACKSLASH
    assert resolved.anchor == ""
    assert resolved.parts == (r"name\with\slashes",)


def test_plain_relative_path_components() -> None:
    resolved = utils._classify_manifest_path("scans/2024/day1")
    assert resolved.kind == utils.PATH_KIND_RELATIVE
    assert resolved.anchor == ""
    assert resolved.parts == ("scans", "2024", "day1")


def test_two_machine_mounts_map_to_same_dataset_root() -> None:
    mappings = {
        r"\\machineA\archive": "/dataset/alpha",
        r"\\machineB\archive": "/dataset/alpha",
    }
    given = [
        r"\\machineB\archive\shard\file10.txt",
        r"\\machineA\archive\shard\file2.txt",
        r"\\machineA\archive\shard\file1.txt",
    ]
    expected = [
        r"\\machineA\archive\shard\file1.txt",
        r"\\machineA\archive\shard\file2.txt",
        r"\\machineB\archive\shard\file10.txt",
    ]
    # The normalized shards cluster together with natural numeric ordering even
    # though the spellings come from two different source machines.
    assert natsorted(given, alg=ns.PATH, root_mappings=mappings) == expected


def test_mapping_is_lossless_and_keeps_every_manifest_row() -> None:
    mappings = {r"\\machineA\archive": "/dataset/alpha"}
    given = [
        r"\\machineA\archive\shard\file10.txt",
        r"\\machineA\archive\shard\file2.txt",
    ]
    result = natsorted(given, alg=ns.PATH, root_mappings=mappings)
    assert len(result) == len(given)
    assert set(result) == set(given)


def test_deepest_complete_component_mapping_wins() -> None:
    mappings = {"/data": "/ds", "/data/ingest": "/ds2"}
    deep = utils._resolve_manifest_path("/data/ingest/x", mappings)
    assert deep.parts == ("ds2", "x")

    shallow = utils._resolve_manifest_path("/data/other/x", mappings)
    assert shallow.parts == ("ds", "other", "x")


def test_mapping_requires_full_component_boundary() -> None:
    mappings = {"/data": "/ds"}
    # "/database" shares a text prefix but not a path component, so no match.
    resolved = utils._resolve_manifest_path("/database/x", mappings)
    assert resolved.kind == utils.PATH_KIND_POSIX
    assert resolved.parts == ("database", "x")


def test_unbound_relative_never_enters_absolute_zone() -> None:
    mappings = {"/abs/root": "/dataset"}
    relative = utils._resolve_manifest_path("rel/path", mappings)
    assert relative.kind == utils.PATH_KIND_RELATIVE

    literal = utils._resolve_manifest_path(r"win\literal", mappings)
    assert literal.kind == utils.PATH_KIND_LITERAL_BACKSLASH

    given = ["/abs/root/f", "rel/path", r"win\literal", "/abs/root/a"]
    result = natsorted(given, alg=ns.PATH, root_mappings=mappings)
    # Absolute (mapped) entries sort first as one block; relative spellings
    # follow in their own zones and never interleave with the absolute block.
    assert result[:2] == ["/abs/root/a", "/abs/root/f"]
    assert set(result[2:]) == {"rel/path", r"win\literal"}


def test_explicitly_bound_relative_enters_target_zone() -> None:
    mappings = {"scans/2024": "/dataset/scans"}
    bound = utils._resolve_manifest_path("scans/2024/day1", mappings)
    assert bound.kind == utils.PATH_KIND_POSIX
    assert bound.anchor == "/"
    assert bound.parts == ("dataset", "scans", "day1")

    unbound = utils._resolve_manifest_path("other/day1", mappings)
    assert unbound.kind == utils.PATH_KIND_RELATIVE


def test_distinct_families_sort_in_separate_zones() -> None:
    given = [r"C:\b\f10", r"C:\b\f2", "/b/f1", r"\\s\s\f", "rel/f"]
    result = natsorted(given, alg=ns.PATH, root_mappings={"/unrelated": "/x"})
    # Zones: UNC, drive, POSIX, then relative - none interleave.
    assert result.index(r"\\s\s\f") < result.index(r"C:\b\f2")
    assert result.index(r"C:\b\f2") < result.index("/b/f1")
    assert result.index("/b/f1") < result.index("rel/f")
    # Natural order is preserved inside the drive zone.
    assert result.index(r"C:\b\f2") < result.index(r"C:\b\f10")


def test_root_mappings_requires_path_algorithm() -> None:
    with pytest.raises(ValueError, match=r"root_mappings.*ns\.PATH"):
        natsorted(["a"], root_mappings={"a": "b"})
    with pytest.raises(ValueError, match=r"root_mappings.*ns\.PATH"):
        natsort_keygen(root_mappings={"a": "b"})
    # An empty mapping still opts into manifest classification, so PATH is
    # required.
    with pytest.raises(ValueError, match=r"root_mappings.*ns\.PATH"):
        natsort_keygen(root_mappings={})


def test_empty_mapping_enables_manifest_zones_without_legacy_parsing() -> None:
    given = [r"C:\b\f10", r"C:\b\f2", "/b/f1", r"\\s\s\f", r"a\b"]
    result = natsorted(given, alg=ns.PATH, root_mappings={})
    # Distinct families still occupy distinct zones with no mapping declared.
    assert result.index(r"\\s\s\f") < result.index(r"C:\b\f2")
    assert result.index(r"C:\b\f2") < result.index("/b/f1")
    assert result.index("/b/f1") < result.index(r"a\b")
    # The Windows backslash path is genuinely split on this POSIX host.
    key = natsort_keygen(alg=ns.PATH, root_mappings={})
    assert key(r"C:\b\f2") == (
        (str(utils.PATH_KIND_DRIVE),),
        ("C:\\",),
        ("b",),
        ("f", 2),
    )


def test_key_splits_extensions_on_mapped_base() -> None:
    mappings = {r"\\machineA\archive": "/dataset/alpha"}
    key = natsort_keygen(alg=ns.PATH, root_mappings=mappings)
    assert key(r"\\machineA\archive\shard\file10.tar.gz") == (
        (str(utils.PATH_KIND_POSIX),),
        ("/",),
        ("dataset",),
        ("alpha",),
        ("shard",),
        ("file", 10),
        (".tar",),
        (".gz",),
    )


def test_mixed_numbers_and_manifest_paths_remain_sortable() -> None:
    # The string zone marker keeps path keys comparable with numeric keys,
    # mirroring the existing ns.PATH behavior for mixed input.
    assert natsorted(
        [43, r"\\machineA\archive\f"],
        alg=ns.PATH,
        root_mappings={r"\\machineA\archive": "/dataset/alpha"},
    ) == [43, r"\\machineA\archive\f"]


def test_index_natsorted_threads_root_mappings() -> None:
    mappings = {r"\\machineA\archive": "/dataset/alpha"}
    given = [
        r"\\machineA\archive\shard\file10.txt",
        r"\\machineA\archive\shard\file2.txt",
    ]
    index = index_natsorted(given, alg=ns.PATH, root_mappings=mappings)
    assert order_by_index(given, index) == [
        r"\\machineA\archive\shard\file2.txt",
        r"\\machineA\archive\shard\file10.txt",
    ]


def test_legacy_path_behavior_without_mappings_is_unchanged() -> None:
    given = ["/p/Folder (10)/f", "/p/Folder (1)/f", "/p/Folder/f"]
    assert natsorted(given, alg=ns.PATH) == [
        "/p/Folder/f",
        "/p/Folder (1)/f",
        "/p/Folder (10)/f",
    ]
    # No zone marker is introduced when mappings are not supplied.
    key = natsort_keygen(alg=ns.PATH)
    assert key("/p/Folder/f") == (("/",), ("p",), ("Folder",), ("f",))
