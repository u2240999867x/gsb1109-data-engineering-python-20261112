"""
ftfy.versioned: atomically versioned, ftfy-cleaned text with a change index.

This module stores a *column of titles* as immutable versions. Every version
delivers one consistent data pair:

- the cleaned text (one record per source line, UTF-8)
- a per-row change index whose positions point back at the stored original
  occurrence of each record

The source is a record stream that can only be read once, and row keys are
allowed to repeat. Identity therefore never depends on a key: every occurrence
gets its own byte position in the stored original text, and the index can
always retrieve that exact occurrence, even when two rows have the same key
or identical text.

A version is built in a staging area and only becomes visible when an atomic
pointer swap publishes it. A reader that opened an older version keeps reading
that complete version while the next version is being written. If cleaning,
index writing, or the version switch is interrupted, reopening the store
exposes either the complete old version or the complete new version -- never a
partial one. Retrying with the same batch id is a no-op: a repeated key is
never counted twice.

Example (no doctest, because it touches the filesystem)::

    from ftfy.versioned import VersionedTitleStore

    store = VersionedTitleStore("title-versions")
    version = store.write_version(stream_of_titles, batch_id="batch-001")
    for row in version.rows():
        if row.changed:
            print(row.position, row.original, "->", row.cleaned)

    with store.open_version(version.version) as pinned:
        titles = list(pinned.read_titles())
"""

from __future__ import annotations

import json
import os
import struct
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, NamedTuple

from ftfy import TextFixerConfig, _config_from_kwargs, fix_text

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

try:
    import fcntl  # type: ignore[import-not-found,unused-ignore]
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

#: Magic prefix of an index file, followed by a little-endian uint64 count.
_INDEX_MAGIC = b"FTFYIDX1\n"
_COUNT_STRUCT = struct.Struct("<Q")
#: One index record: raw position, raw length, cleaned position, cleaned
#: length (all byte counts, little-endian uint64) and a changed flag.
_RECORD_STRUCT = struct.Struct("<QQQQ?")

_RAW_SUFFIX = ".raw.txt"
_TEXT_SUFFIX = ".txt"
_INDEX_SUFFIX = ".idx"
_META_SUFFIX = ".meta.json"
_NEWLINE = "\n"
_SAFE_BATCH_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_EMPTY_BATCH_ERROR = "batch_id must be a non-empty string"
_BATCH_CHARS_ERROR = "batch_id may only contain letters, digits, dots, underscores, and hyphens"
_NON_STRING_RECORD_ERROR = "title records must be str; decode bytes before writing"
_NO_VERSION_ERROR = "no version has been written yet"


class Change(NamedTuple):
    """
    A row whose text changed during cleaning.

    `position` is the byte offset of this occurrence in the version's stored
    original text. Duplicate row keys do not collide: every occurrence has its
    own position and can be retrieved independently with
    :meth:`Version.original_at`.
    """

    position: int
    original_length: int
    cleaned_length: int


class Row(NamedTuple):
    """
    One indexed record, pairing the original text with the cleaned text from
    the same version.
    """

    position: int
    original: str
    cleaned: str
    changed: bool


def _fsync_directory(path: Path) -> None:
    """
    Flush a directory so that file creation, removal, and renames inside it
    are durable.
    """
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_fsynced_text(path: Path, text: str) -> None:
    """Write a whole text file, flushing it to disk before returning."""
    with path.open("w", encoding="utf-8") as file_obj:
        file_obj.write(text)
        file_obj.flush()
        os.fsync(file_obj.fileno())


class Version:
    """
    A readable, immutable handle on one published version.

    The underlying raw text, cleaned text, and index files are held open for
    the lifetime of the handle, so a writer publishing a newer version cannot
    disturb an in-progress read: every lookup stays paired with the text and
    index of this exact version.
    """

    def __init__(self, versions_dir: Path, number: int) -> None:
        self.version = number
        self._raw_path = versions_dir / f"v{number}{_RAW_SUFFIX}"
        self._text_path = versions_dir / f"v{number}{_TEXT_SUFFIX}"
        self._index_path = versions_dir / f"v{number}{_INDEX_SUFFIX}"
        meta_path = versions_dir / f"v{number}{_META_SUFFIX}"

        opened: list[IO[bytes]] = []
        try:
            self._raw_file = self._raw_path.open("rb")
            opened.append(self._raw_file)
            self._text_file = self._text_path.open("rb")
            opened.append(self._text_file)
            self._index_file = self._index_path.open("rb")
            opened.append(self._index_file)
            magic = self._index_file.read(len(_INDEX_MAGIC))
            if magic != _INDEX_MAGIC:
                message = f"{self._index_path} is not an ftfy version index"
                raise ValueError(message)
            (self.record_count,) = _COUNT_STRUCT.unpack(self._index_file.read(_COUNT_STRUCT.size))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except BaseException:
            for file_obj in opened:
                file_obj.close()
            self.closed = True
            raise
        self.batch_id: str = meta["batch_id"]
        self.closed = False

    def _check_open(self) -> None:
        if self.closed:
            message = f"version {self.version} has been closed"
            raise ValueError(message)

    def _entries(self) -> Iterator[tuple[int, int, int, int, bool]]:
        self._check_open()
        self._index_file.seek(len(_INDEX_MAGIC) + _COUNT_STRUCT.size)
        for _ in range(self.record_count):
            raw_pos, raw_len, clean_pos, clean_len, changed = _RECORD_STRUCT.unpack(
                self._index_file.read(_RECORD_STRUCT.size)
            )
            yield raw_pos, raw_len, clean_pos, clean_len, changed

    def read_titles(self) -> Iterator[str]:
        """Yield the cleaned title of every record, in stream order."""
        self._check_open()
        for _raw_pos, _raw_len, clean_pos, clean_len, _changed in self._entries():
            self._text_file.seek(clean_pos)
            yield self._text_file.read(clean_len).decode("utf-8")

    def changes(self) -> Iterator[Change]:
        """Yield a :class:`Change` for every row that ftfy actually modified."""
        for raw_pos, raw_len, _clean_pos, clean_len, changed in self._entries():
            if changed:
                yield Change(raw_pos, raw_len, clean_len)

    def original_at(self, position: int, length: int | None = None) -> str:
        """
        Return the original occurrence stored at `position` in the raw text.

        With `length` (the ``original_length`` from the index), exactly one
        record occurrence is returned. Without it, everything from `position`
        to the end of the raw text is returned.
        """
        self._check_open()
        self._raw_file.seek(position)
        if length is None:
            data = self._raw_file.read()
        else:
            data = self._raw_file.read(length)
        return data.decode("utf-8")

    def rows(self) -> Iterator[Row]:
        """
        Yield every record as a :class:`Row` pairing the original text, the
        cleaned text, and the changed flag from this version's index.
        """
        self._check_open()
        for raw_pos, raw_len, clean_pos, clean_len, changed in self._entries():
            self._raw_file.seek(raw_pos)
            original = self._raw_file.read(raw_len).decode("utf-8")
            self._text_file.seek(clean_pos)
            cleaned = self._text_file.read(clean_len).decode("utf-8")
            yield Row(raw_pos, original, cleaned, changed)

    def close(self) -> None:
        """Release the files backing this version handle."""
        for file_obj in (self._raw_file, self._text_file, self._index_file):
            file_obj.close()
        self.closed = True

    def __enter__(self) -> Version:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


class VersionedTitleStore:
    """
    A directory of atomically versioned, ftfy-cleaned title columns.

    The directory layout is::

        <store>/
            current              # contains the current version number
            lock                 # cross-process writer lock
            versions/v1.raw.txt  # original records, one per line
            versions/v1.txt      # cleaned records, one per line
            versions/v1.idx      # fixed-width per-row change index
            versions/v1.meta.json
            batches/<batch-id>   # marks a batch as already committed
            tmp/                 # staging area and crash journals

    `config` is a :class:`ftfy.TextFixerConfig`; any further keyword arguments
    are the same ftfy configuration keywords accepted by :func:`ftfy.fix_text`.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        config: TextFixerConfig | None = None,
        **kwargs: Any,
    ) -> None:
        self.path = Path(path)
        if config is None:
            config = TextFixerConfig()
        self._config = _config_from_kwargs(config, kwargs)

        self._versions_dir = self.path / "versions"
        self._batches_dir = self.path / "batches"
        self._tmp_dir = self.path / "tmp"
        self._current_path = self.path / "current"
        self._lock_path = self.path / "lock"

        self.path.mkdir(parents=True, exist_ok=True)
        with self._locked():
            for directory in (
                self._versions_dir,
                self._batches_dir,
                self._tmp_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            self._recover()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_file = self._lock_path.open("a+b")
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    @property
    def current_version(self) -> int | None:
        """The published version number, or None if no version exists yet."""
        try:
            return int(self._current_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            return None

    def versions(self) -> list[int]:
        """Return the committed version numbers, oldest first."""
        numbers = [
            int(path.name[1 : -len(_INDEX_SUFFIX)])
            for path in self._versions_dir.glob(f"v*{_INDEX_SUFFIX}")
        ]
        return sorted(numbers)

    def _version_paths(self, number: int) -> dict[str, Path]:
        return {
            "raw": self._versions_dir / f"v{number}{_RAW_SUFFIX}",
            "text": self._versions_dir / f"v{number}{_TEXT_SUFFIX}",
            "index": self._versions_dir / f"v{number}{_INDEX_SUFFIX}",
            "meta": self._versions_dir / f"v{number}{_META_SUFFIX}",
        }

    def _staging_paths(self, number: int) -> dict[str, Path]:
        return {
            "raw": self._tmp_dir / f"v{number}{_RAW_SUFFIX}",
            "text": self._tmp_dir / f"v{number}{_TEXT_SUFFIX}",
            "index": self._tmp_dir / f"v{number}{_INDEX_SUFFIX}",
            "meta": self._tmp_dir / f"v{number}{_META_SUFFIX}",
        }

    def _marker_path(self, batch_id: str) -> Path:
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError(_EMPTY_BATCH_ERROR)
        if any(char not in _SAFE_BATCH_CHARS for char in batch_id):
            raise ValueError(_BATCH_CHARS_ERROR)
        return self._batches_dir / batch_id

    def write_version(self, records: Iterable[str], batch_id: str) -> Version:
        """
        Consume the record stream exactly once, clean every title with ftfy,
        and publish a new immutable version.

        The stream is not rewound or re-read. Each record is stored both in
        its original form and after cleaning, and gets one fixed-width index
        entry recording both byte positions, both lengths, and whether it
        changed.

        If `batch_id` was already committed, the records are not consumed and
        no new version is written: the existing version is returned, so
        retrying a batch never counts a repeated row a second time.
        """
        marker = self._marker_path(batch_id)
        with self._locked():
            if marker.exists():
                committed = json.loads(marker.read_text(encoding="utf-8"))
                return self._open_version_locked(committed["version"])

            current = self.current_version
            number = 1 if current is None else current + 1
            staging = self._staging_paths(number)
            journal_path = self._tmp_dir / f"journal-{batch_id}.json"

            count = self._write_staging(records, staging)
            meta = {
                "version": number,
                "batch_id": batch_id,
                "records": count,
            }
            _write_fsynced_text(staging["meta"], json.dumps(meta))
            sizes = {path.name: path.stat().st_size for path in staging.values()}
            journal = {**meta, "files": sizes}
            _write_fsynced_text(journal_path, json.dumps(journal))
            _fsync_directory(self._tmp_dir)

            targets = self._version_paths(number)
            for name in ("raw", "text", "index", "meta"):
                staging[name].replace(targets[name])
            _fsync_directory(self._versions_dir)

            self._publish_pointer(number)

            _write_fsynced_text(marker, json.dumps({"version": number, "records": count}))
            _fsync_directory(self._batches_dir)

            journal_path.unlink(missing_ok=True)
            _fsync_directory(self._tmp_dir)
            return self._open_version_locked(number)

    def _write_staging(self, records: Iterable[str], staging: dict[str, Path]) -> int:
        count = 0
        raw_position = 0
        clean_position = 0
        newline_bytes = _NEWLINE.encode("utf-8")
        with staging["raw"].open("wb") as raw_file, staging["text"].open(
            "wb"
        ) as text_file, staging["index"].open("wb") as index_file:
            index_file.write(_INDEX_MAGIC)
            index_file.write(b"\x00" * _COUNT_STRUCT.size)
            for record in records:
                if not isinstance(record, str):
                    raise TypeError(_NON_STRING_RECORD_ERROR)
                raw_bytes = record.encode("utf-8")
                cleaned = fix_text(record, self._config)
                clean_bytes = cleaned.encode("utf-8")
                changed = cleaned != record

                index_file.write(
                    _RECORD_STRUCT.pack(
                        raw_position,
                        len(raw_bytes),
                        clean_position,
                        len(clean_bytes),
                        changed,
                    )
                )
                raw_file.write(raw_bytes)
                raw_file.write(newline_bytes)
                text_file.write(clean_bytes)
                text_file.write(newline_bytes)

                raw_position += len(raw_bytes) + len(newline_bytes)
                clean_position += len(clean_bytes) + len(newline_bytes)
                count += 1

            index_file.seek(len(_INDEX_MAGIC))
            index_file.write(_COUNT_STRUCT.pack(count))
            for file_obj in (raw_file, text_file, index_file):
                file_obj.flush()
                os.fsync(file_obj.fileno())
        return count

    def _publish_pointer(self, number: int) -> None:
        pointer_staging = self._tmp_dir / "current.new"
        _write_fsynced_text(pointer_staging, f"{number}\n")
        pointer_staging.replace(self._current_path)
        _fsync_directory(self.path)

    def _recover(self) -> None:
        """
        Finish or discard an interrupted commit so that only whole versions
        are ever visible.
        """
        for journal_path in self._tmp_dir.glob("journal-*.json"):
            journal = self._read_journal(journal_path)
            number = journal.get("version") if journal is not None else None
            if not isinstance(number, int):
                number = None
            targets = self._version_paths(number) if number is not None else {}
            expected_sizes = journal.get("files", {}) if journal is not None else {}
            if (
                journal is not None
                and number is not None
                and isinstance(journal.get("batch_id"), str)
                and self.current_version == number
                and all(
                    path.exists() and path.stat().st_size == expected_sizes.get(path.name)
                    for path in targets.values()
                )
            ):
                # The pointer swap happened; finish the post-commit bookkeeping.
                marker = self._marker_path(journal["batch_id"])
                if not marker.exists():
                    _write_fsynced_text(
                        marker,
                        json.dumps({"version": number, "records": journal["records"]}),
                    )
                    _fsync_directory(self._batches_dir)
            else:
                # The new version was never published; remove every trace of it.
                for path in targets.values():
                    path.unlink(missing_ok=True)
                _fsync_directory(self._versions_dir)

            if number is not None:
                for path in self._tmp_dir.glob(f"v{number}.*"):
                    path.unlink(missing_ok=True)
            journal_path.unlink(missing_ok=True)
            _fsync_directory(self._tmp_dir)

        # Any other staging leftovers come from a process that died before
        # writing its journal; they cannot belong to a published version.
        for path in self._tmp_dir.iterdir():
            if path.is_file():
                path.unlink()

    @staticmethod
    def _read_journal(journal_path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(journal_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def open_version(self, version: int | None = None) -> Version:
        """
        Open a committed version for reading, pinning its files open.

        With no argument, opens the current version. Raises FileNotFoundError
        if the requested version does not exist or was never fully committed.
        """
        with self._locked():
            return self._open_version_locked(version)

    def _open_version_locked(self, version: int | None) -> Version:
        if version is None:
            version = self.current_version
            if version is None:
                raise FileNotFoundError(_NO_VERSION_ERROR)
        paths = self._version_paths(version)
        for path in paths.values():
            if not path.exists():
                message = f"version {version} is not fully committed: {path.name} is missing"
                raise FileNotFoundError(message)
        return Version(self._versions_dir, version)


__all__ = ["Change", "Row", "Version", "VersionedTitleStore"]
