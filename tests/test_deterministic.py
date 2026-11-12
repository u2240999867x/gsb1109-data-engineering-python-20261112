import pytest

from faker import Faker
from faker.providers import BaseProvider


class OrderAddressProvider(BaseProvider):
    """Custom provider whose address field draws names and building numbers internally."""

    def order_address(self):
        return f"{self.generator.name()}, {self.generator.building_number()}"


class FlakyProvider(BaseProvider):
    """Provider that raises on the first invocation of the day."""

    def flaky_field(self):
        if not getattr(self.generator, "_flaky_failed", False):
            self.generator._flaky_failed = True
            raise RuntimeError("transient provider failure")
        return self.generator.word()


def make_faker(seed=1234, **kwargs):
    fake = Faker(**kwargs)
    fake.seed_instance(seed)
    return fake


def generate_row(fake, key, fields):
    record = fake.for_record(key)
    return {field: getattr(record, field)() for field in fields}


def generate_table(fake, keys, fields):
    return {key: generate_row(fake, key, fields) for key in keys}


class TestDeterministicRecords:
    def test_single_record_matches_full_generation(self):
        keys = [f"ORD-{i:04d}" for i in range(20)]
        fields = ["name", "email", "address"]

        fake = make_faker()
        table = generate_table(fake, keys, fields)

        # Regenerating one order by its business key must reproduce the row
        for key in keys:
            assert generate_row(fake, key, fields) == table[key]

    def test_interleaved_shards_on_same_instance(self):
        keys = [f"ORD-{i:04d}" for i in range(20)]
        fields = ["name", "email", "address"]

        fake = make_faker()
        expected = generate_table(fake, keys, fields)

        shard_a = keys[::2]
        shard_b = keys[1::2]
        interleaved = {}
        for key_a, key_b in zip(shard_a, shard_b):
            interleaved[key_a] = generate_row(fake, key_a, fields)
            interleaved[key_b] = generate_row(fake, key_b, fields)

        assert interleaved == expected

    def test_caller_random_sequence_is_untouched(self):
        fake = make_faker()
        before = [fake.name() for _ in range(3)]
        fake.for_record("ORD-0001").address()
        fake.for_record("ORD-0002").name()
        after = [fake.name() for _ in range(3)]

        uninterrupted = make_faker()
        assert before + after == [uninterrupted.name() for _ in range(6)]

    def test_inserted_column_does_not_shift_other_fields(self):
        keys = [f"ORD-{i:04d}" for i in range(10)]

        fake = make_faker()
        narrow = generate_table(fake, keys, ["name", "email"])
        wide = generate_table(fake, keys, ["name", "address", "email"])

        for key in keys:
            assert wide[key]["name"] == narrow[key]["name"]
            assert wide[key]["email"] == narrow[key]["email"]

    def test_recompute_single_field_only(self):
        keys = [f"ORD-{i:04d}" for i in range(10)]
        fields = ["name", "email", "address"]

        fake = make_faker()
        table = generate_table(fake, keys, fields)

        # Recompute only the address of one order
        key = keys[3]
        assert fake.for_record(key).address() == table[key]["address"]

    def test_retry_after_provider_error(self):
        fake = make_faker()
        fake.add_provider(FlakyProvider)

        before = fake.word()
        with pytest.raises(RuntimeError, match="transient provider failure"):
            fake.for_record("ORD-0001").flaky_field()
        # The instance's own random sequence continues as if nothing happened
        after = fake.word()

        uninterrupted = make_faker()
        assert before == uninterrupted.word()
        assert after == uninterrupted.word()

        # Retrying the same record and field now succeeds deterministically
        retried = fake.for_record("ORD-0001").flaky_field()

        fresh = make_faker()
        fresh.add_provider(FlakyProvider)
        with pytest.raises(RuntimeError):
            fresh.for_record("ORD-0001").flaky_field()
        assert fresh.for_record("ORD-0001").flaky_field() == retried

    def test_nested_draws_belong_to_the_field(self):
        keys = [f"ORD-{i:04d}" for i in range(10)]

        fake = make_faker()
        fake.add_provider(OrderAddressProvider)
        expected = {key: fake.for_record(key).order_address() for key in keys}

        # Interleave with other fields and records; values must not change
        for index, key in enumerate(keys):
            assert fake.for_record(key).order_address() == expected[key]
            fake.for_record(f"ORD-{index:04d}-other").name()
            fake.name()

        # A fresh instance reproduces the same values
        fresh = make_faker()
        fresh.add_provider(OrderAddressProvider)
        for key in keys:
            assert fresh.for_record(key).order_address() == expected[key]

        # The composite field has its own stream, distinct from a plain name
        assert fake.for_record(keys[0]).order_address() != fake.for_record(keys[0]).name()

    def test_explicit_seed_is_reused(self):
        keys = [f"ORD-{i:04d}" for i in range(5)]

        first = generate_table(make_faker(seed=42), keys, ["name", "email"])
        second = generate_table(make_faker(seed=42), keys, ["name", "email"])
        other = generate_table(make_faker(seed=43), keys, ["name", "email"])

        assert first == second
        assert first != other

    def test_arguments_are_part_of_field_identity(self):
        fake = make_faker()
        record = fake.for_record("ORD-0001")

        assert record.pyint(0, 10) == fake.for_record("ORD-0001").pyint(0, 10)
        assert record.pyint(0, 10**9) != record.pyint(0, 10**9 + 1)

    def test_multiple_locales_with_weights(self):
        keys = [f"ORD-{i:04d}" for i in range(20)]
        locale_weights = {"en_US": 2, "ja_JP": 1}

        fake = make_faker(locale=locale_weights)
        expected = {key: fake.for_record(key).name() for key in keys}

        # Interleave records and ordinary draws on the same instance
        for key in reversed(keys):
            assert fake.for_record(key).name() == expected[key]
            fake.company()

        # A fresh instance with the same weights and seed reproduces the table
        fresh = make_faker(locale=locale_weights)
        for key in keys:
            assert fresh.for_record(key).name() == expected[key]

    def test_locale_sub_proxy(self):
        fake = make_faker(locale=["en_US", "ja_JP"])
        key = "ORD-0001"

        expected = fake["ja_JP"].for_record(key).name()
        fake["en_US"].for_record(key).name()
        assert fake["ja_JP"].for_record(key).name() == expected

    def test_non_callable_attribute_rejected(self):
        fake = make_faker()
        with pytest.raises(TypeError, match="non-functions"):
            fake.for_record("ORD-0001").locales()
