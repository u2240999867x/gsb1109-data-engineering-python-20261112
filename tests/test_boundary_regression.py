"""
Boundary regression tests for segmentation-dependent fixes in ftfy.

The same long field can be cleaned differently depending on how the input is
split before it reaches ftfy. These tests pin down that behavior around a
single, tightly-clustered field in which three boundary-sensitive features
sit next to each other:

1. a combining-character sequence (NFD text that only NFC can compose),
2. double mojibake that needs two encode/decode rounds to converge, and
3. an HTML entity that is decoded as one regex match.

Three different "cut" mechanisms are kept explicitly separate:

- transport chunking: the transport layer hands ftfy two independent strings,
  so each chunk is fixed with a fresh config and the boundary itself carries
  no semantics;
- logical line breaks: a real ``\\n`` that ``fix_text`` treats as a segment
  delimiter and keeps in the output;
- ``max_decode_length``: a hard length cap that splits a single logical line,
  implemented in ``fix_text`` itself and therefore sharing its config state.

The tests first prove the equivalence that *should* hold when logical
segmentation is preserved, then provide the shortest witnesses where changing
the segmentation actually changes the result. Each shrunk witness keeps the
fix round that causes it and the offset of the cut in the original field, so
the first changed step can be attributed to text repair, the entity policy,
or the segmentation strategy itself.
"""

import unicodedata

import pytest

from ftfy import ExplanationStep, TextFixerConfig, fix_and_explain, fix_text, fix_text_segment

# --- The clustered field ---------------------------------------------------
#
# Offsets are codepoint offsets into FIELD, recorded before any shrinking so
# that a reduced witness can always be mapped back to the original field.
NFD_E_ACUTE = chr(0x65) + chr(0x301)  # "e" + combining acute, offsets 0..2
DOUBLE_MOJIBAKE = "ÃƒÂ©"  # double-encoded "é", offsets 2..6
HTML_ENTITY = "&amp;"  # offsets 6..11
FIELD = NFD_E_ACUTE + DOUBLE_MOJIBAKE + HTML_ENTITY

# Cause category -> (inclusive start, exclusive end) in the original field.
CAUSE_SPANS = {
    "normalization": (0, 2),
    "encoding": (2, 6),
    "entity": (6, 11),
}
SAFE_BOUNDARIES = (2, 6)

CONFIG = TextFixerConfig()


# --- Small boundary harness ------------------------------------------------


def _first_divergence(left_output, right_output):
    """
    Return the first codepoint offset at which two outputs differ, or None.

    When one output is a prefix of the other, the first differing offset is
    the length of the shorter output.
    """
    for index, (left_char, right_char) in enumerate(zip(left_output, right_output)):
        if left_char != right_char:
            return index
    if len(left_output) != len(right_output):
        return min(len(left_output), len(right_output))
    return None


def _transport_chunk(text, cut):
    """
    Model a transport-layer split: two chunks fixed independently with fresh
    configs, then concatenated without any delimiter.
    """
    left, right = text[:cut], text[cut:]
    fixed_left, plan_left = fix_and_explain(left, CONFIG)
    fixed_right, plan_right = fix_and_explain(right, CONFIG)
    return fixed_left + fixed_right, (plan_left, plan_right)


def _logical_cut(text, cut):
    """
    Model a logical line break: insert a real ``\\n`` and let ``fix_text``
    segment on it. The newline is removed again before comparing, so only the
    effect of segmentation remains.
    """
    broken = text[:cut] + "\n" + text[cut:]
    return fix_text(broken).replace("\n", "")


def _hard_cut(text, limit):
    """Model a real ``max_decode_length`` split inside one logical line."""
    return fix_text(text, max_decode_length=limit)


def _forced_equal_chunks(text, size):
    """
    Fix independently in fixed-size codepoint chunks, the segmentation that
    ``max_decode_length`` is specified to produce.
    """
    return "".join(
        fix_text_segment(text[index : index + size]) for index in range(0, len(text), size)
    )


def _cause_at(cut):
    """Attribute an interior cut to the feature whose span it falls inside."""
    for cause, (start, end) in CAUSE_SPANS.items():
        if start < cut < end:
            return cause
    return None


def _shrink_to_witness(field, cut):
    """
    Shrink a divergent field to its shortest contiguous window while keeping
    the same divergence, without deleting characters from inside a feature.

    Returns the witness, the relative cut inside it, and the offset of the
    cut in the original field. The original offset is preserved even though
    the surrounding features have been trimmed away.
    """
    start, end = 0, len(field)
    whole = fix_text_segment(field)

    def window_diverges(window_start, window_end):
        witness = field[window_start:window_end]
        relative_cut = cut - window_start
        chunked = fix_text_segment(witness[:relative_cut]) + fix_text_segment(
            witness[relative_cut:]
        )
        return chunked != fix_text_segment(witness)

    assert _transport_chunk(field, cut)[0] != whole
    while end - 1 > cut and window_diverges(start, end - 1):
        end -= 1
    while start + 1 < cut and window_diverges(start + 1, end):
        start += 1

    witness = field[start:end]
    return witness, cut - start, cut


# --- Equivalence that must hold when segmentation is preserved -------------


def test_safe_boundary_keeps_logical_and_transport_outputs_equivalent():
    """
    A cut on a feature boundary must not change the result, whether it is a
    transport split or a real logical line break. This is the equivalence a
    well-behaved pipeline is entitled to rely on.
    """
    whole = fix_text_segment(FIELD)
    for cut in SAFE_BOUNDARIES:
        transport_output, _ = _transport_chunk(FIELD, cut)
        logical_output = _logical_cut(FIELD, cut)
        assert transport_output == whole
        assert logical_output == whole


def test_max_decode_length_is_a_real_segmentation_not_a_logical_break():
    """
    A hard length cap performs the same fixed-size segmentation as fixing
    equal-sized transport chunks independently (with no '<' in the field, the
    entity policy cannot flip). It must not be confused with a line break.
    """
    for limit in range(1, len(FIELD)):
        assert _hard_cut(FIELD, limit) == _forced_equal_chunks(FIELD, limit)

    # A logical newline at a safe boundary is equivalent; the same offset used
    # as a hard cap is not, proving the two mechanisms are distinguished.
    assert _logical_cut(FIELD, SAFE_BOUNDARIES[0]) == fix_text_segment(FIELD)
    assert _hard_cut(FIELD, SAFE_BOUNDARIES[0]) != fix_text_segment(FIELD)


def test_pinned_entity_policy_makes_line_segmentation_equivalent():
    """
    Equivalence across logical segments requires the entity policy to be
    pinned. With an explicit policy, fixing the whole line and fixing the
    segments separately agree. The "auto" policy is exempt and tested below.
    """
    field = "<p>\n&amp;"
    for policy in (True, False):
        whole = fix_text(field, unescape_html=policy)
        per_line = "\n".join(
            fix_text_segment(segment, unescape_html=policy) for segment in field.split("\n")
        )
        assert whole == per_line


# --- Shortest witnesses that segmentation really changes the result --------


@pytest.mark.parametrize(
    ("witness", "cut", "cause", "first_offset", "original_cut"),
    [
        (NFD_E_ACUTE, 1, "normalization", 0, 1),
        (DOUBLE_MOJIBAKE, 2, "encoding", 0, 4),
        (HTML_ENTITY, 1, "entity", 1, 7),
    ],
)
def test_minimal_witnesses_diverge_only_at_their_cut(
    witness, cut, cause, first_offset, original_cut
):
    """Each feature alone is a minimal witness for its cause."""
    whole, whole_plan = fix_and_explain(witness)
    chunked, (left_plan, right_plan) = _transport_chunk(witness, cut)
    assert chunked != whole
    # The entity case is a pure prefix difference (the decoded char is kept);
    # the other two differ at the very first codepoint.
    assert _first_divergence(whole, chunked) == first_offset
    assert _cause_at(original_cut) == cause
    assert whole_plan is not None
    assert left_plan is not None and right_plan is not None


def test_combining_mark_witness_is_a_normalization_difference():
    """
    Split an NFD base/combining pair: the whole string composes via NFC while
    the chunk containing the bare combining mark cannot compose, so the two
    outputs differ only by Unicode normalization and are NFC-equivalent.
    """
    witness = NFD_E_ACUTE
    whole, plan = fix_and_explain(witness)
    chunked, _ = _transport_chunk(witness, 1)

    assert [hex(ord(char)) for char in whole] == ["0xe9"]
    assert [hex(ord(char)) for char in chunked] == ["0x65", "0x301"]
    assert ExplanationStep("normalize", "NFC") in plan
    assert unicodedata.normalize("NFC", whole) == unicodedata.normalize("NFC", chunked)
    assert fix_text_segment(witness, normalization=None) == witness


def test_double_mojibake_witness_keeps_its_first_repair_round():
    """
    Double mojibake needs two encode/decode rounds in one segment. A split
    after the first round leaves each chunk fixable by exactly one round, so
    the first repair round survives in the output and one more pass converges
    to the whole-string result.
    """
    witness = DOUBLE_MOJIBAKE
    whole, whole_plan = fix_and_explain(witness)
    chunked, (left_plan, right_plan) = _transport_chunk(witness, 2)

    assert whole == "é"
    assert whole_plan == [
        ExplanationStep("encode", "sloppy-windows-1252"),
        ExplanationStep("decode", "utf-8"),
        ExplanationStep("encode", "latin-1"),
        ExplanationStep("decode", "utf-8"),
    ]
    assert chunked == "Ã©"
    # Both halves still carry one encoding round: the first round is retained.
    assert left_plan == [
        ExplanationStep("encode", "sloppy-windows-1252"),
        ExplanationStep("decode", "utf-8"),
    ]
    assert right_plan == [
        ExplanationStep("encode", "latin-1"),
        ExplanationStep("decode", "utf-8"),
    ]
    # The leftover one-round mojibake converges on a single additional pass.
    assert fix_text_segment(chunked) == whole


def test_entity_witness_differs_by_the_unescape_step():
    """
    An entity split by a transport cut cannot match the entity regex in
    either chunk, so the only changed step is the unescape_html fix.
    """
    witness = HTML_ENTITY
    whole, whole_plan = fix_and_explain(witness)
    chunked, (left_plan, right_plan) = _transport_chunk(witness, 1)

    assert whole == "&"
    assert whole_plan == [ExplanationStep("apply", "unescape_html")]
    assert chunked == witness
    assert left_plan == [] and right_plan == []


def test_shrinker_preserves_original_offsets_and_divergence():
    """
    Shrinking a cut of the full field yields a contiguous window that still
    diverges, while the recorded cut stays at its original field offset and
    maps to the correct cause.
    """
    expected_windows = {
        1: (0, 2),
        3: (2, 4),
        4: (3, 6),
        5: (2, 6),
        7: (6, 11),
        10: (6, 11),
    }
    for cut, (start, end) in expected_windows.items():
        witness, relative_cut, original_cut = _shrink_to_witness(FIELD, cut)
        assert witness == FIELD[start:end]
        assert original_cut == cut
        assert _cause_at(original_cut) is not None

        whole = fix_text_segment(witness)
        chunked = fix_text_segment(witness[:relative_cut]) + fix_text_segment(
            witness[relative_cut:]
        )
        assert chunked != whole
        # The locator still pins a concrete first-divergence offset inside the
        # shrunk witness, and it lies within the feature that owns the cut.
        span_start, span_end = CAUSE_SPANS[_cause_at(original_cut)]
        local_span = (span_start - start, span_end - start)
        first_offset = _first_divergence(whole, chunked)
        assert first_offset is not None
        assert local_span[0] <= first_offset < local_span[1]


def test_every_interior_cut_is_locatable_and_attributed():
    """
    Sweep every cut of the clustered field: the locator must find the first
    divergent offset whenever a feature is split, and boundary cuts must stay
    equivalent.
    """
    whole = fix_text_segment(FIELD)
    for cut in range(1, len(FIELD)):
        chunked, _ = _transport_chunk(FIELD, cut)
        if cut in SAFE_BOUNDARIES:
            assert chunked == whole
            assert _cause_at(cut) is None
        else:
            assert chunked != whole
            assert _first_divergence(whole, chunked) is not None
            assert _cause_at(cut) in CAUSE_SPANS


# --- Entity policy vs. segmentation as distinct first changes --------------


def test_auto_entity_policy_flip_is_distinct_from_segmentation():
    """
    A transport cut can separate a '<' from an entity in the same logical
    field. The whole field keeps the entity (auto mode disables unescaping),
    while the entity-only chunk decodes it. The plans differ first at the
    unescape_html step, not at an encoding or normalization step.
    """
    field = "<p>&amp;x"
    whole, whole_plan = fix_and_explain(field)
    chunked, (left_plan, right_plan) = _transport_chunk(field, 3)

    assert whole == field
    assert whole_plan == []
    assert chunked == "<p>&x"
    assert left_plan == []
    assert right_plan == [ExplanationStep("apply", "unescape_html")]


def test_fix_text_carries_entity_policy_state_across_logical_segments():
    """
    Inside fix_text, disabling unescaping for an HTML-looking segment leaks
    into later segments, whereas fixing each segment with a fresh config
    decodes the entity. This pinpoints that the divergence is a policy state
    effect, not the line break itself.
    """
    field = "<p>\n&amp;"
    stateful = fix_text(field)
    fresh = fix_text_segment("<p>\n") + fix_text_segment("&amp;")

    assert stateful == "<p>\n&amp;"
    assert fresh == "<p>\n&"
    # Pinning the policy removes the state-driven difference.
    assert fix_text(field, unescape_html=True) == fresh


def test_logical_newline_inside_a_feature_diverges():
    """
    The shortest logical-segmentation witness: a newline placed inside the
    double-mojibake feature breaks its two-round repair, while a newline on a
    feature boundary is harmless.
    """
    # Minimal standalone witness: the newline is preserved, and rejoining the
    # segments leaves the one-round mojibake "Ã©" instead of the converged "é".
    minimal = DOUBLE_MOJIBAKE[:2] + "\n" + DOUBLE_MOJIBAKE[2:]
    minimal_output = fix_text(minimal)
    assert minimal_output == "Ã\n©"
    assert minimal_output.replace("\n", "") != fix_text_segment(DOUBLE_MOJIBAKE)

    # The same cut inside the clustered field leaves the other two features
    # repaired and only the split mojibake double-encoded.
    safe = _logical_cut(FIELD, SAFE_BOUNDARIES[0])
    broken = _logical_cut(FIELD, 3)
    assert safe == fix_text_segment(FIELD)
    assert broken != fix_text_segment(FIELD)
    assert broken == "éÃƒÂ©&"
