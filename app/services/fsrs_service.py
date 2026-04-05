"""FSRS integration service.

Phase 1 — populate fsrs_stability / fsrs_difficulty / fsrs_last_review
           from existing SRS-lite data. No behavior change.

Phase 2 — shadow mode: after each answer compute the FSRS-suggested interval
           alongside the old one and log the difference. No scheduling change.

Phase 3 (not yet implemented) — replace compute_next_review_at with FSRS.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.learning_unit import LearningProgress, RecallResult
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FSRS_REQUEST_RETENTION = 0.9   # target recall probability (tunable: 0.85–0.95)
FSRS_DEFAULT_STABILITY = 0.5   # new card — fragile memory
FSRS_DEFAULT_DIFFICULTY = 5.0  # new card — unknown difficulty
FSRS_MAX_STABILITY = 30.0      # cap to guard against data anomalies on init


def _load_fsrs_components():
    """Return external FSRS classes when the optional dependency is installed."""
    from fsrs import Card, Rating, Scheduler, State

    return Card, Rating, Scheduler, State


# ---------------------------------------------------------------------------
# S / D initialisation from existing SRS-lite data
# ---------------------------------------------------------------------------

def _compute_initial_stability(progress: LearningProgress) -> float:
    """
    Estimate FSRS stability from the existing scheduled interval.

    Stability = how many days until 90% recall probability.
    Best approximation from SRS-lite: the interval between last review and
    next scheduled review (not the remaining time — avoids timing bias).

    Falls back to FSRS_DEFAULT_STABILITY when timestamps are missing.
    """
    if progress.last_seen is None or progress.next_review_at is None:
        return FSRS_DEFAULT_STABILITY

    last = progress.last_seen
    nxt = progress.next_review_at

    # Normalize to naive (both columns stored as naive UTC in this app)
    if hasattr(last, "tzinfo") and last.tzinfo is not None:
        from datetime import UTC
        last = last.astimezone(UTC).replace(tzinfo=None)
    if hasattr(nxt, "tzinfo") and nxt.tzinfo is not None:
        from datetime import UTC
        nxt = nxt.astimezone(UTC).replace(tzinfo=None)

    interval_days = (nxt - last).total_seconds() / 86400.0
    stability = max(1.0, interval_days)
    return min(stability, FSRS_MAX_STABILITY)


def _compute_initial_difficulty(progress: LearningProgress) -> float:
    """
    Estimate FSRS difficulty from confidence_score and recall_fail_streak.

    Formula (reviewed design):
        D = 5.0 + (fail_streak * 0.3) - (confidence * 2.0)
        D = clamp(D, 1.0, 10.0)

    High confidence → easier (D near 1).
    High fail streak → harder (D near 10).
    Softened coefficients prevent extreme skew.
    """
    fail_streak = progress.recall_fail_streak or 0
    confidence = progress.confidence_score or 0.0

    d = 5.0 + (fail_streak * 0.3) - (confidence * 2.0)
    return max(1.0, min(10.0, d))


def init_fsrs_fields(progress: LearningProgress) -> None:
    """Compute and set fsrs_stability, fsrs_difficulty, fsrs_last_review in-place.

    Does NOT commit. Caller is responsible for db.commit().
    Safe to call on already-initialised rows (idempotent — skips if all three
    fields are already set).
    """
    already_set = (
        progress.fsrs_stability is not None
        and progress.fsrs_difficulty is not None
        and progress.fsrs_last_review is not None
    )
    if already_set:
        return

    progress.fsrs_stability = _compute_initial_stability(progress)
    progress.fsrs_difficulty = _compute_initial_difficulty(progress)
    progress.fsrs_last_review = progress.last_seen  # mirrors current last_seen


# ---------------------------------------------------------------------------
# Batch backfill (Phase 1 entry point)
# ---------------------------------------------------------------------------

def backfill_fsrs_fields(db: Session, batch_size: int = 500) -> dict:
    """Populate FSRS fields for all LearningProgress rows that lack them.

    Processes in batches to avoid loading all rows at once.
    Returns:
        populated  – rows updated
        skipped    – rows already had all three FSRS fields
        total      – total rows examined
    """
    populated = 0
    skipped = 0
    offset = 0

    while True:
        batch = (
            db.query(LearningProgress)
            .offset(offset)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        for progress in batch:
            already_set = (
                progress.fsrs_stability is not None
                and progress.fsrs_difficulty is not None
                and progress.fsrs_last_review is not None
            )
            if already_set:
                skipped += 1
            else:
                init_fsrs_fields(progress)
                db.add(progress)
                populated += 1

        db.commit()
        offset += batch_size

    total = populated + skipped
    logger.info(
        "FSRS backfill complete: total=%d populated=%d skipped=%d",
        total, populated, skipped,
    )
    return {"total": total, "populated": populated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Rating mapping (used from Phase 2 onwards)
# ---------------------------------------------------------------------------

def recall_result_to_fsrs_rating(
    result: RecallResult,
    confidence_score: float,
) -> int:
    """Map a RecallResult + confidence to an FSRS rating (1–4).

    FSRS ratings:
        1 = Again  (complete failure)
        2 = Hard   (recalled with difficulty)
        3 = Good   (normal recall)
        4 = Easy   (effortless recall)

    Mapping:
        FAILED               → 1
        PARTIAL              → 2
        CORRECT (conf < 0.85) → 3
        CORRECT (conf ≥ 0.85) → 4
    """
    if result == RecallResult.FAILED:
        return 1
    if result == RecallResult.PARTIAL:
        return 2
    # CORRECT
    if confidence_score >= 0.85:
        return 4
    return 3


# ---------------------------------------------------------------------------
# Retrievability helper (pure math — no library dependency)
# ---------------------------------------------------------------------------

def compute_retrievability(stability: float, days_elapsed: float) -> float:
    """R(t) = (1 + t / (9 * S)) ** -1  — FSRS exponential forgetting curve.

    Returns probability of recall (0.0–1.0).
    """
    if stability <= 0:
        return 0.0
    return (1 + days_elapsed / (9.0 * stability)) ** -1


def compute_next_interval_days(stability: float) -> float:
    """Compute the next interval (days) that achieves request_retention.

    Derived from inverting R(t) = request_retention:
        t = 9 * S * (R^(-1) - 1)
    where R = FSRS_REQUEST_RETENTION.
    """
    if stability <= 0:
        return 1.0
    r = FSRS_REQUEST_RETENTION
    interval = 9.0 * stability * (r ** -1 - 1)
    return max(1.0, interval)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _apply_builtin_fsrs_fallback(
    progress: LearningProgress,
    fsrs_rating: int,
    prev_last_seen: Optional[datetime],
    now_dt: datetime,
) -> datetime:
    """Approximate FSRS updates when the optional `fsrs` package is unavailable.

    This is intentionally simple and conservative. It preserves monotonic
    scheduling behavior and updates the stored FSRS-like state so recall-mode
    reviews keep progressing instead of collapsing to a fixed 1-day interval.
    """
    previous_stability = float(progress.fsrs_stability or FSRS_DEFAULT_STABILITY)
    previous_difficulty = float(progress.fsrs_difficulty or FSRS_DEFAULT_DIFFICULTY)

    elapsed_days = 0.0
    if prev_last_seen is not None:
        elapsed_days = max(0.0, (now_dt - prev_last_seen).total_seconds() / 86400.0)

    retrievability = compute_retrievability(previous_stability, elapsed_days)

    difficulty_delta = {
        1: 0.6,
        2: 0.25,
        3: -0.1,
        4: -0.35,
    }.get(fsrs_rating, 0.0)
    new_difficulty = _clamp(previous_difficulty + difficulty_delta, 1.0, 10.0)

    if fsrs_rating == 1:
        new_stability = max(FSRS_DEFAULT_STABILITY, previous_stability * 0.45)
    else:
        recall_bonus = {
            2: 0.15,
            3: 0.45,
            4: 0.8,
        }.get(fsrs_rating, 0.0)
        difficulty_factor = (11.0 - new_difficulty) / 10.0
        retrievability_factor = 1.0 + ((1.0 - retrievability) * 0.8)
        new_stability = previous_stability * (1.0 + recall_bonus * difficulty_factor * retrievability_factor)
        new_stability = max(previous_stability * 0.9, new_stability)

    new_stability = _clamp(new_stability, FSRS_DEFAULT_STABILITY, FSRS_MAX_STABILITY)

    progress.fsrs_stability = new_stability
    progress.fsrs_difficulty = new_difficulty
    now_naive = now_dt.replace(tzinfo=None) if now_dt.tzinfo else now_dt
    progress.fsrs_last_review = now_naive

    interval_days = 1.0 if fsrs_rating == 1 else compute_next_interval_days(new_stability)
    due = now_naive + timedelta(days=interval_days)

    logger.warning(
        "fsrs package unavailable; using builtin fallback for unit_id=%s rating=%s stability=%.2f difficulty=%.2f interval_days=%.1f",
        progress.unit_id,
        fsrs_rating,
        new_stability,
        new_difficulty,
        interval_days,
    )
    return due


# ---------------------------------------------------------------------------
# Phase 2: shadow logging
# ---------------------------------------------------------------------------

def shadow_log_fsrs_interval(
    progress: LearningProgress,
    old_next_review_at: datetime,
    fsrs_rating: int,
    prev_last_seen: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> None:
    """Log the FSRS-suggested interval vs the current SRS-lite interval.

    Uses the real FSRS scheduler (not just the static formula) so failed-answer
    intervals are accurate. Does NOT change any DB state — pure observation.

    prev_last_seen: last_seen captured BEFORE the answer update, used for accurate
    retrievability (how forgotten the word was at review time).
    """
    if not progress.fsrs_stability or not progress.fsrs_difficulty:
        return

    try:
        Card, Rating, Scheduler, State = _load_fsrs_components()
        from datetime import timezone

        now_dt = now or utc_now()

        def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        now_utc = _to_utc(now_dt)
        old_utc = _to_utc(old_next_review_at)
        prev_utc = _to_utc(prev_last_seen)

        # Build Card from our current FSRS state (all existing words are in Review state)
        card = Card(
            state=State.Review,
            stability=progress.fsrs_stability,
            difficulty=progress.fsrs_difficulty,
            last_review=prev_utc,
            due=prev_utc,  # doesn't affect interval calculation
        )

        # Run real FSRS scheduler — discard result, only read the interval
        scheduler = Scheduler()
        new_card, _ = scheduler.review_card(card, Rating(fsrs_rating), review_datetime=now_utc)
        fsrs_interval = round((new_card.due - now_utc).total_seconds() / 86400, 1)

        old_interval = (
            round((old_utc - now_utc).total_seconds() / 86400, 1)
            if old_utc else None
        )

        days_since_last = (
            (now_utc - prev_utc).total_seconds() / 86400
            if prev_utc else 0.0
        )
        retrievability = round(
            compute_retrievability(progress.fsrs_stability, days_since_last), 3
        )

        logger.info(
            "fsrs_shadow unit_id=%s rating=%s old_interval=%s fsrs_interval=%s "
            "confidence=%.3f fail_streak=%s stability=%.2f difficulty=%.2f retrievability=%.3f",
            progress.unit_id,
            fsrs_rating,
            old_interval,
            fsrs_interval,
            progress.confidence_score or 0.0,
            progress.recall_fail_streak or 0,
            progress.fsrs_stability,
            progress.fsrs_difficulty,
            retrievability,
        )
    except Exception as e:
        logger.debug("fsrs_shadow skipped for unit_id=%s: %s", progress.unit_id, e)


# ---------------------------------------------------------------------------
# Phase 3: FSRS scheduling (replaces SRS-lite for recall-mode answers)
# ---------------------------------------------------------------------------

def apply_fsrs_scheduling(
    progress: LearningProgress,
    fsrs_rating: int,
    prev_last_seen: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> datetime:
    """Run the FSRS scheduler, update fsrs_stability/difficulty/last_review in-place,
    and return the next review datetime (naive UTC).

    Phase 3 entry point — called from session_service for recall-mode answers only.
    Does NOT commit. Caller is responsible for db.flush()/commit().

    Falls back to a 1-day interval on any library error (safe degradation).

    prev_last_seen: last_seen captured BEFORE the answer update (for accurate elapsed time).
    now: the moment of review (defaults to utc_now()).
    """
    now_dt = now or utc_now()

    try:
        Card, Rating, Scheduler, State = _load_fsrs_components()
        from datetime import timezone

        def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        now_utc = _to_utc(now_dt)
        prev_utc = _to_utc(prev_last_seen) or now_utc  # fallback: no prior review

        card = Card(
            state=State.Review,
            stability=progress.fsrs_stability,
            difficulty=progress.fsrs_difficulty,
            last_review=prev_utc,
            due=prev_utc,
        )

        scheduler = Scheduler()
        new_card, _ = scheduler.review_card(card, Rating(fsrs_rating), review_datetime=now_utc)

        # Update FSRS state in-place
        progress.fsrs_stability = new_card.stability
        progress.fsrs_difficulty = new_card.difficulty
        # Store naive UTC (consistent with app convention)
        now_naive = now_dt.replace(tzinfo=None) if now_dt.tzinfo else now_dt
        progress.fsrs_last_review = now_naive

        # Return naive UTC due date
        due = new_card.due
        if due.tzinfo is not None:
            due = due.replace(tzinfo=None)

        logger.debug(
            "fsrs_phase3 unit_id=%s rating=%s stability=%.2f difficulty=%.2f interval_days=%.1f",
            progress.unit_id,
            fsrs_rating,
            new_card.stability,
            new_card.difficulty,
            (due - now_naive).total_seconds() / 86400,
        )
        return due

    except ModuleNotFoundError:
        return _apply_builtin_fsrs_fallback(progress, fsrs_rating, prev_last_seen, now_dt)
    except Exception as e:
        logger.warning("apply_fsrs_scheduling failed for unit_id=%s: %s — falling back to builtin scheduler", progress.unit_id, e)
        return _apply_builtin_fsrs_fallback(progress, fsrs_rating, prev_last_seen, now_dt)
