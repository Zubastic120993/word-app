"""Pydantic schemas for data export/import functionality."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.learning_unit import UnitType


class ExportLearningUnit(BaseModel):
    """Schema for exported learning unit."""
    id: int
    text: str
    type: UnitType
    part_of_speech: Optional[str] = None
    translation: str
    source_pdf: str
    vocabulary_id: Optional[int] = None
    page_number: Optional[int] = None
    lesson_title: Optional[str] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class ExportLearningProgress(BaseModel):
    """Schema for exported learning progress."""
    id: int
    unit_id: int
    times_seen: int
    times_correct: int
    times_failed: int
    confidence_score: float
    last_seen: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)


class ExportLearningSession(BaseModel):
    """Schema for exported learning session."""
    id: int
    created_at: datetime
    locked: bool
    completed: bool
    completed_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)


class ExportSessionUnit(BaseModel):
    """Schema for exported session unit."""
    id: int
    session_id: int
    unit_id: int
    position: int
    answered: bool
    is_correct: Optional[bool] = None
    answered_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)


class ExportSettings(BaseModel):
    """Schema for exported settings."""
    id: int
    offline_mode: bool
    ai_provider: str
    ollama_model: Optional[str] = None
    strict_mode: bool
    source_language: str
    target_language: str
    
    model_config = ConfigDict(from_attributes=True)


class ExportVocabularyGroup(BaseModel):
    """Schema for exported vocabulary group."""
    id: int
    user_key: str
    name: str
    description: Optional[str] = None
    display_order: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExportVocabulary(BaseModel):
    """Schema for exported vocabulary."""
    id: int
    user_key: str
    name: str
    group_id: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExportAudioAsset(BaseModel):
    """Schema for exported audio asset."""
    id: int
    unit_id: int
    engine: str
    voice: str
    language: str
    audio_hash: str
    file_path: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExportPracticeEvent(BaseModel):
    """Schema for exported practice event."""
    id: int
    created_at: datetime
    event_type: str
    theme: Optional[str] = None
    payload: dict

    model_config = ConfigDict(from_attributes=True)


class ExportMetadata(BaseModel):
    """Metadata for export file."""
    schema_version: str = "1.0"
    app_version: str
    export_timestamp: datetime
    source_language: str
    target_language: str
    session_size: int


class ExportData(BaseModel):
    """Complete export schema containing all learning data."""
    metadata: ExportMetadata
    settings: Optional[ExportSettings] = None
    learning_units: list[ExportLearningUnit] = Field(default_factory=list)
    learning_progress: list[ExportLearningProgress] = Field(default_factory=list)
    learning_sessions: list[ExportLearningSession] = Field(default_factory=list)
    session_units: list[ExportSessionUnit] = Field(default_factory=list)
    vocabularies: list[ExportVocabulary] = Field(default_factory=list)
    vocabulary_groups: list[ExportVocabularyGroup] = Field(default_factory=list)
    audio_assets: list[ExportAudioAsset] = Field(default_factory=list)
    practice_events: list[ExportPracticeEvent] = Field(default_factory=list)
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "metadata": {
                    "app_version": "0.1.0",
                    "export_timestamp": "2025-01-11T12:00:00Z",
                    "source_language": "Polish",
                    "target_language": "English",
                    "session_size": 20
                },
                "settings": {
                    "id": 1,
                    "offline_mode": True,
                    "ai_provider": "ollama",
                    "ollama_model": "llama3.2",
                    "strict_mode": True,
                    "source_language": "Polish",
                    "target_language": "English"
                },
                "learning_units": [],
                "learning_progress": [],
                "learning_sessions": [],
                "session_units": [],
                "vocabularies": [],
                "vocabulary_groups": [],
                "audio_assets": [],
                "practice_events": []
            }
        }
    )


class ImportValidationResult(BaseModel):
    """Result of import validation."""
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unit_count: int = 0
    session_count: int = 0


class ImportResponse(BaseModel):
    """Response schema for import operation."""
    success: bool
    message: str
    units_imported: int = 0
    sessions_imported: int = 0
    backup_created: bool = False
