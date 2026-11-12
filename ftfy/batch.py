"""
Preview a batch cleaning of a column of text, guided by verified examples.

When a column of data (such as old product names) mixes clean text with one
or more chains of mis-decoded text, fixing each row independently with
:func:`ftfy.fix_text` can be unsatisfying: rows that were broken in the same
way may be handled inconsistently, and there is no way to say which rows
should be cleaned together.

This module provides :func:`preview_cleaning`, which takes a more deliberate
approach for data-cleaning pipelines:

- The caller provides a few **verified** pairs of (original, target) text
  that a human has checked, plus rows that must be **kept** exactly as they
  are.
- The candidate transformations are the fix plans that ftfy actually
  produces for the verified originals (see :func:`ftfy.fix_and_explain`).
- A plan is only *admissible* if it leaves every kept row unchanged and
  never rewrites a verified original to anything but its verified target.
- A set of plans is *compatible* if no two plans in the set would rewrite
  the same row to different text, so each row is rewritten by at most one
  plan.

:func:`preview_cleaning` finds the compatible sets of admissible plans that
cover the most verified rows, breaking ties toward fewer plans. There can be
more than one optimal set, and all of them are considered: if every optimal
set would write the same text for an unverified row, that text is the
previewed result; if they disagree, the row is flagged for review and all of
its possible results are shown. The outcome never depends on the order of
the inputs.

    >>> verified = [("cafÃ© au lait", "café au lait"), ("rockâ€™nâ€™roll", "rock'n'roll")]
    >>> preview = preview_cleaning(verified, keep=["100% cotton"], rows=["MontrÃ©al"])
    >>> preview.covered
    2
    >>> preview.rows[0].result
    'Montréal'
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from ftfy import ExplanationStep, TextFixerConfig, apply_plan, fix_and_explain

if TYPE_CHECKING:
    from collections.abc import Iterable


class VerifiedRow(NamedTuple):
    """
    The outcome for one verified (original, target) pair.

    `covered` is True when every optimal set of plans rewrites `original` to
    `target`. If only some of the optimal sets cover this pair, `covered` is
    False, because the cleaning is not guaranteed to fix it.
    """

    original: str
    target: str
    covered: bool


class RowPreview(NamedTuple):
    """
    The previewed outcome for one unverified row.

    When every optimal set of plans writes the same text for this row,
    `result` is that text, `alternatives` contains just that text, and
    `needs_review` is False. When optimal sets disagree, `result` is None,
    `alternatives` contains all of the texts they would write (including the
    unchanged original, if some set would leave the row alone), and
    `needs_review` is True: the row should be left for a human to confirm.
    """

    original: str
    result: str | None
    alternatives: tuple[str, ...]
    needs_review: bool


class CleaningPreview(NamedTuple):
    """
    The result of previewing a batch cleaning with :func:`preview_cleaning`.

    - `solutions`: all of the optimal compatible sets of plans. Each plan is
      a tuple of :class:`ftfy.ExplanationStep` steps that can be applied with
      :func:`ftfy.apply_plan`.
    - `verified`: one :class:`VerifiedRow` per verified pair, in input order.
    - `rows`: one :class:`RowPreview` per unverified row, in input order.
    - `covered`: the number of verified pairs that an optimal set of plans
      covers (the same for every optimal set).
    """

    solutions: tuple[tuple[tuple[ExplanationStep, ...], ...], ...]
    verified: tuple[VerifiedRow, ...]
    rows: tuple[RowPreview, ...]
    covered: int


class _Candidate(NamedTuple):
    """
    A plan that could take part in the cleaning, with the effects it would
    have on the input data.

    - `plan`: the fix plan, as a tuple of ExplanationSteps.
    - `covers`: the indices of the verified pairs whose target the plan
      reproduces (counting pairs that are already correct, which the plan
      leaves unchanged).
    - `effects`: a mapping from input strings to the different string the
      plan would rewrite them to. Strings the plan cannot or would not
      change do not appear.
    """

    plan: tuple[ExplanationStep, ...]
    covers: frozenset[int]
    effects: dict[str, str]


def _apply_safe(text: str, plan: tuple[ExplanationStep, ...]) -> str | None:
    """
    Apply a fix plan to text, returning None if the plan cannot be applied
    (for example, the text cannot be encoded in the plan's encoding). A plan
    that cannot be applied to a row leaves that row unchanged.
    """
    try:
        return apply_plan(text, list(plan))
    except (UnicodeError, ValueError, LookupError):
        return None


def _conflicts(effects1: dict[str, str], effects2: dict[str, str]) -> bool:
    """
    Check whether two plans are incompatible: is there a row that both plans
    would rewrite, but to different text?
    """
    if len(effects1) > len(effects2):
        effects1, effects2 = effects2, effects1
    for text, new_text in effects1.items():
        if effects2.get(text, new_text) != new_text:
            return True
    return False


def _optimal_solutions(
    candidates: list[_Candidate],
) -> list[tuple[int, ...]]:
    """
    Find every compatible set of candidates (as tuples of indices into
    `candidates`) that covers the most verified pairs, breaking ties toward
    the fewest plans. All optimal sets are returned, so that callers can
    detect ambiguity instead of depending on the order of the candidates.
    """
    num_candidates = len(candidates)
    conflicts = [[False] * num_candidates for _ in range(num_candidates)]
    for i in range(num_candidates):
        for j in range(i + 1, num_candidates):
            if _conflicts(candidates[i].effects, candidates[j].effects):
                conflicts[i][j] = conflicts[j][i] = True

    # suffix_covers[i] is the union of the pairs covered by candidates[i:],
    # used to bound what a partial solution could still cover.
    suffix_covers: list[set[int]] = [set() for _ in range(num_candidates + 1)]
    for i in range(num_candidates - 1, -1, -1):
        suffix_covers[i] = suffix_covers[i + 1] | candidates[i].covers

    best_key = (0, 0)
    best: list[tuple[int, ...]] = []
    chosen: list[int] = []

    def search(index: int, covered: frozenset[int]) -> None:
        nonlocal best_key, best
        key = (len(covered), -len(chosen))
        if key > best_key:
            best_key = key
            best = [tuple(chosen)]
        elif key == best_key:
            best.append(tuple(chosen))
        for i in range(index, num_candidates):
            # Any solution extending `chosen` covers at most
            # `covered | suffix_covers[i]` pairs and adds at least one plan.
            # If even that cannot beat the best key, neither can anything
            # later in the loop, because the bound only shrinks.
            bound = len(covered | suffix_covers[i])
            if (bound, -(len(chosen) + 1)) < best_key:
                break
            if any(conflicts[i][other] for other in chosen):
                continue
            chosen.append(i)
            search(i + 1, covered | candidates[i].covers)
            chosen.pop()

    search(0, frozenset())
    return best


def preview_cleaning(
    verified: Iterable[tuple[str, str]],
    keep: Iterable[str] = (),
    rows: Iterable[str] = (),
    config: TextFixerConfig | None = None,
) -> CleaningPreview:
    """
    Preview a consistent cleaning of a column of text, using verified
    examples to choose which of ftfy's fix plans to apply.

    Arguments:

    - `verified`: (original, target) pairs that a human has checked. The
      cleaning should rewrite each original to its target.
    - `keep`: rows that the cleaning must leave exactly as they are.
    - `rows`: the remaining, unverified rows to preview.
    - `config`: an optional TextFixerConfig controlling how ftfy derives
      fix plans from the verified originals.

    Returns a :class:`CleaningPreview`. Unverified rows whose preview has
    `needs_review` set should be left for a human to confirm, because two
    optimal ways of classifying the data would write different text for
    them.

        >>> verified = [("MontrÃ©al €", "Montréal €"), ("Montr√©al", "Montréal")]
        >>> preview = preview_cleaning(verified, rows=["cafÃ©", "plain"])
        >>> preview.covered
        1
        >>> preview.rows[0].needs_review
        True
        >>> preview.rows[0].alternatives == ("café", "caf̩")
        True
        >>> preview.rows[1].result
        'plain'
    """
    pairs = list(verified)
    kept = list(keep)
    unverified = list(rows)

    if config is None:
        config = TextFixerConfig()
    config = config._replace(explain=True)

    # The candidate transformations are the fix plans that actually appear
    # on the verified samples and reproduce the verified target.
    plans: list[tuple[ExplanationStep, ...]] = []
    seen: set[tuple[ExplanationStep, ...]] = set()
    for original, target in pairs:
        explained = fix_and_explain(original, config)
        plan = tuple(explained.explanation or ())
        if explained.text == target and plan not in seen:
            seen.add(plan)
            plans.append(plan)

    inputs = list(dict.fromkeys([original for original, _ in pairs] + kept + unverified))

    candidates = []
    for plan in plans:
        effects = {}
        for text in inputs:
            fixed = _apply_safe(text, plan)
            if fixed is not None and fixed != text:
                effects[text] = fixed
        # A plan is admissible only if it leaves the kept rows unchanged and
        # never rewrites a verified original to anything but its verified
        # target. (Leaving a verified row it doesn't cover unchanged is
        # fine.)
        if any(text in effects for text in kept):
            continue
        if any(original in effects and effects[original] != target for original, target in pairs):
            continue
        covers = frozenset(
            index
            for index, (original, target) in enumerate(pairs)
            if effects.get(original, original) == target
        )
        if not covers:
            continue
        candidates.append(_Candidate(plan, covers, effects))

    # Plans that have exactly the same effect on the input data are
    # equivalent here; keep one representative so they can't create
    # spurious ambiguity.
    merged: dict[frozenset[tuple[str, str]], _Candidate] = {}
    for candidate in candidates:
        signature = frozenset(candidate.effects.items())
        previous = merged.get(signature)
        if previous is None or candidate.plan < previous.plan:
            merged[signature] = candidate
    candidates = sorted(merged.values(), key=lambda candidate: candidate.plan)

    solutions = _optimal_solutions(candidates)
    plan_solutions = tuple(
        tuple(sorted(candidates[index].plan for index in solution)) for solution in solutions
    )
    # Sort the solutions themselves, so the result doesn't depend on the
    # order in which the search happened to find them.
    plan_solutions = tuple(sorted(plan_solutions))

    effects_by_plan = {candidate.plan: candidate.effects for candidate in candidates}
    covers_by_plan = {candidate.plan: candidate.covers for candidate in candidates}
    covered_sets = [
        frozenset().union(*(covers_by_plan[plan] for plan in solution)) if solution else frozenset()
        for solution in plan_solutions
    ]
    covered_count = len(covered_sets[0])

    verified_rows = tuple(
        VerifiedRow(
            original,
            target,
            all(index in covered_set for covered_set in covered_sets),
        )
        for index, (original, target) in enumerate(pairs)
    )

    row_previews = []
    for row in unverified:
        outputs = set()
        for solution in plan_solutions:
            output = row
            for plan in solution:
                output = effects_by_plan[plan].get(row, output)
            outputs.add(output)
        alternatives = tuple(sorted(outputs))
        if len(alternatives) == 1:
            row_previews.append(RowPreview(row, alternatives[0], alternatives, False))
        else:
            row_previews.append(RowPreview(row, None, alternatives, True))

    return CleaningPreview(
        solutions=plan_solutions,
        verified=verified_rows,
        rows=tuple(row_previews),
        covered=covered_count,
    )
