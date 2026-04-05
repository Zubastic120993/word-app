from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.analytics_service import (
    get_study_answer_metrics_since,
    get_study_calendar_week_activity,
    get_theme_summary,
)
from app.services.daily_stats import get_daily_dashboard_stats
from app.services.progress_service import get_per_source_due_weak_counts
from app.utils.time import utc_now

router = APIRouter(prefix="/analytics", tags=["analytics"])
api_router = APIRouter(prefix="/api/analytics", tags=["analytics"])
templates = Jinja2Templates(directory="app/templates")

METRIC_EPSILON = 0.02
COMPOSITE_EPSILON = 0.01
MIN_TREND_ATTEMPTS = 5
CONFIDENCE_ATTEMPT_CAP = 20
WOW_VOLUME_FLAT_PCT = 5.0
WOW_ACCURACY_FLAT_PP = 2.0
WOW_VOLUME_PCT_CLAMP = 200.0
WOW_SMALL_PRIOR_VOLUME = 5
WOW_ACCURACY_MIN_SAMPLES = 5
INSIGHT_MIN_WEAK_FOR_SOURCE = 2


def _confidence_weight(total_attempts: int) -> float:
    if total_attempts <= 0:
        return 0.0
    return min(1.0, total_attempts / CONFIDENCE_ATTEMPT_CAP)


def _composite_score(
    first_try_success_rate: float,
    reveal_rate: float,
    retry_rate: float,
    total_attempts: int,
) -> tuple[float, float, float]:
    friction = (
        (0.45 * (1 - first_try_success_rate))
        + (0.35 * reveal_rate)
        + (0.20 * retry_rate)
    )
    weight = _confidence_weight(total_attempts)
    return friction, weight, friction * weight


def _metric_trend(delta: float, better_is_lower: bool) -> str:
    if better_is_lower:
        if delta <= -METRIC_EPSILON:
            return "improving"
        if delta >= METRIC_EPSILON:
            return "declining"
        return "stable"

    if delta >= METRIC_EPSILON:
        return "improving"
    if delta <= -METRIC_EPSILON:
        return "declining"
    return "stable"


def _composite_trend(score_7d: float, score_30d: float) -> str:
    delta = score_7d - score_30d
    if delta <= -COMPOSITE_EPSILON:
        return "improving"
    if delta >= COMPOSITE_EPSILON:
        return "declining"
    return "stable"


def _study_cutoff_from_anchor(anchor_naive: datetime, days: int) -> datetime:
    """Naive UTC instant ``anchor_naive`` minus ``days`` (used for aligned study windows)."""
    return anchor_naive - timedelta(days=days)


def _compute_study_wow(study_last_7d: dict, study_prev_7d: dict) -> dict:
    """Week-over-week hints: last 7 days vs the prior 7 days (volume + accuracy)."""
    cur = int(study_last_7d["total_answers"])
    prev = int(study_prev_7d["total_answers"])
    acc_cur = float(study_last_7d["success_rate"])
    acc_prev = float(study_prev_7d["success_rate"])
    pct_cur = round(acc_cur * 100)
    pct_prev = round(acc_prev * 100)

    out: dict = {
        "show_volume_line": False,
        "volume_mode": None,
        "volume_pct": None,
        "volume_delta_abs": None,
        "volume_arrow": None,
        "volume_note": None,
        "volume_title": "",
        "show_accuracy_line": False,
        "accuracy_pp": None,
        "accuracy_arrow": None,
        "accuracy_title": "",
        "accuracy_insufficient": False,
    }
    if cur == 0 and prev == 0:
        return out

    base_vol_title = (
        f"Last 7 calendar days (UTC): {cur} answers · Previous 7 calendar days: {prev} answers."
    )

    out["show_volume_line"] = True
    if prev == 0:
        out["volume_mode"] = "new"
        out["volume_arrow"] = "new"
        out["volume_note"] = "More answers than the week before last."
        out["volume_title"] = base_vol_title
    elif prev < WOW_SMALL_PRIOR_VOLUME:
        out["volume_mode"] = "absolute"
        delta_abs = cur - prev
        out["volume_delta_abs"] = delta_abs
        if delta_abs > 0:
            out["volume_arrow"] = "up"
        elif delta_abs < 0:
            out["volume_arrow"] = "down"
        else:
            out["volume_arrow"] = "flat"
        out["volume_title"] = base_vol_title + f" ({delta_abs:+d} vs prior week)."
    else:
        out["volume_mode"] = "percent"
        raw_pct = (cur - prev) / prev * 100.0
        pct = max(-WOW_VOLUME_PCT_CLAMP, min(WOW_VOLUME_PCT_CLAMP, raw_pct))
        out["volume_pct"] = pct
        if pct > WOW_VOLUME_FLAT_PCT:
            out["volume_arrow"] = "up"
        elif pct < -WOW_VOLUME_FLAT_PCT:
            out["volume_arrow"] = "down"
        else:
            out["volume_arrow"] = "flat"
        extra = ""
        if raw_pct != pct:
            extra = f" (capped from {raw_pct:+.0f}%)."
        out["volume_title"] = base_vol_title + f" ({pct:+.0f}% change).{extra}"

    if cur > 0 and prev > 0:
        out["show_accuracy_line"] = True
        out["accuracy_title"] = (
            f"Accuracy — last 7 days: {pct_cur}% · previous 7 days: {pct_prev}%."
        )
        low_n = min(cur, prev)
        if low_n < WOW_ACCURACY_MIN_SAMPLES:
            out["accuracy_insufficient"] = True
            out["accuracy_arrow"] = "insufficient"
        else:
            dpp = (acc_cur - acc_prev) * 100.0
            out["accuracy_pp"] = dpp
            out["accuracy_title"] += f" ({dpp:+.1f} pts)."
            if dpp > WOW_ACCURACY_FLAT_PP:
                out["accuracy_arrow"] = "up"
            elif dpp < -WOW_ACCURACY_FLAT_PP:
                out["accuracy_arrow"] = "down"
            else:
                out["accuracy_arrow"] = "flat"

    return out


def _study_insight_href(
    source_pdf: Optional[str], *, due_only: bool = False, weak_only: bool = False
) -> str:
    parts: list[str] = []
    if source_pdf:
        parts.append(f"source_pdfs={quote(source_pdf, safe='')}")
    if due_only:
        parts.append("due_only=true")
    if weak_only:
        parts.append("weak_only=true")
    return "/study?" + "&".join(parts)


def _pick_top_source_metric(
    per_source_counts: dict[str, dict[str, int]], metric: str
) -> Optional[tuple[str, int]]:
    best_name: Optional[str] = None
    best_val = -1
    for name in sorted(per_source_counts.keys()):
        v = int(per_source_counts[name].get(metric, 0))
        if v > best_val:
            best_val = v
            best_name = name
    if best_name is None or best_val <= 0:
        return None
    return (best_name, best_val)


def build_study_activity_insight(
    *,
    answers_7d: int,
    answers_30d: int,
    answers_all_time: int,
    overdue_word_count: int,
    study_wow: dict[str, Any],
    ai_chat_attempts_30d: int,
    per_source_counts: dict[str, dict[str, int]],
) -> Optional[dict[str, str]]:
    """
    Deterministic “what to do next” for /study, from existing metrics only.
    First matching rule wins.

    IMPORTANT: rule order defines precedence; do not reorder without UX review.

    NOTE: With a single global overdue count, we cannot rank per-source overdue
    when the backlog is spread. We only distinguish concentrated (one PDF holds
    the full count) vs spread (top per-source due sum is below the global total).

    1) Global overdue (backlog spread across sources)
    2) Per-source overdue (entire backlog sits in one PDF)
    3) Week-over-week accuracy drop
    4) Per-source weak (largest weak pool by source)
    5) Quiet UTC week with prior activity
    6) Study on /study but no AI chat practice — momentum nudge
    """
    top_due = _pick_top_source_metric(per_source_counts, "due_count")

    if overdue_word_count > 0:
        concentrated = (
            top_due is not None
            and top_due[1] == overdue_word_count
            and bool(top_due[0] and str(top_due[0]).strip())
        )
        if not concentrated:
            w = "word" if overdue_word_count == 1 else "words"
            if overdue_word_count == 1:
                body = (
                    f"You have 1 overdue {w} — start clearing your backlog with a recall session."
                )
            else:
                body = (
                    f"You have {overdue_word_count} overdue {w} across multiple sources — "
                    "start clearing your backlog."
                )
            return {
                "body": body,
                "cta_label": "Start recall session",
                "cta_href": _study_insight_href(None, due_only=True),
            }
        src, n = top_due
        w = "word" if n == 1 else "words"
        need_review = "needs review" if n == 1 else "need review"
        return {
            "body": f"Focus on “{src}” — {n} overdue {w} {need_review}.",
            "cta_label": "Start recall for this source",
            "cta_href": _study_insight_href(src, due_only=True),
        }

    if (
        study_wow.get("show_accuracy_line")
        and not study_wow.get("accuracy_insufficient")
        and study_wow.get("accuracy_arrow") == "down"
    ):
        return {
            "body": (
                "Your accuracy dropped this week compared to the week before — "
                "focus on weak words."
            ),
            "cta_label": "Practice weak words",
            "cta_href": _study_insight_href(None, weak_only=True),
        }

    top_weak = _pick_top_source_metric(per_source_counts, "weak_count")
    if (
        top_weak is not None
        and top_weak[1] >= INSIGHT_MIN_WEAK_FOR_SOURCE
        and top_weak[0]
        and str(top_weak[0]).strip()
    ):
        src, n = top_weak
        w = "word" if n == 1 else "words"
        return {
            "body": f"“{src}” needs attention — {n} weak {w}.",
            "cta_label": "Practice weak words",
            "cta_href": _study_insight_href(src, weak_only=True),
        }

    if answers_7d == 0 and (answers_30d > 0 or answers_all_time > 0):
        return {
            "body": (
                "You haven't studied this week (UTC) — try a short session to keep momentum."
            ),
            "cta_label": "Start studying",
            "cta_href": "/study",
        }

    if answers_30d > 0 and ai_chat_attempts_30d == 0:
        if answers_7d > 0:
            return {
                "body": (
                    f"You've completed {answers_7d} answers in the last 7 days — keep going!"
                ),
                "cta_label": "Keep studying",
                "cta_href": "/study",
            }
        return {
            "body": (
                f"You've logged {answers_30d} answers in the last 30 days. "
                "A short session this week keeps momentum strong."
            ),
            "cta_label": "Study now",
            "cta_href": "/study",
        }

    return None


@api_router.get("/theme-summary")
def theme_summary(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    return get_theme_summary(db, days)


@router.get("/dashboard", response_class=HTMLResponse)
def analytics_dashboard(request: Request, db: Session = Depends(get_db)):
    merged = []
    recommendation = None
    insight_line = None
    overview_insight = None
    summary = {
        "themes_studied": 0,
        "total_attempts": 0,
        "avg_first_try": 0,
        "avg_reveal": 0,
    }
    study_summary = {
        "answers_30d": 0,
        "accuracy_30d": 0.0,
        "answers_7d": 0,
        "accuracy_7d": 0.0,
        "answers_all_time": 0,
        "accuracy_all_time": 0.0,
    }
    study_activity_insight: Optional[dict[str, str]] = None
    study_wow: dict = _compute_study_wow(
        {"total_answers": 0, "success_rate": 0.0},
        {"total_answers": 0, "success_rate": 0.0},
    )
    answers_per_day_7 = [0] * 7
    chart_day_max = 0
    ad_default = utc_now().replace(tzinfo=None).date()
    chart_start_default = ad_default - timedelta(days=6)
    chart_day_labels = [
        (chart_start_default + timedelta(days=i)).strftime("%a") for i in range(7)
    ]
    chart_day_iso = [
        (chart_start_default + timedelta(days=i)).isoformat() for i in range(7)
    ]
    chart_day_weekend = [
        (chart_start_default + timedelta(days=i)).weekday() >= 5 for i in range(7)
    ]

    try:
        anchor_naive = utc_now().replace(tzinfo=None)
        study_all = get_study_answer_metrics_since(db, None)
        study_30 = get_study_answer_metrics_since(
            db, _study_cutoff_from_anchor(anchor_naive, 30)
        )
        study_last_cal = get_study_calendar_week_activity(db, anchor_naive, week_offset=0)
        study_prev_cal = get_study_calendar_week_activity(db, anchor_naive, week_offset=1)

        answers_per_day_7 = list(study_last_cal["per_day"])
        chart_day_max = max(answers_per_day_7) if answers_per_day_7 else 0
        sd = study_last_cal["start_day"]
        chart_day_labels = [(sd + timedelta(days=i)).strftime("%a") for i in range(7)]
        chart_day_iso = [(sd + timedelta(days=i)).isoformat() for i in range(7)]
        chart_day_weekend = [(sd + timedelta(days=i)).weekday() >= 5 for i in range(7)]

        study_summary = {
            "answers_30d": int(study_30["total_answers"]),
            "accuracy_30d": float(study_30["success_rate"]),
            "answers_7d": int(study_last_cal["total_answers"]),
            "accuracy_7d": float(study_last_cal["success_rate"]),
            "answers_all_time": int(study_all["total_answers"]),
            "accuracy_all_time": float(study_all["success_rate"]),
        }
        study_wow = _compute_study_wow(study_last_cal, study_prev_cal)

        data_30 = get_theme_summary(db, 30)
        data_7 = get_theme_summary(db, 7)

        themes_30 = {t["theme"]: t for t in data_30.get("themes", [])}
        themes_7 = {t["theme"]: t for t in data_7.get("themes", [])}

        for theme, stats in themes_30.items():
            stats_7 = themes_7.get(theme, {})
            attempts_30d = int(stats.get("total_attempts", 0) or 0)
            first_try_30d = float(stats.get("first_try_success_rate", 0) or 0)
            reveal_30d = float(stats.get("reveal_rate", 0) or 0)
            retry_30d = float(stats.get("retry_rate", 0) or 0)
            avg_attempts_30d = (
                float(stats.get("average_attempts", 0))
                if stats.get("has_resolved_attempts", False)
                else None
            )
            _, weight_30d, score_30d = _composite_score(
                first_try_30d, reveal_30d, retry_30d, attempts_30d
            )

            attempts_7d = int(stats_7.get("total_attempts", 0) or 0)
            first_try_7d = float(stats_7.get("first_try_success_rate", 0) or 0)
            reveal_7d = float(stats_7.get("reveal_rate", 0) or 0)
            retry_7d = float(stats_7.get("retry_rate", 0) or 0)
            _, weight_7d_raw, score_7d_raw = _composite_score(
                first_try_7d, reveal_7d, retry_7d, attempts_7d
            )

            trend_has_data = attempts_7d >= MIN_TREND_ATTEMPTS
            if trend_has_data:
                trend_first_try = _metric_trend(
                    first_try_7d - first_try_30d,
                    better_is_lower=False,
                )
                trend_reveal = _metric_trend(
                    reveal_7d - reveal_30d,
                    better_is_lower=True,
                )
                trend_retry = _metric_trend(
                    retry_7d - retry_30d,
                    better_is_lower=True,
                )
                trend_composite = _composite_trend(score_7d_raw, score_30d)
                weight_7d = weight_7d_raw
                score_7d = score_7d_raw
            else:
                trend_first_try = "insufficient"
                trend_reveal = "insufficient"
                trend_retry = "insufficient"
                trend_composite = "insufficient"
                weight_7d = None
                score_7d = None

            merged.append(
                {
                    "theme": theme,
                    "attempts_30d": attempts_30d,
                    "first_try_30d": first_try_30d,
                    "reveal_30d": reveal_30d,
                    "retry_30d": retry_30d,
                    "avg_attempts_30d": avg_attempts_30d,
                    "weight_30d": weight_30d,
                    "score_30d": score_30d,
                    "attempts_7d": attempts_7d,
                    "first_try_7d": first_try_7d,
                    "reveal_7d": reveal_7d,
                    "retry_7d": retry_7d,
                    "weight_7d": weight_7d,
                    "score_7d": score_7d,
                    "trend_has_data": trend_has_data,
                    "trend_first_try": trend_first_try,
                    "trend_reveal": trend_reveal,
                    "trend_retry": trend_retry,
                    "trend_composite": trend_composite,
                    "resolved_attempt_count": stats.get("resolved_attempt_count", 0),
                    "has_resolved_attempts": stats.get("has_resolved_attempts", False),
                    "needs_attention_rank": None,
                    "needs_attention_reason": None,
                }
            )

        merged.sort(
            key=lambda x: (x["score_30d"], x["attempts_30d"], x["reveal_30d"]),
            reverse=True,
        )
        if merged:
            recommendation = merged[0]
            recommendation["needs_attention_rank"] = 1
            recommendation["needs_attention_reason"] = (
                "Highest composite friction in last 30 days."
            )

        total_attempts = sum(t["attempts_30d"] for t in merged)
        themes_studied = len([t for t in merged if t["attempts_30d"] > 0])
        avg_first_try = (
            sum(t["first_try_30d"] * t["attempts_30d"] for t in merged) / total_attempts
        ) if total_attempts else 0
        avg_reveal = (
            sum(t["reveal_30d"] * t["attempts_30d"] for t in merged) / total_attempts
        ) if total_attempts else 0

        summary = {
            "themes_studied": themes_studied,
            "total_attempts": total_attempts,
            "avg_first_try": avg_first_try,
            "avg_reveal": avg_reveal,
        }

        if recommendation:
            theme_label = recommendation["theme"].replace("_", " ").title()
            insight_line = recommendation["needs_attention_reason"]
            if avg_first_try < 0.5:
                overview_insight = (
                    f"Your recall accuracy is currently low. Focus on repeating {theme_label} "
                    "before studying new themes."
                )
            elif recommendation["trend_composite"] == "improving":
                overview_insight = (
                    f"Recall is improving in {theme_label}. Keep practicing before adding more themes."
                )
            else:
                overview_insight = (
                    f"{theme_label} still needs repetition before recall feels automatic."
                )

        daily = get_daily_dashboard_stats(db)
        per_source_counts = get_per_source_due_weak_counts(db)
        study_activity_insight = build_study_activity_insight(
            answers_7d=study_summary["answers_7d"],
            answers_30d=study_summary["answers_30d"],
            answers_all_time=study_summary["answers_all_time"],
            overdue_word_count=int(daily["overdue_word_count"] or 0),
            study_wow=study_wow,
            ai_chat_attempts_30d=int(summary["total_attempts"]),
            per_source_counts=per_source_counts,
        )
    except Exception:
        merged = []
        recommendation = None
        insight_line = None
        overview_insight = None
        summary = {
            "themes_studied": 0,
            "total_attempts": 0,
            "avg_first_try": 0,
            "avg_reveal": 0,
        }
        study_summary = {
            "answers_30d": 0,
            "accuracy_30d": 0.0,
            "answers_7d": 0,
            "accuracy_7d": 0.0,
            "answers_all_time": 0,
            "accuracy_all_time": 0.0,
        }
        study_activity_insight = None
        study_wow = _compute_study_wow(
            {"total_answers": 0, "success_rate": 0.0},
            {"total_answers": 0, "success_rate": 0.0},
        )
        answers_per_day_7 = [0] * 7
        chart_day_max = 0
        ad_ex = utc_now().replace(tzinfo=None).date()
        chart_start_ex = ad_ex - timedelta(days=6)
        chart_day_labels = [
            (chart_start_ex + timedelta(days=i)).strftime("%a") for i in range(7)
        ]
        chart_day_iso = [
            (chart_start_ex + timedelta(days=i)).isoformat() for i in range(7)
        ]
        chart_day_weekend = [
            (chart_start_ex + timedelta(days=i)).weekday() >= 5 for i in range(7)
        ]

    return templates.TemplateResponse(
        request,
        "analytics_dashboard.html",
        {
            "themes": merged,
            "recommendation": recommendation,
            "insight_line": insight_line,
            "overview_insight": overview_insight,
            "summary": summary,
            "study_summary": study_summary,
            "study_activity_insight": study_activity_insight,
            "study_wow": study_wow,
            "answers_per_day_7": answers_per_day_7,
            "chart_day_max": chart_day_max,
            "chart_day_labels": chart_day_labels,
            "chart_day_iso": chart_day_iso,
            "chart_day_weekend": chart_day_weekend,
            "active": "analytics",
        },
    )
