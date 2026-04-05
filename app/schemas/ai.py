"""Pydantic schemas for AI endpoints."""

from typing import Any, Optional
from pydantic import BaseModel, Field


class StudyModeRequest(BaseModel):
    """Request for Study Mode AI."""
    message: str = Field(..., min_length=1, description="User message")
    session_id: Optional[int] = Field(None, description="Current learning session ID")
    include_learned: bool = Field(True, description="Include previously learned vocabulary")
    validate_output: bool = Field(True, description="Validate AI output against vocabulary")


class StudyModeResponse(BaseModel):
    """Response from Study Mode AI."""
    content: str
    user_correct: bool
    ai_vocab_valid: bool
    unknown_words: list[str]
    warning: Optional[str] = None
    session_id: Optional[int] = None
    provider: str
    model: str


class FreeChatRequest(BaseModel):
    """Request for Free Chat AI."""
    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="User message",
    )
    scenario: Optional[str] = Field(
        default="free",
        description="Conversation scenario (free, restaurant, hotel, doctor, job_interview)",
    )
    temperature: float = Field(0.7, ge=0.0, le=2.0, description="Sampling temperature")
    corrections_enabled: bool = Field(
        default=False,
        description="Enable structured correction mode"
    )
    theme: Optional[str] = Field(
        default=None,
        description="Optional active curriculum theme",
    )
    session_vocab: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Vocab from a completed study session — injected into system prompt on first message only",
    )


class FreeChatResponse(BaseModel):
    """Response from Free Chat AI."""
    content: str
    provider: str
    model: str
    tokens_used: Optional[int] = None
    corrections: Optional[list[dict]] = None
    progress: Optional[dict] = None


class TranslateRequest(BaseModel):
    """Request for translation."""
    text: str = Field(..., min_length=1, description="Text to translate")
    source_language: str = Field("Polish", description="Source language")
    target_language: str = Field("English", description="Target language")


class AIStatusResponse(BaseModel):
    """AI provider status response."""
    available: bool
    provider: str
    model: Optional[str] = None
    error: Optional[str] = None
    details: dict = {}


class ClearHistoryResponse(BaseModel):
    """Response for clearing conversation history."""
    message: str
    mode: str


class RetryValidationRequest(BaseModel):
    original_sentence: str
    correct_sentence: str
    user_attempt: str
    attempt_number: Optional[int] = None
    theme: Optional[str] = None


class RetryValidationResponse(BaseModel):
    is_correct: bool
    feedback: Optional[str] = None
