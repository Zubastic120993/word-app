"""Canonical learning metric predicates, counters, and helpers."""

from datetime import UTC, date, datetime, timedelta
from typing import Iterable, Optional

from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult
from app.utils.time import utc_now

WEAK_THRESHOLD = 0.5
MASTERY_THRESHOLD = 0.85


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize datetimes to naive UTC for stable comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _as_date(value: object) -> Optional[date]:
    """Extract a date from a datetime-like object or model instance."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    completed_at = getattr(value, "completed_at", None)
    if completed_at is not None:
        return _as_date(completed_at)

    return None


def is_due(progress: Optional[LearningProgress], now: Optional[datetime] = None) -> bool:
    """Return whether a progress row is currently due by the frozen contract."""
    if progress is None or progress.introduced_at is None or progress.next_review_at is None:
        return False

    now_value = _naive_utc(now or utc_now())
    next_review_at = _naive_utc(progress.next_review_at)
    return bool(next_review_at and next_review_at <= now_value)


def is_weak(progress: Optional[LearningProgress]) -> bool:
    """Return whether a progress row is canonically weak."""
    if progress is None or progress.introduced_at is None:
        return False
    return progress.confidence_score < WEAK_THRESHOLD


def is_mastered(progress: Optional[LearningProgress], now: Optional[datetime] = None) -> bool:
    """Return whether a progress row is canonically mastered."""
    if progress is None or progress.introduced_at is None:
        return False
    if progress.last_recall_result != RecallResult.CORRECT:
        return False
    if progress.confidence_score < MASTERY_THRESHOLD:
        return False
    if progress.next_review_at is None:
        return False

    now_value = _naive_utc(now or utc_now())
    next_review_at = _naive_utc(progress.next_review_at)
    return bool(next_review_at and next_review_at > now_value)


def count_due(progress_rows: Iterable[Optional[LearningProgress]], now: Optional[datetime] = None) -> int:
    """Count due progress rows."""
    return sum(1 for progress in progress_rows if is_due(progress, now))


def count_weak(progress_rows: Iterable[Optional[LearningProgress]]) -> int:
    """Count weak progress rows."""
    return sum(1 for progress in progress_rows if is_weak(progress))


def count_mastered(progress_rows: Iterable[Optional[LearningProgress]], now: Optional[datetime] = None) -> int:
    """Count mastered progress rows."""
    return sum(1 for progress in progress_rows if is_mastered(progress, now))


def compute_learning_streak(
    progress_rows: Iterable[Optional[LearningProgress]],
    today: Optional[datetime] = None,
) -> int:
    """Compute the frozen learning streak from learning_progress rows."""
    now_local = today or datetime.now()
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=UTC)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    learned_dates = set()
    for progress in progress_rows:
        if (
            progress is None
            or progress.last_recall_result != RecallResult.CORRECT
            or progress.times_correct < 1
        ):
            continue

        last_seen = progress.last_seen
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)

        if last_seen and last_seen >= start_of_today - timedelta(days=365):
            learned_dates.add(last_seen.date())

    if start_of_today.date() not in learned_dates:
        return 0

    streak_days = 0
    day_cursor = start_of_today.date()
    while day_cursor in learned_dates:
        streak_days += 1
        day_cursor -= timedelta(days=1)

    return streak_days


def compute_previous_streak(
    progress_rows: Iterable[Optional[LearningProgress]],
    today: Optional[datetime] = None,
) -> int:
    """Return the streak that ended yesterday (i.e. streak as of end-of-yesterday).

    Used to detect a streak break: if today's streak is 0 and this returns >= 2,
    the user broke a streak and should see a loss-aversion message.
    """
    now_local = today or datetime.now()
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=UTC)
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = (start_of_today - timedelta(days=1)).date()

    learned_dates = set()
    for progress in progress_rows:
        if (
            progress is None
            or progress.last_recall_result != RecallResult.CORRECT
            or progress.times_correct < 1
        ):
            continue
        last_seen = progress.last_seen
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        if last_seen and last_seen >= start_of_today - timedelta(days=366):
            learned_dates.add(last_seen.date())

    if yesterday not in learned_dates:
        return 0

    streak_days = 0
    day_cursor = yesterday
    while day_cursor in learned_dates:
        streak_days += 1
        day_cursor -= timedelta(days=1)

    return streak_days


def compute_review_forecast(
    progress_rows: Iterable[Optional[LearningProgress]],
    days: int = 14,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Return review counts per day for the next ``days`` calendar days.

    Each entry: ``{"date": "YYYY-MM-DD", "label": "Mon", "count": int}``.
    Days are in UTC, starting from today (inclusive).
    """
    now_dt = now or utc_now()
    today = now_dt.date() if hasattr(now_dt, "date") else now_dt

    counts: dict = {}
    for progress in progress_rows:
        if progress is None or progress.next_review_at is None:
            continue
        naive = _naive_utc(progress.next_review_at)
        if naive is None:
            continue
        d = naive.date()
        if today <= d < today + timedelta(days=days):
            counts[d] = counts.get(d, 0) + 1

    result = []
    for i in range(days):
        d = today + timedelta(days=i)
        result.append({
            "date": d.isoformat(),
            "label": d.strftime("%a"),
            "count": counts.get(d, 0),
        })
    return result


def compute_study_streak(
    completed_sessions: Iterable[object],
    today: Optional[datetime] = None,
) -> int:
    """Compute the frozen study streak from completed-session timestamps or rows."""
    today_date = (today or utc_now()).date()
    unique_dates = sorted(
        {
            session_date
            for item in completed_sessions
            if (session_date := _as_date(item)) is not None
        },
        reverse=True,
    )

    if not unique_dates or unique_dates[0] < today_date - timedelta(days=1):
        return 0

    streak = 1
    for index in range(1, len(unique_dates)):
        if unique_dates[index] == unique_dates[index - 1] - timedelta(days=1):
            streak += 1
        else:
            break

    return streak


def compute_mastery_stats(units: list[LearningUnit], now: Optional[datetime] = None) -> dict:
    """Compute passive, recall, and mastered percentages for a unit list."""
    total_count = len(units)
    if total_count == 0:
        return {
            "passive_pct": 0.0,
            "recall_pct": 0.0,
            "mastered_pct": 0.0,
            "mastered_count": 0,
            "total_count": 0,
        }

    passive_count = 0
    recall_count = 0
    mastered_count = 0

    for unit in units:
        progress = unit.progress
        if progress and progress.introduced_at is not None:
            passive_count += 1
            if progress.last_recall_result == RecallResult.CORRECT:
                recall_count += 1
        if is_mastered(progress, now):
            mastered_count += 1

    return {
        "passive_pct": round((passive_count / total_count) * 100, 1),
        "recall_pct": round((recall_count / total_count) * 100, 1),
        "mastered_pct": round((mastered_count / total_count) * 100, 1),
        "mastered_count": mastered_count,
        "total_count": total_count,
    }
