"""Primary curriculum map: PL–UA lessons from vocabulary metadata (read-only)."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.learning_unit import LearningProgress, LearningUnit
from app.models.vocabulary import Vocabulary
from app.services.session_service import _is_lesson_completed


def build_primary_curriculum_map(db: Session) -> dict[int, list[int]]:
    """
    Build lesson_index → vocabulary ids for the PL–UA track.

    Returns:
        { lesson_index: [vocabulary_id, ...] } sorted by lesson_index.
        Typically one vocabulary per lesson; lists allow multiple vocabs per lesson later.
    """
    rows = (
        db.query(Vocabulary.id, Vocabulary.lesson_index)
        .filter(
            Vocabulary.track_type == "plua",
            Vocabulary.lesson_index.isnot(None),
        )
        .order_by(Vocabulary.lesson_index.asc(), Vocabulary.id.asc())
        .all()
    )

    lesson_map: dict[int, list[int]] = {}
    for vocab_id, lesson_index in rows:
        lesson_map.setdefault(lesson_index, []).append(vocab_id)

    return lesson_map


def get_plua_lesson_progress(db: Session, lesson_index: int) -> dict:
    """Introduced / total words for one PL–UA lesson (read-only aggregates)."""
    lesson_map = build_primary_curriculum_map(db)
    vocab_ids = lesson_map.get(lesson_index, [])

    if not vocab_ids:
        return {"total": 0, "introduced": 0, "percent": 0}

    total = (
        db.query(func.count(LearningUnit.id))
        .filter(LearningUnit.vocabulary_id.in_(vocab_ids))
        .scalar()
        or 0
    )

    introduced = (
        db.query(func.count(LearningProgress.id))
        .join(LearningUnit, LearningUnit.id == LearningProgress.unit_id)
        .filter(
            LearningUnit.vocabulary_id.in_(vocab_ids),
            LearningProgress.introduced_at.isnot(None),
        )
        .scalar()
        or 0
    )

    percent = int((introduced / total) * 100) if total > 0 else 0

    return {
        "total": total,
        "introduced": introduced,
        "percent": percent,
    }


def detect_current_plua_lesson(db: Session) -> int:
    """
    First PL–UA lesson that is not completed under the same rules as czytaj lesson windows
    (all units introduced and enough share mastery; see ``_is_lesson_completed``).

    If the curriculum map is empty, returns 1. If every lesson is completed, returns the
    highest lesson index (typically 26).
    """
    lesson_map = build_primary_curriculum_map(db)
    if not lesson_map:
        return 1

    for lesson_index in sorted(lesson_map.keys()):
        if not _is_lesson_completed(db, lesson_index, lesson_map):
            return lesson_index

    return max(lesson_map.keys())


def is_plua_lesson_completed(db: Session, lesson_index: int) -> bool:
    """Whether a PL–UA lesson index is complete (threshold mastery; same rules as session selection)."""
    lesson_map = build_primary_curriculum_map(db)
    return _is_lesson_completed(db, lesson_index, lesson_map)
