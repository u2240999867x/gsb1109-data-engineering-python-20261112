"""Spawn every probe in an isolated interpreter and capture the evidence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "probe.py"
EVIDENCE_DIR = HERE / "evidence"
MODES = (
    "fallback-drift",
    "dumb-baked",
    "icu-snapshot",
    "merge-fallback",
    "merge-icu",
)


def main() -> int:
    EVIDENCE_DIR.mkdir(exist_ok=True)
    failures = 0
    for mode in MODES:
        proc = subprocess.run(
            [sys.executable, str(PROBE), mode],
            capture_output=True,
            text=True,
            check=False,
        )
        output = proc.stdout
        if proc.stderr:
            output += "\n[stderr]\n" + proc.stderr
        print(output)
        (EVIDENCE_DIR / f"{mode}.log").write_text(output)
        if proc.returncode != 0:
            failures += 1
            print(f"!! probe {mode} exited {proc.returncode}")
    print(f"evidence written to {EVIDENCE_DIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
