API Reference
=============

Top-level Functions
-------------------

.. autofunction:: chardet.detect

.. autofunction:: chardet.detect_all

.. autofunction:: chardet.detect_consensus

Multi-Sample Consensus Types
----------------------------

.. autoclass:: chardet.ConsensusResult
   :members:
   :undoc-members:

.. autoclass:: chardet.ConsensusStatus
   :members:
   :undoc-members:

.. autoclass:: chardet.ConsensusConflict
   :members:
   :undoc-members:

.. autoclass:: chardet.SampleReport
   :members:
   :undoc-members:

.. autoclass:: chardet.SampleVerdict
   :members:
   :undoc-members:

.. autoclass:: chardet.EncodingSupport
   :members:
   :undoc-members:

.. autoclass:: chardet.EncodingRejection
   :members:
   :undoc-members:

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
