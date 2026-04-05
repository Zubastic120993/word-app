"""UI router for serving HTML pages."""

import logging
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.progress_service import (
    get_chat_page_data,
    get_data_management_page_data,
    get_home_snapshot,
    get_progress_page_data,
    get_recent_uploads,
    get_source_counts,
)
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])

# Templates directory
templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Cache bust value (changes on server restart)
CACHE_BUST = str(int(time.time()))

# Add cache_bust to all templates
templates.env.globals["cache_bust"] = CACHE_BUST
templates.env.filters["urlquote"] = lambda value: quote(str(value or ""), safe="")


@router.get("/")
async def home(request: Request, db: Session = Depends(get_db)):
    """Learning Launchpad — answers "What should I study now?"

    When the client role is "ipad", redirect to /study (the iPad landing
    experience).  The redirect is server-side (302) so the browser never
    renders the full dashboard.
    """
    # iPad clients skip the dashboard entirely
    client_role = getattr(request.state, "client_role", "mac")
    if client_role == "ipad":
        return RedirectResponse(url="/study", status_code=302)

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "active": "home",
            "snapshot": get_home_snapshot(db),
            "max_new_per_day": settings.max_new_per_day,
        },
    )


@router.get("/upload")
async def upload_page(request: Request, db: Session = Depends(get_db)):
    """PDF upload page."""
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "active": "upload",
            "recent_uploads": get_recent_uploads(db),
        },
    )


@router.get("/study")
async def study_page(
    request: Request,
    db: Session = Depends(get_db),
    weak_only: bool = False,
    due_only: bool = False,
    curriculum_mode: str | None = None,
    new_words_focus: bool = False,
):
    """Study Mode page."""
    page_data = SessionService(db).get_study_page_data(
        weak_only=weak_only,
        due_only=due_only,
        curriculum_mode=curriculum_mode,
        new_words_focus=new_words_focus,
    )

    return templates.TemplateResponse(
        request,
        "study.html",
        {
            "active": "study",
            **page_data,
            "session_size": settings.session_size,
            "max_new_per_day": settings.max_new_per_day,
        },
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


@router.get("/chat")
async def chat_page(request: Request):
    """Free Chat page."""
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "active": "chat",
            **(await get_chat_page_data()),
        },
    )


@router.get("/progress")
async def progress_page(request: Request, db: Session = Depends(get_db)):
    """Progress / stats page."""
    return templates.TemplateResponse(
        request,
        "progress.html",
        {
            "active": "progress",
            **get_progress_page_data(db),
        },
    )


@router.get("/history")
async def history_page(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """
    Session history page.
    
    Displays past study sessions in a table format.
    Read-only view - does not affect learning logic.
    """
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "active": "history",
            **SessionService(db).get_history_page_data(limit=limit, offset=offset),
        },
    )


@router.get("/history/{session_id}")
async def history_detail_page(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
):
    """
    Session detail page (read-only).
    
    Displays detailed view of a specific session's units and answers.
    No editing or re-answering allowed - purely historical view.
    """
    session_data = SessionService(db).get_history_detail_page_data(session_id)
    if not session_data:
        # Session not found - redirect to history
        return RedirectResponse(url="/history", status_code=302)

    return templates.TemplateResponse(
        request,
        "history_detail.html",
        {
            "active": "history",
            "session": session_data,
        },
    )


@router.get("/vocabulary")
async def vocabulary_page(request: Request, db: Session = Depends(get_db)):
    """
    Vocabulary management page.
    
    Dedicated page for all vocabulary CRUD operations:
    - Adding new vocabulary
    - Searching and filtering
    - Editing existing units
    - Deleting units
    """
    return templates.TemplateResponse(
        request,
        "vocabulary.html",
        {
            "active": "vocabulary",
            "sources": get_source_counts(db),
        },
    )


@router.get("/data", response_class=HTMLResponse)
def data_management(
    request: Request,
    db: Session = Depends(get_db),
):
    """Data management page for export/import."""
    return templates.TemplateResponse(
        request,
        "data.html",
        {
            "active": "data",
            "stats": get_data_management_page_data(db),
        },
    )
