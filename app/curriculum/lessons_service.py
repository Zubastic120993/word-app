from typing import List, Optional

from app.curriculum.lessons_config import VOCAB_TO_LESSON, LESSON_TO_VOCAB


def get_lesson_by_vocab_id(vocab_id: int) -> Optional[int]:
    return VOCAB_TO_LESSON.get(vocab_id)


def get_vocab_ids_by_lesson(lesson_id: int) -> List[int]:
    return LESSON_TO_VOCAB.get(lesson_id, [])


def validate_lesson_partition():
    """
    Ensure all 26 vocabularies are mapped exactly once.
    """
    expected_ids = set(VOCAB_TO_LESSON.keys())

    if len(expected_ids) != 26:
        raise ValueError("Lesson mapping must contain exactly 26 vocabulary IDs.")

    # Ensure lessons 1..26 exist
    expected_lessons = set(range(1, 27))
    actual_lessons = set(LESSON_TO_VOCAB.keys())

    missing = expected_lessons - actual_lessons
    if missing:
        raise ValueError(f"Missing lesson mappings for: {sorted(missing)}")


# Validate at import time (fail fast)
validate_lesson_partition()
