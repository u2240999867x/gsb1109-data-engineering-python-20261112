"""Isolated, single-purpose probes.

Each invocation runs in its own interpreter process so import-time backend
selection (ICU vs system fallback) and global locale state cannot leak
between experiments.  Run via ``run_probes.py``, which spawns the processes.
"""

from __future__ import annotations

import locale
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _short(key: object) -> str:
    """Stable, readable rendering of a natsort key."""
    return repr(key) if isinstance(key, tuple) else repr((key,))


def _load_natsort(use_fake_icu: bool):
    if use_fake_icu:
        import fake_icu

        sys.modules["icu"] = fake_icu
    import natsort.compat.locale as compat_locale
    from natsort import natsort_keygen
    from natsort.ns_enum import ns

    backend = "icu" if "icu" in sys.modules else "system-locale-fallback"
    return compat_locale, natsort_keygen, ns, backend


def probe_fallback_drift() -> None:
    """Fallback: the captured strxfrm follows the global locale per call."""
    compat_locale, keygen, ns, backend = _load_natsort(False)
    locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
    key = keygen(alg=ns.LOCALE)
    en_key = key("Anna")
    locale.setlocale(locale.LC_ALL, "ru_RU.UTF-8")
    stale_call = key("Anna")
    fresh_call = keygen(alg=ns.LOCALE)("Anna")
    print(f"backend={backend}")
    print(f"get_strxfrm() is locale.strxfrm: "
          f"{compat_locale.get_strxfrm() is locale.strxfrm}")
    print(f"key under en_US : {_short(en_key)}")
    print(f"same func under ru_RU: {_short(stale_call)}")
    print(f"fresh func under ru_RU: {_short(fresh_call)}")
    print(f"stale output changed after setlocale: {en_key != stale_call}")
    print(f"stale call == fresh call (rule read at compare time): "
          f"{stale_call == fresh_call}")


def probe_dumb_baked() -> None:
    """Fallback: NS_DUMB is decided at keygen time and never re-evaluated."""
    _compat, keygen, ns, backend = _load_natsort(False)
    locale.setlocale(locale.LC_ALL, "C")
    dumb_at_creation = locale.strxfrm("A") < locale.strxfrm("a")
    c_key = keygen(alg=ns.LOCALE)
    locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")
    de_key = keygen(alg=ns.LOCALE)
    print(f"backend={backend}")
    print(f"dumb_sort() under C at keygen: {dumb_at_creation}")
    print(f"C-baked key('Apple')  : {_short(c_key('Apple'))}")
    print(f"de-baked key('Apple') : {_short(de_key('Apple'))}")
    print("C-baked keeps groupletters structure after switching to de_DE: "
          f"{c_key('Apple') != de_key('Apple')}")


def probe_icu_snapshot() -> None:
    """ICU: the Collator snapshots rules; stale keys are frozen at creation."""
    import fake_icu

    compat_locale, keygen, ns, backend = _load_natsort(True)
    locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
    key = keygen(alg=ns.LOCALE)
    before = key("Anna")
    # Emulate a locale/rule change AFTER the key function already exists.
    fake_icu.set_rules(script_order=("cyrillic", "latin"))
    stale_call = key("Anna")
    fresh_call = keygen(alg=ns.LOCALE)("Anna")
    print(f"backend={backend} (fake_icu={fake_icu.ICU_VERSION})")
    print(f"key before rule change : {_short(before)}")
    print(f"same func after change : {_short(stale_call)}")
    print(f"fresh func after change: {_short(fresh_call)}")
    print(f"stale output frozen at creation: {before == stale_call}")
    print(f"fresh output follows new rules: {fresh_call != before}")
    print("collator creation audit (id, locale, script_order):")
    for ident, loc_name, rules in fake_icu.create_log():
        print(f"  #{ident} loc={loc_name} script_order={rules['script_order']}")
    print(f"dumb_sort() on ICU backend: {compat_locale.dumb_sort()}")


def probe_merge(use_fake_icu: bool) -> None:
    """End-to-end: locally sorted shards, split duplicate after merge."""
    import heapq

    _compat, keygen, ns, backend = _load_natsort(use_fake_icu)
    if use_fake_icu:
        import fake_icu

    shard_names = {
        "shard-A": (["Andrei", "Борис", "Мария", "anton"], "en_US.UTF-8"),
        "shard-B": (["Andrei", "Сергей", "Anna", "Борис"], "ru_RU.UTF-8"),
    }
    shards = {}
    for name, (values, loc_name) in shard_names.items():
        if use_fake_icu:
            order = (("latin", "cyrillic") if loc_name.startswith("en")
                     else ("cyrillic", "latin"))
            fake_icu.set_rules(script_order=order)
        else:
            locale.setlocale(locale.LC_ALL, loc_name)
        key = keygen(alg=ns.LOCALE)
        shards[name] = (sorted(values, key=key), key)
        print(f"{name} built under {loc_name}: {shards[name][0]}")

    # Aggregation runs under the aggregator's current rule set.
    if use_fake_icu:
        fake_icu.set_rules(script_order=("latin", "cyrillic"))
    else:
        locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
    merge_key = keygen(alg=ns.LOCALE)
    merged = list(heapq.merge(
        shards["shard-A"][0], shards["shard-B"][0], key=merge_key
    ))
    positions: dict[str, list[int]] = {}
    for index, value in enumerate(merged):
        positions.setdefault(value, []).append(index)
    split = {
        value: idxs for value, idxs in positions.items()
        if len(idxs) > 1 and idxs != list(range(idxs[0], idxs[-1] + 1))
    }
    print(f"backend={backend}")
    print(f"merged: {merged}")
    print(f"split duplicates: {split}")
    for name, (run, _) in shards.items():
        keys = [merge_key(v) for v in run]
        inversions = sum(
            1 for i in range(len(keys) - 1) if keys[i] > keys[i + 1]
        )
        print(f"{name} inversions under merge rule: {inversions}")


MODES = {
    "fallback-drift": probe_fallback_drift,
    "dumb-baked": probe_dumb_baked,
    "icu-snapshot": probe_icu_snapshot,
    "merge-fallback": lambda: probe_merge(False),
    "merge-icu": lambda: probe_merge(True),
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} {{{','.join(MODES)}}}", file=sys.stderr)
        return 2
    print(f"===== probe: {sys.argv[1]} (pid={__import__('os').getpid()}) =====")
    MODES[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
