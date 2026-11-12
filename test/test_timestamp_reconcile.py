"""Cross-representation ingestion reconciliation for historical events.

Historical events are partitioned by the exact (seconds, nanoseconds) pair.
The same instant has several legal wire representations (timestamp32/64/96,
plus ext8-wrapped variants), and the pure-Python unpacker can decode each
representation into a Timestamp (timestamp=0), a float (timestamp=1), an
int nanosecond count (timestamp=2), or a datetime (timestamp=3).

These tests pin down, per decode mode, what partitioning, sorting, and
rewriting can guarantee -- using independent (seconds, nanoseconds) integer
pairs as ground truth and hardcoded expected bytes, so nothing depends on
trusting the round-trip itself.
"""

import datetime

import pytest

import msgpack
from msgpack.ext import Timestamp

UTC = datetime.timezone.utc


def wire(hexstr):
    return bytes.fromhex(hexstr)


# --- Ground truth: independent (seconds, nanoseconds) integer pairs ---------
# Each entry maps one instant to every legal wire representation of it.
# Bytes are literal expectations, not derived from Timestamp.to_bytes().

SAME_INSTANT_WIRE_FORMS = [
    # (seconds, nanoseconds, [legal wire forms], canonical rewrite)
    (
        0,
        0,
        [
            "d6ff00000000",  # fixext4 / timestamp32
            "d7ff0000000000000000",  # fixext8 / timestamp64
            "c70cff000000000000000000000000",  # ext8 / timestamp96
            "c704ff00000000",  # ext8 wrapping a 4-byte payload
            "c708ff0000000000000000",  # ext8 wrapping an 8-byte payload
        ],
        "d6ff00000000",
    ),
    (
        42,
        0,
        [
            "d6ff0000002a",
            "d7ff000000000000002a",
            "c70cff00000000000000000000002a",
            "c704ff0000002a",
        ],
        "d6ff0000002a",
    ),
    (
        42,
        14000,
        [
            "d7ff0000dac00000002a",  # 14000 << 34 | 42
            "c70cff000036b0000000000000002a",
        ],
        "d7ff0000dac00000002a",
    ),
    (
        2**32 - 1,
        0,
        [
            "d6ffffffffff",
            "d7ff00000000ffffffff",
            "c70cff0000000000000000ffffffff",
        ],
        "d6ffffffffff",
    ),
    (
        2**34 - 1,
        999999999,
        [
            "d7ffee6b27ffffffffff",
            "c70cff3b9ac9ff00000003ffffffff",
        ],
        "d7ffee6b27ffffffffff",
    ),
    (
        -3,
        700000000,
        [
            "c70cff29b92700fffffffffffffffd",
        ],
        "c70cff29b92700fffffffffffffffd",
    ),
    (
        -16777216,
        0,
        [
            "c70cff00000000ffffffffff000000",
        ],
        "c70cff00000000ffffffffff000000",
    ),
]


def partition_key(seconds, nanoseconds):
    """The warehouse partition key: exact whole-second bucket and ns offset."""
    return (seconds, nanoseconds)


# --- Cross-representation reconciliation ------------------------------------


def test_all_legal_wire_forms_of_one_instant_agree_in_every_mode():
    for seconds, nanoseconds, forms, _canonical in SAME_INSTANT_WIRE_FORMS:
        expected_nano = seconds * 10**9 + nanoseconds
        for form in forms:
            data = wire(form)
            # timestamp=0: exact identity
            ts = msgpack.unpackb(data, timestamp=0)
            assert isinstance(ts, Timestamp)
            assert partition_key(ts.seconds, ts.nanoseconds) == partition_key(
                seconds, nanoseconds
            )
            # timestamp=2: exact total nanoseconds
            assert msgpack.unpackb(data, timestamp=2) == expected_nano
            # timestamp=1 and 3: all forms of one instant agree with each
            # other (whatever precision they keep)
            assert msgpack.unpackb(data, timestamp=1) == msgpack.unpackb(
                wire(forms[0]), timestamp=1
            )
            assert msgpack.unpackb(data, timestamp=3) == msgpack.unpackb(
                wire(forms[0]), timestamp=3
            )


def test_timestamp_mode_partition_sort_rewrite_are_exact():
    # Decode every ground-truth instant in every legal form; the (seconds,
    # nanoseconds) partition key must be recovered exactly, sorting by it is
    # chronological, and repacking canonicalizes to the minimal encoding.
    instants = []
    for seconds, nanoseconds, forms, canonical in SAME_INSTANT_WIRE_FORMS:
        for form in forms:
            ts = msgpack.unpackb(wire(form), timestamp=0)
            assert partition_key(ts.seconds, ts.nanoseconds) == (seconds, nanoseconds)
            assert msgpack.packb(ts) == wire(canonical)
            instants.append((seconds * 10**9 + nanoseconds, ts))
    by_key = sorted(instants, key=lambda item: (item[1].seconds, item[1].nanoseconds))
    assert [key for key, _ in by_key] == sorted(key for key, _ in instants)


def test_unix_nano_mode_is_exact_and_invertible():
    # timestamp=2 loses nothing: distinct instants decode to distinct ints,
    # ordering is chronological, and from_unix_nano rewrites canonical bytes.
    seen = {}
    for seconds, nanoseconds, forms, canonical in SAME_INSTANT_WIRE_FORMS:
        for form in forms:
            nano = msgpack.unpackb(wire(form), timestamp=2)
            assert nano == seconds * 10**9 + nanoseconds
            seen.setdefault(nano, (seconds, nanoseconds))
            assert msgpack.packb(Timestamp.from_unix_nano(nano)) == wire(canonical)
    # injective: one nano value per instant
    assert len(seen) == len({(s, n) for s, n, _, _ in SAME_INSTANT_WIRE_FORMS})


# --- Shortest merge witnesses in low-precision modes -------------------------
#
# Float mode (timestamp=1) computes seconds + nanoseconds / 1e9.  Below
# 2**24 seconds the float spacing (ulp) is at most 2**-29 s, whose half is
# ~9.3e-10 s < 1 ns, so two instants straddling a whole-second boundary can
# never round to the same float.  At 2**24 the half-ulp grows to ~1.9e-9 s
# and the closest straddling pair -- 1 ns apart -- merges.  These are the
# shortest witnesses (smallest |seconds|) of a merge that crosses a
# whole-second partition boundary.

WITNESS_POS_EARLY = "d7ffee6b27fc01000000"  # ( 16777216, 999999999)
WITNESS_POS_LATE = "d6ff01000001"  # ( 16777217, 0)
WITNESS_NEG_EARLY = "c70cff3b9ac9fffffffffffeffffff"  # (-16777217, 999999999)
WITNESS_NEG_LATE = "c70cff00000000ffffffffff000000"  # (-16777216, 0)


def test_float_mode_shortest_merge_witness_crossing_partition_boundary():
    early = msgpack.unpackb(wire(WITNESS_POS_EARLY), timestamp=0)
    late = msgpack.unpackb(wire(WITNESS_POS_LATE), timestamp=0)
    # Distinct instants in adjacent whole-second partitions...
    assert partition_key(early.seconds, early.nanoseconds) == (16777216, 999999999)
    assert partition_key(late.seconds, late.nanoseconds) == (16777217, 0)
    # ...merge into a single float, so float partitioning and sorting cannot
    # separate them...
    assert msgpack.unpackb(wire(WITNESS_POS_EARLY), timestamp=1) == 16777217.0
    assert msgpack.unpackb(wire(WITNESS_POS_LATE), timestamp=1) == 16777217.0
    # ...and rewriting from the float lands on the wrong side of the
    # boundary: the earlier instant is rewritten to the later one's
    # canonical bytes.
    rewritten = Timestamp.from_unix(msgpack.unpackb(wire(WITNESS_POS_EARLY), timestamp=1))
    assert partition_key(rewritten.seconds, rewritten.nanoseconds) == (16777217, 0)
    assert msgpack.packb(rewritten) == wire(WITNESS_POS_LATE)

    # One second closer to the epoch the same pair stays separable: the
    # witness above is the shortest.
    assert (
        msgpack.unpackb(wire("d7ffee6b27fc00ffffff"), timestamp=1)  # (16777215, 999999999)
        != msgpack.unpackb(wire("d6ff01000000"), timestamp=1)  # (16777216, 0)
    )


def test_float_mode_shortest_merge_witness_negative_seconds():
    early = msgpack.unpackb(wire(WITNESS_NEG_EARLY), timestamp=0)
    late = msgpack.unpackb(wire(WITNESS_NEG_LATE), timestamp=0)
    # (-16777217, 999999999) is -16777216.000000001: one ns before the
    # whole-second boundary at -16777216, in an earlier partition.
    assert partition_key(early.seconds, early.nanoseconds) == (-16777217, 999999999)
    assert partition_key(late.seconds, late.nanoseconds) == (-16777216, 0)
    # Both decode to the same float: the pre-boundary instant is pulled
    # across the boundary into the later partition.
    assert msgpack.unpackb(wire(WITNESS_NEG_EARLY), timestamp=1) == -16777216.0
    assert msgpack.unpackb(wire(WITNESS_NEG_LATE), timestamp=1) == -16777216.0
    rewritten = Timestamp.from_unix(msgpack.unpackb(wire(WITNESS_NEG_EARLY), timestamp=1))
    assert partition_key(rewritten.seconds, rewritten.nanoseconds) == (-16777216, 0)
    assert msgpack.packb(rewritten) == wire(WITNESS_NEG_LATE)

    # One second closer to the epoch the pair stays separable.
    assert (
        msgpack.unpackb(wire("c70cff3b9ac9ffffffffffff000000"), timestamp=1)  # (-16777216, 999999999)
        != msgpack.unpackb(wire("c70cff00000000ffffffffff000001"), timestamp=1)  # (-16777215, 0)
    )


def test_float_and_datetime_modes_never_invert_order():
    # Low-precision modes merge but never transpose: decoding is monotonic.
    ordered = [
        "c70cff3b9ac9fffffffffffeffffff",  # (-16777217, 999999999)
        "c70cff00000000ffffffffff000000",  # (-16777216, 0)
        "d6ff00000000",  # (0, 0)
        "d7ff0000dac00000002a",  # (42, 14000)
        "d7ffee6b27fc01000000",  # (16777216, 999999999)
        "d6ff01000001",  # (16777217, 0)
    ]
    for mode in (1, 3):
        decoded = [msgpack.unpackb(wire(form), timestamp=mode) for form in ordered]
        assert decoded == sorted(decoded)


# --- datetime mode: microsecond truncation -----------------------------------


def test_datetime_mode_merges_within_one_microsecond_only():
    # Shortest datetime witness: (0, 0) and (0, 999) share a whole-second
    # partition but occupy distinct nanosecond slots; both truncate to the
    # epoch and rewrite to the same canonical bytes.
    assert msgpack.unpackb(wire("d6ff00000000"), timestamp=3) == datetime.datetime(
        1970, 1, 1, tzinfo=UTC
    )
    assert msgpack.unpackb(
        wire("d7ff00000f9c00000000"), timestamp=3  # (0, 999)
    ) == datetime.datetime(1970, 1, 1, tzinfo=UTC)
    for form in ("d6ff00000000", "d7ff00000f9c00000000"):
        dt = msgpack.unpackb(wire(form), timestamp=3)
        assert msgpack.packb(dt, datetime=True) == wire("d6ff00000000")

    # Negative seconds: (-1, 999999000) and (-1, 999999999) both truncate to
    # 1969-12-31 23:59:59.999999 and rewrite to the floor microsecond.
    floor_form = "c70cff3b9ac618ffffffffffffffff"  # (-1, 999999000)
    ceil_form = "c70cff3b9ac9ffffffffffffffffff"  # (-1, 999999999)
    expected_dt = datetime.datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    assert msgpack.unpackb(wire(floor_form), timestamp=3) == expected_dt
    assert msgpack.unpackb(wire(ceil_form), timestamp=3) == expected_dt
    assert msgpack.packb(expected_dt, datetime=True) == wire(floor_form)


def test_datetime_mode_merge_never_crosses_whole_second_boundary():
    # Seconds survive datetime decoding exactly; only sub-microsecond
    # nanoseconds are truncated.  So any two instants that merge in datetime
    # mode must share the same whole-second partition.
    nano_a = msgpack.unpackb(wire("c70cff3b9ac9fffffffffffeffffff"), timestamp=2)
    nano_b = msgpack.unpackb(wire("c70cff00000000ffffffffff000000"), timestamp=2)
    dt_a = msgpack.unpackb(wire("c70cff3b9ac9fffffffffffeffffff"), timestamp=3)
    dt_b = msgpack.unpackb(wire("c70cff00000000ffffffffff000000"), timestamp=3)
    # 1 ns apart across a whole-second boundary: still distinct datetimes.
    assert nano_b - nano_a == 1
    assert dt_a != dt_b
    assert dt_b - dt_a == datetime.timedelta(microseconds=1)


# --- Illegal payloads vs legal-but-insufficient-precision --------------------
# Kept as separate categories: illegal payloads must be rejected in every
# decode mode; legal-but-insufficient payloads decode fine in exact modes
# and only fail or lose information in low-precision modes.

ILLEGAL_PAYLOADS = [
    "d4ff00",  # fixext 1 with code -1
    "d5ff0000",  # fixext 2 with code -1
    "d8ff" + "00" * 16,  # fixext 16 with code -1
    "c700ff",  # ext8 len 0
    "c703ff" + "00" * 3,  # ext8 len 3
    "c705ff" + "00" * 5,  # ext8 len 5
    "d7ff" + "ff" * 8,  # timestamp64, nanoseconds > 999999999
    "c708ff" + "ff" * 8,  # same payload via ext8
    "c70cff" + "ff" * 12,  # timestamp96, nanoseconds > 999999999
]


@pytest.mark.parametrize("hexform", ILLEGAL_PAYLOADS)
def test_illegal_payloads_rejected_in_every_mode(hexform):
    for mode in (0, 1, 2, 3):
        with pytest.raises(ValueError):
            msgpack.unpackb(wire(hexform), timestamp=mode)


# Legal wire payloads whose values exceed what the low-precision modes can
# hold: the full timestamp96 range.
HUGE_POS = "c70cff3b9ac9ff7fffffffffffffff"  # ( 2**63 - 1, 999999999)
HUGE_NEG = "c70cff000000008000000000000000"  # (-2**63, 0)


def test_legal_but_beyond_datetime_range():
    # Exact modes accept the full 96-bit range...
    assert msgpack.unpackb(wire(HUGE_POS), timestamp=0) == Timestamp(
        2**63 - 1, 999999999
    )
    assert msgpack.unpackb(wire(HUGE_POS), timestamp=2) == 9223372036854775807999999999
    assert msgpack.unpackb(wire(HUGE_NEG), timestamp=0) == Timestamp(-(2**63), 0)
    assert msgpack.unpackb(wire(HUGE_NEG), timestamp=2) == -9223372036854775808000000000
    # ...float mode decodes but cannot hold the precision...
    assert msgpack.unpackb(wire(HUGE_POS), timestamp=1) == 9.223372036854776e18
    # ...and datetime mode must reject what it cannot represent.
    for form in (HUGE_POS, HUGE_NEG):
        with pytest.raises(OverflowError):
            msgpack.unpackb(wire(form), timestamp=3)
