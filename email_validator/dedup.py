"""
Customer-database merge: order-independent email de-duplication.

Two equivalence notions are kept strictly separate:

* **representation equivalence** -- two raw strings denote the same mailbox
  under pure wire-format normalization: Unicode NFC on the local part,
  IDNA/UTS-46 on the domain, and the SMTPUTF8/ASCII split.  It depends on no
  provider policy and is always safe to merge on.
* **business-account equivalence** -- two strings are delivered to the same
  account because a provider aliases them (Gmail-style dot/plus rules, ...).
  Such rules only hold for a declared domain scope and a declared half-open
  validity interval, so equivalence is evaluated *per observation time*.
  The same address may belong to different owners in disjoint intervals.

Clustering is two-layered and order-independent:

1. Hard union-find over records, fed only by *hard same* edges (equal
   representation fingerprint, in-force alias token, shared owner id).
   Each resulting atom that ends up with several distinct owner ids is an
   internally contradicted atom.
2. A candidate graph over those atoms.  Expired/out-of-scope alias hints
   become *weak edges* (they group candidates for review but can never prove
   sameness), and owner divergence becomes conflict evidence.

Final verdicts:

* ``same``         -- one atom, no weak edge, no owner divergence;
* ``insufficient`` -- several atoms joined only by weak (expired) evidence,
                      with at most one owner id in the candidate cluster;
* ``conflict``     -- hard owner divergence, including two proven-different
                      accounts that an expired alias would transitively join
                      through a third record.

A weak edge never promotes a component to ``same`` and never launders a
conflict, regardless of the order in which records are processed.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional, Sequence

from . import validate_email
from .exceptions import EmailNotValidError
from .types import ValidatedEmail

# A half-open time axis.  Stay within one concrete type per run.
Instant = date | datetime

# Provider canonicalization: NFC-normalized local part -> provider account
# token, or None when the rule does not claim the local part at all.
AliasTransform = Callable[[str], Optional[str]]


class Verdict(str, Enum):
    """Three-valued outcome for a record cluster."""

    SAME = "same"
    CONFLICT = "conflict"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class NormalizedEmail:
    """
    The three fingerprints actually computed by this library for one raw
    address.  All of them are retained as migration evidence -- never
    recompute silently from a stored value, because Unicode/IDNA tables
    evolve between runs.
    """

    original: str
    nfc: str                      # NFC local part @ UTS-46/IDNA i18n domain
    ascii_form: Optional[str]     # IDNA A-label form; None => SMTPUTF8 required
    smtputf8: bool
    local_part: str
    domain: str                   # normalized i18n domain
    ascii_domain: str
    policy_version: str           # versions of the rules that produced this

    @property
    def rep_key(self) -> tuple[str, str, bool]:
        """
        Representation-equivalence key.

        Equal key means the same mailbox on the wire: identical i18n
        normalized address, identical ASCII-on-the-wire form (empty string
        when none exists), and identical SMTPUTF8 requirement.  The
        SMTPUTF8 bit prevents collapsing a non-ASCII local-part record onto
        an ASCII-only record merely because the domains normalized alike.
        """
        return (self.nfc, self.ascii_form or "", self.smtputf8)


@dataclass(frozen=True)
class AliasRule:
    """
    One provider alias declaration.

    * ``rule_id`` / ``policy_version`` pin the exact policy statement so a
      migration can be re-run against the same rule text years later.
    * ``domain_scope`` matches the normalized i18n domain and, when present,
      the ASCII A-label form (so ``xn--`` input and Unicode input name the
      same scope).
    * ``valid_from`` / ``valid_to`` are the half-open interval
      ``[valid_from, valid_to)``; ``valid_to=None`` means still in force.
    * ``transform`` maps a normalized local part to the provider canonical
      account token.  Rules never rewrite the domain.
    """

    rule_id: str
    policy_version: str
    domain_scope: str
    valid_from: Instant
    valid_to: Optional[Instant]
    transform: AliasTransform

    def covers_domain(self, domain: str, ascii_domain: str) -> bool:
        return self.domain_scope in (domain, ascii_domain)

    def in_force(self, moment: Instant) -> bool:
        if moment < self.valid_from:
            return False
        return self.valid_to is None or moment < self.valid_to


@dataclass(frozen=True)
class CustomerRecord:
    """One row from either source library."""

    record_id: str
    raw_email: str
    observed_at: Instant
    source: str                           # which customer library it came from
    customer_ref: Optional[str] = None    # pre-existing stable account id
    policy_version: Optional[str] = None  # capture-time normalization policy


@dataclass(frozen=True)
class Evidence:
    """Why records were linked, kept apart, or refused a link."""

    kind: str  # representation | alias | weak_alias | owner | rejected_alias
    detail: str
    record_ids: tuple[str, ...] = ()
    rule_id: Optional[str] = None
    policy_version: Optional[str] = None
    at: Optional[Instant] = None


@dataclass(frozen=True)
class Cluster:
    """A final, order-independent group of records."""

    verdict: Verdict
    record_ids: tuple[str, ...]
    canonical_key: Optional[tuple[str, str, bool]]
    evidence: tuple[Evidence, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "record_ids": list(self.record_ids),
            "canonical_key": list(self.canonical_key) if self.canonical_key else None,
            "evidence": [
                {
                    "kind": e.kind,
                    "detail": e.detail,
                    "record_ids": list(e.record_ids),
                    "rule_id": e.rule_id,
                    "policy_version": e.policy_version,
                    "at": e.at.isoformat() if isinstance(e.at, (date, datetime)) else None,
                }
                for e in self.evidence
            ],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DedupResult:
    clusters: tuple[Cluster, ...]
    rejected: tuple[tuple[str, Evidence], ...]   # unparseable rows / lone stale claims
    normalized: dict[str, NormalizedEmail]

    def by_verdict(self, verdict: Verdict) -> tuple[Cluster, ...]:
        return tuple(c for c in self.clusters if c.verdict is verdict)


# Current normalization-policy fingerprint.  Bump whenever the normalization
# call path or its pinned dependencies change; stored rows keep their own
# version, which is what makes a later re-normalization auditable.
NORMALIZATION_POLICY_VERSION: str


def _module_version() -> str:
    from .version import __version__
    return __version__


def _idna_version() -> str:
    import idna
    return getattr(idna, "__version__", "unknown")


NORMALIZATION_POLICY_VERSION = (
    f"email-validator/{_module_version()}"
    f"::idna/{_idna_version()}"
    f"::unicodedata/UCD-{unicodedata.unidata_version}"
)


# --------------------------------------------------------------------------- #
# Step 1: compute the NFC / IDNA / SMTPUTF8 facts for every raw address.
# --------------------------------------------------------------------------- #

def normalize_address(raw: str) -> NormalizedEmail:
    """
    Run the library's real validation/normalization path and record all
    wire-level facts the migration judgment is based on.

    Raises EmailNotValidError for unparseable rows: they are quarantined by
    the caller instead of being silently dropped.
    """
    result: ValidatedEmail = validate_email(
        raw,
        check_deliverability=False,   # syntax/normalization only, no DNS
        allow_smtputf8=True,
    )
    return NormalizedEmail(
        original=result.original,
        nfc=result.normalized,
        ascii_form=result.ascii_email,
        smtputf8=result.smtputf8,
        local_part=result.local_part,
        domain=result.domain,
        ascii_domain=result.ascii_domain,
        policy_version=NORMALIZATION_POLICY_VERSION,
    )


# --------------------------------------------------------------------------- #
# Step 2a: hard union-find (hard-same edges only).
# --------------------------------------------------------------------------- #

class _HardUnionFind:
    """Union-find building atoms from proven-same edges, in any input order."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> int:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        return ra

    def components(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self._parent)):
            out.setdefault(self.find(i), []).append(i)
        return out


# --------------------------------------------------------------------------- #
# Step 2b: the migration judgment.
# --------------------------------------------------------------------------- #

def deduplicate(
    records: Sequence[CustomerRecord],
    alias_rules: Sequence[AliasRule],
) -> DedupResult:
    """
    Cluster customer records without depending on input order.

    Hard-same edges (layer 1):
      * equal representation fingerprint (NFC + IDNA + SMTPUTF8);
      * equal provider canonical token under a rule that covers the domain
        and is in force at the record's observation time;
      * shared stable ``customer_ref``.

    Weak edges (layer 2):
      * the same alias token under a rule whose domain matches but whose
        validity interval does not cover the observation -- the expired
        declaration is retained as evidence but never proves sameness.

    Conflict evidence:
      * one hard atom containing several distinct ``customer_ref`` values,
        or a weak-connected candidate cluster spanning several owner ids.
    """
    normals: dict[str, NormalizedEmail] = {}
    valid: list[CustomerRecord] = []
    rejected: list[tuple[str, Evidence]] = []
    for rec in records:
        try:
            normals[rec.record_id] = normalize_address(rec.raw_email)
            valid.append(rec)
        except EmailNotValidError as exc:
            rejected.append((
                rec.record_id,
                Evidence("rejected_alias", f"unparseable address: {exc}", (rec.record_id,)),
            ))

    uf = _HardUnionFind(len(valid))
    hard_evidence: list[Evidence] = []
    weak_evidence: list[Evidence] = []

    def rid(i: int) -> str:
        return valid[i].record_id

    # --- Hard edge 1: representation equivalence (group by rep_key) --------
    rep_groups: dict[tuple[str, str, bool], list[int]] = {}
    for i, rec in enumerate(valid):
        rep_groups.setdefault(normals[rec.record_id].rep_key, []).append(i)
    for key, members in rep_groups.items():
        if len(members) < 2:
            continue
        ids = tuple(sorted(rid(i) for i in members))
        hard_evidence.append(Evidence(
            "representation",
            f"identical normalized mailbox {key[0]!r} (NFC+IDNA; "
            f"ascii_form={key[1]!r}; SMTPUTF8={key[2]})",
            ids,
            policy_version=NORMALIZATION_POLICY_VERSION,
        ))
        anchor = members[0]
        for other in members[1:]:
            uf.union(anchor, other)

    # --- Hard edge 2 / weak edge: alias rules, scoped in domain and time ---
    hard_sig: dict[tuple[str, str, str], list[int]] = {}
    weak_sig: dict[tuple[str, str, str], list[int]] = {}
    for i, rec in enumerate(valid):
        info = normals[rec.record_id]
        for rule in alias_rules:
            if not rule.covers_domain(info.domain, info.ascii_domain):
                continue
            token = rule.transform(info.local_part)
            if token is None:
                continue
            sig = (rule.rule_id, info.domain, token)
            if rule.in_force(rec.observed_at):
                hard_sig.setdefault(sig, []).append(i)
            else:
                # The rule is stale for this observation.  Keep the hint,
                # but it can never produce a hard-same edge.
                weak_sig.setdefault(sig, []).append(i)
                rejected.append((
                    rec.record_id,
                    Evidence(
                        "rejected_alias",
                        f"rule {rule.rule_id} (policy {rule.policy_version}) not "
                        f"in force at {rec.observed_at.isoformat()} for "
                        f"{info.nfc!r}; stale claim retained as weak evidence",
                        (rid(i),),
                        rule_id=rule.rule_id,
                        policy_version=rule.policy_version,
                        at=rec.observed_at,
                    ),
                ))

    for (rule_id, domain, token), members in hard_sig.items():
        if len(members) < 2:
            continue
        rule = next(r for r in alias_rules if r.rule_id == rule_id)
        ids = tuple(sorted(rid(i) for i in members))
        hard_evidence.append(Evidence(
            "alias",
            f"rule {rule_id} maps every local part to canonical token "
            f"{token!r} on {domain!r} while in force "
            f"[{rule.valid_from.isoformat()}, "
            f"{rule.valid_to.isoformat() if rule.valid_to else 'open'})",
            ids,
            rule_id=rule_id,
            policy_version=rule.policy_version,
        ))
        anchor = members[0]
        for other in members[1:]:
            uf.union(anchor, other)

    weak_pairs: list[tuple[int, int, Evidence]] = []
    for sig in sorted(set(hard_sig) | set(weak_sig)):
        hard = hard_sig.get(sig, [])
        weak = weak_sig.get(sig, [])
        if not hard and len(weak) < 2:
            continue
        rule_id, domain, token = sig
        rule = next(r for r in alias_rules if r.rule_id == rule_id)
        if weak:
            ids = tuple(sorted(rid(i) for i in hard + weak))
            ev = Evidence(
                "weak_alias",
                f"rule {rule_id} would map every local part to token "
                f"{token!r} on {domain!r}, but its interval "
                f"[{rule.valid_from.isoformat()}, "
                f"{rule.valid_to.isoformat() if rule.valid_to else 'open'}) "
                "does not cover every observation -- stale claim, cannot "
                "prove sameness",
                ids,
                rule_id=rule_id,
                policy_version=rule.policy_version,
            )
            weak_evidence.append(ev)
            # The hard members (if any) already form one atom; every weak
            # member is attached to that atom by a weak edge only.  With no
            # hard members, the weak members are chained among themselves.
            anchor = hard[0] if hard else weak[0]
            for w in weak:
                if w != anchor:
                    weak_pairs.append((anchor, w, ev))

    # --- Hard edge 3: stable owner ids -------------------------------------
    ref_groups: dict[str, list[int]] = {}
    for i, rec in enumerate(valid):
        if rec.customer_ref is not None:
            ref_groups.setdefault(rec.customer_ref, []).append(i)
    for ref, members in sorted(ref_groups.items()):
        if len(members) < 2:
            continue
        ids = tuple(sorted(rid(i) for i in members))
        hard_evidence.append(Evidence(
            "owner",
            f"shared stable customer_ref {ref!r}",
            ids,
        ))
        anchor = members[0]
        for other in members[1:]:
            uf.union(anchor, other)

    # --- Layer-2 graph: connect hard atoms via weak edges ------------------
    atoms = uf.components()
    atom_of = {i: root for root, members in atoms.items() for i in members}
    # Distinct owner ids inside one hard atom are a direct contradiction.
    atom_refs: dict[int, set[str]] = {}
    for root, members in atoms.items():
        atom_refs[root] = {valid[i].customer_ref for i in members
                           if valid[i].customer_ref}
    candidate_uf = _HardUnionFind(len(atoms))
    atom_roots = sorted(atoms)
    atom_pos = {root: pos for pos, root in enumerate(atom_roots)}
    for a, b, _ev in weak_pairs:
        candidate_uf.union(atom_pos[atom_of[a]], atom_pos[atom_of[b]])

    # --- Verdicts -----------------------------------------------------------
    clusters: list[Cluster] = []
    candidate_components = candidate_uf.components()
    # Deterministic emission order, independent of record input order.
    ordered = sorted(
        candidate_components.values(),
        key=lambda positions: min(rid(atoms[atom_roots[p]][0]) for p in positions),
    )
    for positions in ordered:
        member_idx = sorted(
            (i for p in positions for i in atoms[atom_roots[p]]),
            key=lambda i: rid(i),
        )
        member_ids = tuple(rid(i) for i in member_idx)
        member_set = set(member_ids)

        refs = sorted({valid[i].customer_ref for i in member_idx
                       if valid[i].customer_ref})
        weak_here = tuple(sorted(
            (e for e in weak_evidence if set(e.record_ids) & member_set),
            key=lambda e: (e.kind, e.detail),
        ))
        hard_here = tuple(sorted(
            (e for e in hard_evidence if set(e.record_ids) & member_set),
            key=lambda e: (e.kind, e.detail),
        ))

        internal_conflict = any(
            len(atom_refs[atom_of[i]]) > 1 for i in member_idx
        )
        uses_weak_edge = bool(weak_here)

        if internal_conflict:
            verdict = Verdict.CONFLICT
            reasons = ["hard atom contains several distinct customer_ref "
                       "values: the sources contradict each other directly"]
        elif uses_weak_edge and len(refs) > 1:
            verdict = Verdict.CONFLICT
            reasons = [
                "proven-different owner ids meet only through expired alias "
                "evidence; the stale rule is rejected and cannot transit via "
                "a third record (indirect conflict)",
            ]
        elif uses_weak_edge:
            verdict = Verdict.INSUFFICIENT
            reasons = [
                "atoms are connected only by expired/out-of-scope alias "
                "evidence; no in-force rule, representation match, or owner "
                "id closes the gap -- manual review required",
            ]
        else:
            verdict = Verdict.SAME
            reasons = [
                "every link is a hard-same edge (representation equivalence, "
                "in-force alias, or shared owner id) with no contradiction",
            ]

        if refs:
            reasons = (*reasons, f"owner id(s) present: {', '.join(refs)}")
        keys = {normals[r].rep_key for r in member_ids}
        canonical = next(iter(keys)) if len(keys) == 1 else None
        clusters.append(Cluster(
            verdict=verdict,
            record_ids=member_ids,
            canonical_key=canonical,
            evidence=tuple(sorted(hard_here + weak_here,
                                  key=lambda e: (e.kind, e.detail))),
            reasons=tuple(reasons),
        ))

    return DedupResult(
        clusters=tuple(clusters),
        rejected=tuple(rejected),
        normalized=normals,
    )


# --------------------------------------------------------------------------- #
# Built-in provider transforms (the "dot/plus alias rules" side of the merge).
# --------------------------------------------------------------------------- #

def gmail_style_canonical(local_part: str) -> Optional[str]:
    """
    Google-style canonicalization: lowercase, drop every '.' and strip a
    '+tag' suffix.  Applies only where the declaring rule's domain scope
    matches; the transform itself intentionally never sees the domain.
    """
    if not local_part or local_part.startswith('"'):
        return None
    base = local_part.split("+", 1)[0]
    return base.replace(".", "").casefold()


def plus_only_canonical(local_part: str) -> Optional[str]:
    """Providers that honor RFC 5233 subaddressing but treat dots as real."""
    if not local_part or local_part.startswith('"'):
        return None
    return local_part.split("+", 1)[0].casefold()
