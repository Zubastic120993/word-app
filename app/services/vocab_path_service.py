"""Learning-path helpers: vocabulary filename tiers and next focus for Home/daily copy."""

from __future__ import annotations

from typing import Any, Literal, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.learning_unit import LearningProgress, LearningUnit

VocabTier = Literal["pl_ua", "czytaj", "other"]

_TIER_ORDER: tuple[VocabTier, ...] = ("pl_ua", "czytaj")


def classify_vocab_source_pdf(source_pdf: str) -> VocabTier:
    s = (source_pdf or "").lower()
    if "polish_ukrainian" in s:
        return "pl_ua"
    if "czytaj" in s:
        return "czytaj"
    return "other"


def _never_seen_counts_by_source(db: Session) -> dict[str, int]:
    rows = (
        db.query(
            LearningUnit.source_pdf,
            func.count(LearningUnit.id).label("cnt"),
        )
        .outerjoin(LearningProgress, LearningUnit.id == LearningProgress.unit_id)
        .filter(LearningProgress.id.is_(None))
        .group_by(LearningUnit.source_pdf)
        .all()
    )
    return {r.source_pdf: int(r.cnt) for r in rows if r.cnt and r.source_pdf}


def compute_next_vocab_focus(db: Session) -> Optional[dict[str, Any]]:
    """
    Next passive focus for copy: pl_ua sources first (alphabetical), then czytaj.
    Counts only units with **no** LearningProgress row (never seen).
    """
    by_source = _never_seen_counts_by_source(db)
    if not by_source:
        return None

    for tier in _TIER_ORDER:
        candidates = sorted(
            pdf
            for pdf, n in by_source.items()
            if n > 0 and classify_vocab_source_pdf(pdf) == tier
        )
        if not candidates:
            continue
        pdf = candidates[0]
        return {
            "source": pdf,
            "type": tier,
            "remaining": by_source[pdf],
        }
    return None
