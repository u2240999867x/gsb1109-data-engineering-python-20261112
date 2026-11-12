"""Engine for generating coherent rows whose fields share one locale source.

A regular multi-locale :class:`~faker.Faker` proxy picks a factory independently
for every provider method, so the fields of a single row (for example a name, a
street and a phone number) can be sourced from different locale datasets.

The engine in this module selects, for a whole set of fields, a single locale
factory that can supply every field. Each field may declare an ordered list of
allowed source layers. A locale's row priority is the worst (largest) layer
position at which any single field accepts it: the engine first looks for
locales able to supply the whole row at the highest possible shared layer, then
picks one within that layer using the original locale weights.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Optional, Sequence, Union

from .exceptions import LocaleCoherenceException
from .utils.distribution import choices_distribution

# A source layer is a locale code or a group of locale codes at one rank.
LayerSpec = Union[str, Sequence[str]]
# Per-field or row-wide ordered collection of layers.
SourceOrder = Sequence[LayerSpec]
SourcesSpec = Optional[Union[SourceOrder, dict[str, SourceOrder]]]


@dataclass(frozen=True)
class FieldProvenance:
    """Traceability information for a single field of a coherent row."""

    method: str
    #: Locale of the factory selected for the whole row.
    factory_locale: str
    #: Value of ``provider.__lang__``; differs from ``factory_locale`` when the
    #: locale was unavailable and the provider fell back to another locale.
    resolved_locale: Optional[str]
    #: Locale encoded in the module that actually implements the method, found
    #: by walking the provider MRO. ``None`` for locale-neutral base providers.
    implementation_locale: Optional[str]
    #: Provider name (``provider.__provider__``) owning the active method.
    provider: str
    provider_class: str
    implementing_class: str


@dataclass
class CoherentRow:
    """The values of one coherent row together with their provenance."""

    values: dict[str, Any]
    locale: str
    fields: dict[str, FieldProvenance] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class LocaleEntry:
    locale: str
    generator: Any
    weight: Optional[float]


@dataclass(frozen=True)
class Resolution:
    entry: LocaleEntry
    layer: int
    field_sources: dict[str, dict[str, Any]]


def _underlying_generator(factory: Any) -> Any:
    """Return the :class:`~faker.generator.Generator` behind a proxy/factory."""
    factories = getattr(factory, "factories", None)
    if factories:
        return factories[0]
    return factory


def _provider_for(generator: Any, method_name: str) -> Optional[Any]:
    """Return the provider instance currently serving ``method_name``."""
    for provider in generator.providers:
        attr = getattr(provider, method_name, None)
        if callable(attr):
            return provider
    return None


def _declared_locales(provider: Any, factory_locale: str) -> tuple[str, ...]:
    """Locale sources a provider explicitly accepts for coherent rows."""
    declared = getattr(provider, "__locales__", None)
    if declared is None:
        return (factory_locale,)
    return tuple(declared)


def _implementation_origin(provider: Any, method_name: str) -> tuple[Optional[type], Optional[str]]:
    """Find the class actually defining ``method_name`` and its locale.

    Walks the MRO so that methods merely inherited from another locale provider
    (for example ``fr_QC`` inheriting ``fr_CA``) are reported against their real
    implementation source.
    """
    owner: Optional[type] = None
    for klass in type(provider).__mro__:
        descriptor = klass.__dict__.get(method_name)
        if callable(descriptor):
            owner = klass
            break
    if owner is None:
        return None, None

    module_parts = owner.__module__.split(".")
    # Localized provider modules look like ``faker.providers.person.es_ES``
    # (4 parts); locale-neutral base providers have 3 parts.
    implementation_locale = module_parts[-1] if len(module_parts) >= 4 else None
    return owner, implementation_locale


def _has_method_anywhere(entries: Sequence[LocaleEntry], method_name: str) -> bool:
    return any(hasattr(_underlying_generator(entry.generator), method_name) for entry in entries)


def normalize_layers(
    sources: SourcesSpec,
    known_locales: Sequence[str],
) -> Optional[Union[list[tuple[str, ...]], dict[str, list[tuple[str, ...]]]]]:
    """Validate and normalize source-order declarations.

    Returns ``None`` when no preference was given (all locales form one layer),
    a list of global layers, or a mapping of field name to its own layers.
    """
    if sources is None:
        return None

    known = set(known_locales)

    def normalize_one(order: SourceOrder) -> list[tuple[str, ...]]:
        normalized: list[tuple[str, ...]] = []
        for layer in order:
            codes: tuple[str, ...]
            if isinstance(layer, str):
                codes = (layer.replace("-", "_"),)
            else:
                codes = tuple(code.replace("-", "_") for code in layer)
            unknown = [code for code in codes if code not in known]
            if unknown:
                raise ValueError(f"Unknown locale source(s) {unknown}; known locales: {list(known_locales)}")
            normalized.append(codes)
        if not normalized:
            raise ValueError("At least one source layer must be provided")
        return normalized

    if isinstance(sources, dict):
        return {name: normalize_one(order) for name, order in sources.items()}
    return normalize_one(sources)


class CoherenceEngine:
    """Resolve and generate coherent rows over a fixed set of locale factories."""

    def __init__(self, entries: Sequence[LocaleEntry]) -> None:
        if not entries:
            raise ValueError("At least one locale factory is required for coherent row generation")
        self._entries = list(entries)

    @property
    def random(self) -> Any:
        return self._entries[0].generator.random

    def _field_sources(self, method_name: str) -> dict[str, Any]:
        """Map every locale able to serve ``method_name`` to entry and provider.

        Computed live from the registered providers, so a provider added at
        runtime benefits the very next row.
        """
        sources: dict[str, Any] = {}
        for entry in self._entries:
            generator = _underlying_generator(entry.generator)
            provider = _provider_for(generator, method_name)
            if provider is None:
                continue
            if entry.locale not in _declared_locales(provider, entry.locale):
                # The provider explicitly refuses to serve this locale in a
                # coherent row, which changes the row-level candidate set.
                continue
            sources[entry.locale] = (entry, provider)
        return sources

    def _ranks(
        self,
        field_name: str,
        normalized: Optional[Union[list[tuple[str, ...]], dict[str, list[tuple[str, ...]]]]],
    ) -> dict[str, int]:
        """Smallest layer index at which each locale is accepted by a field."""
        if normalized is None:
            return {entry.locale: 0 for entry in self._entries}

        if isinstance(normalized, dict):
            # A field without an explicit order accepts every known locale on
            # the first layer.
            layers = normalized.get(field_name)
            if layers is None:
                return {entry.locale: 0 for entry in self._entries}
        else:
            layers = normalized
        ranks: dict[str, int] = {}
        for index, layer in enumerate(layers):
            for code in layer:
                ranks.setdefault(code, index)
        return ranks

    def _resolve(
        self,
        fields: Sequence[str],
        normalized: Optional[Union[list[tuple[str, ...]], dict[str, list[tuple[str, ...]]]]],
    ) -> Resolution:
        field_sources = {name: self._field_sources(name) for name in fields}

        missing = [name for name in fields if not _has_method_anywhere(self._entries, name)]
        if missing:
            raise AttributeError(f"No generator object has attribute {missing[0]!r}")

        field_ranks = {name: self._ranks(name, normalized) for name in fields}

        # A locale is a row candidate only when every field can and will accept
        # it. Its row layer is the worst per-field acceptance rank.
        scored: dict[int, list[LocaleEntry]] = {}
        for entry in self._entries:
            ranks: list[int] = []
            for name in fields:
                if entry.locale not in field_sources[name]:
                    ranks = []
                    break
                rank = field_ranks[name].get(entry.locale)
                if rank is None:
                    ranks = []
                    break
                ranks.append(rank)
            if ranks:
                scored.setdefault(max(ranks), []).append(entry)

        if not scored:
            conflicts = self._minimal_conflicts(fields, field_sources, field_ranks)
            raise LocaleCoherenceException(
                "No locale source can supply all requested fields as one coherent row; "
                f"minimal conflicting field groups: {conflicts}",
                conflicts,
            )

        best_layer = min(scored)
        return Resolution(entry=self._choose_entry(scored[best_layer]), layer=best_layer, field_sources=field_sources)

    @staticmethod
    def _minimal_conflicts(
        fields: Sequence[str],
        field_sources: dict[str, dict[str, Any]],
        field_ranks: dict[str, dict[str, int]],
    ) -> list[list[str]]:
        """Return inclusion-minimal field groups without a common locale."""

        def accepted(name: str) -> set[str]:
            return set(field_sources[name]) & set(field_ranks[name])

        conflicts: list[list[str]] = []
        for size in range(2, len(fields) + 1):
            for combo in combinations(range(len(fields)), size):
                group = [fields[index] for index in combo]
                if set.intersection(*(accepted(name) for name in group)):
                    continue
                if any(set(existing).issubset(set(group)) for existing in conflicts):
                    continue
                conflicts.append(group)
        if not conflicts:
            conflicts = [[name] for name in fields if not accepted(name)]
        return conflicts

    def _choose_entry(self, candidates: list[LocaleEntry]) -> LocaleEntry:
        if len(candidates) == 1:
            return candidates[0]
        if any(entry.weight is not None for entry in candidates):
            weights = [float(entry.weight) if entry.weight is not None else 0.0 for entry in candidates]
            return choices_distribution(candidates, weights, self.random, length=1)[0]
        return self.random.choice(candidates)

    def generate(
        self,
        fields: Sequence[str],
        normalized: Optional[Union[list[tuple[str, ...]], dict[str, list[tuple[str, ...]]]]],
        strict: bool = True,
        fallback: Optional[Callable[[str], Any]] = None,
    ) -> CoherentRow:
        unique_fields = list(dict.fromkeys(fields))
        if not unique_fields:
            raise ValueError("At least one field must be requested for a coherent row")

        try:
            resolution = self._resolve(unique_fields, normalized)
        except LocaleCoherenceException:
            if strict or fallback is None:
                raise
            fallback_values = {name: fallback(name) for name in unique_fields}
            return CoherentRow(values=fallback_values, locale="", fields={})

        # No random draw happens before this point, so a failed resolution never
        # consumes the random choice of a subsequent row.
        entry = resolution.entry
        generator = _underlying_generator(entry.generator)

        values: dict[str, Any] = {}
        provenance: dict[str, FieldProvenance] = {}
        for name in unique_fields:
            values[name] = getattr(generator, name)()
            provider = resolution.field_sources[name][entry.locale][1]
            owner, implementation_locale = _implementation_origin(provider, name)
            provenance[name] = FieldProvenance(
                method=name,
                factory_locale=entry.locale,
                resolved_locale=getattr(provider, "__lang__", None),
                implementation_locale=implementation_locale,
                provider=getattr(provider, "__provider__", ""),
                provider_class=type(provider).__name__,
                implementing_class=owner.__name__ if owner is not None else type(provider).__name__,
            )

        return CoherentRow(values=values, locale=entry.locale, fields=provenance)
