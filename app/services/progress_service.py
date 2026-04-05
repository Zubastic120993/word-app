"""Progress and mastery computation service."""

from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult
from app.models.session import SessionUnit, LearningSession, StudyModeType
from app.config import settings
from app.services.ai.ollama_client import OllamaClient
from app.services.daily_stats import DUE_RECALL_URGENT_THRESHOLD, get_daily_dashboard_stats
from app.services.learning_snapshot import get_learning_snapshot
from app.services.session_service import WEAK_FOLLOW_UP_MIN
from app.services.lesson_service import detect_current_plua_lesson, get_plua_lesson_progress
from app.services.progress_metrics_service import (
    compute_learning_streak,
    compute_mastery_stats as canonical_compute_mastery_stats,
    count_due,
    count_mastered,
    count_weak,
    is_mastered,
    is_due,
)
from app.utils.time import utc_now


def is_word_mastered(progress: Optional[LearningProgress], now: Optional[datetime] = None) -> bool:
    """
    Check if a word is mastered according to strict definition.
    
    A word is MASTERED if ALL conditions are true:
    - introduced_at IS NOT NULL
    - last recall_result == correct
    - confidence_score >= 0.85
    - next_review_at > now() (not due)
    
    Args:
        progress: LearningProgress object (can be None if no progress exists)
        now: Current time (defaults to utcnow, injectable for testing)
        
    Returns:
        True if word is mastered, False otherwise
    """
    return is_mastered(progress, now)


def compute_mastery_stats(units: list[LearningUnit], now: Optional[datetime] = None) -> dict:
    """
    Compute mastery statistics for a list of learning units.
    
    Returns:
        {
            "passive_pct": float,      # % with introduced_at IS NOT NULL
            "recall_pct": float,      # % with at least one correct recall
            "mastered_pct": float,    # % that are mastered (strict definition)
            "mastered_count": int,    # Count of mastered words
            "total_count": int,       # Total count of units
        }
    """
    if now is None:
        now = utc_now()
    return canonical_compute_mastery_stats(units, now)


def compute_selection_progress_stats(
    db: Session,
    source_pdfs: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Compute progress statistics for selected vocabulary sources.
    
    Calculates per-mode progress percentages based on selected vocabulary sources.
    
    Args:
        db: Database session.
        source_pdfs: Optional list of PDF filenames to filter by. If None, includes all sources.
        now: Current time (defaults to utcnow, injectable for testing).
        
    Returns:
        Dictionary with statistics:
        {
            "total_units": int,
            "passive_pct": float,        # % with introduced_at IS NOT NULL
            "recall_visual_pct": float,  # % with at least one correct recall (visual)
            "recall_audio_pct": float,   # % with at least one correct recall (audio)
            "mastered_pct": float        # % that are mastered (strict definition)
        }
    """
    if now is None:
        now = utc_now()
    
    # Filter units by source_pdfs if provided
    query = db.query(LearningUnit)
    if source_pdfs:
        query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))
    
    units = query.options(
        joinedload(LearningUnit.progress)
    ).all()
    
    total_units = len(units)
    
    if total_units == 0:
        return {
            "total_units": 0,
            "passive_pct": 0.0,
            "recall_visual_pct": 0.0,
            "recall_audio_pct": 0.0,
            "mastered_pct": 0.0,
        }
    
    # Get unit IDs for this selection
    unit_ids = [unit.id for unit in units]
    
    # Count passive (introduced_at IS NOT NULL)
    passive_count = sum(
        1 for unit in units
        if unit.progress and unit.progress.introduced_at is not None
    )
    
    # Count recall visual (at least one correct SessionUnit in RECALL mode)
    recall_visual_unit_ids = (
        db.query(SessionUnit.unit_id)
        .join(LearningSession)
        .filter(
            SessionUnit.unit_id.in_(unit_ids),
            LearningSession.mode.in_([StudyModeType.RECALL, StudyModeType.CLOZE]),
            SessionUnit.recall_result == RecallResult.CORRECT,
        )
        .distinct()
        .all()
    )
    recall_visual_count = len([row[0] for row in recall_visual_unit_ids])
    
    # Count recall audio (at least one correct SessionUnit in RECALL_AUDIO mode)
    recall_audio_unit_ids = (
        db.query(SessionUnit.unit_id)
        .join(LearningSession)
        .filter(
            SessionUnit.unit_id.in_(unit_ids),
            LearningSession.mode == StudyModeType.RECALL_AUDIO,
            SessionUnit.recall_result == RecallResult.CORRECT,
        )
        .distinct()
        .all()
    )
    recall_audio_count = len([row[0] for row in recall_audio_unit_ids])
    
    # Count mastered (strict definition)
    mastered_count = sum(
        1 for unit in units
        if is_mastered(unit.progress, now)
    )
    
    # Calculate percentages
    passive_pct = round((passive_count / total_units) * 100, 1) if total_units > 0 else 0.0
    recall_visual_pct = round((recall_visual_count / total_units) * 100, 1) if total_units > 0 else 0.0
    recall_audio_pct = round((recall_audio_count / total_units) * 100, 1) if total_units > 0 else 0.0
    mastered_pct = round((mastered_count / total_units) * 100, 1) if total_units > 0 else 0.0
    
    return {
        "total_units": total_units,
        "passive_pct": passive_pct,
        "recall_visual_pct": recall_visual_pct,
        "recall_audio_pct": recall_audio_pct,
        "mastered_pct": mastered_pct,
    }


def get_home_snapshot(db: Session) -> dict:
    """Return dashboard snapshot with the rendered last-session label."""
    snapshot = get_learning_snapshot(db)
    daily_stats = get_daily_dashboard_stats(db)
    last_at = snapshot.get("last_session_at")

    snapshot["session_size"] = settings.session_size
    # One weak count for home: matches "Recommended today" (same computation as daily_stats).
    snapshot["weak_words_count"] = daily_stats["weak_words_count"]
    snapshot["recommended_plan"] = daily_stats["recommended_plan"]
    snapshot["daily_goal_sessions"] = daily_stats["daily_goal_sessions"]
    snapshot["daily_goal_lessons"] = daily_stats["daily_goal_lessons"]
    snapshot["due_recall_urgent_threshold"] = DUE_RECALL_URGENT_THRESHOLD
    snapshot["weak_follow_up_min"] = WEAK_FOLLOW_UP_MIN
    lesson_index = detect_current_plua_lesson(db)
    snapshot["current_lesson"] = lesson_index
    snapshot["lesson_progress"] = get_plua_lesson_progress(db, lesson_index)

    if last_at is None:
        snapshot["last_session_label"] = "No sessions yet"
        return snapshot

    now_utc = utc_now()
    last_date = last_at.date() if hasattr(last_at, "date") else last_at
    delta_days = (now_utc.date() - last_date).days

    if delta_days == 0:
        snapshot["last_session_label"] = "Today"
    elif delta_days == 1:
        snapshot["last_session_label"] = "Yesterday"
    else:
        snapshot["last_session_label"] = f"{delta_days} days ago"

    return snapshot


def get_source_counts(db: Session) -> list[dict]:
    """Return source PDF counts for UI filter/dropdown pages."""
    sources = (
        db.query(
            LearningUnit.source_pdf,
            func.count(LearningUnit.id).label("count"),
        )
        .group_by(LearningUnit.source_pdf)
        .all()
    )
    return [{"name": source_pdf, "count": count} for source_pdf, count in sources]


def get_recent_uploads(db: Session, limit: int = 5) -> list[dict]:
    """Return recent uploads for the upload page."""
    recent_uploads = (
        db.query(
            LearningUnit.source_pdf,
            func.count(LearningUnit.id).label("count"),
        )
        .group_by(LearningUnit.source_pdf)
        .order_by(func.max(LearningUnit.created_at).desc())
        .limit(limit)
        .all()
    )
    return [{"source_pdf": source_pdf, "count": count} for source_pdf, count in recent_uploads]


async def get_chat_page_data() -> dict:
    """Return AI availability for the chat page."""
    ai_available = False
    ai_error = None

    try:
        status = await OllamaClient().check_health()
        ai_available = status.available
        ai_error = status.error
    except Exception as exc:
        ai_error = str(exc)

    return {
        "ai_available": ai_available,
        "ai_error": ai_error,
    }


def get_progress_page_data(db: Session) -> dict:
    """Return the aggregated data required by the progress page."""
    total_units = db.query(func.count(LearningUnit.id)).scalar() or 0

    words = db.query(func.count(LearningUnit.id)).filter(LearningUnit.type == "word").scalar() or 0
    phrases = db.query(func.count(LearningUnit.id)).filter(LearningUnit.type == "phrase").scalar() or 0
    sentences = db.query(func.count(LearningUnit.id)).filter(LearningUnit.type == "sentence").scalar() or 0

    never_seen = total_units - (db.query(func.count(LearningProgress.id)).scalar() or 0)
    learning = (
        db.query(func.count(LearningProgress.id))
        .filter(LearningProgress.confidence_score < 0.7)
        .filter(LearningProgress.confidence_score > 0)
        .scalar()
        or 0
    )
    learned = (
        db.query(func.count(LearningProgress.id))
        .filter(LearningProgress.confidence_score >= 0.7)
        .scalar()
        or 0
    )

    total_sessions = db.query(func.count(LearningSession.id)).scalar() or 0
    completed_sessions = (
        db.query(func.count(LearningSession.id))
        .filter(LearningSession.completed == True)
        .scalar()
        or 0
    )

    total_answers = (
        db.query(func.count(SessionUnit.id))
        .filter(SessionUnit.answered == True)
        .scalar()
        or 0
    )
    correct_answers = (
        db.query(func.count(SessionUnit.id))
        .filter(SessionUnit.is_correct == True)
        .scalar()
        or 0
    )
    correct_rate = round((correct_answers / total_answers * 100) if total_answers > 0 else 0)

    now_local = datetime.now()
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_today - timedelta(days=start_of_today.weekday())

    learned_base_query = (
        db.query(func.count(LearningProgress.id))
        .filter(LearningProgress.last_recall_result == RecallResult.CORRECT)
        .filter(LearningProgress.times_correct >= 1)
        .filter(LearningProgress.last_seen.isnot(None))
    )
    learned_today = learned_base_query.filter(LearningProgress.last_seen >= start_of_today).scalar() or 0
    learned_this_week = learned_base_query.filter(LearningProgress.last_seen >= start_of_week).scalar() or 0

    progress_rows = db.query(LearningProgress).all()
    streak_days = compute_learning_streak(progress_rows, today=now_local)
    now_utc = utc_now()
    weak_words_count = count_weak(progress_rows)
    due_words_count = count_due(progress_rows, now=now_utc)
    mastered_count = count_mastered(progress_rows, now=now_utc)
    mastered_pct = round((mastered_count / total_units * 100) if total_units > 0 else 0)

    week_answers = (
        db.query(func.count(SessionUnit.id))
        .join(LearningSession, SessionUnit.session_id == LearningSession.id)
        .filter(LearningSession.created_at >= start_of_week)
        .filter(SessionUnit.answered == True)
        .scalar()
        or 0
    )
    week_correct = (
        db.query(func.count(SessionUnit.id))
        .join(LearningSession, SessionUnit.session_id == LearningSession.id)
        .filter(LearningSession.created_at >= start_of_week)
        .filter(SessionUnit.is_correct == True)
        .scalar()
        or 0
    )
    week_accuracy = round((week_correct / week_answers * 100) if week_answers > 0 else 0)

    stats = {
        "total_units": total_units,
        "words": words,
        "phrases": phrases,
        "sentences": sentences,
        "never_seen": never_seen,
        "learning": learning,
        "learned": learned,
        "learned_today": learned_today,
        "learned_this_week": learned_this_week,
        "learning_streak_days": streak_days,
        "total_sessions": total_sessions,
        "completed_sessions": completed_sessions,
        "total_answers": total_answers,
        "correct_rate": correct_rate,
        "weak_words_count": weak_words_count,
        "due_words_count": due_words_count,
        "mastered_count": mastered_count,
        "mastered_pct": mastered_pct,
        "week_answers": week_answers,
        "week_accuracy": week_accuracy,
    }

    sources = get_source_counts(db)

    all_units = db.query(LearningUnit).options(joinedload(LearningUnit.progress)).all()
    source_units: dict[str, list[LearningUnit]] = {}
    for unit in all_units:
        source_units.setdefault(unit.source_pdf, []).append(unit)

    source_stats = {
        source_pdf: canonical_compute_mastery_stats(units, now_utc)
        for source_pdf, units in source_units.items()
    }
    source_stats["__all__"] = canonical_compute_mastery_stats(all_units, now_utc)

    per_source_counts: dict[str, dict[str, int]] = {}
    for source_pdf, units in source_units.items():
        progresses = [u.progress for u in units]
        per_source_counts[source_pdf] = {
            "due_count": count_due(progresses, now=now_utc),
            "weak_count": count_weak(progresses),
        }

    for source_key, st in source_stats.items():
        if source_key == "__all__":
            st["due_count"] = due_words_count
            st["weak_count"] = weak_words_count
        else:
            extra = per_source_counts.get(source_key, {})
            st["due_count"] = extra.get("due_count", 0)
            st["weak_count"] = extra.get("weak_count", 0)

    weak_units_query = (
        db.query(
            LearningUnit,
            LearningProgress.confidence_score,
            LearningProgress.last_recall_result,
            LearningProgress.next_review_at,
            LearningProgress.recall_fail_streak,
        )
        .join(LearningProgress)
        .filter(LearningProgress.confidence_score < 0.5)
        .order_by(
            LearningProgress.confidence_score.asc(),
            LearningProgress.recall_fail_streak.desc(),
            LearningProgress.next_review_at.asc(),
            LearningUnit.source_pdf.asc(),
        )
        .limit(20)
        .all()
    )

    now = utc_now()
    weak_units = []
    for unit, conf, recall_result, next_review_at, recall_fail_streak in weak_units_query:
        progress = unit.progress
        review_status = None
        due_now = is_due(progress, now=now)

        if next_review_at:
            if next_review_at.tzinfo is None:
                next_review_at = next_review_at.replace(tzinfo=UTC)
            diff_days = (next_review_at - now).days
            if due_now:
                review_status = "Due now"
            elif diff_days == 1:
                review_status = "Due in 1 day"
            else:
                review_status = f"Due in {diff_days} days"

        weak_units.append(
            {
                "text": unit.text,
                "translation": unit.translation,
                "source_pdf": unit.source_pdf,
                "confidence": conf,
                "last_recall_result": recall_result.value if recall_result else None,
                "next_review_at": next_review_at.strftime("%Y-%m-%d") if next_review_at else None,
                "review_status": review_status,
                "is_due": due_now,
                "recall_fail_streak": recall_fail_streak or 0,
            }
        )

    return {
        "stats": stats,
        "sources": sources,
        "source_stats": source_stats,
        "weak_units": weak_units,
        "daily_stats": get_daily_dashboard_stats(db),
    }


def get_per_source_due_weak_counts(db: Session) -> dict[str, dict[str, int]]:
    """
    Per vocabulary file: due_count and weak_count using the same contract as
    progress ``source_stats`` (excluding ``__all__`` aggregate).
    """
    now_utc = utc_now()
    all_units = db.query(LearningUnit).options(joinedload(LearningUnit.progress)).all()
    source_units: dict[str, list[LearningUnit]] = {}
    for unit in all_units:
        source_units.setdefault(unit.source_pdf, []).append(unit)
    out: dict[str, dict[str, int]] = {}
    for source_pdf, units in source_units.items():
        progresses = [u.progress for u in units]
        out[source_pdf] = {
            "due_count": count_due(progresses, now=now_utc),
            "weak_count": count_weak(progresses),
        }
    return out


def get_data_management_page_data(db: Session) -> dict:
    """Return entity counts for the data management page."""
    return {
        "units": db.query(LearningUnit).count(),
        "sessions": db.query(LearningSession).count(),
        "progress": db.query(LearningProgress).count(),
    }
