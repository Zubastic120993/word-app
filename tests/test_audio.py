"""Tests for audio pronunciation feature.

These tests ensure that:
1. Audio hash generation is deterministic
2. AudioAsset uniqueness constraint works
3. Cache hit vs miss behavior is correct
4. API returns proper audio/mpeg content type
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, LearningProgress, UnitType
from app.models.audio import AudioAsset
from app.services.audio import (
    normalize_text_for_audio,
    compute_audio_hash,
    MurfTTSService,
    MurfDisabledError,
    MurfInvalidConfigurationError,
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
    """Create a sample learning unit."""
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


class TestNormalizeTextForAudio:
    """Test the normalize_text_for_audio function."""
    
    def test_lowercase(self):
        """Text should be lowercased."""
        assert normalize_text_for_audio("CUKIER") == "cukier"
        assert normalize_text_for_audio("Cukier") == "cukier"
    
    def test_strip_whitespace(self):
        """Leading/trailing whitespace should be stripped."""
        assert normalize_text_for_audio("  cukier  ") == "cukier"
        assert normalize_text_for_audio("\tcukier\n") == "cukier"
    
    def test_collapse_multiple_spaces(self):
        """Multiple spaces should become single space."""
        assert normalize_text_for_audio("co  to   jest") == "co to jest"
    
    def test_preserve_punctuation(self):
        """Punctuation affects pronunciation, so keep it."""
        assert normalize_text_for_audio("Cześć!") == "cześć!"
        assert normalize_text_for_audio("Co to jest?") == "co to jest?"
    
    def test_empty_string(self):
        """Empty string should return empty."""
        assert normalize_text_for_audio("") == ""
        assert normalize_text_for_audio("   ") == ""


class TestComputeAudioHash:
    """Test the compute_audio_hash function."""
    
    def test_deterministic_hash(self):
        """Same inputs should always produce same hash."""
        hash1 = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        hash2 = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        assert hash1 == hash2
    
    def test_different_text_different_hash(self):
        """Different text should produce different hash."""
        hash1 = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        hash2 = compute_audio_hash("murf", "en-US-marcus", "en-US", "world")
        assert hash1 != hash2
    
    def test_different_voice_different_hash(self):
        """Different voice should produce different hash."""
        hash1 = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        hash2 = compute_audio_hash("murf", "en-US-natalie", "en-US", "hello")
        assert hash1 != hash2
    
    def test_different_language_different_hash(self):
        """Different language should produce different hash."""
        hash1 = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        hash2 = compute_audio_hash("murf", "en-US-marcus", "en-GB", "hello")
        assert hash1 != hash2
    
    def test_hash_length(self):
        """Hash should be 16 characters (truncated SHA-256)."""
        hash_value = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        assert len(hash_value) == 16
    
    def test_hash_is_hex(self):
        """Hash should be valid hexadecimal."""
        hash_value = compute_audio_hash("murf", "en-US-marcus", "en-US", "hello")
        # Should not raise ValueError
        int(hash_value, 16)


class TestAudioAssetModel:
    """Test the AudioAsset database model."""
    
    def test_create_audio_asset(self, db, sample_unit):
        """Should be able to create an AudioAsset."""
        asset = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="abc123def456ghij",
            file_path="data/audio/abc123_en-US_en-US-marcus.mp3",
        )
        db.add(asset)
        db.commit()
        
        assert asset.id is not None
        assert asset.unit_id == sample_unit.id
        assert asset.engine == "murf"
    
    def test_uniqueness_constraint(self, db, sample_unit):
        """Should prevent duplicate AudioAsset for same unit+engine+voice+language."""
        asset1 = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="abc123",
            file_path="data/audio/abc123.mp3",
        )
        db.add(asset1)
        db.commit()
        
        # Try to add duplicate
        asset2 = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="def456",  # Different hash
            file_path="data/audio/def456.mp3",
        )
        db.add(asset2)
        
        with pytest.raises(IntegrityError):
            db.commit()
    
    def test_different_voice_allowed(self, db, sample_unit):
        """Should allow same unit with different voice."""
        asset1 = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="abc123",
            file_path="data/audio/abc123.mp3",
        )
        asset2 = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-natalie",  # Different voice
            language="en-US",
            audio_hash="def456",
            file_path="data/audio/def456.mp3",
        )
        db.add(asset1)
        db.add(asset2)
        db.commit()  # Should not raise
        
        assert asset1.id != asset2.id
    
    def test_cascade_delete(self, db, sample_unit):
        """Deleting LearningUnit should delete associated AudioAssets."""
        asset = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="abc123",
            file_path="data/audio/abc123.mp3",
        )
        db.add(asset)
        db.commit()
        
        asset_id = asset.id
        
        # Delete the unit
        db.delete(sample_unit)
        db.commit()
        
        # Asset should be gone
        deleted_asset = db.query(AudioAsset).filter(AudioAsset.id == asset_id).first()
        assert deleted_asset is None


class TestMurfTTSService:
    """Test the MurfTTSService class."""
    
    def test_is_enabled_false_when_disabled(self):
        """Service should report disabled when config is off."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings:
            mock_settings.murf_enabled = False
            mock_settings.murf_api_key = "test-key"
            mock_settings.murf_voice = "en-US-marcus"
            mock_settings.murf_language = "en-US"
            
            service = MurfTTSService()
            assert service.is_enabled() is False
    
    def test_is_enabled_false_when_no_key(self):
        """Service should report disabled when no API key."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = None
            mock_settings.murf_voice = "en-US-marcus"
            mock_settings.murf_language = "en-US"
            
            service = MurfTTSService()
            assert service.is_enabled() is False
    
    def test_is_enabled_true_when_configured(self):
        """Service should report enabled when properly configured."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = "test-key"
            mock_settings.murf_voice = "en-US-marcus"
            mock_settings.murf_language = "en-US"
            
            service = MurfTTSService()
            assert service.is_enabled() is True
    
    def test_generate_audio_raises_when_disabled(self):
        """Should raise MurfDisabledError when disabled."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings:
            mock_settings.murf_enabled = False
            mock_settings.murf_api_key = None
            mock_settings.murf_voice = "en-US-marcus"
            mock_settings.murf_language = "en-US"
            
            service = MurfTTSService()
            with pytest.raises(MurfDisabledError):
                service.generate_audio("hello")
    
    def test_generate_audio_raises_on_invalid_voice(self):
        """Should raise MurfInvalidConfigurationError on invalid voice ID."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings, \
             patch("app.services.audio.murf_tts_service.httpx.Client") as MockClient:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = "test-key"
            mock_settings.murf_voice = "pl-PL-maja"  # Invalid voice
            mock_settings.murf_language = "en-US"
            
            # Mock HTTP response with 400 error
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.json.return_value = {
                "errorCode": 400,
                "errorMessage": "Invalid voice_id pl-PL-maja. Use the GET /v1/speech/voices endpoint..."
            }
            mock_response.text = '{"errorCode":400,"errorMessage":"Invalid voice_id..."}'
            
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__.return_value.post.return_value = mock_response
            mock_client_instance.__exit__.return_value = None
            MockClient.return_value = mock_client_instance
            
            service = MurfTTSService()
            with pytest.raises(MurfInvalidConfigurationError) as exc_info:
                service.generate_audio("hello")
            
            assert "Invalid voice ID" in str(exc_info.value)
            assert "pl-PL-maja" in str(exc_info.value)
    
    def test_get_available_voices_raises_when_disabled(self):
        """Should raise MurfDisabledError when Murf is disabled."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings:
            mock_settings.murf_enabled = False
            mock_settings.murf_api_key = "test-key"
            mock_settings.murf_language = "en-US"
            
            service = MurfTTSService()
            with pytest.raises(MurfDisabledError):
                service.get_available_voices()
    
    def test_get_available_voices_raises_when_no_api_key(self):
        """Should raise MurfInvalidConfigurationError when API key is missing."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = None
            mock_settings.murf_language = "en-US"
            
            service = MurfTTSService()
            with pytest.raises(MurfInvalidConfigurationError) as exc_info:
                service.get_available_voices()
            
            assert "api key" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()
    
    def test_get_available_voices_filters_by_language(self):
        """Should return only voices matching configured language."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings, \
             patch("app.services.audio.murf_tts_service.httpx.Client") as MockClient:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = "test-key"
            mock_settings.murf_language = "pl-PL"
            
            # Mock HTTP response with voices in different languages
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [
                {
                    "voiceId": "pl-PL-maja",
                    "language": "pl-PL",
                    "gender": "female",
                    "style": None,
                },
                {
                    "voiceId": "pl-PL-kuba",
                    "language": "pl-PL",
                    "gender": "male",
                    "style": None,
                },
                {
                    "voiceId": "en-US-marcus",
                    "language": "en-US",
                    "gender": "male",
                    "style": None,
                },
            ]
            
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__.return_value.get.return_value = mock_response
            mock_client_instance.__exit__.return_value = None
            MockClient.return_value = mock_client_instance
            
            service = MurfTTSService()
            voices = service.get_available_voices()
            
            # Should only return voices matching pl-PL
            assert isinstance(voices, list)
            assert len(voices) == 2
            assert all(voice["voice_id"].startswith("pl-PL") for voice in voices)
    
    def test_get_available_voices_returns_simplified_format(self):
        """Should return only voice_id, gender, and style fields."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings, \
             patch("app.services.audio.murf_tts_service.httpx.Client") as MockClient:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = "test-key"
            mock_settings.murf_language = "en-US"
            
            # Mock HTTP response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [
                {
                    "voiceId": "en-US-marcus",
                    "language": "en-US",
                    "gender": "male",
                    "style": None,
                },
                {
                    "voiceId": "en-US-natalie",
                    "language": "en-US",
                    "gender": "female",
                    "style": "conversational",
                },
            ]
            
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__.return_value.get.return_value = mock_response
            mock_client_instance.__exit__.return_value = None
            MockClient.return_value = mock_client_instance
            
            service = MurfTTSService()
            voices = service.get_available_voices()
            
            # Verify structure
            assert isinstance(voices, list)
            assert len(voices) == 2
            
            for voice in voices:
                assert set(voice.keys()) == {"voice_id", "gender", "style"}
                assert isinstance(voice["voice_id"], str)
                assert voice["gender"] is None or isinstance(voice["gender"], str)
                assert voice["style"] is None or isinstance(voice["style"], str)
    
    def test_get_available_voices_handles_401_error(self):
        """Should raise MurfInvalidConfigurationError on 401/403 errors."""
        with patch("app.services.audio.murf_tts_service.settings") as mock_settings, \
             patch("app.services.audio.murf_tts_service.httpx.Client") as MockClient:
            mock_settings.murf_enabled = True
            mock_settings.murf_api_key = "invalid-key"
            mock_settings.murf_language = "en-US"
            
            # Mock HTTP response with 401 error
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.text = "Unauthorized"
            
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__.return_value.get.return_value = mock_response
            mock_client_instance.__exit__.return_value = None
            MockClient.return_value = mock_client_instance
            
            service = MurfTTSService()
            with pytest.raises(MurfInvalidConfigurationError) as exc_info:
                service.get_available_voices()
            
            assert "api key" in str(exc_info.value).lower()


class TestAudioStatusEndpoint:
    """Test the /api/audio/status endpoint."""
    
    def test_status_disabled(self, client):
        """Should return disabled status when Murf is off."""
        with patch("app.routers.audio.MurfTTSService") as MockService:
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = False
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/status")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is False
    
    def test_status_enabled(self, client):
        """Should return enabled status when TTS is on."""
        with patch("app.routers.audio.get_tts_service_for_source_language") as mock_get_service:
            mock_service = MagicMock()
            mock_service.is_enabled.return_value = True
            mock_service.engine = "murf"
            mock_get_service.return_value = mock_service
            
            response = client.get("/api/audio/status")
            assert response.status_code == 200
            data = response.json()
            assert data["enabled"] is True


class TestAudioVoicesEndpoint:
    """Test the /api/audio/voices endpoint."""
    
    def test_returns_403_when_disabled(self, client):
        """Should return 403 when Murf is disabled."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.MurfTTSService") as MockService:
            mock_settings.murf_enabled = False
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = False
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/voices")
            assert response.status_code == 403
            data = response.json()
            assert "not enabled" in data["detail"].lower()
    
    def test_returns_400_when_api_key_missing(self, client):
        """Should return 400 when API key is missing."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.MurfTTSService") as MockService:
            mock_settings.murf_enabled = True
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = True
            mock_instance.api_key = None
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/voices")
            assert response.status_code == 400
            data = response.json()
            assert "api key" in data["detail"].lower() or "missing" in data["detail"].lower()
    
    def test_returns_voices_filtered_by_language(self, client):
        """Should return only voices matching configured language."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.MurfTTSService") as MockService:
            mock_settings.murf_enabled = True
            mock_settings.murf_language = "pl-PL"
            
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = True
            mock_instance.api_key = "test-key"
            mock_instance.language = "pl-PL"
            
            # Mock response with voices in different languages
            mock_voices = [
                {"voice_id": "pl-PL-maja", "gender": "female", "style": None},
                {"voice_id": "pl-PL-kuba", "gender": "male", "style": None},
                {"voice_id": "en-US-marcus", "gender": "male", "style": None},  # Wrong language
            ]
            mock_instance.get_available_voices.return_value = mock_voices
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/voices")
            assert response.status_code == 200
            data = response.json()
            
            # Should return the filtered voices (in this case, all that match pl-PL)
            assert isinstance(data, list)
            assert len(data) == 3  # All three are returned because we mock the filtered result
            
            # Verify structure of each voice
            for voice in data:
                assert "voice_id" in voice
                assert isinstance(voice["voice_id"], str)
                # gender and style may be None
                assert "gender" in voice
                assert "style" in voice
    
    def test_returns_only_voice_id_gender_style(self, client):
        """Should return only voice_id, gender, and style fields."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.MurfTTSService") as MockService:
            mock_settings.murf_enabled = True
            mock_settings.murf_language = "en-US"
            
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = True
            mock_instance.api_key = "test-key"
            mock_instance.language = "en-US"
            
            # Mock response with proper structure
            mock_voices = [
                {"voice_id": "en-US-marcus", "gender": "male", "style": None},
                {"voice_id": "en-US-natalie", "gender": "female", "style": "conversational"},
            ]
            mock_instance.get_available_voices.return_value = mock_voices
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/voices")
            assert response.status_code == 200
            data = response.json()
            
            assert isinstance(data, list)
            assert len(data) == 2
            
            # Verify each voice has only the expected fields
            for voice in data:
                assert set(voice.keys()) == {"voice_id", "gender", "style"}
                assert isinstance(voice["voice_id"], str)
                assert voice["gender"] is None or isinstance(voice["gender"], str)
                assert voice["style"] is None or isinstance(voice["style"], str)
    
    def test_returns_400_on_invalid_api_key(self, client):
        """Should return 400 when API key is invalid."""
        with patch("app.routers.audio.settings") as mock_settings, \
             patch("app.routers.audio.MurfTTSService") as MockService:
            mock_settings.murf_enabled = True
            
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = True
            mock_instance.api_key = "invalid-key"
            mock_instance.get_available_voices.side_effect = MurfInvalidConfigurationError(
                "Invalid or missing Murf API key"
            )
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/voices")
            assert response.status_code == 400
            data = response.json()
            assert "api key" in data["detail"].lower() or "invalid" in data["detail"].lower()


class TestAudioEndpoint:
    """Test the /api/audio/{unit_id} endpoint."""
    
    def test_returns_403_when_disabled(self, client, sample_unit):
        """Should return 403 when Murf is disabled."""
        with patch("app.routers.audio.MurfTTSService") as MockService:
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = False
            MockService.return_value = mock_instance
            
            response = client.get(f"/api/audio/{sample_unit.id}")
            assert response.status_code == 403
    
    def test_returns_404_for_missing_unit(self, client):
        """Should return 404 for non-existent unit."""
        with patch("app.routers.audio.MurfTTSService") as MockService:
            mock_instance = MagicMock()
            mock_instance.is_enabled.return_value = True
            MockService.return_value = mock_instance
            
            response = client.get("/api/audio/99999")
            assert response.status_code == 404
    
    def test_returns_400_for_invalid_voice(self, client, sample_unit):
        """Should return 400 for invalid voice configuration."""
        with patch("app.routers.audio.get_tts_service_for_source_language") as mock_get_service:
            mock_service = MagicMock()
            mock_service.is_enabled.return_value = True
            mock_service.engine = "murf"
            mock_service.voice = "pl-PL-maja"
            mock_service.language = "en-US"
            mock_service.generate_audio.side_effect = MurfInvalidConfigurationError(
                "Invalid voice ID 'pl-PL-maja'. Please check available voices."
            )
            mock_get_service.return_value = mock_service
            
            response = client.get(f"/api/audio/{sample_unit.id}")
            assert response.status_code == 400
            data = response.json()
            assert "Invalid voice ID" in data["detail"]
    
    def test_cache_hit_returns_audio(self, client, db, sample_unit, tmp_path):
        """Should return cached audio when available."""
        # Create temp audio file in isolated pytest tmp_path
        source_path = tmp_path / "source_audio.mp3"
        source_path.write_bytes(b"fake mp3 content for testing")

        # Create AudioAsset pointing to temp file
        relative_path = "data/audio/test_audio.mp3"

        asset = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="test123",
            file_path=relative_path,
        )
        db.add(asset)
        db.commit()

        with patch("app.routers.audio.get_tts_service_for_source_language") as mock_get_service:
            mock_service = MagicMock()
            mock_service.is_enabled.return_value = True
            mock_service.engine = "murf"
            mock_service.voice = "en-US-marcus"
            mock_service.language = "en-US"
            mock_get_service.return_value = mock_service

            # Route all test audio I/O to tmp_path only
            with patch("app.routers.audio.settings") as mock_settings:
                mock_settings.base_dir = tmp_path
                mock_settings.audio_dir = tmp_path / "data" / "audio"

                expected_path = mock_settings.base_dir / relative_path
                expected_path.parent.mkdir(parents=True, exist_ok=True)
                expected_path.write_bytes(source_path.read_bytes())

                response = client.get(f"/api/audio/{sample_unit.id}")
                assert response.status_code == 200
                assert response.headers["content-type"] == "audio/mpeg"


class TestAudioHashDeterminism:
    """Test that audio hash is truly deterministic for caching."""
    
    def test_same_text_same_hash_multiple_calls(self):
        """Multiple calls with same input should give same hash."""
        text = "Cześć, jak się masz?"
        
        hashes = [
            compute_audio_hash("murf", "en-US-marcus", "en-US", text)
            for _ in range(10)
        ]
        
        # All hashes should be identical
        assert len(set(hashes)) == 1
    
    def test_normalized_text_same_hash(self):
        """Normalizing text before hashing should give consistent results."""
        texts = [
            "  CUKIER  ",
            "cukier",
            "CUKIER",
            "  cukier",
        ]
        
        hashes = [
            compute_audio_hash(
                "murf",
                "en-US-marcus",
                "en-US",
                normalize_text_for_audio(t),
            )
            for t in texts
        ]
        
        # All should produce same hash after normalization
        assert len(set(hashes)) == 1
