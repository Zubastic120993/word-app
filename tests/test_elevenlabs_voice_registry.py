"""Tests for ElevenLabs Polish voice registry and configuration validation."""

import pytest
import os
from pydantic import ValidationError

from app.services.audio.elevenlabs_voices import (
    POLISH_VOICES,
    get_voice_by_id,
    get_all_voice_ids,
    get_voice_display_name,
    is_valid_voice_id,
)
from app.config import Settings


class TestVoiceRegistry:
    """Test the Polish voice registry."""
    
    def test_registry_has_three_voices(self):
        """Registry should contain exactly 3 voices."""
        assert len(POLISH_VOICES) == 3
    
    def test_voice_ids_are_unique(self):
        """All voice IDs should be unique."""
        voice_ids = [voice.voice_id for voice in POLISH_VOICES]
        assert len(voice_ids) == len(set(voice_ids))
    
    def test_get_voice_by_id(self):
        """Should retrieve voice by ID."""
        voice = get_voice_by_id("zzBTsLBFM6AOJtkr1e9b")
        assert voice is not None
        assert voice.voice_id == "zzBTsLBFM6AOJtkr1e9b"
        assert voice.display_name == "Paweł Pro"
    
    def test_get_voice_by_id_not_found(self):
        """Should return None for invalid voice ID."""
        voice = get_voice_by_id("invalid-voice-id")
        assert voice is None
    
    def test_get_all_voice_ids(self):
        """Should return all voice IDs."""
        voice_ids = get_all_voice_ids()
        assert len(voice_ids) == 3
        assert "H5xTcsAIeS5RAykjz57a" in voice_ids
        assert "zzBTsLBFM6AOJtkr1e9b" in voice_ids
        assert "Sgu2YCTorC0ao3q8kFyk" in voice_ids
    
    def test_get_voice_display_name(self):
        """Should return display name for valid voice ID."""
        name = get_voice_display_name("zzBTsLBFM6AOJtkr1e9b")
        assert name == "Paweł Pro"
    
    def test_get_voice_display_name_not_found(self):
        """Should return None for invalid voice ID."""
        name = get_voice_display_name("invalid-voice-id")
        assert name is None
    
    def test_is_valid_voice_id(self):
        """Should return True for valid voice IDs."""
        assert is_valid_voice_id("zzBTsLBFM6AOJtkr1e9b") is True
        assert is_valid_voice_id("H5xTcsAIeS5RAykjz57a") is True
        assert is_valid_voice_id("Sgu2YCTorC0ao3q8kFyk") is True
    
    def test_is_valid_voice_id_invalid(self):
        """Should return False for invalid voice IDs."""
        assert is_valid_voice_id("invalid-voice-id") is False
        assert is_valid_voice_id("") is False


class TestSettingsValidation:
    """Test Settings validation for ElevenLabs voice ID."""
    
    def test_valid_voice_id_passes_validation(self):
        """Settings should accept valid voice ID when ElevenLabs is enabled."""
        # Use model_validate to test validation logic
        settings_data = {
            "elevenlabs_enabled": True,
            "elevenlabs_voice_pl": "zzBTsLBFM6AOJtkr1e9b",
        }
        # This should not raise an error
        validated = Settings.model_validate(settings_data)
        assert validated.elevenlabs_voice_pl == "zzBTsLBFM6AOJtkr1e9b"
    
    def test_invalid_voice_id_raises_error(self):
        """Settings should raise ValidationError for invalid voice ID when ElevenLabs is enabled."""
        settings_data = {
            "elevenlabs_enabled": True,
            "elevenlabs_voice_pl": "invalid-voice-id",
        }
        
        with pytest.raises(ValidationError) as exc_info:
            Settings.model_validate(settings_data)
        
        # Check that the error message mentions the invalid voice ID
        error_str = str(exc_info.value)
        assert "invalid-voice-id" in error_str.lower() or "invalid" in error_str.lower()
        # Check that error mentions valid voices
        assert "WORD_APP_ELEVENLABS_VOICE_PL" in error_str or "voice" in error_str.lower()
    
    def test_validation_passes_when_elevenlabs_disabled(self):
        """Settings should not validate voice ID when ElevenLabs is disabled."""
        settings_data = {
            "elevenlabs_enabled": False,
            "elevenlabs_voice_pl": "invalid-voice-id",  # Invalid, but should pass
        }
        
        # This should not raise an error
        validated = Settings.model_validate(settings_data)
        assert validated.elevenlabs_voice_pl == "invalid-voice-id"
    
    def test_validation_passes_when_voice_not_set(self):
        """Settings should not validate voice ID when voice is None."""
        settings_data = {
            "elevenlabs_enabled": True,
            "elevenlabs_voice_pl": None,
        }
        
        # This should not raise an error
        validated = Settings.model_validate(settings_data)
        assert validated.elevenlabs_voice_pl is None
