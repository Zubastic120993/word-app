"""
Read-only analytics event recorder.

Example usage (safe, no impact on main flow):
    from app.services.analytics_service import record_event

    record_event(
        db=db,
        event_type="token_guard_hit",
        theme=current_theme,
        payload={"estimated_tokens": estimated},
    )
"""

import logging
from typing import Optional
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.practice_event import PracticeEvent
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

STUDY_ANSWER_EVENT_TYPE = "study_answer_submitted"


def _normalize_answer_index(
    answer_index: Optional[int],
    fallback_answer_index: Optional[int] = None,
) -> int:
    if answer_index is not None:
        return int(answer_index)
    if fallback_answer_index is not None:
        return int(fallback_answer_index)
    raise ValueError("answer_index is required for study-answer analytics")


def _study_answer_key(payload: Optional[dict]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    payload = payload or {}
    return (
        payload.get("session_id"),
        payload.get("unit_id"),
        payload.get("answer_index"),
    )


def _study_answer_events_query(db: Session, *, session_id: Optional[int] = None):
    query = db.query(PracticeEvent).filter(PracticeEvent.event_type == STUDY_ANSWER_EVENT_TYPE)
    if session_id is not None:
        query = query.filter(func.json_extract(PracticeEvent.payload, "$.session_id") == int(session_id))
    return query


def _iter_study_answer_events(db: Session, *, session_id: Optional[int] = None):
    return _study_answer_events_query(db, session_id=session_id).order_by(PracticeEvent.id.asc()).all()


def _aggregate_deduped_study_events(events) -> dict:
    """Shared dedupe + counts for ``study_answer_submitted`` event rows (ordered by id)."""
    seen_keys: set[tuple[Optional[int], Optional[int], Optional[int]]] = set()
    correct_answers = 0
    incorrect_answers = 0

    for event in events:
        payload = event.payload or {}

        dedupe_key = _study_answer_key(payload)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        if payload.get("result") == "correct":
            correct_answers += 1
        elif payload.get("result") == "incorrect":
            incorrect_answers += 1

    total_answers = correct_answers + incorrect_answers
    if total_answers == 0:
        success_rate = 0.0
        failure_rate = 0.0
    else:
        success_rate = correct_answers / total_answers
        failure_rate = incorrect_answers / total_answers

    return {
        "total_answers": total_answers,
        "correct_answers": correct_answers,
        "incorrect_answers": incorrect_answers,
        "success_rate": success_rate,
        "failure_rate": failure_rate,
    }


def record_event(db: Session, event_type: str, theme: Optional[str], payload: dict):
    try:
        event = PracticeEvent(
            event_type=event_type,
            theme=theme,
            payload=payload,
        )
        db.add(event)
        db.commit()
    except Exception:
        logger.exception("Failed to record analytics event")
        db.rollback()


def record_study_answer_event(
    db: Session,
    *,
    session_id: int,
    unit_id: int,
    result: str,
    timestamp: datetime | str,
    answer_index: Optional[int] = None,
    fallback_answer_index: Optional[int] = None,
) -> bool:
    normalized_result = str(result).lower()
    if normalized_result not in {"correct", "incorrect"}:
        raise ValueError(f"Unsupported study-answer result: {result}")

    normalized_answer_index = _normalize_answer_index(answer_index, fallback_answer_index)
    dedupe_key = (int(session_id), int(unit_id), normalized_answer_index)

    existing = (
        _study_answer_events_query(db, session_id=session_id)
        .filter(func.json_extract(PracticeEvent.payload, "$.unit_id") == int(unit_id))
        .filter(func.json_extract(PracticeEvent.payload, "$.answer_index") == normalized_answer_index)
        .first()
    )
    if existing and _study_answer_key(existing.payload) == dedupe_key:
        return False

    timestamp_value = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
    payload = {
        "session_id": int(session_id),
        "unit_id": int(unit_id),
        "answer_index": normalized_answer_index,
        "result": normalized_result,
        "timestamp": timestamp_value,
    }

    try:
        with db.begin_nested():
            event = PracticeEvent(
                event_type=STUDY_ANSWER_EVENT_TYPE,
                theme=None,
                payload=payload,
            )
            db.add(event)
            db.flush()
        return True
    except Exception:
        logger.exception(
            "Failed to record study-answer analytics event",
            extra={"session_id": session_id, "unit_id": unit_id, "answer_index": normalized_answer_index},
        )
        raise


def get_study_answer_metrics_since(db: Session, since: Optional[datetime] = None) -> dict:
    """
    Aggregate deduplicated study-answer events from ``since`` (inclusive) to now.

    Uses ``PracticeEvent.created_at`` for the window. ``since`` should be naive UTC
    to match stored ``created_at`` values.
    """
    query = _study_answer_events_query(db, session_id=None)
    if since is not None:
        query = query.filter(PracticeEvent.created_at >= since)
    events = query.order_by(PracticeEvent.id.asc()).all()
    return _aggregate_deduped_study_events(events)


def get_study_answer_metrics_between(
    db: Session,
    since_inclusive: datetime,
    until_exclusive: datetime,
) -> dict:
    """
    Aggregate deduplicated study answers with
    ``since_inclusive <= created_at < until_exclusive`` (naive UTC).
    """
    query = (
        _study_answer_events_query(db, session_id=None)
        .filter(PracticeEvent.created_at >= since_inclusive)
        .filter(PracticeEvent.created_at < until_exclusive)
    )
    events = query.order_by(PracticeEvent.id.asc()).all()
    return _aggregate_deduped_study_events(events)


def get_study_calendar_week_activity(
    db: Session,
    anchor_naive: datetime,
    *,
    week_offset: int = 0,
) -> dict:
    """
    Deduped study answers over exactly seven **calendar** days (naive UTC dates).

    ``week_offset=0`` — days ``anchor_naive.date() - 6`` through ``anchor_naive.date()`` (inclusive).
    ``week_offset=1`` — the seven days immediately before that window.

    Returns aggregate metrics (same keys as ``_aggregate_deduped_study_events``) plus
    ``per_day`` (seven ints, oldest day first) so ``sum(per_day) == total_answers``.
    """
    end_day: date = anchor_naive.date() - timedelta(days=7 * week_offset)
    start_day: date = end_day - timedelta(days=6)
    range_start = datetime(start_day.year, start_day.month, start_day.day)
    day_after_end = end_day + timedelta(days=1)
    range_end_exclusive = datetime(day_after_end.year, day_after_end.month, day_after_end.day)

    query = (
        _study_answer_events_query(db, session_id=None)
        .filter(PracticeEvent.created_at >= range_start)
        .filter(PracticeEvent.created_at < range_end_exclusive)
    )
    events = query.order_by(PracticeEvent.id.asc()).all()

    seen_keys: set[tuple[Optional[int], Optional[int], Optional[int]]] = set()
    per_day_map: dict[date, int] = {start_day + timedelta(days=i): 0 for i in range(7)}
    correct_answers = 0
    incorrect_answers = 0

    for event in events:
        payload = event.payload or {}
        dedupe_key = _study_answer_key(payload)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        evd = event.created_at.date()
        if evd in per_day_map:
            per_day_map[evd] += 1

        if payload.get("result") == "correct":
            correct_answers += 1
        elif payload.get("result") == "incorrect":
            incorrect_answers += 1

    per_day = [per_day_map[start_day + timedelta(days=i)] for i in range(7)]
    total_answers = correct_answers + incorrect_answers
    if total_answers == 0:
        success_rate = 0.0
        failure_rate = 0.0
    else:
        success_rate = correct_answers / total_answers
        failure_rate = incorrect_answers / total_answers

    return {
        "per_day": per_day,
        "start_day": start_day,
        "end_day": end_day,
        "total_answers": total_answers,
        "correct_answers": correct_answers,
        "incorrect_answers": incorrect_answers,
        "success_rate": success_rate,
        "failure_rate": failure_rate,
    }


def get_study_answer_metrics(db: Session, session_id: Optional[int] = None) -> dict:
    seen_keys: set[tuple[Optional[int], Optional[int], Optional[int]]] = set()
    correct_answers = 0
    incorrect_answers = 0

    for event in _iter_study_answer_events(db, session_id=session_id):
        payload = event.payload or {}

        dedupe_key = _study_answer_key(payload)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        if payload.get("result") == "correct":
            correct_answers += 1
        elif payload.get("result") == "incorrect":
            incorrect_answers += 1

    total_answers = correct_answers + incorrect_answers
    if total_answers == 0:
        success_rate = 0.0
        failure_rate = 0.0
    else:
        success_rate = correct_answers / total_answers
        failure_rate = incorrect_answers / total_answers

    return {
        "total_answers": total_answers,
        "correct_answers": correct_answers,
        "incorrect_answers": incorrect_answers,
        "success_rate": success_rate,
        "failure_rate": failure_rate,
    }


def get_theme_summary(db: Session, days: int):
    try:
        since = utc_now() - timedelta(days=days)

        events = (
            db.query(PracticeEvent)
            .filter(
                PracticeEvent.event_type.in_(
                    ["retry_attempt", "retry_resolved", "retry_revealed"]
                )
            )
            .filter(PracticeEvent.created_at >= since)
            .order_by(PracticeEvent.id.asc())
            .all()
        )

        theme_stats = {}

        for e in events:
            theme = e.theme or "global"
            theme_stats.setdefault(
                theme,
                {
                    "total_attempts": 0,
                    "first_try_success": 0,
                    "resolved_attempts": [],
                    "reveals": 0,
                },
            )

            payload = e.payload or {}

            if e.event_type == "retry_attempt":
                theme_stats[theme]["total_attempts"] += 1
                if payload.get("attempt_number") == 1 and payload.get("is_correct"):
                    theme_stats[theme]["first_try_success"] += 1

            if e.event_type == "retry_resolved":
                attempt = payload.get("attempt_number")
                if attempt:
                    theme_stats[theme]["resolved_attempts"].append(attempt)

            if e.event_type == "retry_revealed":
                theme_stats[theme]["reveals"] += 1

        result = []

        for theme, stats in theme_stats.items():
            total = stats["total_attempts"] or 1
            resolved = stats["resolved_attempts"]
            avg_attempts = sum(resolved) / len(resolved) if resolved else 0

            result.append(
                {
                    "theme": theme,
                    "total_attempts": stats["total_attempts"],
                    "first_try_success_rate": stats["first_try_success"] / total,
                    "average_attempts": avg_attempts,
                    "reveal_rate": stats["reveals"] / total,
                    "retry_rate": (total - stats["first_try_success"]) / total,
                    "resolved_attempt_count": len(resolved),
                    "has_resolved_attempts": bool(resolved),
                }
            )

        return {
            "window_days": days,
            "themes": result,
        }
    except Exception:
        logger.exception("Failed to compute theme summary")
        return {
            "window_days": days,
            "themes": [],
        }
