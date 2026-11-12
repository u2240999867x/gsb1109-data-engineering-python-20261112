.. _evidence:

Tracing evidence for the heuristic
==================================

.. module:: ftfy.evidence

ftfy's badness heuristic (see :ref:`heuristic`) tells you *that* text looks
like mojibake, and ``fix_and_explain`` tells you *which steps* would fix it.
The ``ftfy.evidence`` module connects the two: it runs the same fixing
pipeline, and at each stage -- the input, and the text after each fix that
changed it -- it records which heuristic signals matched, and maps each match
back to the code points of the *original input* that it came from.

This is what lets an editor, looking at a cleaned-up field such as a title,
click on a suspicious word and be taken back to the exact characters in the
original field that made ftfy suspicious.

Collecting evidence is opt-in. When you don't ask for it, none of this code
runs, and the fixed text and badness scores are exactly what they have always
been::

    >>> import ftfy
    >>> evidence = ftfy.fix_text_with_evidence('schÃ¶n')
    >>> evidence.text
    'schön'
    >>> evidence.score
    1
    >>> hit = evidence.stages[0].signals[0]
    >>> hit.signal, hit.span, hit.input_ranges
    ('capital_then_numeric', (3, 5), ((3, 5),))
    >>> print(hit.describe())
    signal 'capital_then_numeric' matched 'Ã¶' at code points [3, 5) of this stage, contributing 1 to the badness score; traced to input code points [3, 5)

Each signal's ``score`` is its contribution to the badness of the text at
that stage, so the signals add up to a complete explanation of the score.

Alignments
----------

Mapping a match back to the input is not always a matter of keeping the same
offsets, because fixes insert, delete, merge, and expand characters. The
``Alignment`` class tracks where each code point of a fixed string came
from, including the cases where text doesn't line up one-to-one:

- Multiple input characters that merge into one (an HTML entity such as
  ``&amp;`` becoming ``&``, a surrogate pair becoming an emoji, or NFC
  normalization composing two code points into one): the output character
  maps to the whole input range it was made from.
- One input character that expands into many (a ligature such as ``ﬁ``
  becoming ``fi``, or an entity such as ``&ThickSpace;`` that decodes to
  two characters): each output character maps to its source.
- Repeated text where only one occurrence changed (two identical words, of
  which only the second was mojibake): the change is aligned to the
  occurrence that actually changed, not the first one that looks the same.
- Rewrites where a per-character correspondence can't be determined: the
  evidence keeps the entire range of input that was involved, and marks the
  mapping as ``ambiguous`` instead of guessing.

.. autofunction:: ftfy.evidence.fix_text_with_evidence

.. autoclass:: ftfy.evidence.FixEvidence

.. autoclass:: ftfy.evidence.StageEvidence

.. autoclass:: ftfy.evidence.SignalEvidence

.. autoclass:: ftfy.evidence.Alignment
   :members:
