"""Session generation and management service."""

import logging
import random
import re
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Literal, Optional

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.curriculum.lessons_config import build_lesson_to_vocab
from app.curriculum.themes_service import get_theme_by_id, get_theme_by_vocabulary_id
from app.models.learning_unit import LearningUnit, LearningProgress, RecallResult
from app.models.session import (
    LearningSession,
    SessionLifecycleStatus,
    SessionUnit,
    StudyModeType,
    is_recall_like_study_mode,
)
from app.models.vocabulary import Vocabulary
from app.services.analytics_service import record_study_answer_event
from app.services.progress_metrics_service import (
    MASTERY_THRESHOLD,
    WEAK_THRESHOLD as PROGRESS_WEAK_THRESHOLD,
    compute_study_streak,
    count_weak,
)
from app.services.fsrs_service import apply_fsrs_scheduling, init_fsrs_fields, recall_result_to_fsrs_rating
from app.services.vocab_path_service import classify_vocab_source_pdf
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


ACTIVE_SESSION_STATUSES = (
    SessionLifecycleStatus.CREATED,
    SessionLifecycleStatus.ACTIVE,
)


@dataclass(frozen=True)
class SelectionRequest:
    mode: Literal["due_only", "weak_only", "normal"]
    study_mode: StudyModeType
    session_size: int
    pool_kind: Literal["due", "weak", "normal"]
    apply_balancing: bool = False
    apply_reinforcement_only: bool = False
    use_due_first_split: bool = False
    use_direct_query_limit: bool = False
    weak_only_padding: bool = False
    source_pdfs: Optional[list[str]] = None
    theme: Optional[str] = None
    theme_vocab_ids: Optional[set[int]] = None
    lesson_vocab_ids: Optional[set[int]] = None
    now: Optional[datetime] = None
    due_units_query: Optional[object] = None
    available_due_count: Optional[int] = None
    exclude_unit_ids: frozenset[int] = frozenset()
    balanced_never_seen_mix: bool = False
    new_words_focus: bool = False


RecallControllerMode = Literal["legacy_adaptive", "observe_only", "v3_experimental"]


@dataclass(frozen=True)
class RecallControllerSnapshot:
    mode: RecallControllerMode
    recall_depth: float
    weak_ratio: float
    due_ratio: float
    reinforcement_depth_bias: int
    session_difficulty_signal: float


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize datetime to naive UTC to avoid naive/aware comparison issues."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _utc_now_naive() -> datetime:
    """Current UTC timestamp as naive datetime (for DB compatibility)."""
    return _naive_utc(utc_now())


def _selection_source_top_n(units: list[LearningUnit], limit: int = 5) -> str:
    """Compact top-N source_pdf counts for single-line logs."""
    if not units:
        return "[]"
    counts = Counter((getattr(u, "source_pdf", None) or "") for u in units)
    return repr(counts.most_common(limit))


def _selection_confidence_min_avg_max(units: list[LearningUnit]) -> tuple[float, float, float]:
    vals: list[float] = []
    for u in units:
        prog = getattr(u, "progress", None)
        if prog is not None and getattr(prog, "confidence_score", None) is not None:
            vals.append(float(prog.confidence_score))
    if not vals:
        return (0.0, 0.0, 0.0)
    return (min(vals), sum(vals) / len(vals), max(vals))


# Session size constant
SESSION_SIZE = settings.session_size
DAILY_REVIEW_CAP = 160
MAX_DUPLICATES_PER_UNIT = 3
# Offer "retry failed words" after recall session when enough distinct units failed.
RECALL_RETRY_FAILED_MIN = 3
# After due-only session: suggest weak recall when enough weak units exist (global scope).
WEAK_FOLLOW_UP_MIN = 5

# Ephemeral: session_id -> { unit_id: selection_reason }. Mirrors SessionUnit.selection_reason
# when the row was just created; single-process. Prefer DB column when cache is cold.
_SESSION_SELECTION_REASONS: dict[int, dict[int, str]] = {}
# Ephemeral: passive sessions that should offer recall follow-up (same words) after completion.
# Values: "weak" (weak-only pool), "lesson" (curriculum_mode=lesson passive).
_SESSION_PASSIVE_RECALL_AFTER: dict[int, Literal["weak", "lesson"]] = {}
MAX_SELECTION_REASON_CACHE = 200

# Punctuation characters to ignore in lexical mode (includes Polish typographic quotes „ ")
PUNCTUATION_CHARS = ".?!,;:\"'()[]{}…—–-\u201e\u201c\u201d\u201f\u00ab\u00bb"

# ===================
# Time Decay Configuration
# ===================
# Effective confidence decays over time to prioritize words not seen recently.
# Formula: effective_confidence = confidence_score * time_decay
# time_decay = max(MIN_DECAY, 1.0 - days_since_seen * DECAY_RATE)
#
# With default values:
# - Day 0: decay = 1.0 (100%)
# - Day 3: decay = 0.76 (76%)
# - Day 7: decay = 0.44 (44%)
# - Day 8+: decay = 0.4 (40% minimum)
TIME_DECAY_RATE = 0.08   # Decay 8% per day
TIME_DECAY_MIN = 0.4     # Minimum decay factor (40%)

# Thresholds using effective confidence
WEAK_THRESHOLD = 0.5     # Below this = weak

# PL–UA / czytaj lesson progression: all words introduced + share mastered (see _is_lesson_completed)
LESSON_PASS_RATIO = 0.8  # Fraction of lesson words that must meet MASTERY_THRESHOLD

# ===================
# Weighted Random Session Configuration
# ===================
# Bucket composition (target percentages)
BUCKET_NEW_PERCENT = 0.30       # 30% new words
BUCKET_WEAK_PERCENT = 0.40      # 40% weak/failed words
BUCKET_REVIEW_PERCENT = 0.30    # 30% review (known words)


def _compute_new_words_readiness(
    words_introduced_today: int,
    max_new_per_day: int,
    accuracy_today: float,
    weak_count: int,
) -> tuple[float, Optional[str]]:
    """
    Adaptive gate: returns (bucket_fraction, gate_reason).

    gate_reason is one of: "weak" | "fatigue" | "accuracy" | None (ready).
    Budget freed by suppressing new words is redistributed to weak/review by the caller.
    """
    # Gate 1: too many weak words — must consolidate before expanding
    if weak_count >= SESSION_SIZE:
        logger.info(
            "readiness_gate: new_words suppressed — weak_count=%s >= SESSION_SIZE=%s",
            weak_count, SESSION_SIZE,
        )
        return 0.0, "weak"

    # Gate 2: fatigue — already introduced ≥50% of daily cap
    fatigue_threshold = max(1, max_new_per_day) * 0.5
    if words_introduced_today >= fatigue_threshold:
        logger.info(
            "readiness_gate: new_words suppressed — words_introduced_today=%s >= fatigue_threshold=%.0f",
            words_introduced_today, fatigue_threshold,
        )
        return 0.0, "fatigue"

    # Gate 3: poor accuracy — user is struggling, reinforce first
    # Only applies when we have meaningful data (skip if no sessions yet today)
    if accuracy_today is not None and accuracy_today < 0.65:
        logger.info(
            "readiness_gate: new_words suppressed — accuracy_today=%.2f < 0.65",
            accuracy_today,
        )
        return 0.0, "accuracy"

    return BUCKET_NEW_PERCENT, None

# SRS-lite: Due items selection
# Due items (next_review_at <= now) get priority selection
DUE_ITEMS_MAX_PERCENT = 0.70    # Due items can occupy up to 70% of session
SPILLOVER_DEPTH = 2

# Weight multipliers for selection priority
WEIGHT_BASE_NEW = 3.0           # New words have high base priority
WEIGHT_BASE_WEAK = 2.0          # Weak words have medium-high priority
WEIGHT_BASE_REVIEW = 1.0        # Review words have base priority

# Failure multiplier: words with recall failures get boosted
WEIGHT_FAILURE_MULTIPLIER = 2.0  # 2x weight for failed words

# Recall penalty: words that failed recall recently are prioritized
WEIGHT_RECALL_PENALTY = 1.5     # 1.5x weight for last_recall_result == failed

# Stuck boost: words with recall_fail_streak >= 3 are "stuck" and need extra exposure
WEIGHT_STUCK_BOOST = 2.5        # 2.5x weight for stuck words (fail_streak >= 3)
WEIGHT_STUCK_THRESHOLD = 3      # Minimum recall_fail_streak to be considered stuck

# Maximum weight cap to prevent priority explosion
WEIGHT_MAX_CAP = 10.0           # Cap total weight to prevent runaway priority

# Soft diversity: down-weight recently seen non-due units (reduces tail repetition; due items exempt)
RECENCY_DIVERSITY_WINDOW_SHORT_SEC = 600   # 10 minutes
RECENCY_DIVERSITY_WINDOW_LONG_SEC = 3600   # 1 hour
RECENCY_DIVERSITY_FACTOR_SHORT = 0.7
RECENCY_DIVERSITY_FACTOR_LONG = 0.85

# Down-weight units that appeared in the previous completed session (non-due only; DB-loaded per request)
LAST_SESSION_DIVERSITY_FACTOR = 0.6

# Retry follow-up: cap session length so tail does not dominate (failed prefix + padding)
RETRY_SESSION_TAIL_PADDING = 10
# Follow-up tail: max cards from the same vocabulary_id (within the tail only).
FOLLOW_UP_TAIL_MAX_PER_VOCABULARY = 4


def compute_time_decay(last_seen: Optional[datetime], now: Optional[datetime] = None) -> float:
    """
    Compute time decay factor based on days since last seen.
    
    Formula: max(MIN_DECAY, 1.0 - days_since_seen * DECAY_RATE)
    
    Args:
        last_seen: Timestamp of when unit was last practiced.
        now: Current time (defaults to utcnow, injectable for testing).
        
    Returns:
        Decay factor between TIME_DECAY_MIN and 1.0.
        Returns 1.0 if never seen (no decay for new units).
    """
    if last_seen is None:
        return 1.0  # Never seen = no decay

    last_seen = _naive_utc(last_seen)
    
    if now is None:
        now = _utc_now_naive()
    else:
        now = _naive_utc(now)
    
    delta = now - last_seen
    days_since_seen = max(0, delta.total_seconds() / 86400)  # Convert to days
    
    decay = 1.0 - (days_since_seen * TIME_DECAY_RATE)
    return max(TIME_DECAY_MIN, decay)


def compute_effective_confidence(
    confidence_score: float,
    last_seen: Optional[datetime],
    now: Optional[datetime] = None,
) -> float:
    """
    Compute effective confidence with time decay applied.
    
    This is used for session unit selection and scheduling decisions only,
    NOT for updating stored values.
    
    INVARIANT: Stored confidence_score is modified ONLY by user actions (answers),
    NEVER by time decay. This function computes a transient value for selection/scheduling.
    
    Args:
        confidence_score: Stored confidence (0.0 - 1.0).
        last_seen: Timestamp of when unit was last practiced.
        now: Current time (injectable for testing).
        
    Returns:
        Effective confidence (0.0 - 1.0) with time decay applied.
    """
    decay = compute_time_decay(last_seen, now)
    return confidence_score * decay


# ===================
# SRS-Lite Review Scheduling
# ===================
# Intervals based on effective confidence level:
# - Very weak (< 0.3): Immediate review (10 minutes)
# - Weak (0.3-0.5): 1 day
# - Moderate (0.5-0.7): 3 days
# - Good (0.7-0.85): 7 days
# - Strong (≥ 0.85): 14 days
#
# INVARIANT: Stored confidence_score is modified ONLY by user actions (answers),
# NEVER by time decay. Time decay affects only the effective_confidence used
# for session selection and scheduling decisions.
SRS_INTERVAL_IMMEDIATE = timedelta(minutes=10)  # "Immediate" = 10 minutes (prevents spam)
SRS_INTERVAL_10_MIN = timedelta(minutes=10)     # Failed recall retry delay (same as immediate)
SRS_INTERVAL_1_DAY = timedelta(days=1)
SRS_INTERVAL_3_DAYS = timedelta(days=3)
SRS_INTERVAL_7_DAYS = timedelta(days=7)
SRS_INTERVAL_14_DAYS = timedelta(days=14)

# ===================
# Confidence Smoothing
# ===================
# Base alpha (SMOOTHING_NEW) is 0.3, but becomes adaptive when stability is present:
#   alpha = BASE_ALPHA + (BASE_ALPHA * stability_score)   →  range [0.3, 0.6]
# This means mature words weight new evidence more, making them more resistant
# to single failures (because a failure's raw_score=0 gets averaged against a
# large old_confidence with a smaller (1-alpha) weight... but the higher alpha
# on correct answers also lets them recover faster).
#
# INVARIANT: Stored confidence_score is modified ONLY by user actions (answers),
# NEVER by time decay. Time decay affects only the effective_confidence used
# for session selection and scheduling decisions.
CONFIDENCE_SMOOTHING_OLD = 0.7  # Weight for previous confidence (base, before stability)
CONFIDENCE_SMOOTHING_NEW = 0.3  # Weight for new raw score (base alpha)

# PARTIAL recall: bidirectional smooth target (between passive 0.3 and correct 1.0)
PARTIAL_RAW_SCORE = 0.5

# Legacy constant kept for backward compatibility in tests
PARTIAL_CONFIDENCE_PENALTY = 0.05

# ===================
# Stability Score Deltas
# ===================
# stability_score represents long-term memory maturity (0.0 → 1.0).
# It is modified ONLY by recall outcomes, never by time decay or passive mode.
STABILITY_INCREMENT_CORRECT = 0.02   # Recall correct
STABILITY_INCREMENT_PARTIAL = 0.01   # Recall partial (minor typo)
STABILITY_DECREMENT_FAILED = 0.01    # Recall failed

# Consecutive failure thresholds
FAIL_STREAK_CONFIDENCE_FLOOR_THRESHOLD = 3  # Reduce confidence floor after 3 failures
FAIL_STREAK_BLOCKED_THRESHOLD = 5           # Mark as "blocked" after 5 failures
FAIL_STREAK_CONFIDENCE_FLOOR_REDUCTION = 0.05  # How much to reduce floor per streak

# Due-only session selection
MINIMUM_DUE_RATIO = 0.5  # At least 50% of session must be true due words


def compute_next_review_at(effective_confidence: float, now: datetime) -> datetime:
    """
    Compute the next scheduled review time based on effective confidence.
    
    Pure function - deterministic and side-effect free.
    
    Scheduling rules:
    - effective_confidence < 0.3       → now + 10 minutes (immediate)
    - 0.3 ≤ ec < 0.5                   → now + 1 day
    - 0.5 ≤ ec < 0.7                   → now + 3 days
    - 0.7 ≤ ec < 0.85                  → now + 7 days
    - ec ≥ 0.85                        → now + 14 days
    
    Note: "Immediate" is defined as 10 minutes to prevent review spam.
    This is distinct from recall failure retry (also 10 min) which bypasses
    this function entirely.
    
    Args:
        effective_confidence: Confidence with time decay applied (0.0 - 1.0).
        now: Current timestamp for calculating review time.
        
    Returns:
        Datetime when the unit should next be reviewed.
    """
    if effective_confidence < 0.3:
        return now + SRS_INTERVAL_IMMEDIATE  # 10 minutes
    elif effective_confidence < 0.5:
        return now + SRS_INTERVAL_1_DAY
    elif effective_confidence < 0.7:
        return now + SRS_INTERVAL_3_DAYS
    elif effective_confidence < 0.85:
        return now + SRS_INTERVAL_7_DAYS
    else:
        return now + SRS_INTERVAL_14_DAYS


def _is_lesson_completed(db: Session, lesson_id: int, lesson_to_vocab: dict[int, list[int]]) -> bool:
    """
    Lesson is complete when every unit in the lesson is introduced and a sufficient
    fraction have stored confidence at or above MASTERY_THRESHOLD (0.85).

    Long-tail difficult words no longer block progression; weak/recovery flows
    handle remaining items.
    """
    vocab_ids = lesson_to_vocab.get(lesson_id, [])
    if not vocab_ids:
        return False

    units = (
        db.query(LearningUnit)
        .filter(LearningUnit.vocabulary_id.in_(vocab_ids))
        .all()
    )

    total = len(units)
    if total == 0:
        return False

    introduced = 0
    mastered = 0
    for unit in units:
        progress = (
            db.query(LearningProgress)
            .filter(LearningProgress.unit_id == unit.id)
            .first()
        )
        if progress is not None and progress.introduced_at is not None:
            introduced += 1
        if progress is not None and progress.confidence_score >= MASTERY_THRESHOLD:
            mastered += 1

    introduced_ok = introduced == total
    mastery_ratio = mastered / total

    if introduced_ok and mastery_ratio >= LESSON_PASS_RATIO:
        logger.info(
            "lesson completed via threshold: lesson_index=%s mastery_ratio=%.2f",
            lesson_id,
            mastery_ratio,
        )
        return True
    return False


def _detect_current_lesson(db: Session) -> int:
    lesson_to_vocab = build_lesson_to_vocab(db)
    if not lesson_to_vocab:
        return 1

    max_lesson = max(lesson_to_vocab.keys())
    for lesson_id in range(1, max_lesson + 1):
        if not _is_lesson_completed(db, lesson_id, lesson_to_vocab):
            return lesson_id
    return max_lesson


def compute_next_review(
    *,
    is_correct: bool,
    times_seen: int,
    confidence_score: Optional[float],
    last_seen: datetime,
    is_recall_mode: bool,
    recall_result: Optional[RecallResult],
    last_recall_result: Optional[RecallResult],
    previous_next_review_at: Optional[datetime],
) -> datetime:
    """
    Compute `next_review_at` for a unit after an answer is recorded.

    This centralizes the SRS-lite rules. It follows the same base structure as before
    (10min / 1 / 3 / 7 / 14 day buckets derived from effective confidence), then applies a
    gentle confidence-weighting multiplier to the chosen interval.

    Plain-English rules:
    - First compute `effective_confidence` using the same time-decay function used elsewhere.
    - Recall mode:
      - FAILED  → review in 10 minutes (bypasses normal scheduling)
      - PARTIAL → use NORMAL interval selection based on effective_confidence,
                  then apply: final_interval = min(calculated_interval, 3 days)
      - CORRECT → schedule normally based on effective confidence
    - Passive mode:
      - If the last recall attempt was FAILED, a passive "correct" MUST NOT delay review:
        keep the previous `next_review_at` (or schedule 10 min if missing), and never extend beyond now.
      - Otherwise schedule normally based on effective confidence
    
    Interval calculation order (for all normal scheduling):
      final_interval = clamp(
          base_interval * confidence_multiplier,
          min=0.5 day,
          max=21 days
      )
    
    Confidence multipliers:
      - confidence_score >= 0.85 → × 1.3
      - 0.6 <= confidence_score < 0.85 → × 1.0
      - 0.4 <= confidence_score < 0.6 → × 0.8
      - confidence_score < 0.4 → × 0.6
      - confidence_score is None → × 1.0

    Inputs include `times_seen`/`confidence_score` to keep the API explicit, even if the
    current algorithm only uses them via `effective_confidence`.
    """
    # Keep scheduling deterministic and aligned with stored timestamps:
    # use the same "now" as last_seen, just like the previous inline logic.
    now = last_seen

    effective_conf = compute_effective_confidence(
        confidence_score or 0.0,
        last_seen,
        now,
    )

    def _confidence_multiplier(conf: Optional[float]) -> float:
        """Get multiplier based on stored confidence score."""
        if conf is None:
            return 1.0
        if conf >= 0.85:
            return 1.3
        if conf >= 0.6:
            return 1.0
        if conf >= 0.4:
            return 0.8
        return 0.6

    def _apply_confidence_weighting(base_next_review_at: datetime) -> datetime:
        """
        Apply confidence weighting to base interval with explicit clamp.
        
        Calculation order:
          1. Compute base_interval from base_next_review_at
          2. Apply confidence multiplier: weighted = base_interval * multiplier
          3. Clamp result: final = clamp(weighted, min=0.5 day, max=21 days)
        
        Args:
            base_next_review_at: The base scheduled review time from bucket selection.
        
        Returns:
            Final review datetime with confidence weighting applied.
        """
        # Step 1: Get base interval in days
        base_interval_days = (base_next_review_at - now).total_seconds() / 86400.0
        
        # Step 2: Apply confidence multiplier
        multiplier = _confidence_multiplier(confidence_score)
        weighted_days = base_interval_days * multiplier
        
        # Step 3: Clamp to hard bounds (0.5 day min, 21 days max)
        final_interval_days = max(0.5, min(21.0, weighted_days))

        if settings.srs_debug:
            logger.debug(
                "SRS: is_correct=%s recall_result=%s confidence=%s base_days=%.3f multiplier=%.3f final_days=%.3f",
                is_correct,
                recall_result.value if recall_result else None,
                confidence_score,
                base_interval_days,
                multiplier,
                final_interval_days,
            )

        return now + timedelta(days=final_interval_days)

    if is_recall_mode and recall_result is not None:
        # Recall mode scheduling
        if recall_result == RecallResult.FAILED:
            # Failed recall → retry in 10 minutes (bypasses normal scheduling entirely)
            return now + SRS_INTERVAL_10_MIN
        
        if recall_result == RecallResult.PARTIAL:
            # Partial recall scheduling:
            # 1. Use NORMAL interval selection based on effective_confidence
            # 2. Apply confidence weighting (includes 0.5 day min, 21 day max clamp)
            # 3. Apply additional cap: final_interval = min(calculated_interval, 3 days)
            base_next = compute_next_review_at(effective_conf, now)
            weighted_result = _apply_confidence_weighting(base_next)
            # Hard cap at 3 days for PARTIAL
            max_partial = now + SRS_INTERVAL_3_DAYS
            return min(weighted_result, max_partial)
        
        # CORRECT → normal scheduling based on effective confidence
        base_next = compute_next_review_at(effective_conf, now)
        return _apply_confidence_weighting(base_next)

    # Passive mode scheduling
    if is_correct and last_recall_result == RecallResult.FAILED:
        # CRITICAL: Passive success MUST NOT extend next_review_at if last recall failed.
        # Keep the existing review time. If no existing review time, schedule 10 min.
        if previous_next_review_at is None:
            return now + SRS_INTERVAL_10_MIN
        # Don't extend - keep existing or use now + 10 min if past
        return min(previous_next_review_at, now + SRS_INTERVAL_10_MIN)

    # Normal passive: schedule based on effective confidence
    base_next = compute_next_review_at(effective_conf, now)
    return _apply_confidence_weighting(base_next)


def apply_due_load_cap(
    db: Session,
    desired: datetime,
    max_per_day: int,
) -> datetime:
    """
    If the calendar day of `desired` already has >= max_per_day reviews scheduled,
    return the same time on the next day with capacity (up to 14 days ahead).
    Otherwise return `desired` unchanged. Used when smooth_due_load is enabled.
    """
    target_date = desired.date()
    for offset in range(14):
        d = target_date + timedelta(days=offset)
        cnt = (
            db.query(func.count(LearningProgress.id))
            .filter(
                LearningProgress.next_review_at.isnot(None),
                func.date(LearningProgress.next_review_at) == d.isoformat(),
            )
            .scalar()
            or 0
        )
        if cnt < max_per_day:
            if offset == 0:
                return desired
            return datetime.combine(d, desired.time())
    return desired


def spread_overdue_reviews(
    db: Session,
    max_per_day: int,
    window_days: int = 7,
) -> int:
    """
    Reschedule overdue reviews (next_review_at < now) across the next
    window_days so each day has at most max_per_day. Preserves order
    (most overdue first get today). Returns number of rows updated.
    """
    now = _utc_now_naive()
    overdue = (
        db.query(LearningProgress)
        .filter(
            LearningProgress.next_review_at.isnot(None),
            LearningProgress.next_review_at < now,
        )
        .order_by(LearningProgress.next_review_at.asc())
        .all()
    )
    if not overdue:
        return 0

    # Assign to days [today, today+window_days), filling each day up to max_per_day
    start = now.date()
    day_slots = []
    for i in range(window_days):
        d = start + timedelta(days=i)
        day_slots.extend([d] * max_per_day)
    for i, progress in enumerate(overdue):
        if i >= len(day_slots):
            break
        d = day_slots[i]
        # Same time-of-day pattern as rebalance script: 09:00 + small offset
        new_dt = datetime.combine(d, now.time().replace(hour=9, minute=0, second=0, microsecond=0))
        if i < 60:
            new_dt = new_dt.replace(second=min(i, 59))
        progress.next_review_at = new_dt
    db.commit()
    logger.info(
        "Spread %d overdue reviews across the next %d days (max %d/day)",
        len(overdue),
        window_days,
        max_per_day,
    )
    return len(overdue)


def ensure_overdue_spread(db: Session) -> bool:
    """
    If overdue count exceeds settings.spread_overdue_when_above, spread
    those reviews across the next 7 days (max_due_per_day per day).
    Call when loading the dashboard or before creating a session.
    Returns True if a spread was performed.
    """
    if not settings.smooth_due_load:
        return False
    now = _utc_now_naive()
    overdue_count = (
        db.query(func.count(LearningProgress.id))
        .filter(
            LearningProgress.next_review_at.isnot(None),
            LearningProgress.next_review_at < now,
        )
        .scalar()
        or 0
    )
    if overdue_count <= settings.spread_overdue_when_above:
        return False
    spread_overdue_reviews(db, settings.max_due_per_day, window_days=7)
    return True


def apply_confidence_smoothing(
    old_confidence: float,
    raw_score: float,
    stability_score: float = 0.0,
) -> float:
    """
    Apply stability-aware exponential smoothing to confidence updates.
    
    The smoothing alpha adapts based on the word's stability_score:
      alpha = 0.3 + (0.3 * stability_score)   →  range [0.3, 0.6]
    
    Higher stability → higher alpha → new evidence is weighted more.
    For correct answers this speeds recovery; for mature words that fail,
    the large old_confidence * (1-alpha) term still preserves most of
    the existing confidence, making them resilient to isolated slips.
    
    Formula: new_confidence = old_confidence * (1 - alpha) + raw_score * alpha
    
    Args:
        old_confidence: Previous confidence score (0.0 - 1.0).
        raw_score: New raw score from this answer (0.0, 0.5, or 1.0 typically).
        stability_score: Long-term memory maturity (0.0 - 1.0). Default 0.0
            preserves the original fixed-alpha behavior for callers that
            don't provide it (e.g., passive mode).
        
    Returns:
        Smoothed confidence score (0.0 - 1.0).
    """
    # Adaptive alpha: base 0.3 scaled up by stability (max 0.6)
    alpha = CONFIDENCE_SMOOTHING_NEW + (CONFIDENCE_SMOOTHING_NEW * stability_score)
    smoothed = old_confidence * (1.0 - alpha) + raw_score * alpha
    return max(0.0, min(1.0, smoothed))


def apply_partial_penalty(confidence: float) -> float:
    """
    Apply a small penalty to confidence for PARTIAL recall results.
    
    DEPRECATED: Kept for backward compatibility. New PARTIAL handling uses
    raw_score=PARTIAL_RAW_SCORE through apply_confidence_smoothing instead of subtracting
    a fixed penalty after smoothing.
    
    Args:
        confidence: Current confidence score.
        
    Returns:
        Confidence with penalty applied, floored at 0.0.
    """
    return max(0.0, confidence - PARTIAL_CONFIDENCE_PENALTY)


def get_due_reason(progress: "LearningProgress", now: Optional[datetime] = None) -> Optional[str]:
    """
    Determine the primary reason why a word is due for review.
    
    This helper is used for UI hints and debugging.
    
    Args:
        progress: The LearningProgress record to analyze.
        now: Current timestamp (defaults to utcnow).
        
    Returns:
        One of:
        - "failed_recall": Word failed recent recall and is due for retry
        - "time_decay": Word hasn't been seen recently and effective confidence decayed
        - "low_confidence": Word has low stored confidence
        - None: Word is not due for review
    """
    if now is None:
        now = _utc_now_naive()
    
    # Check if word is actually due
    if progress.next_review_at is None or progress.next_review_at > now:
        return None
    
    # Check for recent failed recall (highest priority reason)
    if progress.last_recall_result == RecallResult.FAILED:
        return "failed_recall"
    
    # Check for time decay
    if progress.last_seen is not None:
        effective_conf = compute_effective_confidence(
            progress.confidence_score,
            progress.last_seen,
            now,
        )
        # If effective confidence is significantly lower than stored confidence,
        # time decay is the primary reason
        if progress.confidence_score > 0.5 and effective_conf < progress.confidence_score * 0.8:
            return "time_decay"
    
    # Default to low confidence
    if progress.confidence_score < WEAK_THRESHOLD:
        return "low_confidence"
    
    # If none of the above, it's likely time decay for moderate confidence words
    return "time_decay"


class EvaluationMode(str, Enum):
    """Mode for evaluating recall answers."""
    STRICT = "strict"    # Exact match after normalization (case, whitespace, unicode)
    LEXICAL = "lexical"  # Also ignores punctuation


class InsufficientUnitsError(Exception):
    """Raised when there aren't enough units to create a session."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class NoDueUnitsInThemeError(Exception):
    """Raised when a theme-scoped due-only session has no due units."""

    def __init__(self, theme: str):
        self.theme = theme
        super().__init__(f"No due words in theme: {theme}")


class NoDueUnitsError(Exception):
    """Raised when due-only practice has zero due units in scope (non-theme)."""

    def __init__(self, message: str = "No words are due for review right now."):
        super().__init__(message)


def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calculate the Levenshtein (edit) distance between two strings.
    
    This is used to detect minor typos (≤1 character difference).
    
    Args:
        s1: First string.
        s2: Second string.
        
    Returns:
        Number of single-character edits needed to transform s1 into s2.
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Calculate costs for insertions, deletions, and substitutions
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    return previous_row[-1]


def _levenshtein_distance(a: str, b: str) -> int:
    """Simple DP Levenshtein distance helper."""
    if len(a) < len(b):
        return _levenshtein_distance(b, a)
    if len(b) == 0:
        return len(a)

    previous_row = list(range(len(b) + 1))
    for i, c1 in enumerate(a):
        current_row = [i + 1]
        for j, c2 in enumerate(b):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


@dataclass
class AnswerEvaluation:
    """Result of evaluating a recall mode answer."""
    is_correct: bool
    result: RecallResult  # correct | partial | failed
    user_input: str
    normalized_input: str
    expected_answer: str
    normalized_expected: str
    evaluation_mode: EvaluationMode
    punctuation_only_mistake: bool = False  # True if only punctuation differs
    typo_distance: int = 0  # Levenshtein distance if partial match


def normalize_input(text: str, strip_punctuation: bool = False) -> str:
    """
    Normalize text for comparison.
    
    - Lowercase
    - Strip whitespace
    - Normalize Unicode (NFC)
    - Remove extra whitespace
    - Optionally remove punctuation
    
    Args:
        text: Raw user input.
        strip_punctuation: If True, remove punctuation characters.
        
    Returns:
        Normalized string for comparison.
    """
    if not text:
        return ""
    
    # Normalize Unicode to NFC form (composed characters)
    normalized = unicodedata.normalize("NFC", text)
    
    # Lowercase
    normalized = normalized.lower()
    
    # Strip leading/trailing whitespace
    normalized = normalized.strip()
    
    # Collapse multiple spaces to single space
    normalized = re.sub(r"\s+", " ", normalized)
    
    # Optionally remove punctuation
    if strip_punctuation:
        for char in PUNCTUATION_CHARS:
            normalized = normalized.replace(char, "")
        # Clean up any resulting double spaces
        normalized = re.sub(r"\s+", " ", normalized).strip()
    
    return normalized


def evaluate_answer(
    user_input: str,
    expected: str,
    mode: EvaluationMode = EvaluationMode.LEXICAL,
) -> AnswerEvaluation:
    """
    Evaluate user's answer against expected answer.
    
    Uses deterministic string comparison after normalization.
    Returns a result of correct, partial, or failed based on:
    - correct: Exact match (after normalization)
    - partial: ≤1 character typo OR punctuation-only difference
    - failed: More than 1 character difference
    
    Note: Diacritics are REQUIRED (żółć ≠ zolc).
    
    Args:
        user_input: User's typed answer.
        expected: Expected correct answer (learning_unit.text).
        mode: Evaluation mode (strict or lexical).
        
    Returns:
        AnswerEvaluation with comparison results and RecallResult.
    """
    strip_punctuation = mode == EvaluationMode.LEXICAL
    
    normalized_input = normalize_input(user_input, strip_punctuation=strip_punctuation)
    normalized_expected = normalize_input(expected, strip_punctuation=strip_punctuation)
    
    # Check for exact match
    is_exact_match = normalized_input == normalized_expected
    
    # Check if it's a punctuation-only mistake (in strict mode)
    punctuation_only_mistake = False
    if not is_exact_match and mode == EvaluationMode.STRICT:
        input_no_punct = normalize_input(user_input, strip_punctuation=True)
        expected_no_punct = normalize_input(expected, strip_punctuation=True)
        punctuation_only_mistake = input_no_punct == expected_no_punct
    
    # Calculate typo distance for partial credit
    typo_distance = 0
    if not is_exact_match:
        # Use normalized versions without punctuation for typo detection
        input_for_typo = normalize_input(user_input, strip_punctuation=True)
        expected_for_typo = normalize_input(expected, strip_punctuation=True)
        typo_distance = levenshtein_distance(input_for_typo, expected_for_typo)
    
    # Determine result: correct, partial, or failed
    if is_exact_match:
        result = RecallResult.CORRECT
        is_correct = True
    elif punctuation_only_mistake or typo_distance <= 1:
        # Partial credit for:
        # - Punctuation-only mistakes (strict mode)
        # - Single character typo (any mode)
        result = RecallResult.PARTIAL
        is_correct = False
    else:
        result = RecallResult.FAILED
        is_correct = False
    
    return AnswerEvaluation(
        is_correct=is_correct,
        result=result,
        user_input=user_input,
        normalized_input=normalized_input,
        expected_answer=expected,
        normalized_expected=normalized_expected,
        evaluation_mode=mode,
        punctuation_only_mistake=punctuation_only_mistake,
        typo_distance=typo_distance,
    )


class SessionService:
    """
    Service for creating and managing learning sessions.
    
    Sessions contain 50 units by default selected by weighted random sampling:
    
    Bucket composition:
    - 30% new (never seen)
    - 40% weak/failed (low confidence or recall failures)
    - 30% review (known words needing reinforcement)
    
    Weight formula:
    weight = base_priority * failure_multiplier * time_decay_multiplier * recall_penalty * stuck_boost
    
    This ensures:
    - Failed words are prioritized
    - New words are introduced steadily
    - Known words are reviewed to prevent forgetting
    - Randomness prevents predictable patterns
    """
    
    def __init__(self, db: Session, random_seed: Optional[int] = None):
        """
        Initialize session service.
        
        Args:
            db: SQLAlchemy database session.
            random_seed: Optional seed for deterministic testing.
        """
        self.db = db
        self._rng = random.Random(random_seed)
        self.random = self._rng
        self._consecutive_failures = 0
        self._difficulty_bias = 0.0
        self._session_gain = 0.0
        self._answers_count = 0
        self._target_gain_per_answer = 0.03
        self._gain_history = deque(maxlen=5)
        self._spacing_aggressiveness = 1.0
        self._recall_controller_mode: RecallControllerMode = settings.recall_controller_mode
        self._last_recall_controller_snapshot: Optional[RecallControllerSnapshot] = None
        # Set per create_session from DB; used only within that request for selection weights
        self._last_session_unit_ids: frozenset[int] = frozenset()

    def _get_ordered_unit_ids_from_session(self, session_id: int) -> list[int]:
        """Return ordered unit_ids from a completed session (by position).

        Only includes units that have been introduced (introduced_at set), in session
        position order. Aligns with recall gating and "what you just learned" UX.
        """
        session = (
            self.db.query(LearningSession)
            .filter(LearningSession.id == session_id)
            .first()
        )

        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session.status != SessionLifecycleStatus.COMPLETED:
            raise ValueError(f"Session {session_id} is not completed")

        session_units = (
            self.db.query(SessionUnit)
            .filter(SessionUnit.session_id == session_id)
            .order_by(SessionUnit.position.asc())
            .all()
        )

        unit_ids = [su.unit_id for su in session_units]

        if not unit_ids:
            return []

        progresses = (
            self.db.query(LearningProgress)
            .filter(LearningProgress.unit_id.in_(unit_ids))
            .all()
        )

        introduced_map = {
            p.unit_id: p.introduced_at is not None for p in progresses
        }

        return [
            uid for uid in unit_ids
            if introduced_map.get(uid, False)
        ]

    def _get_failed_unit_ids_from_session(self, session_id: int) -> list[int]:
        """Return unit_ids where recall result was FAILED, in session position order.

        Uses ``SessionUnit`` (recall outcomes); passive sessions typically yield an empty list.
        """
        session = (
            self.db.query(LearningSession)
            .filter(LearningSession.id == session_id)
            .first()
        )

        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session.status != SessionLifecycleStatus.COMPLETED:
            raise ValueError(f"Session {session_id} is not completed")

        rows = (
            self.db.query(SessionUnit)
            .filter(SessionUnit.session_id == session_id)
            .filter(SessionUnit.answered.is_(True))
            .filter(SessionUnit.recall_result == RecallResult.FAILED)
            .order_by(SessionUnit.position.asc())
            .all()
        )
        ordered = [su.unit_id for su in rows]
        return list(dict.fromkeys(ordered))

    def _build_follow_up_priority_units(
        self,
        *,
        priority_unit_ids: list[int],
        source_pdfs: Optional[list[str]],
        theme_vocab_ids: Optional[set[int]],
        lesson_vocab_ids: Optional[set[int]],
        mode: StudyModeType,
        relax_selection_scope: bool = False,
    ) -> list[LearningUnit]:
        """Load follow-up units in session order; recall + blocking rules.

        When ``relax_selection_scope`` is True, skip source/theme/lesson filters so priority
        units are not dropped when the UI scope differs. Used for: (1) ``retry_failed_only``
        (e.g. after due-only recall), and (2) recall follow-up after **passive** (weak passive can
        pull units outside the current lesson window; narrowing filters would empty the prefix).
        """
        if not priority_unit_ids:
            return []

        q = (
            self.db.query(LearningUnit)
            .options(joinedload(LearningUnit.progress))
            .filter(LearningUnit.id.in_(priority_unit_ids))
        )
        if not relax_selection_scope:
            q = self.apply_selection_filters(
                q,
                source_pdfs=source_pdfs,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=lesson_vocab_ids,
            )
        by_id = {u.id: u for u in q.all()}
        is_recall_mode = is_recall_like_study_mode(mode)
        out: list[LearningUnit] = []
        for uid in priority_unit_ids:
            unit = by_id.get(uid)
            if not unit:
                continue
            progress = unit.progress
            if is_recall_mode:
                if not progress or progress.introduced_at is None:
                    continue
            if progress and getattr(progress, "is_blocked", False):
                continue
            out.append(unit)
        return out

    def _compute_difficulty_score(self, progress: LearningProgress, now: datetime) -> float:
        """
        Compute an internal difficulty score for future due-unit prioritization.

        This helper blends effective confidence decay, recall fail streak, and
        stability maturity into a single scalar without changing current selection.
        """
        effective_confidence = compute_effective_confidence(
            confidence_score=progress.confidence_score,
            last_seen=progress.last_seen,
            now=now,
        )

        score = (
            (1 - effective_confidence) * 0.6
            + (progress.recall_fail_streak * 0.1)
            + (1 - progress.stability_score) * 0.3
        )
        return score

    def _compute_effective_confidence_from_progress(
        self,
        progress: LearningProgress,
        now: Optional[datetime] = None,
    ) -> float:
        return compute_effective_confidence(
            confidence_score=progress.confidence_score,
            last_seen=progress.last_seen,
            now=now or _utc_now_naive(),
        )

    def _compute_reinforcement_depth(
        self,
        progress,
        session_gain_last_5: float,
        *,
        now: Optional[datetime] = None,
        depth_bias: int = 0,
    ) -> int:
        effective_confidence = self._compute_effective_confidence_from_progress(progress, now=now)
        effective_stability = self._effective_stability(progress.stability_score)

        weakness_score = (
            (1 - effective_confidence) * 0.5
            + (1 - effective_stability) * 0.3
            + min(0.2, progress.recall_fail_streak * 0.05)
        )

        if session_gain_last_5 < getattr(settings, "srs_low_gain_threshold", 0.03):
            weakness_score *= 1.1

        if weakness_score > 0.7:
            depth = 3
        elif weakness_score > 0.4:
            depth = 2
        else:
            depth = 1

        return max(1, min(3, depth + depth_bias))

    def _controller_mode_is_observe_only(self) -> bool:
        return self._recall_controller_mode == "observe_only"

    def _controller_mode_is_experimental(self) -> bool:
        return self._recall_controller_mode == "v3_experimental"

    def _compute_controller_reinforcement_depth_bias(
        self,
        *,
        weak_ratio: float,
        due_ratio: float,
        session_difficulty_signal: float,
    ) -> int:
        if session_difficulty_signal >= 0.7 and weak_ratio >= (due_ratio - 0.15):
            return 1
        if session_difficulty_signal <= 0.35 and due_ratio >= (weak_ratio + 0.2):
            return -1
        return 0

    def _snapshot_units_from_pool(self, request: SelectionRequest, pool) -> tuple[list[LearningUnit], list[LearningUnit]]:
        if request.pool_kind == "due":
            due_units = pool if isinstance(pool, list) else []
            return due_units, []
        if request.pool_kind == "weak":
            return [], [unit for unit, _ in pool]
        return (
            [unit for unit, _ in pool["due_units"]],
            [unit for unit, _ in pool["weak_units"]],
        )

    def _record_recall_controller_snapshot(
        self,
        request: SelectionRequest,
        pool,
        *,
        selected_units: Optional[list[LearningUnit]] = None,
    ) -> RecallControllerSnapshot:
        due_units, weak_units = self._snapshot_units_from_pool(request, pool)
        ratio_denominator = max(1, len(due_units) + len(weak_units))
        due_ratio = len(due_units) / ratio_denominator
        weak_ratio = len(weak_units) / ratio_denominator

        difficulty_population = [
            unit for unit in (selected_units or (due_units + weak_units)) if unit.progress
        ]
        if difficulty_population:
            difficulty_signal = sum(
                self._compute_difficulty_score(unit.progress, request.now)
                for unit in difficulty_population
            ) / len(difficulty_population)
        else:
            difficulty_signal = 0.0

        if self._gain_history:
            session_gain_last_5 = sum(self._gain_history) / len(self._gain_history)
        else:
            session_gain_last_5 = 0.0

        recall_depth_population = [
            unit for unit in (selected_units or weak_units or due_units) if unit.progress
        ]
        if recall_depth_population:
            recall_depth = sum(
                self._compute_reinforcement_depth(
                    unit.progress,
                    session_gain_last_5,
                    now=request.now,
                )
                for unit in recall_depth_population
            ) / len(recall_depth_population)
        else:
            recall_depth = 1.0

        bias = self._compute_controller_reinforcement_depth_bias(
            weak_ratio=weak_ratio,
            due_ratio=due_ratio,
            session_difficulty_signal=difficulty_signal,
        )
        snapshot = RecallControllerSnapshot(
            mode=self._recall_controller_mode,
            recall_depth=round(recall_depth, 4),
            weak_ratio=round(weak_ratio, 4),
            due_ratio=round(due_ratio, 4),
            reinforcement_depth_bias=bias,
            session_difficulty_signal=round(difficulty_signal, 4),
        )
        self._last_recall_controller_snapshot = snapshot
        logger.info(
            "Recall controller mode=%s recall_depth=%.4f weak_ratio=%.4f due_ratio=%.4f reinforcement_depth_bias=%d session_difficulty_signal=%.4f",
            snapshot.mode,
            snapshot.recall_depth,
            snapshot.weak_ratio,
            snapshot.due_ratio,
            snapshot.reinforcement_depth_bias,
            snapshot.session_difficulty_signal,
        )
        return snapshot

    def _compute_gain_adjustment(self, session_gain_last_5: float):
        target = getattr(settings, "srs_target_gain_per_session", self._target_gain_per_answer)
        tolerance = 0.05

        delta = session_gain_last_5 - target

        reinforcement_bonus = 0
        difficulty_adjustment = 0.0

        if delta < -tolerance:
            reinforcement_bonus = 1
            difficulty_adjustment = -0.05
        elif delta > tolerance:
            reinforcement_bonus = -1
            difficulty_adjustment = 0.05

        return reinforcement_bonus, difficulty_adjustment

    def _effective_stability(self, stability_score: float) -> float:
        from math import exp
        k = 3.0
        return 1 - exp(-k * stability_score)

    def _classify_error(self, user_input: str, correct_text: str, allowed_vocab: set[str]) -> str:
        normalized_input = normalize_input(user_input, strip_punctuation=True).lower()
        normalized_correct = normalize_input(correct_text, strip_punctuation=True).lower()

        if _levenshtein_distance(normalized_input, normalized_correct) <= 1:
            return "near_miss"
        elif normalized_input in allowed_vocab:
            return "semantic_confusion"
        else:
            return "unknown"

    def _interleave_units(self, buckets: dict[str, list]) -> list:
        pattern = ["critical", "weak", "critical", "stable", "weak", "mature"]
        non_empty_buckets = [name for name, items in buckets.items() if items]
        if len(non_empty_buckets) <= 1:
            for items in buckets.values():
                if items:
                    return list(items)
            return []

        result = []
        while any(buckets.values()):
            for bucket_name in pattern:
                if buckets.get(bucket_name):
                    result.append(buckets[bucket_name].pop(0))
        return result

    def _build_selection_request(
        self,
        *,
        mode: Literal["due_only", "weak_only", "normal"],
        study_mode: StudyModeType,
        session_size: int,
        pool_kind: Literal["due", "weak", "normal"],
        apply_balancing: bool = False,
        apply_reinforcement_only: bool = False,
        use_due_first_split: bool = False,
        use_direct_query_limit: bool = False,
        weak_only_padding: bool = False,
        source_pdfs: Optional[list[str]] = None,
        theme: Optional[str] = None,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: Optional[set[int]] = None,
        now: Optional[datetime] = None,
        due_units_query=None,
        available_due_count: Optional[int] = None,
        exclude_unit_ids: frozenset[int] = frozenset(),
        balanced_never_seen_mix: bool = False,
        new_words_focus: bool = False,
    ) -> SelectionRequest:
        request = SelectionRequest(
            mode=mode,
            study_mode=study_mode,
            session_size=session_size,
            pool_kind=pool_kind,
            apply_balancing=apply_balancing,
            apply_reinforcement_only=apply_reinforcement_only,
            use_due_first_split=use_due_first_split,
            use_direct_query_limit=use_direct_query_limit,
            weak_only_padding=weak_only_padding,
            source_pdfs=source_pdfs,
            theme=theme,
            theme_vocab_ids=theme_vocab_ids,
            lesson_vocab_ids=lesson_vocab_ids,
            now=now,
            due_units_query=due_units_query,
            available_due_count=available_due_count,
            exclude_unit_ids=exclude_unit_ids,
            balanced_never_seen_mix=balanced_never_seen_mix,
            new_words_focus=new_words_focus,
        )
        logger.info(
            "_build_selection_request trace: new_words_focus=%s source_pdfs=%s "
            "lesson_vocab_ids=%s theme_vocab_ids=%s session_size=%s",
            request.new_words_focus,
            request.source_pdfs,
            request.lesson_vocab_ids,
            request.theme_vocab_ids,
            request.session_size,
        )
        return request

    def apply_selection_filters(
        self,
        query,
        *,
        source_pdfs: Optional[list[str]] = None,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: Optional[set[int]] = None,
        weak_only: bool = False,
        due_only: bool = False,
        now: Optional[datetime] = None,
    ):
        """Apply selection filters in the canonical order: theme, lesson/source, weak, due."""
        if theme_vocab_ids:
            query = query.filter(LearningUnit.vocabulary_id.in_(theme_vocab_ids))

        if lesson_vocab_ids is not None:
            query = query.filter(LearningUnit.vocabulary_id.in_(lesson_vocab_ids))
        if source_pdfs:
            query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))

        if weak_only:
            query = query.filter(LearningProgress.confidence_score < WEAK_THRESHOLD)

        if due_only:
            effective_now = now or _utc_now_naive()
            query = query.filter(
                LearningProgress.introduced_at.isnot(None),
                LearningProgress.next_review_at <= effective_now,
            )

        return query

    def _get_due_pool(self, request: SelectionRequest):
        if request.due_units_query is not None:
            return request.due_units_query

        return self._get_due_units_weighted(
            request.source_pdfs,
            request.now,
            request.study_mode,
            theme_vocab_ids=request.theme_vocab_ids,
            lesson_vocab_ids=request.lesson_vocab_ids,
        )

    def _get_weak_pool(self, request: SelectionRequest):
        weak_units = self._get_weak_units_weighted(
            request.source_pdfs,
            request.now,
            request.study_mode,
            strict=True,
            include_blocked=True,
            theme_vocab_ids=request.theme_vocab_ids,
            lesson_vocab_ids=request.lesson_vocab_ids,
        )
        if request.weak_only_padding and len(weak_units) < request.session_size:
            logger.warning(f"Only {len(weak_units)} weak units available, padding with review units")
            review_units = self._get_review_units_weighted(
                request.source_pdfs,
                request.now,
                request.study_mode,
                theme_vocab_ids=request.theme_vocab_ids,
                lesson_vocab_ids=request.lesson_vocab_ids,
            )
            all_units = weak_units + review_units

            if len(all_units) < request.session_size:
                logger.warning(
                    f"Only {len(all_units)} weak+review units available, padding with new units"
                )
                new_units = self._get_new_units_weighted(
                    request.source_pdfs,
                    request.now,
                    request.study_mode,
                    theme_vocab_ids=request.theme_vocab_ids,
                    lesson_vocab_ids=request.lesson_vocab_ids,
                )
                all_units = all_units + new_units
        else:
            all_units = weak_units

        return all_units

    def _get_normal_pool(self, request: SelectionRequest):
        due_units = self._get_due_units_weighted(
            request.source_pdfs,
            request.now,
            request.study_mode,
            theme_vocab_ids=request.theme_vocab_ids,
            lesson_vocab_ids=request.lesson_vocab_ids,
        )

        return {
            "due_units": due_units,
            "new_units": self._get_new_units_weighted(
                request.source_pdfs,
                request.now,
                request.study_mode,
                theme_vocab_ids=request.theme_vocab_ids,
                lesson_vocab_ids=request.lesson_vocab_ids,
            ),
            "weak_units": self._get_weak_units_weighted(
                request.source_pdfs,
                request.now,
                request.study_mode,
                theme_vocab_ids=request.theme_vocab_ids,
                lesson_vocab_ids=request.lesson_vocab_ids,
            ),
            "review_units": self._get_review_units_weighted(
                request.source_pdfs,
                request.now,
                request.study_mode,
                theme_vocab_ids=request.theme_vocab_ids,
                lesson_vocab_ids=request.lesson_vocab_ids,
            ),
        }

    def _get_pool(self, request: SelectionRequest):
        if request.pool_kind == "due":
            return self._get_due_pool(request)
        if request.pool_kind == "weak":
            return self._get_weak_pool(request)
        return self._get_normal_pool(request)

    def _get_never_seen_weighted(self, request: SelectionRequest) -> list[tuple[LearningUnit, float]]:
        """Never-seen units only: no LearningProgress row (not the passive `new` bucket with introduced_at NULL)."""
        query = (
            self.db.query(LearningUnit)
            .outerjoin(LearningProgress, LearningUnit.id == LearningProgress.unit_id)
            .filter(LearningProgress.id.is_(None))
        )
        now = request.now or _utc_now_naive()
        query = self.apply_selection_filters(
            query,
            source_pdfs=request.source_pdfs,
            theme_vocab_ids=request.theme_vocab_ids,
            lesson_vocab_ids=request.lesson_vocab_ids,
        )
        units = query.order_by(LearningUnit.id).all()
        return [(u, self._compute_unit_weight(u, "new", now)) for u in units]

    def _pick_never_seen_balanced(
        self,
        request: SelectionRequest,
        count: int,
        selected_ids: set[int],
    ) -> list[LearningUnit]:
        """
        60/40 pl_ua vs czytaj among never-seen units; overflow from pl/cz tails first.
        `other` sources only when both main pools are insufficient for their quotas and after tails.
        """
        if count <= 0:
            return []

        weighted = self._get_never_seen_weighted(request)
        pl: list[tuple[LearningUnit, float]] = []
        cz: list[tuple[LearningUnit, float]] = []
        other: list[tuple[LearningUnit, float]] = []
        for pair in weighted:
            u = pair[0]
            if u.id in selected_ids:
                continue
            tier = classify_vocab_source_pdf(u.source_pdf)
            if tier == "pl_ua":
                pl.append(pair)
            elif tier == "czytaj":
                cz.append(pair)
            else:
                other.append(pair)

        n_pl = round(count * 0.6)
        n_cz = count - n_pl
        both_insufficient = len(pl) < n_pl and len(cz) < n_cz

        out: list[LearningUnit] = []

        for pair in pl[:n_pl]:
            u = pair[0]
            if u.id not in selected_ids:
                out.append(u)
                selected_ids.add(u.id)

        for pair in cz[:n_cz]:
            u = pair[0]
            if u.id not in selected_ids:
                out.append(u)
                selected_ids.add(u.id)

        remaining = count - len(out)
        if remaining > 0:
            overflow = pl[n_pl:] + cz[n_cz:]
            for pair in overflow:
                if remaining <= 0:
                    break
                u = pair[0]
                if u.id in selected_ids:
                    continue
                out.append(u)
                selected_ids.add(u.id)
                remaining -= 1

        remaining = count - len(out)
        if remaining > 0 and both_insufficient:
            for pair in other:
                if remaining <= 0:
                    break
                u = pair[0]
                if u.id in selected_ids:
                    continue
                out.append(u)
                selected_ids.add(u.id)
                remaining -= 1

        return out

    def _new_words_focus_debug_unit_mix(
        self,
        units: list[LearningUnit],
        context: str,
        *,
        error_if_introduced: bool,
    ) -> None:
        """
        Diagnostic logging for tracing introduced units in passive new_words_focus sessions.
        Does not alter selection; remove or downgrade once the leak path is identified.
        """
        if not units:
            logger.info("new_words_debug [%s] empty unit list", context)
            return
        ids = [u.id for u in units]
        rows = (
            self.db.query(LearningProgress)
            .filter(LearningProgress.unit_id.in_(ids))
            .all()
        )
        by_uid = {r.unit_id: r for r in rows}
        no_row = 0
        intro_null = 0
        intro_set = 0
        for u in units:
            prog = by_uid.get(u.id)
            if prog is None:
                no_row += 1
            elif prog.introduced_at is None:
                intro_null += 1
            else:
                intro_set += 1
                if error_if_introduced:
                    logger.error(
                        "new_words_debug [%s] introduced unit in new_words_focus path: "
                        "unit_id=%s text=%r introduced_at=%s times_seen=%s",
                        context,
                        u.id,
                        u.text,
                        prog.introduced_at,
                        prog.times_seen,
                    )
        logger.info(
            "new_words_debug [%s] total=%s no_progress_row=%s introduced_at_null=%s introduced_at_set=%s",
            context,
            len(units),
            no_row,
            intro_null,
            intro_set,
        )

    def _sample_base_selection(self, request: SelectionRequest, pool) -> list[LearningUnit]:
        selected: list[LearningUnit] = []
        selected_ids: set[int] = set(request.exclude_unit_ids)

        if request.pool_kind == "due":
            if request.due_units_query is not None:
                if request.use_direct_query_limit:
                    return pool.limit(request.session_size).all()
                return []

            if request.use_direct_query_limit:
                return pool.limit(request.session_size).all()

            num_due = len(pool)
            if num_due == 0:
                raise InsufficientUnitsError("No words are due for review right now.")

            selected = self._weighted_random_sample(pool, num_due, selected_ids)
            logger.info(f"Due-only session: selected {len(selected)} units from {num_due} due units")
            return selected

        if request.pool_kind == "weak":
            if len(pool) == 0:
                pdf_info = f" from selected files" if request.source_pdfs else ""
                raise InsufficientUnitsError(
                    f"No difficult words for practice (confidence below 50%{pdf_info}) in this lesson scope. "
                    f"Try normal study or widen your vocabulary selection."
                )
            take = min(request.session_size, len(pool))
            if take < request.session_size:
                logger.info(
                    "Weak-only: %s strict-weak units in lesson scope (session cap %s); "
                    "sampling %s — reinforcement may repeat cards to fill the session.",
                    len(pool),
                    request.session_size,
                    take,
                )
            return self._weighted_random_sample(pool, take, selected_ids)

        max_due_items = int(request.session_size * DUE_ITEMS_MAX_PERCENT)
        due_units = pool["due_units"]

        if request.use_due_first_split and due_units:
            due_selected = self._weighted_random_sample(due_units, max_due_items, selected_ids)
            selected.extend(due_selected)
            logger.debug(f"Selected {len(due_selected)} due items for session")

        remaining_slots = request.session_size - len(selected)

        if remaining_slots > 0:
            if (
                request.new_words_focus
                and request.study_mode == StudyModeType.PASSIVE
            ):
                # Home "new words": passive new bucket only; refill must not pull weak/review.
                # Still run readiness gate so session_reason is surfaced in the UI.
                from app.services.daily_stats import get_daily_dashboard_stats
                _ds_nwf = get_daily_dashboard_stats(self.db)
                _, _nwf_gate = _compute_new_words_readiness(
                    words_introduced_today=_ds_nwf.get("words_introduced_today", 0),
                    max_new_per_day=_ds_nwf.get("max_new_per_day", 60),
                    accuracy_today=_ds_nwf.get("recall_accuracy_today"),
                    weak_count=_ds_nwf.get("weak_words_count", 0),
                )
                if _nwf_gate:
                    self._last_readiness_gate = _nwf_gate
                target_new = remaining_slots
                target_weak = 0
                target_review = 0
            else:
                # Adaptive readiness gate: may suppress new words and shift budget to weak/review
                from app.services.daily_stats import get_daily_dashboard_stats
                _ds = get_daily_dashboard_stats(self.db)
                new_pct, _gate_reason = _compute_new_words_readiness(
                    words_introduced_today=_ds.get("words_introduced_today", 0),
                    max_new_per_day=_ds.get("max_new_per_day", 60),
                    accuracy_today=_ds.get("recall_accuracy_today"),
                    weak_count=_ds.get("weak_words_count", 0),
                )
                if _gate_reason:
                    self._last_readiness_gate = _gate_reason
                freed = BUCKET_NEW_PERCENT - new_pct  # 0.0 or 0.30
                # Redistribute freed budget: +20% weak, +10% review (preserves due dominance)
                effective_weak_pct = BUCKET_WEAK_PERCENT + freed * (2 / 3)
                effective_review_pct = BUCKET_REVIEW_PERCENT + freed * (1 / 3)
                target_new = int(remaining_slots * new_pct)
                target_weak = int(remaining_slots * effective_weak_pct)
                target_review = remaining_slots - target_new - target_weak

            if (
                request.balanced_never_seen_mix
                and request.study_mode == StudyModeType.PASSIVE
                and target_new > 0
            ):
                balanced_picks = self._pick_never_seen_balanced(
                    request, target_new, selected_ids
                )
                selected.extend(balanced_picks)
                shortfall = target_new - len(balanced_picks)
                if shortfall > 0:
                    fill = self._weighted_random_sample(
                        pool["new_units"], shortfall, selected_ids
                    )
                    selected.extend(fill)
            else:
                new_selected = self._weighted_random_sample(
                    pool["new_units"], target_new, selected_ids
                )
                selected.extend(new_selected)

            weak_selected = self._weighted_random_sample(pool["weak_units"], target_weak, selected_ids)
            selected.extend(weak_selected)

            review_selected = self._weighted_random_sample(
                pool["review_units"], target_review, selected_ids
            )
            selected.extend(review_selected)

            remaining_needed = request.session_size - len(selected)
            if remaining_needed > 0:
                strict_new_refill = (
                    request.new_words_focus
                    and request.study_mode == StudyModeType.PASSIVE
                )
                if strict_new_refill:
                    all_remaining = [
                        (u, w)
                        for u, w in pool["new_units"]
                        if u.id not in selected_ids
                    ]
                else:
                    all_remaining = (
                        [(u, w) for u, w in pool["new_units"] if u.id not in selected_ids]
                        + [(u, w) for u, w in pool["weak_units"] if u.id not in selected_ids]
                        + [(u, w) for u, w in pool["review_units"] if u.id not in selected_ids]
                    )
                fill_selected = self._weighted_random_sample(
                    all_remaining, remaining_needed, selected_ids
                )
                selected.extend(fill_selected)
                if strict_new_refill and len(fill_selected) < remaining_needed:
                    logger.info(
                        "new_words_focus: new pool exhausted; session has %s/%s units (no weak/review pad)",
                        len(selected),
                        request.session_size,
                    )

        if request.new_words_focus and request.study_mode == StudyModeType.PASSIVE:
            chat_vocabulary_id = (
                self.db.query(Vocabulary.id)
                .filter(
                    Vocabulary.name == "Chat Vocabulary",
                    Vocabulary.user_key == "local",
                )
                .scalar()
            )

            if chat_vocabulary_id is not None:
                if isinstance(pool, dict) and pool.get("new_units"):
                    chat_units = [
                        unit for (unit, _) in pool["new_units"]
                        if getattr(unit, "vocabulary_id", None) == chat_vocabulary_id
                    ]

                    selected_unit_ids = {u.id for u in selected}
                    missing_chat = [u for u in chat_units if u.id not in selected_unit_ids]

                    injection_count = min(3, len(missing_chat))

                    replaced = 0
                    i = len(selected) - 1
                    while replaced < injection_count and i >= 0:
                        candidate = selected[i]

                        if getattr(candidate, "vocabulary_id", None) != chat_vocabulary_id:
                            selected[i] = missing_chat[replaced]
                            replaced += 1

                        i -= 1

                    final_chat_count = sum(
                        1
                        for unit in selected
                        if getattr(unit, "vocabulary_id", None) == chat_vocabulary_id
                    )
                    logger.info(
                        "Chat injection: inserted=%s final_chat_count=%s",
                        replaced,
                        final_chat_count,
                    )

        out = selected[: request.session_size]
        if request.new_words_focus and request.study_mode == StudyModeType.PASSIVE:
            logger.info(
                "new_words_debug [_sample_base_selection] new_words_focus=%s study_mode=%s "
                "balanced_never_seen_mix=%s len(new_units_pool)=%s session_size=%s "
                "selected_count=%s exclude_unit_ids=%s "
                "(follow_up_session_id is not on SelectionRequest; prefix is applied in create_session)",
                request.new_words_focus,
                request.study_mode,
                request.balanced_never_seen_mix,
                len(pool["new_units"]),
                request.session_size,
                len(out),
                len(request.exclude_unit_ids),
            )
            self._new_words_focus_debug_unit_mix(
                out,
                "_sample_base_selection (normal-pool tail batch)",
                error_if_introduced=True,
            )
        return out

    def _apply_balancing_if_needed(
        self,
        request: SelectionRequest,
        pool,
        selected_units: list[LearningUnit],
    ) -> list[LearningUnit]:
        if request.apply_balancing and request.due_units_query is not None:
            if request.available_due_count is None or request.available_due_count <= request.session_size:
                self._record_recall_controller_snapshot(request, selected_units, selected_units=selected_units)
                return selected_units

            due_units = pool.all()
            snapshot = self._record_recall_controller_snapshot(request, due_units, selected_units=selected_units)
            controller_bias = (
                snapshot.reinforcement_depth_bias if self._controller_mode_is_experimental() else 0
            )
            selected_units = self._select_balanced_units(
                due_units=due_units,
                session_size=request.session_size,
                now=request.now,
                controller_reinforcement_depth_bias=controller_bias,
            )
        elif request.apply_reinforcement_only:
            if self._gain_history:
                session_gain_last_5 = sum(self._gain_history) / len(self._gain_history)
            else:
                session_gain_last_5 = 0.0
            snapshot = self._record_recall_controller_snapshot(request, pool, selected_units=selected_units)
            controller_bias = (
                snapshot.reinforcement_depth_bias if self._controller_mode_is_experimental() else 0
            )

            reinforced_units: list[LearningUnit] = []
            for unit in selected_units:
                reinforced_units.append(unit)
                if not unit.progress:
                    continue
                depth = self._compute_reinforcement_depth(
                    unit.progress,
                    session_gain_last_5,
                    now=request.now,
                    depth_bias=controller_bias,
                )
                additional_inserts = max(0, depth - 1)
                for _ in range(additional_inserts):
                    reinforced_units.append(unit)
            capped_units = self._cap_duplicate_occurrences(reinforced_units)
            if (
                request.mode == "weak_only"
                and len(capped_units) < request.session_size
                and selected_units
            ):
                unique_weak: list[LearningUnit] = []
                seen_w: set[int] = set()
                for u in selected_units:
                    if u.id not in seen_w:
                        seen_w.add(u.id)
                        unique_weak.append(u)
                capped_units = self._weak_fill_to_session_size(
                    unique_weak, request.session_size
                )
            return capped_units[:request.session_size]
        else:
            self._record_recall_controller_snapshot(request, pool, selected_units=selected_units)
            return selected_units

        return self._cap_duplicate_occurrences(selected_units)

    def _cap_duplicate_occurrences(self, selected_units: list[LearningUnit]) -> list[LearningUnit]:
        duplicate_counts: dict[int, int] = {}
        capped_units: list[LearningUnit] = []
        for unit in selected_units:
            seen = duplicate_counts.get(unit.id, 0)
            if seen >= MAX_DUPLICATES_PER_UNIT:
                continue
            duplicate_counts[unit.id] = seen + 1
            capped_units.append(unit)
        return capped_units

    def _weak_fill_to_session_size(
        self,
        unique_weak: list[LearningUnit],
        session_size: int,
    ) -> list[LearningUnit]:
        """Round-robin weak units with a per-unit cap high enough to reach session_size."""
        n = len(unique_weak)
        if n == 0 or session_size <= 0:
            return []
        max_per = max(MAX_DUPLICATES_PER_UNIT, (session_size + n - 1) // n)
        counts: dict[int, int] = {}
        out: list[LearningUnit] = []
        idx = 0
        for _ in range(session_size * n * 4):
            if len(out) >= session_size:
                break
            u = unique_weak[idx % n]
            idx += 1
            if counts.get(u.id, 0) >= max_per:
                continue
            out.append(u)
            counts[u.id] = counts.get(u.id, 0) + 1
        return out

    def _log_selection_pool_composition(self, request: SelectionRequest, pool) -> None:
        """Diagnostic: pool sizes and source skew (does not alter selection)."""
        pk = request.pool_kind
        if pk == "normal" and isinstance(pool, dict):
            due = pool.get("due_units") or []
            weak = pool.get("weak_units") or []
            rev = pool.get("review_units") or []
            new = pool.get("new_units") or []
            nd, nw, nr, nn = len(due), len(weak), len(rev), len(new)
            seen: set[int] = set()
            union_units: list[LearningUnit] = []
            for pairs in (due, weak, rev, new):
                for u, _ in pairs:
                    if u.id not in seen:
                        seen.add(u.id)
                        union_units.append(u)
            logger.info(
                "selection pool: pool_kind=normal bucket_entries=%d due=%d weak=%d review=%d new=%d "
                "union_unique=%d top_sources=%s",
                nd + nw + nr + nn,
                nd,
                nw,
                nr,
                nn,
                len(union_units),
                _selection_source_top_n(union_units),
            )
        elif pk == "due":
            if request.due_units_query is not None:
                try:
                    q_count = int(pool.count())
                except Exception:
                    q_count = -1
                logger.info(
                    "selection pool: pool_kind=due query_mode=True due_query_count=%d "
                    "(no per-bucket breakdown)",
                    q_count,
                )
            else:
                pairs = pool if isinstance(pool, list) else []
                units = [u for u, _ in pairs]
                logger.info(
                    "selection pool: pool_kind=due bucket_entries=%d due=%d weak=0 review=0 new=0 "
                    "union_unique=%d top_sources=%s",
                    len(pairs),
                    len(pairs),
                    len({u.id for u in units}),
                    _selection_source_top_n(units),
                )
        elif pk == "weak":
            pairs = pool if isinstance(pool, list) else []
            units = [u for u, _ in pairs]
            logger.info(
                "selection pool: pool_kind=weak bucket_entries=%d union_unique=%d top_sources=%s "
                "(merged weak path; not split into due/weak/review/new)",
                len(pairs),
                len({u.id for u in units}),
                _selection_source_top_n(units),
            )

    def _log_selection_final(self, request: SelectionRequest, selected_units: list[LearningUnit]) -> None:
        """Diagnostic: outcome of pipeline after balancing (does not alter selection)."""
        if not selected_units:
            logger.info("selected: total=0 unique_units=0 sources=0 pool_kind=%s", request.pool_kind)
            return
        uid_set = {u.id for u in selected_units}
        if len(selected_units) != len(uid_set):
            if request.apply_reinforcement_only:
                # Intentional: reinforcement depth inserts the same unit multiple times.
                logger.debug(
                    "reinforcement duplicates: total=%d unique=%d extra=%d (expected)",
                    len(selected_units),
                    len(uid_set),
                    len(selected_units) - len(uid_set),
                )
            else:
                logger.warning(
                    "DUPLICATE UNITS DETECTED IN SELECTION — unexpected for pool_kind=%s "
                    "(total=%d unique=%d extra=%d). Check pool overlap.",
                    request.pool_kind,
                    len(selected_units),
                    len(uid_set),
                    len(selected_units) - len(uid_set),
                )
        src_set = {getattr(u, "source_pdf", None) or "" for u in selected_units}
        cmin, cavg, cmax = _selection_confidence_min_avg_max(selected_units)
        logger.info(
            "selected: total=%d unique_units=%d sources=%d top_sources=%s "
            "confidence[min=%.2f avg=%.2f max=%.2f] pool_kind=%s",
            len(selected_units),
            len(uid_set),
            len(src_set),
            _selection_source_top_n(selected_units),
            cmin,
            cavg,
            cmax,
            request.pool_kind,
        )

    def _execute_selection_pipeline(self, request: SelectionRequest) -> list[LearningUnit]:
        pool = self._get_pool(request)
        self._log_selection_pool_composition(request, pool)
        selected_units = self._sample_base_selection(request, pool)
        selected_units = self._apply_balancing_if_needed(request, pool, selected_units)
        self._log_selection_final(request, selected_units)
        return selected_units

    def _infer_selection_reason(
        self,
        unit: LearningUnit,
        progress: Optional[LearningProgress],
        now: Optional[datetime] = None,
    ) -> str:
        """Classify why a unit was selected when cache and SessionUnit.selection_reason are absent."""
        effective_now = now or _utc_now_naive()
        if progress:
            nra = progress.next_review_at
            if nra is not None:
                nra = _naive_utc(nra) if isinstance(nra, datetime) else nra
                if nra <= effective_now:
                    return "due"
            if progress.confidence_score < WEAK_THRESHOLD:
                return "weak"
            return "review"
        return "new"

    def _resolve_passive_recall_chain(
        self, session: LearningSession
    ) -> Optional[Literal["weak", "lesson"]]:
        """Prefer LearningSession.passive_recall_chain; fall back to process cache (two-phase)."""
        val = getattr(session, "passive_recall_chain", None)
        if val in ("weak", "lesson"):
            return val
        return _SESSION_PASSIVE_RECALL_AFTER.get(session.id)

    def _build_cloze_session_unit_fields(
        self,
        selected_units: list[LearningUnit],
    ) -> list[dict[str, Any]]:
        """Per-slot cloze metadata; falls back to visual recall when generation or cloze build fails."""
        from app.services.cloze_service import get_or_generate_sentence, make_cloze_prompt

        rows: list[dict[str, Any]] = []
        recall_fallback = {
            "exercise_type": "recall",
            "cloze_prompt": None,
            "context_sentence_translation": None,
        }
        for unit in selected_units:
            fresh = (
                self.db.query(LearningUnit)
                .filter(LearningUnit.id == unit.id)
                .first()
            )
            if not fresh:
                rows.append(dict(recall_fallback))
                continue
            sentence = get_or_generate_sentence(fresh, self.db, ai_service=None)
            if not sentence:
                rows.append(dict(recall_fallback))
                continue
            try:
                prompt = make_cloze_prompt(sentence, fresh.text)
            except ValueError:
                rows.append(dict(recall_fallback))
                continue
            rows.append(
                {
                    "exercise_type": "cloze",
                    "cloze_prompt": prompt,
                    "context_sentence_translation": fresh.context_sentence_translation,
                }
            )
        return rows

    def _persist_session(
        self,
        *,
        mode: StudyModeType,
        theme: Optional[str],
        selected_units: list[LearningUnit],
        due_only: bool = False,
        selection_reasons: Optional[dict[int, str]] = None,
        passive_recall_after_completion: Optional[Literal["weak", "lesson"]] = None,
    ) -> LearningSession:
        cloze_fields: Optional[list[dict[str, Any]]] = None
        if mode == StudyModeType.CLOZE:
            cloze_fields = self._build_cloze_session_unit_fields(selected_units)

        session = LearningSession(
            mode=mode,
            status=SessionLifecycleStatus.CREATED,
            locked=True,
            completed=False,
            due_only=due_only,
            passive_recall_chain=passive_recall_after_completion,
        )
        session.theme = theme
        self.db.add(session)
        self.db.flush()  # Get session ID

        reasons_map = selection_reasons or {}
        for position, unit in enumerate(selected_units, start=1):
            extra: dict[str, Any] = {
                "exercise_type": "recall",
                "cloze_prompt": None,
                "context_sentence_translation": None,
            }
            if cloze_fields is not None and position - 1 < len(cloze_fields):
                extra = cloze_fields[position - 1]
            session_unit = SessionUnit(
                session_id=session.id,
                unit_id=unit.id,
                position=position,
                answered=False,
                selection_reason=reasons_map.get(unit.id),
                exercise_type=extra["exercise_type"],
                cloze_prompt=extra.get("cloze_prompt"),
                context_sentence_translation=extra.get("context_sentence_translation"),
            )
            self.db.add(session_unit)

        self.db.commit()
        self.db.refresh(session)

        _SESSION_SELECTION_REASONS[session.id] = dict(selection_reasons or {})
        if passive_recall_after_completion:
            _SESSION_PASSIVE_RECALL_AFTER[session.id] = passive_recall_after_completion
        if len(_SESSION_SELECTION_REASONS) > MAX_SELECTION_REASON_CACHE:
            drop_n = len(_SESSION_SELECTION_REASONS) - MAX_SELECTION_REASON_CACHE
            for k in list(_SESSION_SELECTION_REASONS.keys())[:drop_n]:
                _SESSION_SELECTION_REASONS.pop(k, None)
                _SESSION_PASSIVE_RECALL_AFTER.pop(k, None)

        if due_only:
            logger.info(
                f"Created {mode.value} due-only session {session.id} with {len(selected_units)} units"
            )
        else:
            logger.info(
                f"Created {mode.value} session {session.id} with {len(selected_units)} units"
            )

        # Carry readiness gate reason to router (transient, not persisted to DB)
        session._readiness_gate = getattr(self, "_last_readiness_gate", None)
        self._last_readiness_gate = None  # reset after use
        return session

    def _select_balanced_units(
        self,
        due_units,
        session_size: int,
        now: datetime,
        controller_reinforcement_depth_bias: int = 0,
    ):
        """Select due units using quartile-balanced difficulty sampling."""
        if len(due_units) <= session_size:
            return due_units

        if self._gain_history:
            session_gain_last_5 = sum(self._gain_history) / len(self._gain_history)
        else:
            session_gain_last_5 = 0.0
        reinforcement_bonus, difficulty_adjustment = self._compute_gain_adjustment(session_gain_last_5)
        effective_difficulty_bias = max(-0.2, min(0.2, self._difficulty_bias + difficulty_adjustment))

        scored_units: list[tuple[LearningUnit, float]] = []
        for unit in due_units:
            if not unit.progress:
                continue
            score = self._compute_difficulty_score(unit.progress, now)
            scored_units.append((unit, score))

        sorted_units = [
            unit for unit, _ in sorted(scored_units, key=lambda item: item[1], reverse=True)
        ]

        if len(sorted_units) <= session_size:
            return sorted_units

        quartiles = [
            sorted_units[i * len(sorted_units) // 4: (i + 1) * len(sorted_units) // 4]
            for i in range(4)
        ]

        base_critical = 0.25
        base_stable = 0.25

        critical_ratio = max(0.10, min(0.40, base_critical - effective_difficulty_bias))
        stable_ratio = max(0.15, min(0.40, base_stable + effective_difficulty_bias))
        weak_ratio = 0.35
        mature_ratio = 1.0 - (critical_ratio + weak_ratio + stable_ratio)

        critical = round(session_size * critical_ratio)
        weak = round(session_size * weak_ratio)
        stable = round(session_size * stable_ratio)
        mature = session_size - (critical + weak + stable)
        targets = [critical, weak, stable, mature]
        bucket_names = ["critical", "weak", "stable", "mature"]

        selected_buckets: dict[str, list[LearningUnit]] = {
            "critical": [],
            "weak": [],
            "stable": [],
            "mature": [],
        }
        carry = 0
        for idx in range(4):
            needed = targets[idx] + carry
            available = quartiles[idx]
            if needed <= 0 or not available:
                carry = max(0, needed)
                continue

            if len(available) <= needed:
                picked = list(available)
                quartiles[idx] = []
                carry = needed - len(picked)
            else:
                picked = self._rng.sample(available, needed)
                for item in picked:
                    available.remove(item)
                carry = 0

            selected_buckets[bucket_names[idx]].extend(picked)

        # Final safety fill from any remaining units.
        selected_count = sum(len(v) for v in selected_buckets.values())
        if selected_count < session_size:
            leftovers = []
            for idx, bucket in enumerate(quartiles):
                for unit in bucket:
                    leftovers.append((bucket_names[idx], unit))
            if leftovers:
                need = min(session_size - selected_count, len(leftovers))
                sampled = self._rng.sample(leftovers, need)
                for bucket_name, unit in sampled:
                    selected_buckets[bucket_name].append(unit)

        selected = self._interleave_units(selected_buckets)[:session_size]

        # Intra-session reinforcement for hard units:
        # repeat a limited number of difficult units later in the same session.
        if session_size < 6:
            return selected

        max_fraction = 0.3
        reinforcement_limit = int(len(selected) * max_fraction)
        if reinforcement_limit <= 0:
            return selected

        scored_selected: list[tuple[int, LearningUnit, float]] = []
        for idx, unit in enumerate(selected):
            if not unit.progress:
                continue
            score = self._compute_difficulty_score(unit.progress, now)
            if score >= 0.65:
                scored_selected.append((idx, unit, score))

        if not scored_selected:
            return selected

        # Deterministic tie-breaking with seeded RNG.
        self.random.shuffle(scored_selected)
        scored_selected.sort(key=lambda x: x[2], reverse=True)

        max_total = session_size + reinforcement_limit
        inserts_made = 0

        for _, candidate_unit, _ in scored_selected:
            if inserts_made >= reinforcement_limit:
                break
            if len(selected) >= max_total:
                break
            if not candidate_unit.progress:
                continue

            depth = self._compute_reinforcement_depth(
                candidate_unit.progress,
                session_gain_last_5,
                now=now,
                depth_bias=controller_reinforcement_depth_bias + reinforcement_bonus,
            )
            additional_inserts = max(0, depth - 1)

            # Locate candidate in current sequence (it may have shifted due to prior insertions).
            original_idx = next(
                (i for i, u in enumerate(selected) if u.id == candidate_unit.id),
                None,
            )
            if original_idx is None:
                continue

            for _ in range(additional_inserts):
                if inserts_made >= reinforcement_limit:
                    break
                if len(selected) >= max_total:
                    break

                min_insert_idx = original_idx + 5
                second_half_start = len(selected) // 2
                insert_start = max(min_insert_idx, second_half_start)

                if insert_start > len(selected):
                    break

                insert_idx = self.random.randint(insert_start, len(selected))
                selected.insert(insert_idx, candidate_unit)
                inserts_made += 1

        return selected
    
    def create_session(
        self,
        mode: StudyModeType = StudyModeType.PASSIVE,
        source_pdfs: Optional[list[str]] = None,
        theme: Optional[str] = None,
        lesson_id: Optional[int] = None,
        weak_only: bool = False,
        due_only: bool = False,
        override_daily_cap: bool = False,
        follow_up_session_id: Optional[int] = None,
        retry_failed_only: bool = False,
        new_words_focus: bool = False,
        curriculum_mode: Optional[str] = None,
    ) -> LearningSession | dict:
        """
        Create a new learning session with SESSION_SIZE units (except follow-up sessions
        with ``follow_up_session_id``, which use the priority prefix only—no tail fill;
        retry_failed_only still caps the prefix slice at
        min(SESSION_SIZE, failed_count + RETRY_SESSION_TAIL_PADDING)).
        
        Uses weighted random sampling with bucket composition:
        - 30% new (never seen)
        - 40% weak/failed
        - 30% review (known)
        
        Args:
            mode: Study mode type (passive or recall).
            source_pdfs: Optional list of PDF filenames to filter by.
            weak_only: If True, only include units with confidence <50%.
            due_only: If True, only include units with next_review_at <= now.
        
        Returns:
            Created LearningSession with SESSION_SIZE units.
            
        Raises:
            InsufficientUnitsError: If fewer than SESSION_SIZE units exist (or weak_only
                with fewer than SESSION_SIZE strict weak units).
        """
        if retry_failed_only and follow_up_session_id is None:
            raise ValueError("retry_failed_only requires follow_up_session_id")

        # Previous completed session unit IDs (for tail diversity). Skip for follow-ups / retries.
        self._last_session_unit_ids = frozenset()
        if not follow_up_session_id:
            last_session = (
                self.db.query(LearningSession)
                .filter(LearningSession.completed_at.isnot(None))
                .filter(LearningSession.status == SessionLifecycleStatus.COMPLETED)
                .order_by(LearningSession.completed_at.desc())
                .first()
            )
            if last_session:
                rows = (
                    self.db.query(SessionUnit.unit_id)
                    .filter(SessionUnit.session_id == last_session.id)
                    .all()
                )
                self._last_session_unit_ids = frozenset(r[0] for r in rows)

        follow_up_prior_session: Optional[LearningSession] = None
        if follow_up_session_id:
            logger.info(
                "Follow-up session requested: %s (retry_failed_only=%s)",
                follow_up_session_id,
                retry_failed_only,
            )
            follow_up_prior_session = (
                self.db.query(LearningSession)
                .filter(LearningSession.id == follow_up_session_id)
                .first()
            )

        # Relax source/theme for follow-ups where UI filters would empty the priority prefix
        # or fail the SESSION_SIZE preflight (retry after due-only, or passive→recall CTA).
        if follow_up_session_id and retry_failed_only:
            source_pdfs = None
            theme = None
            logger.info("Retry session: ignoring source/theme filters for session scope.")
        elif (
            follow_up_session_id
            and not retry_failed_only
            and follow_up_prior_session is not None
            and follow_up_prior_session.mode == StudyModeType.PASSIVE
        ):
            source_pdfs = None
            theme = None
            logger.info(
                "Passive follow-up recall: ignoring source/theme filters for session scope."
            )

        if (
            new_words_focus
            and mode == StudyModeType.PASSIVE
            and not weak_only
            and not due_only
            and source_pdfs is not None
        ):
            logger.info(
                "new_words_focus: ignoring source_pdfs filter for full new-word session (had %s)",
                source_pdfs,
            )
            source_pdfs = None

        theme_vocab_ids: Optional[set[int]] = None
        if theme:
            theme_obj = get_theme_by_id(theme)
            if not theme_obj:
                raise ValueError(f"Unknown theme: {theme}")
            theme_vocab_ids = set(theme_obj["vocabulary_ids"])
        lesson_vocab_ids: Optional[set[int]] = None

        follow_up_priority_ids: list[int] = []
        # due_only: skip follow-up prefix/retry ordering; never force recall via this path.
        if follow_up_session_id and not due_only:
            try:
                if retry_failed_only:
                    follow_up_priority_ids = self._get_failed_unit_ids_from_session(
                        follow_up_session_id
                    )
                else:
                    follow_up_priority_ids = self._get_ordered_unit_ids_from_session(
                        follow_up_session_id
                    )
            except Exception as e:
                logger.warning(
                    "Failed to load follow-up session %s: %s",
                    follow_up_session_id,
                    e,
                )
                follow_up_priority_ids = []

        if retry_failed_only and follow_up_prior_session is not None:
            src_session_total = (
                self.db.query(func.count(SessionUnit.id))
                .filter(SessionUnit.session_id == follow_up_prior_session.id)
                .scalar()
                or 0
            )
            logger.info(
                "RETRY SOURCE: session_id=%s failed_count=%d total=%d",
                follow_up_prior_session.id,
                len(follow_up_priority_ids),
                src_session_total,
            )

        # Global SRS due-only mode:
        # - ignore curriculum lesson filtering/spillover
        # - select oldest introduced due units across all vocabularies
        # - target SESSION_SIZE cards (min with how many are due in scope); daily cap does not shrink batch
        if due_only:
            ensure_overdue_spread(self.db)
            now = _utc_now_naive()
            due_units_query = (
                self.db.query(LearningUnit)
                .join(LearningProgress)
                .order_by(LearningProgress.next_review_at.asc())
            )
            due_units_query = self.apply_selection_filters(
                due_units_query,
                source_pdfs=source_pdfs,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=lesson_vocab_ids,
                weak_only=weak_only,
                due_only=True,
                now=now,
            )

            available_due_count = due_units_query.count()
            if available_due_count == 0:
                if theme:
                    raise NoDueUnitsInThemeError(theme)
                raise NoDueUnitsError("No words are due for review right now.")

            total_due_global = available_due_count
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # Only due-only sessions count toward the due-review daily cap (not mixed/recall).
            reviewed_today_due = (
                self.db.query(func.count(SessionUnit.id))
                .join(
                    LearningSession,
                    SessionUnit.session_id == LearningSession.id,
                )
                .filter(LearningSession.due_only.is_(True))
                .filter(SessionUnit.answered.is_(True))
                .filter(SessionUnit.answered_at >= start_of_day)
                .scalar()
                or 0
            )
            remaining_daily_quota = DAILY_REVIEW_CAP - reviewed_today_due

            if remaining_daily_quota <= 0 and not override_daily_cap:
                logger.info(
                    "create_session early return: reason=due_daily_cap_reached "
                    "reviewed_today=%s total_due=%s cap=%s",
                    reviewed_today_due,
                    total_due_global,
                    DAILY_REVIEW_CAP,
                )
                return {
                    "session_id": None,
                    "daily_cap_reached": True,
                    "total_due": total_due_global,
                    "reviewed_today": reviewed_today_due,
                    "remaining_due": total_due_global,
                    "message": (
                        f"Daily due-review limit reached for today ({DAILY_REVIEW_CAP} cards). "
                        f"Reviewed today: {reviewed_today_due}. "
                        f"Total due in scope: {total_due_global}."
                    ),
                }

            session_size = min(available_due_count, SESSION_SIZE)

            request = self._build_selection_request(
                mode="due_only",
                study_mode=mode,
                session_size=session_size,
                pool_kind="due",
                apply_balancing=True,
                use_direct_query_limit=available_due_count <= session_size,
                theme=theme,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=lesson_vocab_ids,
                now=now,
                due_units_query=due_units_query,
                available_due_count=available_due_count,
            )
            selected_units = self._execute_selection_pipeline(request)
            if len(selected_units) > session_size:
                selected_units = selected_units[:session_size]
            selection_reasons = {u.id: "due" for u in selected_units}
            session = self._persist_session(
                mode=mode,
                theme=theme,
                selected_units=selected_units,
                due_only=True,
                selection_reasons=selection_reasons,
            )
            n_units = len(session.units)
            if n_units < SESSION_SIZE:
                session._short_session_note = (
                    f"Only {n_units} word(s) are due for review with your current "
                    "vocabulary scope (theme / selected files). "
                    f"Switch to “All vocabulary” or add sources to reach ~{SESSION_SIZE} when more are due."
                )
            return session

        # Structured PL–UA lesson: isolated path — does not use czytaj filename lesson map.
        if curriculum_mode == "lesson" and not due_only:
            if mode not in (
                StudyModeType.PASSIVE,
                StudyModeType.RECALL,
                StudyModeType.RECALL_AUDIO,
                StudyModeType.CLOZE,
            ):
                raise ValueError(
                    "curriculum_mode=lesson is only supported in passive, recall, recall_audio, or cloze mode"
                )
            if follow_up_session_id:
                raise ValueError(
                    "curriculum_mode=lesson cannot be combined with follow_up_session_id"
                )
            if weak_only:
                raise ValueError("curriculum_mode=lesson cannot be combined with weak_only")

            from app.services.lesson_service import (
                build_primary_curriculum_map,
                detect_current_plua_lesson,
            )

            lesson_index = detect_current_plua_lesson(self.db)
            lesson_map = build_primary_curriculum_map(self.db)
            lesson_vocab_ids_scope = set(lesson_map.get(lesson_index, []))
            if not lesson_vocab_ids_scope:
                raise InsufficientUnitsError(
                    "No PL–UA vocabulary is configured for the current lesson."
                )

            ensure_overdue_spread(self.db)

            count_lesson = self.db.query(func.count(LearningUnit.id))
            count_lesson = self.apply_selection_filters(
                count_lesson,
                source_pdfs=None,
                theme_vocab_ids=None,
                lesson_vocab_ids=lesson_vocab_ids_scope,
            )
            lesson_available = count_lesson.scalar() or 0

            if lesson_available < SESSION_SIZE:
                raise InsufficientUnitsError(
                    f"Need at least {SESSION_SIZE} words in the current PL–UA lesson. "
                    f"Have {lesson_available}."
                )

            if mode == StudyModeType.PASSIVE:
                # Strict lesson mode: 100% current PL–UA lesson only (no Czytaj reinforcement).
                lesson_quota = SESSION_SIZE
                reinforce_quota = 0

                balanced_never_seen_mix = True
                use_due_first_split = True
                now_for_selection = _utc_now_naive()

                lesson_request = self._build_selection_request(
                    mode="normal",
                    study_mode=mode,
                    session_size=lesson_quota,
                    pool_kind="normal",
                    apply_reinforcement_only=False,
                    use_due_first_split=use_due_first_split,
                    weak_only_padding=False,
                    source_pdfs=None,
                    theme=None,
                    theme_vocab_ids=None,
                    lesson_vocab_ids=lesson_vocab_ids_scope,
                    now=now_for_selection,
                    exclude_unit_ids=frozenset(),
                    balanced_never_seen_mix=balanced_never_seen_mix,
                    new_words_focus=False,
                )
                lesson_units = self._execute_selection_pipeline(lesson_request)
                logger.info(
                    "lesson mode: lesson_index=%s candidates=%d selected=%d",
                    lesson_index,
                    lesson_available,
                    len(lesson_units),
                )

                selected_units = lesson_units
                self._rng.shuffle(selected_units)
                now_snap = _utc_now_naive()
                selection_reasons: dict[int, str] = {}
                for u in selected_units:
                    selection_reasons[u.id] = self._infer_selection_reason(
                        u, getattr(u, "progress", None), now_snap
                    )
                logger.info(
                    "Curriculum lesson session: lesson_index=%s plua_vocab_ids=%s "
                    "lesson_quota=%s reinforce_quota=%s units=%s",
                    lesson_index,
                    sorted(lesson_vocab_ids_scope),
                    lesson_quota,
                    reinforce_quota,
                    len(selected_units),
                )
                return self._persist_session(
                    mode=mode,
                    theme=None,
                    selected_units=selected_units,
                    due_only=False,
                    selection_reasons=selection_reasons,
                    passive_recall_after_completion="lesson",
                )

            # RECALL / RECALL_AUDIO: same PL–UA lesson scope, introduced units only (UI may open
            # lesson URL with recall after introduction is complete; no passive→recall follow-up id).
            introduced_query = (
                self.db.query(LearningUnit)
                .options(joinedload(LearningUnit.progress))
                .join(LearningProgress, LearningProgress.unit_id == LearningUnit.id)
                .filter(LearningProgress.introduced_at.isnot(None))
            )
            introduced_query = self.apply_selection_filters(
                introduced_query,
                source_pdfs=None,
                theme_vocab_ids=None,
                lesson_vocab_ids=lesson_vocab_ids_scope,
            )
            candidates = introduced_query.all()
            if len(candidates) < SESSION_SIZE:
                raise InsufficientUnitsError(
                    f"Not enough introduced words for Recall in the current PL–UA lesson. "
                    f"Need at least {SESSION_SIZE} introduced words, "
                    f"currently have {len(candidates)}.",
                    code="INSUFFICIENT_INTRODUCED_RECALL",
                )
            self._rng.shuffle(candidates)
            selected_units = candidates[:SESSION_SIZE]
            logger.info(
                "lesson mode: lesson_index=%s candidates=%d selected=%d",
                lesson_index,
                len(candidates),
                len(selected_units),
            )
            now_snap = _utc_now_naive()
            selection_reasons_lr: dict[int, str] = {}
            for u in selected_units:
                selection_reasons_lr[u.id] = self._infer_selection_reason(
                    u, getattr(u, "progress", None), now_snap
                )
            logger.info(
                "Curriculum lesson recall session: lesson_index=%s plua_vocab_ids=%s units=%s mode=%s",
                lesson_index,
                sorted(lesson_vocab_ids_scope),
                len(selected_units),
                mode.value,
            )
            return self._persist_session(
                mode=mode,
                theme=None,
                selected_units=selected_units,
                due_only=False,
                selection_reasons=selection_reasons_lr,
                passive_recall_after_completion=None,
            )

        lesson_to_vocab = build_lesson_to_vocab(self.db)
        weak_session = weak_only and not due_only

        # Home "new words" (new_words_focus): lesson map is only czytaj_* groups — Polish–Ukrainian
        # lists are excluded from curriculum IDs. Use full library for selection + preflight so
        # 60/40 balancing and passive pools can see all PDFs (theme filter still applies).
        full_library_new_words = (
            new_words_focus
            and mode == StudyModeType.PASSIVE
            and not weak_only
            and not due_only
            and source_pdfs is None
            and lesson_id is None
        )

        if full_library_new_words:
            query = self.db.query(func.count(LearningUnit.id))
            query = self.apply_selection_filters(
                query,
                source_pdfs=source_pdfs,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=None,
            )
            total_units = query.scalar() or 0
            if total_units < SESSION_SIZE:
                pdf_info = f" from selected files" if source_pdfs else ""
                raise InsufficientUnitsError(
                    f"Need at least {SESSION_SIZE} words{pdf_info} to start practice. "
                    f"Currently have {total_units}."
                )
            lesson_vocab_ids: set[int] = set()
            selection_lesson_vocab_ids: Optional[set[int]] = None
        else:
            if not lesson_to_vocab:
                raise InsufficientUnitsError(
                    "Not enough words for this lesson window. Check lesson mapping."
                )

            max_lesson = max(lesson_to_vocab.keys())
            if lesson_id is not None:
                effective_lesson = max(1, min(lesson_id, max_lesson))
            else:
                effective_lesson = _detect_current_lesson(self.db)

            lower_bound = max(1, effective_lesson - SPILLOVER_DEPTH)
            lesson_window = range(lower_bound, effective_lesson + 1)

            lesson_vocab_ids = set()
            for lid in lesson_window:
                lesson_vocab_ids.update(lesson_to_vocab.get(lid, []))

            def _count_units_for_vocab_ids(vocab_ids: set[int]) -> int:
                count_query = self.db.query(func.count(LearningUnit.id))
                count_query = self.apply_selection_filters(
                    count_query,
                    source_pdfs=source_pdfs,
                    theme_vocab_ids=theme_vocab_ids,
                    lesson_vocab_ids=vocab_ids,
                )
                return count_query.scalar() or 0

            # Mandatory autopacing with strict curriculum bounds:
            # expand lesson window within lesson_to_vocab only.
            required_units = SESSION_SIZE
            total_units_in_window = _count_units_for_vocab_ids(lesson_vocab_ids)
            upper_bound = effective_lesson
            while total_units_in_window < required_units and (lower_bound > 1 or upper_bound < max_lesson):
                if lower_bound > 1:
                    lower_bound -= 1
                elif upper_bound < max_lesson:
                    upper_bound += 1

                lesson_vocab_ids = set()
                for lid in range(lower_bound, upper_bound + 1):
                    lesson_vocab_ids.update(lesson_to_vocab.get(lid, []))
                total_units_in_window = _count_units_for_vocab_ids(lesson_vocab_ids)

            if total_units_in_window < required_units:
                if lesson_vocab_ids is not None and total_units_in_window == 0 and source_pdfs:
                    logger.info(
                        "Lesson window empty for selected sources %s — falling back to source-only filtering.",
                        source_pdfs,
                    )
                    lesson_vocab_ids = None
                else:
                    raise InsufficientUnitsError(
                        "Not enough words for this lesson window. Check lesson mapping."
                    )

            query = self.db.query(func.count(LearningUnit.id))
            query = self.apply_selection_filters(
                query,
                source_pdfs=source_pdfs,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=lesson_vocab_ids,
            )
            total_units = query.scalar()

            if not due_only and not weak_session and total_units < SESSION_SIZE:
                pdf_info = f" from selected files" if source_pdfs else ""
                raise InsufficientUnitsError(
                    f"Need at least {SESSION_SIZE} words{pdf_info} to start practice. "
                    f"Currently have {total_units}."
                )

            selection_lesson_vocab_ids = (
                None if weak_session and lesson_id is None else lesson_vocab_ids
            )

            # --- Chat Vocabulary integration (safe, normal sessions only) ---
            chat_vocab_id = None

            chat_vocab = (
                self.db.query(Vocabulary)
                .filter(
                    Vocabulary.name == "Chat Vocabulary",
                    Vocabulary.user_key == "local",
                )
                .first()
            )

            if chat_vocab:
                chat_vocab_id = chat_vocab.id

            if (
                chat_vocab_id is not None
                and selection_lesson_vocab_ids is not None
            ):
                selection_lesson_vocab_ids = set(selection_lesson_vocab_ids)
                selection_lesson_vocab_ids.add(chat_vocab_id)

        # If too many reviews are overdue, spread them across the next days first
        ensure_overdue_spread(self.db)

        relax_follow_up_scope = retry_failed_only or (
            follow_up_session_id is not None
            and follow_up_prior_session is not None
            and follow_up_prior_session.mode == StudyModeType.PASSIVE
        )

        balanced_never_seen_mix = (
            mode == StudyModeType.PASSIVE
            and not weak_only
            and not due_only
            and source_pdfs is None
        )

        # Normal passive: by default due items fill up to 70% first. Home "new words" skips that so
        # introduced/due cards are not stacked at the start of the session.
        use_due_first_split = not weak_only and not (
            new_words_focus and mode == StudyModeType.PASSIVE
        )

        priority_units: list[LearningUnit] = []
        if follow_up_priority_ids:
            priority_units = self._build_follow_up_priority_units(
                priority_unit_ids=follow_up_priority_ids,
                source_pdfs=source_pdfs,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=selection_lesson_vocab_ids,
                mode=mode,
                relax_selection_scope=relax_follow_up_scope,
            )

        # For Recall modes: validate that enough introduced units exist (same scope as selection)
        is_recall_mode = is_recall_like_study_mode(mode)
        if is_recall_mode:
            introduced_count_query = (
                self.db.query(func.count(LearningUnit.id))
                .join(LearningProgress)
            )
            introduced_count_query = self.apply_selection_filters(
                introduced_count_query,
                source_pdfs=source_pdfs,
                theme_vocab_ids=theme_vocab_ids,
                lesson_vocab_ids=selection_lesson_vocab_ids,
            ).filter(LearningProgress.introduced_at.isnot(None))
            introduced_count = introduced_count_query.scalar() or 0

            if weak_session:
                if not follow_up_session_id:
                    if introduced_count < 1:
                        pdf_info = f" from selected files" if source_pdfs else ""
                        raise InsufficientUnitsError(
                            f"No introduced words{pdf_info} for Recall weak practice. "
                            f"Study in Passive mode first."
                        )
                elif not priority_units:
                    if retry_failed_only:
                        raise InsufficientUnitsError(
                            "No failed words to retry from previous session"
                        )
                    raise InsufficientUnitsError(
                        "No eligible introduced words from follow-up session.",
                        code="NO_FOLLOWUP_INTRODUCED",
                    )
            elif not follow_up_session_id:
                if introduced_count < SESSION_SIZE:
                    pdf_info = f" from selected files" if source_pdfs else ""
                    raise InsufficientUnitsError(
                        f"Not enough introduced words for Recall mode. "
                        f"Please study in Passive mode first. "
                        f"Need at least {SESSION_SIZE} introduced words{pdf_info}, "
                        f"currently have {introduced_count}.",
                        code="INSUFFICIENT_INTRODUCED_RECALL",
                    )
            elif not priority_units:
                if retry_failed_only:
                    raise InsufficientUnitsError(
                        "No failed words to retry from previous session"
                    )
                raise InsufficientUnitsError(
                    "No eligible introduced words from follow-up session.",
                    code="NO_FOLLOWUP_INTRODUCED",
                )

        effective_session_size = SESSION_SIZE
        if retry_failed_only and follow_up_session_id:
            failed_count = len(priority_units)
            effective_session_size = min(SESSION_SIZE, failed_count + RETRY_SESSION_TAIL_PADDING)
            logger.info(
                "Retry session size cap: failed=%s effective_session_size=%s (SESSION_SIZE=%s)",
                failed_count,
                effective_session_size,
                SESSION_SIZE,
            )

        selected_prefix = priority_units[:effective_session_size]
        remaining_slots = effective_session_size - len(selected_prefix)
        if follow_up_session_id is not None:
            # Follow-up sessions use prefix only; tail refill disabled (see _follow_up_tail_refill_pass).
            remaining_slots = 0
        already_selected_ids = frozenset(u.id for u in selected_prefix)

        additional_units: list[LearningUnit] = []
        retry_tail_fallback_used = False
        if remaining_slots > 0:
            seen_prefix = {u.id for u in selected_prefix}
            if follow_up_session_id:
                # Pipeline may emit duplicate ids; dedupe and refill until full or exhausted.
                seen_tail = set(seen_prefix)
                slots_left = remaining_slots
                tail_requested = remaining_slots
                max_refill_passes = 8
                failed_ids_for_retry_tail = (
                    frozenset(follow_up_priority_ids) if retry_failed_only else frozenset()
                )

                def _follow_up_tail_refill_pass(
                    pass_label: str,
                    exclude_failed_from_pool: bool,
                ) -> None:
                    nonlocal slots_left
                    for pass_idx in range(1, max_refill_passes + 1):
                        if slots_left <= 0:
                            break
                        excl = set(seen_tail)
                        if exclude_failed_from_pool and failed_ids_for_retry_tail:
                            excl |= set(failed_ids_for_retry_tail)
                        request = self._build_selection_request(
                            mode="weak_only" if weak_only else "normal",
                            study_mode=mode,
                            session_size=slots_left,
                            pool_kind="weak" if weak_only else "normal",
                            apply_reinforcement_only=weak_only,
                            use_due_first_split=use_due_first_split,
                            weak_only_padding=False,
                            source_pdfs=source_pdfs,
                            theme=theme,
                            theme_vocab_ids=theme_vocab_ids,
                            lesson_vocab_ids=selection_lesson_vocab_ids,
                            now=_utc_now_naive(),
                            exclude_unit_ids=frozenset(excl),
                            balanced_never_seen_mix=balanced_never_seen_mix,
                            new_words_focus=new_words_focus,
                        )
                        batch = self._execute_selection_pipeline(request)
                        added = 0
                        skip_never_seen = 0
                        skip_weak = 0
                        skip_vocab_cap = 0
                        tail_vocab_counts: dict[Optional[int], int] = {}
                        for existing in additional_units:
                            vid = existing.vocabulary_id
                            tail_vocab_counts[vid] = tail_vocab_counts.get(vid, 0) + 1
                        for u in batch:
                            if u.id in seen_tail:
                                continue
                            prog = getattr(u, "progress", None)
                            if prog is None or prog.introduced_at is None:
                                skip_never_seen += 1
                                continue
                            if (
                                prog.confidence_score < WEAK_THRESHOLD
                                or prog.times_failed > prog.times_correct
                                or prog.last_recall_result == RecallResult.FAILED
                            ):
                                skip_weak += 1
                                continue
                            vid = u.vocabulary_id
                            if (
                                tail_vocab_counts.get(vid, 0)
                                >= FOLLOW_UP_TAIL_MAX_PER_VOCABULARY
                            ):
                                skip_vocab_cap += 1
                                continue
                            seen_tail.add(u.id)
                            additional_units.append(u)
                            tail_vocab_counts[vid] = tail_vocab_counts.get(vid, 0) + 1
                            added += 1
                            slots_left -= 1
                            if slots_left <= 0:
                                break
                        logger.debug(
                            "Follow-up tail refill [%s]: pass=%d added=%d slots_left=%d tail_total=%d "
                            "skip_never_seen=%d skip_weak=%d skip_vocab_cap=%d",
                            pass_label,
                            pass_idx,
                            added,
                            slots_left,
                            len(additional_units),
                            skip_never_seen,
                            skip_weak,
                            skip_vocab_cap,
                        )
                        if added == 0:
                            break

                _follow_up_tail_refill_pass("strict", exclude_failed_from_pool=bool(retry_failed_only))
                if retry_failed_only and slots_left > 0:
                    retry_tail_fallback_used = True
                    logger.warning(
                        "RETRY FALLBACK: insufficient non-failed units, reusing failed units in tail"
                    )
                    _follow_up_tail_refill_pass("fallback_include_failed", exclude_failed_from_pool=False)

                if len(additional_units) < tail_requested:
                    logger.warning(
                        "Follow-up tail underfilled: requested=%d got=%d follow_up_session_id=%s",
                        tail_requested,
                        len(additional_units),
                        follow_up_session_id,
                    )
            else:
                request = self._build_selection_request(
                    mode="weak_only" if weak_only else "normal",
                    study_mode=mode,
                    session_size=remaining_slots,
                    pool_kind="weak" if weak_only else "normal",
                    apply_reinforcement_only=weak_only,
                    use_due_first_split=use_due_first_split,
                    weak_only_padding=False,
                    source_pdfs=source_pdfs,
                    theme=theme,
                    theme_vocab_ids=theme_vocab_ids,
                    lesson_vocab_ids=selection_lesson_vocab_ids,
                    now=_utc_now_naive(),
                    exclude_unit_ids=already_selected_ids,
                    balanced_never_seen_mix=balanced_never_seen_mix,
                    new_words_focus=new_words_focus,
                )
                batch = self._execute_selection_pipeline(request)
                additional_units = [
                    u for u in batch if u.id not in seen_prefix
                ][:remaining_slots]

        tail = list(additional_units)
        if tail:
            self._rng.shuffle(tail)
        selected_units = selected_prefix + tail

        chat_vocab = (
            self.db.query(Vocabulary)
            .filter(
                Vocabulary.name == "Chat Vocabulary",
                Vocabulary.user_key == "local",
            )
            .first()
        )

        chat_vocab_id = chat_vocab.id if chat_vocab else None

        RECENT_DAYS = 7
        recent_cutoff = _utc_now_naive() - timedelta(days=RECENT_DAYS)

        if chat_vocab_id is not None and selected_units:
            recent_chat_units = [
                u for u in selected_units
                if (
                    u.vocabulary_id == chat_vocab_id
                    and u.created_at is not None
                    and u.created_at >= recent_cutoff
                )
            ]

            other_units = [
                u for u in selected_units
                if u not in recent_chat_units
            ]

            selected_units = recent_chat_units + other_units

        chat_vocab = (
            self.db.query(Vocabulary)
            .filter(
                Vocabulary.name == "Chat Vocabulary",
                Vocabulary.user_key == "local",
            )
            .first()
        )
        chat_vocab_id = chat_vocab.id if chat_vocab else None

        print("---- FINAL SELECTION ----")

        chat_selected = 0
        for u in selected_units:
            if u.vocabulary_id == chat_vocab_id:
                chat_selected += 1

        print("Chat words selected:", chat_selected)

        if retry_failed_only and follow_up_session_id:
            failed_unit_ids = set(follow_up_priority_ids)
            logger.info(
                "RETRY SELECTION: failed_units=%d selected=%d unit_ids=%s",
                len(follow_up_priority_ids),
                len(selected_units),
                [u.id for u in selected_units][:10],
            )
            n_pref = len(selected_prefix)
            tail_id_set = {u.id for u in selected_units[n_pref:]}
            if tail_id_set & failed_unit_ids and not retry_tail_fallback_used:
                logger.error(
                    "RETRY LEAK: tail reuses source-session failed units after strict exclude"
                )

        if new_words_focus and mode == StudyModeType.PASSIVE:
            logger.info(
                "new_words_debug [create_session] follow_up_session_id=%s retry_failed_only=%s "
                "prefix_count=%s tail_count=%s total=%s",
                follow_up_session_id,
                retry_failed_only,
                len(selected_prefix),
                len(tail),
                len(selected_units),
            )
            if selected_prefix:
                self._new_words_focus_debug_unit_mix(
                    selected_prefix,
                    "create_session prefix (follow_up / retry order)",
                    error_if_introduced=False,
                )
            if tail:
                self._new_words_focus_debug_unit_mix(
                    tail,
                    "create_session tail (_execute_selection_pipeline)",
                    error_if_introduced=False,
                )
            self._new_words_focus_debug_unit_mix(
                selected_units,
                "create_session full session (prefix + tail)",
                error_if_introduced=True,
            )

        if follow_up_session_id:
            logger.info(
                "Follow-up session create: source_id=%s prefix=%d tail=%d total=%d",
                follow_up_session_id,
                len(selected_prefix),
                len(tail),
                len(selected_units),
            )

        selection_reasons: dict[int, str] = {}
        if follow_up_session_id:
            prefix_label = "retry" if retry_failed_only else "follow_up"
            for u in selected_prefix:
                selection_reasons[u.id] = prefix_label
        now_snap = _utc_now_naive()
        for u in selected_units:
            if u.id not in selection_reasons:
                selection_reasons[u.id] = self._infer_selection_reason(
                    u, getattr(u, "progress", None), now_snap
                )
        assert all(u.id in selection_reasons for u in selected_units)

        return self._persist_session(
            mode=mode,
            theme=theme,
            selected_units=selected_units,
            selection_reasons=selection_reasons,
            passive_recall_after_completion=(
                "weak" if weak_session and mode == StudyModeType.PASSIVE else None
            ),
        )
    
    def _compute_unit_weight(
        self,
        unit: LearningUnit,
        bucket_type: str,
        now: Optional[datetime] = None,
    ) -> float:
        """
        Compute selection weight for a unit.
        
        Weight formula:
        weight = min(
            base_priority * failure_multiplier * time_boost * recall_penalty * stuck_boost,
            WEIGHT_MAX_CAP  # 10.0 - prevents priority explosion
        )
        
        Args:
            unit: The learning unit.
            bucket_type: "new", "weak", or "review".
            now: Current time for decay calculation.
            
        Returns:
            Selection weight (higher = more likely to be selected), capped at WEIGHT_MAX_CAP.
        """
        if now is None:
            now = _utc_now_naive()
        
        # Base priority by bucket type
        if bucket_type == "new":
            base = WEIGHT_BASE_NEW
        elif bucket_type == "weak":
            base = WEIGHT_BASE_WEAK
        else:  # review
            base = WEIGHT_BASE_REVIEW
        
        # If no progress, return base weight
        if not unit.progress:
            return base
        
        progress = unit.progress
        weight = base
        now_naive = _naive_utc(now)

        next_review_at = getattr(progress, "next_review_at", None)
        next_review_at = _naive_utc(next_review_at) if isinstance(next_review_at, datetime) else None
        is_due = next_review_at is not None and next_review_at <= now_naive
        
        # Failure multiplier: boost words with more failures than successes
        if progress.times_failed > progress.times_correct:
            weight *= WEIGHT_FAILURE_MULTIPLIER
        
        # Time decay multiplier: words not seen recently get boosted
        # Inverse of decay - older words get higher weight
        decay = compute_time_decay(progress.last_seen, now)
        time_boost = 1.0 + (1.0 - decay)  # 1.0 to 1.6 range
        weight *= time_boost

        if next_review_at is not None and next_review_at < now_naive:
            overdue_seconds = max(0, (now_naive - next_review_at).total_seconds())
            overdue_days = overdue_seconds / 86400
            overdue_multiplier = 1 + min(1.5, overdue_days * 0.15)
            weight *= overdue_multiplier
        
        # Recall penalty: words with recent recall failure get boosted
        if progress.last_recall_result == RecallResult.FAILED:
            weight *= WEIGHT_RECALL_PENALTY
        
        # Stuck boost: words failing repeatedly need more exposure
        recall_fail_streak = getattr(progress, "recall_fail_streak", 0) or 0
        if not isinstance(recall_fail_streak, int):
            recall_fail_streak = 0
        if recall_fail_streak >= WEIGHT_STUCK_THRESHOLD:
            weight *= WEIGHT_STUCK_BOOST

        # Soft diversity: recently seen non-due units compete a bit less (due scheduling unchanged)
        last_seen = getattr(progress, "last_seen", None)
        if last_seen is not None:
            last_seen = _naive_utc(last_seen)
            if not is_due:
                delta_sec = (now_naive - last_seen).total_seconds()
                if 0 <= delta_sec < RECENCY_DIVERSITY_WINDOW_SHORT_SEC:
                    weight *= RECENCY_DIVERSITY_FACTOR_SHORT
                elif delta_sec < RECENCY_DIVERSITY_WINDOW_LONG_SEC:
                    weight *= RECENCY_DIVERSITY_FACTOR_LONG

        # Previous completed session diversity (non-due only; empty if follow-up / retry)
        if (
            not is_due
            and self._last_session_unit_ids
            and unit.id in self._last_session_unit_ids
        ):
            weight *= LAST_SESSION_DIVERSITY_FACTOR
        
        # Cap weight to prevent priority explosion
        return min(weight, WEIGHT_MAX_CAP)
    
    def _weighted_random_sample(
        self,
        units_with_weights: list[tuple[LearningUnit, float]],
        count: int,
        selected_ids: set[int],
    ) -> list[LearningUnit]:
        """
        Select units using weighted random sampling without replacement.
        
        Args:
            units_with_weights: List of (unit, weight) tuples.
            count: Number of units to select.
            selected_ids: Set of already-selected unit IDs to exclude.
            
        Returns:
            List of selected LearningUnit objects.
        """
        # Filter out already-selected units
        available = [(u, w) for u, w in units_with_weights if u.id not in selected_ids]

        logger.debug(
            "_weighted_random_sample: pool=%d k=%d post_shuffle=no",
            len(available),
            count,
        )

        if not available:
            return []
        
        selected = []
        remaining = list(available)
        
        for _ in range(min(count, len(remaining))):
            if not remaining:
                break
            
            # Extract weights
            weights = [w for _, w in remaining]
            total_weight = sum(weights)
            
            if total_weight == 0:
                # All weights are 0, select uniformly
                idx = self._rng.randint(0, len(remaining) - 1)
            else:
                # Weighted random selection
                r = self._rng.uniform(0, total_weight)
                cumulative = 0
                idx = 0
                for i, w in enumerate(weights):
                    cumulative += w
                    if r <= cumulative:
                        idx = i
                        break
            
            # Select and remove from remaining
            unit, _ = remaining.pop(idx)
            selected.append(unit)
            selected_ids.add(unit.id)
        
        return selected
    
    def _select_units_weighted_random(
        self,
        source_pdfs: Optional[list[str]] = None,
        mode: StudyModeType = StudyModeType.PASSIVE,
        theme: Optional[str] = None,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: set[int] = None,
        weak_only: bool = False,
        due_only: bool = False,
    ) -> list[LearningUnit]:
        """
        Select units using weighted random sampling with bucket composition.
        
        Selection order (SRS-lite prioritization):
        1. Due items (next_review_at <= now) - up to 70% of session
        2. Bucket-based selection for remaining slots:
           - 30% new (never seen)
           - 40% weak/failed
           - 30% review (known)
        
        Falls back to other buckets if a bucket is empty.
        
        Passive → Recall gating:
        - Passive mode: Prioritizes units where introduced_at IS NULL (new words)
        - Recall modes: ONLY selects units where introduced_at IS NOT NULL
        
        Args:
            source_pdfs: Optional list of PDF filenames to filter by.
            mode: Study mode type (affects introduced_at filtering).
            weak_only: If True, only select from weak units (confidence <50%).
            due_only: If True, only select from due units (next_review_at <= now).
            
        Returns:
            List of exactly SESSION_SIZE LearningUnit objects.
        """
        now = _utc_now_naive()
        selected: list[LearningUnit] = []
        selected_ids: set[int] = set()
        is_recall_mode = is_recall_like_study_mode(mode)
        
        # ===================
        # Special filtering modes (weak_only or due_only)
        # ===================
        if weak_only:
            # Only strict weak units (stored confidence <50%); no review/new padding.
            weak_units = self._get_weak_units_weighted(
                source_pdfs, now, mode, strict=True, include_blocked=True,
                theme_vocab_ids=theme_vocab_ids, lesson_vocab_ids=lesson_vocab_ids
            )
            if len(weak_units) == 0:
                pdf_info = f" from selected files" if source_pdfs else ""
                raise InsufficientUnitsError(
                    f"No difficult words for practice (confidence below 50%{pdf_info}) in this lesson scope. "
                    f"Try normal study or widen your vocabulary selection."
                )
            take = min(SESSION_SIZE, len(weak_units))
            selected = self._weighted_random_sample(weak_units, take, selected_ids)
            return selected
        
        if due_only:
            # Due-only mode uses dynamic session size: all available due units.
            due_units = self._get_due_units_weighted(
                source_pdfs, now, mode,
                theme_vocab_ids=theme_vocab_ids, lesson_vocab_ids=lesson_vocab_ids
            )
            num_due = len(due_units)
            if num_due == 0:
                raise InsufficientUnitsError("No words are due for review right now.")

            selected = self._weighted_random_sample(due_units, num_due, selected_ids)
            logger.info(f"Due-only session: selected {len(selected)} units from {num_due} due units")
            return selected
        
        # ===================
        # SRS-lite: First select due items (normal mode)
        # ===================
        max_due_items = int(SESSION_SIZE * DUE_ITEMS_MAX_PERCENT)
        due_units = self._get_due_units_weighted(
            source_pdfs, now, mode,
            theme_vocab_ids=theme_vocab_ids, lesson_vocab_ids=lesson_vocab_ids
        )
        
        if due_units:
            # Select due items first (up to max_due_items)
            due_selected = self._weighted_random_sample(due_units, max_due_items, selected_ids)
            selected.extend(due_selected)
            logger.debug(f"Selected {len(due_selected)} due items for session")
        
        # ===================
        # Fill remaining slots with bucket-based selection
        # ===================
        remaining_slots = SESSION_SIZE - len(selected)
        
        if remaining_slots > 0:
            # Adaptive readiness gate: may suppress new words and shift budget to weak/review
            from app.services.daily_stats import get_daily_dashboard_stats
            _ds = get_daily_dashboard_stats(self.db)
            new_pct, _gate_reason = _compute_new_words_readiness(
                words_introduced_today=_ds.get("words_introduced_today", 0),
                max_new_per_day=_ds.get("max_new_per_day", 60),
                accuracy_today=_ds.get("recall_accuracy_today"),
                weak_count=_ds.get("weak_words_count", 0),
            )
            if _gate_reason:
                self._last_readiness_gate = _gate_reason
            freed = BUCKET_NEW_PERCENT - new_pct
            effective_weak_pct = BUCKET_WEAK_PERCENT + freed * (2 / 3)
            effective_review_pct = BUCKET_REVIEW_PERCENT + freed * (1 / 3)
            target_new = int(remaining_slots * new_pct)
            target_weak = int(remaining_slots * effective_weak_pct)
            target_review = remaining_slots - target_new - target_weak
            
            # Get all units categorized into buckets (with mode-aware filtering)
            new_units = self._get_new_units_weighted(
                source_pdfs, now, mode,
                theme_vocab_ids=theme_vocab_ids, lesson_vocab_ids=lesson_vocab_ids
            )
            weak_units = self._get_weak_units_weighted(
                source_pdfs, now, mode,
                theme_vocab_ids=theme_vocab_ids, lesson_vocab_ids=lesson_vocab_ids
            )
            review_units = self._get_review_units_weighted(
                source_pdfs, now, mode,
                theme_vocab_ids=theme_vocab_ids, lesson_vocab_ids=lesson_vocab_ids
            )
            
            # Select from each bucket using weighted random sampling
            # Bucket 1: New units
            new_selected = self._weighted_random_sample(new_units, target_new, selected_ids)
            selected.extend(new_selected)
            
            # Bucket 2: Weak/failed units
            weak_selected = self._weighted_random_sample(weak_units, target_weak, selected_ids)
            selected.extend(weak_selected)
            
            # Bucket 3: Review units
            review_selected = self._weighted_random_sample(review_units, target_review, selected_ids)
            selected.extend(review_selected)
            
            # Fill remaining slots from any available bucket
            remaining_needed = SESSION_SIZE - len(selected)
            if remaining_needed > 0:
                # Combine all remaining units
                all_remaining = (
                    [(u, w) for u, w in new_units if u.id not in selected_ids] +
                    [(u, w) for u, w in weak_units if u.id not in selected_ids] +
                    [(u, w) for u, w in review_units if u.id not in selected_ids]
                )
                fill_selected = self._weighted_random_sample(all_remaining, remaining_needed, selected_ids)
                selected.extend(fill_selected)
        
        return selected[:SESSION_SIZE]
    
    def _get_due_units_weighted(
        self,
        source_pdfs: Optional[list[str]],
        now: datetime,
        mode: StudyModeType = StudyModeType.PASSIVE,
        include_blocked: bool = False,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: set[int] = None,
    ) -> list[tuple[LearningUnit, float]]:
        """
        Get units that are due for review (next_review_at <= now) with weights.
        
        Due units are prioritized for session selection.
        Weights are computed the same as weak units to maintain bias toward
        failed/struggling words within due items.
        
        Blocked word behavior:
        - By default (include_blocked=False), blocked words are EXCLUDED from normal sessions
        - Blocked words only appear in weak-only practice sessions (include_blocked=True)
        
        Passive → Recall gating:
        - Recall modes: Only includes units where introduced_at IS NOT NULL
        - Passive mode: Includes all due units (introduced or not)
        """
        is_recall_mode = is_recall_like_study_mode(mode)
        
        query = (
            self.db.query(LearningUnit)
            .join(LearningProgress)
            .options(joinedload(LearningUnit.progress))
        )
        
        # Exclude blocked words from normal sessions
        if not include_blocked:
            query = query.filter(LearningProgress.is_blocked == False)
        
        # Recall modes: only introduced units
        if is_recall_mode:
            query = query.filter(LearningProgress.introduced_at.isnot(None))
        
        query = self.apply_selection_filters(
            query,
            source_pdfs=source_pdfs,
            theme_vocab_ids=theme_vocab_ids,
            lesson_vocab_ids=lesson_vocab_ids,
        )
        query = query.filter(LearningProgress.next_review_at <= now)

        units = query.all()
        
        # Use weak bucket weighting to maintain bias toward failed words
        return [(u, self._compute_unit_weight(u, "weak", now)) for u in units]
    
    def _get_new_units_weighted(
        self,
        source_pdfs: Optional[list[str]],
        now: datetime,
        mode: StudyModeType = StudyModeType.PASSIVE,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: set[int] = None,
    ) -> list[tuple[LearningUnit, float]]:
        """
        Get new units with weights.
        
        Blocked word behavior:
        - New units in Passive mode cannot be blocked (no progress record yet)
        - In Recall mode, blocked words are excluded
        
        Passive → Recall gating:
        - Passive mode: Units where introduced_at IS NULL (never introduced)
        - Recall modes: Units where introduced_at IS NOT NULL (newly introduced, eligible for recall)
        """
        is_recall_mode = is_recall_like_study_mode(mode)
        
        if is_recall_mode:
            # Recall mode: "new" means newly introduced (introduced_at IS NOT NULL)
            # Prioritize units with recent introduced_at
            # Exclude blocked words
            query = (
                self.db.query(LearningUnit)
                .join(LearningProgress)
                .options(joinedload(LearningUnit.progress))
                .filter(LearningProgress.introduced_at.isnot(None))
                .filter(LearningProgress.is_blocked == False)
            )
        else:
            # Passive mode: "new" means never introduced (introduced_at IS NULL)
            # New units can't be blocked (no progress record or not yet introduced)
            query = (
                self.db.query(LearningUnit)
                .outerjoin(LearningProgress)
                .filter(
                    (LearningProgress.id.is_(None)) |  # No progress record
                    (LearningProgress.introduced_at.is_(None))  # Progress exists but not introduced
                )
            )
        
        query = self.apply_selection_filters(
            query,
            source_pdfs=source_pdfs,
            theme_vocab_ids=theme_vocab_ids,
            lesson_vocab_ids=lesson_vocab_ids,
        )

        candidates = query.all()

        chat_vocab = (
            self.db.query(Vocabulary)
            .filter(
                Vocabulary.name == "Chat Vocabulary",
                Vocabulary.user_key == "local",
            )
            .first()
        )
        chat_vocab_id = chat_vocab.id if chat_vocab else None

        print("---- NEW POOL DEBUG ----")
        print("Total new candidates:", len(candidates))

        chat_count = 0
        for u in candidates:
            if u.vocabulary_id == chat_vocab_id:
                chat_count += 1

        print("Chat vocab candidates:", chat_count)

        return [(u, self._compute_unit_weight(u, "new", now)) for u in candidates]
    
    def _get_weak_units_weighted(
        self,
        source_pdfs: Optional[list[str]],
        now: datetime,
        mode: StudyModeType = StudyModeType.PASSIVE,
        strict: bool = False,
        include_blocked: bool = False,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: set[int] = None,
    ) -> list[tuple[LearningUnit, float]]:
        """
        Get weak/failed units with weights.
        
        Blocked word behavior:
        - By default (include_blocked=False), blocked words are EXCLUDED
        - When include_blocked=True (used for weak-only sessions), blocked words ARE included
        - Blocked words with fail_streak >= 5 appear ONLY in weak-only practice sessions
        - This is the designated place for users to work on persistently difficult words
        
        When strict=False (default, used for normal session composition):
        - Units with effective confidence < WEAK_THRESHOLD
        - Units with times_failed > times_correct
        - Units with last_recall_result == FAILED
        
        When strict=True (used for weak_only sessions):
        - ONLY units with stored confidence_score < WEAK_THRESHOLD
        - This matches the weak words count shown on the progress page
        
        Passive → Recall gating:
        - Recall modes: Only includes units where introduced_at IS NOT NULL
        - Passive mode: Includes all weak units (introduced or not)
        """
        is_recall_mode = is_recall_like_study_mode(mode)
        
        query = (
            self.db.query(LearningUnit)
            .join(LearningProgress)
            .options(joinedload(LearningUnit.progress))
        )
        
        # Exclude blocked words unless explicitly requested (weak-only sessions)
        if not include_blocked:
            query = query.filter(LearningProgress.is_blocked == False)
        
        # Recall modes: only introduced units
        if is_recall_mode:
            query = query.filter(LearningProgress.introduced_at.isnot(None))
        
        query = self.apply_selection_filters(
            query,
            source_pdfs=source_pdfs,
            theme_vocab_ids=theme_vocab_ids,
            lesson_vocab_ids=lesson_vocab_ids,
            weak_only=strict,
        )
        
        units = query.all()
        
        weak_units = []
        for unit in units:
            if not unit.progress:
                continue
            
            if strict:
                # Strict mode: only confidence_score < WEAK_THRESHOLD
                # (already filtered at DB level, but double-check)
                is_weak = unit.progress.confidence_score < WEAK_THRESHOLD
            else:
                # Normal mode: broader definition for session composition
                effective_conf = compute_effective_confidence(
                    unit.progress.confidence_score,
                    unit.progress.last_seen,
                    now,
                )
                is_weak = (
                    effective_conf < WEAK_THRESHOLD or
                    unit.progress.times_failed > unit.progress.times_correct or
                    unit.progress.last_recall_result == RecallResult.FAILED
                )
            
            if is_weak:
                weight = self._compute_unit_weight(unit, "weak", now)
                weak_units.append((unit, weight))
        
        return weak_units
    
    def _get_review_units_weighted(
        self,
        source_pdfs: Optional[list[str]],
        now: datetime,
        mode: StudyModeType = StudyModeType.PASSIVE,
        theme_vocab_ids: Optional[set[int]] = None,
        lesson_vocab_ids: set[int] = None,
    ) -> list[tuple[LearningUnit, float]]:
        """
        Get review units (known but need reinforcement) with weights.
        
        Units with effective confidence >= WEAK_THRESHOLD that aren't failed.
        
        Blocked word behavior:
        - Blocked words (is_blocked=True) are EXCLUDED from review units
        - They belong in the weak bucket for weak-only practice sessions
        
        Passive → Recall gating:
        - Recall modes: Only includes units where introduced_at IS NOT NULL
        - Passive mode: Includes all review units (introduced or not)
        """
        is_recall_mode = is_recall_like_study_mode(mode)
        
        query = (
            self.db.query(LearningUnit)
            .join(LearningProgress)
            .options(joinedload(LearningUnit.progress))
            # Exclude blocked words from review units
            .filter(LearningProgress.is_blocked == False)
        )
        
        # Recall modes: only introduced units
        if is_recall_mode:
            query = query.filter(LearningProgress.introduced_at.isnot(None))
        
        query = self.apply_selection_filters(
            query,
            source_pdfs=source_pdfs,
            theme_vocab_ids=theme_vocab_ids,
            lesson_vocab_ids=lesson_vocab_ids,
        )
        
        units = query.all()
        
        review_units = []
        for unit in units:
            if not unit.progress:
                continue
            
            effective_conf = compute_effective_confidence(
                unit.progress.confidence_score,
                unit.progress.last_seen,
                now,
            )
            
            is_known = (
                effective_conf >= WEAK_THRESHOLD and
                unit.progress.times_failed <= unit.progress.times_correct and
                unit.progress.last_recall_result != RecallResult.FAILED
            )
            
            if is_known:
                weight = self._compute_unit_weight(unit, "review", now)
                review_units.append((unit, weight))
        
        return review_units
    
    def _select_units_by_priority(
        self,
        source_pdfs: Optional[list[str]] = None,
    ) -> list[LearningUnit]:
        """
        [DEPRECATED] Select units using deterministic priority order.
        
        Kept for backward compatibility. Use _select_units_weighted_random instead.
        
        Args:
            source_pdfs: Optional list of PDF filenames to filter by.
        
        Returns:
            List of exactly 20 LearningUnit objects.
        """
        selected: list[LearningUnit] = []
        selected_ids: set[int] = set()
        
        # Priority 1: Never seen units (no progress record)
        never_seen = self._get_never_seen_units(SESSION_SIZE, source_pdfs=source_pdfs)
        for unit in never_seen:
            if len(selected) >= SESSION_SIZE:
                break
            if unit.id not in selected_ids:
                selected.append(unit)
                selected_ids.add(unit.id)
        
        if len(selected) >= SESSION_SIZE:
            return selected[:SESSION_SIZE]
        
        # Priority 2: Weak units (confidence < 0.5)
        weak_units = self._get_weak_units(SESSION_SIZE - len(selected), selected_ids, source_pdfs)
        for unit in weak_units:
            if len(selected) >= SESSION_SIZE:
                break
            if unit.id not in selected_ids:
                selected.append(unit)
                selected_ids.add(unit.id)
        
        if len(selected) >= SESSION_SIZE:
            return selected[:SESSION_SIZE]
        
        # Priority 3: Failed units (times_failed > times_correct)
        failed_units = self._get_failed_units(SESSION_SIZE - len(selected), selected_ids, source_pdfs)
        for unit in failed_units:
            if len(selected) >= SESSION_SIZE:
                break
            if unit.id not in selected_ids:
                selected.append(unit)
                selected_ids.add(unit.id)
        
        if len(selected) >= SESSION_SIZE:
            return selected[:SESSION_SIZE]
        
        # Priority 4: Known units (fill remaining)
        known_units = self._get_known_units(SESSION_SIZE - len(selected), selected_ids, source_pdfs)
        for unit in known_units:
            if len(selected) >= SESSION_SIZE:
                break
            if unit.id not in selected_ids:
                selected.append(unit)
                selected_ids.add(unit.id)
        
        return selected[:SESSION_SIZE]
    
    def _get_never_seen_units(
        self,
        limit: int,
        source_pdfs: Optional[list[str]] = None,
    ) -> list[LearningUnit]:
        """
        Get units that have never been seen (no progress record).
        
        Ordered by ID for deterministic selection.
        """
        query = (
            self.db.query(LearningUnit)
            .outerjoin(LearningProgress)
            .filter(LearningProgress.id.is_(None))
        )
        
        if source_pdfs:
            query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))
        
        return query.order_by(LearningUnit.id).limit(limit).all()
    
    def _get_weak_units(
        self,
        limit: int,
        exclude_ids: set[int],
        source_pdfs: Optional[list[str]] = None,
    ) -> list[LearningUnit]:
        """
        Get units with low EFFECTIVE confidence (< WEAK_THRESHOLD).
        
        Effective confidence = stored confidence * time decay.
        This prioritizes words not seen recently.
        
        Ordered by effective confidence ascending (weakest first), then by ID.
        """
        now = _utc_now_naive()
        
        # Fetch all units with progress (we need to calculate effective confidence)
        query = (
            self.db.query(LearningUnit)
            .join(LearningProgress)
            .options(joinedload(LearningUnit.progress))
        )
        
        if exclude_ids:
            query = query.filter(LearningUnit.id.notin_(exclude_ids))
        
        if source_pdfs:
            query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))
        
        units = query.all()
        
        # Calculate effective confidence and filter
        weak_units = []
        for unit in units:
            effective_conf = compute_effective_confidence(
                unit.progress.confidence_score,
                unit.progress.last_seen,
                now,
            )
            if effective_conf < WEAK_THRESHOLD:
                weak_units.append((unit, effective_conf))
        
        # Sort by effective confidence (ascending), then by ID for determinism
        weak_units.sort(key=lambda x: (x[1], x[0].id))
        
        return [u for u, _ in weak_units[:limit]]
    
    def _get_failed_units(
        self,
        limit: int,
        exclude_ids: set[int],
        source_pdfs: Optional[list[str]] = None,
    ) -> list[LearningUnit]:
        """
        Get units where times_failed > times_correct.
        
        Ordered by failure ratio descending, then by ID.
        """
        query = (
            self.db.query(LearningUnit)
            .join(LearningProgress)
            .filter(LearningProgress.times_failed > LearningProgress.times_correct)
        )
        
        if exclude_ids:
            query = query.filter(LearningUnit.id.notin_(exclude_ids))
        
        if source_pdfs:
            query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))
        
        return query.order_by(
            (LearningProgress.times_failed - LearningProgress.times_correct).desc(),
            LearningUnit.id,
        ).limit(limit).all()
    
    def _get_known_units(
        self,
        limit: int,
        exclude_ids: set[int],
        source_pdfs: Optional[list[str]] = None,
    ) -> list[LearningUnit]:
        """
        Get known units (effective confidence >= WEAK_THRESHOLD).
        
        These are units that don't qualify as weak after time decay.
        Used to fill remaining session slots.
        
        Ordered by effective confidence ascending (prioritize those closer to
        becoming weak), then by last_seen (older first), then by ID.
        """
        now = _utc_now_naive()
        
        # Fetch all units with progress
        query = (
            self.db.query(LearningUnit)
            .join(LearningProgress)
            .options(joinedload(LearningUnit.progress))
        )
        
        if exclude_ids:
            query = query.filter(LearningUnit.id.notin_(exclude_ids))
        
        if source_pdfs:
            query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))
        
        units = query.all()
        
        # Calculate effective confidence and filter for known (>= threshold)
        known_units = []
        for unit in units:
            effective_conf = compute_effective_confidence(
                unit.progress.confidence_score,
                unit.progress.last_seen,
                now,
            )
            if effective_conf >= WEAK_THRESHOLD:
                known_units.append((unit, effective_conf, unit.progress.last_seen))
        
        # Sort by:
        # 1. Effective confidence ascending (review lower-confidence known first)
        # 2. Last seen ascending (older first)
        # 3. ID for determinism
        known_units.sort(key=lambda x: (
            x[1],  # effective confidence
            x[2] or datetime.min,  # last_seen (None = very old)
            x[0].id,
        ))
        
        return [u for u, _, _ in known_units[:limit]]

    def _get_theme_metadata_for_unit(
        self,
        unit: Optional[LearningUnit],
    ) -> tuple[Optional[str], Optional[str]]:
        if not unit or unit.vocabulary_id is None:
            return None, None

        try:
            theme = get_theme_by_vocabulary_id(unit.vocabulary_id)
        except KeyError:
            return None, None

        return theme["theme_id"], theme["theme_name"]
    
    def get_session(self, session_id: int) -> Optional[LearningSession]:
        """
        Get a session by ID with all units loaded.
        
        Args:
            session_id: Session ID.
            
        Returns:
            LearningSession or None if not found.
        """
        session = (
            self.db.query(LearningSession)
            .options(
                joinedload(LearningSession.units)
                .joinedload(SessionUnit.unit)
                .joinedload(LearningUnit.progress)
            )
            .filter(LearningSession.id == session_id)
            .first()
        )
        if session:
            reasons = _SESSION_SELECTION_REASONS.get(session.id, {})
            now_snap = _utc_now_naive()
            for su in session.units:
                is_stuck = False
                if su.unit and su.unit.progress and su.unit.progress.recall_fail_streak >= 3:
                    is_stuck = True
                su.is_stuck = is_stuck
                su.theme_id, su.theme_name = self._get_theme_metadata_for_unit(su.unit)
                reason = reasons.get(su.unit_id)
                if reason is None:
                    reason = getattr(su, "selection_reason", None)
                if reason is None and su.unit is not None:
                    reason = self._infer_selection_reason(
                        su.unit,
                        su.unit.progress,
                        now_snap,
                    )
                su.selection_reason = reason
        return session

    def _mark_session_active(self, session: LearningSession) -> None:
        if session.status == SessionLifecycleStatus.CREATED:
            session.status = SessionLifecycleStatus.ACTIVE

    def _mark_session_completed(self, session: LearningSession) -> None:
        session.status = SessionLifecycleStatus.COMPLETED
        session.completed = True
        session.completed_at = _utc_now_naive()
        session.abandoned_at = None

    def _mark_session_abandoned(self, session: LearningSession) -> None:
        session.status = SessionLifecycleStatus.ABANDONED
        session.completed = False
        session.completed_at = None
        if session.abandoned_at is None:
            session.abandoned_at = _utc_now_naive()

    def abandon_session(self, session_id: int) -> LearningSession:
        """Explicitly abandon a single CREATED/ACTIVE session (user-triggered).

        Does NOT touch any other sessions.  Only marks the given session as
        abandoned and commits.  Raises ValueError on bad state so the caller
        can map it to the appropriate HTTP status.
        """
        session = (
            self.db.query(LearningSession)
            .filter(LearningSession.id == session_id)
            .first()
        )
        if not session:
            raise ValueError(f"Session {session_id} not found")
        if session.status not in ACTIVE_SESSION_STATUSES:
            raise ValueError(
                f"Session {session_id} cannot be abandoned — "
                f"current status: {session.status.value}"
            )
        self._mark_session_abandoned(session)
        self.db.commit()
        return session

    def close_incomplete_sessions(self) -> int:
        """Mark any non-terminal sessions as abandoned and return the count."""
        orphans = (
            self.db.query(LearningSession)
            .filter(LearningSession.status.in_(ACTIVE_SESSION_STATUSES))
            .all()
        )
        if not orphans:
            return 0

        for orphan in orphans:
            self._mark_session_abandoned(orphan)
        self.db.commit()
        return len(orphans)

    def get_recall_availability(
        self,
        source_pdfs: Optional[list[str]] = None,
    ) -> dict:
        """Return Recall mode availability for the provided source filters."""
        query = (
            self.db.query(func.count(LearningUnit.id))
            .join(LearningProgress)
            .filter(LearningProgress.introduced_at.isnot(None))
        )

        if source_pdfs:
            query = query.filter(LearningUnit.source_pdf.in_(source_pdfs))

        introduced_count = query.scalar() or 0
        is_available = introduced_count >= SESSION_SIZE

        return {
            "available": is_available,
            "introduced_count": introduced_count,
            "required_count": SESSION_SIZE,
            "message": (
                f"Recall is unlocked after studying words in Passive mode. "
                f"Need {SESSION_SIZE} introduced words, currently have {introduced_count}."
                if not is_available
                else f"Recall modes available ({introduced_count} introduced words)."
            ),
        }

    def create_session_from_request(self, request) -> LearningSession | dict:
        """Apply router-level session preflight logic and create the session."""
        from app.services.audio import ElevenLabsTTSService
        from app.services.daily_stats import get_daily_dashboard_stats

        closed = self.close_incomplete_sessions()
        if closed:
            logger.info(
                "create_session_from_request: marked %s incomplete session(s) abandoned; "
                "this request still creates a new LearningSession row (no in-place reuse).",
                closed,
            )

        logger.info(
            "create_session_from_request inbound: new_words_focus=%s follow_up_session_id=%s "
            "weak_only=%s due_only=%s lesson_id=%s theme=%s mode=%s "
            "client_page_instance_id=%s client_post_seq=%s client_debug_tag=%s",
            request.new_words_focus,
            request.follow_up_session_id,
            request.weak_only,
            request.due_only,
            request.lesson_id,
            request.theme,
            request.mode,
            request.client_page_instance_id,
            request.client_post_seq,
            request.client_debug_tag,
        )

        mode = StudyModeType(request.mode.value)
        source_pdfs = None
        if request.source_pdfs:
            source_pdfs = [
                unicodedata.normalize("NFC", source_pdf)
                for source_pdf in request.source_pdfs
            ]

        if mode == StudyModeType.PASSIVE and not request.override_cap:
            daily_stats = get_daily_dashboard_stats(self.db)
            if daily_stats["cap_exceeded"]:
                logger.info(
                    "create_session early return: reason=passive_intro_cap_exceeded "
                    "words_introduced_today=%s max_new_per_day=%s",
                    daily_stats["words_introduced_today"],
                    daily_stats["max_new_per_day"],
                )
                return {
                    "session_id": None,
                    "cap_warning": True,
                    "message": (
                        f"You've introduced "
                        f"{daily_stats['words_introduced_today']} words today "
                        f"(limit {daily_stats['max_new_per_day']}). "
                        "Continue anyway?"
                    ),
                }

        if mode == StudyModeType.RECALL_AUDIO:
            if not ElevenLabsTTSService().is_enabled():
                raise ValueError(
                    "recall_audio mode requires ElevenLabs TTS to be enabled and configured. "
                    "Please configure ElevenLabs in settings."
                )

        curriculum_mode = request.curriculum_mode
        if request.follow_up_session_id is not None:
            curriculum_mode = None

        return self.create_session(
            mode=mode,
            source_pdfs=source_pdfs,
            theme=request.theme,
            lesson_id=request.lesson_id,
            weak_only=request.weak_only,
            due_only=request.due_only,
            override_daily_cap=request.override_daily_cap,
            follow_up_session_id=request.follow_up_session_id,
            retry_failed_only=request.retry_failed_only,
            new_words_focus=request.new_words_focus,
            curriculum_mode=curriculum_mode,
        )

    def _get_session_status(self, session: LearningSession) -> str:
        if session.status is not None:
            return session.status.value
        if session.completed:
            return SessionLifecycleStatus.COMPLETED.value
        if session.answered_units > 0:
            return SessionLifecycleStatus.ACTIVE.value
        return SessionLifecycleStatus.CREATED.value

    def _get_session_score_counts(self, session: LearningSession) -> tuple[int, int, int, int]:
        if session.status == SessionLifecycleStatus.COMPLETED and session.summary_total_units is not None:
            return (
                session.summary_total_units,
                session.summary_correct_count or 0,
                session.summary_partial_count or 0,
                session.summary_failed_count or 0,
            )
        return (
            session.total_units,
            session.correct_count,
            session.partial_count,
            session.failed_count,
        )

    def _serialize_history_session(
        self,
        session: LearningSession,
        vocabulary_map: Optional[dict[int, list[dict]]] = None,
    ) -> dict:
        total_units, correct_count, partial_count, failed_count = self._get_session_score_counts(session)
        session_date = session.started_at
        if session.status == SessionLifecycleStatus.COMPLETED and session.completed_at is not None:
            session_date = session.completed_at
        elif session.status == SessionLifecycleStatus.ABANDONED and session.abandoned_at is not None:
            session_date = session.abandoned_at
        return {
            "session_id": session.id,
            "date": session_date,
            "mode": session.mode.value,
            "total_units": total_units,
            "correct_count": correct_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
            "status": self._get_session_status(session),
            "vocabularies": (
                vocabulary_map.get(session.id, [])
                if vocabulary_map is not None
                else self.get_session_vocabularies(session.id)
            ),
        }

    def get_session_history_response_data(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Return history payload data for the API router."""
        from app.schemas.session import SessionHistoryItem, SessionStatus

        sessions, total = self.get_session_history(
            limit=limit,
            offset=offset,
            include_active=False,
        )
        vocabulary_map = self.get_session_vocabularies_map([session.id for session in sessions])
        history_items = [
            SessionHistoryItem(
                **{
                    **item,
                    "status": SessionStatus(item["status"]),
                }
            )
            for item in (
                self._serialize_history_session(session, vocabulary_map=vocabulary_map)
                for session in sessions
            )
        ]

        return {
            "sessions": history_items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": self.get_history_summary(),
        }

    def get_history_page_data(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Return template data for the session history page."""
        sessions, total = self.get_session_history(
            limit=limit,
            offset=offset,
            include_active=False,
        )
        vocabulary_map = self.get_session_vocabularies_map([session.id for session in sessions])
        return {
            "sessions": [
                self._serialize_history_session(session, vocabulary_map=vocabulary_map)
                for session in sessions
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": self.get_history_summary(),
        }

    def get_history_detail_page_data(self, session_id: int) -> Optional[dict]:
        """Return template data for a single history detail page."""
        session = self.get_session(session_id)
        if not session:
            return None

        total_units, correct_count, partial_count, failed_count = self._get_session_score_counts(session)
        units_data = []
        for session_unit in session.units:
            if not session_unit.answered:
                result = "unanswered"
                result_class = ""
            elif session_unit.recall_result:
                result = session_unit.recall_result.value
                result_class = result
            elif session_unit.is_correct:
                result = "correct"
                result_class = "correct"
            else:
                result = "failed"
                result_class = "failed"

            units_data.append(
                {
                    "position": session_unit.position,
                    "text": session_unit.unit.text,
                    "translation": session_unit.unit.translation,
                    "answered": session_unit.answered,
                    "is_correct": session_unit.is_correct,
                    "user_input": session_unit.user_input,
                    "result": result,
                    "result_class": result_class,
                }
            )

        return {
            "id": session.id,
            "date": (
                session.completed_at
                if session.status == SessionLifecycleStatus.COMPLETED and session.completed_at is not None
                else session.abandoned_at
                if session.status == SessionLifecycleStatus.ABANDONED and session.abandoned_at is not None
                else session.started_at
            ),
            "mode": session.mode.value,
            "status": self._get_session_status(session),
            "total_units": total_units,
            "correct_count": correct_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
            "units": units_data,
        }

    def get_study_page_data(
        self,
        weak_only: bool = False,
        due_only: bool = False,
        *,
        curriculum_mode: str | None = None,
        new_words_focus: bool = False,
    ) -> dict:
        """Return template data for the study page."""
        from app.services.audio import ElevenLabsTTSService
        from app.services.daily_stats import (
            DUE_RECALL_URGENT_THRESHOLD,
            compute_daily_goal_targets,
        )
        from app.services.learning_snapshot import get_learning_snapshot
        from app.services.lesson_service import detect_current_plua_lesson, get_plua_lesson_progress

        current_lesson = _detect_current_lesson(self.db)
        plua_current_lesson = detect_current_plua_lesson(self.db)
        plua_lesson_progress = get_plua_lesson_progress(self.db, plua_current_lesson)
        spillover_start = max(1, current_lesson - SPILLOVER_DEPTH)
        force_new_session = weak_only or due_only

        active_session = None
        if not force_new_session:
            active_session = (
                self.db.query(LearningSession)
                .filter(LearningSession.status.in_(ACTIVE_SESSION_STATUSES))
                .order_by(LearningSession.created_at.desc())
                .first()
            )
            # Lesson / new-words entry URLs force passive-only UI. Resuming an in-flight recall
            # session would inject recall mode while the client locks radios to passive → wrong
            # /answer payloads (400).
            passive_only_entry = curriculum_mode == "lesson" or new_words_focus
            if (
                active_session
                and passive_only_entry
                and is_recall_like_study_mode(active_session.mode)
            ):
                active_session = None

        session_data = None
        if active_session:
            annotated_session = self.get_session(active_session.id)
            has_audio_enabled = ElevenLabsTTSService().is_enabled()
            session_mode_value = annotated_session.mode.value

            units_data = []
            for session_unit in sorted(annotated_session.units, key=lambda unit: unit.position):
                unit = session_unit.unit
                if not unit:
                    continue

                progress_data = None
                if unit.progress:
                    progress_data = {
                        "times_seen": unit.progress.times_seen,
                        "times_correct": unit.progress.times_correct,
                        "times_failed": unit.progress.times_failed,
                        "confidence_score": unit.progress.confidence_score,
                        "last_seen": unit.progress.last_seen.isoformat() if unit.progress.last_seen else None,
                        "next_review_at": unit.progress.next_review_at.isoformat() if unit.progress.next_review_at else None,
                        "introduced_at": (
                            unit.progress.introduced_at.isoformat()
                            if unit.progress.introduced_at
                            else None
                        ),
                    }

                units_data.append(
                    {
                        "id": session_unit.id,
                        "position": session_unit.position,
                        "answered": session_unit.answered,
                        "is_correct": session_unit.is_correct,
                        "user_input": session_unit.user_input,
                        "is_stuck": getattr(session_unit, "is_stuck", False),
                        "study_mode": session_mode_value,
                        "has_audio": has_audio_enabled,
                        "exercise_type": getattr(
                            session_unit, "exercise_type", None
                        )
                        or "recall",
                        "cloze_prompt": getattr(session_unit, "cloze_prompt", None),
                        "context_sentence_translation": getattr(
                            session_unit, "context_sentence_translation", None
                        ),
                        "unit": {
                            "id": unit.id,
                            "text": unit.text,
                            "translation": unit.translation,
                            "type": unit.type.value,
                            "part_of_speech": unit.part_of_speech,
                            "source_pdf": unit.source_pdf,
                            "progress": progress_data,
                        },
                    }
                )

            answered = sum(1 for unit in units_data if unit["answered"])
            correct = sum(1 for unit in units_data if unit["is_correct"])
            initial_prompt = ""
            for unit in units_data:
                if unit["answered"]:
                    continue
                if session_mode_value == "recall_audio":
                    initial_prompt = "Listen to the audio, then type the Polish word/phrase"
                elif session_mode_value == "recall":
                    initial_prompt = unit["unit"]["translation"]
                elif session_mode_value == "cloze":
                    if (
                        unit.get("exercise_type") == "cloze"
                        and unit.get("cloze_prompt")
                    ):
                        initial_prompt = unit["cloze_prompt"]
                    else:
                        initial_prompt = unit["unit"]["translation"]
                else:
                    initial_prompt = unit["unit"]["text"]
                break

            session_data = {
                "id": annotated_session.id,
                "mode": annotated_session.mode,
                "total_units": len(units_data),
                "answered_units": answered,
                "correct_count": correct,
                "units": units_data,
                "initial_prompt": initial_prompt,
            }

        _snap = get_learning_snapshot(self.db)
        due_words_count = _snap["due_words_count"]
        weak_words_count = _snap["weak_words_count"]
        due_recall_urgent_threshold = DUE_RECALL_URGENT_THRESHOLD
        _goals = compute_daily_goal_targets(self.db)

        return {
            "session": session_data,
            "current_lesson": current_lesson,
            "spillover_start": spillover_start,
            "session_size": settings.session_size,
            "due_words_count": due_words_count,
            "weak_words_count": weak_words_count,
            "due_recall_urgent_threshold": due_recall_urgent_threshold,
            "plua_current_lesson": plua_current_lesson,
            "plua_lesson_progress": plua_lesson_progress,
            "daily_goal_sessions": _goals["sessions"],
            "daily_goal_lessons": _goals["lessons"],
        }

    def _build_answer_response(
        self,
        session: LearningSession,
        session_unit: SessionUnit,
        unit_position: int,
        evaluation: Optional["AnswerEvaluation"] = None,
    ):
        from app.schemas.session import AnswerResponse, EvaluationMode as ApiEvaluationMode, RecallResultType

        response = AnswerResponse(
            session_id=session.id,
            unit_position=unit_position,
            is_correct=session_unit.is_correct,
            session_completed=session.status == SessionLifecycleStatus.COMPLETED,
            correct_count=session.correct_count,
            answered_count=session.answered_units,
            total_units=session.total_units,
            message=(
                "Session completed!" if session.status == SessionLifecycleStatus.COMPLETED
                else f"Answered {session.answered_units}/{session.total_units}"
            ),
        )

        if evaluation:
            response.user_input = evaluation.user_input
            response.expected_answer = evaluation.expected_answer
            response.evaluation_mode = ApiEvaluationMode(evaluation.evaluation_mode.value)
            response.punctuation_only_mistake = evaluation.punctuation_only_mistake
            response.recall_result = RecallResultType(evaluation.result.value)
        elif session_unit.user_input is not None:
            response.user_input = session_unit.user_input
            response.expected_answer = session_unit.unit.text
            if session_unit.recall_result is not None:
                response.recall_result = RecallResultType(session_unit.recall_result.value)

        return response

    def _get_idempotent_answer_response(
        self,
        session_id: int,
        unit_position: int,
    ):
        session = self.get_session(session_id)
        if not session:
            raise LookupError(f"Session {session_id} not found.")

        session_unit = (
            self.db.query(SessionUnit)
            .options(joinedload(SessionUnit.unit))
            .filter(
                SessionUnit.session_id == session_id,
                SessionUnit.position == unit_position,
            )
            .first()
        )
        if not session_unit:
            raise LookupError(
                f"Unit at position {unit_position} not found in session {session_id}."
            )

        return self._build_answer_response(session, session_unit, unit_position)

    def submit_answer_and_build_response(self, session_id: int, answer):
        """Submit an answer and return the API response payload."""
        session = self.get_session(session_id)
        if not session:
            raise LookupError(f"Session {session_id} not found.")

        try:
            session_unit, evaluation = self.submit_answer(
                session_id=session_id,
                unit_position=answer.unit_position,
                is_correct=answer.is_correct,
                user_input=answer.user_input,
            )
        except ValueError as exc:
            error_msg = str(exc)
            if "already answered" in error_msg:
                return self._get_idempotent_answer_response(session_id, answer.unit_position)
            raise

        updated_session = self.get_session(session_id)
        return self._build_answer_response(updated_session, session_unit, answer.unit_position, evaluation)

    def _passive_session_should_offer_recall_follow_up(self, session: LearningSession) -> bool:
        """
        Passive→recall follow-up for learning flows, weak-only passive, and lesson passive.

        Skips due-only passive (every slot is a scheduled due review).

        Relies on selection labels from cache, SessionUnit.selection_reason, or inference in
        get_session(), plus passive_recall_chain / _SESSION_PASSIVE_RECALL_AFTER for weak-only
        and curriculum lesson passive sessions (not due-only).
        """
        if getattr(session, "due_only", False):
            return False
        if session.mode != StudyModeType.PASSIVE:
            return False
        units = session.units
        if not units:
            return False
        cached = _SESSION_SELECTION_REASONS.get(session.id, {})
        labels: list[Optional[str]] = []
        for su in units:
            lab = cached.get(su.unit_id) if cached else None
            if lab is None:
                lab = getattr(su, "selection_reason", None)
            labels.append(lab)
        if any(x is None for x in labels):
            return False
        # Due-only passive: every slot is a due review
        if all(l == "due" for l in labels):
            return False
        if self._resolve_passive_recall_chain(session):
            return True
        if any(l == "new" for l in labels):
            return True
        # PL–UA lesson-style sessions often select only already-seen spine words (all "review")
        if all(l == "review" for l in labels):
            return True
        # Mixed review without new introductions (e.g. due + review) — no forced recall chain
        return False

    def _count_weak_units_global_for_next_recommendation(self) -> int:
        """Same as dashboard weak_words_count / is_weak (introduced, confidence < WEAK_THRESHOLD)."""
        return count_weak(self.db.query(LearningProgress).all())

    def get_next_recommendation(self, session_id: int) -> dict:
        """Return the next-study recommendation for a completed session."""
        from app.services.daily_stats import (
            DUE_RECALL_URGENT_THRESHOLD,
            get_daily_dashboard_stats,
        )

        session = self.get_session(session_id)
        if not session:
            raise LookupError(f"Session {session_id} not found.")
        chain = self._resolve_passive_recall_chain(session)
        session.weak_only = chain == "weak"
        if session.status != SessionLifecycleStatus.COMPLETED:
            logger.info(
                "next_recommendation: session %s not terminal yet (status=%s answered=%s/%s)",
                session_id,
                session.status.value if session.status else None,
                session.answered_units,
                session.total_units,
            )
            return {
                "status": "pending",
                "message": "Practice isn't finished yet.",
            }

        answered = session.summary_answered_units or session.answered_units or 0
        correct = session.summary_correct_count or session.correct_count or 0
        accuracy = (correct / answered) if answered > 0 else 0.0

        daily_stats = get_daily_dashboard_stats(self.db)
        overdue_count = daily_stats["overdue_word_count"]
        cap_exceeded = daily_stats["cap_exceeded"]
        minimum_due_count = int(SESSION_SIZE * MINIMUM_DUE_RATIO)

        recommendation: dict = {"accuracy": round(accuracy, 2)}

        if is_recall_like_study_mode(session.mode):
            try:
                failed_ids = self._get_failed_unit_ids_from_session(session.id)
            except Exception as e:
                logger.warning(
                    "Failed to load failed units for session %s: %s",
                    session.id,
                    e,
                )
                failed_ids = []

            if len(failed_ids) >= RECALL_RETRY_FAILED_MIN:
                recommendation.update(
                    type="retry",
                    mode="recall",
                    message="Some words were difficult — let's review them again.",
                    cta_label="Review Difficult Words",
                    follow_up_session_id=session.id,
                    retry_failed_only=True,
                )
                return recommendation

        # Completed passive: recall follow-up for learning flows, weak/lesson passive;
        # not for due-only passive (SRS due review must not chain into recall).
        if (
            session.mode == StudyModeType.PASSIVE
            and not session.due_only
            and (
                session.weak_only
                or self._passive_session_should_offer_recall_follow_up(session)
            )
        ):
            if chain == "weak":
                recall_msg = (
                    "You practiced difficult words in Passive mode. "
                    "Lock them in with Recall."
                )
            elif chain == "lesson":
                recall_msg = (
                    "You studied this lesson in Passive mode. "
                    "Reinforce the same words with Recall."
                )
            else:
                recall_msg = "You introduced new words. Reinforce them with Recall."
            recommendation.update(
                type="recall",
                mode="recall",
                message=recall_msg,
                cta_label="Start recall practice",
                follow_up_session_id=session.id,
            )
            if session.weak_only:
                recommendation["weak_only"] = True
            return recommendation

        # After recall (or recall_audio), never chain another follow-up recall — that loops
        # completion → next-recommendation → createSession(recall+follow_up) forever.
        # Retry-failed-only recall is handled above.
        completed_recall = is_recall_like_study_mode(session.mode)

        if session.due_only:
            weak_count = self._count_weak_units_global_for_next_recommendation()
            if (
                overdue_count < DUE_RECALL_URGENT_THRESHOLD
                and weak_count >= WEAK_FOLLOW_UP_MIN
            ):
                recommendation.update(
                    type="weak",
                    mode="recall",
                    weak_only=True,
                    message=f"You have {weak_count} weak words to strengthen.",
                    cta_label="Practice Weak Words",
                )
            else:
                recommendation.update(
                    type="passive",
                    mode="passive",
                    message="You're doing well. Introduce some new words.",
                    cta_label="Start passive practice",
                )
            return recommendation

        if not session.due_only and overdue_count >= minimum_due_count:
            recommendation.update(
                type="due",
                mode="recall",
                due_only=True,
                message=f"You have {overdue_count} overdue words. Clear them first.",
                cta_label="Review Due Words",
            )
        elif completed_recall and accuracy < 0.6:
            if overdue_count > 0:
                recommendation.update(
                    type="due",
                    mode="recall",
                    due_only=True,
                    message="Recall accuracy was low — review overdue words to reinforce.",
                    cta_label="Review Due Words",
                )
            else:
                recommendation.update(
                    type="passive",
                    mode="passive",
                    message="Try passive review, then return to recall when ready.",
                    cta_label="Start passive practice",
                )
        elif not cap_exceeded:
            recommendation.update(
                type="passive",
                mode="passive",
                message="You're doing well. Introduce some new words.",
                cta_label="Start passive practice",
            )
        elif overdue_count > 0:
            recommendation.update(
                type="due",
                mode="recall",
                due_only=True,
                message=(
                    f"Daily new-word cap reached. You still have {overdue_count} overdue words."
                ),
                cta_label="Review Due Words",
            )
        else:
            recommendation.update(
                type="passive",
                mode="passive",
                message=(
                    "Daily new-word cap reached. Continue in passive review or come back tomorrow."
                ),
                cta_label="Start passive practice",
            )

        return recommendation
    
    def get_session_history(
        self,
        limit: int = 20,
        offset: int = 0,
        include_active: Optional[bool] = None,
    ) -> tuple[list[LearningSession], int]:
        """
        Get session history for display.
        
        Returns sessions ordered by completed_at DESC (completed sessions first),
        then by created_at DESC for incomplete sessions.
        
        Args:
            limit: Maximum number of sessions to return (default 20).
            offset: Number of sessions to skip (for pagination).
            include_active: If False, exclude active sessions. If omitted, preserve
                historical behavior and include all sessions.
        
        Returns:
            Tuple of (list of sessions, total count).
        """
        # Base query
        query = self.db.query(LearningSession).options(selectinload(LearningSession.units))
        
        if include_active is False:
            query = query.filter(
                or_(
                    LearningSession.status.in_(
                        [
                            SessionLifecycleStatus.COMPLETED,
                            SessionLifecycleStatus.ABANDONED,
                        ]
                    ),
                    (
                        LearningSession.status.is_(None)
                        & (
                            LearningSession.completed_at.isnot(None)
                            | LearningSession.abandoned_at.isnot(None)
                        )
                    ),
                )
            )
        
        # Count total before pagination
        total = query.count()
        
        # Order by completed_at DESC (nulls last), then by created_at DESC
        # Completed sessions first, then in-progress/abandoned by start date
        sessions = (
            query
            .order_by(
                LearningSession.completed_at.desc().nullslast(),
                LearningSession.abandoned_at.desc().nullslast(),
                LearningSession.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        
        return sessions, total

    def get_session_vocabularies_map(self, session_ids: list[int]) -> dict[int, list[dict]]:
        """Batch vocabulary counts for multiple sessions."""
        from app.schemas.session import VocabularyCount

        if not session_ids:
            return {}

        results = (
            self.db.query(
                SessionUnit.session_id,
                LearningUnit.source_pdf,
                func.count(SessionUnit.id).label("count"),
            )
            .join(LearningUnit, SessionUnit.unit_id == LearningUnit.id)
            .filter(SessionUnit.session_id.in_(session_ids))
            .group_by(SessionUnit.session_id, LearningUnit.source_pdf)
            .order_by(SessionUnit.session_id.asc(), func.count(SessionUnit.id).desc())
            .all()
        )

        vocabulary_map: dict[int, list[dict]] = defaultdict(list)
        for session_id, name, count in results:
            vocabulary_map[session_id].append(VocabularyCount(name=name, count=count))
        return dict(vocabulary_map)

    def get_session_vocabularies(self, session_id: int) -> list[dict]:
        """
        Get vocabularies studied in a session with unit counts.
        
        Args:
            session_id: ID of the session
            
        Returns:
            List of dicts with 'name' and 'count' keys
        """
        return self.get_session_vocabularies_map([session_id]).get(session_id, [])
    
    def get_history_summary(self) -> dict:
        """
        Calculate summary statistics for the history page.
        
        Returns:
            Dict with:
            - study_streak_days: Consecutive days with completed sessions
            - words_this_week: Total units answered this week
            - recall_accuracy_7d: Accuracy for recall mode in last 7 days (None if no recall sessions)
            - weak_words_count: Count of units with confidence <50%
        """
        from datetime import datetime, timedelta
        from app.schemas.session import HistorySummary
        
        now = _utc_now_naive()
        week_ago = now - timedelta(days=7)
        
        # 1. Calculate study streak (consecutive days with completed sessions)
        completed_sessions = (
            self.db.query(LearningSession.completed_at)
            .filter(LearningSession.status == SessionLifecycleStatus.COMPLETED)
            .filter(LearningSession.completed_at != None)
            .order_by(LearningSession.completed_at.desc())
            .all()
        )
        
        streak = compute_study_streak(completed_sessions, today=now)
        
        # 2. Words studied this week (total answered units)
        words_this_week = (
            self.db.query(func.count(SessionUnit.id))
            .join(LearningSession, SessionUnit.session_id == LearningSession.id)
            .filter(LearningSession.created_at >= week_ago)
            .filter(SessionUnit.answered == True)
            .scalar() or 0
        )
        
        # 3. Recall accuracy last 7 days (Recall mode only)
        recall_stats = (
            self.db.query(
                func.count(SessionUnit.id).label("total"),
                func.sum(
                    case(
                        (SessionUnit.is_correct == True, 1),
                        else_=0
                    )
                ).label("correct")
            )
            .join(LearningSession, SessionUnit.session_id == LearningSession.id)
            .filter(LearningSession.created_at >= week_ago)
            .filter(
                LearningSession.mode.in_(
                    [StudyModeType.RECALL, StudyModeType.RECALL_AUDIO, StudyModeType.CLOZE]
                )
            )
            .filter(SessionUnit.answered == True)
            .first()
        )
        
        recall_accuracy = None
        if recall_stats and recall_stats.total and recall_stats.total > 0:
            recall_accuracy = round((recall_stats.correct / recall_stats.total) * 100, 1)
        
        # 4. Weak words count (confidence <50%)
        weak_words = (
            self.db.query(func.count(LearningProgress.id))
            .filter(LearningProgress.introduced_at.isnot(None))
            .filter(LearningProgress.confidence_score < PROGRESS_WEAK_THRESHOLD)
            .scalar()
            or 0
        )
        
        return HistorySummary(
            study_streak_days=streak,
            words_this_week=words_this_week,
            recall_accuracy_7d=recall_accuracy,
            weak_words_count=weak_words,
        )
    
    def submit_answer(
        self,
        session_id: int,
        unit_position: int,
        is_correct: Optional[bool] = None,
        user_input: Optional[str] = None,
    ) -> tuple[SessionUnit, Optional[AnswerEvaluation]]:
        """
        Submit an answer for a unit in a session.
        
        Updates both the session unit and the learning progress.
        
        For passive mode: is_correct must be provided (user self-assessment).
        For recall mode: user_input must be provided (backend evaluates).
        
        Recall scoring (authoritative):
        - correct: +1.0 times_correct
        - partial: +0.5 times_correct
        - failed: +1 times_failed
        
        Args:
            session_id: Session ID.
            unit_position: Position of unit in session (1 to 50 by default).
            is_correct: Whether the answer was correct (passive mode).
            user_input: User's typed answer (recall mode).
            
        Returns:
            Tuple of (SessionUnit, AnswerEvaluation or None).
            
        Raises:
            ValueError: If session/unit not found, already answered, or invalid input.
        """
        # Get session with mode info
        session = (
            self.db.query(LearningSession)
            .filter(LearningSession.id == session_id)
            .first()
        )
        
        if not session:
            raise ValueError(f"Session {session_id} not found")
        self._mark_session_active(session)
        
        # Get session unit with learning unit
        session_unit = (
            self.db.query(SessionUnit)
            .options(joinedload(SessionUnit.unit))
            .filter(
                SessionUnit.session_id == session_id,
                SessionUnit.position == unit_position,
            )
            .first()
        )
        
        if not session_unit:
            raise ValueError(
                f"Unit at position {unit_position} not found in session {session_id}"
            )
        
        if session_unit.answered:
            raise ValueError(
                f"Unit at position {unit_position} already answered"
            )
        
        evaluation: Optional[AnswerEvaluation] = None
        recall_result: Optional[RecallResult] = None
        error_type = "unknown"
        
        # Handle based on session mode
        if is_recall_like_study_mode(session.mode):
            # Recall / cloze / audio recall: evaluate user input against expected answer
            if user_input is None:
                raise ValueError(
                    "user_input is required for recall and cloze mode sessions"
                )
            
            # Get expected answer (the source language text)
            expected = session_unit.unit.text

            if session_unit.exercise_type == "cloze":
                from app.services.cloze_service import get_cloze_answer
                context = session_unit.unit.context_sentence
                if context:
                    # Extract the actual inflected form used in the sentence.
                    # Pass cloze_prompt for positional matching (handles irregular inflection).
                    # Falls back to stem matching, then to unit.text if nothing found.
                    actual = get_cloze_answer(
                        context,
                        session_unit.unit.text,
                        cloze_prompt=session_unit.cloze_prompt,
                    )
                    if actual:
                        expected = actual
                elif len(expected.split()) > 1:
                    # No context_sentence: multi-word phrase fallback — blank was the last word.
                    expected = expected.split()[-1]

            # Evaluate answer
            evaluation = evaluate_answer(user_input, expected)
            is_correct = evaluation.is_correct
            recall_result = evaluation.result

            if recall_result == RecallResult.FAILED:
                allowed_vocab = {
                    normalize_input(su.unit.text, strip_punctuation=True).lower()
                    for su in session.units
                    if su.unit and su.unit.text
                }
                error_type = self._classify_error(user_input, expected, allowed_vocab)
            
            # Store user input
            session_unit.user_input = user_input
            
        else:
            # Passive mode: use provided is_correct
            if is_correct is None:
                raise ValueError(
                    "is_correct is required for passive mode sessions"
                )
            # No recall_result for passive mode (stays None)
        
        # Update session unit
        session_unit.answered = True
        session_unit.is_correct = is_correct
        session_unit.recall_result = recall_result  # Store for history (None for passive mode)
        session_unit.answered_at = _utc_now_naive()

        analytics_answer_index = unit_position if unit_position is not None else session_unit.position
        analytics_result = "correct" if is_correct else "incorrect"

        if recall_result == RecallResult.CORRECT:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
        
        # Update learning progress with recall result
        is_recall_mode = is_recall_like_study_mode(session.mode)
        progress_before = (
            self.db.query(LearningProgress)
            .filter(LearningProgress.unit_id == session_unit.unit_id)
            .first()
        )
        old_confidence = progress_before.confidence_score if progress_before else 0.0
        self._update_progress(
            session_unit.unit_id,
            is_correct,
            recall_result=recall_result,
            is_recall_mode=is_recall_mode,
            error_type=error_type,
        )
        progress_after = (
            self.db.query(LearningProgress)
            .filter(LearningProgress.unit_id == session_unit.unit_id)
            .first()
        )
        if progress_after:
            delta = progress_after.confidence_score - old_confidence
            gain = max(0.0, delta * (1 + self._effective_stability(progress_after.stability_score)))
            self._session_gain += gain
            self._answers_count += 1

        try:
            record_study_answer_event(
                self.db,
                session_id=session.id,
                unit_id=session_unit.unit_id,
                answer_index=analytics_answer_index,
                fallback_answer_index=session_unit.position,
                result=analytics_result,
                timestamp=session_unit.answered_at,
            )
        except Exception:
            logger.warning(
                "Study-answer analytics write failed for session_id=%s unit_id=%s answer_index=%s",
                session.id,
                session_unit.unit_id,
                analytics_answer_index,
                exc_info=True,
            )

        if self._consecutive_failures >= 3:
            remaining_units = (
                self.db.query(SessionUnit)
                .options(
                    joinedload(SessionUnit.unit).joinedload(LearningUnit.progress)
                )
                .filter(
                    SessionUnit.session_id == session_id,
                    SessionUnit.answered == False,
                )
                .order_by(SessionUnit.position.asc())
                .all()
            )
            if remaining_units:
                now = _utc_now_naive()

                def _difficulty(su: SessionUnit) -> float:
                    progress = su.unit.progress if su.unit else None
                    if not progress:
                        return 1.0
                    return self._compute_difficulty_score(progress, now)

                easiest = min(remaining_units, key=_difficulty)
                next_unit = remaining_units[0]
                if easiest.position != next_unit.position:
                    original_easy_position = easiest.position
                    original_next_position = next_unit.position
                    temp_position = 1000000 + original_easy_position
                    easiest.position = temp_position
                    self.db.flush()
                    next_unit.position = original_easy_position
                    easiest.position = original_next_position
                self._consecutive_failures = 0

        # Check if session is complete
        session = self.get_session(session_id)
        if session and session.is_complete:
            self._mark_session_completed(session)
            if self._answers_count > 0:
                avg_gain = self._session_gain / self._answers_count
            else:
                avg_gain = 0
            self._gain_history.append(avg_gain)
            smoothed_gain = sum(self._gain_history) / len(self._gain_history)
            gain_error = self._target_gain_per_answer - smoothed_gain
            self._difficulty_bias += gain_error * 0.2
            self._difficulty_bias = max(-0.15, min(0.15, self._difficulty_bias))
            self._spacing_aggressiveness += -gain_error * 0.05
            self._spacing_aggressiveness = max(0.9, min(1.1, self._spacing_aggressiveness))
            self._session_gain = 0.0
            self._answers_count = 0
            # Persist summary metrics for session history
            self._persist_session_summary(session)
        
        self.db.commit()
        self.db.refresh(session_unit)
        
        return session_unit, evaluation
    
    def _persist_session_summary(self, session: LearningSession) -> None:
        """
        Persist session summary metrics on completion.
        
        These values are stored (not computed) to preserve historical accuracy.
        Called when session.is_complete becomes True.
        
        Args:
            session: The completed learning session.
        """
        session.summary_total_units = session.total_units
        session.summary_answered_units = session.answered_units
        session.summary_correct_count = session.correct_count
        session.summary_partial_count = session.partial_count
        session.summary_failed_count = session.failed_count
        
        logger.debug(
            f"Persisted summary for session {session.id}: "
            f"total={session.summary_total_units}, "
            f"correct={session.summary_correct_count}, "
            f"partial={session.summary_partial_count}, "
            f"failed={session.summary_failed_count}"
        )
    
    def _update_progress(
        self,
        unit_id: int,
        is_correct: bool,
        recall_result: Optional[RecallResult] = None,
        is_recall_mode: bool = False,
        error_type: str = "unknown",
    ) -> None:
        """
        Update learning progress for a unit.
        
        Creates progress record if it doesn't exist.
        Recalculates confidence score using stability-aware exponential smoothing.
        
        Recall mode scoring (AUTHORITATIVE):
        - CORRECT: raw_score=1.0, stability += 0.02, reset fail streak
        - PARTIAL: raw_score=PARTIAL_RAW_SCORE (0.5), stability += STABILITY_INCREMENT_PARTIAL;
          does not modify fail streak, counters, or blocked state
        - FAILED: raw_score=0.0, stability -= 0.01, increment fail streak,
                  all penalties scaled by (1 - stability_score)
        
        Passive mode scoring (WEAKER EVIDENCE):
        - correct: raw_score=0.3 (weaker signal)
        - wrong: raw_score=0.0
        - CRITICAL: If last_recall_result == failed, passive success
          MUST NOT increase confidence (recall failure is authoritative)
        - stability_score is NEVER modified by passive mode
        
        Confidence smoothing formula (stability-aware):
          alpha = 0.3 + (0.3 * stability_score)      →  [0.3, 0.6]
          new_confidence = old * (1 - alpha) + raw * alpha
        
        INVARIANTS:
        - stability_score represents long-term memory maturity
        - stability_score is modified ONLY by recall outcomes
        - confidence_score remains the short-term, decaying retrievability signal
        - time decay NEVER modifies stability_score
        
        Args:
            unit_id: Learning unit ID.
            is_correct: Whether the answer was correct.
            recall_result: RecallResult for recall mode (None for passive).
            is_recall_mode: Whether this is recall mode.
        """
        progress = (
            self.db.query(LearningProgress)
            .filter(LearningProgress.unit_id == unit_id)
            .first()
        )
        
        if not progress:
            progress = LearningProgress(
                unit_id=unit_id,
                times_seen=0,
                times_correct=0,
                times_failed=0,
                confidence_score=0.0,
                stability_score=0.0,
                recall_fail_streak=0,
                is_blocked=False,
            )
            self.db.add(progress)
            init_fsrs_fields(progress)  # give new words default FSRS state (S=0.5, D=5.0)

        # Capture last_seen BEFORE update — used by FSRS scheduler for accurate retrievability
        _prev_last_seen = progress.last_seen

        # Always increment times_seen
        progress.times_seen += 1
        progress.last_seen = _utc_now_naive()
        
        # Store previous confidence for passive mode blocking check and smoothing
        previous_confidence = progress.confidence_score
        
        if is_recall_mode and recall_result is not None:
            # Active Recall mode - AUTHORITATIVE scoring
            # Store the recall result (this is the source of truth)
            progress.last_recall_result = recall_result
            
            if recall_result == RecallResult.CORRECT:
                progress.times_correct += 1
                # Reset fail streak on correct answer
                progress.recall_fail_streak = 0
                # Unblock word on successful recall (allows it back into normal sessions)
                if progress.is_blocked:
                    progress.is_blocked = False
                    logger.info(f"Unit {unit_id} unblocked: successful recall")
                # Update stability: reward correct recall
                progress.stability_score = min(
                    1.0,
                    progress.stability_score + STABILITY_INCREMENT_CORRECT
                )
                # Apply stability-aware confidence smoothing with raw_score = 1.0
                progress.confidence_score = apply_confidence_smoothing(
                    previous_confidence, 1.0, self._effective_stability(progress.stability_score)
                )
                
            elif recall_result == RecallResult.PARTIAL:
                progress.stability_score = min(
                    1.0,
                    progress.stability_score + STABILITY_INCREMENT_PARTIAL,
                )
                progress.confidence_score = apply_confidence_smoothing(
                    previous_confidence,
                    PARTIAL_RAW_SCORE,
                    self._effective_stability(progress.stability_score),
                )

            else:  # FAILED
                progress.times_failed += 1
                # Increment fail streak
                progress.recall_fail_streak += 1

                if error_type == "near_miss":
                    penalty_factor = 0.5
                    stability_decrement = 0.0
                elif error_type == "semantic_confusion":
                    penalty_factor = 0.75
                    stability_decrement = STABILITY_DECREMENT_FAILED * 0.5
                else:
                    penalty_factor = 1.0
                    stability_decrement = STABILITY_DECREMENT_FAILED
                
                # Check if word should be marked as blocked (persistent difficulty)
                # Blocked words are excluded from normal sessions, only appear in weak-only practice
                if progress.recall_fail_streak >= FAIL_STREAK_BLOCKED_THRESHOLD:
                    progress.is_blocked = True
                    logger.info(
                        f"Unit {unit_id} marked as blocked: fail_streak={progress.recall_fail_streak}"
                    )
                
                # Update stability: penalize failed recall
                progress.stability_score = max(
                    0.0, progress.stability_score - stability_decrement
                )
                # Apply stability-aware confidence smoothing with raw_score = 0.0
                smoothed = apply_confidence_smoothing(
                    previous_confidence, 0.0, progress.stability_score
                )
                
                # Apply fail-streak penalty for consecutive failures, scaled by
                # (1 - stability_score). Mature words take a smaller hit because
                # a single lapse doesn't erase deep memory; new/unstable words
                # are penalized strongly to keep them in active rotation.
                stability_factor = 1.0 - progress.stability_score
                extra_penalty = max(0, progress.recall_fail_streak - 2) * FAIL_STREAK_CONFIDENCE_FLOOR_REDUCTION
                scaled_penalty = extra_penalty * stability_factor * penalty_factor
                if scaled_penalty > 0:
                    smoothed = max(0.0, smoothed - scaled_penalty)
                    logger.debug(
                        f"Applied streak penalty for unit {unit_id}: "
                        f"streak={progress.recall_fail_streak}, "
                        f"base_penalty={extra_penalty:.3f}, "
                        f"stability={progress.stability_score:.3f}, "
                        f"scaled_penalty={scaled_penalty:.3f}"
                    )
                
                progress.confidence_score = smoothed
                
        else:
            # Passive mode - WEAKER evidence
            # Introduce only on first correct answer (idempotent - only set if NULL)
            if is_correct and progress.introduced_at is None:
                progress.introduced_at = _utc_now_naive()
            
            # Check if last recall was a failure - if so, passive success cannot boost confidence
            recall_failure_blocks_boost = (
                progress.last_recall_result == RecallResult.FAILED
            )
            
            if is_correct:
                if recall_failure_blocks_boost:
                    # CRITICAL: Passive success MUST NOT override recall failure
                    # User got it right passively, but they failed recall - no confidence boost
                    # Don't increment times_correct, don't increase confidence
                    logger.debug(
                        f"Passive success blocked for unit {unit_id}: "
                        f"last_recall_result=failed"
                    )
                    # Keep confidence unchanged
                    progress.confidence_score = previous_confidence
                else:
                    # Normal passive success: apply smoothing with weaker signal (0.3)
                    progress.times_correct += 1
                    # Passive correct must never reduce confidence.
                    # Recognition reinforces memory but should not penalize mature words.
                    # We therefore clamp confidence to at least the previous value.
                    smoothed = apply_confidence_smoothing(
                        previous_confidence, 0.3, self._effective_stability(progress.stability_score)
                    )
                    progress.confidence_score = max(previous_confidence, smoothed)
            else:
                progress.times_failed += 1
                # Passive failure: apply smoothing with raw_score = 0.0
                progress.confidence_score = apply_confidence_smoothing(
                    previous_confidence, 0.0
                )
        
        # ===================
        # Scheduling: FSRS (recall) / SRS-lite (passive)
        # ===================
        now = progress.last_seen  # naive UTC, just set above

        if is_recall_mode and recall_result is not None and progress.fsrs_stability is not None:
            # Phase 3: FSRS scheduling for active recall
            fsrs_rating = recall_result_to_fsrs_rating(recall_result, progress.confidence_score)
            next_dt = apply_fsrs_scheduling(
                progress,
                fsrs_rating,
                prev_last_seen=_prev_last_seen,
                now=now,
            )
        else:
            # SRS-lite scheduling for passive mode (or words missing FSRS state)
            next_dt = compute_next_review(
                is_correct=is_correct,
                times_seen=progress.times_seen,
                confidence_score=progress.confidence_score,
                last_seen=progress.last_seen,
                is_recall_mode=is_recall_mode,
                recall_result=recall_result,
                last_recall_result=progress.last_recall_result,
                previous_next_review_at=progress.next_review_at,
            )
            interval_days = (next_dt - now).total_seconds() / 86400.0
            if interval_days >= 0.5:
                interval_days *= self._spacing_aggressiveness
                interval_days = max(0.5, min(21.0, interval_days))
                stability_cap_days = 7.0 + (self._effective_stability(progress.stability_score) * 14.0)
                interval_days = min(interval_days, stability_cap_days)
                next_dt = now + timedelta(days=interval_days)

        if settings.smooth_due_load:
            next_dt = apply_due_load_cap(self.db, next_dt, settings.max_due_per_day)
        progress.next_review_at = next_dt

        self.db.flush()
