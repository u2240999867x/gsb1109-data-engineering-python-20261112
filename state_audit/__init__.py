# -*- coding: utf-8 -*-
"""FlashText 词典编辑状态对照工具包。"""

from state_audit.reference_matcher import (
    ReferenceMatcher,
    EntityHit,
    DEFAULT_NON_WORD_BOUNDARIES,
    boundary_explanation,
)
from state_audit.auditor import (
    HitRecord,
    FieldDiff,
    TitleCheck,
    StepSnapshot,
    run_audit,
    compare_title,
    build_reference,
    build_flashtext,
    minimize_edits,
    first_failing_step,
    diff_signatures,
    buggy_remove_break_prefix,
    OffsetDistortingProcessor,
)

__all__ = [
    'ReferenceMatcher',
    'EntityHit',
    'DEFAULT_NON_WORD_BOUNDARIES',
    'boundary_explanation',
    'HitRecord',
    'FieldDiff',
    'TitleCheck',
    'StepSnapshot',
    'run_audit',
    'compare_title',
    'build_reference',
    'build_flashtext',
    'minimize_edits',
    'first_failing_step',
    'diff_signatures',
    'buggy_remove_break_prefix',
    'OffsetDistortingProcessor',
]
