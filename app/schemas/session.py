"""Pydantic schemas for learning sessions."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.schemas.learning_unit import LearningUnitResponse


class StudyModeType(str, Enum):
    """Type of study mode for a session."""
    PASSIVE = "passive"
    RECALL = "recall"
    RECALL_AUDIO = "recall_audio"
    CLOZE = "cloze"


class EvaluationMode(str, Enum):
    """Mode for evaluating recall answers."""
    STRICT = "strict"    # Exact match after normalization
    LEXICAL = "lexical"  # Also ignores punctuation


class RecallResultType(str, Enum):
    """Result of active recall evaluation."""
    CORRECT = "correct"    # Exact or near-exact match
    PARTIAL = "partial"    # Minor typo (≤1 char), punctuation difference  
    FAILED = "failed"      # Incorrect answer


class SessionStatus(str, Enum):
    """Lifecycle status of a session."""
    CREATED = "created"
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class SessionUnitResponse(BaseModel):
    """Schema for a unit within a session."""
    id: int
    position: int
    answered: bool
    is_correct: Optional[bool] = None
    recall_result: Optional[RecallResultType] = None  # correct/partial/failed (recall mode)
    user_input: Optional[str] = None
    answered_at: Optional[datetime] = None
    unit: LearningUnitResponse
    study_mode: Optional[StudyModeType] = None  # Session's study mode (for UI convenience)
    has_audio: Optional[bool] = None  # True if Polish unit and ElevenLabs enabled
    is_stuck: bool = False
    theme_id: Optional[str] = None
    theme_name: Optional[str] = None
    selection_reason: Optional[str] = None  # follow_up | retry | due | weak | new | review (persisted when known)
    exercise_type: str = "recall"
    cloze_prompt: Optional[str] = None
    context_sentence_translation: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SessionResponse(BaseModel):
    """Schema for learning session in API responses."""
    id: int
    created_at: datetime
    started_at: datetime  # Alias for created_at for semantic clarity
    mode: StudyModeType
    status: SessionStatus
    locked: bool
    completed: bool
    completed_at: Optional[datetime] = None
    abandoned_at: Optional[datetime] = None
    total_units: int
    answered_units: int
    correct_count: int
    partial_count: int
    failed_count: int
    units: list[SessionUnitResponse]
    
    model_config = ConfigDict(from_attributes=True)


class SessionCreateRequest(BaseModel):
    """Schema for session creation request."""
    mode: StudyModeType = StudyModeType.PASSIVE
    source_pdfs: Optional[list[str]] = None  # Filter by PDF files (None = all)
    theme: Optional[str] = None  # Optional curriculum theme filter
    lesson_id: Optional[int] = None  # Optional lesson-first filter
    weak_only: bool = False  # Only include units with confidence <50%
    due_only: bool = False  # Only include units with next_review_at <= now
    # Passive: skip filling up to 70% with due words first; use mixed buckets only (Home "new words" CTA).
    new_words_focus: bool = False
    override_cap: bool = False
    override_daily_cap: bool = False
    follow_up_session_id: Optional[int] = None
    retry_failed_only: bool = False
    # Structured PL–UA lesson scope (passive); bypasses czytaj filename lesson map.
    curriculum_mode: Optional[str] = None
    # Debug tracing for duplicate-create investigation.
    client_page_instance_id: Optional[str] = None
    client_post_seq: Optional[int] = None
    client_debug_tag: Optional[str] = None


class SessionCreateResponse(BaseModel):
    """Schema for session creation response."""
    session_id: int
    mode: StudyModeType
    units_count: int
    message: str
    fallback_notice: Optional[str] = None
    # When due-only run is shorter than settings.session_size (few due in scope vs daily cap).
    short_session_note: Optional[str] = None
    # Adaptive readiness gate: "weak" | "fatigue" | "accuracy" | None (ready for new words)
    session_reason: Optional[str] = None


class AnswerRequest(BaseModel):
    """
    Schema for submitting an answer.
    
    For passive mode: is_correct is required (user self-assessment).
    For recall mode: user_input is required (backend evaluates correctness).
    """
    unit_position: int  # 1 to 50 by default
    is_correct: Optional[bool] = None  # Required for passive mode
    user_input: Optional[str] = None   # Required for recall mode


class AnswerResponse(BaseModel):
    """Schema for answer submission response."""
    session_id: int
    unit_position: int
    is_correct: bool
    recall_result: Optional[RecallResultType] = None  # correct/partial/failed (recall mode only)
    user_input: Optional[str] = None           # Echo of user's input (recall mode)
    expected_answer: Optional[str] = None      # Correct answer (recall mode)
    evaluation_mode: Optional[EvaluationMode] = None  # Mode used for evaluation
    punctuation_only_mistake: bool = False     # True if only punctuation differs
    session_completed: bool
    correct_count: int
    answered_count: int
    total_units: int
    message: str


class SessionSummary(BaseModel):
    """Schema for session list summary."""
    id: int
    created_at: datetime
    started_at: datetime  # Alias for created_at
    mode: StudyModeType
    status: SessionStatus
    completed: bool
    completed_at: Optional[datetime] = None
    abandoned_at: Optional[datetime] = None
    answered_units: int
    correct_count: int
    partial_count: int
    failed_count: int
    
    model_config = ConfigDict(from_attributes=True)


class SessionListResponse(BaseModel):
    """Schema for listing sessions."""
    sessions: list[SessionSummary]
    total: int


class VocabularyCount(BaseModel):
    """Count of units from a specific vocabulary in a session."""
    name: str
    count: int


class SessionHistoryItem(BaseModel):
    """Schema for a session in the history list."""
    session_id: int
    date: datetime  # completed_at for completed sessions, started_at otherwise
    mode: StudyModeType
    total_units: int
    correct_count: int
    partial_count: int
    failed_count: int
    status: SessionStatus
    vocabularies: list[VocabularyCount] = []  # Vocabularies studied in this session
    
    model_config = ConfigDict(from_attributes=True)


class HistorySummary(BaseModel):
    """Summary statistics for the history page."""
    study_streak_days: int
    words_this_week: int
    recall_accuracy_7d: Optional[float] = None  # Recall mode only, last 7 days
    weak_words_count: int  # Words with confidence <50%


class SessionHistoryResponse(BaseModel):
    """Schema for session history endpoint response."""
    sessions: list[SessionHistoryItem]
    total: int
    limit: int
    offset: int
    summary: HistorySummary
