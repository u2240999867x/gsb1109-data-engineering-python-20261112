API Reference
=============

Top-level Functions
-------------------

.. autofunction:: chardet.detect

.. autofunction:: chardet.detect_all

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
