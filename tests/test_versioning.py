"""Tests for version registry sorting and paged sorted-registry merging."""

from __future__ import annotations

import random

import pytest

from natsort import (
    PageCursor,
    VersionPage,
    VersionRecord,
    index_versionsorted,
    iter_merged_versions,
    merge_sorted_versions,
    merge_versions,
    natsorted,
    order_by_index,
    sorted_version_records,
    version_records,
    version_sort_keygen,
    versionsorted,
)


class TestVersionRecords:
    def test_global_order_follows_ingestion_sequence(self) -> None:
        records = version_records(["2.0.0", "1.0.0", "plain"])
        assert [record.order for record in records] == [0, 1, 2]
        assert [record.value for record in records] == ["2.0.0", "1.0.0", "plain"]

    def test_start_offsets_the_sequence_numbers(self) -> None:
        records = version_records(["1.0.0", "2.0.0"], start=10)
        assert [record.order for record in records] == [10, 11]

    def test_build_is_preserved_verbatim_but_never_drives_order(self) -> None:
        records = version_records(["1.0.0+Build.A", "1.0.0+build.b"])
        assert records[0].build == "Build.A"
        assert records[1].build == "build.b"
        assert records[0].semver == records[1].semver

    def test_non_semantic_values_land_in_natural_zone(self) -> None:
        records = version_records(["plain-label", "1.0.0"])
        assert records[0].semver is None
        assert records[0].is_semver is False
        assert records[1].is_semver is True

    def test_version_callable_extracts_text(self) -> None:
        rows = [{"name": "a", "ver": "1.0.0"}, {"name": "b", "ver": None}]
        records = version_records(rows, version=lambda row: row["ver"])
        assert records[0].semver is not None
        assert records[1].semver is None

    def test_strict_mode_rejects_shortened_forms_to_natural_zone(self) -> None:
        records = version_records(["v1.2", "1.2.3"], strict=True)
        assert records[0].semver is None
        assert records[1].semver is not None


class TestVersionSorted:
    def test_semver_precedence_with_prereleases(self) -> None:
        given = [
            "1.0.0",
            "2.0.0-alpha",
            "1.0.0-alpha.1",
            "2.0.0",
            "1.0.0-alpha",
        ]
        expected = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0",
            "2.0.0-alpha",
            "2.0.0",
        ]
        assert versionsorted(given) == expected

    def test_equal_precedence_ties_keep_ingestion_order_regardless_of_build(
        self,
    ) -> None:
        given = ["1.0.0+build.2", "1.0.0+build.1", "1.0.0"]
        assert versionsorted(given) == given

    def test_semver_zone_precedes_natural_zone(self) -> None:
        given = ["node-10", "2.0.0", "node-2", "1.0.0"]
        assert versionsorted(given) == ["1.0.0", "2.0.0", "node-2", "node-10"]

    def test_natural_zone_keeps_historical_natsort_semantics(self) -> None:
        plain = ["a10", "a2", "b1", "a1", "a20"]
        assert versionsorted(plain) == natsorted(plain)

    def test_reverse_reverses_the_full_ordering(self) -> None:
        given = ["1.0.0+z", "plain-2", "1.0.0+a", "plain-10"]
        assert versionsorted(given, reverse=True) == list(
            reversed(versionsorted(given)),
        )

    def test_version_callable_on_mapping_rows(self) -> None:
        rows = [
            {"ver": "2.0.0"},
            {"ver": "1.0.0-alpha"},
            {"ver": "1.0.0"},
        ]
        assert [
            row["ver"] for row in versionsorted(rows, version=lambda r: r["ver"])
        ] == [
            "1.0.0-alpha",
            "1.0.0",
            "2.0.0",
        ]

    def test_plain_zone_falls_back_to_whole_value_when_label_is_none(self) -> None:
        # The callable recognizes only digit-leading strings as versions;
        # everything else yields None and must stay naturally sortable.
        def version(value: str) -> str | None:
            return value if value[:1].isdigit() else None

        given = ["node-10", "1.0.0", "node-2"]
        assert versionsorted(given, version=version) == [
            "1.0.0",
            "node-2",
            "node-10",
        ]

    def test_long_numeric_prerelease_is_compared_exactly(self) -> None:
        smaller = "1.0.0-alpha." + "9" * 250
        larger = "1.0.0-alpha." + "1" + "0" * 250
        assert versionsorted([larger, smaller]) == [smaller, larger]

    def test_version_sort_keygen_matches_versionsorted(self) -> None:
        given = ["10.0.0", "2.0.0", "legacy-10", "legacy-2", "1.0.0"]
        key = version_sort_keygen()
        assert sorted(given, key=key) == versionsorted(given)

    def test_index_versionsorted_reorders_parallel_lists(self) -> None:
        versions = ["2.0.0", "1.0.0", "1.0.0+b1"]
        owners = ["carol", "alice", "bob"]
        index = index_versionsorted(versions)
        assert order_by_index(versions, index) == ["1.0.0", "1.0.0+b1", "2.0.0"]
        assert order_by_index(owners, index) == ["alice", "bob", "carol"]

    def test_index_versionsorted_reverse(self) -> None:
        given = ["1.0.0", "2.0.0", "3.0.0"]
        assert index_versionsorted(given, reverse=True) == [2, 1, 0]


class TestMergeSortedVersions:
    def test_basic_two_way_merge(self) -> None:
        left = ["1.0.0", "3.0.0", "5.0.0"]
        right = ["2.0.0", "4.0.0"]
        page = merge_sorted_versions([left, right])
        assert page.values == ["1.0.0", "2.0.0", "3.0.0", "4.0.0", "5.0.0"]
        assert page.finished is True
        assert page.cursor.offsets == (3, 2)

    def test_merge_matches_global_versionsorted(self) -> None:
        values = [
            "1.0.0+b2",
            "3.0.0",
            "1.0.0-alpha",
            "2.0.0",
            "1.0.0+b1",
            "legacy-2",
            "legacy-10",
        ]
        records = version_records(values)
        left: list[VersionRecord[str]] = sorted_version_records(
            records[index] for index in (0, 1, 2)
        )
        right: list[VersionRecord[str]] = sorted_version_records(
            records[index] for index in (3, 4, 5, 6)
        )
        merged: list[str] = merge_versions([left, right])
        assert merged == versionsorted(values)

    @pytest.mark.parametrize("page_size", [1, 2, 3, 4, 5, 7, 100])
    def test_paging_never_skips_or_repeats_a_row(self, page_size: int) -> None:
        left = ["1.0.0", "2.0.0", "4.0.0", "6.0.0"]
        right = ["1.5.0", "2.5.0", "3.5.0", "5.5.0", "7.0.0"]
        cursor: PageCursor | None = None
        seen: list[str] = []
        for _ in range(10):
            page = merge_sorted_versions([left, right], page_size, cursor)
            seen.extend(page.values)
            cursor = page.cursor
            if page.finished:
                break
        assert seen == merge_versions([left, right])
        assert len(seen) == len(set(seen))

    def test_cursor_resumes_inside_an_equal_precedence_build_tie(self) -> None:
        # Three builds of identical precedence with global ingestion
        # numbers that interleave across the two partitions.
        records = version_records(
            [
                "1.0.0+build.1",
                "1.0.0+build.2",
                "1.0.0+build.3",
                "2.0.0",
            ],
        )
        left: list[VersionRecord[str]] = sorted_version_records(
            [records[0], records[2]]
        )
        right: list[VersionRecord[str]] = sorted_version_records(
            [records[1], records[3]]
        )
        first: VersionPage[str] = merge_sorted_versions([left, right], page_size=2)
        assert first.values == ["1.0.0+build.1", "1.0.0+build.2"]
        assert first.cursor.offsets == (1, 1)
        assert first.finished is False
        # The boundary sits between build.2 and build.3, same precedence.
        second: VersionPage[str] = merge_sorted_versions(
            [left, right], 10, first.cursor
        )
        assert second.values == ["1.0.0+build.3", "2.0.0"]
        assert second.finished is True

    def test_tuple_cursor_is_accepted(self) -> None:
        left = ["1.0.0", "3.0.0"]
        right = ["2.0.0", "4.0.0"]
        page: VersionPage[str] = merge_sorted_versions([left, right], 1, (0, 0))
        assert page.values == ["1.0.0"]
        resumed: VersionPage[str] = merge_sorted_versions(
            [left, right], 1, page.cursor.offsets
        )
        assert resumed.values == ["2.0.0"]

    def test_page_records_carry_build_and_global_order(self) -> None:
        left_records = version_records(["1.0.0+b1", "3.0.0"])
        right_records = version_records(["1.0.0+b2", "2.0.0"], start=10)
        page: VersionPage[str] = merge_sorted_versions([left_records, right_records])
        assert [record.build for record in page.records] == [
            "b1",
            "b2",
            None,
            None,
        ]
        assert [record.order for record in page.records] == [0, 10, 11, 1]

    def test_empty_partitions_and_empty_input(self) -> None:
        empty: list[str] = []
        page: VersionPage[str] = merge_sorted_versions([empty, empty], page_size=5)
        assert page.values == []
        assert page.finished is True
        assert page.cursor.offsets == (0, 0)

    def test_iter_merged_versions_walks_every_row_once(self) -> None:
        left = ["1.0.0", "4.0.0"]
        right = ["2.0.0", "3.0.0", "5.0.0"]
        pages = list(iter_merged_versions([left, right], 2))
        assert [page.values for page in pages] == [
            ["1.0.0", "2.0.0"],
            ["3.0.0", "4.0.0"],
            ["5.0.0"],
        ]
        assert pages[-1].finished is True

    def test_randomized_paging_is_lossless_for_every_page_size(self) -> None:
        rng = random.Random(20261112)  # noqa: S311 deterministic test seed
        values = [
            f"{rng.randint(1, 5)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}+build.{index}"
            for index in range(60)
        ]
        shuffled = values[:]
        rng.shuffle(shuffled)
        midpoint = 25
        left = versionsorted(shuffled[:midpoint])
        right = versionsorted(shuffled[midpoint:])
        expected = merge_versions([left, right])

        for page_size in range(1, 12):
            cursor = None
            collected: list[str] = []
            while True:
                page = merge_sorted_versions([left, right], page_size, cursor)
                collected.extend(page.values)
                cursor = page.cursor
                if page.finished:
                    break
            assert collected == expected
            assert len(collected) == len(set(collected))

    def test_invalid_page_size_raises(self) -> None:
        with pytest.raises(ValueError, match="page_size"):
            merge_sorted_versions([["1.0.0"]], page_size=0)
        with pytest.raises(ValueError, match="page_size"):
            merge_sorted_versions([["1.0.0"]], page_size=True)

    def test_resume_is_exact_after_repartitioning_inside_a_build_tie(self) -> None:
        # Four rows where the three builds share one precedence and carry
        # global ingestion numbers that interleave across partitions.
        records = version_records(
            [
                "1.0.0+build.1",
                "1.0.0+build.2",
                "1.0.0+build.3",
                "2.0.0",
            ],
        )
        expected = [record.value for record in sorted_version_records(records)]

        # First layout: two partitions. Read a page that stops between
        # build.2 and build.3, i.e. in the middle of the equal-precedence tie.
        layout_one: list[list[VersionRecord[str]]] = [
            sorted_version_records([records[0], records[2]]),
            sorted_version_records([records[1], records[3]]),
        ]
        first: VersionPage[str] = merge_sorted_versions(layout_one, page_size=2)
        assert first.values == ["1.0.0+build.1", "1.0.0+build.2"]

        # Second layout: re-split the same records across three partitions.
        layout_two: list[list[VersionRecord[str]]] = [
            sorted_version_records([records[0]]),
            sorted_version_records([records[1], records[2]]),
            sorted_version_records([records[3]]),
        ]
        second: VersionPage[str] = merge_sorted_versions(layout_two, 10, first.cursor)
        assert second.values == ["1.0.0+build.3", "2.0.0"]
        assert second.finished is True

        joined = first.values + second.values
        assert joined == expected
        assert len(joined) == len(set(joined))

    def test_resume_with_single_partition_after_multi_partition_read(self) -> None:
        values = [
            "1.0.0+build.2",
            "1.0.0+build.1",
            "1.0.0-alpha",
            "2.0.0",
            "legacy-2",
            "legacy-10",
        ]
        records = version_records(values)
        expected = [record.value for record in sorted_version_records(records)]

        first: VersionPage[str] = merge_sorted_versions(
            [
                sorted_version_records(records[:3]),
                sorted_version_records(records[3:]),
            ],
            page_size=3,
        )
        # Collapse to a single partition before reading the rest.
        resumed: VersionPage[str] = merge_sorted_versions(
            [sorted_version_records(records)],
            10,
            first.cursor,
        )
        joined = first.values + resumed.values
        assert joined == expected
        assert len(joined) == len(set(joined))

    def test_stale_offsets_are_ignored_when_layout_changes(self) -> None:
        # The offset hint points past the (smaller) new partition; the
        # global anchor must still produce a lossless continuation.
        records = version_records(["1.0.0", "2.0.0", "3.0.0", "4.0.0"])
        first: VersionPage[str] = merge_sorted_versions(
            [
                sorted_version_records(records[:2]),
                sorted_version_records(records[2:]),
            ],
            page_size=2,
        )
        resumed: VersionPage[str] = merge_sorted_versions(
            [sorted_version_records(records)],
            10,
            first.cursor,
        )
        assert resumed.values == ["3.0.0", "4.0.0"]

    def test_page_cursor_start_factory(self) -> None:
        assert PageCursor.start(3).offsets == (0, 0, 0)
        assert PageCursor.start(3).last_key is None
        with pytest.raises(ValueError, match="non-negative"):
            PageCursor.start(-1)
