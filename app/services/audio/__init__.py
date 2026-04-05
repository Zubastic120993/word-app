"""Audio services package."""

from app.services.audio.murf_tts_service import (
    MurfTTSService,
    AudioGenerationError,
    MurfDisabledError,
    MurfInvalidConfigurationError,
    normalize_text_for_audio,
    compute_audio_hash,
)
from app.services.audio.elevenlabs_tts_service import (
    ElevenLabsTTSService,
    ElevenLabsDisabledError,
    ElevenLabsInvalidConfigurationError,
)

__all__ = [
    "MurfTTSService",
    "ElevenLabsTTSService",
    "AudioGenerationError",
    "MurfDisabledError",
    "MurfInvalidConfigurationError",
    "ElevenLabsDisabledError",
    "ElevenLabsInvalidConfigurationError",
    "normalize_text_for_audio",
    "compute_audio_hash",
    "get_tts_service_for_source_language",
]

# Type hint for TTS service union
from typing import Union

TTSService = Union[MurfTTSService, ElevenLabsTTSService]


def get_tts_service_for_source_language(source_language: str) -> TTSService:
    """
    Get the appropriate TTS service for a given source language.
    
    Auto-selects:
    - ElevenLabs for Polish when enabled
    - Murf for all other languages (or when ElevenLabs is disabled)
    
    Args:
        source_language: Source language name (e.g., "Polish", "English").
        
    Returns:
        TTS service instance (ElevenLabsTTSService or MurfTTSService).
    """
    from app.config import settings
    
    # Normalize language name for comparison (case-insensitive)
    lang_lower = source_language.lower() if source_language else ""
    
    # Use ElevenLabs for Polish if enabled
    if lang_lower == "polish" and settings.elevenlabs_enabled:
        service = ElevenLabsTTSService()
        if service.is_enabled():
            return service
        # Fall back to Murf if ElevenLabs is not properly configured
    
    # Default to Murf for all other cases
    return MurfTTSService()
