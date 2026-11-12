"""
Subprocess entry point for the release replay.

This module imports nothing from pytest so it can run inside a fresh
interpreter that only has the built wheel installed. Usage::

    python replay_subprocess.py --catalog rules.json --partition wheel
    python replay_subprocess.py --catalog rules.json \\
        --fault jsonpath_ng._ply_tables.parser_table

Output is a single JSON document on stdout. The exit status is:

* ``0`` - replay finished and matched the frozen catalog;
* ``1`` - semantic or catalog mismatches were found;
* ``2`` - the package could not be loaded at all (an isolated loader fault);
* ``3`` - the harness itself failed unexpectedly.
"""

from __future__ import annotations

import argparse
import importlib.abc
import json
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


class _TableImportBlocker(importlib.abc.MetaPathFinder):
    """Make a PLY table module unimportable to simulate a broken install."""

    def __init__(self, blocked_module: str) -> None:
        self.blocked_module = blocked_module

    def find_spec(self, fullname, path, target=None):
        if fullname == self.blocked_module:
            raise RuntimeError(f"simulated loader fault in {fullname}")
        return None


def _emit(payload: dict, exit_code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--partition", required=True, choices=("source", "wheel"))
    parser.add_argument("--fault")
    args = parser.parse_args()

    if args.fault:
        sys.meta_path.insert(0, _TableImportBlocker(args.fault))

    try:
        import jsonpath_ng

        import verifier
    except Exception as exc:
        _emit(
            {
                "partition": args.partition,
                "optimize": sys.flags.optimize,
                "startup_loader_error": {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            },
            2,
        )

    catalog = verifier.load_catalog(args.catalog)
    report = verifier.replay_catalog(catalog)
    failures = verifier.verify_against_catalog(catalog, report)
    divergences = verifier.find_semantic_divergences(catalog, report)

    loader_errors = [
        f"{rule_id}/{mode}"
        for rule_id, rule_report in report.items()
        for mode in verifier.MODES
        if rule_report[mode]["status"] == verifier.LOADER_ERROR
    ]

    payload = {
        "partition": args.partition,
        "optimize": sys.flags.optimize,
        "python": sys.version.split()[0],
        "package_file": jsonpath_ng.__file__,
        "loader_errors": loader_errors,
        "catalog_failures": failures,
        "divergences": divergences,
        "report": report,
    }
    _emit(payload, 1 if (failures or loader_errors) else 0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - harness boundary
        _emit(
            {
                "harness_error": {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            },
            3,
        )
