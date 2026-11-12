"""Replayable regression tests for ``Faker.unique`` as a backstop for
composite natural keys that contain lists and dicts.

The guiding idea is a *row ledger*: whenever a row is emitted, its business
value is snapshotted (deep copied) at that instant, together with the number of
underlying provider draws it took and the final outcome (accepted / redrawn /
exhausted / behaviour around ``clear()``). The recorded ledger is then compared
step by step against:

* the real ``UniqueProxy`` driven through deterministic, scripted providers, and
* an independent greedy oracle that models the same loop without importing any
  Faker uniqueness code.

Sequences are short (at most six operations) and fully deterministic, so every
case is replayable. The exhaustive sweep varies parameter type, locale and the
scope of ``clear()`` and is able to detect both a wrong *merge* of two domains
that must stay independent and a wrong *split* of one domain that must stay
shared. Cases where the shipped semantics diverge from the ledger are kept as
executable ``xfail`` difference cases (or as passing characterisation tests for
hard limitations such as unhashable call arguments).
"""

import copy
import itertools
from collections import deque

import pytest

import faker.proxy as proxy_module
from faker import Faker
from faker.exceptions import UniquenessException
from faker.providers import BaseProvider
from faker.proxy import UniqueProxy

REPLAY_CAP = 5
LOCALES = ("en_US", "fr_FR")
HASH = UniqueProxy(None)._make_hashable


# --------------------------------------------------------------------------- #
# Deterministic test providers
# --------------------------------------------------------------------------- #
class ScriptedKeyProvider(BaseProvider):
    """Draws values from explicit per-method scripts.

    ``raw_calls`` counts every underlying provider invocation, including the
    redraws performed inside the uniqueness loop, so redraw behaviour is
    observable. ``draw(mode)`` takes an argument so that different signatures
    resolve to different uniqueness pools.
    """

    def __init__(self, generator):
        super().__init__(generator)
        self._scripts = {}
        self.raw_calls = 0

    def set_script(self, name, values):
        # An infinite, deterministic stream keeps replays reproducible even
        # when the uniqueness loop redraws many times.
        self._scripts[name] = itertools.cycle(list(values))

    def _next(self, name):
        self.raw_calls += 1
        return next(self._scripts[name])

    def draw(self, mode=0):
        return self._next("draw")


class ReusedObjectProvider(BaseProvider):
    """Returns the *same* container on every call, mutating it in place.

    This models providers that reuse and rewrite a previously returned object,
    which is the dangerous case for a caller that retains emitted rows.
    """

    def __init__(self, generator):
        super().__init__(generator)
        self._bucket = {"items": []}
        self._n = 0

    def reused_record(self):
        self._n += 1
        self._bucket["items"] = [self._n]
        return self._bucket


class FreshObjectProvider(BaseProvider):
    """Returns a brand new container on every call."""

    def __init__(self, generator):
        super().__init__(generator)
        self._n = 0

    def fresh_record(self):
        self._n += 1
        return {"seq": self._n, "tags": [self._n]}


class TypeSwitchProvider(BaseProvider):
    """Returns values of distinct business types that collide once normalised."""

    def __init__(self, generator):
        super().__init__(generator)
        self._coord_script = deque()
        self._scalar_script = deque()

    def set_scripts(self, coord_values=(), scalar_values=()):
        self._coord_script = itertools.cycle(list(coord_values)) if coord_values else deque()
        self._scalar_script = itertools.cycle(list(scalar_values)) if scalar_values else deque()

    def coord(self):
        return next(self._coord_script)

    def scalar(self):
        return next(self._scalar_script)


# --------------------------------------------------------------------------- #
# Independent reference oracle (no Faker uniqueness code)
# --------------------------------------------------------------------------- #
class GreedyOracle:
    """Reference model of the greedy uniqueness loop.

    ``mode`` selects how values are partitioned into pools:

    * ``"correct"``: pools are keyed by call signature only; locales share one
      pool (this is the shipped ``UniqueProxy`` semantics).
    * ``"merge"``: one global pool regardless of signature/locale; models an
      implementation that wrongly merges two independent domains.
    * ``"split"``: pools keyed by ``(locale, signature)``; models an
      implementation that wrongly splits one shared domain per locale.
    """

    def __init__(self, mode="correct", cap=REPLAY_CAP):
        self.mode = mode
        self.cap = cap
        self.pools = {}
        self.streams = {locale: itertools.cycle([0, 1]) for locale in LOCALES}

    def _pool_key(self, locale, mode_arg):
        if self.mode == "merge":
            return ("global",)
        if self.mode == "split":
            return (locale, mode_arg)
        return (mode_arg,)

    def emit(self, locale, mode_arg):
        generated = self.pools.setdefault(self._pool_key(locale, mode_arg), set())
        for attempts in range(1, self.cap + 1):
            value = next(self.streams[locale])
            hashable = HASH(value)
            if hashable not in generated:
                generated.add(hashable)
                return {"outcome": "accept", "attempts": attempts, "value": value}
        return {"outcome": "exhaust", "attempts": self.cap, "value": None}

    def clear(self):
        self.pools.clear()


# --------------------------------------------------------------------------- #
# Production-code replay engine backed by a lightweight stub
# --------------------------------------------------------------------------- #
class _CallCounter:
    def __init__(self):
        self.counts = {locale: 0 for locale in LOCALES}


class StubFaker:
    """Minimal object behind a real ``UniqueProxy``.

    Locale selection flows through the real ``UniqueProxy.__getitem__`` (which
    shares ``_seen`` across locale portals), while value generation comes from
    deterministic per-locale streams. No randomness is involved, so the whole
    replay is reproducible.
    """

    def __init__(self, streams, counter, locale=LOCALES[0]):
        self._streams = streams
        self._counter = counter
        self._locale = locale

    def __getitem__(self, locale):
        return StubFaker(self._streams, self._counter, locale)

    def __getattr__(self, name):
        locale = self._locale
        stream = self._streams[locale][name]

        def _call(*args, **kwargs):
            self._counter.counts[locale] += 1
            return next(stream)

        return _call


def _fresh_streams():
    return {
        locale: {"draw": itertools.cycle([0, 1])}
        for locale in LOCALES
    }


def replay_production(sequence):
    """Drive the real ``UniqueProxy`` through ``sequence`` and return events.

    Each operation is either ``"clear"`` (a root-level clear) or a
    ``(locale, mode)`` draw. Events mirror :class:`GreedyOracle` output and also
    carry a deep-copied ledger snapshot of every accepted value.
    """
    counter = _CallCounter()
    unique = UniqueProxy(StubFaker(_fresh_streams(), counter))
    events = []
    for operation in sequence:
        if operation == "clear":
            unique.clear()
            events.append({"outcome": "clear", "attempts": 0, "value": None})
            continue
        locale, mode = operation
        before = counter.counts[locale]
        try:
            value = unique[locale].draw(mode)
        except UniquenessException:
            events.append(
                {
                    "outcome": "exhaust",
                    "attempts": counter.counts[locale] - before,
                    "value": None,
                }
            )
        else:
            events.append(
                {
                    "outcome": "accept",
                    "attempts": counter.counts[locale] - before,
                    "value": copy.deepcopy(value),
                }
            )
    return events


def replay_oracle(sequence, mode="correct"):
    oracle = GreedyOracle(mode=mode)
    events = []
    for operation in sequence:
        if operation == "clear":
            oracle.clear()
            events.append({"outcome": "clear", "attempts": 0, "value": None})
            continue
        locale, mode_arg = operation
        events.append(oracle.emit(locale, mode_arg))
    return events


# --------------------------------------------------------------------------- #
# Hashable representation: characterisation of normalisation collisions
# --------------------------------------------------------------------------- #
class TestHashableRepresentation:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ([1, 2], (1, 2)),
            ({1, 2}, frozenset({1, 2})),
            ({1: "a"}, [(1, "a")]),
            ({"k": [1]}, {"k": (1,)}),
            ((), []),
            ({}, []),
            (1, True),
            (0, False),
        ],
    )
    def test_distinct_business_keys_normalise_to_the_same_hashable(self, left, right):
        # Pins the current lossy normalisation: type information is erased.
        assert HASH(left) == HASH(right)

    def test_nested_list_and_dict_are_hashed_by_value(self):
        assert HASH({"a": [1, 2], "b": {"c": [3]}}) == HASH({"a": (1, 2), "b": {"c": (3,)}})


# --------------------------------------------------------------------------- #
# Row ledger: accept / redraw / exhaust against an independent oracle
# --------------------------------------------------------------------------- #
class TestRowLedgerReplay:
    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        # Shrink the retry budget so exhaustion stays cheap and deterministic.
        monkeypatch.setattr(proxy_module, "_UNIQUE_ATTEMPTS", REPLAY_CAP)

    def _scripted_faker(self, values):
        fake = Faker()
        fake.seed_instance(12345)
        fake.add_provider(ScriptedKeyProvider)
        provider = next(p for p in fake.get_providers() if isinstance(p, ScriptedKeyProvider))
        # Pad the script so every redraw can be served.
        provider.set_script("draw", list(values))
        return fake, provider

    def test_ledger_records_accept_redraw_and_exhaust(self):
        # First unique call takes 1; the second must redraw through two
        # collisions before landing on a fresh value; the domain then exhausts.
        fake, provider = self._scripted_faker([1, 1, 1, 2] + [2] * REPLAY_CAP)

        ledger = []

        first = fake.unique.draw(0)
        ledger.append((copy.deepcopy(first), provider.raw_calls))
        assert first == 1
        assert provider.raw_calls == 1

        second = fake.unique.draw(0)
        ledger.append((copy.deepcopy(second), provider.raw_calls))
        assert second == 2
        # Two colliding redraws (1, 1) plus the successful draw.
        assert provider.raw_calls - ledger[0][1] == 3

        with pytest.raises(UniquenessException, match=r"Got duplicated values after"):
            fake.unique.draw(0)
        # Exhaustion burns exactly the capped number of attempts.
        assert provider.raw_calls - ledger[1][1] == REPLAY_CAP

        # Ledger holds the two distinct emit-time business values.
        assert [row[0] for row in ledger] == [1, 2]

    def test_clear_then_first_emit_is_accepted_and_reusable(self):
        fake, provider = self._scripted_faker([7, 7] + [7] * REPLAY_CAP)
        first = fake.unique.draw(0)
        assert first == 7
        with pytest.raises(UniquenessException):
            fake.unique.draw(0)

        fake.unique.clear()
        # First emission after clear is accepted even though the value was seen
        # before clearing, and it costs exactly one provider draw (no redraw).
        calls_before = provider.raw_calls
        again = fake.unique.draw(0)
        assert again == 7
        assert provider.raw_calls - calls_before == 1


# --------------------------------------------------------------------------- #
# Mutable reuse and post-emission caller mutation
# --------------------------------------------------------------------------- #
class TestMutableValues:
    def test_caller_mutation_of_fresh_object_does_not_corrupt_cache(self):
        fake = Faker()
        fake.seed_instance(7)
        fake.add_provider(FreshObjectProvider)

        first = fake.unique.fresh_record()
        snapshot = copy.deepcopy(first)
        second = fake.unique.fresh_record()
        # The caller rewrites a field on the row it already holds.
        first["tags"].append("mutated-after-emit")

        # The cache stores an immutable normalised snapshot taken at emission,
        # so post-hoc caller edits neither create false duplicates nor corrupt
        # the uniqueness bookkeeping.
        assert snapshot == {"seq": 1, "tags": [1]}
        assert second == {"seq": 2, "tags": [2]}
        assert HASH(snapshot) in _domain_values(fake, "fresh_record")

    def test_provider_reusing_one_object_collapses_caller_rows(self):
        fake = Faker()
        fake.seed_instance(7)
        fake.add_provider(ReusedObjectProvider)

        ledger = []
        held = []
        for _ in range(3):
            row = fake.unique.reused_record()
            ledger.append(copy.deepcopy(row))  # business value at emit time
            held.append(row)

        # Every emission is accepted and the emit-time ledger is distinct...
        assert len({HASH(row) for row in ledger}) == 3
        # ...but all three returned references alias the provider's one object,
        # so the rows the caller actually retains are now duplicates.
        assert all(row is held[0] for row in held)
        assert len({HASH(row) for row in held}) == 1

    @pytest.mark.xfail(
        strict=True,
        reason="UniqueProxy caches a normalised snapshot but returns the "
        "provider's shared, mutable object; retained rows alias each other.",
    )
    def test_ideal_retained_rows_match_emit_time_ledger(self):
        fake = Faker()
        fake.seed_instance(7)
        fake.add_provider(ReusedObjectProvider)

        ledger, held = [], []
        for _ in range(3):
            row = fake.unique.reused_record()
            ledger.append(copy.deepcopy(row))
            held.append(row)

        # Ideal semantics: retained rows preserve their emit-time business
        # values even when the provider reuses one object. Fails on the shipped
        # implementation and turns into an XPASS if the gap is ever fixed.
        assert [HASH(row) for row in held] == [HASH(row) for row in ledger]


def _domain_values(fake, method_name):
    for cache_key, generated in fake.unique._seen.items():
        if cache_key[0] == method_name:
            return set(generated)
    return set()


# --------------------------------------------------------------------------- #
# False rejects: distinct business keys refused because normalisation collides
# --------------------------------------------------------------------------- #
class TestFalseRejectDifferences:
    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        monkeypatch.setattr(proxy_module, "_UNIQUE_ATTEMPTS", REPLAY_CAP)

    def _faker_with(self, provider_cls):
        fake = Faker()
        fake.seed_instance(99)
        fake.add_provider(provider_cls)
        return fake

    @pytest.mark.xfail(
        strict=True,
        reason="list and tuple with equal content normalise to the same "
        "hashable, so the genuinely distinct second key is wrongly exhausted.",
    )
    def test_list_vs_tuple_are_both_accepted(self):
        fake = self._faker_with(TypeSwitchProvider)
        provider = next(p for p in fake.get_providers() if isinstance(p, TypeSwitchProvider))
        provider.set_scripts(coord_values=[[1, 2], (1, 2)])

        first = fake.unique.coord()
        second = fake.unique.coord()  # currently raises UniquenessException
        assert first == [1, 2]
        assert second == (1, 2)

    @pytest.mark.xfail(
        strict=True,
        reason="dict and a list of key/value pairs normalise identically.",
    )
    def test_dict_vs_list_of_pairs_are_both_accepted(self):
        fake = self._faker_with(TypeSwitchProvider)
        provider = next(p for p in fake.get_providers() if isinstance(p, TypeSwitchProvider))
        provider.set_scripts(coord_values=[{1: "a"}, [(1, "a")]])

        assert fake.unique.coord() == {1: "a"}
        assert fake.unique.coord() == [(1, "a")]

    @pytest.mark.xfail(
        strict=True,
        reason="bool is a subclass of int; 1 and True normalise to the same key.",
    )
    def test_int_vs_bool_are_both_accepted(self):
        fake = self._faker_with(TypeSwitchProvider)
        provider = next(p for p in fake.get_providers() if isinstance(p, TypeSwitchProvider))
        provider.set_scripts(scalar_values=[1, True])

        assert fake.unique.scalar() == 1
        assert fake.unique.scalar() is True

    def test_collision_is_observed_as_a_false_reject_today(self):
        # Characterisation pin: the second, genuinely distinct key is refused.
        fake = self._faker_with(TypeSwitchProvider)
        provider = next(p for p in fake.get_providers() if isinstance(p, TypeSwitchProvider))
        provider.set_scripts(coord_values=[[1, 2], (1, 2)])

        assert fake.unique.coord() == [1, 2]
        with pytest.raises(UniquenessException):
            fake.unique.coord()


# --------------------------------------------------------------------------- #
# Parameter type: call signatures define independent uniqueness pools
# --------------------------------------------------------------------------- #
class TestParameterScope:
    def test_two_parameter_entry_points_keep_independent_pools(self):
        fake = Faker()
        fake.seed_instance(3)
        for _ in range(2):
            fake.unique.random_int(min=0, max=1)
        with pytest.raises(UniquenessException):
            fake.unique.random_int(min=0, max=1)

        # Different parameter types -> different cache key -> fresh pool.
        fake.unique.random_int(min=2, max=3)
        keys = list(fake.unique._seen.keys())
        assert ("random_int", (), (("max", 1), ("min", 0))) in keys
        assert ("random_int", (), (("max", 3), ("min", 2))) in keys

    def test_positional_and_keyword_signatures_are_separate_pools(self):
        fake = Faker()
        fake.seed_instance(3)
        fake.unique.random_int(0, 1)
        fake.unique.random_int(0, 1)
        with pytest.raises(UniquenessException):
            fake.unique.random_int(0, 1)

        # Same values, keyword form: a distinct signature key, so it succeeds.
        fake.unique.random_int(min=0, max=1)
        assert ("random_int", (0, 1), ()) in fake.unique._seen
        assert ("random_int", (), (("max", 1), ("min", 0))) in fake.unique._seen

    def test_list_or_dict_call_arguments_raise_type_error(self):
        # Only return values are normalised; unhashable call arguments cannot
        # form a cache key at all. This is a hard limitation for composite keys
        # passed positionally as lists/dicts.
        fake = Faker()
        with pytest.raises(TypeError, match="unhashable type: 'list'"):
            fake.unique.pydict(nb_elements=1, value_types=[str])
        with pytest.raises(TypeError, match="unhashable type: 'dict'"):
            fake.unique.pydict(nb_elements=1, value_types={"a": int})

    def test_tuple_call_argument_is_supported(self):
        # The immutable equivalent of a list-valued composite argument works.
        fake = Faker()
        value = fake.unique.pydict(nb_elements=1, value_types=(str,))
        assert isinstance(value, dict)


# --------------------------------------------------------------------------- #
# Locale scope: two locales of one proxy share a single domain
# --------------------------------------------------------------------------- #
class TestLocaleScope:
    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        monkeypatch.setattr(proxy_module, "_UNIQUE_ATTEMPTS", REPLAY_CAP)

    def _multi(self, values=(0, 1)):
        multi = Faker(list(LOCALES))
        for _locale, sub in multi.items():
            sub.add_provider(ScriptedKeyProvider)
            provider = next(p for p in sub.get_providers() if isinstance(p, ScriptedKeyProvider))
            provider.set_script("draw", list(values))
        return multi

    def test_interleaved_locales_share_one_uniqueness_domain(self):
        multi = self._multi(values=range(4))
        ledger = []
        for locale in (LOCALES[0], LOCALES[1], LOCALES[0], LOCALES[1]):
            value = multi.unique[locale].draw(0)
            assert HASH(value) not in [HASH(v) for v in ledger]
            ledger.append(copy.deepcopy(value))
        assert len({HASH(v) for v in ledger}) == 4

        # The four-value domain is now full for either locale.
        for locale in LOCALES:
            with pytest.raises(UniquenessException):
                multi.unique[locale].draw(0)

    def test_duplicates_between_separate_fakers_are_out_of_scope(self):
        # Two independent proxies do not share a cache: equal rows are allowed.
        first = Faker()
        second = Faker()
        first.seed_instance(11)
        second.seed_instance(11)
        left = first.unique.random_int(min=0, max=100)
        right = second.unique.random_int(min=0, max=100)
        assert left == right  # same seed -> same value, no cross-instance check

    def test_excluded_types_are_intentionally_outside_uniqueness(self):
        fake = Faker()
        proxy = fake.unique.exclude_types([bool])
        values = {proxy.pybool() for _ in range(20)}
        assert values <= {True, False}  # duplicates permitted on purpose

    def test_locale_subscript_proxies_share_the_seen_mapping(self):
        multi = self._multi()
        en = multi.unique[LOCALES[0]]
        fr = multi.unique[LOCALES[1]]
        assert en._seen is multi.unique._seen
        assert fr._seen is multi.unique._seen

    def test_clear_on_a_locale_portal_silently_splits_the_domain(self):
        # Characterisation of the current clear() scope: clear() rebinds that
        # portal's own _seen instead of emptying the shared container.
        multi = self._multi()
        en = multi.unique[LOCALES[0]]
        fr = multi.unique[LOCALES[1]]
        en.draw(0)
        en.draw(0)  # two-value domain full
        with pytest.raises(UniquenessException):
            fr.draw(0)

        en.clear()
        assert en._seen is not multi.unique._seen  # portal split off
        assert fr._seen is multi.unique._seen  # sibling still on old container

        # en_US can now re-emit a value the global ledger already recorded...
        en.draw(0)
        # ...while fr_FR remains exhausted against the untouched container.
        with pytest.raises(UniquenessException):
            fr.draw(0)

    @pytest.mark.xfail(
        strict=True,
        reason="clear() on a locale portal rebinds only that portal's _seen; "
        "the shared domain is not reset consistently across locales.",
    )
    def test_ideal_locale_clear_resets_the_shared_domain(self):
        multi = self._multi()
        en = multi.unique[LOCALES[0]]
        fr = multi.unique[LOCALES[1]]
        en.draw(0)
        en.draw(0)
        with pytest.raises(UniquenessException):
            fr.draw(0)

        en.clear()
        # Ideal: clearing one portal of a shared domain resets the domain for
        # every portal, so fr_FR can draw again. Today it stays exhausted.
        fr.draw(0)

    def test_cached_portal_diverges_after_a_root_clear(self):
        multi = self._multi()
        cached_en = multi.unique[LOCALES[0]]
        cached_en.draw(0)
        cached_en.draw(0)
        with pytest.raises(UniquenessException):
            cached_en.draw(0)

        multi.unique.clear()  # root rebinds its own _seen
        # A portal cached before the clear keeps pointing at the old container.
        assert cached_en._seen is not multi.unique._seen
        with pytest.raises(UniquenessException):
            cached_en.draw(0)
        # A freshly fetched portal uses the new, empty container.
        multi.unique[LOCALES[0]].draw(0)


# --------------------------------------------------------------------------- #
# Exhaustive, replayable state enumeration (length <= 6)
# --------------------------------------------------------------------------- #
VOCABULARY = [
    (LOCALES[0], 0),
    (LOCALES[1], 0),
    (LOCALES[0], 1),
    (LOCALES[1], 1),
    "clear",
]


def _all_sequences(max_length):
    for length in range(1, max_length + 1):
        yield from itertools.product(VOCABULARY, repeat=length)


def _draw_events(events):
    return [event for event in events if event["outcome"] != "clear"]


class TestExhaustiveReplay:
    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        # Keep exhaustion cheap and identical to the oracle's retry budget.
        monkeypatch.setattr(proxy_module, "_UNIQUE_ATTEMPTS", REPLAY_CAP)

    def test_production_matches_correct_oracle_for_every_sequence(self):
        checked = 0
        for sequence in _all_sequences(6):
            production = replay_production(sequence)
            expected = replay_oracle(sequence, mode="correct")
            assert production == expected, f"diverging sequence: {sequence!r}"
            checked += 1
        assert checked == 19530

    def test_sweep_detects_wrong_merge_and_wrong_split(self):
        merge_witnesses = []
        split_witnesses = []
        for sequence in _all_sequences(6):
            correct = replay_oracle(sequence, mode="correct")
            if replay_oracle(sequence, mode="merge") != correct:
                merge_witnesses.append(sequence)
            if replay_oracle(sequence, mode="split") != correct:
                split_witnesses.append(sequence)

        # Minimal, replayable witnesses for each class of defect.
        assert ((LOCALES[0], 0), (LOCALES[0], 0), (LOCALES[0], 1)) in merge_witnesses
        assert ((LOCALES[0], 0), (LOCALES[0], 0), (LOCALES[1], 0)) in split_witnesses
        assert merge_witnesses and split_witnesses

    def test_ledger_uniqueness_holds_within_each_signature_domain(self):
        # Independently re-derive accept/exhaust from the emitted ledger using
        # only signature-scoped, locale-shared pools, and confirm it matches the
        # production outcomes for every enumerated sequence.
        for sequence in _all_sequences(6):
            events = replay_production(sequence)
            pools = {}
            event_iter = iter(events)
            for operation in sequence:
                event = next(event_iter)
                if operation == "clear":
                    assert event["outcome"] == "clear"
                    pools.clear()
                    continue
                _locale, mode = operation
                generated = pools.setdefault(mode, set())
                if event["outcome"] == "accept":
                    hashable = HASH(event["value"])
                    assert hashable not in generated, f"duplicate released: {sequence!r}"
                    generated.add(hashable)
                else:
                    assert event["outcome"] == "exhaust"
                    assert event["attempts"] == REPLAY_CAP

    def test_redraw_attempts_agree_with_oracle(self):
        for sequence in _all_sequences(6):
            production = _draw_events(replay_production(sequence))
            expected = _draw_events(replay_oracle(sequence, mode="correct"))
            assert [e["attempts"] for e in production] == [e["attempts"] for e in expected]
            assert [e["value"] for e in production] == [e["value"] for e in expected]


class TestExhaustiveReplayWithRealFaker:
    """Integration cross-check: the same enumeration against real Faker
    generators and real custom providers (kept short to stay fast)."""

    @pytest.fixture(autouse=True)
    def _small_cap(self, monkeypatch):
        monkeypatch.setattr(proxy_module, "_UNIQUE_ATTEMPTS", REPLAY_CAP)

    def _build_multi_faker(self):
        multi = Faker(list(LOCALES))
        providers = {}
        for locale, sub in multi.items():
            sub.add_provider(ScriptedKeyProvider)
            provider = next(p for p in sub.get_providers() if isinstance(p, ScriptedKeyProvider))
            provider.set_script("draw", [0, 1] * 200)
            providers[locale] = provider
        return multi, providers

    def test_short_sequences_match_oracle(self):
        for sequence in _all_sequences(4):
            multi, providers = self._build_multi_faker()
            expected = replay_oracle(sequence, mode="correct")
            actual = []
            for operation in sequence:
                if operation == "clear":
                    multi.unique.clear()
                    actual.append({"outcome": "clear", "attempts": 0, "value": None})
                    continue
                locale, mode = operation
                before = providers[locale].raw_calls
                try:
                    value = multi.unique[locale].draw(mode)
                except UniquenessException:
                    actual.append(
                        {
                            "outcome": "exhaust",
                            "attempts": providers[locale].raw_calls - before,
                            "value": None,
                        }
                    )
                else:
                    actual.append(
                        {
                            "outcome": "accept",
                            "attempts": providers[locale].raw_calls - before,
                            "value": value,
                        }
                    )
            assert actual == expected, f"diverging sequence: {sequence!r}"
