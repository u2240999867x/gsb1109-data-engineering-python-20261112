# Offline decision regression for email_validator's deliverability check.
#
# Every case is replayed against an independent DNS fact table (see
# dns_fact_table.py) that serves MX, A, AAAA and SPF/TXT answers which
# may change over a logical clock. For each run we assert two things:
#
#   1. the final warehouse row status: "deliverable", "rejected"
#      (definitive) or "unknown" (transient), and
#   2. the evidence actually used: the exact trace of DNS questions
#      and answers the decision was based on.

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from email_validator import EmailUndeliverableError
from email_validator.deliverability import validate_email_deliverability

from dns_fact_table import (
    Answer,
    DnsFactTable,
    Fact,
    FactTableResolver,
    NoAnswer,
    NoNameservers,
    NXDomain,
    Timeout,
    TraceEntry,
    trace_kinds,
)

DOMAIN = "warehouse.example"

STATUS_DELIVERABLE = "deliverable"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"


@dataclass
class DecisionRow:
    # One warehouse row: the classified outcome plus the evidence
    # (DNS answer trace) that produced it.
    status: str
    info: dict[str, Any]
    reason: Optional[str] = None
    evidence: list[TraceEntry] = field(default_factory=list)


def classify(domain: str, table: DnsFactTable) -> DecisionRow:
    # Run one deliverability decision and bucket it for the warehouse.
    resolver = FactTableResolver(table)
    try:
        info = validate_email_deliverability(domain, domain, dns_resolver=resolver)
    except EmailUndeliverableError as exc:
        return DecisionRow(STATUS_REJECTED, {}, reason=str(exc), evidence=resolver.trace)
    if "unknown-deliverability" in info:
        return DecisionRow(STATUS_UNKNOWN, info, reason=info["unknown-deliverability"], evidence=resolver.trace)
    return DecisionRow(STATUS_DELIVERABLE, info, evidence=resolver.trace)


# --- Scenario matrix: final status + evidence ---------------------------------

@dataclass
class Scenario:
    name: str
    facts: tuple[Fact, ...]
    expected_status: str
    expected_evidence: list[tuple[str, str]]
    expected_info: Optional[dict[str, Any]] = None
    expected_reason: Optional[str] = None


SCENARIOS = [
    Scenario(
        name="mx_record",
        facts=(Fact(DOMAIN, "MX", Answer(("10 mx1." + DOMAIN + ".", "20 mx2." + DOMAIN + "."))),),
        expected_status=STATUS_DELIVERABLE,
        expected_evidence=[("MX", "Answer")],
        expected_info={"mx": [(10, "mx1." + DOMAIN), (20, "mx2." + DOMAIN)], "mx_fallback_type": None},
    ),
    Scenario(
        name="null_mx",
        facts=(Fact(DOMAIN, "MX", Answer(("0 .",))),),
        expected_status=STATUS_REJECTED,
        expected_evidence=[("MX", "Answer")],
        expected_reason=f"The domain name {DOMAIN} does not accept email.",
    ),
    Scenario(
        name="a_fallback",
        facts=(
            Fact(DOMAIN, "MX", NoAnswer()),
            Fact(DOMAIN, "A", Answer(("8.8.8.8",))),
            Fact(DOMAIN, "TXT", NoAnswer()),  # no SPF policy on file
        ),
        expected_status=STATUS_DELIVERABLE,
        expected_evidence=[("MX", "NoAnswer"), ("A", "Answer"), ("TXT", "NoAnswer")],
        expected_info={"mx": [(0, DOMAIN)], "mx_fallback_type": "A"},
    ),
    Scenario(
        name="aaaa_fallback_after_nonglobal_a",
        facts=(
            Fact(DOMAIN, "MX", NoAnswer()),
            Fact(DOMAIN, "A", Answer(("10.0.0.1",))),  # private: not a valid fallback
            Fact(DOMAIN, "AAAA", Answer(("2001:4860:4860::8888",))),
            Fact(DOMAIN, "TXT", NoAnswer()),
        ),
        expected_status=STATUS_DELIVERABLE,
        expected_evidence=[("MX", "NoAnswer"), ("A", "Answer"), ("AAAA", "Answer"), ("TXT", "NoAnswer")],
        expected_info={"mx": [(0, DOMAIN)], "mx_fallback_type": "AAAA"},
    ),
    Scenario(
        name="no_mx_a_aaaa",
        facts=(
            Fact(DOMAIN, "MX", NoAnswer()),
            Fact(DOMAIN, "A", NoAnswer()),
            Fact(DOMAIN, "AAAA", NoAnswer()),
        ),
        expected_status=STATUS_REJECTED,
        expected_evidence=[("MX", "NoAnswer"), ("A", "NoAnswer"), ("AAAA", "NoAnswer")],
        expected_reason=f"The domain name {DOMAIN} does not accept email.",
    ),
    Scenario(
        name="nxdomain",
        facts=(Fact(DOMAIN, "MX", NXDomain()),),
        expected_status=STATUS_REJECTED,
        expected_evidence=[("MX", "NXDomain")],
        expected_reason=f"The domain name {DOMAIN} does not exist.",
    ),
    Scenario(
        name="timeout",
        facts=(Fact(DOMAIN, "MX", Timeout()),),
        expected_status=STATUS_UNKNOWN,
        expected_evidence=[("MX", "Timeout")],
        expected_reason="timeout",
    ),
    Scenario(
        name="no_nameservers",
        facts=(Fact(DOMAIN, "MX", NoNameservers()),),
        expected_status=STATUS_UNKNOWN,
        expected_evidence=[("MX", "NoNameservers")],
        expected_reason="no_nameservers",
    ),
    Scenario(
        name="spf_reject_all_with_a_fallback",
        facts=(
            Fact(DOMAIN, "MX", NoAnswer()),
            Fact(DOMAIN, "A", Answer(("8.8.8.8",))),
            Fact(DOMAIN, "TXT", Answer(('"v=spf1 -all"',))),
        ),
        expected_status=STATUS_REJECTED,
        expected_evidence=[("MX", "NoAnswer"), ("A", "Answer"), ("TXT", "Answer")],
        expected_reason=f"The domain name {DOMAIN} does not send email.",
    ),
    Scenario(
        name="spf_non_reject_policy_is_not_evidence_of_rejection",
        facts=(
            Fact(DOMAIN, "MX", NoAnswer()),
            Fact(DOMAIN, "A", Answer(("8.8.8.8",))),
            Fact(DOMAIN, "TXT", Answer(('"v=spf1 include:spf." + DOMAIN + " -all"',))),
        ),
        expected_status=STATUS_DELIVERABLE,
        expected_evidence=[("MX", "NoAnswer"), ("A", "Answer"), ("TXT", "Answer")],
        expected_info={"mx": [(0, DOMAIN)], "mx_fallback_type": "A"},
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_decision_status_and_evidence(scenario: Scenario) -> None:
    row = classify(DOMAIN, DnsFactTable(scenario.facts))
    assert row.status == scenario.expected_status
    assert trace_kinds(row.evidence) == scenario.expected_evidence
    if scenario.expected_info is not None:
        assert row.info == scenario.expected_info
    if scenario.expected_reason is not None:
        assert row.reason == scenario.expected_reason
    if scenario.expected_status == STATUS_DELIVERABLE:
        assert "mx" in row.info
    else:
        assert "mx" not in row.info


# --- Time-varying facts --------------------------------------------------------

def test_facts_may_change_over_time() -> None:
    # The MX record lapses at t=10; the A record and a reject-all SPF
    # policy appear at t=20. The same domain is re-decided per tick.
    table = DnsFactTable((
        Fact(DOMAIN, "MX", Answer(("10 mx." + DOMAIN + ".",)), valid_to=10),
        Fact(DOMAIN, "MX", NoAnswer(), valid_from=10),
        Fact(DOMAIN, "A", Answer(("8.8.8.8",)), valid_from=10),
        Fact(DOMAIN, "TXT", NoAnswer(), valid_from=10, valid_to=20),
        Fact(DOMAIN, "TXT", Answer(('"v=spf1 -all"',)), valid_from=20),
    ))

    early = classify(DOMAIN, table.at(5))
    assert early.status == STATUS_DELIVERABLE
    assert early.info["mx_fallback_type"] is None
    assert trace_kinds(early.evidence) == [("MX", "Answer")]

    middle = classify(DOMAIN, table.at(15))
    assert middle.status == STATUS_DELIVERABLE
    assert middle.info["mx_fallback_type"] == "A"
    assert trace_kinds(middle.evidence) == [("MX", "NoAnswer"), ("A", "Answer"), ("TXT", "NoAnswer")]

    late = classify(DOMAIN, table.at(25))
    assert late.status == STATUS_REJECTED
    assert late.reason == f"The domain name {DOMAIN} does not send email."
    assert trace_kinds(late.evidence) == [("MX", "NoAnswer"), ("A", "Answer"), ("TXT", "Answer")]


# --- Early conclusions must not depend on unread branches ----------------------

def test_mx_hit_makes_other_branches_irrelevant() -> None:
    facts = (
        Fact(DOMAIN, "MX", Answer(("10 mx." + DOMAIN + ".",))),
        Fact(DOMAIN, "A", Answer(("8.8.8.8",))),
        Fact(DOMAIN, "AAAA", Answer(("2001:4860:4860::8888",))),
        Fact(DOMAIN, "TXT", Answer(('"v=spf1 -all"',))),
    )
    baseline = classify(DOMAIN, DnsFactTable(facts))
    assert baseline.status == STATUS_DELIVERABLE
    assert trace_kinds(baseline.evidence) == [("MX", "Answer")]

    # Delete the unread branches outright: any read of them would now
    # raise UnknownQuestion. The row must be bit-for-bit identical.
    mutated = DnsFactTable(facts)
    for rdtype in ("A", "AAAA", "TXT"):
        mutated.drop(DOMAIN, rdtype)
    row = classify(DOMAIN, mutated)
    assert row.status == baseline.status
    assert row.info == baseline.info
    assert row.evidence == baseline.evidence


def test_null_mx_concludes_before_spf_branch() -> None:
    facts = (
        Fact(DOMAIN, "MX", Answer(("0 .",))),
        Fact(DOMAIN, "TXT", Answer(('"v=spf1 include:spf." + DOMAIN + " -all"',))),
    )
    baseline = classify(DOMAIN, DnsFactTable(facts))
    assert baseline.status == STATUS_REJECTED
    assert trace_kinds(baseline.evidence) == [("MX", "Answer")]

    mutated = DnsFactTable(facts).drop(DOMAIN, "TXT")
    row = classify(DOMAIN, mutated)
    assert row.status == baseline.status
    assert row.reason == baseline.reason
    assert row.evidence == baseline.evidence


def test_aaaa_branch_unread_once_a_fallback_succeeds() -> None:
    facts = (
        Fact(DOMAIN, "MX", NoAnswer()),
        Fact(DOMAIN, "A", Answer(("8.8.8.8",))),
        Fact(DOMAIN, "AAAA", NXDomain()),  # even a contradictory answer is irrelevant
        Fact(DOMAIN, "TXT", NoAnswer()),
    )
    baseline = classify(DOMAIN, DnsFactTable(facts))
    assert baseline.status == STATUS_DELIVERABLE
    assert trace_kinds(baseline.evidence) == [("MX", "NoAnswer"), ("A", "Answer"), ("TXT", "NoAnswer")]

    mutated = DnsFactTable(facts).drop(DOMAIN, "AAAA")
    row = classify(DOMAIN, mutated)
    assert row.status == baseline.status
    assert row.info == baseline.info
    assert row.evidence == baseline.evidence


# --- Transient failure must not become a permanent rejection -------------------

@pytest.mark.parametrize("transient", [Timeout(), NoNameservers()], ids=["timeout", "no_nameservers"])
def test_transient_failure_is_unknown_not_rejected(transient: Any) -> None:
    table = DnsFactTable((
        Fact(DOMAIN, "MX", transient, valid_to=10),
        Fact(DOMAIN, "MX", Answer(("10 mx." + DOMAIN + ".",)), valid_from=10),
    ))

    during_outage = classify(DOMAIN, table.at(5))
    # No evidence was obtained, so the row is "unknown": it carries no
    # mx field and must not be recorded as a definitive rejection.
    assert during_outage.status == STATUS_UNKNOWN
    assert "mx" not in during_outage.info
    assert during_outage.status != STATUS_REJECTED

    # Once the transient condition clears, the same domain is decided
    # on real evidence — proving the earlier row was not a permanent
    # verdict.
    after_recovery = classify(DOMAIN, table.at(15))
    assert after_recovery.status == STATUS_DELIVERABLE
    assert after_recovery.info["mx"] == [(10, "mx." + DOMAIN)]


# --- Shortest traces distinguishing two rows that both lack "mx" ---------------

def test_shortest_traces_distinguishing_transient_from_permanent() -> None:
    # Row 1: temporary failure. The decision gives up after a single
    # unanswered question, so the trace is one transient answer long.
    transient_row = classify(DOMAIN, DnsFactTable((Fact(DOMAIN, "MX", Timeout()),)))
    # Row 2: permanent denial. One NXDOMAIN answer is the shortest
    # definitive "no" — no fallback questions are even asked.
    permanent_row = classify(DOMAIN, DnsFactTable((Fact(DOMAIN, "MX", NXDomain()),)))

    # Both rows lack the mx field...
    assert "mx" not in transient_row.info
    assert "mx" not in permanent_row.info

    # ...yet they land in different buckets, and the minimal evidence
    # that separates them is a single answer in each trace:
    assert trace_kinds(transient_row.evidence) == [("MX", "Timeout")]
    assert trace_kinds(permanent_row.evidence) == [("MX", "NXDomain")]
    assert len(transient_row.evidence) == len(permanent_row.evidence) == 1
    assert transient_row.status == STATUS_UNKNOWN
    assert permanent_row.status == STATUS_REJECTED

    # The longer permanent-denial path (name exists, but no MX, A or
    # AAAA records) needs three answers before it may reject — it is
    # not the shortest trace.
    nodata_row = classify(DOMAIN, DnsFactTable((
        Fact(DOMAIN, "MX", NoAnswer()),
        Fact(DOMAIN, "A", NoAnswer()),
        Fact(DOMAIN, "AAAA", NoAnswer()),
    )))
    assert nodata_row.status == STATUS_REJECTED
    assert len(nodata_row.evidence) == 3
