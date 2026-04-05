from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.learning_unit import LearningProgress
from app.models.session import LearningSession, StudyModeType
from app.services.progress_metrics_service import count_due, count_weak
from app.services.session_service import WEAK_FOLLOW_UP_MIN
from app.services.vocab_path_service import compute_next_vocab_focus
from app.utils.time import utc_now

# Overdue count above this → prioritize recall; at or below → guided new-word path.
DUE_RECALL_URGENT_THRESHOLD = 20


def _utc_now_naive() -> datetime:
    """Current UTC timestamp as naive datetime for DB comparisons."""
    return utc_now().replace(tzinfo=None)


def _count_weak_units_for_dashboard(db: Session) -> int:
    """Same definition as snapshot.weak_words_count / is_weak: introduced, confidence < WEAK_THRESHOLD."""
    return count_weak(db.query(LearningProgress).all())


def compute_daily_goal_targets(db: Session) -> Dict[str, int]:
    """
    Derive daily practice targets from the last 7 *complete* UTC days before today
    (yesterday and the six days prior): average completed sessions per day.

    Lesson target scales with session target (lesson completions are tracked client-side
    only when ``curriculum_mode=lesson`` is in the URL).

    Returns:
        ``{"sessions": int, "lessons": int}`` with conservative bounds.
    """
    start_of_today = _utc_now_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    counts: list[int] = []
    for i in range(1, 8):
        day_start = start_of_today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        c = (
            db.query(func.count(LearningSession.id))
            .filter(LearningSession.completed.is_(True))
            .filter(LearningSession.completed_at.isnot(None))
            .filter(LearningSession.completed_at >= day_start)
            .filter(LearningSession.completed_at < day_end)
            .scalar()
            or 0
        )
        counts.append(int(c))

    total = sum(counts)
    if total == 0:
        return {"sessions": 10, "lessons": 2}

    avg = total / 7.0
    sessions_target = int(round(avg))
    sessions_target = max(5, min(40, sessions_target))
    # Roughly one lesson-style goal slot per ~4 sessions; cap for sanity.
    lessons_target = max(2, min(15, max(2, round(sessions_target / 4))))
    return {"sessions": sessions_target, "lessons": lessons_target}


def get_daily_dashboard_stats(db: Session) -> Dict[str, Any]:
    """
    Returns daily learning statistics for dashboard.
    """
    now = _utc_now_naive()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    words_introduced_today = (
        db.query(func.count(LearningProgress.id))
        .filter(LearningProgress.introduced_at >= start_of_day)
        .scalar()
    )

    max_new_per_day = settings.max_new_per_day
    cap_exceeded = words_introduced_today >= max_new_per_day

    recall_sessions_today = (
        db.query(func.count(LearningSession.id))
        .filter(
            LearningSession.completed_at >= start_of_day,
            LearningSession.mode.in_(
                [StudyModeType.RECALL, StudyModeType.RECALL_AUDIO, StudyModeType.CLOZE]
            ),
        )
        .scalar()
    )

    passive_sessions_today = (
        db.query(func.count(LearningSession.id))
        .filter(
            LearningSession.completed_at >= start_of_day,
            LearningSession.mode == StudyModeType.PASSIVE,
        )
        .scalar()
    )

    recall_totals = (
        db.query(
            func.coalesce(func.sum(LearningSession.summary_correct_count), 0),
            func.coalesce(func.sum(LearningSession.summary_answered_units), 0),
        )
        .filter(
            LearningSession.completed_at >= start_of_day,
            LearningSession.mode.in_(
                [StudyModeType.RECALL, StudyModeType.RECALL_AUDIO, StudyModeType.CLOZE]
            ),
        )
        .one()
    )
    total_correct, total_attempts = recall_totals
    recall_accuracy_today = round(total_correct / total_attempts, 2) if total_attempts else 0.0

    overdue_word_count = count_due(db.query(LearningProgress).all(), now=now)
    weak_count = _count_weak_units_for_dashboard(db)

    if cap_exceeded:
        recommended_plan = (
            f"You've introduced {words_introduced_today} words today "
            f"(limit {max_new_per_day}). Focus on recall practice."
        )
    elif overdue_word_count >= DUE_RECALL_URGENT_THRESHOLD:
        recommended_plan = "Start with overdue recall reviews."
    elif weak_count >= WEAK_FOLLOW_UP_MIN:
        recommended_plan = (
            f"You have {weak_count} weak words to strengthen. Practice recall."
        )
    elif overdue_word_count > 0:
        focus = compute_next_vocab_focus(db)
        if focus:
            recommended_plan = (
                f"You have {overdue_word_count} due words. "
                f"Start a new practice and continue with {focus['source']}."
            )
        else:
            recommended_plan = (
                f"You have {overdue_word_count} due words. "
                f"You may introduce new words."
            )
    else:
        focus = compute_next_vocab_focus(db)
        if focus:
            recommended_plan = (
                f"Continue with {focus['source']} "
                f"({focus['remaining']} new words remaining)."
            )
        else:
            recommended_plan = "No overdue words. You may introduce new words."

    goals = compute_daily_goal_targets(db)

    return {
        "words_introduced_today": words_introduced_today,
        "recall_sessions_today": recall_sessions_today,
        "passive_sessions_today": passive_sessions_today,
        "recall_accuracy_today": recall_accuracy_today,
        "overdue_word_count": overdue_word_count,
        # Same weak_count used for recommended_plan (single source for dashboard copy + counts).
        "weak_words_count": weak_count,
        "recommended_plan": recommended_plan,
        "max_new_per_day": max_new_per_day,
        "cap_exceeded": cap_exceeded,
        "daily_goal_sessions": goals["sessions"],
        "daily_goal_lessons": goals["lessons"],
    }
