"""API router for learning sessions."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.source_list_service import get_pdf_sources, get_vocabulary_groups
from app.schemas.session import (
    AnswerRequest,
    AnswerResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionHistoryResponse,
    SessionResponse,
    StudyModeType,
)
from app.services.lesson_service import (
    build_primary_curriculum_map,
    detect_current_plua_lesson,
    get_plua_lesson_progress,
    is_plua_lesson_completed,
)
from app.services.daily_stats import get_daily_dashboard_stats
from app.services.session_service import (
    InsufficientUnitsError,
    NoDueUnitsError,
    NoDueUnitsInThemeError,
    SessionService,
)

logger = logging.getLogger(__name__)
session_trace_logger = logging.getLogger("app.services.session_service")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("/sources")
def list_study_sources(db: Session = Depends(get_db)) -> list[dict]:
    """
    List PDF sources for study setup (iPad-accessible).
    Same data as /api/pdfs/sources but without require_mac_role.
    """
    return get_pdf_sources(db)


@router.get("/vocabulary-groups")
def list_study_vocabulary_groups(db: Session = Depends(get_db)) -> list[dict]:
    """
    List vocabulary groups for study setup (iPad-accessible).
    Same data as /api/vocabulary-groups but without require_mac_role.
    """
    return get_vocabulary_groups(db)


@router.get("/plua-curriculum-snapshot")
def plua_curriculum_snapshot(
    check_lesson: Optional[int] = Query(
        None,
        description="If set, include check_lesson_complete using the same rules as the curriculum engine.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only PL–UA lesson pointer and progress (same helpers as Home snapshot)."""
    lesson_map = build_primary_curriculum_map(db)
    current = detect_current_plua_lesson(db)
    max_idx = max(lesson_map.keys()) if lesson_map else 1
    progress = get_plua_lesson_progress(db, current)
    out: dict = {
        "current_plua_lesson": current,
        "lesson_progress": progress,
        "max_lesson_index": max_idx,
    }
    if check_lesson is not None:
        out["check_lesson_index"] = check_lesson
        out["check_lesson_complete"] = is_plua_lesson_completed(db, check_lesson)
    return out


@router.get("/passive-intro-cap")
def passive_intro_cap_status(db: Session = Depends(get_db)) -> dict:
    """
    Read-only snapshot for passive "new words per day" cap (same logic as create preflight).
    Lets the study client confirm once and send override_cap on the first POST.
    """
    daily = get_daily_dashboard_stats(db)
    return {
        "cap_exceeded": bool(daily["cap_exceeded"]),
        "words_introduced_today": int(daily["words_introduced_today"] or 0),
        "max_new_per_day": int(daily["max_new_per_day"]),
    }


@router.get("/recall-availability")
def check_recall_availability(
    source_pdfs: Optional[list[str]] = Query(None, description="Optional list of PDF filenames to filter by"),
    db: Session = Depends(get_db),
):
    """
    Check if enough introduced words exist for Recall modes.
    
    Returns availability status for Recall (Visual) and Recall (Audio) modes.
    
    Args:
        source_pdfs: Optional list of PDF filenames to filter by.
        
    Returns:
        Dictionary with availability status and counts.
    """
    return SessionService(db).get_recall_availability(source_pdfs=source_pdfs)


@router.post("/create", response_model=SessionCreateResponse)
def create_session(
    http_request: Request,
    request: SessionCreateRequest = SessionCreateRequest(),
    db: Session = Depends(get_db),
) -> SessionCreateResponse:
    """
    Create a new learning session (size from settings, default 50 units).
    
    Units are selected by priority:
    1. Never seen
    2. Weak (low confidence)
    3. Failed
    4. Known (if needed)
    
    Sessions are locked (immutable) once created.
    
    Mode options:
    - passive: User sees source text, self-assesses (default)
    - recall: User sees translation, types source text, backend evaluates (visual recall)
    - recall_audio: User hears audio, types source text, backend evaluates (audio recall)
    - cloze: Sentence with a blank; user types the source word (mixed with visual recall if needed)
    
    Returns:
        Session creation confirmation with session ID.
        
    Raises:
        HTTPException 400: If fewer than 20 units exist, or recall_audio requested but ElevenLabs disabled.
    """
    service = SessionService(db)
    fallback_notice: Optional[str] = None
    response_mode = request.mode
    session_trace_logger.info(
        "session create trace: page_instance_id=%s post_seq=%s debug_tag=%s client=%s",
        http_request.headers.get("x-study-page-instance-id"),
        http_request.headers.get("x-session-create-post-seq"),
        http_request.headers.get("x-session-create-debug-tag"),
        http_request.client.host if http_request.client else None,
    )
    try:
        try:
            session = service.create_session_from_request(request)
        except InsufficientUnitsError as e:
            if (
                getattr(e, "code", None) == "NO_FOLLOWUP_INTRODUCED"
                and request.follow_up_session_id is not None
            ):
                logger.info(
                    "Follow-up recall had no introduced words from prior session; "
                    "retrying create without follow_up_session_id"
                )
                fallback_notice = (
                    "No words from the previous session were ready for recall yet. "
                    "Continuing with regular practice."
                )
                fallback_request = request.model_copy(
                    update={
                        "follow_up_session_id": None,
                        "retry_failed_only": False,
                    }
                )
                try:
                    session = service.create_session_from_request(fallback_request)
                except InsufficientUnitsError as e2:
                    if getattr(e2, "code", None) == "INSUFFICIENT_INTRODUCED_RECALL":
                        logger.info(
                            "Recall after empty follow-up still below introduced threshold; "
                            "fallback to passive session"
                        )
                        passive_extra = (
                            " Not enough introduced words for a full recall practice—"
                            "continuing in Passive mode."
                        )
                        fallback_notice = (fallback_notice or "") + passive_extra
                        passive_request = fallback_request.model_copy(
                            update={"mode": StudyModeType.PASSIVE}
                        )
                        try:
                            session = service.create_session_from_request(passive_request)
                            response_mode = StudyModeType.PASSIVE
                        except InsufficientUnitsError as e3:
                            logger.warning(
                                "Session create failed (after passive fallback): %s", e3
                            )
                            raise HTTPException(status_code=400, detail=str(e3)) from e3
                    else:
                        logger.warning(
                            "Session create failed (after follow-up fallback): %s", e2
                        )
                        raise HTTPException(status_code=400, detail=str(e2)) from e2
            else:
                logger.warning("Session create failed: %s", e)
                raise HTTPException(status_code=400, detail=str(e)) from e

        if isinstance(session, dict) and (
            session.get("daily_cap_reached") or session.get("cap_warning")
        ):
            return JSONResponse(
                status_code=200,
                content=session,
            )

        if isinstance(session, dict):
            logger.error(
                "create_session_from_request returned unexpected dict keys: %s",
                list(session.keys()),
            )
            raise HTTPException(
                status_code=500,
                detail="Unexpected session create response",
            )

        # Use persisted session.mode so clients match server (e.g. follow-up fallbacks may downgrade mode).
        actual_mode = session.mode
        return SessionCreateResponse(
            session_id=session.id,
            mode=actual_mode,
            units_count=len(session.units),
            message=f"{actual_mode.value.title()} practice started — {len(session.units)} words.",
            fallback_notice=fallback_notice,
            short_session_note=getattr(session, "_short_session_note", None),
            session_reason=getattr(session, "_readiness_gate", None),
        )
        
    except NoDueUnitsInThemeError as e:
        session_trace_logger.info(
            "create_session early return: reason=no_due_units_in_theme theme=%s",
            e.theme,
        )
        return JSONResponse(
            status_code=200,
            content={
                "session_id": None,
                "status": "empty",
                "reason": "no_due_units_in_theme",
                "theme": e.theme,
                "message": str(e),
            },
        )
    except NoDueUnitsError as e:
        session_trace_logger.info("create_session early return: reason=no_due_units")
        return JSONResponse(
            status_code=200,
            content={
                "session_id": None,
                "status": "empty",
                "reason": "no_due_units",
                "message": str(e),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/history", response_model=SessionHistoryResponse)
def get_session_history(
    limit: int = Query(default=20, ge=1, le=100, description="Number of sessions to return"),
    offset: int = Query(default=0, ge=0, description="Number of sessions to skip"),
    db: Session = Depends(get_db),
) -> SessionHistoryResponse:
    """
    Get session history for review.
    
    Returns sessions ordered by completion date (most recent first).
    Includes completed and abandoned sessions, excludes active (in-progress) sessions.
    
    This endpoint is READ-ONLY and does not affect learning logic or scoring.
    
    Args:
        limit: Maximum number of sessions to return (1-100, default 20).
        offset: Number of sessions to skip for pagination.
        
    Returns:
        List of session history items with pagination info.
    """
    return SessionHistoryResponse(**SessionService(db).get_session_history_response_data(limit=limit, offset=offset))


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
) -> SessionResponse:
    """
    Get a learning session by ID.
    
    Returns full session with all units and their details.
    
    Args:
        session_id: Session ID.
        
    Returns:
        Full session details.
        
    Raises:
        HTTPException 404: If session not found.
    """
    service = SessionService(db)
    session = service.get_session(session_id)
    
    if not session:
        raise HTTPException(
            status_code=404,
            detail=f"Session {session_id} not found.",
        )
    
    return session


@router.post("/{session_id}/answer", response_model=AnswerResponse)
def submit_answer(
    session_id: int,
    answer: AnswerRequest,
    db: Session = Depends(get_db),
) -> AnswerResponse:
    """
    Submit an answer for a unit in a session.
    
    Updates both session progress and learning progress.
    
    For passive mode:
    - is_correct is required (user self-assessment)
    
    For recall mode:
    - user_input is required (backend evaluates correctness)
    - Response includes expected_answer for feedback
    
    Args:
        session_id: Session ID.
        answer: Answer details (unit_position from 1 to 50 by default — see ``WORD_APP_SESSION_SIZE`` in ``app/config.py``; plus ``is_correct`` or ``user_input``).
        
    Returns:
        Answer confirmation with updated session stats.
        
    Raises:
        HTTPException 404: If session not found.
        HTTPException 400: If unit already answered, invalid position, or missing input.
    """
    service = SessionService(db)
    try:
        return service.submit_answer_and_build_response(session_id=session_id, answer=answer)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{session_id}/abandon")
def abandon_session(
    session_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Explicitly abandon a CREATED/ACTIVE session (user-triggered Start Fresh).

    Only marks the given session abandoned — does NOT touch other sessions
    and does NOT create a new one.

    Returns:
        {"status": "abandoned"}

    Raises:
        404: Session not found.
        400: Session already completed or abandoned.
    """
    try:
        SessionService(db).abandon_session(session_id)
    except ValueError as exc:
        msg = str(exc)
        status_code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=status_code, detail=msg)
    return {"status": "abandoned"}


@router.get("/{session_id}/next-recommendation")
def next_recommendation(
    session_id: int,
    db: Session = Depends(get_db),
):
    """Return an intelligent post-session recommendation for what to study next."""
    try:
        return SessionService(db).get_next_recommendation(session_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
