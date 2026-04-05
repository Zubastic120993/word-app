"""Pydantic schemas for learning units and API responses."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.learning_unit import UnitType


class LearningUnitBase(BaseModel):
    """Base schema for learning unit data."""
    text: str
    type: UnitType
    part_of_speech: Optional[str] = None
    translation: str
    context_sentence: Optional[str] = None
    source_pdf: str
    page_number: Optional[int] = None
    lesson_title: Optional[str] = None


class LearningUnitCreate(LearningUnitBase):
    """Schema for creating a new learning unit."""
    pass


class LearningUnitUpdate(BaseModel):
    """Schema for updating a learning unit (text and translation only)."""
    text: Optional[str] = None
    translation: Optional[str] = None


class LearningProgressResponse(BaseModel):
    """Schema for learning progress in API responses."""
    times_seen: int = 0
    times_correct: int = 0
    times_failed: int = 0
    confidence_score: float = 0.0
    last_seen: Optional[datetime] = None
    next_review_at: Optional[datetime] = None  # SRS-lite scheduling
    introduced_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class LearningUnitResponse(LearningUnitBase):
    """Schema for learning unit in API responses."""
    id: int
    created_at: datetime
    progress: Optional[LearningProgressResponse] = None
    
    model_config = ConfigDict(from_attributes=True)


class PDFUploadResponse(BaseModel):
    """Schema for PDF upload response (direct upload without review)."""
    filename: str
    units_created: int
    units_skipped: int
    message: str


class PaginatedUnitsResponse(BaseModel):
    """Schema for paginated list of learning units."""
    items: list[LearningUnitResponse]
    total: int
    page: int
    page_size: int
    pages: int


# ============================================
# Two-step upload flow (parse → review → save)
# ============================================

class ParsedUnitItem(BaseModel):
    """A single parsed unit with optional AI suggestions."""
    index: int
    text: str
    translation: str
    type: str
    part_of_speech: Optional[str] = None
    page_number: Optional[int] = None
    context_sentence: Optional[str] = None
    
    # AI validation suggestions
    suggested_text: Optional[str] = None
    suggested_translation: Optional[str] = None
    has_spelling_error: bool = False
    has_punctuation_error: bool = False
    confidence: float = 1.0  # AI confidence in suggestion (0.0-1.0)
    validation_level: str = "clean"  # clean, safe_fix, review, manual
    validation_notes: Optional[str] = None
    
    # Duplicate detection
    is_duplicate: bool = False  # True if this unit already exists in database


class ValidationSummary(BaseModel):
    """Summary of validation results by level."""
    clean: int = 0
    safe_fix: int = 0
    review: int = 0
    manual: int = 0
    auto_accepted: int = 0
    needs_review: int = 0


class PDFParseResponse(BaseModel):
    """Response from parsing a PDF (before saving)."""
    filename: str
    total_parsed: int
    skipped_lines: int
    units: list[ParsedUnitItem]
    
    # AI validation status
    ai_validated: bool = False
    ai_error: Optional[str] = None
    ai_tokens_used: Optional[int] = None
    
    # Validation summary
    validation_summary: Optional[ValidationSummary] = None
    
    # Duplicate detection
    duplicates_found: int = 0  # Number of units that already exist in database
    
    # Group assignment
    group_id: Optional[int] = None
    new_group_name: Optional[str] = None
    new_group_description: Optional[str] = None


class UnitConfirmation(BaseModel):
    """User decision for a single unit."""
    index: int
    action: str  # "accept", "edit", "reject"
    text: Optional[str] = None  # Final text (for edit)
    translation: Optional[str] = None  # Final translation (for edit)


class PDFConfirmRequest(BaseModel):
    """Request to save confirmed units."""
    filename: str
    units: list[UnitConfirmation]
    
    # Original parsed data (for reference)
    original_units: list[ParsedUnitItem]
    
    # Group assignment
    group_id: Optional[int] = None
    new_group_name: Optional[str] = None
    new_group_description: Optional[str] = None


class PDFConfirmResponse(BaseModel):
    """Response from saving confirmed units."""
    filename: str
    units_saved: int
    units_rejected: int
    units_auto_accepted: int = 0
    duplicates_skipped: int = 0  # Units skipped due to duplicate detection
    message: str
