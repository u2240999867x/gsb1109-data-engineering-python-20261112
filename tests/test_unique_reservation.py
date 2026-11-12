import threading

import pytest

from faker import Faker
from faker.exceptions import UniqueConflict, UniqueReservationClosed
from faker.unique import (
    Column,
    UniqueReservationManager,
    normalize_business_key,
)


@pytest.fixture(autouse=True)
def clear_domains():
    UniqueReservationManager._clear_domains()
    yield
    UniqueReservationManager._clear_domains()


class SequenceGenerator:
    """Deterministic value source used to assert row-level retries."""

    def __init__(self, values):
        self._values = list(values)
        self.calls = 0

    def __call__(self):
        index = min(self.calls, len(self._values) - 1)
        value = self._values[index]
        self.calls += 1
        return value


def make_columns(**callables):
    return [Column(name=name, function=function) for name, function in callables.items()]


class TestNormalization:
    def test_numeric_forms_share_one_key(self):
        variants = [7, 7.0, "7", "007", " 7 ", "7.0", "007.00"]
        keys = {normalize_business_key(value) for value in variants}
        assert keys == {normalize_business_key(7)}

    def test_zero_variants_and_scientific_notation_share_one_key(self):
        variants = [0, 0.0, "0", "000", "-0", "-0.0", "0.00"]
        keys = {normalize_business_key(value) for value in variants}
        assert keys == {normalize_business_key(0)}
        assert normalize_business_key("1e3") == normalize_business_key(1000)

    def test_plain_strings_keep_text_identity(self):
        assert normalize_business_key("abc") != normalize_business_key("def")
        assert normalize_business_key("abc") == normalize_business_key(" abc ")

    def test_bool_is_distinct_from_integer(self):
        assert normalize_business_key(True) != normalize_business_key(1)
        assert normalize_business_key(False) != normalize_business_key(0)

    def test_none_and_containers_are_supported(self):
        assert normalize_business_key(None) == normalize_business_key(None)
        assert normalize_business_key(["007", 8]) == normalize_business_key([7, "8"])
        assert normalize_business_key({"id": "007"}) == normalize_business_key({"id": 7})


class TestRowReservation:
    def test_whole_row_is_delivered_together(self):
        manager = UniqueReservationManager.domain("customers")
        primary = SequenceGenerator([1001])
        alias = SequenceGenerator(["ACME-1"])
        reservation = manager.reserve_row(make_columns(primary=primary, alias=alias))

        row = reservation.reserve()

        assert row == {"primary": 1001, "alias": "ACME-1"}
        # Pending keys block other reservations but are not delivered yet.
        assert manager.is_occupied(1001)
        assert manager.is_occupied("ACME-1")
        assert not manager.is_committed(1001)
        assert not manager.is_committed("ACME-1")

        delivered = reservation.commit()
        assert delivered == row
        assert manager.is_committed(1001)
        assert manager.is_committed("ACME-1")

    def test_different_providers_feed_one_shared_key_space(self):
        fake = Faker()
        manager = UniqueReservationManager.domain("customers")
        columns = [
            Column(name="id", function=lambda: fake.random_int(min=1, max=500)),
            Column(name="code", function=lambda: str(fake.random_int(min=1, max=500))),
        ]

        seen_keys = set()
        for _ in range(50):
            reservation = manager.reserve_row(columns)
            row = reservation.reserve()
            row_keys = {normalize_business_key(row["id"]), normalize_business_key(row["code"])}
            assert row_keys.isdisjoint(seen_keys)
            reservation.commit()
            seen_keys.update(row_keys)

    def test_intra_row_numeric_normalization_conflict(self):
        # '007' and 7 normalize to the same business key inside one row.
        manager = UniqueReservationManager.domain("customers")
        primary = SequenceGenerator([7, 8])
        alias = SequenceGenerator(["007", "acme"])
        reservation = manager.reserve_row(
            make_columns(primary=primary, alias=alias),
            attempts=10,
        )

        row = reservation.reserve()

        assert row == {"primary": 8, "alias": "acme"}
        # The conflicting attempt regenerated every column of the row.
        assert primary.calls == 2
        assert alias.calls == 2

    def test_single_column_conflict_regenerates_entire_row(self):
        manager = UniqueReservationManager.domain("customers")
        seeded = manager.reserve_row(
            make_columns(primary=SequenceGenerator([1]), alias=SequenceGenerator(["a"])),
        )
        seeded.reserve()
        seeded.commit()

        primary = SequenceGenerator([1, 2])
        alias = SequenceGenerator(["b", "c"])
        reservation = manager.reserve_row(make_columns(primary=primary, alias=alias))

        row = reservation.reserve()
        reservation.commit()

        assert row == {"primary": 2, "alias": "c"}
        assert primary.calls == 2
        assert alias.calls == 2
        assert manager.is_occupied(2)
        assert manager.is_occupied("c")
        assert not manager.is_occupied("b")

    def test_attempt_limit_returns_conflicting_keys(self):
        manager = UniqueReservationManager.domain("customers")
        reservation = manager.reserve_row(
            make_columns(primary=SequenceGenerator([1])),
            attempts=3,
        )
        reservation.reserve()
        reservation.commit()

        retry = manager.reserve_row(
            make_columns(primary=SequenceGenerator([1])),
            attempts=3,
        )
        with pytest.raises(UniqueConflict) as exc_info:
            retry.reserve()

        assert exc_info.value.conflicting_keys == [normalize_business_key(1)]
        # Nothing leaked into the key space after the failed reservation.
        assert manager.committed_keys == frozenset({normalize_business_key(1)})

    def test_release_returns_only_its_own_pending_keys(self):
        manager = UniqueReservationManager.domain("customers")
        committed = manager.reserve_row(make_columns(value=SequenceGenerator([42])))
        committed.reserve()
        committed.commit()

        pending = manager.reserve_row(make_columns(value=SequenceGenerator([7])))
        pending.reserve()
        pending.release()

        assert not manager.is_occupied(7)
        # The other generator's delivered value is untouched.
        assert manager.is_occupied(42)
        with pytest.raises(UniqueReservationClosed):
            pending.reserve()

    def test_reserve_again_releases_previous_pending_keys(self):
        manager = UniqueReservationManager.domain("customers")
        reservation = manager.reserve_row(make_columns(value=SequenceGenerator([1, 2])))
        reservation.reserve()
        assert manager.is_occupied(1)

        reservation.reserve()
        assert not manager.is_occupied(1)
        assert manager.is_occupied(2)
        reservation.commit()
        assert manager.is_occupied(2)

    def test_context_manager_commits_on_success_and_releases_on_error(self):
        manager = UniqueReservationManager.domain("customers")
        with manager.reserve_row(make_columns(value=SequenceGenerator([1]))) as reservation:
            reservation.reserve()
        assert manager.is_occupied(1)

        with pytest.raises(RuntimeError):
            with manager.reserve_row(make_columns(value=SequenceGenerator([2]))) as reservation:
                reservation.reserve()
                raise RuntimeError("boom")
        assert not manager.is_occupied(2)

    def test_override_value_conflicts_like_generated_value(self):
        manager = UniqueReservationManager.domain("customers")
        seeded = manager.reserve_row(make_columns(primary=SequenceGenerator([9])))
        seeded.reserve()
        seeded.commit()

        reservation = manager.reserve_row(
            make_columns(primary=SequenceGenerator([9]), alias=SequenceGenerator(["x"])),
            attempts=2,
        )
        with pytest.raises(UniqueConflict):
            reservation.reserve(primary=9)
        assert not manager.is_occupied("x")

    def test_unknown_override_is_rejected(self):
        manager = UniqueReservationManager.domain("customers")
        reservation = manager.reserve_row(make_columns(primary=SequenceGenerator([1])))
        with pytest.raises(TypeError):
            reservation.reserve(unknown=1)


class TestInterleavedGenerators:
    def test_two_generators_share_named_domain(self):
        manager = UniqueReservationManager.domain("customers")
        fake_a = Faker()
        fake_b = Faker()
        columns_a = [Column(name="id", function=lambda: fake_a.random_int(min=1, max=100))]
        columns_b = [Column(name="id", function=lambda: fake_b.random_int(min=1, max=100))]

        delivered = set()
        reservation_a = manager.reserve_row(columns_a)
        row_a = reservation_a.reserve()
        assert normalize_business_key(row_a["id"]) not in delivered

        reservation_b = manager.reserve_row(columns_b)
        for _ in range(20):
            row_b = reservation_b.reserve()
            if normalize_business_key(row_b["id"]) != normalize_business_key(row_a["id"]):
                break
        reservation_b.commit()
        delivered.add(normalize_business_key(row_b["id"]))

        reservation_a.commit()
        delivered.add(normalize_business_key(row_a["id"]))

        assert len(delivered) == 2

    def test_release_does_not_clear_other_generator_delivered_values(self):
        manager = UniqueReservationManager.domain("customers")
        generator_b = manager.reserve_row(make_columns(value=SequenceGenerator(["kept"])))
        generator_b.reserve()
        generator_b.commit()

        generator_a = manager.reserve_row(make_columns(value=SequenceGenerator(["dropped"])))
        generator_a.reserve()
        generator_a.release()

        assert manager.is_occupied("kept")
        assert not manager.is_occupied("dropped")

    def test_concurrent_reservations_never_double_book(self):
        manager = UniqueReservationManager.domain("customers")
        fake = Faker()
        columns = [Column(name="id", function=lambda: fake.random_int(min=1, max=40))]
        committed = set()
        committed_lock = threading.Lock()
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            for _ in range(8):
                reservation = manager.reserve_row(columns)
                row = reservation.reserve()
                reservation.commit()
                with committed_lock:
                    key = normalize_business_key(row["id"])
                    assert key not in committed
                    committed.add(key)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(committed) == 32


class TestRebuild:
    def test_rebuild_from_committed_rows_continues_next_batch(self):
        manager = UniqueReservationManager.domain("customers")
        columns = make_columns(
            primary=SequenceGenerator([1]),
            alias=SequenceGenerator(["a"]),
        )
        historical_rows = [
            {"primary": 1, "alias": "001"},
            {"primary": 2, "alias": "x2"},
        ]

        count = manager.rebuild(historical_rows, columns)

        assert count == 2
        assert manager.is_occupied(1)
        assert manager.is_occupied("001")
        assert manager.is_occupied(2)

        reservation = manager.reserve_row(
            make_columns(
                primary=SequenceGenerator([1, 2, 3]),
                alias=SequenceGenerator(["001", "x2", "z3"]),
            ),
            attempts=10,
        )
        row = reservation.reserve()
        assert row == {"primary": 3, "alias": "z3"}

    def test_rebuild_rejects_rows_missing_columns(self):
        manager = UniqueReservationManager.domain("customers")
        columns = make_columns(primary=SequenceGenerator([1]))
        with pytest.raises(KeyError):
            manager.rebuild([{"alias": "x"}], columns)

    def test_rebuilt_domain_is_reusable_across_faker_instances(self):
        fake = Faker()
        columns = [Column(name="id", function=lambda: fake.random_int(min=1, max=30))]
        historical = [{"id": value} for value in range(1, 11)]

        manager = UniqueReservationManager.domain("customers")
        manager.rebuild(historical, columns)

        seen = set(manager.committed_keys)
        for _ in range(10):
            reservation = manager.reserve_row(columns)
            row = reservation.reserve()
            assert normalize_business_key(row["id"]) not in seen
            seen.add(normalize_business_key(row["id"]))
            reservation.commit()

    def test_rebuild_keeps_live_pending_reservation(self):
        manager = UniqueReservationManager.domain("customers")
        pending = manager.reserve_row(make_columns(value=SequenceGenerator([99])))
        pending.reserve()

        manager.rebuild([{"value": 1}], make_columns(value=SequenceGenerator([1])))

        assert manager.is_occupied(99)
        assert not manager.is_committed(99)
        pending.commit()
        assert manager.is_committed(99)
        assert manager.is_committed(1)


class TestFakerFacade:
    def test_reserve_row_through_faker(self):
        fake = Faker()
        columns = [
            fake.reserve.column("id", "random_int", min=1, max=100),
            fake.reserve.column("code", "random_int", min=1, max=100),
        ]
        with fake.reserve.row("customers", columns) as reservation:
            row = reservation.reserve()
            assert set(row) == {"id", "code"}

        assert fake.reserve.domain("customers").is_occupied(row["id"])

    def test_rebuild_through_faker(self):
        fake = Faker()
        columns = [fake.reserve.column("id", "random_int", min=1, max=10)]
        count = fake.reserve.rebuild("customers", [{"id": 1}, {"id": 2}], columns)
        assert count == 2
        assert fake.reserve.domain("customers").is_occupied("2")

    def test_column_rejects_non_callable_attribute(self):
        fake = Faker()
        with pytest.raises(TypeError):
            fake.reserve.column("id", "locales")
