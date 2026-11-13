import pickle

from random import Random

import pytest

from faker.utils.sampling import WeightedSamplingFrame


class StubRandom:
    """Minimal stand-in for ``random.Random`` returning scripted randrange values."""

    def __init__(self, values):
        self.values = list(values)
        self.requested = []

    def randrange(self, stop):
        self.requested.append(stop)
        return self.values.pop(0)

    def getstate(self):
        return ("stub", tuple(self.values))

    def setstate(self, state):
        self.values = list(state[1])


def test_records_merge_by_business_key_not_value():
    frame = WeightedSamplingFrame(
        [
            ("key-1", "same-value", 2),
            ("key-2", "same-value", 3),  # same display value, different key: two entries
            ("key-1", "same-value", 5),  # same key spread over two records: weights merge
        ]
    )
    assert len(frame) == 2
    assert frame.weight_of("key-1") == 7
    assert frame.weight_of("key-2") == 3
    assert frame.value_of("key-1") == "same-value"


def test_draw_removes_whole_merged_key():
    frame = WeightedSamplingFrame([("a", 1), ("b", 2), ("b", 3)], random=StubRandom([0, 0]))
    assert frame.draw() == "a"
    assert frame.draw() == "b"
    assert len(frame) == 0
    with pytest.raises(ValueError):
        frame.draw()


def test_huge_integer_weights_keep_exact_proportions():
    records = [("big", 2**80), ("small-a", 1), ("small-b", 1)]
    # canonical key order: big < small-a < small-b; total weight 2**80 + 2
    cases = [
        (2**80 - 1, "big"),  # last ticket of the huge key
        (2**80, "small-a"),  # first ticket past the huge key
        (2**80 + 1, "small-b"),  # very last ticket
    ]
    for ticket, expected in cases:
        stub = StubRandom([ticket])
        frame = WeightedSamplingFrame(records, random=stub)
        assert frame.draw() == expected
        assert stub.requested == [2**80 + 2]


def test_unit_weights_still_get_proportional_chances():
    records = [("big", 2**80), ("small-a", 1), ("small-b", 1)]
    counts = {"small-a": 0, "small-b": 0}
    random = Random(20261112)
    for _ in range(2000):
        frame = WeightedSamplingFrame(records, random=random)
        frame.draw()  # practically always "big"; then the two unit weights remain
        counts[frame.draw()] += 1
    assert 800 < counts["small-a"] < 1200
    assert 800 < counts["small-b"] < 1200


def test_input_order_does_not_change_sequence():
    records = [(f"key-{i}", f"value-{i}", i * 3 + 1) for i in range(15)]
    forward = WeightedSamplingFrame(records, random=Random(99))
    shuffled = WeightedSamplingFrame(list(reversed(records)), random=Random(99))
    assert forward.sample(15) == shuffled.sample(15)


def test_resume_matches_one_shot_across_processes():
    records = [(f"key-{i}", f"value-{i}", i * 7 + 1) for i in range(20)]

    one_shot = WeightedSamplingFrame(records, random=Random(1234))
    expected = one_shot.sample(20)

    first = WeightedSamplingFrame(records, random=Random(1234))
    head = first.sample(7)
    # simulate handing progress to another process
    state = pickle.loads(pickle.dumps(first.save_state()))

    resumed = WeightedSamplingFrame(list(reversed(records)), random=Random())
    resumed.restore(state)
    tail = resumed.sample(13)

    assert head + tail == expected
    assert resumed.drawn == tuple(expected)


def test_resume_classmethod():
    records = [("a", 1), ("b", 2), ("c", 3)]
    first = WeightedSamplingFrame(records, random=Random(7))
    first.draw()
    state = first.save_state()
    resumed = WeightedSamplingFrame.resume(records, state, random=Random())
    assert resumed.drawn == first.drawn
    assert resumed.remaining == 2


def test_resume_refuses_changed_frame():
    records = [("a", 1), ("b", 2), ("c", 3)]
    first = WeightedSamplingFrame(records, random=Random(7))
    first.draw()
    state = first.save_state()

    changed_weight = WeightedSamplingFrame([("a", 1), ("b", 2), ("c", 4)])
    with pytest.raises(ValueError):
        changed_weight.restore(state)

    missing_key = WeightedSamplingFrame([("a", 1), ("b", 2)])
    with pytest.raises(ValueError):
        missing_key.restore(state)

    extra_key = WeightedSamplingFrame([("a", 1), ("b", 2), ("c", 3), ("d", 1)])
    with pytest.raises(ValueError):
        extra_key.restore(state)


def test_cannot_add_records_once_started():
    frame = WeightedSamplingFrame([("a", 1), ("b", 1)], random=Random(1))
    frame.draw()
    with pytest.raises(ValueError):
        frame.add([("c", 1)])


def test_zero_weight_keys_are_never_drawn():
    frame = WeightedSamplingFrame([("dead", 0), ("alive", 1)], random=Random(5))
    assert frame.draw() == "alive"
    with pytest.raises(ValueError):
        frame.draw()


def test_invalid_weights_rejected():
    for bad_weight in (1.5, "2", True, -1):
        with pytest.raises(ValueError):
            WeightedSamplingFrame([("a", bad_weight)])


def test_sample_validates_size():
    frame = WeightedSamplingFrame([("a", 1), ("b", 1)], random=Random(3))
    with pytest.raises(ValueError):
        frame.sample(3)
    with pytest.raises(ValueError):
        frame.sample(-1)
    assert len(frame.sample(2)) == 2
