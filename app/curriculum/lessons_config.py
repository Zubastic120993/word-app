"""Dynamic lesson mapping for czytaj_* vocabulary sources."""

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.learning_unit import LearningUnit
from app.models.vocabulary import Vocabulary

LESSON_VERSION = 2

_CZYTAJ_GROUP_RE = re.compile(r"^czytaj_(\d{2})_(\d{2})_")


def build_lesson_to_vocab(db: Session) -> dict[int, list[int]]:
    """
    Build lesson mapping from real DB vocabulary sources.

    Rules:
    - only vocabularies with names matching ``czytaj_XX_YY_``
    - group by ``(XX, YY)``
    - skip vocabularies with zero learning units
    - sort groups by ``(XX, YY)`` and re-index lessons sequentially 1..N
    """
    vocabularies = db.query(Vocabulary).all()

    unit_counts = {
        vocab_id: count
        for vocab_id, count in (
            db.query(LearningUnit.vocabulary_id, func.count(LearningUnit.id))
            .filter(LearningUnit.vocabulary_id.isnot(None))
            .group_by(LearningUnit.vocabulary_id)
            .all()
        )
    }

    grouped: dict[tuple[int, int], list[int]] = {}
    for vocab in vocabularies:
        match = _CZYTAJ_GROUP_RE.match(vocab.name or "")
        if not match:
            continue
        if unit_counts.get(vocab.id, 0) <= 0:
            continue
        group_key = (int(match.group(1)), int(match.group(2)))
        grouped.setdefault(group_key, []).append(vocab.id)

    lesson_to_vocab: dict[int, list[int]] = {}
    for idx, group_key in enumerate(sorted(grouped.keys()), start=1):
        lesson_to_vocab[idx] = sorted(grouped[group_key])

    return lesson_to_vocab
