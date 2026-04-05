from sqlalchemy import select, asc, desc
from sqlalchemy.orm import Session

from app.models.learning_unit import LearningUnit, LearningProgress


def fetch_vocab_bias(session: Session, limit: int = 25) -> list[str]:
    """
    Returns a deterministic list of vocabulary words for chat bias.

    Rules:
    - Strict weak definition (confidence_score < 0.5)
    - introduced_at must not be null
    - Exclude blocked words
    - Weak words take priority
    - No weighting
    - No randomness
    - Deterministic ordering
    """

    # 1. Strict weak words
    weak_stmt = (
        select(LearningUnit.text)
        .join(LearningProgress)
        .where(
            LearningProgress.confidence_score < 0.5,
            LearningProgress.introduced_at.is_not(None),
            LearningProgress.is_blocked.is_(False),
        )
        .order_by(
            asc(LearningProgress.confidence_score),
            asc(LearningProgress.last_seen),
        )
    )

    weak_words = session.execute(weak_stmt).scalars().all()

    # 2. Recent words (by last_seen descending)
    recent_stmt = (
        select(LearningUnit.text)
        .join(LearningProgress)
        .where(
            LearningProgress.introduced_at.is_not(None),
            LearningProgress.is_blocked.is_(False),
        )
        .order_by(desc(LearningProgress.last_seen))
        .limit(20)
    )

    recent_words = session.execute(recent_stmt).scalars().all()

    # 3. Merge with weak priority
    seen = set(weak_words)
    combined = list(weak_words) + [
        w for w in recent_words if w not in seen
    ]

    return combined[:limit]
