"""
`ftfy.badness` contains a heuristic that detects likely mojibake.

This heuristic signals to ftfy which segments of text need to be fixed, and
also indicates when the text can stop being fixed.

The design of this heuristic is that we categorize the approximately 400
Unicode characters that occur in UTF-8 mojibake, specifically the characters
that come from mixing up UTF-8 with the other encodings we support. We
identify sequences and contexts of these characters that are much more likely
to be mojibake than intended strings, such as lowercase accented letters
followed immediately by currency symbols.
"""

import re
import warnings
from typing import NamedTuple


# There are only a few hundred characters that occur in known UTF-8 mojibake, and we can
# characterize them:

MOJIBAKE_CATEGORIES = {
    # Characters that appear in many different contexts. Sequences that contain
    # them are not inherently mojibake
    "common": (
        "\N{NO-BREAK SPACE}"
        "\N{SOFT HYPHEN}"
        "\N{MIDDLE DOT}"
        "\N{ACUTE ACCENT}"
        "\N{EN DASH}"
        "\N{EM DASH}"
        "\N{HORIZONTAL BAR}"
        "\N{HORIZONTAL ELLIPSIS}"
        "\N{RIGHT SINGLE QUOTATION MARK}"
    ),
    # the C1 control character range, which have no uses outside of mojibake anymore
    "c1": "\x80-\x9f",
    # Characters that are nearly 100% used in mojibake
    "bad": (
        "\N{BROKEN BAR}"
        "\N{CURRENCY SIGN}"
        "\N{DIAERESIS}"
        "\N{NOT SIGN}"
        "\N{MACRON}"
        "\N{CEDILLA}"
        "\N{LATIN SMALL LETTER F WITH HOOK}"
        "\N{MODIFIER LETTER CIRCUMFLEX ACCENT}"  # it's not a modifier
        "\N{CARON}"
        "\N{BREVE}"
        "\N{OGONEK}"
        "\N{SMALL TILDE}"
        "\N{DAGGER}"
        "\N{DOUBLE DAGGER}"
        "\N{PER MILLE SIGN}"
        "\N{REVERSED NOT SIGN}"
        "\N{LOZENGE}"
        "\ufffd"
        # Theoretically these would appear in 'numeric' contexts, but when they
        # co-occur with other mojibake characters, it's not really ambiguous
        "\N{FEMININE ORDINAL INDICATOR}"
        "\N{MASCULINE ORDINAL INDICATOR}"
    ),
    # Characters used in legalese
    "law": (
        "\N{PILCROW SIGN}"
        "\N{SECTION SIGN}"
    ),
    "currency": (
        "\N{CENT SIGN}"
        "\N{POUND SIGN}"
        "\N{YEN SIGN}"
        "\N{PESETA SIGN}"
        "\N{EURO SIGN}"
    ),
    "start_punctuation": (
        "\N{INVERTED EXCLAMATION MARK}"
        "\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}"
        "\N{INVERTED QUESTION MARK}"
        "\N{COPYRIGHT SIGN}"
        "\N{GREEK TONOS}"
        "\N{GREEK DIALYTIKA TONOS}"
        "\N{LEFT SINGLE QUOTATION MARK}"
        "\N{SINGLE LOW-9 QUOTATION MARK}"
        "\N{LEFT DOUBLE QUOTATION MARK}"
        "\N{DOUBLE LOW-9 QUOTATION MARK}"
        "\N{BULLET}"
        "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}"
        "\uf8ff"  # OS-specific symbol, usually the Apple logo
    ),
    "end_punctuation": (
        "\N{REGISTERED SIGN}"
        "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"
        "\N{DOUBLE ACUTE ACCENT}"
        "\N{RIGHT DOUBLE QUOTATION MARK}"
        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
        "\N{TRADE MARK SIGN}"
    ),
    "numeric": (
        "\N{SUPERSCRIPT TWO}"
        "\N{SUPERSCRIPT THREE}"
        "\N{SUPERSCRIPT ONE}"
        "\N{PLUS-MINUS SIGN}"
        "\N{VULGAR FRACTION ONE QUARTER}"
        "\N{VULGAR FRACTION ONE HALF}"
        "\N{VULGAR FRACTION THREE QUARTERS}"
        "\N{MULTIPLICATION SIGN}"
        "\N{MICRO SIGN}"
        "\N{DIVISION SIGN}"
        "\N{FRACTION SLASH}"
        "\N{PARTIAL DIFFERENTIAL}"
        "\N{INCREMENT}"
        "\N{N-ARY PRODUCT}"
        "\N{N-ARY SUMMATION}"
        "\N{SQUARE ROOT}"
        "\N{INFINITY}"
        "\N{INTERSECTION}"
        "\N{INTEGRAL}"
        "\N{ALMOST EQUAL TO}"
        "\N{NOT EQUAL TO}"
        "\N{IDENTICAL TO}"
        "\N{LESS-THAN OR EQUAL TO}"
        "\N{GREATER-THAN OR EQUAL TO}"
        "\N{NUMERO SIGN}"
    ),
    # Letters that might be used to make emoticon faces (kaomoji), and
    # therefore might need to appear in more improbable-looking contexts.
    #
    # These are concatenated character ranges for use in a regex. I know
    # they look like faces themselves. I think expressing the ranges like
    # this helps to illustrate why we need to be careful with these
    # characters.
    "kaomoji": (
        "Ò-Ö"
        "Ù-Ü"
        "ò-ö"
        "ø-ü"
        "\N{LATIN CAPITAL LETTER O WITH DOUBLE ACUTE}"
        "\N{LATIN CAPITAL LETTER O WITH MACRON}"
        "\N{LATIN CAPITAL LETTER U WITH MACRON}"
        "\N{LATIN CAPITAL LETTER U WITH OGONEK}"
        "\N{DEGREE SIGN}"
    ),
    "upper_accented": (
        # LATIN CAPITAL LETTER A WITH GRAVE - LATIN CAPITAL LETTER N WITH TILDE
        "\xc0-\xd1"
        # skip capital O's and U's that could be used in kaomoji, but
        # include Ø because it's very common in Arabic mojibake:
        "\N{LATIN CAPITAL LETTER O WITH STROKE}"
        "\N{LATIN CAPITAL LETTER U WITH DIAERESIS}"
        "\N{LATIN CAPITAL LETTER Y WITH ACUTE}"
        "\N{LATIN CAPITAL LETTER A WITH BREVE}"
        "\N{LATIN CAPITAL LETTER A WITH MACRON}"
        "\N{LATIN CAPITAL LETTER A WITH OGONEK}"
        "\N{LATIN CAPITAL LETTER C WITH ACUTE}"
        "\N{LATIN CAPITAL LETTER C WITH CARON}"
        "\N{LATIN CAPITAL LETTER D WITH CARON}"
        "\N{LATIN CAPITAL LETTER D WITH STROKE}"
        "\N{LATIN CAPITAL LETTER E WITH OGONEK}"
        "\N{LATIN CAPITAL LETTER E WITH CARON}"
        "\N{LATIN CAPITAL LETTER E WITH MACRON}"
        "\N{LATIN CAPITAL LETTER E WITH DOT ABOVE}"
        "\N{LATIN CAPITAL LETTER G WITH BREVE}"
        "\N{LATIN CAPITAL LETTER G WITH CEDILLA}"
        "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
        "\N{LATIN CAPITAL LETTER I WITH MACRON}"
        "\N{LATIN CAPITAL LETTER K WITH CEDILLA}"
        "\N{LATIN CAPITAL LETTER L WITH ACUTE}"
        "\N{LATIN CAPITAL LETTER L WITH CARON}"
        "\N{LATIN CAPITAL LETTER L WITH STROKE}"
        "\N{LATIN CAPITAL LETTER L WITH CEDILLA}"
        "\N{LATIN CAPITAL LETTER N WITH ACUTE}"
        "\N{LATIN CAPITAL LETTER N WITH CARON}"
        "\N{LATIN CAPITAL LETTER N WITH CEDILLA}"
        "\N{LATIN CAPITAL LIGATURE OE}"
        "\N{LATIN CAPITAL LETTER R WITH CARON}"
        "\N{LATIN CAPITAL LETTER S WITH ACUTE}"
        "\N{LATIN CAPITAL LETTER S WITH CEDILLA}"
        "\N{LATIN CAPITAL LETTER S WITH CARON}"
        "\N{LATIN CAPITAL LETTER T WITH CEDILLA}"
        "\N{LATIN CAPITAL LETTER T WITH CARON}"
        "\N{LATIN CAPITAL LETTER U WITH RING ABOVE}"
        "\N{LATIN CAPITAL LETTER U WITH DOUBLE ACUTE}"
        "\N{LATIN CAPITAL LETTER Y WITH DIAERESIS}"
        "\N{LATIN CAPITAL LETTER Z WITH ACUTE}"
        "\N{LATIN CAPITAL LETTER Z WITH DOT ABOVE}"
        "\N{LATIN CAPITAL LETTER Z WITH CARON}"
        "\N{CYRILLIC CAPITAL LETTER GHE WITH UPTURN}"
    ),
    "lower_accented": (
        "\N{LATIN SMALL LETTER SHARP S}"
        # LATIN SMALL LETTER A WITH GRAVE - LATIN SMALL LETTER N WITH TILDE
        "\xe0-\xf1"
        # skip o's and u's that could be used in kaomoji
        "\N{LATIN SMALL LETTER A WITH BREVE}"
        "\N{LATIN SMALL LETTER A WITH OGONEK}"
        "\N{LATIN SMALL LETTER A WITH MACRON}"
        "\N{LATIN SMALL LETTER C WITH ACUTE}"
        "\N{LATIN SMALL LETTER C WITH CARON}"
        "\N{LATIN SMALL LETTER D WITH CARON}"
        "\N{LATIN SMALL LETTER D WITH STROKE}"
        "\N{LATIN SMALL LETTER E WITH OGONEK}"
        "\N{LATIN SMALL LETTER E WITH CARON}"
        "\N{LATIN SMALL LETTER E WITH MACRON}"
        "\N{LATIN SMALL LETTER E WITH DOT ABOVE}"
        "\N{LATIN SMALL LETTER G WITH BREVE}"
        "\N{LATIN SMALL LETTER G WITH CEDILLA}"
        "\N{LATIN SMALL LETTER I WITH OGONEK}"
        "\N{LATIN SMALL LETTER I WITH MACRON}"
        "\N{LATIN SMALL LETTER K WITH CEDILLA}"
        "\N{LATIN SMALL LETTER L WITH ACUTE}"
        "\N{LATIN SMALL LETTER L WITH CARON}"
        "\N{LATIN SMALL LETTER L WITH STROKE}"
        "\N{LATIN SMALL LETTER L WITH CEDILLA}"
        "\N{LATIN SMALL LIGATURE OE}"
        "\N{LATIN SMALL LETTER R WITH ACUTE}"
        "\N{LATIN SMALL LETTER S WITH ACUTE}"
        "\N{LATIN SMALL LETTER S WITH CEDILLA}"
        "\N{LATIN SMALL LETTER S WITH CARON}"
        "\N{LATIN SMALL LETTER T WITH CARON}"
        "\N{LATIN SMALL LETTER U WITH DIAERESIS}"
        "\N{LATIN SMALL LETTER Z WITH ACUTE}"
        "\N{LATIN SMALL LETTER Z WITH DOT ABOVE}"
        "\N{LATIN SMALL LETTER Z WITH CARON}"
        "\N{CYRILLIC SMALL LETTER GHE WITH UPTURN}"
        "\N{LATIN SMALL LIGATURE FI}"
        "\N{LATIN SMALL LIGATURE FL}"
    ),
    "upper_common": (
        "\N{LATIN CAPITAL LETTER THORN}"
        "\N{GREEK CAPITAL LETTER ALPHA}-\N{GREEK CAPITAL LETTER OMEGA}"
        # not included under 'accented' because these can commonly
        # occur at ends of words, in positions where they'd be detected
        # as mojibake
        "\N{GREEK CAPITAL LETTER ALPHA WITH TONOS}"
        "\N{GREEK CAPITAL LETTER EPSILON WITH TONOS}"
        "\N{GREEK CAPITAL LETTER ETA WITH TONOS}"
        "\N{GREEK CAPITAL LETTER IOTA WITH TONOS}"
        "\N{GREEK CAPITAL LETTER OMICRON WITH TONOS}"
        "\N{GREEK CAPITAL LETTER UPSILON WITH TONOS}"
        "\N{GREEK CAPITAL LETTER OMEGA WITH TONOS}"
        "\N{GREEK CAPITAL LETTER IOTA WITH DIALYTIKA}"
        "\N{GREEK CAPITAL LETTER UPSILON WITH DIALYTIKA}"
        "\N{CYRILLIC CAPITAL LETTER IO}-\N{CYRILLIC CAPITAL LETTER YA}"
    ),
    "lower_common": (
        # lowercase thorn does not appear in mojibake
        "\N{GREEK SMALL LETTER ALPHA}-\N{GREEK SMALL LETTER OMEGA}"
        "\N{GREEK SMALL LETTER ALPHA WITH TONOS}"
        "\N{GREEK SMALL LETTER EPSILON WITH TONOS}"
        "\N{GREEK SMALL LETTER ETA WITH TONOS}"
        "\N{GREEK SMALL LETTER IOTA WITH TONOS}"
        "\N{GREEK SMALL LETTER UPSILON WITH DIALYTIKA AND TONOS}"
        "\N{CYRILLIC SMALL LETTER A}-\N{CYRILLIC SMALL LETTER DZHE}"
    ),
    "box": (
        # omit the single horizontal line, might be used in kaomoji
        "│┌┐┘├┤┬┼"
        "\N{BOX DRAWINGS DOUBLE HORIZONTAL}-\N{BOX DRAWINGS DOUBLE VERTICAL AND HORIZONTAL}"
        "▀▄█▌▐░▒▓"
    ),
}


# We can now build a regular expression that detects unlikely juxtapositions
# of characters, mostly based on their categories.
#
# Another regular expression, which detects sequences that look more specifically
# like UTF-8 mojibake, appears in chardata.py.
#
# The rules are expressed as a list of (name, pattern) pairs, so that each
# match can be traced back to the rule -- the "signal" -- that produced it.
# Joining the patterns with "|" reproduces the alternation that ftfy has
# always used, in the same order, so the overall matches are unchanged.
#
# The patterns are verbose regular expressions, with whitespace added for
# somewhat more readability. Remember that the only spaces that count as
# literal spaces in these expressions are ones inside character classes
# (square brackets).

_BADNESS_RULES: list[tuple[str, str]] = [
    ("c1_control", r"[{c1}]"),
    (
        "symbol_then_bad",
        r"[{bad}{lower_accented}{upper_accented}{box}{start_punctuation}{end_punctuation}{currency}{numeric}{law}] [{bad}]",
    ),
    ("latin_common_bad", r"[a-zA-Z] [{lower_common}{upper_common}] [{bad}]"),
    (
        "bad_then_symbol",
        r"[{bad}] [{bad}{lower_accented}{upper_accented}{box}{start_punctuation}{end_punctuation}{currency}{numeric}{law}]",
    ),
    (
        "lower_then_upper_accented",
        r"[{lower_accented}{lower_common}{box}{end_punctuation}{currency}{numeric}] [{upper_accented}]",
    ),
    (
        "symbol_then_lower_accented",
        r"[{box}{end_punctuation}{currency}{numeric}] [{lower_accented}]",
    ),
    ("accented_then_currency", r"[{lower_accented}{box}{end_punctuation}] [{currency}]"),
    ("space_capital_currency", r"\s [{upper_accented}] [{currency}]"),
    ("capital_then_numeric", r"[{upper_accented}{box}] [{numeric}{law}]"),
    (
        "symbol_start_numeric",
        r"[{lower_accented}{upper_accented}{box}{currency}{end_punctuation}] [{start_punctuation}] [{numeric}]",
    ),
    (
        "end_then_start_punctuation",
        r"[{lower_accented}{upper_accented}{currency}{numeric}{box}{law}] [{end_punctuation}] [{start_punctuation}]",
    ),
    ("symbol_then_start_punctuation", r"[{currency}{numeric}{box}] [{start_punctuation}]"),
    ("lower_capital_punctuation", r"[a-z] [{upper_accented}] [{start_punctuation}{currency}]"),
    ("box_then_kaomoji", r"[{box}] [{kaomoji}]"),
    (
        "symbol_then_box",
        r"[{lower_accented}{upper_accented}{currency}{numeric}{start_punctuation}{end_punctuation}{law}] [{box}]",
    ),
    ("box_then_end_punctuation", r"[{box}] [{end_punctuation}]"),
    (
        "accented_punctuation_word",
        r"[{lower_accented}{upper_accented}] [{start_punctuation}{end_punctuation}] \w",
    ),
    # The ligature œ when not followed by an unaccented Latin letter
    ("oe_ligature_context", r"[Œœ][^A-Za-z]"),
    # Degree signs after capital letters
    ("capital_then_degree", r"[{upper_accented}]°"),
    # Common Windows-1252 2-character mojibake that isn't covered by the cases above
    (
        "windows_1252_pair",
        r"[ÂÃÎÐ][€œŠš¢£Ÿž\xa0\xad®©°·»{start_punctuation}{end_punctuation}–—´]",
    ),
    ("multiplication_superscript", r"× [²³]"),
    # Windows-1252 mojibake of Arabic words needs to include the 'common' characters.
    # To compensate, we require four characters to be matched.
    (
        "arabic_windows_1252",
        r"[ØÙ] [{common}{currency}{bad}{numeric}{start_punctuation}ŸŠ®°µ»]"
        r" [ØÙ] [{common}{currency}{bad}{numeric}{start_punctuation}ŸŠ®°µ»]",
    ),
    # Windows-1252 mojibake that starts 3-character sequences for some South Asian
    # alphabets
    ("south_asian_windows_1252", r"à[²µ¹¼½¾]"),
    # MacRoman mojibake that isn't covered by the cases above
    ("macroman_math", r"√[±∂†≠®™´≤≥¥µø]"),
    ("macroman_almost", r"≈[°¢]"),
    ("windows_1252_utf8_triple", r"‚Ä[ìîïòôúùû†°¢π]"),
    ("windows_1252_utf8_triple_low", r"‚[âó][àä°ê]"),
    # Windows-1251 mojibake of characters in the U+2000 range
    ("windows_1251_u2000", r"вЂ"),
    # Windows-1251 mojibake of Latin-1 characters and/or the Cyrillic alphabet.
    # Because the 2-character sequences involved here may be common, we require
    # seeing a 3-character sequence.
    (
        "windows_1251_triple",
        r"[ВГРС][{c1}{bad}{start_punctuation}{end_punctuation}{currency}°µ][ВГРС]",
    ),
    # A distinctive five-character sequence of Cyrillic letters, which can be
    # Windows-1251 mojibake on top of Latin-1 mojibake of Windows-1252 characters.
    # Require a Latin letter nearby.
    ("windows_1251_latin1_chain", r"ГўВЂВ.[A-Za-z ]"),
    # Windows-1252 encodings of 'à' and 'á', as well as \xa0 itself
    ("a0_mojibake", r"Ã[\xa0¡]"),
    ("trailing_a_mojibake", r"[a-z]\s?[ÃÂ][ ]"),
    ("leading_a_mojibake", r"^[ÃÂ][ ]"),
    # Cases where Â precedes a character as an encoding of exactly the same
    # character, and the character is common enough
    (
        "a_around_punctuation",
        r"[a-z.,?!{end_punctuation}] Â [ {start_punctuation}{end_punctuation}]",
    ),
    # Windows-1253 mojibake of characters in the U+2000 range
    ("windows_1253_u2000", r"β€[™\xa0Ά\xad®°]"),
    # Windows-1253 mojibake of Latin-1 characters and/or the Greek alphabet
    (
        "windows_1253_triple",
        r"[ΒΓΞΟ][{c1}{bad}{start_punctuation}{end_punctuation}{currency}°][ΒΓΞΟ]",
    ),
    # Windows-1257 mojibake of characters in the U+2000 range
    ("windows_1257_u2000", r"ā€"),
]


def _badness_pattern() -> str:
    """
    Combine the badness rules into a single alternation, substituting in the
    character categories. This is the same pattern ftfy has always used.
    """
    return "|".join(pattern for _name, pattern in _BADNESS_RULES).format(
        **MOJIBAKE_CATEGORIES
    )


def _badness_signal_pattern() -> str:
    """
    Combine the badness rules into an alternation where each rule is wrapped
    in a named group, so that a match can be attributed to a particular rule.
    """
    return "|".join(
        f"(?P<{name}>{pattern})" for name, pattern in _BADNESS_RULES
    ).format(**MOJIBAKE_CATEGORIES)


BADNESS_RE = re.compile(_badness_pattern(), re.VERBOSE)

# A variant of BADNESS_RE that attributes each match to the rule (signal)
# that produced it. It matches exactly the same text as BADNESS_RE.
BADNESS_SIGNAL_RE = re.compile(_badness_signal_pattern(), re.VERBOSE)


def sequence_weirdness(text: str) -> int:
    """
    This was the name of the heuristic used in ftfy 2.x through 5.x. As an
    attempt at compatibility with external code that calls the heuristic
    directly, we redirect to our new heuristic, :func:`badness`.
    """
    warnings.warn(
        "`sequence_weirdness()` is an old heuristic, and the current "
        "closest equivalent is `ftfy.badness.badness()`"
    )
    return badness(text)


def badness(text: str) -> int:
    """
    Get the 'badness' of a sequence of text, counting the number of unlikely
    character sequences. A badness greater than 0 indicates that some of it
    seems to be mojibake.
    """
    return len(BADNESS_RE.findall(text))


class SignalHit(NamedTuple):
    """
    A single piece of evidence for the badness heuristic: one match of one
    of the rules (signals) that make up :data:`BADNESS_RE`.

    - `signal`: the name of the rule that matched, such as "c1_control"
    - `span`: the (start, end) code point offsets of the match in the text
    - `matched`: the matched text itself
    - `score`: this match's contribution to the badness score (always 1,
      because :func:`badness` counts matches)
    """

    signal: str
    span: tuple[int, int]
    matched: str
    score: int


def signal_hits(text: str) -> list[SignalHit]:
    """
    Get the evidence behind :func:`badness`: a list of the signals that
    matched this text, in the order they were found, with the code point
    offsets where each one matched.

    The number of hits is the same as the result of ``badness(text)``, and
    each hit's `score` of 1 is its contribution to that total, so the hits
    add up to a complete explanation of the score.
    """
    hits = []
    for match in BADNESS_SIGNAL_RE.finditer(text):
        signal = match.lastgroup
        assert signal is not None  # every alternative is a named group
        hits.append(
            SignalHit(
                signal=signal,
                span=match.span(),
                matched=match.group(0),
                score=1,
            )
        )
    return hits


def is_bad(text: str) -> bool:
    """
    Returns true iff the given text looks like it contains mojibake.

    This can be faster than `badness`, because it returns when the first match
    is found to a regex instead of counting matches. Note that as strings get
    longer, they have a higher chance of returning True for `is_bad(string)`.
    """
    return bool(BADNESS_RE.search(text))
