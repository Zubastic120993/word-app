"""Lightweight learning snapshot for the Home page.

Returns only the handful of numbers needed to answer
"What should I study now?" — no charts, no breakdowns.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.learning_unit import LearningUnit, LearningProgress
from app.models.session import LearningSession
from app.services.progress_metrics_service import (
    compute_learning_streak,
    compute_previous_streak,
    compute_review_forecast,
    count_due,
    count_mastered,
    count_weak,
)
from app.utils.time import utc_now


def get_learning_snapshot(db: Session) -> dict:
    """Return a compact learning snapshot (COUNT-only queries).

    Keys returned:
        weak_words_count   – confidence < 0.5 (introduced only)
        due_words_count    – next_review_at <= now (introduced only)
        learning_streak_days – consecutive calendar days with recall-correct
        total_units        – all learning units
        learned_units      – confidence >= 0.7
        mastery_percent    – % of units satisfying strict mastery
        last_session_at    – timestamp of most recent completed session, or None
    """
    # Total units
    total_units = db.query(func.count(LearningUnit.id)).scalar() or 0

    # Learned (confidence >= 0.7)
    learned_units = (
        db.query(func.count(LearningProgress.id))
        .filter(LearningProgress.confidence_score >= 0.7)
        .scalar() or 0
    )

    progress_rows = db.query(LearningProgress).all()

    weak_words_count = count_weak(progress_rows)
    due_words_count = count_due(progress_rows)
    mastered_count = count_mastered(progress_rows)
    mastery_percent = round((mastered_count / total_units * 100) if total_units > 0 else 0, 1)

    # --- Learning streak + break detection ---
    now = utc_now()
    streak_days = compute_learning_streak(progress_rows, today=now)
    # If no activity today but yesterday had a streak, the user broke it.
    # Only signal the break when the lost streak was meaningful (>= 2 days).
    if streak_days == 0:
        prev = compute_previous_streak(progress_rows, today=now)
        streak_broken_days = prev if prev >= 2 else 0
    else:
        streak_broken_days = 0

    # Most recent completed session (single query, no aggregation)
    last_session = (
        db.query(LearningSession)
        .filter(LearningSession.completed == True)
        .filter(LearningSession.completed_at.isnot(None))
        .order_by(LearningSession.completed_at.desc())
        .first()
    )
    last_session_at = last_session.completed_at if last_session else None

    review_forecast = compute_review_forecast(progress_rows, now=now)

    return {
        "weak_words_count": weak_words_count,
        "due_words_count": due_words_count,
        "learning_streak_days": streak_days,
        "streak_broken_days": streak_broken_days,
        "total_units": total_units,
        "learned_units": learned_units,
        "mastery_percent": mastery_percent,
        "last_session_at": last_session_at,
        "review_forecast": review_forecast,
    }
