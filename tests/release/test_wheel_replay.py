"""
End-to-end release replay against a real built wheel.

These tests are slow and need wheel/venv tooling, so they are gated behind the
``release_wheel`` marker::

    pytest -m release_wheel

They reproduce the two-partition deployment described in the release notes:

1. build the wheel with the project's real build backend;
2. install it into a brand-new virtual environment backed by a different
   interpreter (the editable source tree is never on ``sys.path`` there);
3. make the installed package directory read-only and replay every rule,
   including under ``-O`` and ``-OO``, asserting the wheel matches the source
   partition and never writes parser tables at runtime.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys

import pytest

pytestmark = pytest.mark.release_wheel

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RELEASE_DIR = REPO_ROOT / "tests" / "release"
SOURCE_RULES = RELEASE_DIR / "rules.json"


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, capture_output=True, check=False, text=True, **kwargs
    )


@pytest.fixture(scope="module")
def uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        pytest.skip("the `uv` CLI is required to build and seed the fresh venv")
    return executable


def _fresh_interpreter() -> str:
    """Pick an interpreter that differs from the one running the test suite."""

    requested = os.environ.get("RELEASE_REPLAY_PYTHON")
    candidates = [requested] if requested else []
    candidates += [f"python3.{minor}" for minor in range(15, 10, -1)]

    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate)
        if path and os.path.realpath(path) != os.path.realpath(sys.executable):
            return path
    return sys.executable


def _has_build_frontend(interpreter: str) -> bool:
    probe = subprocess.run(
        [
            interpreter,
            "-c",
            "import pip, setuptools, wheel",
        ],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def _builder_interpreter() -> str:
    """Find an interpreter able to build a wheel offline.

    The test-suite interpreter may be a pip-less virtual environment, so the
    builder is discovered independently and must expose pip, setuptools and
    wheel.
    """

    requested = os.environ.get("RELEASE_REPLAY_BUILD_PYTHON")
    candidates = [requested] if requested else []
    candidates += [f"python3.{minor}" for minor in range(15, 10, -1)]
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate)
        if path and _has_build_frontend(path):
            return path
    pytest.skip(
        "no interpreter with pip, setuptools and wheel is available "
        "to build the wheel"
    )


@pytest.fixture(scope="module")
def wheel_environment(uv, tmp_path_factory):
    work = tmp_path_factory.mktemp("wheel-replay")
    wheel_dir = work / "dist"
    venv_dir = work / "venv"

    builder = _builder_interpreter()
    build = _run(
        [
            builder,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(wheel_dir),
            str(REPO_ROOT),
        ]
    )
    assert build.returncode == 0, build.stderr

    wheels = list(wheel_dir.glob("jsonpath_ng-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]

    interpreter = _fresh_interpreter()
    create = _run([uv, "venv", "--python", interpreter, str(venv_dir)])
    assert create.returncode == 0, create.stderr
    venv_python = venv_dir / "bin" / "python"
    assert venv_python.exists()

    install = _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--offline",
            str(wheel),
        ]
    )
    if install.returncode != 0:
        install = _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(venv_python),
                str(wheel),
            ]
        )
    assert install.returncode == 0, install.stderr

    return work, wheel, venv_dir, venv_python


def _replay(venv_python: pathlib.Path, flags: list[str], fault: str | None = None):
    command = [
        str(venv_python),
        *flags,
        str(RELEASE_DIR / "replay_subprocess.py"),
        "--catalog",
        str(SOURCE_RULES),
        "--partition",
        "wheel",
    ]
    if fault:
        command += ["--fault", fault]
    env = dict(os.environ)
    # The verifier lives in the repository; the package under test must still
    # resolve to the wheel inside the fresh venv, not the editable source tree.
    existing_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(RELEASE_DIR), existing_path) if part
    )
    run = subprocess.run(command, capture_output=True, check=False, text=True, env=env)
    return run


def _site_packages(venv_dir: pathlib.Path) -> pathlib.Path:
    lib = venv_dir / "lib"
    return next(lib.glob("python*/site-packages/jsonpath_ng"))


def _tree_snapshot(package_dir: pathlib.Path):
    return sorted(
        (
            path.relative_to(package_dir),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in package_dir.rglob("*")
    )


def _set_tree_writable(path: pathlib.Path, writable: bool) -> None:
    # Include the root directory itself: a read-only directory is what blocks
    # creation of new files such as regenerated PLY tables.
    entries = [path, *path.rglob("*")]
    for entry in entries:
        mode = entry.stat().st_mode
        write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        entry.chmod(mode | write_bits if writable else mode & ~write_bits)


def test_wheel_is_loaded_from_the_fresh_venv(wheel_environment):
    _, _, venv_dir, venv_python = wheel_environment
    run = _replay(venv_python, [])
    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    package_file = pathlib.Path(payload["package_file"])
    assert package_file.is_relative_to(_site_packages(venv_dir))
    assert "src" not in package_file.parts


@pytest.mark.parametrize("flags", ([], ["-O"], ["-OO"]), ids=("default", "O", "OO"))
def test_wheel_partition_replays_all_rules_read_only(wheel_environment, flags):
    _, _, venv_dir, venv_python = wheel_environment
    package_dir = _site_packages(venv_dir)
    snapshot = _tree_snapshot(package_dir)

    _set_tree_writable(package_dir, writable=False)
    try:
        run = _replay(venv_python, flags)
    finally:
        _set_tree_writable(package_dir, writable=True)

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["catalog_failures"] == []
    assert payload["loader_errors"] == []
    assert payload["optimize"] == (2 if flags == ["-OO"] else len(flags))

    assert _tree_snapshot(package_dir) == snapshot, (
        "the read-only package directory was modified at runtime"
    )


def test_wheel_partition_reports_loader_fault_in_isolation(wheel_environment):
    _, _, _, venv_python = wheel_environment
    run = _replay(
        venv_python, [], fault="jsonpath_ng._ply_tables.ext_parser_table"
    )
    assert run.returncode == 2, run.stderr
    payload = json.loads(run.stdout)
    assert "startup_loader_error" in payload
    assert payload["startup_loader_error"]["error_type"] == "RuntimeError"
    assert "report" not in payload


def test_wheel_matches_source_partition(wheel_environment, monkeypatch):
    _, _, _, venv_python = wheel_environment
    run = _replay(venv_python, [])
    assert run.returncode == 0, run.stderr
    wheel_payload = json.loads(run.stdout)

    monkeypatch.syspath_prepend(str(RELEASE_DIR))
    import verifier

    catalog = verifier.load_catalog(SOURCE_RULES)
    source_report = verifier.replay_catalog(catalog)

    for rule_id, source_rule in source_report.items():
        wheel_rule = wheel_payload["report"][rule_id]
        for mode in verifier.MODES:
            assert wheel_rule[mode]["status"] == source_rule[mode]["status"]
            if source_rule[mode]["status"] == verifier.OK:
                assert (
                    wheel_rule[mode]["updated_columns"]
                    == source_rule[mode]["updated_columns"]
                )
                assert (
                    wheel_rule[mode]["wrong_column"]
                    == source_rule[mode]["wrong_column"]
                )
