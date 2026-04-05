"""API router for PDF upload and learning unit management."""

import logging
import re
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.learning_unit import LearningUnit, LearningProgress, UnitType, normalize_text
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.services.progress_service import compute_mastery_stats
from app.schemas.learning_unit import (
    LearningUnitResponse,
    LearningUnitUpdate,
    PaginatedUnitsResponse,
    ParsedUnitItem,
    PDFParseResponse,
    PDFConfirmRequest,
    PDFConfirmResponse,
    ValidationSummary,
)
from app.services.pdf_parser import PDFParser
from app.services.ai.vocabulary_validation import VocabularyValidator, ValidationLevel
from app.dependencies import require_mac_role
from app.services.source_list_service import get_vocabulary_groups
from app.services.vocabulary_projection_service import get_effective_vocabularies
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["learning-units"],
    dependencies=[Depends(require_mac_role)],
)

DEFAULT_USER_KEY = "local"
USER_VOCAB_NAME = "Chat Vocabulary"
INBOX_VOCAB_NAME = "Inbox"


def guess_base_form(text: str) -> str:
    word = text.strip()

    if not word:
        return text

    if " " in word:
        return text

    if not re.fullmatch(r"[A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ]+", word):
        return text

    if len(word) < 4:
        return text

    w = word.lower()

    # High-confidence noun heuristics only.
    if w.endswith("ę"):
        return word[:-1] + "a"

    if w.endswith("ów") and len(w) > 5:
        return word[:-2]

    return text


class CreateUnitRequest(BaseModel):
    text: str
    translation: str
    vocabulary_id: Optional[int] = None


def _get_or_create_vocabulary(db: Session, *, user_key: str, name: str) -> Vocabulary:
    vocab = (
        db.query(Vocabulary)
        .filter(Vocabulary.user_key == user_key)
        .filter(Vocabulary.name == name)
        .first()
    )
    if vocab:
        return vocab

    vocab = Vocabulary(user_key=user_key, name=name)
    db.add(vocab)
    db.commit()
    db.refresh(vocab)
    return vocab


def _ensure_user_vocabulary(db: Session, *, user_key: str) -> Vocabulary:
    # Created only once per user_key and reused thereafter.
    return _get_or_create_vocabulary(db, user_key=user_key, name=USER_VOCAB_NAME)


def _ensure_inbox_vocabulary(db: Session, *, user_key: str) -> Vocabulary:
    # Dedicated inbox for words captured via Quick Add.
    return _get_or_create_vocabulary(db, user_key=user_key, name=INBOX_VOCAB_NAME)


def _sync_vocabularies_from_sources(db: Session, *, user_key: str) -> None:
    """
    Keep vocabularies table in sync with existing LearningUnit.source_pdf values.

    This makes vocabulary assignment deterministic without rewriting legacy data.
    """
    # Ensure the per-user fallback exists.
    _ensure_user_vocabulary(db, user_key=user_key)

    sources = [r[0] for r in db.query(LearningUnit.source_pdf).distinct().all()]
    for name in sources:
        if not name:
            continue
        _get_or_create_vocabulary(db, user_key=user_key, name=name)


def _get_readonly_vocabularies(db: Session, *, user_key: str) -> list[dict]:
    """Return the effective vocabulary list without mutating database state."""
    return get_effective_vocabularies(db, user_key)


@router.put("/vocabularies/{vocabulary_id}/group")
def assign_vocabulary_to_group(
    vocabulary_id: int,
    group_id: Optional[int] = Query(None, description="Group ID to assign to, or null to unassign"),
    db: Session = Depends(get_db),
) -> dict:
    """
    Assign or reassign a vocabulary to a group.
    
    Args:
        vocabulary_id: ID of vocabulary to assign.
        group_id: ID of group to assign to, or null to unassign.
        
    Returns:
        Success message with updated vocabulary info.
    """
    # Get vocabulary
    vocabulary = db.query(Vocabulary).filter(Vocabulary.id == vocabulary_id).first()
    if not vocabulary:
        raise HTTPException(status_code=404, detail=f"Vocabulary {vocabulary_id} not found")
    
    # Validate group exists if group_id provided
    if group_id is not None:
        group = db.query(VocabularyGroup).filter(VocabularyGroup.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    
    # Update group assignment
    old_group_id = vocabulary.group_id
    vocabulary.group_id = group_id
    db.commit()
    db.refresh(vocabulary)
    
    logger.info(f"Reassigned vocabulary '{vocabulary.name}' from group {old_group_id} to group {group_id}")
    
    return {
        "success": True,
        "message": f"Vocabulary '{vocabulary.name}' assigned to group",
        "vocabulary_id": vocabulary.id,
        "old_group_id": old_group_id,
        "new_group_id": group_id,
    }


@router.post("/vocabulary-groups")
def create_vocabulary_group(
    name: str,
    description: str | None = None,
    display_order: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    """
    Create a new vocabulary group.
    
    Args:
        name: Name of the group.
        description: Optional description.
        display_order: Display order (lower = shown first).
        
    Returns:
        Created group info.
    """
    # Check if group with this name already exists
    existing = (
        db.query(VocabularyGroup)
        .filter(VocabularyGroup.user_key == DEFAULT_USER_KEY)
        .filter(VocabularyGroup.name == name)
        .first()
    )
    
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Group '{name}' already exists",
        )
    
    # Create new group
    group = VocabularyGroup(
        user_key=DEFAULT_USER_KEY,
        name=name,
        description=description,
        display_order=display_order,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    
    logger.info(f"Created vocabulary group: {name}")
    
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "display_order": group.display_order,
        "created_at": group.created_at.isoformat(),
    }


@router.get("/vocabulary-groups")
def list_vocabulary_groups(db: Session = Depends(get_db)) -> list[dict]:
    """
    List vocabulary groups with their vocabularies and unit counts.
    
    Returns groups ordered by display_order, with vocabularies nested inside each group.
    Ungrouped vocabularies are returned in a special "Ungrouped" group at the end.
    """
    return get_vocabulary_groups(db)


@router.get("/vocabularies")
def list_vocabularies(db: Session = Depends(get_db)) -> list[dict]:
    """
    List vocabularies for the current (local) user.
    Always includes "Chat Vocabulary".
    """
    return _get_readonly_vocabularies(db, user_key=DEFAULT_USER_KEY)



# Supported file extensions
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}


@router.post("/pdfs/parse", response_model=PDFParseResponse)
async def parse_document(
    file: UploadFile = File(...),
    group_id: Optional[str] = Form(None),
    new_group_name: Optional[str] = Form(None),
    new_group_description: Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> PDFParseResponse:
    """
    Parse a document file (PDF or Word) and return units for review (without saving).
    
    Optionally validates units using AI if OpenAI is configured.
    
    Args:
        file: PDF or Word (.docx) file to parse.
        group_id: ID of existing group to assign to (optional).
        new_group_name: Name of new group to create (optional).
        new_group_description: Description of new group (optional).
        
    Returns:
        Parsed units with AI suggestions (if available).
    """
    # Validate file type
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required.")
    
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Accepted: PDF, DOCX. Got: {file_ext}",
        )
    
    # Save uploaded file to temp location
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = Path(tmp.name)
    
    try:
        # Parse document (PDF or Word)
        parser = PDFParser(source_filename=file.filename)
        result = parser.parse_file(tmp_path)
        
        # Build parsed units list and check for duplicates
        parsed_units = []
        duplicates_found = 0
        
        for i, unit in enumerate(result.units):
            # Check if this unit already exists in the database
            norm_text = normalize_text(unit.text)
            norm_translation = normalize_text(unit.translation)
            
            existing = db.query(LearningUnit).filter(
                LearningUnit.normalized_text == norm_text,
                LearningUnit.normalized_translation == norm_translation,
            ).first()
            
            is_duplicate = existing is not None
            if is_duplicate:
                duplicates_found += 1
            
            parsed_units.append(ParsedUnitItem(
                index=i,
                text=unit.text,
                translation=unit.translation,
                type=unit.type.value,
                part_of_speech=unit.part_of_speech,
                page_number=unit.page_number,
                context_sentence=unit.context_sentence if isinstance(getattr(unit, "context_sentence", None), str) else None,
                is_duplicate=is_duplicate,
            ))
        
        # Try AI validation
        validator = VocabularyValidator()
        ai_validated = False
        ai_error = None
        ai_tokens = None
        validation_summary = None
        
        if validator.is_available and parsed_units:
            units_for_validation = [
                {"text": u.text, "translation": u.translation}
                for u in parsed_units
            ]
            
            validation_result = await validator.validate_units(units_for_validation)
            
            ai_validated = validation_result.ai_available
            ai_error = validation_result.ai_error
            ai_tokens = validation_result.tokens_used
            
            # Merge suggestions into parsed units
            for i, suggestion in enumerate(validation_result.suggestions):
                if i < len(parsed_units):
                    parsed_units[i].suggested_text = suggestion.suggested_text
                    parsed_units[i].suggested_translation = suggestion.suggested_translation
                    parsed_units[i].has_spelling_error = suggestion.has_spelling_error
                    parsed_units[i].has_punctuation_error = suggestion.has_punctuation_error
                    parsed_units[i].confidence = suggestion.confidence
                    parsed_units[i].validation_level = suggestion.validation_level.value
                    parsed_units[i].validation_notes = suggestion.notes
            
            # Build validation summary
            summary = validation_result.summary
            validation_summary = ValidationSummary(
                clean=summary["clean"],
                safe_fix=summary["safe_fix"],
                review=summary["review"],
                manual=summary["manual"],
                auto_accepted=summary["auto_accepted"],
                needs_review=summary["needs_review"],
            )
        else:
            # No AI validation - all units are "clean" (no suggestions)
            validation_summary = ValidationSummary(
                clean=len(parsed_units),
                auto_accepted=len(parsed_units),
            )
        
        logger.info(
            f"Parsed {file.filename}: {len(parsed_units)} units, "
            f"{result.skipped_lines} skipped, {duplicates_found} duplicates, "
            f"AI validated: {ai_validated}"
        )
        
        # Convert group_id to int if provided
        parsed_group_id = None
        if group_id and group_id.isdigit():
            parsed_group_id = int(group_id)
        
        return PDFParseResponse(
            filename=file.filename,
            total_parsed=len(parsed_units),
            skipped_lines=result.skipped_lines,
            units=parsed_units,
            ai_validated=ai_validated,
            ai_error=ai_error,
            ai_tokens_used=ai_tokens,
            validation_summary=validation_summary,
            duplicates_found=duplicates_found,
            group_id=parsed_group_id,
            new_group_name=new_group_name,
            new_group_description=new_group_description,
        )
        
    except Exception as e:
        logger.error(f"Failed to parse PDF: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse PDF: {str(e)}",
        )
    finally:
        # Clean up temp file
        tmp_path.unlink(missing_ok=True)


@router.post("/pdfs/confirm", response_model=PDFConfirmResponse)
async def confirm_units(
    request: PDFConfirmRequest,
    db: Session = Depends(get_db),
) -> PDFConfirmResponse:
    """
    Save confirmed units to the database.
    
    Users can accept, edit, or reject each unit.
    Only accepted/edited units are saved.
    Auto-accepted units (clean/safe_fix) are applied automatically.
    
    SAFETY: This is the ONLY endpoint that writes vocabulary to the database.
    All units must have an explicit decision (accept/edit/reject).
    
    Args:
        request: Confirmation request with user decisions.
        db: Database session.
        
    Returns:
        Summary of saved units.
        
    Raises:
        HTTPException: If any units are missing decisions or have invalid actions.
    """
    # Build lookup for original units
    original_lookup = {u.index: u for u in request.original_units}
    confirmation_lookup = {c.index: c for c in request.units}
    
    # SAFETY CHECK: Ensure all original units have a corresponding decision
    missing_decisions = []
    invalid_actions = []
    
    for original in request.original_units:
        confirmation = confirmation_lookup.get(original.index)
        if not confirmation:
            missing_decisions.append(original.index)
        elif confirmation.action not in ("accept", "edit", "reject"):
            invalid_actions.append((original.index, confirmation.action))
    
    if missing_decisions:
        raise HTTPException(
            status_code=400,
            detail=f"Missing decisions for units: {missing_decisions}. All units must be explicitly confirmed or rejected.",
        )
    
    if invalid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid actions: {invalid_actions}. Valid actions are: accept, edit, reject.",
        )
    
    units_saved = 0
    units_rejected = 0
    units_auto_accepted = 0
    duplicates_skipped = 0
    
    # Handle group assignment
    target_group_id = request.group_id
    
    # Create new group if requested
    if request.new_group_name:
        # Check if group already exists
        existing_group = db.query(VocabularyGroup).filter(
            VocabularyGroup.user_key == DEFAULT_USER_KEY,
            VocabularyGroup.name == request.new_group_name
        ).first()
        
        if existing_group:
            target_group_id = existing_group.id
        else:
            # Get max display_order
            max_order = db.query(func.max(VocabularyGroup.display_order)).scalar() or 0
            
            new_group = VocabularyGroup(
                user_key=DEFAULT_USER_KEY,
                name=request.new_group_name,
                description=request.new_group_description,
                display_order=max_order + 1
            )
            db.add(new_group)
            db.flush()  # Get the ID without committing
            target_group_id = new_group.id
            logger.info(f"Created new vocabulary group: {request.new_group_name}")
    
    # Resolve vocabulary for this upload (deterministic: filename -> vocabulary)
    vocab = _get_or_create_vocabulary(db, user_key=DEFAULT_USER_KEY, name=request.filename)
    
    # Assign vocabulary to group
    if target_group_id and vocab.group_id != target_group_id:
        vocab.group_id = target_group_id
        logger.info(f"Assigned vocabulary '{request.filename}' to group ID {target_group_id}")
    
    # Track normalized pairs added in this batch to prevent duplicates within the batch
    batch_normalized_pairs = set()
    
    for confirmation in request.units:
        if confirmation.action == "reject":
            units_rejected += 1
            continue
        
        original = original_lookup.get(confirmation.index)
        if not original:
            continue
        
        # Skip duplicates by default
        if original.is_duplicate:
            duplicates_skipped += 1
            logger.debug(f"Skipping duplicate: {original.text}")
            continue
        
        # Track auto-accepted units
        level = original.validation_level
        if level in ("clean", "safe_fix") and confirmation.action == "accept":
            units_auto_accepted += 1
        
        # Determine final values
        if confirmation.action == "edit":
            final_text = confirmation.text or original.text
            final_translation = confirmation.translation or original.translation
        elif confirmation.text or confirmation.translation:
            # Accept with explicit values (for safe_fix auto-applied)
            final_text = confirmation.text or original.suggested_text or original.text
            final_translation = confirmation.translation or original.suggested_translation or original.translation
        else:  # accept
            # Use suggestion if available, otherwise original
            final_text = original.suggested_text or original.text
            final_translation = original.suggested_translation or original.translation
        
        # Determine unit type from text
        unit_type = _detect_unit_type(final_text)
        
        # Compute normalized versions for duplicate detection
        norm_text = normalize_text(final_text)
        norm_translation = normalize_text(final_translation)
        
        # Create normalized pair for duplicate checking
        normalized_pair = (norm_text, norm_translation)
        
        # Check for duplicates within the current batch
        if normalized_pair in batch_normalized_pairs:
            duplicates_skipped += 1
            logger.debug(f"Skipping duplicate (within batch): {final_text}")
            continue
        
        # Double-check for duplicates in database (in case of race conditions or missed detections)
        existing = db.query(LearningUnit).filter(
            LearningUnit.normalized_text == norm_text,
            LearningUnit.normalized_translation == norm_translation,
        ).first()
        
        if existing:
            duplicates_skipped += 1
            logger.debug(f"Skipping duplicate (DB check): {final_text}")
            continue
        
        # Add to batch tracking set
        batch_normalized_pairs.add(normalized_pair)
        
        # Create unit in database with normalized columns
        db_unit = LearningUnit(
            text=final_text,
            type=unit_type,
            part_of_speech=original.part_of_speech,
            translation=final_translation,
            context_sentence=original.context_sentence if isinstance(getattr(original, "context_sentence", None), str) else None,
            source_pdf=request.filename,
            vocabulary_id=vocab.id,
            page_number=original.page_number,
            normalized_text=norm_text,
            normalized_translation=norm_translation,
        )
        db.add(db_unit)
        units_saved += 1
    
    # Commit with error handling for edge cases (e.g., race conditions)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        # Check if it's a unique constraint violation
        if "UNIQUE constraint failed" in str(e.orig) and "normalized" in str(e.orig):
            logger.warning(
                f"IntegrityError during commit (likely duplicate): {e.orig}. "
                f"Rolled back transaction. Some units may have been duplicates."
            )
            raise HTTPException(
                status_code=409,
                detail="Duplicate entries detected. Some units already exist in the database. Please try again.",
            )
        else:
            # Re-raise if it's a different integrity error
            raise
    
    logger.info(
        f"Confirmed {request.filename}: {units_saved} saved ({units_auto_accepted} auto-accepted), "
        f"{units_rejected} rejected, {duplicates_skipped} duplicates skipped"
    )
    
    return PDFConfirmResponse(
        filename=request.filename,
        units_saved=units_saved,
        units_rejected=units_rejected,
        units_auto_accepted=units_auto_accepted,
        duplicates_skipped=duplicates_skipped,
        message=f"Saved {units_saved} units from {request.filename}",
    )


def _detect_unit_type(text: str) -> UnitType:
    """Detect unit type from text content."""
    text = text.strip()
    
    # Check for sentence-ending punctuation
    if text and text[-1] in ".!?":
        return UnitType.SENTENCE
    
    # Check for spaces (indicating multiple words)
    if " " in text:
        return UnitType.PHRASE
    
    return UnitType.WORD


@router.get("/units", response_model=PaginatedUnitsResponse)
def get_units(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    unit_type: Optional[str] = Query(None, description="Filter by type: word, phrase, sentence"),
    source_pdf: Optional[str] = Query(None, description="Filter by source PDF filename"),
    group_id: Optional[str] = Query(None, description="Filter by vocabulary group id, or 'ungrouped'"),
    search: Optional[str] = Query(None, description="Search in text and translation"),
    sort_by: str = Query("text", description="Sort by: text, translation, confidence, created_at"),
    db: Session = Depends(get_db),
) -> PaginatedUnitsResponse:
    """
    Get paginated list of learning units.

    Args:
        page: Page number (1-indexed).
        page_size: Number of items per page.
        unit_type: Optional filter by unit type.
        source_pdf: Optional filter by source PDF.
        group_id: Optional filter by vocabulary group (integer id or 'ungrouped').
        search: Optional search term to filter by text or translation.
        sort_by: Field to sort by (default: text for alphabetical).
        db: Database session.

    Returns:
        Paginated list of learning units.
    """
    query = db.query(LearningUnit)

    # Apply filters
    if unit_type:
        query = query.filter(LearningUnit.type == unit_type)
    if source_pdf:
        query = query.filter(LearningUnit.source_pdf == source_pdf)
    if group_id:
        if group_id == "ungrouped":
            ungrouped_names = [
                v.name for v in
                db.query(Vocabulary.name).filter(Vocabulary.group_id.is_(None)).all()
            ]
            if ungrouped_names:
                query = query.filter(LearningUnit.source_pdf.in_(ungrouped_names))
            else:
                query = query.filter(False)
        else:
            group_vocab_names = [
                v.name for v in
                db.query(Vocabulary.name).filter(Vocabulary.group_id == int(group_id)).all()
            ]
            if group_vocab_names:
                query = query.filter(LearningUnit.source_pdf.in_(group_vocab_names))
            else:
                query = query.filter(False)
    
    # Apply search filter (case-insensitive search in text and translation)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                LearningUnit.text.ilike(search_term),
                LearningUnit.translation.ilike(search_term)
            )
        )

    # Apply sorting (alphabetical by text is default)
    if sort_by == "translation":
        query = query.order_by(func.lower(LearningUnit.translation))
    elif sort_by == "created_at":
        query = query.order_by(LearningUnit.created_at.desc())
    else:  # default: text (alphabetical)
        query = query.order_by(func.lower(LearningUnit.text))

    # Get total count
    total = query.count()

    # Calculate pagination
    pages = (total + page_size - 1) // page_size if total > 0 else 1
    offset = (page - 1) * page_size

    # Get items
    items = query.offset(offset).limit(page_size).all()
    
    return PaginatedUnitsResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/pdfs/sources")
def get_pdf_sources(
    db: Session = Depends(get_db),
) -> list[dict]:
    """
    Get list of available PDF sources with unit counts and mastery stats.
    
    Returns:
        List of PDFs with filename, unit count, and mastery percentage.
    """
    results = (
        db.query(
            LearningUnit.source_pdf,
            func.count(LearningUnit.id).label("unit_count"),
        )
        .group_by(LearningUnit.source_pdf)
        .order_by(LearningUnit.source_pdf)
        .all()
    )
    
    now = utc_now()
    sources = []
    
    for r in results:
        # Get units for this source with progress
        units = (
            db.query(LearningUnit)
            .filter(LearningUnit.source_pdf == r.source_pdf)
            .options(joinedload(LearningUnit.progress))
            .all()
        )
        
        # Compute mastery stats
        mastery_stats = compute_mastery_stats(units, now)
        
        sources.append({
            "filename": r.source_pdf,
            "unit_count": r.unit_count,
            "mastered_pct": mastery_stats["mastered_pct"],
            "is_fully_mastered": mastery_stats["mastered_pct"] == 100.0,
        })
    
    return sources


@router.post("/units", response_model=LearningUnitResponse)
def create_unit(
    text: Optional[str] = Query(None, description="Polish text"),
    translation: Optional[str] = Query(None, description="Translation"),
    vocabulary_id: Optional[int] = Query(None, description="Vocabulary ID (optional)"),
    payload: Optional[CreateUnitRequest] = Body(None),
    db: Session = Depends(get_db),
) -> LearningUnitResponse:
    """
    Manually add a single vocabulary unit.
    
    Args:
        text: Polish word/phrase/sentence.
        translation: Translation.
        db: Database session.
        
    Returns:
        Created learning unit.
    """
    # Support both legacy query params and JSON body payload.
    if payload is not None:
        text = payload.text
        translation = payload.translation
        vocabulary_id = payload.vocabulary_id

    if text is None or translation is None:
        logger.warning(f"Create unit failed: text={text}, translation={translation}")
        raise HTTPException(status_code=400, detail="Both text and translation are required.")

    text = text.strip()
    translation = translation.strip()
    
    if not text or not translation:
        logger.warning(f"Create unit failed: empty text or translation after strip")
        raise HTTPException(
            status_code=400,
            detail="Both text and translation are required.",
        )
    
    # Detect unit type
    if text[-1] in ".!?":
        unit_type = UnitType.SENTENCE
    elif " " in text:
        unit_type = UnitType.PHRASE
    else:
        unit_type = UnitType.WORD
    
    # Resolve vocabulary first (needed for per-vocabulary duplicate check)
    # - If vocabulary_id provided: validate it exists for the current user and use it.
    # - If missing/null: use per-user "Chat Vocabulary" (created once and reused).
    if vocabulary_id is not None:
        vocab = (
            db.query(Vocabulary)
            .filter(Vocabulary.id == vocabulary_id)
            .filter(Vocabulary.user_key == DEFAULT_USER_KEY)
            .first()
        )
        if not vocab:
            raise HTTPException(status_code=400, detail="Selected vocabulary not found.")
    else:
        vocab = _ensure_user_vocabulary(db, user_key=DEFAULT_USER_KEY)
    
    # Check for duplicates globally (database has unique constraint on text+translation)
    norm_text = normalize_text(text)
    norm_translation = normalize_text(translation)
    
    existing = db.query(LearningUnit).filter(
        LearningUnit.normalized_text == norm_text,
        LearningUnit.normalized_translation == norm_translation,
    ).first()
    
    if existing:
        if existing.vocabulary_id == vocab.id:
            logger.info(f"Duplicate in same vocab: '{text}' already exists in '{vocab.name}' (ID: {existing.id})")
            raise HTTPException(
                status_code=400,
                detail=f"This word already exists in this vocabulary (ID: {existing.id}).",
            )
        else:
            logger.info(f"Duplicate in different vocab: '{text}' exists in '{existing.source_pdf}' (ID: {existing.id})")
            raise HTTPException(
                status_code=400,
                detail=f"This word already exists in '{existing.source_pdf}' (ID: {existing.id}). "
                       f"The same text+translation cannot exist in multiple vocabularies.",
            )

    # Create unit with normalized columns
    db_unit = LearningUnit(
        text=text,
        type=unit_type,
        translation=translation,
        # Keep legacy source grouping deterministic and explicit.
        # This ensures existing UI filters by source_pdf continue to work.
        source_pdf=vocab.name,
        vocabulary_id=vocab.id,
        normalized_text=norm_text,
        normalized_translation=norm_translation,
    )
    db.add(db_unit)
    
    try:
        db.commit()
        db.refresh(db_unit)
    except IntegrityError as e:
        db.rollback()
        # Check if this is a duplicate constraint violation
        if "UNIQUE constraint failed" in str(e):
            # Find the existing entry
            existing = db.query(LearningUnit).filter(
                LearningUnit.normalized_text == norm_text,
                LearningUnit.normalized_translation == norm_translation,
            ).first()
            
            if existing:
                logger.info(f"Global duplicate detected: '{text}' already exists in '{existing.source_pdf}' (ID: {existing.id})")
                raise HTTPException(
                    status_code=400,
                    detail=f"This word already exists in '{existing.source_pdf}' (ID: {existing.id}). "
                           f"The same text+translation combination cannot exist in multiple vocabularies."
                )
        
        logger.error(f"Database error creating unit: {e}")
        raise HTTPException(status_code=500, detail="Database error while creating unit.")
    
    logger.info(f"Created unit: '{text}' - '{translation}' (ID: {db_unit.id}, vocab: {vocab.name})")
    
    return db_unit


class QuickAddRequest(BaseModel):
    text: str
    translation: str


@router.post("/units/inbox", response_model=LearningUnitResponse)
def quick_add_to_inbox(
    payload: QuickAddRequest,
    db: Session = Depends(get_db),
) -> LearningUnitResponse:
    """
    Quick-add a word directly to the Inbox vocabulary.

    No vocabulary selection required — words land in the Inbox and can be
    triaged to the correct vocabulary later via PUT /api/units/{id}/move.
    """
    text = payload.text.strip()
    translation = payload.translation.strip()
    suggested_base = guess_base_form(text)

    if suggested_base != text:
        logger.info("base form suggestion: %s -> %s", text, suggested_base)

    if not text or not translation:
        raise HTTPException(status_code=400, detail="Both text and translation are required.")

    if text[-1] in ".!?":
        unit_type = UnitType.SENTENCE
    elif " " in text:
        unit_type = UnitType.PHRASE
    else:
        unit_type = UnitType.WORD

    inbox = _ensure_inbox_vocabulary(db, user_key=DEFAULT_USER_KEY)

    norm_text = normalize_text(text)
    norm_translation = normalize_text(translation)

    existing = db.query(LearningUnit).options(
        joinedload(LearningUnit.vocabulary)
    ).filter(
        LearningUnit.normalized_text == norm_text,
        LearningUnit.normalized_translation == norm_translation,
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "exists",
                "unit_id": existing.id,
                "vocabulary_id": existing.vocabulary_id,
                "vocabulary_name": existing.vocabulary.name if existing.vocabulary else None,
            },
        )

    db_unit = LearningUnit(
        text=text,
        type=unit_type,
        translation=translation,
        source_pdf=INBOX_VOCAB_NAME,
        vocabulary_id=inbox.id,
        normalized_text=norm_text,
        normalized_translation=norm_translation,
    )
    db.add(db_unit)

    try:
        db.commit()
        db.refresh(db_unit)
    except IntegrityError:
        db.rollback()
        existing = db.query(LearningUnit).options(
            joinedload(LearningUnit.vocabulary)
        ).filter(
            LearningUnit.normalized_text == norm_text,
            LearningUnit.normalized_translation == norm_translation,
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "exists",
                    "unit_id": existing.id,
                    "vocabulary_id": existing.vocabulary_id,
                    "vocabulary_name": existing.vocabulary.name if existing.vocabulary else None,
                },
            )
        raise HTTPException(status_code=400, detail={"status": "exists"})

    logger.info(f"Quick-added to Inbox: '{text}' — '{translation}' (ID: {db_unit.id})")
    return db_unit


class MoveUnitRequest(BaseModel):
    vocabulary_id: int


@router.put("/units/{unit_id}/move", response_model=LearningUnitResponse)
def move_unit_to_vocabulary(
    unit_id: int,
    payload: MoveUnitRequest,
    db: Session = Depends(get_db),
) -> LearningUnitResponse:
    """
    Move a unit from its current vocabulary (e.g. Inbox) to a different one.

    Updates both vocabulary_id and source_pdf so all existing filters continue
    to work correctly.
    """
    unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
    if not unit:
        raise HTTPException(status_code=404, detail=f"Unit {unit_id} not found.")

    target = (
        db.query(Vocabulary)
        .filter(Vocabulary.id == payload.vocabulary_id)
        .filter(Vocabulary.user_key == DEFAULT_USER_KEY)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail=f"Vocabulary {payload.vocabulary_id} not found.")

    if unit.vocabulary_id == target.id:
        return unit  # already there — idempotent

    unit.vocabulary_id = target.id
    unit.source_pdf = target.name
    db.commit()
    db.refresh(unit)

    logger.info(f"Moved unit {unit_id} ('{unit.text}') to vocabulary '{target.name}'")
    return unit


@router.get("/units/inbox")
def get_inbox_units(db: Session = Depends(get_db)) -> list[dict]:
    """Return all units currently in the Inbox vocabulary."""
    inbox = (
        db.query(Vocabulary)
        .filter(
            Vocabulary.user_key == DEFAULT_USER_KEY,
            Vocabulary.name == INBOX_VOCAB_NAME,
        )
        .first()
    )
    if inbox is None:
        return []

    units = (
        db.query(LearningUnit)
        .filter(LearningUnit.vocabulary_id == inbox.id)
        .order_by(LearningUnit.created_at.desc())
        .all()
    )

    return [
        {
            "id": u.id,
            "text": u.text,
            "translation": u.translation,
            "type": u.type.value if u.type else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in units
    ]


@router.get("/vocabulary/known-tokens")
def get_known_tokens(db: Session = Depends(get_db)) -> dict:
    """Return normalized tokens for all units the user has been introduced to."""
    rows = (
        db.query(LearningUnit.normalized_text)
        .join(LearningProgress, LearningProgress.unit_id == LearningUnit.id)
        .filter(LearningProgress.introduced_at.isnot(None))
        .distinct()
        .all()
    )
    return {"tokens": [r.normalized_text for r in rows if r.normalized_text]}


@router.get("/units/{unit_id}", response_model=LearningUnitResponse)
def get_unit(
    unit_id: int,
    db: Session = Depends(get_db),
) -> LearningUnitResponse:
    """
    Get a single learning unit by ID.
    
    Args:
        unit_id: ID of the learning unit.
        db: Database session.
        
    Returns:
        Learning unit details.
        
    Raises:
        HTTPException: If unit not found.
    """
    unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
    
    if not unit:
        raise HTTPException(
            status_code=404,
            detail=f"Learning unit with ID {unit_id} not found.",
        )
    
    return unit


@router.put("/units/{unit_id}", response_model=LearningUnitResponse)
def update_unit(
    unit_id: int,
    update_data: LearningUnitUpdate,
    db: Session = Depends(get_db),
) -> LearningUnitResponse:
    """
    Update a learning unit's text and/or translation.
    
    Args:
        unit_id: ID of the learning unit to update.
        update_data: Fields to update (text, translation).
        db: Database session.
        
    Returns:
        Updated learning unit.
        
    Raises:
        HTTPException: If unit not found.
    """
    unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
    
    if not unit:
        raise HTTPException(
            status_code=404,
            detail=f"Learning unit with ID {unit_id} not found.",
        )
    
    # Update only provided fields
    if update_data.text is not None:
        unit.text = update_data.text.strip()
    if update_data.translation is not None:
        unit.translation = update_data.translation.strip()
    
    db.commit()
    db.refresh(unit)
    
    logger.info(f"Updated learning unit {unit_id}")
    
    return unit


@router.delete("/units/{unit_id}")
def delete_unit(
    unit_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """
    Delete a learning unit by ID.
    
    Args:
        unit_id: ID of the learning unit to delete.
        db: Database session.
        
    Returns:
        Confirmation message.
        
    Raises:
        HTTPException: If unit not found.
    """
    unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
    
    if not unit:
        raise HTTPException(
            status_code=404,
            detail=f"Learning unit with ID {unit_id} not found.",
        )
    
    db.delete(unit)
    db.commit()
    
    logger.info(f"Deleted learning unit {unit_id}")
    
    return {"message": f"Learning unit {unit_id} deleted successfully."}


@router.delete("/pdfs/{filename}")
def delete_pdf_units(
    filename: str,
    db: Session = Depends(get_db),
) -> dict:
    """
    Delete ALL learning units from a specific PDF source.
    
    Use this to remove corrupted uploads and re-upload clean data.
    
    Args:
        filename: Name of the PDF file whose units should be deleted.
        db: Database session.
        
    Returns:
        Confirmation message with count of deleted units.
    """
    # Count units to delete
    count = db.query(LearningUnit).filter(LearningUnit.source_pdf == filename).count()
    
    if count == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No units found from PDF '{filename}'.",
        )
    
    # Delete all units from this PDF
    db.query(LearningUnit).filter(LearningUnit.source_pdf == filename).delete()
    db.commit()
    
    logger.info(f"Deleted {count} units from PDF '{filename}'")
    
    return {
        "message": f"Deleted {count} units from '{filename}'",
        "units_deleted": count,
        "filename": filename,
    }
