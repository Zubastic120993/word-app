"""API router for data export and import operations."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import create_manual_backup, get_db
from app.dependencies import require_mac_role
from app.schemas.export_import import ImportResponse, ImportValidationResult
from app.services.export_service import ExportService
from app.services.import_service import import_all_data, validate_import_payload

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["data-management"],
    dependencies=[Depends(require_mac_role)],
)


@router.post("/backup")
def create_backup() -> dict[str, Any]:
    """
    Create a manual backup of the database (data + progress/scores).
    Saves a copy to data/backups/vocabulary_manual_YYYYMMDD_HHMMSS.db.
    """
    filename = create_manual_backup(settings.database_url)
    if filename is None:
        raise HTTPException(
            status_code=500,
            detail="Backup failed. Check that the database has all required tables and try again.",
        )
    return {"success": True, "filename": filename}


@router.get("/export")
def export_data(
    db: Session = Depends(get_db),
) -> Response:
    """
    Export all application data as a downloadable JSON file.
    
    Returns a JSON file containing:
    - Application settings
    - All learning units
    - All learning progress records
    - All learning sessions
    - All session units
    
    The export is complete and deterministic - same database state
    produces identical output.
    
    Returns:
        JSON file download with timestamp in filename.
    """
    logger.info("Starting data export")
    
    # Export all data
    service = ExportService(db)
    export_data = service.export_all_data()
    
    # Generate filename with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"word_app_export_{timestamp}.json"
    
    # Serialize to JSON with pretty formatting for human readability
    json_content = export_data.model_dump_json(indent=2)
    
    logger.info(f"Export complete: {filename}")
    
    # Return as downloadable file
    return Response(
        content=json_content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/import", response_model=ImportResponse)
async def import_data(
    file: UploadFile = File(...),
    confirm: bool = Query(
        False,
        description="Must be true to confirm import. Import is destructive and will replace all existing data.",
    ),
    db: Session = Depends(get_db),
) -> ImportResponse:
    """
    Import data from a JSON export file.
    
    **WARNING**: This operation is DESTRUCTIVE. It will:
    - Create a backup of current data
    - Delete ALL existing data
    - Import data from the uploaded file
    
    If import fails, the backup will be restored.
    
    Args:
        file: JSON file to import.
        confirm: Must be true to confirm the destructive operation.
        db: Database session.
        
    Returns:
        ImportResponse with result status.
        
    Raises:
        HTTPException 400: If confirm is not true.
        HTTPException 400: If file is not valid JSON.
        HTTPException 422: If validation fails.
    """
    # Require explicit confirmation
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Import requires explicit confirmation. Set confirm=true to proceed. "
                   "WARNING: This will DELETE ALL existing data and replace it with imported data.",
        )
    
    # Validate file type
    if not file.filename or not file.filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="Only JSON files are accepted for import.",
        )
    
    # Read and parse file
    try:
        content = await file.read()
        data = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON file: {str(e)}",
        )
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="File must be UTF-8 encoded.",
        )
    
    logger.info(f"Starting import from file: {file.filename}")
    
    # Perform import
    result = import_all_data(db, data)
    
    if not result.success:
        logger.error(f"Import failed: {result.message}")
        raise HTTPException(
            status_code=422,
            detail=result.message,
        )
    
    logger.info(
        f"Import successful: {result.units_imported} units, "
        f"{result.sessions_imported} sessions"
    )
    
    return result


@router.post("/import/validate", response_model=ImportValidationResult)
async def validate_import(
    file: UploadFile = File(...),
) -> ImportValidationResult:
    """
    Validate an import file without making changes.
    
    Use this endpoint to check if an import file is valid
    before performing the actual import.
    
    Args:
        file: JSON file to validate.
        
    Returns:
        ImportValidationResult with validation status.
        
    Raises:
        HTTPException 400: If file is not valid JSON.
    """
    # Validate file type
    if not file.filename or not file.filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="Only JSON files are accepted for import.",
        )
    
    # Read and parse file
    try:
        content = await file.read()
        data = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON file: {str(e)}",
        )
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="File must be UTF-8 encoded.",
        )
    
    logger.info(f"Validating import file: {file.filename}")

    # Validate without importing
    result = validate_import_payload(data)

    return result


@router.post("/admin/backfill-context")
def backfill_context(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Batch-generate AI context sentences for words that don't have one.

    Call repeatedly until ``remaining`` reaches 0. Each call processes up to
    ``limit`` words (1–200, default 50). Idempotent — already-filled words
    are never touched.
    """
    from app.services.cloze_service import backfill_context_sentences

    result = backfill_context_sentences(db, limit=limit)
    logger.info(
        "Context backfill: processed=%d succeeded=%d skipped=%d remaining=%d",
        result["processed"],
        result["succeeded"],
        result["skipped"],
        result["remaining"],
    )
    return result
