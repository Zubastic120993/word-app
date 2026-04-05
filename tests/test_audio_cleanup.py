"""Tests for audio cleanup service.

These tests ensure that:
1. Orphaned audio files are deleted
2. Referenced audio files are kept
3. Cleanup is idempotent (running twice does nothing)
4. Missing audio directory is handled safely
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.learning_unit import LearningUnit, UnitType
from app.models.audio import AudioAsset
from app.services.audio.audio_cleanup_service import cleanup_orphaned_audio_files
from app.config import settings


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
def temp_audio_dir(monkeypatch, tmp_path):
    """Create a temporary audio directory for testing."""
    # Create directory structure matching production: data/audio/
    audio_dir = tmp_path / "data" / "audio"
    audio_dir.mkdir(parents=True)
    
    # Patch settings.audio_dir and settings.base_dir
    original_audio_dir = settings.audio_dir
    original_base_dir = settings.base_dir
    
    monkeypatch.setattr(settings, "audio_dir", audio_dir)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    
    yield audio_dir
    
    # Restore original values
    monkeypatch.setattr(settings, "audio_dir", original_audio_dir)
    monkeypatch.setattr(settings, "base_dir", original_base_dir)


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


def create_test_audio_file(file_path: Path, content: bytes = b"fake audio data") -> Path:
    """Create a test audio file."""
    file_path.write_bytes(content)
    return file_path


class TestCleanupOrphanedAudioFiles:
    """Test the cleanup_orphaned_audio_files function."""
    
    def test_deletes_unreferenced_files(self, db, temp_audio_dir, sample_unit):
        """Should delete files that are not referenced by any AudioAsset."""
        # Create referenced file
        referenced_file = create_test_audio_file(
            temp_audio_dir / "referenced.mp3",
            b"referenced audio"
        )
        
        # Create orphaned file
        orphaned_file = create_test_audio_file(
            temp_audio_dir / "orphaned.mp3",
            b"orphaned audio"
        )
        
        # Create AudioAsset for referenced file
        asset = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="hash1",
            file_path=f"data/audio/referenced.mp3",
        )
        db.add(asset)
        db.commit()
        
        # Run cleanup
        result = cleanup_orphaned_audio_files(db)
        
        # Check results
        assert result["files_deleted"] == 1
        assert result["bytes_freed"] > 0
        
        # Referenced file should still exist
        assert referenced_file.exists()
        
        # Orphaned file should be deleted
        assert not orphaned_file.exists()
    
    def test_keeps_referenced_files(self, db, temp_audio_dir, sample_unit):
        """Should keep files that are referenced by AudioAsset."""
        # Create referenced files
        file1 = create_test_audio_file(temp_audio_dir / "file1.mp3")
        file2 = create_test_audio_file(temp_audio_dir / "file2.mp3")
        
        # Create AudioAssets for both files
        asset1 = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="hash1",
            file_path=f"data/audio/file1.mp3",
        )
        asset2 = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-natalie",
            language="en-US",
            audio_hash="hash2",
            file_path=f"data/audio/file2.mp3",
        )
        db.add(asset1)
        db.add(asset2)
        db.commit()
        
        # Run cleanup
        result = cleanup_orphaned_audio_files(db)
        
        # No files should be deleted
        assert result["files_deleted"] == 0
        assert result["bytes_freed"] == 0
        
        # Both files should still exist
        assert file1.exists()
        assert file2.exists()
    
    def test_idempotency(self, db, temp_audio_dir, sample_unit):
        """Running cleanup twice should have no effect the second time."""
        # Create orphaned file
        orphaned_file = create_test_audio_file(temp_audio_dir / "orphaned.mp3")
        
        # First cleanup
        result1 = cleanup_orphaned_audio_files(db)
        assert result1["files_deleted"] == 1
        assert not orphaned_file.exists()
        
        # Second cleanup (should do nothing)
        result2 = cleanup_orphaned_audio_files(db)
        assert result2["files_deleted"] == 0
        assert result2["bytes_freed"] == 0
    
    def test_missing_audio_directory(self, db, monkeypatch, tmp_path):
        """Should handle missing audio directory gracefully."""
        # Set audio_dir to non-existent directory
        non_existent_dir = tmp_path / "nonexistent" / "audio"
        monkeypatch.setattr(settings, "audio_dir", non_existent_dir)
        monkeypatch.setattr(settings, "base_dir", tmp_path)
        
        # Run cleanup (should not raise)
        result = cleanup_orphaned_audio_files(db)
        
        assert result["files_deleted"] == 0
        assert result["bytes_freed"] == 0
    
    def test_empty_audio_directory(self, db, temp_audio_dir):
        """Should handle empty audio directory."""
        # Directory exists but is empty
        result = cleanup_orphaned_audio_files(db)
        
        assert result["files_deleted"] == 0
        assert result["bytes_freed"] == 0
    
    def test_multiple_orphaned_files(self, db, temp_audio_dir, sample_unit):
        """Should delete all orphaned files."""
        # Create multiple orphaned files
        orphaned_files = []
        for i in range(5):
            file = create_test_audio_file(temp_audio_dir / f"orphaned_{i}.mp3")
            orphaned_files.append(file)
        
        # Create one referenced file
        referenced_file = create_test_audio_file(temp_audio_dir / "referenced.mp3")
        asset = AudioAsset(
            unit_id=sample_unit.id,
            engine="murf",
            voice="en-US-marcus",
            language="en-US",
            audio_hash="hash1",
            file_path=f"data/audio/referenced.mp3",
        )
        db.add(asset)
        db.commit()
        
        # Run cleanup
        result = cleanup_orphaned_audio_files(db)
        
        # All orphaned files should be deleted
        assert result["files_deleted"] == 5
        
        # Referenced file should still exist
        assert referenced_file.exists()
        
        # All orphaned files should be gone
        for file in orphaned_files:
            assert not file.exists()


class TestCleanupEndpoint:
    """Test the /api/audio/cleanup endpoint."""
    
    def test_cleanup_endpoint_dev_mode(self, db, temp_audio_dir, sample_unit, client, monkeypatch):
        """Endpoint should work in dev mode (debug=True)."""
        # Enable debug mode
        monkeypatch.setattr(settings, "debug", True)
        
        # Create orphaned file
        create_test_audio_file(temp_audio_dir / "orphaned.mp3")
        
        # Call endpoint
        response = client.post("/api/audio/cleanup")
        
        assert response.status_code == 200
        data = response.json()
        assert "files_deleted" in data
        assert "bytes_freed" in data
        assert data["files_deleted"] == 1
    
    def test_cleanup_endpoint_dev_env(self, db, temp_audio_dir, sample_unit, client, monkeypatch):
        """Endpoint should work in dev mode (env='development')."""
        # Set env to development
        monkeypatch.setattr(settings, "env", "development")
        monkeypatch.setattr(settings, "debug", False)
        
        # Create orphaned file
        create_test_audio_file(temp_audio_dir / "orphaned.mp3")
        
        # Call endpoint
        response = client.post("/api/audio/cleanup")
        
        assert response.status_code == 200
        data = response.json()
        assert data["files_deleted"] == 1
    
    def test_cleanup_endpoint_production_forbidden(self, db, client, monkeypatch):
        """Endpoint should be forbidden in production mode."""
        # Disable debug mode and set env to production
        monkeypatch.setattr(settings, "debug", False)
        monkeypatch.setattr(settings, "env", "production")
        
        # Call endpoint
        response = client.post("/api/audio/cleanup")
        
        assert response.status_code == 403
        assert "development mode" in response.json()["detail"].lower()
