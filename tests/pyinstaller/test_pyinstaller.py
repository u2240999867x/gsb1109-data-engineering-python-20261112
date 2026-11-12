"""
Verify that jsonpath-ng works when frozen by PyInstaller.

PLY loads the pre-generated parser and lexer tables by module name,
which PyInstaller can't discover by following ``import`` statements.
The tables must therefore be imported explicitly by jsonpath-ng itself.

These tests build a real executable, which is slow,
so they only run when selected with ``pytest -m pyinstaller``.
"""

import json
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.pyinstaller

frozen_app = pathlib.Path(__file__).parent / "frozen_app.py"


def test_frozen_app_can_parse(tmp_path):
    """A one-file executable can use the base and extended parsers."""

    # Run PyInstaller with `python -O`.
    build = subprocess.run(
        [
            sys.executable,
            "-O",
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--console",
            f"--distpath={tmp_path / 'dist'}",
            f"--workpath={tmp_path / 'build'}",
            f"--specpath={tmp_path}",
            "--name=frozen_app",
            str(frozen_app),
        ],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, build.stderr

    executables = list((tmp_path / "dist").glob("frozen_app*"))
    assert len(executables) == 1

    run = subprocess.run(
        [str(executables[0])],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        text=True,
        timeout=60,
    )

    # PLY reports failures on stderr and then silently builds the tables in memory.
    assert run.stderr == ""
    assert run.returncode == 0
    assert json.loads(run.stdout) == {"base": [1, -1, 2], "ext": [1, 2]}
