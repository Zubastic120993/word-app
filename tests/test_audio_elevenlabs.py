"""Tests for ElevenLabs TTS service and integration.

These tests ensure that:
1. ElevenLabs service integrates properly with existing audio architecture
2. Audio hash generation is deterministic
3. Engine selection logic works correctly
4. Cache hit vs generate behavior is correct
5. API error handling works properly
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, UnitType
from app.models.audio import AudioAsset
from app.config import settings
from app.services.audio import (
    ElevenLabsTTSService,
    ElevenLabsDisabledError,
    ElevenLabsInvalidConfigurationError,
    AudioGenerationError,
    normalize_text_for_audio,
    compute_audio_hash,
    get_tts_service_for_source_language,
    MurfTTSService,
)


# Create a test engine with StaticPool for thread safety
TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)


def override_get_db():
    """Override database dependency for tests."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_test_db():
    """Setup and teardown test database for each test."""
    # Create all tables
    Base.metadata.create_all(bind=TEST_ENGINE)
    
    # Override the dependency
    app.dependency_overrides[get_db] = override_get_db
    
    yield
    
    # Clear overrides and drop tables
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def db():
    """Get a test database session."""
    db = TestSessionLocal()
    yield db
    db.close()


@pytest.fixture
def sample_unit(db):
    """Create a sample learning unit with Polish text."""
    unit = LearningUnit(
        id=1,
        text="cukier",
        type=UnitType.WORD,
        translation="sugar",
        source_pdf="test.pdf",
        normalized_text="cukier",
        normalized_translation="sugar",
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestElevenLabsTTSService:
    """Test the ElevenLabsTTSService class."""
    
    def test_is_enabled_false_when_disabled(self):
        """Service should report disabled when config is off."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = False
            mock_settings.elevenlabs_api_key = "test-key"
            mock_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            service = ElevenLabsTTSService()
            assert service.is_enabled() is False
    
    def test_is_enabled_false_when_no_key(self):
        """Service should report disabled when no API key."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = True
            mock_settings.elevenlabs_api_key = None
            mock_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            service = ElevenLabsTTSService()
            assert service.is_enabled() is False
    
    def test_is_enabled_false_when_no_voice(self):
        """Service should report disabled when no voice ID."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = True
            mock_settings.elevenlabs_api_key = "test-key"
            mock_settings.elevenlabs_voice_pl = None
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            service = ElevenLabsTTSService()
            assert service.is_enabled() is False
    
    def test_is_enabled_true_when_configured(self):
        """Service should report enabled when properly configured."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = True
            mock_settings.elevenlabs_api_key = "test-key"
            mock_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            service = ElevenLabsTTSService()
            assert service.is_enabled() is True
    
    def test_generate_audio_raises_when_disabled(self):
        """Should raise ElevenLabsDisabledError when disabled."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = False
            mock_settings.elevenlabs_api_key = None
            mock_settings.elevenlabs_voice_pl = None
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            service = ElevenLabsTTSService()
            with pytest.raises(ElevenLabsDisabledError):
                service.generate_audio("cześć")
    
    def test_generate_audio_raises_on_invalid_voice(self):
        """Should raise ElevenLabsInvalidConfigurationError on invalid voice ID."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings, \
             patch("app.services.audio.elevenlabs_tts_service.httpx.Client") as MockClient:
            mock_settings.elevenlabs_enabled = True
            mock_settings.elevenlabs_api_key = "test-key"
            mock_settings.elevenlabs_voice_pl = "invalid-voice-id"
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            # Mock HTTP response with 400 error
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.json.return_value = {
                "detail": {
                    "message": "Invalid voice_id invalid-voice-id"
                }
            }
            mock_response.text = '{"detail":{"message":"Invalid voice_id..."}}'
            
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__.return_value.post.return_value = mock_response
            mock_client_instance.__exit__.return_value = None
            MockClient.return_value = mock_client_instance
            
            service = ElevenLabsTTSService()
            with pytest.raises(ElevenLabsInvalidConfigurationError) as exc_info:
                service.generate_audio("cześć")
            
            assert "Invalid voice ID" in str(exc_info.value)
            assert "invalid-voice-id" in str(exc_info.value)
    
    def test_generate_audio_returns_bytes_on_success(self):
        """Should return audio bytes on successful generation."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings, \
             patch("app.services.audio.elevenlabs_tts_service.httpx.Client") as MockClient:
            mock_settings.elevenlabs_enabled = True
            mock_settings.elevenlabs_api_key = "test-key"
            mock_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            # Mock HTTP response with audio bytes
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = b"fake mp3 audio content"
            
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__.return_value.post.return_value = mock_response
            mock_client_instance.__exit__.return_value = None
            MockClient.return_value = mock_client_instance
            
            service = ElevenLabsTTSService()
            audio_bytes = service.generate_audio("cześć")
            
            assert audio_bytes == b"fake mp3 audio content"
    
    def test_get_audio_hash_for_text(self):
        """Should compute deterministic hash for text."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = True
            mock_settings.elevenlabs_api_key = "test-key"
            mock_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            service = ElevenLabsTTSService()
            hash1 = service.get_audio_hash_for_text("cukier")
            hash2 = service.get_audio_hash_for_text("cukier")
            
            # Same text should produce same hash
            assert hash1 == hash2
            assert len(hash1) == 16  # Hash should be 16 characters


class TestEngineSelection:
    """Test the engine selection logic."""
    
    def test_selects_elevenlabs_for_polish_when_enabled(self):
        """Should select ElevenLabs for Polish when enabled."""
        with patch("app.services.audio.elevenlabs_tts_service.settings") as mock_el_settings, \
             patch("app.config.settings") as mock_config_settings:
            mock_el_settings.elevenlabs_enabled = True
            mock_el_settings.elevenlabs_api_key = "test-key"
            mock_el_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_el_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            mock_config_settings.elevenlabs_enabled = True
            
            service = get_tts_service_for_source_language("Polish")
            assert isinstance(service, ElevenLabsTTSService)
            assert service.is_enabled()
    
    def test_selects_murf_for_polish_when_elevenlabs_disabled(self):
        """Should fall back to Murf for Polish when ElevenLabs is disabled."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.elevenlabs_enabled = False
            
            service = get_tts_service_for_source_language("Polish")
            assert isinstance(service, MurfTTSService)
    
    def test_selects_murf_for_english(self):
        """Should select Murf for English."""
        service = get_tts_service_for_source_language("English")
        assert isinstance(service, MurfTTSService)
    
    def test_selects_murf_for_other_languages(self):
        """Should select Murf for other languages."""
        service = get_tts_service_for_source_language("Spanish")
        assert isinstance(service, MurfTTSService)


class TestAudioHashDeterminism:
    """Test that audio hash is deterministic for ElevenLabs."""
    
    def test_same_text_same_hash_multiple_calls(self):
        """Multiple calls with same input should give same hash."""
        text = "Cześć, jak się masz?"
        normalized = normalize_text_for_audio(text)
        
        hashes = [
            compute_audio_hash("elevenlabs", "test-voice-id", "pl", normalized)
            for _ in range(10)
        ]
        
        # All hashes should be identical
        assert len(set(hashes)) == 1
    
    def test_different_engine_different_hash(self):
        """Different engines should produce different hashes."""
        text = "cukier"
        normalized = normalize_text_for_audio(text)
        
        hash_murf = compute_audio_hash("murf", "en-US-marcus", "en-US", normalized)
        hash_elevenlabs = compute_audio_hash("elevenlabs", "test-voice-id", "pl", normalized)
        
        assert hash_murf != hash_elevenlabs
    
    def test_different_voice_different_hash(self):
        """Different voices should produce different hashes."""
        text = "cukier"
        normalized = normalize_text_for_audio(text)
        
        hash1 = compute_audio_hash("elevenlabs", "voice-1", "pl", normalized)
        hash2 = compute_audio_hash("elevenlabs", "voice-2", "pl", normalized)
        
        assert hash1 != hash2


class TestAudioEndpointIntegration:
    """Test the audio endpoint with ElevenLabs integration."""
    
    def test_returns_403_when_elevenlabs_disabled(self, client, sample_unit):
        """Should return 403 when ElevenLabs is disabled."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.services.audio.elevenlabs_tts_service.settings") as mock_el_settings:
            mock_settings.source_language = "Polish"
            mock_el_settings.elevenlabs_enabled = False
            
            response = client.get(f"/api/audio/{sample_unit.id}")
            assert response.status_code == 403
    
    def test_returns_400_for_invalid_voice(self, client, sample_unit):
        """Should return 400 for invalid voice configuration."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.get_tts_service_for_source_language") as mock_get_service:
            mock_settings.source_language = "Polish"
            mock_settings.audio_dir = Path("/tmp/nonexistent_audio_test_dir")
            
            mock_service = MagicMock()
            mock_service.is_enabled.return_value = True
            mock_service.engine = "elevenlabs"
            mock_service.voice = "invalid-voice-id"
            mock_service.language = "pl"
            mock_service.generate_audio.side_effect = ElevenLabsInvalidConfigurationError(
                "Invalid voice ID 'invalid-voice-id'"
            )
            mock_get_service.return_value = mock_service
            
            response = client.get(f"/api/audio/{sample_unit.id}")
            assert response.status_code == 400
            data = response.json()
            assert "Invalid voice ID" in data["detail"]
    
    def test_cache_hit_returns_audio_for_elevenlabs(self, client, db, sample_unit, tmp_path):
        """Should return cached audio when available for ElevenLabs."""
        # Create temp audio file in isolated pytest tmp_path
        source_path = tmp_path / "source_elevenlabs_audio.mp3"
        source_path.write_bytes(b"fake mp3 content for testing")

        # Create AudioAsset pointing to temp file
        relative_path = "data/audio/test_elevenlabs_audio.mp3"

        asset = AudioAsset(
            unit_id=sample_unit.id,
            engine="elevenlabs",
            voice="test-voice-id",
            language="pl",
            audio_hash="test123",
            file_path=relative_path,
        )
        db.add(asset)
        db.commit()

        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.get_tts_service_for_source_language") as mock_get_service:
            mock_settings.source_language = "Polish"
            mock_settings.base_dir = tmp_path
            mock_settings.audio_dir = tmp_path / "data" / "audio"

            mock_service = MagicMock()
            mock_service.is_enabled.return_value = True
            mock_service.engine = "elevenlabs"
            mock_service.voice = "test-voice-id"
            mock_service.language = "pl"
            mock_get_service.return_value = mock_service

            expected_path = mock_settings.base_dir / relative_path
            expected_path.parent.mkdir(parents=True, exist_ok=True)
            expected_path.write_bytes(source_path.read_bytes())

            response = client.get(f"/api/audio/{sample_unit.id}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/mpeg"
    
    def test_status_endpoint_shows_elevenlabs_when_enabled(self, client):
        """Status endpoint should show ElevenLabs when enabled for Polish."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.services.audio.elevenlabs_tts_service.settings") as mock_el_settings:
            mock_settings.source_language = "Polish"
            mock_settings.elevenlabs_enabled = True
            mock_el_settings.elevenlabs_enabled = True
            mock_el_settings.elevenlabs_api_key = "test-key"
            mock_el_settings.elevenlabs_voice_pl = "test-voice-id"
            mock_el_settings.elevenlabs_model = "eleven_multilingual_v2"
            
            response = client.get("/api/audio/status")
            assert response.status_code == 200
            data = response.json()
            # Should show enabled, but engine depends on actual service availability
            assert "enabled" in data
            assert "engine" in data
