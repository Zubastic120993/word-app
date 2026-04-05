"""Pydantic schemas for API validation."""

from app.schemas.learning_unit import (
    LearningUnitBase,
    LearningUnitCreate,
    LearningUnitResponse,
    LearningProgressResponse,
    PDFUploadResponse,
)
from app.schemas.session import (
    SessionResponse,
    SessionCreateResponse,
    SessionUnitResponse,
    AnswerRequest,
    AnswerResponse,
    SessionSummary,
    SessionListResponse,
)
from app.schemas.ai import (
    StudyModeRequest,
    StudyModeResponse,
    FreeChatRequest,
    FreeChatResponse,
    TranslateRequest,
    AIStatusResponse,
    ClearHistoryResponse,
)
from app.schemas.export_import import (
    ExportLearningUnit,
    ExportLearningProgress,
    ExportLearningSession,
    ExportSessionUnit,
    ExportSettings,
    ExportMetadata,
    ExportData,
    ImportValidationResult,
    ImportResponse,
)

__all__ = [
    "LearningUnitBase",
    "LearningUnitCreate",
    "LearningUnitResponse",
    "LearningProgressResponse",
    "PDFUploadResponse",
    "SessionResponse",
    "SessionCreateResponse",
    "SessionUnitResponse",
    "AnswerRequest",
    "AnswerResponse",
    "SessionSummary",
    "SessionListResponse",
    # AI schemas
    "StudyModeRequest",
    "StudyModeResponse",
    "FreeChatRequest",
    "FreeChatResponse",
    "TranslateRequest",
    "AIStatusResponse",
    "ClearHistoryResponse",
    # Export/Import schemas
    "ExportLearningUnit",
    "ExportLearningProgress",
    "ExportLearningSession",
    "ExportSessionUnit",
    "ExportSettings",
    "ExportMetadata",
    "ExportData",
    "ImportValidationResult",
    "ImportResponse",
]
