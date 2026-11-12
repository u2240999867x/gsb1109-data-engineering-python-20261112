"""
Tests for the migration de-duplication design in email_validator.dedup.

The final test replays one legacy dataset that simultaneously contains:

* a wrong merge   -- a gmail-style rule applied across a validity boundary;
* a missed merge  -- NFD/NFC and IDNA A-label/Unicode pairs kept apart by the
                     old system because it compared raw strings;
* an indirect conflict -- an expired alias reaching two proven-different
                     accounts through a third record.
"""

import random
from datetime import date

import pytest

from email_validator.dedup import (
    AliasRule,
    CustomerRecord,
    NORMALIZATION_POLICY_VERSION,
    Verdict,
    deduplicate,
    gmail_style_canonical,
    normalize_address,
    plus_only_canonical,
)
from email_validator.exceptions import EmailNotValidError


# --------------------------------------------------------------------------- #
# Wire-level facts: NFC, IDNA, SMTPUTF8 computed by the actual library.
# --------------------------------------------------------------------------- #

def test_nfc_collapses_nfd_form_but_keeps_smtputf8_fact() -> None:
    nfc = "é.user@example.com"
    nfd = "é.user@example.com"
    a = normalize_address(nfc)
    b = normalize_address(nfd)

    # Representation equivalence: identical normalized mailbox + ascii form
    # + SMTPUTF8 requirement, even though the raw strings differ.
    assert a.rep_key == b.rep_key
    assert a.nfc == b.nfc == "é.user@example.com"
    assert a.original == nfc and b.original == nfd
    # Non-ASCII local part has no ASCII-on-the-wire form: SMTPUTF8 is a
    # recorded fact, not something the migration is allowed to paper over.
    assert a.smtputf8 is True and a.ascii_form is None
    assert a.policy_version.startswith("email-validator/")
    assert "idna/" in a.policy_version
    assert "unicodedata/UCD-" in a.policy_version


def test_idna_alabel_and_unicode_domain_are_representation_equivalent() -> None:
    alabel = normalize_address("xmpp@xn--r8jz45g.xn--zckzah")
    unicode_form = normalize_address("xmpp@例え.テスト")

    assert alabel.rep_key == unicode_form.rep_key
    assert alabel.domain == "例え.テスト"
    assert alabel.ascii_domain == "xn--r8jz45g.xn--zckzah"
    assert alabel.smtputf8 is False  # ASCII local part + IDNA domain
    assert alabel.ascii_form == "xmpp@xn--r8jz45g.xn--zckzah"


def test_unparseable_rows_are_quarantined_not_dropped() -> None:
    with pytest.raises(EmailNotValidError):
        normalize_address("not-an-email")


# --------------------------------------------------------------------------- #
# Rule semantics: domain scope and validity interval.
# --------------------------------------------------------------------------- #

def test_alias_rule_respects_domain_scope() -> None:
    rule = AliasRule(
        "gmail-v2", "policy-gmail-2", "gmail.com",
        date(2020, 1, 1), None, gmail_style_canonical,
    )
    assert rule.covers_domain("gmail.com", "gmail.com")
    assert not rule.covers_domain("example.net", "example.net")
    assert rule.in_force(date(2026, 1, 1))
    assert not rule.in_force(date(2019, 12, 31))  # half-open [from, to)


def test_transforms_implement_distinct_provider_policies() -> None:
    assert gmail_style_canonical("A.B+tag") == "ab"
    assert plus_only_canonical("A.B+tag") == "a.b"  # dots are real here
    assert gmail_style_canonical('"quoted"') is None


# --------------------------------------------------------------------------- #
# The full legacy replay.
# --------------------------------------------------------------------------- #

def _rules() -> list[AliasRule]:
    return [
        # Vendor X (example.com): dot-stripping plus subaddressing, in force
        # only from 2024 onward.
        AliasRule(
            "vendorx-example", "vendorx-alias-v1", "example.com",
            date(2024, 1, 1), None, gmail_style_canonical,
        ),
        # Google-style rule on gmail.com, current policy generation.
        AliasRule(
            "gmail-dotplus", "gmail-alias-v2", "gmail.com",
            date(2020, 1, 1), None, gmail_style_canonical,
        ),
    ]


def _records() -> list[CustomerRecord]:
    return [
        # --- legitimate in-force alias merge (lib A normalized, lib B raw) -
        CustomerRecord("R1", "Jane.Doe+news@Example.COM", date(2026, 1, 10),
                       "lib_a", customer_ref="C100",
                       policy_version="capture-policy-2025"),
        CustomerRecord("R2", "janedoe@example.com", date(2026, 2, 1),
                       "lib_b", customer_ref="C100"),

        # --- missed merge in the old system: NFD vs NFC --------------------
        CustomerRecord("R3", "é.user@example.com", date(2026, 3, 1),
                       "lib_a", customer_ref="C200",
                       policy_version="capture-policy-2025"),
        CustomerRecord("R4", "é.user@example.com", date(2026, 3, 2),
                       "lib_b"),

        # --- wrong merge in the old system: gmail rule applied retroactively
        # R5 predates the rule's validity interval; R6/R7 are current.
        CustomerRecord("R5", "a.b+old@gmail.com", date(2018, 5, 1),
                       "legacy", customer_ref="C300",
                       policy_version="legacy-policy-2018"),
        CustomerRecord("R6", "ab@gmail.com", date(2026, 4, 1),
                       "lib_a", customer_ref="C400"),
        # Third record through which the stale alias would transit.
        CustomerRecord("R7", "ab+receipts@gmail.com", date(2026, 4, 10),
                       "lib_b", customer_ref="C400"),

        # --- cross-domain mis-application: dots are real on example.net ----
        CustomerRecord("R8", "team.lead@example.net", date(2026, 5, 1),
                       "lib_a", customer_ref="C500"),
        CustomerRecord("R9", "teamlead@example.net", date(2026, 5, 2),
                       "lib_b", customer_ref="C600"),

        # --- missed merge in the old system: IDNA A-label vs Unicode -------
        CustomerRecord("R10", "xmpp@xn--r8jz45g.xn--zckzah", date(2026, 6, 1),
                       "lib_a", customer_ref="C700"),
        CustomerRecord("R11", "xmpp@例え.テスト", date(2026, 6, 2),
                       "lib_b", customer_ref="C700"),

        # --- same mailbox, owner changes across disjoint intervals ---------
        CustomerRecord("R12", "billing@example.com", date(2019, 6, 1),
                       "legacy", customer_ref="C800",
                       policy_version="legacy-policy-2018"),
        CustomerRecord("R13", "billing@example.com", date(2026, 6, 1),
                       "lib_a", customer_ref="C900"),

        # --- expired alias with no owner id at all: evidence insufficient --
        CustomerRecord("R14", "old.user+tag@gmail.com", date(2018, 7, 1),
                       "legacy"),
        CustomerRecord("R15", "olduser@gmail.com", date(2026, 7, 1),
                       "lib_b"),

        # --- unparseable legacy row ----------------------------------------
        CustomerRecord("R16", "a@b@c.example", date(2026, 1, 1), "lib_b"),
    ]


def _cluster_map(result) -> dict[frozenset[str], Verdict]:
    return {frozenset(c.record_ids): c.verdict for c in result.clusters}


def test_legacy_replay_verdicts() -> None:
    result = deduplicate(_records(), _rules())
    groups = _cluster_map(result)

    # Legit in-force alias merge.
    assert groups[frozenset({"R1", "R2"})] is Verdict.SAME
    # NFC representation equivalence repairs the missed merge.
    assert groups[frozenset({"R3", "R4"})] is Verdict.SAME
    # IDNA representation equivalence repairs the other missed merge.
    assert groups[frozenset({"R10", "R11"})] is Verdict.SAME
    # Indirect conflict: stale R5 may not join C300 to C400 via R7.
    assert groups[frozenset({"R5", "R6", "R7"})] is Verdict.CONFLICT
    # No rule covers example.net: old cross-domain wrong merge stays undone.
    assert groups[frozenset({"R8"})] is Verdict.SAME
    assert groups[frozenset({"R9"})] is Verdict.SAME
    # Same mailbox, two owners across time: never auto-collapsed.
    assert groups[frozenset({"R12", "R13"})] is Verdict.CONFLICT
    # Weak-only connection with no owner id: needs human review.
    assert groups[frozenset({"R14", "R15"})] is Verdict.INSUFFICIENT

    rejected_ids = {rid for rid, _ in result.rejected}
    assert "R16" in rejected_ids


def test_stale_alias_cannot_transit_through_third_record() -> None:
    result = deduplicate(_records(), _rules())
    cluster = next(c for c in result.clusters
                   if frozenset(c.record_ids) == frozenset({"R5", "R6", "R7"}))

    assert cluster.verdict is Verdict.CONFLICT
    kinds = {e.kind for e in cluster.evidence}
    assert "weak_alias" in kinds       # the expired gmail claim is retained
    assert "alias" in kinds            # the in-force R6/R7 link is retained
    assert any("distinct customer_refs" in r or "expired alias" in r
               for r in cluster.reasons)
    weak = next(e for e in cluster.evidence if e.kind == "weak_alias")
    assert weak.rule_id == "gmail-dotplus"
    assert weak.policy_version == "gmail-alias-v2"


def test_insufficient_cluster_explains_the_missing_evidence() -> None:
    result = deduplicate(_records(), _rules())
    cluster = next(c for c in result.clusters
                   if frozenset(c.record_ids) == frozenset({"R14", "R15"}))
    assert cluster.verdict is Verdict.INSUFFICIENT
    assert all(e.kind == "weak_alias" for e in cluster.evidence)
    assert any("expired" in r for r in cluster.reasons)


def test_raw_values_and_policy_versions_are_retained_as_evidence() -> None:
    result = deduplicate(_records(), _rules())

    # Original bytes-as-strings survive alongside every normalized form.
    assert result.normalized["R1"].original == "Jane.Doe+news@Example.COM"
    assert result.normalized["R1"].nfc == "Jane.Doe+news@example.com"
    # Capture-time policy versions on the records are never overwritten.
    r3 = next(r for r in _records() if r.record_id == "R3")
    assert r3.policy_version == "capture-policy-2025"
    # Re-normalization policy is versioned per run.
    assert result.normalized["R3"].policy_version == NORMALIZATION_POLICY_VERSION
    # Stale claims are kept in the rejection log with their policy version.
    stale = [e for _, e in result.rejected if e.kind == "rejected_alias"]
    assert any(e.rule_id == "gmail-dotplus"
               and e.policy_version == "gmail-alias-v2" for e in stale)
    assert any(e.rule_id == "vendorx-example"
               and e.policy_version == "vendorx-alias-v1" for e in stale)


def test_clustering_is_order_independent() -> None:
    baseline = deduplicate(_records(), _rules())
    baseline_ser = sorted(
        (c.verdict.value, sorted(c.record_ids),
         sorted((e.kind, e.detail) for e in c.evidence))
        for c in baseline.clusters
    )

    rng = random.Random(20261112)
    for _ in range(10):
        shuffled = _records()
        rng.shuffle(shuffled)
        result = deduplicate(shuffled, _rules())
        serialized = sorted(
            (c.verdict.value, sorted(c.record_ids),
             sorted((e.kind, e.detail) for e in c.evidence))
            for c in result.clusters
        )
        assert serialized == baseline_ser


def test_same_address_owner_handoff_is_a_conflict_not_a_silent_merge() -> None:
    records = [
        CustomerRecord("H1", "billing@example.com", date(2019, 1, 1),
                       "legacy", customer_ref="C800"),
        CustomerRecord("H2", "billing@example.com", date(2026, 1, 1),
                       "lib_a", customer_ref="C900"),
    ]
    result = deduplicate(records, _rules())
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.verdict is Verdict.CONFLICT
    assert cluster.canonical_key is not None  # mailbox identity is proven...
    assert any("customer_ref" in r for r in cluster.reasons)  # ...ownership is not
