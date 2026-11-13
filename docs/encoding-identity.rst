Encoding identity across filters, detection, and persisted names
================================================================

A pipeline that turns a detected encoding into a directory or file name is
asking three different questions, and chardet answers them at three different
moments.  Keeping them apart is what makes the answer explainable when a
re-detect disagrees with what is already on disk.

1. **Which codec did the filter select?**  ``include_encodings``,
   ``exclude_encodings``, and ``encoding_era`` are resolved by
   :func:`~chardet.registry.normalize_encodings` before detection runs.  A
   familiar spelling can land on a *different* real codec: ``shift_jis``
   selects ``shift_jis_2004``, ``gb2312`` selects ``gb18030``,
   ``iso-8859-11`` selects ``tis-620``.
2. **What text did detection validate?**  The internal codec decodes the
   examination window and the pipeline scores and filters against that.
3. **What name is persisted, and what does it decode to?**  Rendering applies
   the superset remap and the display-name map.  Both are checked against the
   window, but only for *decodability*: the persisted name may decode the same
   bytes to different text than the internal codec produced.

Run ``uv run python scripts/audit_encoding_identity.py`` for the live tables
this page summarizes.  The whole audit is deterministic and needs no test
data.

Layer 1: aliases select a real codec
------------------------------------

The registry normalizes every filter entry before the candidate set is built.
The normalization is not cosmetic — it is a codec choice:

.. list-table:: Familiar whitelist spellings and the codec they select
   :header-rows: 1
   :widths: 24 24 24 24

   * - Whitelist spelling
     - Canonical entry
     - Python codec run
     - Persisted name
   * - ``shift_jis``, ``sjis``, ``shift-jis``
     - ``shift_jis_2004``
     - ``shift_jis_2004``
     - ``SHIFT_JIS``
   * - ``cp932``, ``windows-31j``
     - ``cp932``
     - ``cp932``
     - ``CP932``
   * - ``euc-jp``, ``euc_jis_2004``
     - ``euc_jis_2004``
     - ``euc_jis_2004``
     - ``EUC-JP``
   * - ``big5``, ``cp950``, ``big5hkscs``
     - ``big5hkscs``
     - ``big5hkscs``
     - ``Big5``
   * - ``gb2312``, ``gbk``, ``cp936``, ``gb18030``
     - ``gb18030``
     - ``gb18030``
     - ``GB18030``
   * - ``iso-2022-jp``, ``iso2022_jp_2``
     - ``iso2022_jp_2``
     - ``iso2022_jp_2``
     - ``ISO-2022-JP``
   * - ``iso-8859-11``, ``tis-620``
     - ``tis-620``
     - ``tis-620``
     - ``TIS-620``
   * - ``latin-1``, ``iso-8859-1``
     - ``iso8859-1``
     - ``iso8859-1``
     - ``ISO-8859-1``

Two consequences for a persistence layer:

- ``include_encodings=["shift_jis"]`` does **not** restrict detection to
  plain ``shift_jis``.  JIS X 0213 rows that plain ``shift_jis`` rejects are
  accepted, and the result is reported as ``SHIFT_JIS`` — a display name that
  resolves to the narrower codec.  ``include_encodings=["cp932"]`` is the
  spelling that selects the actual CP932 codec, and it will *reject* those
  same rows.
- ``iso-8859-11`` is an alias of ``tis-620``; a whitelist listing both
  normalizes to one entry, so it does not widen the candidate set.

Layer 2 and 3: the persisted name is not the identity
-----------------------------------------------------

The internal codec and the persisted name usually agree.  They can differ in
two independent ways, and both are silent from the name alone.

**Superset remap.**  ``prefer_superset=True`` rewrites a subset result to its
Windows superset.  The remap is skipped when the superset cannot decode the
window, but when both decode, the texts can differ.  The discriminating byte
is the witness:

.. list-table:: Superset text witnesses (one byte, both codecs decode)
   :header-rows: 1
   :widths: 16 16 14 14 14 26

   * - Persisted name
     - Members
     - Witness
     - Subset reads
     - Superset reads
     - Witness kind
   * - ``Windows-1252``
     - ``cp1252`` / ``iso8859-1``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1250``
     - ``cp1250`` / ``iso8859-2``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1251``
     - ``cp1251`` / ``iso8859-5``
     - ``80``
     - ``\x80``
     - ``Ђ``
     - text
   * - ``Windows-1253``
     - ``cp1253`` / ``iso8859-7``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1254``
     - ``cp1254`` / ``iso8859-9``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1255``
     - ``cp1255`` / ``iso8859-8``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1256``
     - ``cp1256`` / ``iso8859-6``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1257``
     - ``cp1257`` / ``iso8859-13``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``CP874``
     - ``cp874`` / ``tis-620``
     - ``80``
     - ``\x80``
     - ``€``
     - text
   * - ``Windows-1252``
     - ``ascii`` / ``cp1252``
     - ``80``
     - *error*
     - ``€``
     - acceptance
   * - ``CP949``
     - ``cp949`` / ``euc_kr``
     - ``8141``
     - *error*
     - ``갂``
     - acceptance

The last two rows are the **fallback cases**: no byte string decrypts to
different text under both codecs, so decoded text alone cannot tell them
apart.

- ``ascii`` accepts no byte at or above ``0x80``, so against ``cp1252`` or
  ``iso8859-1`` every shared byte necessarily decodes to the same ASCII text.
  The distinction is acceptance, not text; the scope is the whole single-byte
  domain.
- ``cp949`` and ``euc_kr`` agree on **every** two-byte sequence both accept
  (8225 strings, zero text differences); ``cp949`` only adds 8823 sequences
  it alone accepts.  The scope is therefore the shared decode domain; the
  witness ``81 41`` is a CP949-only extension byte that ``euc_kr`` rejects.

**Display-name narrowing.**  ``compat_names=True`` reports four internal
names under a display name that Python resolves to a *narrower* codec:

.. list-table:: Narrowing display names
   :header-rows: 1
   :widths: 20 22 16 22 20

   * - Internal
     - Persisted
     - Persisted codec
     - Text witness
     - Acceptance witness
   * - ``big5hkscs``
     - ``Big5``
     - ``big5``
     - ``c6 a1``: ``①`` / ``ヾ``
     - ``87 40``: ``䏰`` / error
   * - ``shift_jis_2004``
     - ``SHIFT_JIS``
     - ``shift_jis``
     - ``5c``: ``¥`` / ``\``
     - ``81 ad``: ``＇`` / error
   * - ``euc_jis_2004``
     - ``EUC-JP``
     - ``euc_jp``
     - none in the 1–3 byte domain
     - ``a2 af``: ``＇`` / error; ``8f a1 a1``: ``𠂉`` / error
   * - ``iso2022_jp_2``
     - ``ISO-2022-JP``
     - ``iso2022_jp``
     - none in the ESC-designation scan
     - ``1b 24 28 41 21 21 1b 28 42``: ``U+3000`` / error

``euc_jis_2004`` and ``iso2022_jp_2`` have no text witness in the scanned
domain: every string both codecs accept decodes identically, over the
single-byte set, the two-byte rows, and the ``0x8E``/``0x8F`` three-byte
planes.  Their difference is purely acceptance — JIS X 0213 rows the
display codec rejects (``8f a1 a1`` is EUC-JP plane-2 ``𠂉``).  That is also
why rendering keeps the internal name when the display codec cannot decode
the window; see ``decodes_under_public_name``.

The same bytes, three entry points
----------------------------------

``detect``, ``detect_all``, and ``UniversalDetector`` are adapters over one
:class:`~chardet._request.DetectionRequest`, so on the same options and the
same examined bytes they return the same verdict.  They can disagree when
they see *different amounts of the caller's input past ``max_bytes``*,
because that difference sets ``input_truncated``, which two stages consume:
the BOM stage's UTF-32 alignment check and the decode-safety flip.

Both families below use the same original bytes for all three entries.  The
streaming caller follows the loop the docs show: feed a chunk, then stop as
soon as ``done`` is true.

**Family A — a UTF-32 BOM whose window ends mid-code-unit.**

::

   original (13 bytes) = ff fe 00 00 | 41 00 00 00 | 42 00 00 00 | 43
   examined window (max_bytes=9) = ff fe 00 00 | 41 00 00 00 | 42
                                 payload 5 bytes: not a multiple of 4

.. list-table::
   :header-rows: 1
   :widths: 34 12 12 14 16

   * - Constraints
     - ``detect``
     - ``detect_all``
     - stream (``done``)
     - stream ``input_truncated``
   * - defaults
     - ``UTF-32``
     - ``UTF-32``
     - ``UTF-16``
     - False
   * - ``include=["utf-16"]``
     - ``None``
     - ``None``
     - ``UTF-16``
     - False
   * - ``include=["utf-32"]``
     - ``UTF-32``
     - ``UTF-32``
     - ``None``
     - False
   * - ``include=["utf-16","utf-32"]``
     - ``UTF-32``
     - ``UTF-32``
     - ``UTF-16``
     - False
   * - ``exclude=["utf-32"]``
     - ``None``
     - ``None``
     - ``UTF-16``
     - False

``detect`` sees bytes beyond the cap (``input_truncated=True``), so the BOM
stage accepts the 1–3 byte partial code unit and reports UTF-32.  A stream
that stops exactly at the cap has ``input_truncated=False``, the BOM stage
rejects the misaligned mark, and the shorter UTF-16 BOM wins.

Family A needs **no filter at all**: ``max_bytes=9`` plus a caller whose
bytes continue past the cap is sufficient.  The include/exclude rows show the
same asymmetry surviving when a filter removes one of the two codecs — the
losing side then reports ``encoding=None`` instead of the other name.  No
``encoding_era`` value changes the outcome, because UTF-16 and UTF-32 are both
``MODERN_WEB``.

**Family B — an ASCII window with a dangling multi-byte tail.**

::

   original (14 bytes) = 68 65 6c 6c 6f 20 6d 61 6d e1 | 5a 5a 5a 5a
   examined window (max_bytes=10) = "hello mam" + 0xE1

Here the decode-safety flip is what changes hands.  With
``include=["utf-8","cp720"]``:

.. list-table::
   :header-rows: 1
   :widths: 34 14 14 14 18

   * - Constraint
     - ``detect``
     - ``detect_all``
     - stream (``done``)
     - Why
   * - defaults
     - ``Windows-1250``
     - ``Windows-1250``
     - ``Windows-1250``
     - no split
   * - ``include=["utf-8","cp720"]``
     - ``utf-8``
     - ``utf-8``
     - ``cp720``
     - flip fires only with ``input_truncated=False``
   * - ``include=["utf-8","mac-iceland"]``
     - ``utf-8``
     - ``utf-8``
     - ``MacIceland``
     - same shape
   * - ``include=["utf-8","cp1252"]``
     - ``Windows-1252``
     - ``Windows-1252``
     - ``Windows-1252``
     - rival outranks utf-8 before the flip
   * - ``include=["utf-8","cp720"], era=MODERN_WEB``
     - ``utf-8``
     - ``utf-8``
     - ``utf-8``
     - cp720 is not ``MODERN_WEB``
   * - ``include=["utf-8","cp720"], era=LEGACY_REGIONAL``
     - ``cp720``
     - ``cp720``
     - ``cp720``
     - utf-8 is ``MODERN_WEB``, so the flip's own input is gone

The minimal constraint set for both families is the same:

1. the caller's bytes run **at least one byte past** ``max_bytes``, so
   ``detect``/``detect_all`` set ``input_truncated=True``; and
2. the streaming caller stops on the *exact* chunk boundary where the
   buffer reaches the cap, so its ``input_truncated`` stays ``False``.

Family A additionally needs a BOM whose window payload is not a whole code
unit, and Family B needs a ``utf-8`` winner whose only multi-byte evidence
is the dangling tail plus a lower-ranked rival that decodes the whole
window.  In both families a further ``include``/``era`` filter that removes
the codec on one side collapses the split to ``encoding=None`` on that side;
the asymmetry remains, but the losing answer disappears rather than changes
name.

Practical rules for a persistence layer
---------------------------------------

- Persist the name **and** the bytes it was chosen from, or re-detect with
  the same ``max_bytes`` the writer used.  A detector that stops at
  ``done`` on an exact boundary and one fed the whole file are not the same
  question.
- Treat ``include_encodings`` as a codec selection, not a name selection.
  If the directory name must resolve to the codec that wrote the bytes,
  spell it in its Python form (``cp932``, ``euc_jis_2004``, ``big5hkscs``,
  ``gb18030``) and validate with ``codecs.lookup(name).name``.
- Do not treat a persisted display name as proof of the text.  ``Big5`` and
  ``SHIFT_JIS`` are the two names where a byte can decode cleanly to the
  wrong character (``①``/``ヾ`` and ``¥``/``\``).
- When two candidates collapse onto one name, keep the byte witness in the
  metadata.  For ``cp949``/``euc_kr`` no text witness exists at all; an
  acceptance witness (``81 41``) is the only evidence, and it can only
  distinguish *which codec accepted the bytes*, never what they say.
