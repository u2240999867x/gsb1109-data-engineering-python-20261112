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

Legacy Directory Migration
--------------------------

.. autofunction:: chardet.migrate_directory

.. autoclass:: chardet.LegacyPolicy
   :members:
   :undoc-members:

.. autoclass:: chardet.LegacyRecord
   :members:
   :undoc-members:

.. autoclass:: chardet.MigrationVerdict
   :members:
   :undoc-members:

.. autoclass:: chardet.RecordMigration
   :members:
   :undoc-members:

.. autoclass:: chardet.DirectoryMigration
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

.. py:data:: chardet.MIGRATION_POLICY_VERSION
   :type: int
   :value: 1

   Policy version written by :func:`chardet.migrate_directory`; directories
   whose policy carries it are re-checked, not re-migrated.
