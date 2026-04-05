"""API router for progress statistics."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.progress_service import compute_selection_progress_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/progress", tags=["progress"])


@router.get("/selection-stats")
def get_selection_stats(
    source_pdfs: Optional[list[str]] = Query(None, description="Optional list of PDF filenames to filter by. If null, includes all sources."),
    db: Session = Depends(get_db),
):
    """
    Get progress statistics for selected vocabulary sources.
    
    Returns per-mode progress percentages based on selected vocabulary sources.
    
    Args:
        source_pdfs: Optional list of PDF filenames to filter by. If null, includes all sources.
        
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
    stats = compute_selection_progress_stats(db, source_pdfs=source_pdfs)
    return stats
