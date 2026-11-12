from ftfy import preview_cleaning
from ftfy.batch import CleaningPreview, RowPreview, VerifiedRow

LATIN1_PLAN = (("encode", "latin-1"), ("decode", "utf-8"))
WINDOWS_PLAN = (
    ("encode", "sloppy-windows-1252"),
    ("decode", "utf-8"),
    ("apply", "uncurl_quotes"),
)
INCONSISTENT_UTF8_PLAN = (("apply", "decode_inconsistent_utf8"),)
MACROMAN_PLAN = (("encode", "macroman"), ("decode", "utf-8"))


def test_one_plan_can_clean_both_chains():
    # The Windows-1252 chain and the Latin-1 chain look different, but the
    # plan derived from the Windows-1252 sample also fixes the Latin-1
    # sample, so a single plan covers everything.
    verified = [
        ("cafÃ© au lait", "café au lait"),
        ("rockâ€™nâ€™roll", "rock'n'roll"),
    ]
    preview = preview_cleaning(verified, keep=["100% cotton"], rows=["MontrÃ©al", "â‚¬5 off"])

    assert preview.covered == 2
    assert preview.solutions == ((WINDOWS_PLAN,),)
    assert all(row.covered for row in preview.verified)
    assert preview.rows == (
        RowPreview("MontrÃ©al", "Montréal", ("Montréal",), False),
        RowPreview("â‚¬5 off", "€5 off", ("€5 off",), False),
    )


def test_conflicting_chains_leave_rows_for_review():
    # Two chains whose plans can't be told apart with the verified data:
    # each plan covers one verified pair, and they would rewrite "cafÃ©"
    # differently. Both classifications are optimal, so the row is flagged
    # for review with both possible results.
    verified = [
        ("MontrÃ©al €", "Montréal €"),
        ("Montr√©al", "Montréal"),
    ]
    preview = preview_cleaning(verified, rows=["cafÃ©", "plain"])

    assert preview.covered == 1
    assert preview.solutions == ((INCONSISTENT_UTF8_PLAN,), (MACROMAN_PLAN,))
    assert preview.verified == (
        VerifiedRow("MontrÃ©al €", "Montréal €", False),
        VerifiedRow("Montr√©al", "Montréal", False),
    )

    flagged, clean = preview.rows
    assert flagged.original == "cafÃ©"
    assert flagged.needs_review
    assert flagged.result is None
    assert flagged.alternatives == ("café", "caf̩")
    assert clean == RowPreview("plain", "plain", ("plain",), False)


def test_ambiguity_does_not_depend_on_input_order():
    verified = [
        ("MontrÃ©al €", "Montréal €"),
        ("Montr√©al", "Montréal"),
    ]
    rows = ["cafÃ©", "MontrÃ©al", "plain"]
    forward = preview_cleaning(verified, rows=rows)
    backward = preview_cleaning(list(reversed(verified)), rows=list(reversed(rows)))

    assert forward.solutions == backward.solutions
    assert forward.covered == backward.covered
    assert sorted(forward.verified) == sorted(backward.verified)
    assert sorted(forward.rows) == sorted(backward.rows)


def test_plan_that_contradicts_a_verified_row_is_rejected():
    # The MacRoman plan would fix "Montr√©al", but it would also rewrite the
    # verified "MontrÃ©al" to something other than its verified target, so it
    # can't take part in the cleaning.
    verified = [
        ("MontrÃ©al", "Montréal"),
        ("Montr√©al", "Montréal"),
    ]
    preview = preview_cleaning(verified, rows=["cafÃ©", "caf√©"])

    assert preview.covered == 1
    assert preview.solutions == ((LATIN1_PLAN,),)
    assert preview.verified == (
        VerifiedRow("MontrÃ©al", "Montréal", True),
        VerifiedRow("Montr√©al", "Montréal", False),
    )
    assert preview.rows == (
        RowPreview("cafÃ©", "café", ("café",), False),
        RowPreview("caf√©", "caf√©", ("caf√©",), False),
    )


def test_keep_rows_must_stay_unchanged():
    # The only candidate plan would rewrite the kept row "schÃ¶n", so it is
    # inadmissible and nothing gets cleaned.
    preview = preview_cleaning([("cafÃ©", "café")], keep=["schÃ¶n"], rows=["cafÃ©"])

    assert preview.covered == 0
    assert preview.solutions == ((),)
    assert preview.verified == (VerifiedRow("cafÃ©", "café", False),)
    assert preview.rows == (RowPreview("cafÃ©", "cafÃ©", ("cafÃ©",), False),)


def test_fewest_plans_wins_among_equal_coverage():
    # Both {Windows plan} and {Windows plan, Latin-1 plan} cover both
    # verified pairs, but the smaller set wins.
    verified = [
        ("cafÃ© au lait", "café au lait"),
        ("rockâ€™nâ€™roll", "rock'n'roll"),
    ]
    preview = preview_cleaning(verified)

    assert preview.covered == 2
    assert preview.solutions == ((WINDOWS_PLAN,),)


def test_nothing_verified_means_nothing_changes():
    preview = preview_cleaning([], rows=["cafÃ©", "plain"])

    assert preview.covered == 0
    assert preview.solutions == ((),)
    assert preview.rows == (
        RowPreview("cafÃ©", "cafÃ©", ("cafÃ©",), False),
        RowPreview("plain", "plain", ("plain",), False),
    )


def test_preview_types_are_exported():
    preview = preview_cleaning([("cafÃ©", "café")])
    assert isinstance(preview, CleaningPreview)
    assert isinstance(preview.verified[0], VerifiedRow)
