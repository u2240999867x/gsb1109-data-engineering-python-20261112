# An offline DNS "fact table" for decision-regression tests of
# email_validator.deliverability.
#
# Unlike tests/mocked_dns_response.py (a fixed snapshot of recorded
# answers), a DnsFactTable is built declaratively per test and its
# answers may vary over a logical clock, so the same domain can be
# replayed at different points in time. The resolver built on top of
# the table records every query it actually serves, which gives us
# the "evidence" (the answer trace) behind each decision.

from dataclasses import dataclass
from typing import Any, Iterator, Optional, Union

import dns.exception
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.resolver


# --- Possible outcomes of a single DNS question -----------------------------

@dataclass(frozen=True)
class Answer:
    # A positive answer: rdata records in zone-file text form,
    # e.g. "10 mail.example.com." (MX), "192.0.2.1" (A),
    # '"v=spf1 -all"' (TXT).
    rdata: tuple[str, ...]


@dataclass(frozen=True)
class NoAnswer:
    # NODATA: the name exists but has no records of this type.
    pass


@dataclass(frozen=True)
class NXDomain:
    # The name does not exist at all.
    pass


@dataclass(frozen=True)
class Timeout:
    # Transient: the query timed out.
    pass


@dataclass(frozen=True)
class NoNameservers:
    # Transient: every nameserver failed to answer.
    pass


Outcome = Union[Answer, NoAnswer, NXDomain, Timeout, NoNameservers]


# --- The fact table ----------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    # One row of the fact table: `outcome` is the answer to
    # (name, rdtype) during the half-open interval
    # [valid_from, valid_to) on the table's logical clock.
    name: str
    rdtype: str
    outcome: Outcome
    valid_from: int = 0
    valid_to: Optional[int] = None

    def is_valid_at(self, tick: int) -> bool:
        return self.valid_from <= tick and (self.valid_to is None or tick < self.valid_to)


class UnknownQuestion(AssertionError):
    # Raised when the code under test asks a question the fact table
    # has no row for. In a regression suite an unexpected question is
    # a failure, not something to silently paper over.
    pass


class DnsFactTable:
    def __init__(self, facts: tuple[Fact, ...] = ()) -> None:
        self._facts = list(facts)
        self.clock = 0

    def at(self, tick: int) -> "DnsFactTable":
        # Move the logical clock; answers may differ afterwards.
        self.clock = tick
        return self

    def add(self, *facts: Fact) -> "DnsFactTable":
        self._facts.extend(facts)
        return self

    def drop(self, name: str, rdtype: str) -> "DnsFactTable":
        # Remove every fact for (name, rdtype). Any later read of that
        # question raises UnknownQuestion, which is exactly what we
        # want when asserting a branch is never consulted.
        self._facts = [f for f in self._facts if not (f.name == name and f.rdtype == rdtype)]
        return self

    def lookup(self, name: str, rdtype: str) -> Outcome:
        for fact in self._facts:
            if fact.name == name and fact.rdtype == rdtype and fact.is_valid_at(self.clock):
                return fact.outcome
        raise UnknownQuestion(f"fact table has no answer for {name!r} {rdtype!r} at t={self.clock}")


# --- Resolver + evidence trace ------------------------------------------------

@dataclass(frozen=True)
class TraceEntry:
    # One served question: what was asked and what came back.
    # `kind` is the outcome class name ("Answer", "NoAnswer",
    # "NXDomain", "Timeout", "NoNameservers").
    name: str
    rdtype: str
    kind: str
    rdata: tuple[str, ...] = ()


class _AnswerView:
    # Minimal stand-in for dns.resolver.Answer: the deliverability
    # code only iterates over it, so a parsed rdata list suffices.
    def __init__(self, rdtype: str, rdata: tuple[str, ...]) -> None:
        self._rdatas = [
            dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.from_text(rdtype), text)
            for text in rdata
        ]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rdatas)


class FactTableResolver:
    # Implements the single method of dns.resolver.Resolver that
    # email_validator.deliverability uses: resolve(qname, rdtype).
    def __init__(self, table: DnsFactTable) -> None:
        self.table = table
        self.trace: list[TraceEntry] = []

    def resolve(self, qname: str, rdtype: str, **kwargs: Any) -> _AnswerView:
        name = str(qname).rstrip(".")
        rdtype = dns.rdatatype.to_text(dns.rdatatype.from_text(rdtype))
        outcome = self.table.lookup(name, rdtype)
        if isinstance(outcome, Answer):
            self.trace.append(TraceEntry(name, rdtype, "Answer", outcome.rdata))
            return _AnswerView(rdtype, outcome.rdata)
        self.trace.append(TraceEntry(name, rdtype, type(outcome).__name__))
        if isinstance(outcome, NoAnswer):
            raise dns.resolver.NoAnswer
        if isinstance(outcome, NXDomain):
            raise dns.resolver.NXDOMAIN
        if isinstance(outcome, Timeout):
            raise dns.exception.Timeout
        if isinstance(outcome, NoNameservers):
            raise dns.resolver.NoNameservers
        raise UnknownQuestion(f"unhandled outcome {outcome!r}")


def trace_kinds(trace: list[TraceEntry]) -> list[tuple[str, str]]:
    # Compact view of an evidence trace for assertions:
    # [("MX", "Answer"), ("A", "NoAnswer"), ...]
    return [(entry.rdtype, entry.kind) for entry in trace]
