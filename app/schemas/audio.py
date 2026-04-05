"""Pydantic schemas for audio API."""

from pydantic import BaseModel


class VoiceOverrideRequest(BaseModel):
    """Request schema for voice override."""
    voice: str  # Voice ID
    confirm: bool  # If false, preview only; if true, save to DB


class SentenceAudioRequest(BaseModel):
    """Request schema for cloze sentence TTS audio."""
    session_unit_id: int


class VoiceInfo(BaseModel):
    """Schema for voice information."""
    voice_id: str
    name: str
