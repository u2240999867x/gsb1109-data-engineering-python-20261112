from datetime import timedelta

import pytest

from pyhocon.period_parser import parse_period
from pyhocon.period_serializer import timedelta_to_hocon

try:
    from dateutil.relativedelta import relativedelta as period
except ImportError:
    period = timedelta


@pytest.mark.parametrize('data_set', [
    ('1 minutes', period(minutes=1)),
    ('1minutes', period(minutes=1)),
    ('2 minute', period(minutes=2)),
    ('3 m', period(minutes=3)),
    ('3m', period(minutes=3)),

    ('4 seconds', period(seconds=4)),
    ('5 second', period(seconds=5)),
    ('6 s', period(seconds=6)),

    ('7 hours', period(hours=7)),
    ('8 hour', period(hours=8)),
    ('9 h', period(hours=9)),

    ('10 weeks', period(weeks=10)),
    ('11 week', period(weeks=11)),
    ('12 w', period(weeks=12)),

    ('10 days', period(days=10)),
    ('11 day', period(days=11)),
    ('12 d', period(days=12)),

    ('110 microseconds', period(microseconds=110)),
    ('111 microsecond', period(microseconds=111)),
    ('112 micros', period(microseconds=112)),
    ('113 micro', period(microseconds=113)),
    ('114 us', period(microseconds=114)),

    ('110 milliseconds', timedelta(milliseconds=110)),
    ('111 millisecond', timedelta(milliseconds=111)),
    ('112 millis', timedelta(milliseconds=112)),
    ('113 milli', timedelta(milliseconds=113)),
    ('114 ms', timedelta(milliseconds=114)),

    ('110 nanoseconds', period(microseconds=0)),
    ('11000 nanoseconds', period(microseconds=11)),
    ('1110000 nanosecond', period(microseconds=1110)),
    ('1120000 nanos', period(microseconds=1120)),
    ('1130000 nano', period(microseconds=1130)),
    ('1140000 ns', period(microseconds=1140)),
])
def test_parse_string_with_duration(data_set):
    config = parse_period(data_set[0])

    assert config == data_set[1]


try:
    from dateutil.relativedelta import relativedelta

    @pytest.mark.parametrize('data_set', [
        ('1 months', relativedelta(months=1)),
        ('1months', relativedelta(months=1)),
        ('2 month', relativedelta(months=2)),
        ('3 mo', relativedelta(months=3)),
        ('3mo', relativedelta(months=3)),

        ('1 years', relativedelta(years=1)),
        ('1years', relativedelta(years=1)),
        ('2 year', relativedelta(years=2)),
        ('3 y', relativedelta(years=3)),
        ('3y', relativedelta(years=3)),

    ])
    def test_parse_string_with_duration_optional_units(data_set):
        parsed = parse_period(data_set[0])

        assert parsed == data_set[1]

    def test_format_relativedelta():

        for time_delta, expected_result in ((relativedelta(seconds=0), '0 seconds'),
                                            (relativedelta(hours=0), '0 seconds'),
                                            (relativedelta(days=5), '5 days'),
                                            (relativedelta(weeks=3), '21 days'),
                                            (relativedelta(hours=2), '2 hours'),
                                            (relativedelta(minutes=43), '43 minutes'),):
            assert expected_result == timedelta_to_hocon(time_delta)
except ImportError:
    pass


def test_format_time_delta():
    for time_delta, expected_result in ((timedelta(days=0), '0 seconds'),
                                        (timedelta(days=5), '5 days'),
                                        (timedelta(seconds=51), '51 seconds'),
                                        (timedelta(microseconds=786), '786 microseconds')):
        assert expected_result == timedelta_to_hocon(time_delta)


from datetime import datetime, timezone
from decimal import Decimal

from pyhocon.exact_period import (Period, locate_partition, parse_exact_period,
                                  partition_bounds)
from pyhocon.period_serializer import period_to_hocon

ANCHOR = datetime(2021, 1, 30, tzinfo=timezone.utc)  # non-leap year, UTC


class TestExactPeriodParsing(object):
    def test_signed_decimal_fixed_units(self):
        assert parse_exact_period('0.0000001s') == parse_exact_period('0.0000001 seconds')
        assert parse_exact_period('-1.5 hours').components[0].value == Decimal('-1.5')
        assert parse_exact_period('+2 days').components[0].value == Decimal('2')
        assert parse_exact_period('0.0000001s').fixed_seconds() == Decimal('0.0000001')

    def test_whole_months_and_years(self):
        period = parse_exact_period('1 month')
        assert period.has_calendar
        assert parse_exact_period('2 years').components[0].months() == 24
        with pytest.raises(ValueError):
            parse_exact_period('1.5 months')

    def test_compound_items_keep_written_order(self):
        period = parse_exact_period('1 month 1 day')
        assert [component.unit for component in period.components] == ['months', 'days']
        assert parse_exact_period('1 month, 1 day') == period

    def test_read_option_on_parse_period(self):
        assert isinstance(parse_period('1 month 1 day', exact=True), Period)


class TestExactPeriodApplication(object):
    def test_month_then_day_from_jan_30(self):
        assert parse_exact_period('1 month 1 day').apply(ANCHOR) == datetime(
            2021, 3, 1, tzinfo=timezone.utc)

    def test_day_then_month_from_jan_30(self):
        assert parse_exact_period('1 day 1 month').apply(ANCHOR) == datetime(
            2021, 2, 28, tzinfo=timezone.utc)

    def test_month_end_clamps_to_last_day_of_target_month(self):
        assert parse_exact_period('1 month').apply(ANCHOR) == datetime(
            2021, 2, 28, tzinfo=timezone.utc)

    def test_fixed_only_conversion_without_anchor(self):
        assert parse_exact_period('1.5 hours').to_timedelta() == timedelta(minutes=90)
        assert parse_exact_period('2 days').to_timedelta() == timedelta(days=2)

    def test_calendar_conversion_requires_anchor(self):
        with pytest.raises(ValueError):
            parse_exact_period('1 month').to_timedelta()
        assert parse_exact_period('1 month').to_timedelta(anchor=ANCHOR) == timedelta(days=29)

    def test_sub_microsecond_width_stays_exact_on_epoch_axis(self):
        period = parse_exact_period('0.0000001s')
        bounds = partition_bounds(Decimal('0'), period, count=2)
        assert bounds == [Decimal('0'), Decimal('0.0000001'), Decimal('0.0000002')]
        assert bounds[1] > bounds[0]  # not a zero-width window


class TestExactPeriodRoundTrip(object):
    @pytest.mark.parametrize('text', [
        '0.0000001 seconds',
        '-1.5 hours',
        '1 month 1 day',
        '2 years -3 days',
        '0 seconds',
    ])
    def test_write_then_read_keeps_value(self, text):
        period = parse_exact_period(text)
        assert parse_exact_period(period_to_hocon(period)) == period

    def test_boundary_membership_survives_round_trip(self):
        period = parse_exact_period('1 month 1 day')
        restored = parse_exact_period(period_to_hocon(period))
        for candidate in (period, restored):
            bounds = partition_bounds(ANCHOR, candidate, count=2)
            assert bounds == [datetime(2021, 1, 30, tzinfo=timezone.utc),
                              datetime(2021, 3, 1, tzinfo=timezone.utc),
                              datetime(2021, 4, 2, tzinfo=timezone.utc)]
            just_before = bounds[1] - timedelta(microseconds=1)
            assert locate_partition(bounds, just_before) == 0
            assert locate_partition(bounds, bounds[1]) == 1
