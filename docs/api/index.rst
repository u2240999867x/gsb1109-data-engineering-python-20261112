API Reference
=============

Top-level Functions
-------------------

.. autofunction:: chardet.detect

.. autofunction:: chardet.detect_all

.. autofunction:: chardet.detect_with_evidence

UniversalDetector
-----------------

.. autoclass:: chardet.UniversalDetector
   :members:
   :undoc-members:

Enumerations
------------

.. autoclass:: chardet.EncodingEra
   :members:
   :undoc-members:

.. autoclass:: chardet.LanguageFilter
   :members:
   :undoc-members:

Result Types
------------

.. autoclass:: chardet.DetectionResult
   :members:
   :undoc-members:

.. autoclass:: chardet.DetectionDict
   :members:
   :undoc-members:

Detection Evidence
------------------

.. autoclass:: chardet.DetectionReport
   :members:
   :undoc-members:

.. autoclass:: chardet.DetectionEvidence
   :members:
   :undoc-members:

.. autoclass:: chardet.ReplayVerdict
   :members:
   :undoc-members:

.. autoclass:: chardet.CandidateVerdict
   :members:
   :undoc-members:

.. autoclass:: chardet.RenderEvent
   :members:
   :undoc-members:

.. autoclass:: chardet.CorrectionEvent
   :members:
   :undoc-members:

Constants
---------

.. py:data:: chardet.DEFAULT_MAX_BYTES
   :type: int
   :value: 200000

   Default maximum number of bytes to examine during detection.

.. py:data:: chardet.MINIMUM_THRESHOLD
   :type: float
   :value: 0.20

   Default minimum confidence threshold for filtering results in
   :func:`chardet.detect_all`.
