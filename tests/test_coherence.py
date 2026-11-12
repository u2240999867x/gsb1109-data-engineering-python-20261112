import copy
import pickle

from collections import OrderedDict

import pytest

from faker import Faker
from faker.exceptions import LocaleCoherenceException
from faker.providers import DynamicProvider


class TestCoherentRow:
    def test_all_fields_share_one_locale(self):
        fake = Faker(["en_US", "de_DE", "ja_JP", "fr_FR"])
        row = fake.coherent_row(["name", "street_address", "phone_number"])

        assert set(row.values) == {"name", "street_address", "phone_number"}
        assert all(prov.factory_locale == row.locale for prov in row.fields.values())
        for field_name in row.values:
            assert isinstance(row[field_name], str) and row[field_name]

    def test_provenance_reports_factory_and_resolved_locale(self):
        # es_CA has a localized person provider but no address/phone provider,
        # so those fields fall back to the en_US implementation while the
        # selected factory remains es_CA.
        fake = Faker(["es_CA"])
        row = fake.coherent_row(["name", "street_address", "phone_number"])

        assert row.locale == "es_CA"
        assert row.fields["name"].factory_locale == "es_CA"
        assert row.fields["name"].resolved_locale == "es_CA"
        assert row.fields["street_address"].factory_locale == "es_CA"
        assert row.fields["street_address"].resolved_locale == "en_US"
        assert row.fields["phone_number"].resolved_locale == "en_US"

    def test_provenance_reports_actual_implementation_source(self):
        # ``city_name`` is genuinely overridden in the de_DE address provider,
        # so the implementation source must point at de_DE rather than the base
        # address provider (which reports no locale).
        fake = Faker(["de_DE"])
        row = fake.coherent_row(["city_name", "city_with_postcode"])

        assert row.fields["city_name"].implementation_locale == "de_DE"
        assert row.fields["city_name"].implementing_class == "Provider"
        # Methods only defined on the base provider report a neutral source.
        base_row = fake.coherent_row(["name"])
        assert base_row.fields["name"].implementation_locale is None

    def test_method_unique_to_one_locale_forces_whole_row(self):
        fake = Faker(["en_US", "en_PH"])
        row = fake.coherent_row(["name", "luzon_province"])
        assert row.locale == "en_PH"
        assert all(prov.factory_locale == "en_PH" for prov in row.fields.values())

    def test_source_layer_priority_prefers_highest_complete_layer(self):
        fake = Faker(["en_US", "ja_JP"])
        fake.seed_instance(7)
        for _ in range(25):
            row = fake.coherent_row(
                ["name", "street_address"],
                sources=[["ja_JP"], ["en_US"]],
            )
            assert row.locale == "ja_JP"

    def test_layer_without_full_row_falls_to_next_layer(self):
        # ja_JP cannot supply luzon_province, so the row must drop to the layer
        # containing en_PH even though ja_JP is requested first.
        fake = Faker(["ja_JP", "en_PH"])
        row = fake.coherent_row(
            ["name", "luzon_province"],
            sources=[["ja_JP"], ["en_PH"]],
        )
        assert row.locale == "en_PH"

    def test_locale_weights_apply_within_selected_layer(self):
        fake = Faker(OrderedDict([("en_US", 1), ("de_DE", 9)]))
        fake.seed_instance(7)
        counts = {code: 0 for code in ("en_US", "de_DE")}
        for _ in range(300):
            counts[fake.coherent_row(["name"], sources=[["en_US", "de_DE"]]).locale] += 1

        assert counts["de_DE"] > counts["en_US"]

    def test_per_field_source_orders(self):
        fake = Faker(["en_US", "de_DE"])
        sources = {
            "name": [["de_DE"], ["en_US"]],
            "street_address": [["en_US"], ["de_DE"]],
        }
        locales = {fake.coherent_row(["name", "street_address"], sources=sources).locale for _ in range(20)}
        assert locales <= {"en_US", "de_DE"}

    def test_partial_per_field_order_leaves_others_unrestricted(self):
        fake = Faker(["en_US", "de_DE"])
        # Only ``name`` has an order; ``phone_number`` accepts any locale.
        rows = fake.coherent_rows(
            ["name", "phone_number"],
            10,
            sources={"name": [["de_DE"], ["en_US"]]},
        )
        assert all(row.locale in {"en_US", "de_DE"} for row in rows)

    def test_grouped_layer_codes(self):
        fake = Faker(["en_US", "de_DE", "ja_JP"])
        row = fake.coherent_row(["name"], sources=[["en_US", "de_DE"], ["ja_JP"]])
        assert row.locale in {"en_US", "de_DE"}

    def test_conflict_reports_minimal_field_groups(self):
        fake = Faker(["en_US", "en_PH", "ja_JP"])
        # luzon_province exists only in en_PH, kana only in ja_JP; ``name`` is
        # available everywhere and must not appear in the minimal conflict set.
        with pytest.raises(LocaleCoherenceException) as exc:
            fake.coherent_row(["name", "luzon_province", "kana"])

        assert exc.value.conflicts == [["luzon_province", "kana"]]

    def test_failed_resolution_does_not_consume_randomness(self):
        fake = Faker(["en_PH", "ja_JP"])
        fake.seed_instance(99)
        state = fake.factories[0].random.getstate()

        for _ in range(5):
            with pytest.raises(LocaleCoherenceException):
                fake.coherent_row(["luzon_province", "kana"])

        assert fake.factories[0].random.getstate() == state

    def test_next_row_after_conflict_keeps_random_sequence(self):
        fake = Faker(["en_PH", "ja_JP"])
        fake.seed_instance(123)
        reference = [fake.coherent_row(["name"]).values["name"] for _ in range(5)]

        fake.seed_instance(123)
        produced = []
        for _ in range(5):
            with pytest.raises(LocaleCoherenceException):
                fake.coherent_row(["luzon_province", "kana"])
            produced.append(fake.coherent_row(["name"]).values["name"])

        assert produced == reference

    def test_fallback_mode_skips_exception(self):
        fake = Faker(["en_PH", "ja_JP"])
        row = fake.coherent_row(
            ["luzon_province", "kana"],
            strict=False,
            fallback=lambda method: getattr(fake, method)(),
        )
        assert row.locale == ""
        assert row.fields == {}
        assert set(row.values) == {"luzon_province", "kana"}

    def test_runtime_registration_benefits_next_row(self):
        fake = Faker(["de_DE"])
        assert not hasattr(fake, "custom_metric")

        fake.add_provider(DynamicProvider(provider_name="custom_metric", elements=["alpha", "beta"]))

        row = fake.coherent_row(["name", "custom_metric"])
        assert row.locale == "de_DE"
        assert row.values["custom_metric"] in {"alpha", "beta"}

    def test_runtime_registration_on_each_factory_is_scanned_live(self):
        fake = Faker(["en_US", "de_DE"])
        for factory in fake.factories:
            factory.add_provider(DynamicProvider(provider_name="custom_metric", elements=["alpha"]))

        rows = fake.coherent_rows(["name", "custom_metric"], 4)
        assert all(row.values["custom_metric"] == "alpha" for row in rows)

    def test_locale_declaration_changes_row_candidates(self):
        class RestrictedProvider(DynamicProvider):
            __locales__ = ("de_DE",)

        fake = Faker(["en_US", "de_DE"])
        for factory in fake.factories:
            factory.add_provider(RestrictedProvider(provider_name="regional_only", elements=["x"]))

        row = fake.coherent_row(["name", "regional_only"])
        assert row.locale == "de_DE"

    def test_empty_locale_declaration_excludes_provider_from_rows(self):
        class ExcludedProvider(DynamicProvider):
            __locales__ = ()

        fake = Faker(["en_US", "de_DE"])
        for factory in fake.factories:
            factory.add_provider(ExcludedProvider(provider_name="never_coherent", elements=["y"]))

        with pytest.raises(LocaleCoherenceException) as exc:
            fake.coherent_row(["name", "never_coherent"])
        assert exc.value.conflicts == [["name", "never_coherent"]]

    def test_coherent_rows_generates_requested_count(self):
        fake = Faker(["en_US", "de_DE"])
        rows = fake.coherent_rows(["name", "phone_number"], 6)
        assert len(rows) == 6
        assert all(len(row.values) == 2 for row in rows)

    def test_duplicate_fields_deduplicated(self):
        row = Faker(["en_US"]).coherent_row(["name", "name"])
        assert list(row.values) == ["name"]

    def test_single_locale_proxy(self):
        row = Faker("ja_JP").coherent_row(["name", "phone_number"])
        assert row.locale == "ja_JP"

    def test_unknown_method_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="not_a_method_xyz"):
            Faker(["en_US"]).coherent_row(["not_a_method_xyz"])

    def test_unknown_source_locale_rejected(self):
        with pytest.raises(ValueError, match="Unknown locale source"):
            Faker(["en_US"]).coherent_row(["name"], sources=[["zz_ZZ"]])

    def test_empty_fields_rejected(self):
        with pytest.raises(ValueError, match="At least one field"):
            Faker(["en_US"]).coherent_row([])

    def test_deepcopy_and_pickle(self):
        fake = Faker(["en_US", "de_DE"])
        fake.coherent_row(["name"])

        cloned = copy.deepcopy(fake)
        assert cloned.coherent_row(["name"]).locale in {"en_US", "de_DE"}

        restored = pickle.loads(pickle.dumps(fake))
        assert restored.coherent_row(["name"]).locale in {"en_US", "de_DE"}

    def test_seeded_rows_are_deterministic(self):
        def run():
            fake = Faker(OrderedDict([("en_US", 2), ("de_DE", 3)]))
            fake.seed_instance(2048)
            return [fake.coherent_row(["name", "street_address"]).locale for _ in range(15)]

        assert run() == run()
