"""Repeatable weight verification for multi-locale factory dispatch.

These tests treat every proxied call as a *tagged sample*: a source recorder
observes which locale's factory actually produced each value, and the observed
source frequencies are checked against an independently computed conditional
distribution (locale weights re-normalized over the factories that really
provide the requested method).

The matrix below evolves one scenario table through five cases:

- all factories provide the method
- one factory is missing the method (conditional candidate set)
- a locale carries zero weight
- two locales inherit the same provider implementation (identical values)
- the method was fetched before its provider was registered (stale cache)

When observed and expected distributions diverge, `diagnose_source_deviation`
separates three causes: a stale candidate cache, a normalization error in the
expected distribution, or field values that coincide across locales.
"""

import bisect
import itertools
import random

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple
from unittest.mock import patch

import pytest

from faker import Faker
from faker.proxy import Faker as FakerProxy
from tests.mymodule.en_US import Provider as SharedProvider

SEED = 20261112
SAMPLE_SIZE = 3000
FREQUENCY_TOLERANCE = 0.03
TWO_SAMPLE_TOLERANCE = 0.04


class ControlledRandom:
    """Stand-in for ``random.Random`` driven by an explicit queue of uniform draws.

    ``choices`` and ``choice`` replicate the stdlib selection semantics
    (``bisect_right`` over cumulative weights) so tests can place samples on
    both sides of a candidate interval boundary. Any other method falls back
    to a privately seeded ``Random`` instance.
    """

    def __init__(self, uniforms: Sequence[float], fallback_seed: int = 0):
        self._uniforms = deque(uniforms)
        self._fallback = random.Random(fallback_seed)

    def __getattr__(self, name):
        return getattr(self._fallback, name)

    def _next(self) -> float:
        if not self._uniforms:
            raise AssertionError(
                "ControlledRandom queue exhausted: the code under test drew more samples than expected"
            )
        return self._uniforms.popleft()

    def choices(self, population, weights=None, *, k=1):
        if weights is None:
            count = len(population)
            return [population[int(self._next() * count)] for _ in range(k)]
        cum_weights = list(itertools.accumulate(weights))
        total = cum_weights[-1]
        return [population[bisect.bisect_right(cum_weights, self._next() * total)] for _ in range(k)]

    def choice(self, seq):
        return seq[int(self._next() * len(seq))]


class SourceRecorder:
    """Context manager tagging every factory selection with its source locale."""

    def __init__(self, fake: FakerProxy):
        self._locale_by_factory_id = {id(factory): locale for locale, factory in fake.items()}
        self.selections: List[Tuple[str, str]] = []
        self._patcher = None

    def __enter__(self) -> "SourceRecorder":
        original = FakerProxy._select_factory
        recorder = self

        def wrapped(proxy, method_name):
            factory = original(proxy, method_name)
            locale = recorder._locale_by_factory_id.get(id(factory))
            if locale is not None:
                recorder.selections.append((method_name, locale))
            return factory

        self._patcher = patch.object(FakerProxy, "_select_factory", wrapped)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info):
        self._patcher.stop()
        return False

    def tags_for(self, method_name: str) -> List[str]:
        return [locale for method, locale in self.selections if method == method_name]


def expected_conditional_distribution(fake: FakerProxy, method_name: str) -> Dict[str, float]:
    """Independently compute P(source locale) for one proxied call.

    Only factories actually providing ``method_name`` are candidates, so the
    configured weights are re-normalized over that subset. A locale with zero
    weight keeps probability zero.
    """
    weights = fake.weights if fake.weights is not None else [1] * len(fake.factories)
    candidates = [
        (locale, weight)
        for (locale, factory), weight in zip(fake.items(), weights)
        if hasattr(factory, method_name)
    ]
    total = sum(weight for _, weight in candidates)
    return {locale: weight / total for locale, weight in candidates}


def assert_frequencies_close(counts: Counter, expected: Dict[str, float], tolerance: float = FREQUENCY_TOLERANCE):
    total = sum(counts.values())
    assert total > 0, "no samples were recorded"
    for locale, probability in expected.items():
        observed = counts.get(locale, 0) / total
        assert abs(observed - probability) <= tolerance, (
            f"source {locale!r} observed {observed:.4f}, expected {probability:.4f} (+/-{tolerance})"
        )
    unexpected = set(counts) - set(expected)
    assert not unexpected, f"samples came from locales outside the candidate set: {sorted(unexpected)}"


def assert_two_samples_consistent(counts_a: Counter, counts_b: Counter, tolerance: float = TWO_SAMPLE_TOLERANCE):
    """Assert two observed source distributions are the same up to sampling noise."""
    total_a = sum(counts_a.values())
    total_b = sum(counts_b.values())
    assert total_a > 0 and total_b > 0, "both samples must be non-empty"
    for locale in set(counts_a) | set(counts_b):
        diff = abs(counts_a.get(locale, 0) / total_a - counts_b.get(locale, 0) / total_b)
        assert diff <= tolerance, (
            f"distribution changed for source {locale!r}: "
            f"{counts_a.get(locale, 0) / total_a:.4f} vs {counts_b.get(locale, 0) / total_b:.4f} (+/-{tolerance})"
        )


@dataclass
class DeviationDiagnosis:
    """Structured result separating the possible causes of a weight deviation."""

    method_name: str
    cache_present: bool
    cache_stale: bool
    stale_detail: str
    normalization_ok: bool
    expected_probability_sum: float
    value_collisions: Dict[object, List[str]] = field(default_factory=dict)

    @property
    def has_anomaly(self) -> bool:
        return self.cache_stale or not self.normalization_ok or bool(self.value_collisions)

    def explain(self) -> str:
        causes = []
        if self.cache_stale:
            causes.append(f"stale candidate cache ({self.stale_detail})")
        if not self.normalization_ok:
            causes.append(f"normalization error (expected probabilities sum to {self.expected_probability_sum:.6f})")
        if self.value_collisions:
            collisions = "; ".join(
                f"{value!r} from {sorted(locales)}" for value, locales in self.value_collisions.items()
            )
            causes.append(f"identical field values across sources ({collisions})")
        return "; ".join(causes) if causes else "no anomaly detected"


def diagnose_source_deviation(
    fake: FakerProxy,
    method_name: str,
    tagged_samples: Sequence[Tuple[object, str]],
    expected_probs: Dict[str, float],
) -> DeviationDiagnosis:
    """Classify why observed sources may deviate from ``expected_probs``.

    Distinguishes, in order:
    1. a stale ``_cached_<method>_mapping`` that no longer matches the
       factories/weights a fresh candidate scan would produce,
    2. a normalization error in the expected conditional distribution, and
    3. field values that coincide across source locales, which makes
       value-based (rather than tag-based) attribution unreliable.
    """
    weights = fake.weights if fake.weights is not None else [1] * len(fake.factories)
    fresh = [
        (locale, weight)
        for (locale, factory), weight in zip(fake.items(), weights)
        if hasattr(factory, method_name)
    ]
    fresh_locales = [locale for locale, _ in fresh]
    fresh_weights = [weight for _, weight in fresh]

    cache_attr = f"_cached_{method_name}_mapping"
    cached = getattr(fake, cache_attr, None)
    cache_present = cached is not None
    cache_stale = False
    stale_detail = ""
    if cache_present:
        locale_by_id = {id(factory): locale for locale, factory in fake.items()}
        cached_factories, cached_weights = cached
        cached_locales = [locale_by_id.get(id(factory)) for factory in cached_factories]
        # Unweighted proxies cache `None` weights; weighted proxies cache a list.
        if fake.weights is None:
            weights_match = cached_weights is None
        else:
            weights_match = list(cached_weights or []) == fresh_weights
        if cached_locales != fresh_locales or not weights_match:
            cache_stale = True
            stale_detail = f"cached candidates {cached_locales} but a fresh scan yields {fresh_locales}"

    probability_sum = sum(expected_probs.values())
    total_weight = sum(fresh_weights)
    normalization_ok = (
        abs(probability_sum - 1.0) <= 1e-6
        and all(0.0 <= probability <= 1.0 for probability in expected_probs.values())
        and set(expected_probs) == set(fresh_locales)
        and all(
            abs(expected_probs[locale] - weight / total_weight) <= 1e-9 for locale, weight in fresh if total_weight > 0
        )
    )

    locales_by_value: Dict[object, set] = {}
    for value, locale in tagged_samples:
        locales_by_value.setdefault(value, set()).add(locale)
    value_collisions = {
        value: sorted(locales) for value, locales in locales_by_value.items() if len(locales) > 1
    }

    return DeviationDiagnosis(
        method_name=method_name,
        cache_present=cache_present,
        cache_stale=cache_stale,
        stale_detail=stale_detail,
        normalization_ok=normalization_ok,
        expected_probability_sum=probability_sum,
        value_collisions=value_collisions,
    )


def sample_tagged(fake: FakerProxy, method_name: str, count: int) -> Tuple[List[object], List[str]]:
    """Draw ``count`` samples, returning values aligned with their source tags.

    The attribute lookup must happen inside the loop: each ``getattr`` on the
    proxy triggers a fresh factory selection, and only then is the chosen
    factory's method called.
    """
    with SourceRecorder(fake) as recorder:
        values = [getattr(fake, method_name)() for _ in range(count)]
    tags = recorder.tags_for(method_name)
    assert len(tags) == count, "every proxied call must produce exactly one source tag"
    return values, tags


@dataclass
class DispatchScenario:
    """One row of the dispatch matrix."""

    locales: object  # OrderedDict of weights, or a plain list of locale codes
    method: str
    includes: Tuple[str, ...] = ()
    late_registration: bool = False


SCENARIOS = [
    # Every factory provides the method: candidates are all locales.
    pytest.param(
        DispatchScenario(
            locales=OrderedDict([("de_DE", 3), ("en_US", 2), ("en_PH", 1), ("ja_JP", 5)]),
            method="name",
        ),
        id="all-factories-provide-method",
    ),
    # de_DE and en_PH lack `zipcode`: the conditional candidate set is
    # {en_US, ja_JP} and their weights re-normalize to 2/7 and 5/7.
    pytest.param(
        DispatchScenario(
            locales=OrderedDict([("de_DE", 3), ("en_US", 2), ("en_PH", 1), ("ja_JP", 5)]),
            method="zipcode",
        ),
        id="factory-missing-method",
    ),
    # A zero-weight locale is a candidate but must never be selected.
    pytest.param(
        DispatchScenario(
            locales=OrderedDict([("de_DE", 0), ("en_US", 2), ("ja_JP", 5)]),
            method="name",
        ),
        id="zero-weight-locale",
    ),
    # Both locales inherit the same provider implementation via `includes`,
    # so every value is the constant "bar": only source tags can attribute
    # samples, and the 1:3 weight split must still show through.
    pytest.param(
        DispatchScenario(
            locales=OrderedDict([("en_US", 1), ("ja_JP", 3)]),
            method="foo",
            includes=("tests.mymodule.en_US",),
        ),
        id="shared-implementation-inherited",
    ),
    # `foo` is fetched before any factory provides it, then registered on a
    # single locale: the stale empty cache must be cleared before dispatch
    # can see the new candidate. (Unweighted: with weights, an empty
    # candidate set raises ValueError before any cache is written.)
    pytest.param(
        DispatchScenario(
            locales=["en_US", "ja_JP"],
            method="foo",
            late_registration=True,
        ),
        id="fetched-before-registration",
    ),
]


class TestWeightedDispatchMatrix:
    """One scenario table covering the dispatch edge cases."""

    def _build(self, scenario: DispatchScenario) -> FakerProxy:
        return Faker(scenario.locales, includes=list(scenario.includes))

    @pytest.mark.parametrize("scenario", SCENARIOS)
    def test_observed_sources_match_conditional_distribution(self, scenario: DispatchScenario):
        fake = self._build(scenario)

        if scenario.late_registration:
            # Fetching the method before registration caches an empty
            # candidate mapping, and the failure itself is cached.
            with pytest.raises(AttributeError):
                getattr(fake, scenario.method)()
            fake["en_US"].add_provider(SharedProvider)
            with pytest.raises(AttributeError):
                getattr(fake, scenario.method)()
            delattr(fake, f"_cached_{scenario.method}_mapping")

        fake.seed_instance(SEED)
        expected = expected_conditional_distribution(fake, scenario.method)
        values, tags = sample_tagged(fake, scenario.method, SAMPLE_SIZE)

        counts = Counter(tags)
        assert_frequencies_close(counts, expected)

        # A zero-weight candidate must produce exactly zero samples.
        for locale, probability in expected.items():
            if probability == 0.0:
                assert counts.get(locale, 0) == 0, f"zero-weight locale {locale!r} was selected"

        # When two locales inherit the same implementation, values cannot
        # attribute sources; the tag-based check above is the only signal.
        if scenario.includes:
            assert len(set(values)) == 1, "shared implementation should yield identical values"
            collisions = diagnose_source_deviation(
                fake, scenario.method, list(zip(values, tags)), expected
            ).value_collisions
            assert collisions, "identical values across sources should be reported as collisions"

    def test_seeded_sampling_is_reproducible(self):
        locales = OrderedDict([("de_DE", 3), ("en_US", 2), ("en_PH", 1), ("ja_JP", 5)])

        first = Faker(locales)
        first.seed_instance(SEED)
        _, first_tags = sample_tagged(first, "name", 500)

        second = Faker(locales)
        second.seed_instance(SEED)
        _, second_tags = sample_tagged(second, "name", 500)

        assert first_tags == second_tags


class TestBoundarySampling:
    """Place uniform draws on both sides of each candidate interval boundary."""

    @staticmethod
    def _record_with_uniforms(fake: FakerProxy, method: str, uniforms: Sequence[float]) -> List[str]:
        fake.factories[0].random = ControlledRandom(uniforms)
        with SourceRecorder(fake) as recorder:
            for _ in uniforms:
                getattr(fake, method)()
        return recorder.tags_for(method)

    def test_weighted_boundaries_all_candidates(self):
        # Weights 3:2:5 put the interval boundaries at 0.3 and 0.5.
        fake = Faker(
            OrderedDict([("de_DE", 3), ("en_US", 2), ("ja_JP", 5)]),
            includes=["tests.mymodule.en_US"],
        )
        uniforms = [0.0, 0.2999, 0.3, 0.3001, 0.4999, 0.5, 0.5001, 0.9999]
        tags = self._record_with_uniforms(fake, "foo", uniforms)
        # bisect_right assigns a draw exactly on a boundary to the next bucket.
        assert tags == ["de_DE", "de_DE", "en_US", "en_US", "en_US", "ja_JP", "ja_JP", "ja_JP"]

    def test_weighted_boundary_with_missing_candidate(self):
        # Only en_US (2) and ja_JP (5) provide `zipcode`, so the conditional
        # boundary sits at 2/7 and de_DE/en_PH weights play no role.
        fake = Faker(OrderedDict([("de_DE", 3), ("en_US", 2), ("en_PH", 1), ("ja_JP", 5)]))
        boundary = 2 / 7
        uniforms = [boundary - 1e-9, boundary, boundary + 1e-9]
        tags = self._record_with_uniforms(fake, "zipcode", uniforms)
        assert tags == ["en_US", "ja_JP", "ja_JP"]

    def test_zero_weight_boundary(self):
        # A zero-weight interval is empty: even a draw of exactly 0.0 skips it.
        fake = Faker(
            OrderedDict([("de_DE", 0), ("en_US", 1)]),
            includes=["tests.mymodule.en_US"],
        )
        tags = self._record_with_uniforms(fake, "foo", [0.0, 0.0, 0.9999])
        assert tags == ["en_US", "en_US", "en_US"]

    def test_unweighted_choice_boundary(self):
        # Without weights, `random.choice` splits [0, 1) into equal halves.
        fake = Faker(["en_US", "ja_JP"], includes=["tests.mymodule.en_US"])
        tags = self._record_with_uniforms(fake, "foo", [0.4999, 0.5, 0.9999])
        assert tags == ["en_US", "ja_JP", "ja_JP"]


class TestLocaleOrderAndWeightSplit:
    """Acceptable sequence variation must not be confused with a real
    distribution change."""

    @staticmethod
    def _counts(locales, method="name", seed=SEED, count=SAMPLE_SIZE) -> Counter:
        fake = Faker(locales)
        fake.seed_instance(seed)
        _, tags = sample_tagged(fake, method, count)
        return Counter(tags)

    def test_swapped_locale_order_keeps_distribution(self):
        forward = self._counts(OrderedDict([("en_US", 1), ("ja_JP", 1)]))
        swapped = self._counts(OrderedDict([("ja_JP", 1), ("en_US", 1)]))

        # The seeded tag *sequences* may legitimately differ because the same
        # cumulative interval now maps to the other locale; only the
        # distribution has to be preserved.
        assert_frequencies_close(forward, {"en_US": 0.5, "ja_JP": 0.5})
        assert_frequencies_close(swapped, {"en_US": 0.5, "ja_JP": 0.5})
        assert_two_samples_consistent(forward, swapped)

    def test_split_equal_weight_sources_keep_distribution(self):
        # Scaling equal weights (1:1 vs 2:2) leaves the normalized
        # distribution unchanged even though absolute weights differ.
        unit = self._counts(OrderedDict([("en_US", 1), ("ja_JP", 1)]))
        doubled = self._counts(OrderedDict([("en_US", 2), ("ja_JP", 2)]))

        assert_frequencies_close(unit, {"en_US": 0.5, "ja_JP": 0.5})
        assert_frequencies_close(doubled, {"en_US": 0.5, "ja_JP": 0.5})
        assert_two_samples_consistent(unit, doubled)

    def test_real_distribution_change_is_detected(self):
        balanced = self._counts(OrderedDict([("en_US", 1), ("ja_JP", 1)]))
        skewed = self._counts(OrderedDict([("en_US", 1), ("ja_JP", 3)]))

        assert_frequencies_close(skewed, {"en_US": 0.25, "ja_JP": 0.75})
        with pytest.raises(AssertionError, match="distribution changed"):
            assert_two_samples_consistent(balanced, skewed)


class TestDeviationDiagnosis:
    """The diagnostic helper must separate cache, normalization, and value
    collision causes instead of reporting a generic mismatch."""

    def test_stale_cache_detected_and_cleared(self):
        # Unweighted mode: a failed lookup caches an empty candidate mapping.
        fake = Faker(["en_US", "ja_JP"])

        # Fetch before registration: the failed lookup is itself cached.
        with pytest.raises(AttributeError):
            fake.foo()
        fake["en_US"].add_provider(SharedProvider)

        expected = expected_conditional_distribution(fake, "foo")
        diagnosis = diagnose_source_deviation(fake, "foo", [], expected)
        assert diagnosis.cache_present
        assert diagnosis.cache_stale
        assert "en_US" in diagnosis.stale_detail
        assert "stale candidate cache" in diagnosis.explain()

        # Clearing the cached mapping lets dispatch see the new candidate.
        delattr(fake, "_cached_foo_mapping")
        fake.seed_instance(SEED)
        _, tags = sample_tagged(fake, "foo", 100)
        assert set(tags) == {"en_US"}
        diagnosis = diagnose_source_deviation(fake, "foo", [], expected)
        assert not diagnosis.cache_stale
        assert not diagnosis.has_anomaly

    def test_normalization_error_detected(self):
        fake = Faker(OrderedDict([("de_DE", 3), ("en_US", 2), ("en_PH", 1), ("ja_JP", 5)]))
        fake.zipcode()  # populate the candidate cache

        correct = expected_conditional_distribution(fake, "zipcode")
        assert correct == pytest.approx({"en_US": 2 / 7, "ja_JP": 5 / 7})
        diagnosis = diagnose_source_deviation(fake, "zipcode", [], correct)
        assert diagnosis.normalization_ok
        assert not diagnosis.has_anomaly

        # Normalizing over all configured locales instead of the candidates
        # leaves probability mass unassigned: a normalization error.
        wrong = {"en_US": 2 / 11, "ja_JP": 5 / 11}
        diagnosis = diagnose_source_deviation(fake, "zipcode", [], wrong)
        assert not diagnosis.normalization_ok
        assert diagnosis.expected_probability_sum == pytest.approx(7 / 11)
        assert "normalization error" in diagnosis.explain()
        assert not diagnosis.cache_stale

    def test_coincidental_values_detected(self):
        fake = Faker(
            OrderedDict([("en_US", 1), ("ja_JP", 3)]),
            includes=["tests.mymodule.en_US"],
        )
        fake.seed_instance(SEED)
        values, tags = sample_tagged(fake, "foo", 200)
        expected = expected_conditional_distribution(fake, "foo")

        # Every value is "bar" regardless of source, so the same field value
        # maps to both locales; the deviation signal is a value collision,
        # not a cache or normalization problem.
        diagnosis = diagnose_source_deviation(fake, "foo", list(zip(values, tags)), expected)
        assert diagnosis.value_collisions == {"bar": ["en_US", "ja_JP"]}
        assert "identical field values" in diagnosis.explain()
        assert not diagnosis.cache_stale
        assert diagnosis.normalization_ok

        # Distinct values per source produce no collision signal.
        distinct_samples = [("alpha", "en_US"), ("beta", "ja_JP")]
        diagnosis = diagnose_source_deviation(fake, "foo", distinct_samples, expected)
        assert diagnosis.value_collisions == {}
        assert not diagnosis.has_anomaly
