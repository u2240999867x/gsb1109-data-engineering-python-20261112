"""
Tests for ftfy.versioned: versioned, atomically published cleaned text with a
per-row change index.
"""

import json

import pytest

import ftfy.versioned
from ftfy import TextFixerConfig
from ftfy.versioned import Change, Row, VersionedTitleStore

BROKEN_TITLES = [
    "âœ” No problems",
    "cafÃ© menu",
    "plain title",
    "âœ” No problems",  # a repeated row, same text as the first occurrence
    "Broken text&hellip; ok",
]
CLEANED_TITLES = [
    "✔ No problems",
    "café menu",
    "plain title",
    "✔ No problems",
    "Broken text… ok",
]


def test_write_and_read_roundtrip(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    version = store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    assert version.version == 1
    assert version.record_count == len(BROKEN_TITLES)
    assert version.batch_id == "batch-1"
    assert store.current_version == 1
    assert store.versions() == [1]

    with store.open_version() as pinned:
        assert list(pinned.read_titles()) == CLEANED_TITLES

    rows = list(version.rows())
    assert len(rows) == len(BROKEN_TITLES)
    assert all(isinstance(row, Row) for row in rows)
    assert [row.cleaned for row in rows] == CLEANED_TITLES
    assert [row.original for row in rows] == BROKEN_TITLES
    assert [row.changed for row in rows] == [True, True, False, True, True]

    changes = list(version.changes())
    assert all(isinstance(change, Change) for change in changes)
    assert len(changes) == 4
    assert [change.position for change in changes] == [row.position for row in rows if row.changed]
    version.close()


def test_index_positions_retrieve_each_occurrence(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    version = store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    positions = [row.position for row in version.rows()]
    # Every occurrence, including the repeated title, gets its own position.
    assert len(set(positions)) == len(positions)

    for change in version.changes():
        original = version.original_at(change.position, change.original_length)
        assert original in BROKEN_TITLES
        assert ftfy.fix_text(original) != original
        # The position points at this exact occurrence inside the raw file.
        raw_path = tmp_path / "titles" / "versions" / "v1.raw.txt"
        with raw_path.open("rb") as raw_file:
            raw_file.seek(change.position)
            assert raw_file.read(change.original_length).decode("utf-8") == original

    duplicate_rows = [row for row in version.rows() if row.original == BROKEN_TITLES[0]]
    assert len(duplicate_rows) == 2
    assert duplicate_rows[0].position != duplicate_rows[1].position
    for row in duplicate_rows:
        assert version.original_at(row.position, len(row.original.encode("utf-8"))) == row.original


def test_stream_is_consumed_exactly_once(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    consumed = []

    def counting_stream():
        for title in BROKEN_TITLES:
            consumed.append(title)
            yield title

    stream = counting_stream()
    store.write_version(stream, batch_id="batch-1")
    assert consumed == BROKEN_TITLES
    with pytest.raises(StopIteration):
        next(stream)


def test_batch_retry_is_idempotent_and_does_not_reconsume(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    first = store.write_version(iter(BROKEN_TITLES), batch_id="dup-batch")
    assert first.version == 1

    def fail_if_consumed():
        message = "the retry must not read the stream again"
        raise AssertionError(message)
        yield  # pragma: no cover

    retried = store.write_version(fail_if_consumed(), batch_id="dup-batch")
    assert retried.version == 1
    assert store.versions() == [1]
    assert store.current_version == 1
    assert list(retried.read_titles()) == CLEANED_TITLES

    # A different batch id creates a distinct next version instead.
    second = store.write_version(iter(["cafÃ© again"]), batch_id="other-batch")
    assert second.version == 2
    assert second.record_count == 1
    assert store.versions() == [1, 2]


def test_versions_coexist_and_old_handle_stays_pinned(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    first = store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    with store.open_version(1) as pinned:
        store.write_version(iter(["new âœ” line"]), batch_id="batch-2")
        assert store.current_version == 2
        # The reader keeps pairing with the version it opened.
        assert list(pinned.read_titles()) == CLEANED_TITLES
        assert pinned.record_count == len(BROKEN_TITLES)

    assert list(first.read_titles()) == CLEANED_TITLES
    with store.open_version(2) as second:
        assert list(second.read_titles()) == ["new ✔ line"]


def test_store_reopens_with_committed_data(tmp_path):
    path = tmp_path / "titles"
    VersionedTitleStore(path).write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    reopened = VersionedTitleStore(path)
    assert reopened.current_version == 1
    with reopened.open_version() as version:
        assert list(version.read_titles()) == CLEANED_TITLES
        assert [row.original for row in version.rows()] == BROKEN_TITLES


def test_empty_stream_writes_empty_version(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    version = store.write_version(iter(()), batch_id="empty")
    assert version.record_count == 0
    assert list(version.rows()) == []
    assert list(version.changes()) == []
    assert list(version.read_titles()) == []
    assert store.current_version == 1


def test_custom_ftfy_config(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles", TextFixerConfig(uncurl_quotes=False))
    curly = "these â€œquotesâ9d"
    version = store.write_version(iter([curly]), batch_id="cfg")
    cleaned = next(version.read_titles())
    assert "“" in cleaned or "”" in cleaned

    keyword_store = VersionedTitleStore(tmp_path / "titles2", unescape_html=False)
    entity_version = keyword_store.write_version(iter(["Broken text&hellip; ok"]), batch_id="cfg2")
    assert next(entity_version.read_titles()) == "Broken text&hellip; ok"


def test_invalid_records_and_batch_ids(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    with pytest.raises(TypeError):
        store.write_version(iter([b"bytes are not accepted"]), batch_id="bad")
    assert store.current_version is None

    with pytest.raises(ValueError):
        store.write_version(iter(["x"]), batch_id="")
    with pytest.raises(ValueError):
        store.write_version(iter(["x"]), batch_id="../escape")
    with pytest.raises(ValueError):
        store.write_version(iter(["x"]), batch_id="a/b")


def test_opening_unknown_or_missing_version(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    with pytest.raises(FileNotFoundError):
        store.open_version()
    with pytest.raises(FileNotFoundError):
        store.open_version(7)

    store.write_version(iter(["x"]), batch_id="b1")
    with pytest.raises(FileNotFoundError):
        store.open_version(2)


def test_closed_handle_rejects_reads(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    version = store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")
    version.close()
    with pytest.raises(ValueError):
        list(version.read_titles())
    with pytest.raises(ValueError):
        version.original_at(0, 1)


def test_recovery_after_failure_during_cleaning(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    def exploding_stream():
        yield "cafÃ©"
        message = "stream broke halfway through"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        store.write_version(exploding_stream(), batch_id="batch-2")

    # No journal was written and no version was published; the partial
    # staging files must not survive a reopen.
    assert store.current_version == 1
    reopened = VersionedTitleStore(tmp_path / "titles")
    assert reopened.current_version == 1
    assert reopened.versions() == [1]
    tmp_files = list((tmp_path / "titles" / "tmp").iterdir())
    assert tmp_files == []

    # The same batch can now be committed cleanly.
    version = reopened.write_version(iter(["cafÃ©", "âœ” done"]), batch_id="batch-2")
    assert version.version == 2
    assert list(version.read_titles()) == ["café", "✔ done"]


def test_recovery_when_pointer_swap_never_happened(tmp_path, monkeypatch):
    store = VersionedTitleStore(tmp_path / "titles")
    store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    def fail_publish(self, number):
        message = "crashed before publishing pointer"
        raise RuntimeError(message)

    monkeypatch.setattr(VersionedTitleStore, "_publish_pointer", fail_publish)
    with pytest.raises(RuntimeError):
        store.write_version(iter(["cafÃ© crash"]), batch_id="batch-2")

    monkeypatch.undo()
    reopened = VersionedTitleStore(tmp_path / "titles")
    assert reopened.current_version == 1
    assert reopened.versions() == [1]
    # The fully-staged but unpublished version was rolled back entirely.
    version_dir = tmp_path / "titles" / "versions"
    assert not (version_dir / "v2.idx").exists()
    assert list((tmp_path / "titles" / "tmp").iterdir()) == []

    version = reopened.write_version(iter(["cafÃ© crash"]), batch_id="batch-2")
    assert version.version == 2
    assert list(version.read_titles()) == ["café crash"]


def test_recovery_when_crash_happened_after_pointer_swap(tmp_path, monkeypatch):
    real_write = ftfy.versioned._write_fsynced_text

    def fail_marker_write(path, text):
        if "batches" in path.parts:
            message = "crashed before writing the batch marker"
            raise RuntimeError(message)
        return real_write(path, text)

    monkeypatch.setattr(ftfy.versioned, "_write_fsynced_text", fail_marker_write)
    store = VersionedTitleStore(tmp_path / "titles")
    with pytest.raises(RuntimeError):
        store.write_version(iter(BROKEN_TITLES), batch_id="lucky-batch")
    monkeypatch.undo()

    # The pointer and all version files are already in place; recovery only
    # has to finish the post-commit bookkeeping.
    reopened = VersionedTitleStore(tmp_path / "titles")
    assert reopened.current_version == 1
    assert reopened.versions() == [1]
    with reopened.open_version(1) as version:
        assert list(version.read_titles()) == CLEANED_TITLES

    marker = tmp_path / "titles" / "batches" / "lucky-batch"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "version": 1,
        "records": len(BROKEN_TITLES),
    }

    def fail_if_consumed():
        message = "the recovered batch must not be reprocessed"
        raise AssertionError(message)
        yield  # pragma: no cover

    retried = reopened.write_version(fail_if_consumed(), batch_id="lucky-batch")
    assert retried.version == 1


def test_recovery_tolerates_corrupt_journal(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")

    tmp_dir = tmp_path / "titles" / "tmp"
    (tmp_dir / "journal-broken.json").write_text("{not json", encoding="utf-8")
    (tmp_dir / "v2.txt").write_text("partial staging\n", encoding="utf-8")

    reopened = VersionedTitleStore(tmp_path / "titles")
    assert reopened.current_version == 1
    assert list(tmp_dir.iterdir()) == []
    with reopened.open_version() as version:
        assert list(version.read_titles()) == CLEANED_TITLES


def test_on_disk_layout(tmp_path):
    store = VersionedTitleStore(tmp_path / "titles")
    store.write_version(iter(BROKEN_TITLES), batch_id="batch-1")
    base = tmp_path / "titles"
    assert (base / "current").read_text(encoding="utf-8") == "1\n"
    assert (base / "lock").exists()
    version_dir = base / "versions"
    for suffix in (".raw.txt", ".txt", ".idx", ".meta.json"):
        assert (version_dir / f"v1{suffix}").exists()
    assert (base / "batches" / "batch-1").exists()


def test_positions_are_byte_offsets_with_multibyte_and_newlines(tmp_path):
    # Read-back is position based rather than line based, so neither
    # multi-byte characters nor an embedded newline can desync the pairing.
    records = ["cafÃ©\nsecond line", "âœ”", "plain"]
    store = VersionedTitleStore(tmp_path / "titles")
    version = store.write_version(iter(records), batch_id="bytes")

    raw_path = tmp_path / "titles" / "versions" / "v1.raw.txt"
    raw = raw_path.read_bytes()
    for row in version.rows():
        assert raw[row.position : row.position + len(row.original.encode("utf-8"))] == (
            row.original.encode("utf-8")
        )
        assert version.original_at(row.position, len(row.original.encode("utf-8"))) == (
            row.original
        )
    assert [row.original for row in version.rows()] == records
    assert list(version.read_titles()) == [ftfy.fix_text(record) for record in records]
