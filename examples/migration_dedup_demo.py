"""
Runnable walkthrough of the email de-duplication migration design.

Usage:  .venv/bin/python examples/migration_dedup_demo.py

It replays a legacy dataset that contains a wrong merge, a missed merge and
an indirect conflict at the same time, and prints the wire-level facts
(NFC / IDNA / SMTPUTF8), the three-valued clusters with their evidence, and
the raw values / policy versions that must be retained.
"""

from datetime import date

from email_validator.dedup import (
    AliasRule,
    CustomerRecord,
    Verdict,
    deduplicate,
    gmail_style_canonical,
    normalize_address,
)


def show_wire_facts() -> None:
    print("=== 1. Wire-level facts actually computed by this library ===")
    pairs = [
        ("NFC", "é.user@example.com", "é.user@example.com"),
        ("IDNA", "xmpp@xn--r8jz45g.xn--zckzah", "xmpp@例え.テスト"),
        ("case/dot", "Jane.Doe+news@Example.COM", "janedoe@example.com"),
    ]
    for label, left, right in pairs:
        a, b = normalize_address(left), normalize_address(right)
        print(f"[{label}] {left!r}")
        print(f"        nfc={a.nfc!r} ascii={a.ascii_form!r} "
              f"smtputf8={a.smtputf8}")
        print(f"        {right!r}")
        print(f"        nfc={b.nfc!r} ascii={b.ascii_form!r} "
              f"smtputf8={b.smtputf8}")
        print(f"        representation-equivalent: {a.rep_key == b.rep_key}")
    print(f"normalization policy version: "
          f"{normalize_address('janedoe@example.com').policy_version}\n")


def build_rules() -> list[AliasRule]:
    return [
        AliasRule("vendorx-example", "vendorx-alias-v1", "example.com",
                  date(2024, 1, 1), None, gmail_style_canonical),
        AliasRule("gmail-dotplus", "gmail-alias-v2", "gmail.com",
                  date(2020, 1, 1), None, gmail_style_canonical),
    ]


def build_records() -> list[CustomerRecord]:
    return [
        CustomerRecord("R1", "Jane.Doe+news@Example.COM", date(2026, 1, 10),
                       "lib_a", "C100", "capture-policy-2025"),
        CustomerRecord("R2", "janedoe@example.com", date(2026, 2, 1),
                       "lib_b", "C100"),
        CustomerRecord("R3", "é.user@example.com", date(2026, 3, 1),
                       "lib_a", "C200", "capture-policy-2025"),
        CustomerRecord("R4", "é.user@example.com", date(2026, 3, 2),
                       "lib_b"),
        CustomerRecord("R5", "a.b+old@gmail.com", date(2018, 5, 1),
                       "legacy", "C300", "legacy-policy-2018"),
        CustomerRecord("R6", "ab@gmail.com", date(2026, 4, 1),
                       "lib_a", "C400"),
        CustomerRecord("R7", "ab+receipts@gmail.com", date(2026, 4, 10),
                       "lib_b", "C400"),
        CustomerRecord("R8", "team.lead@example.net", date(2026, 5, 1),
                       "lib_a", "C500"),
        CustomerRecord("R9", "teamlead@example.net", date(2026, 5, 2),
                       "lib_b", "C600"),
        CustomerRecord("R10", "xmpp@xn--r8jz45g.xn--zckzah", date(2026, 6, 1),
                       "lib_a", "C700"),
        CustomerRecord("R11", "xmpp@例え.テスト", date(2026, 6, 2),
                       "lib_b", "C700"),
        CustomerRecord("R12", "billing@example.com", date(2019, 6, 1),
                       "legacy", "C800", "legacy-policy-2018"),
        CustomerRecord("R13", "billing@example.com", date(2026, 6, 1),
                       "lib_a", "C900"),
        CustomerRecord("R14", "old.user+tag@gmail.com", date(2018, 7, 1),
                       "legacy"),
        CustomerRecord("R15", "olduser@gmail.com", date(2026, 7, 1),
                       "lib_b"),
        CustomerRecord("R16", "a@b@c.example", date(2026, 1, 1), "lib_b"),
    ]


def main() -> None:
    show_wire_facts()

    rules, records = build_rules(), build_records()
    print("=== 2. Alias declarations (domain scope + half-open interval) ===")
    for r in rules:
        print(f"{r.rule_id} ({r.policy_version}) scope={r.domain_scope} "
              f"interval=[{r.valid_from}, {r.valid_to or 'open'})")
    print()

    result = deduplicate(records, rules)
    print("=== 3. Order-independent clusters ===")
    for cluster in result.clusters:
        print(f"[{cluster.verdict.value.upper():>12}] {list(cluster.record_ids)}")
        for reason in cluster.reasons:
            print(f"               reason : {reason}")
        for ev in cluster.evidence:
            print(f"               evidence[{ev.kind}] {ev.detail} "
                  f"(policy={ev.policy_version})")
    print()

    print("=== 4. Quarantined / stale-claim log ===")
    for rid, ev in result.rejected:
        print(f"{rid}: [{ev.kind}] {ev.detail} "
              f"rule={ev.rule_id} policy={ev.policy_version}")
    print()

    print("=== 5. Values that must be retained in the target schema ===")
    print("- raw original string per row (e.g. R1 keeps "
          f"{result.normalized['R1'].original!r}, never only the normalized form)")
    print("- nfc / ascii_form / smtputf8 triple + the normalization policy "
          f"version ({result.normalized['R1'].policy_version})")
    print("- capture-time policy_version on each record "
          "(e.g. R3='capture-policy-2025', R5='legacy-policy-2018')")
    print("- every alias declaration: rule_id, policy_version, domain scope, "
          "[valid_from, valid_to), transform identity")
    print("- rejected/stale claims with observation timestamp, and the full "
          "edge evidence chain behind each SAME/CONFLICT/INSUFFICIENT cluster")


if __name__ == "__main__":
    main()
