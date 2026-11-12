"""Tests for scripts/utils.py shared utilities."""

from __future__ import annotations

from pathlib import Path

from utils import collect_test_files, normalize_language


def test_normalize_language_iso_code():
    """ISO 639-1 code should be returned lowered."""
    assert normalize_language("fr") == "fr"
    assert normalize_language("ja") == "ja"
    assert normalize_language("zh") == "zh"


def test_normalize_language_case_insensitive():
    """Uppercase or mixed-case codes should be lowered."""
    assert normalize_language("FR") == "fr"
    assert normalize_language("Ja") == "ja"


def test_normalize_language_unknown_code():
    """Unknown codes should be returned lowered as-is."""
    assert normalize_language("xx") == "xx"


def test_normalize_language_none():
    """None input should return None."""
    assert normalize_language(None) is None


def test_collect_test_files_structure(tmp_path: Path):
    """collect_test_files should parse encoding-language directory names."""
    enc_dir = tmp_path / "utf-8-en"
    enc_dir.mkdir()
    (enc_dir / "sample.txt").write_bytes(b"Hello")
    (enc_dir / "sample2.txt").write_bytes(b"World")

    results = collect_test_files(tmp_path)
    assert len(results) == 2
    assert results[0][0] == "utf-8"
    assert results[0][1] == "en"
    assert results[0][2].name == "sample.txt"


def test_collect_test_files_none_encoding(tmp_path: Path):
    """'None-None' directory should produce Python None values."""
    enc_dir = tmp_path / "None-None"
    enc_dir.mkdir()
    (enc_dir / "binary.bin").write_bytes(b"\x00\x01")

    results = collect_test_files(tmp_path)
    assert len(results) == 1
    assert results[0][0] is None
    assert results[0][1] is None


def test_collect_test_files_skips_non_dirs(tmp_path: Path):
    """Files at the top level should be skipped."""
    (tmp_path / "readme.txt").write_text("ignore me")
    enc_dir = tmp_path / "utf-8-en"
    enc_dir.mkdir()
    (enc_dir / "sample.txt").write_bytes(b"Hello")

    results = collect_test_files(tmp_path)
    assert len(results) == 1


def test_collect_test_files_skips_bad_names(tmp_path: Path):
    """Directories without a hyphen should be skipped."""
    bad_dir = tmp_path / "nohyphen"
    bad_dir.mkdir()
    (bad_dir / "file.txt").write_bytes(b"data")

    results = collect_test_files(tmp_path)
    assert len(results) == 0


def test_collect_test_files_hyphenated_encoding(tmp_path: Path):
    """Encodings with hyphens (e.g., hz-gb-2312) should split on last hyphen."""
    enc_dir = tmp_path / "hz-gb-2312-zh"
    enc_dir.mkdir()
    (enc_dir / "sample.txt").write_bytes(b"Hello")

    results = collect_test_files(tmp_path)
    assert len(results) == 1
    assert results[0][0] == "hz-gb-2312"
    assert results[0][1] == "zh"
