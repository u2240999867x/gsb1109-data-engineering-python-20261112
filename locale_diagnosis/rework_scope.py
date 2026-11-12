"""Decide whether a split-duplicate partition needs a reorder or full rebuild.

Two partitions can show the identical symptom (a duplicate customer name in
two segments of the merged output) while requiring very different fixes.
The decision is driven entirely by information already stored with each
shard: its build manifest and a key fingerprint over a fixed probe corpus.
"""

from __future__ import annotations

import hashlib
import locale
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_icu  # noqa: E402

sys.modules["icu"] = fake_icu

from natsort import natsort_keygen  # noqa: E402
from natsort.ns_enum import ns  # noqa: E402
import natsort  # noqa: E402
import natsort.compat.locale as compat_locale  # noqa: E402

PROBE_CORPUS = ("Anna", "Борис", "Andrei", "Мария", "anton", "1.234")

# Fields that define whether two shards hold keys from the same key function.
KEY_DEFINING_FIELDS = (
    "locale",
    "backend",
    "engine_version",
    "natsort_version",
    "alg",
    "dumb_at_build",
    "key_fingerprint",
)


@dataclass(frozen=True)
class ShardManifest:
    shard: str
    locale: str
    backend: str
    engine_version: str
    natsort_version: str
    alg: int
    dumb_at_build: bool
    key_fingerprint: str
    physically_sorted: bool


def _fingerprint(key_func) -> str:
    material = repr([key_func(value) for value in PROBE_CORPUS])
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def build_manifest(
    shard: str,
    locale_name: str,
    rules: tuple[str, str],
    engine_version: str,
    physically_sorted: bool,
) -> ShardManifest:
    """Build a shard manifest exactly as a partition writer would."""
    locale.setlocale(locale.LC_ALL, locale_name)
    fake_icu.set_rules(script_order=rules)
    key_func = natsort_keygen(alg=ns.LOCALE)
    return ShardManifest(
        shard=shard,
        locale=locale_name,
        backend="icu",
        engine_version=engine_version,
        natsort_version=natsort.__version__,
        alg=int(ns.LOCALE),
        dumb_at_build=compat_locale.dumb_sort(),
        key_fingerprint=_fingerprint(key_func),
        physically_sorted=physically_sorted,
    )


def decide(manifests: list[ShardManifest]) -> tuple[str, list[str]]:
    """Return (verdict, evidence_lines)."""
    evidence: list[str] = []
    reference = manifests[0]
    divergent: dict[str, tuple[object, object]] = {}
    for candidate in manifests[1:]:
        for field_name in KEY_DEFINING_FIELDS:
            old = getattr(reference, field_name)
            new = getattr(candidate, field_name)
            if old != new:
                divergent.setdefault(field_name, (old, new))

    if divergent:
        for field_name, (old, new) in divergent.items():
            evidence.append(f"field '{field_name}' differs: {old!r} != {new!r}")
        return "REGENERATE-ALL-KEYS", evidence

    evidence.append("all key-defining manifest fields and fingerprints match")
    unsorted = [m.shard for m in manifests if not m.physically_sorted]
    if unsorted:
        evidence.append(f"stored keys are valid; physical order broken in {unsorted}")
        return f"REORDER-ONLY: {', '.join(unsorted)}", evidence
    evidence.append("stored keys and ordering are valid everywhere")
    return "RE-MERGE ONLY (no shard rewrite)", evidence


def scenario_reorder_only() -> list[ShardManifest]:
    """Same key rules everywhere; one shard was written out of order."""
    common = dict(
        locale_name="en_US.UTF-8",
        rules=("latin", "cyrillic"),
        engine_version=fake_icu.ICU_VERSION,
    )
    return [
        build_manifest("P1/shard-A", physically_sorted=True, **common),
        build_manifest("P1/shard-B", physically_sorted=False, **common),
    ]


def scenario_regenerate_all() -> list[ShardManifest]:
    """Same symptom, but shard-B was built after an ICU/CLDR upgrade."""
    return [
        build_manifest(
            "P2/shard-A",
            locale_name="en_US.UTF-8",
            rules=("latin", "cyrillic"),
            engine_version="fake-1.0",
            physically_sorted=True,
        ),
        build_manifest(
            "P2/shard-B",
            locale_name="en_US.UTF-8",
            rules=("cyrillic", "latin"),
            engine_version="fake-2.0",
            physically_sorted=True,
        ),
    ]


def main() -> int:
    header = (
        f"platform={platform.system()} {platform.mac_ver()[0] or platform.release()} "
        f"python={sys.version.split()[0]}"
    )
    for title, builder in (
        ("partition P1: identical build rules, one shard mis-ordered",
         scenario_reorder_only),
        ("partition P2: ICU/CLDR changed between shard builds",
         scenario_regenerate_all),
    ):
        print(f"===== {title} =====")
        print(header)
        manifests = builder()
        for manifest in manifests:
            print(manifest)
        verdict, evidence = decide(manifests)
        print(f"verdict: {verdict}")
        for line in evidence:
            print(f"  - {line}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
