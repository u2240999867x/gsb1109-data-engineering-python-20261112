# fragments.py - reassembly of Mod 97, 10 numbers delivered as fragments
#
# Copyright (C) 2026 Arthur de Jong
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/

"""Reassembly of very long Mod 97, 10 numbers from fragment digests.

A very long number is split by the source into numbered fragments. The
source only keeps a short digest per fragment, so the original number can
never be read back from the stored data. Each digest captures exactly what
the ISO 7064 Mod 97, 10 checksum (see :mod:`stdnum.iso7064.mod_97_10`)
needs from that fragment:

* the fragment content interpreted the stdnum way, where letters are base36
  digits that expand to two base10 digits and leading zeroes keep their
  position in the expansion;
* the length of the fragment's base10 expansion;
* the Mod 97, 10 remainder of that expansion, stored as an absolute term
  positioned at the fragment's place in the whole number.

Fragments may arrive out of order, may be redelivered and may be replaced by
a newer version. A redelivery that carries the same source content
fingerprint is ignored. At any point the assembly distinguishes a number
that is fully covered and checksum-valid (``'complete'``) from one that
still has holes (``'incomplete'``) and from one where fragments of the same
version contradict each other (``'conflicting'``). Corrections may only
replace an interval with the exact same boundaries; a partial overlap is
rejected with the first conflicting position reported.

Because every digest stores its remainder at an absolute position,
replacing one fragment only requires swapping that single term in the
running modular sum. The old fragment content is never re-read and the
``recomputed`` counter in the merge result stays at one.

>>> from stdnum.iso7064 import mod_97_10
>>> full = '9999123456789012141490'
>>> mod_97_10.is_valid(full)
True
>>> parts = split_fragments(full, 8)
>>> assembly = FragmentAssembly(len(full))
>>> # deliver the tail and the head first, proving order does not matter
>>> assembly.deliver(parts[2][0], parts[2][1], fingerprint='src-3')
MergeResult(merged=1, skipped=0, recomputed=0, state='incomplete')
>>> assembly.deliver(parts[0][0], parts[0][1], fingerprint='src-1')
MergeResult(merged=1, skipped=0, recomputed=0, state='incomplete')
>>> assembly.deliver(parts[1][0], parts[1][1], fingerprint='src-2')
MergeResult(merged=1, skipped=0, recomputed=0, state='complete')
>>> assembly.is_valid()
True

A redelivery with the same source content fingerprint does nothing.

>>> assembly.deliver(parts[1][0], parts[1][1], fingerprint='src-2')
MergeResult(merged=0, skipped=1, recomputed=0, state='complete')

Two fragments of the same version that disagree on an overlapping position
are a contradiction and the first conflicting position is reported.

>>> other = '56789099'
>>> assembly.deliver(parts[1][0], other, fingerprint='src-other')
Traceback (most recent call last):
    ...
FragmentConflict: conflicting content at position 8
>>> assembly.state
'conflicting'

A replacement carries a new version and must cover the exact same interval.

>>> assembly = FragmentAssembly(len(full))
>>> for position, content in parts:
...     _ = assembly.deliver(position, content, fingerprint='src-%d' % position)
>>> assembly.replace(parts[1][0], parts[1][1], version=2,
...                  fingerprint='src-8-v2')
MergeResult(merged=1, skipped=0, recomputed=1, state='complete')
"""

from __future__ import annotations

import hashlib

from stdnum.exceptions import *
from stdnum.iso7064 import mod_97_10
from stdnum.util import clean


# the three states an assembly can be in
COMPLETE = 'complete'
INCOMPLETE = 'incomplete'
CONFLICTING = 'conflicting'


def _compact(content: str) -> str:
    """Return the fragment without separators, upper-cased.

    Upper-casing makes the fingerprint match the Mod 97, 10 semantics where
    lower-case and upper-case letters are the same base36 digit.

    >>> _compact('ab-0042 9')
    'AB00429'
    """
    return clean(content, ' -.').strip().upper()


def _base10_expansion(content: str) -> str:
    """Expand the fragment the way Mod 97, 10 does.

    Every character is interpreted as a base36 digit (A is 10, Z is 35) and
    expanded independently, exactly like ``mod_97_10._to_base10``. Letters
    contribute two digits while digits contribute one, and leading zeroes
    stay in their positions instead of collapsing away.

    >>> _base10_expansion('AB0042')
    '10110042'
    >>> _base10_expansion('0A')
    '010'
    """
    try:
        expansion = ''.join(
            str(int(char, 36)) for char in _compact(content))
    except ValueError:
        raise InvalidFormat()
    if not expansion:
        raise InvalidFormat()
    return expansion


def fragment_digest(content: str) -> tuple[int, int]:
    """Return the ``(length, remainder)`` digest of a fragment.

    ``length`` is the number of base10 digits the fragment expands to and
    ``remainder`` is its Mod 97, 10 remainder. Letters and leading zeroes
    are handled exactly as in the checksum itself.

    >>> fragment_digest('99991234')
    (8, 45)
    >>> fragment_digest('0042')
    (4, 42)
    >>> fragment_digest('AB')
    (4, 41)
    """
    expansion = _base10_expansion(content)
    return len(expansion), int(expansion) % 97


def fingerprint(content: str) -> str:
    """Return a stable source content fingerprint for a fragment.

    The fingerprint is a hex digest of the fragment in its compact form so
    redeliveries from the same source are recognised regardless of
    separators or letter case.

    >>> fingerprint('AB-0042') == fingerprint('AB0042')
    True
    >>> fingerprint('ab0042') == fingerprint('AB0042')
    True
    >>> fingerprint('AB0043') == fingerprint('AB0042')
    False
    """
    return hashlib.sha256(_compact(content).encode('ascii')).hexdigest()


def split_fragments(number: str, fragment_size: int) -> list[tuple[int, str]]:
    """Split a valid whole number into ``(position, content)`` fragments.

    The number is first checked with the Mod 97, 10 algorithm. Positions are
    offsets in the base10 expansion, so a letter (which expands to two
    base10 digits) is never split across two fragments.

    >>> split_fragments('9999123456789012141490', 8)
    [(0, '99991234'), (8, '56789012'), (16, '141490')]
    >>> body = 'AB0042'
    >>> number = body + mod_97_10.calc_check_digits(body)
    >>> split_fragments(number, 4)
    [(0, 'AB'), (4, '0042'), (8, '29')]
    >>> split_fragments('9999123456789012141499', 8)
    Traceback (most recent call last):
        ...
    InvalidChecksum: ...
    """
    mod_97_10.validate(number)
    if fragment_size <= 0:
        raise InvalidFormat()
    compact = _compact(number)
    fragments: list[tuple[int, str]] = []
    start = 0
    position = 0
    while start < len(compact):
        cursor = start
        width = 0
        while cursor < len(compact) and width < fragment_size:
            width += len(str(int(compact[cursor], 36)))
            cursor += 1
        fragments.append((position, compact[start:cursor]))
        position += width
        start = cursor
    return fragments


class Fragment:
    """A stored fragment digest with its positional metadata.

    The original content is deliberately not retained; only the values that
    can be combined into the whole-number checksum are kept. ``term`` is the
    fragment's remainder positioned absolutely in the whole number, so the
    full checksum is the sum of all terms modulo 97.
    """

    def __init__(self, position: int, content: str, total_length: int,
                 version: int = 1, fp: str | None = None) -> None:
        if position < 0:
            raise InvalidFormat()
        self.position = position
        self.version = version
        self.fingerprint = fp if fp is not None else fingerprint(content)
        self.length, self.remainder = fragment_digest(content)
        self.end = position + self.length
        if self.end > total_length:
            raise InvalidLength()
        self.term = (
            self.remainder * pow(10, total_length - self.end, 97)) % 97

    def __repr__(self) -> str:
        return (
            'Fragment(position=%r, end=%r, version=%r, remainder=%r, '
            'fingerprint=%r)' % (
                self.position, self.end, self.version, self.remainder,
                self.fingerprint))


class MergeResult:
    """Outcome of a delivery or replacement.

    The counters describe how many stored digests were touched:

    * ``merged`` digests newly stored or overwritten;
    * ``skipped`` deliveries ignored because the fingerprint was already
      known;
    * ``recomputed`` digests whose term had to be swapped because of a
      replacement; this stays at one for a single-fragment replacement,
      proving the other fragments were not walked again;
    * ``state`` is the assembly state after the operation.

    Rejected deliveries raise instead of being counted, so a rejection can
    never hide inside an otherwise successful merge.
    """

    __slots__ = ('merged', 'skipped', 'recomputed', 'state')

    def __init__(self, merged: int = 0, skipped: int = 0,
                 recomputed: int = 0, state: str = INCOMPLETE) -> None:
        self.merged = merged
        self.skipped = skipped
        self.recomputed = recomputed
        self.state = state

    def __repr__(self) -> str:
        return (
            'MergeResult(merged=%r, skipped=%r, recomputed=%r, state=%r)'
            % (self.merged, self.skipped, self.recomputed, self.state))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MergeResult):
            return NotImplemented
        return (
            self.merged == other.merged and self.skipped == other.skipped and
            self.recomputed == other.recomputed and
            self.state == other.state)


class FragmentAssembly:
    """Collect fragment digests and track the state of the whole number.

    ``total_length`` is the number of base10 digits of the fully expanded
    number, including the two Mod 97, 10 check digits. Fragments are
    addressed by their starting position in that expansion.
    """

    def __init__(self, total_length: int) -> None:
        if total_length <= 2:
            raise InvalidLength()
        self.total_length = total_length
        self._fragments: dict[int, Fragment] = {}
        self._remainder = 0
        self._conflicts: list[tuple[int, int]] = []

    # -- internal helpers -------------------------------------------------

    def _covering(self, position: int, end: int) -> list[Fragment]:
        """Return stored fragments that touch the half-open interval."""
        return [
            fragment for fragment in self._fragments.values()
            if fragment.position < end and fragment.end > position]

    def _raise_conflict(self, start: int, end: int,
                        message: str) -> None:
        """Record and raise a conflict over the given half-open interval."""
        interval = (start, end)
        if interval not in self._conflicts:
            self._conflicts.append(interval)
        raise FragmentConflict('%s at position %d' % (message, start))

    def _partial_overlap(self, fragment: Fragment) -> tuple[int, int] | None:
        """Return the overlap interval with a differently bounded fragment.

        Exact re-coverage of the same interval is not a partial overlap and
        is handled by the caller; everything else that touches a stored
        fragment is a conflict, even across versions.
        """
        for stored in self._covering(fragment.position, fragment.end):
            if (stored.position != fragment.position or
                    stored.end != fragment.end):
                return (
                    max(stored.position, fragment.position),
                    min(stored.end, fragment.end))
        return None

    def _clear_conflicts(self, position: int, end: int) -> None:
        """Drop recorded conflicts that the replacement fully covers."""
        self._conflicts = [
            interval for interval in self._conflicts
            if not (position <= interval[0] and interval[1] <= end)]

    # -- public API -------------------------------------------------------

    def deliver(self, position: int, content: str, version: int = 1,
                fingerprint: str | None = None) -> MergeResult:
        """Accept a fragment that may arrive out of order or be redelivered.

        A redelivery carrying a fingerprint already stored for the same
        interval and version is skipped. Same-version fragments that
        contradict each other raise :class:`FragmentConflict`, as does any
        partial overlap; a different version on an existing interval must be
        installed with :meth:`replace`.
        """
        fragment = Fragment(position, content, self.total_length,
                            version=version, fp=fingerprint)
        overlap = self._partial_overlap(fragment)
        if overlap is not None:
            self._raise_conflict(
                overlap[0], overlap[1], 'partially overlapping fragment')
        stored = self._fragments.get(position)
        if stored is not None:
            if stored.version != fragment.version:
                raise InvalidComponent(
                    'use replace() to install a new version at position %d'
                    % position)
            if stored.fingerprint == fragment.fingerprint:
                return MergeResult(skipped=1, state=self.state)
            self._raise_conflict(position, fragment.end,
                                 'conflicting content')
        self._fragments[position] = fragment
        self._remainder = (self._remainder + fragment.term) % 97
        return MergeResult(merged=1, state=self.state)

    def replace(self, position: int, content: str, version: int,
                fingerprint: str | None = None) -> MergeResult:
        """Replace a complete fragment with a newer version.

        The replacement must use the exact same boundaries as the interval
        it supersedes; a different length would spill into a neighbour and is
        rejected as a conflict. Only the term of the replaced fragment is
        swapped in the running sum, so the ``recomputed`` counter is one and
        no other fragment is revisited.
        """
        fragment = Fragment(position, content, self.total_length,
                            version=version, fp=fingerprint)
        stored = self._fragments.get(position)
        if stored is None:
            raise InvalidComponent(
                'no fragment at position %d to replace' % position)
        if stored.end != fragment.end:
            self._raise_conflict(
                max(stored.position, fragment.position),
                min(stored.end, fragment.end),
                'replacement boundary mismatch')
        if version < stored.version:
            raise InvalidComponent(
                'replacement version %d is older than version %d'
                % (version, stored.version))
        if version == stored.version:
            if stored.fingerprint == fragment.fingerprint:
                return MergeResult(skipped=1, state=self.state)
            self._raise_conflict(position, fragment.end,
                                 'conflicting content')
        self._remainder = (
            self._remainder - stored.term + fragment.term) % 97
        self._fragments[position] = fragment
        self._clear_conflicts(position, fragment.end)
        return MergeResult(merged=1, recomputed=1, state=self.state)

    @property
    def holes(self) -> list[tuple[int, int]]:
        """The half-open positions not covered by any stored fragment."""
        covered = sorted(
            (fragment.position, fragment.end)
            for fragment in self._fragments.values())
        missing: list[tuple[int, int]] = []
        cursor = 0
        for start, end in covered:
            if start > cursor:
                missing.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < self.total_length:
            missing.append((cursor, self.total_length))
        return missing

    @property
    def conflicts(self) -> list[tuple[int, int]]:
        """The half-open positions where same-version content clashed."""
        return list(self._conflicts)

    @property
    def state(self) -> str:
        """Return ``'complete'``, ``'incomplete'`` or ``'conflicting'``."""
        if self._conflicts:
            return CONFLICTING
        if self.holes:
            return INCOMPLETE
        return COMPLETE

    def checksum(self) -> int:
        """Return the Mod 97, 10 remainder of the stored digests.

        A complete and valid number has remainder ``1``. With holes the
        value only covers the digests received so far and is not meaningful
        as a whole-number check.
        """
        return self._remainder

    def is_valid(self) -> bool:
        """Return whether all positions are covered and the checksum is 1."""
        return self.state == COMPLETE and self._remainder == 1

    def validate(self) -> str:
        """Validate the assembly, raising on holes, conflicts or checksum."""
        if self._conflicts:
            raise FragmentConflict(
                'conflicting content at positions %r' % self._conflicts)
        if self.holes:
            raise InvalidLength(
                'missing fragment coverage at positions %r' % self.holes)
        if self._remainder != 1:
            raise InvalidChecksum()
        return COMPLETE
