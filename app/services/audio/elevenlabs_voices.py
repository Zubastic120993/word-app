"""ElevenLabs Polish voice registry with stable IDs and human-readable names."""

from typing import NamedTuple


class PolishVoice(NamedTuple):
    """Polish ElevenLabs voice definition."""
    voice_id: str
    display_name: str


# Canonical registry of allowed Polish ElevenLabs voices
POLISH_VOICES = [
    PolishVoice(voice_id="H5xTcsAIeS5RAykjz57a", display_name="Alex – Professional Narration"),
    PolishVoice(voice_id="zzBTsLBFM6AOJtkr1e9b", display_name="Paweł Pro"),
    PolishVoice(voice_id="Sgu2YCTorC0ao3q8kFyk", display_name="Marek – Neutral"),
]


def get_voice_by_id(voice_id: str) -> PolishVoice | None:
    """
    Get a Polish voice by its ID.
    
    Args:
        voice_id: ElevenLabs voice ID.
        
    Returns:
        PolishVoice if found, None otherwise.
    """
    for voice in POLISH_VOICES:
        if voice.voice_id == voice_id:
            return voice
    return None


def get_all_voice_ids() -> list[str]:
    """
    Get all registered Polish voice IDs.
    
    Returns:
        List of voice IDs.
    """
    return [voice.voice_id for voice in POLISH_VOICES]


def get_voice_display_name(voice_id: str) -> str | None:
    """
    Get display name for a voice ID.
    
    Args:
        voice_id: ElevenLabs voice ID.
        
    Returns:
        Display name if found, None otherwise.
    """
    voice = get_voice_by_id(voice_id)
    return voice.display_name if voice else None


def is_valid_voice_id(voice_id: str) -> bool:
    """
    Check if a voice ID is in the registry.
    
    Args:
        voice_id: ElevenLabs voice ID.
        
    Returns:
        True if voice ID is registered, False otherwise.
    """
    return get_voice_by_id(voice_id) is not None
