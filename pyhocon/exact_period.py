import calendar
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR

MICROS_PER_SECOND = Decimal(1000000)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

FIXED_UNIT_ALIASES = {
    'nanoseconds': ['ns', 'nano', 'nanos', 'nanosecond', 'nanoseconds'],
    'microseconds': ['us', 'micro', 'micros', 'microsecond', 'microseconds'],
    'milliseconds': ['ms', 'milli', 'millis', 'millisecond', 'milliseconds'],
    'seconds': ['s', 'second', 'seconds'],
    'minutes': ['m', 'minute', 'minutes'],
    'hours': ['h', 'hour', 'hours'],
    'weeks': ['w', 'week', 'weeks'],
    'days': ['d', 'day', 'days'],
}

CALENDAR_UNIT_ALIASES = {
    'months': ['mo', 'month', 'months'],
    'years': ['y', 'year', 'years'],
}

FIXED_UNIT_SECONDS = {
    'nanoseconds': Decimal('0.000000001'),
    'microseconds': Decimal('0.000001'),
    'milliseconds': Decimal('0.001'),
    'seconds': Decimal('1'),
    'minutes': Decimal('60'),
    'hours': Decimal('3600'),
    'days': Decimal('86400'),
    'weeks': Decimal('604800'),
}

_UNIT_LOOKUP = {}
for _canonical, _aliases in list(FIXED_UNIT_ALIASES.items()) + list(CALENDAR_UNIT_ALIASES.items()):
    for _alias in _aliases:
        _UNIT_LOOKUP[_alias] = _canonical

_COMPONENT_RE = re.compile(
    r'\s*,?\s*'
    r'(?P<value>[+-]?(?:\d+(?:\.\d+)?|\.\d+))'
    r'\s*'
    r'(?P<unit>[A-Za-z]+)'
)


class PeriodComponent(object):
    """A single written duration item, e.g. ``-1.5 hours`` or ``1 month``."""

    __slots__ = ('value', 'unit')

    def __init__(self, value, unit):
        if unit not in FIXED_UNIT_SECONDS and unit not in CALENDAR_UNIT_ALIASES:
            raise ValueError('unknown period unit: %r' % (unit,))
        value = Decimal(value)
        if self.is_calendar_unit(unit) and value != value.to_integral_value():
            raise ValueError('calendar unit %r only accepts whole values, got %r' % (unit, value))
        self.value = value
        self.unit = unit

    @staticmethod
    def is_calendar_unit(unit):
        return unit in CALENDAR_UNIT_ALIASES

    @property
    def is_calendar(self):
        return self.is_calendar_unit(self.unit)

    def seconds(self):
        """Exact length of a fixed (non-calendar) component in seconds."""
        if self.is_calendar:
            raise ValueError('calendar component %r has no fixed length' % (self.unit,))
        return self.value * FIXED_UNIT_SECONDS[self.unit]

    def months(self):
        """Whole months of a calendar component (years count as 12 months)."""
        if not self.is_calendar:
            raise ValueError('fixed component %r has no calendar length' % (self.unit,))
        months = int(self.value)
        return months * 12 if self.unit == 'years' else months

    def __eq__(self, other):
        return (isinstance(other, PeriodComponent)
                and self.value == other.value and self.unit == other.unit)

    def __hash__(self):
        return hash((self.value, self.unit))

    def __repr__(self):
        return 'PeriodComponent(%r, %r)' % (self.value, self.unit)


def _format_decimal(value):
    return format(value, 'f')


def _datetime_to_epoch(instant):
    delta = instant - EPOCH
    return (Decimal(delta.days) * 86400 + Decimal(delta.seconds)
            + Decimal(delta.microseconds) / MICROS_PER_SECOND)


def _epoch_to_datetime_split(epoch):
    """Split exact epoch seconds into a UTC datetime and a sub-microsecond rest."""
    total_micros = int((epoch * MICROS_PER_SECOND).to_integral_value(rounding=ROUND_FLOOR))
    residual = epoch - Decimal(total_micros) / MICROS_PER_SECOND
    return EPOCH + timedelta(microseconds=total_micros), residual


def _epoch_to_datetime(epoch):
    instant, residual = _epoch_to_datetime_split(epoch)
    if residual != 0:
        raise ValueError('instant %s is not representable as a datetime' % (epoch,))
    return instant


def _add_months(instant, months):
    """Shift a datetime by whole months, clamping to the last day of the target month."""
    total = instant.year * 12 + (instant.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return instant.replace(year=year, month=month, day=min(instant.day, last_day))


class Period(object):
    """Fidelity-preserving duration: an ordered sequence of written components.

    Fixed components keep signed decimal values (e.g. ``0.0000001 seconds``)
    and calendar components keep whole months/years, so nothing is silently
    truncated to zero or approximated as 30 days.
    """

    __slots__ = ('_components',)

    def __init__(self, components):
        self._components = tuple(components)

    @property
    def components(self):
        return self._components

    @property
    def has_calendar(self):
        return any(component.is_calendar for component in self._components)

    def fixed_seconds(self):
        """Exact total of the fixed components in seconds (calendar parts excluded)."""
        total = Decimal(0)
        for component in self._components:
            if not component.is_calendar:
                total += component.seconds()
        return total

    def to_timedelta(self, anchor=None):
        """Convert to a timedelta.

        Without an anchor only fixed components may be converted; calendar
        components (months/years) require a datetime anchor so their actual
        length can be measured.
        """
        if self.has_calendar:
            if anchor is None:
                raise ValueError('calendar components require a datetime anchor')
            if not isinstance(anchor, datetime):
                raise TypeError('anchor must be a datetime, got %r' % type(anchor))
            return self.apply(anchor) - anchor
        micros = self.fixed_seconds() * MICROS_PER_SECOND
        if micros != micros.to_integral_value():
            raise ValueError('period %r is not representable as a timedelta' % (self,))
        return timedelta(microseconds=int(micros))

    def apply(self, anchor):
        """Apply every component to the anchor in written order.

        A datetime anchor yields a datetime (month-end clamps to the last day
        of the target month). A numeric anchor is treated as exact UTC epoch
        seconds and yields a Decimal, preserving sub-microsecond widths.
        """
        if isinstance(anchor, datetime):
            moment = anchor if anchor.tzinfo is not None else anchor.replace(tzinfo=timezone.utc)
            return _epoch_to_datetime(self.apply_epoch(_datetime_to_epoch(moment)))
        if isinstance(anchor, Decimal):
            epoch = anchor
        elif isinstance(anchor, bool):
            raise TypeError('anchor must be a datetime or a number, got %r' % type(anchor))
        else:
            epoch = Decimal(anchor)
        return self.apply_epoch(epoch)

    def apply_epoch(self, epoch):
        """Apply to exact UTC epoch seconds (Decimal in, Decimal out)."""
        current = epoch
        for component in self._components:
            if component.is_calendar:
                instant, residual = _epoch_to_datetime_split(current)
                current = _datetime_to_epoch(_add_months(instant, component.months())) + residual
            else:
                current += component.seconds()
        return current

    def partition_bounds(self, start, count=1):
        """Half-open boundaries of ``count`` adjacent partitions starting at ``start``."""
        return partition_bounds(start, self, count)

    def to_hocon(self):
        if not self._components:
            return '0 seconds'
        return ' '.join('%s %s' % (_format_decimal(component.value), component.unit)
                        for component in self._components)

    def __eq__(self, other):
        return isinstance(other, Period) and self._components == other._components

    def __hash__(self):
        return hash(self._components)

    def __repr__(self):
        return 'Period(%r)' % (self.to_hocon(),)


def parse_exact_period(content):
    """Parse a fidelity-preserving period such as ``1 month 1 day`` or ``0.0000001s``."""
    text = content.strip(' ,\t\n\r')
    components = []
    position = 0
    while position < len(text):
        match = _COMPONENT_RE.match(text, position)
        if match is None:
            raise ValueError('invalid period %r at position %d' % (content, position))
        unit = _UNIT_LOOKUP.get(match.group('unit').lower())
        if unit is None:
            raise ValueError('unknown period unit %r in %r' % (match.group('unit'), content))
        components.append(PeriodComponent(Decimal(match.group('value')), unit))
        position = match.end()
    if not components:
        raise ValueError('invalid period %r' % (content,))
    return Period(components)


def partition_bounds(start, period, count=1):
    """Return ``count + 1`` exact boundaries of adjacent half-open partitions.

    Partition ``i`` covers ``[bounds[i], bounds[i + 1])``; every bound is the
    previous one with ``period`` applied in written order.
    """
    if count < 1:
        raise ValueError('count must be at least 1, got %r' % (count,))
    bounds = [start]
    current = start
    for _ in range(count):
        current = period.apply(current)
        bounds.append(current)
    return bounds


def locate_partition(bounds, instant):
    """Index of the half-open partition ``[bounds[i], bounds[i + 1])`` holding ``instant``."""
    if len(bounds) < 2:
        raise ValueError('need at least two bounds, got %r' % (bounds,))
    if instant < bounds[0] or instant >= bounds[-1]:
        raise ValueError('instant %r is outside the partitioned range' % (instant,))
    low, high = 0, len(bounds) - 1
    while low + 1 < high:
        middle = (low + high) // 2
        if instant < bounds[middle]:
            high = middle
        else:
            low = middle
    return low
