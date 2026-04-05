"""Tests for import service with backup and rollback."""

import pytest
import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.audio import AudioAsset
from app.models.learning_unit import LearningUnit, LearningProgress, Settings, UnitType
from app.models.practice_event import PracticeEvent
from app.models.session import LearningSession, SessionUnit
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.services.import_service import ImportService, import_all_data
from app.services.export_service import ExportService


def create_valid_import_data() -> dict:
    """Create valid import data for testing."""
    return {
        "metadata": {
            "schema_version": "1.0",
            "app_version": "0.1.0",
            "export_timestamp": "2025-01-11T12:00:00Z",
            "source_language": "Polish",
            "target_language": "English",
            "session_size": 50,
        },
        "settings": {
            "id": 1,
            "offline_mode": True,
            "ai_provider": "ollama",
            "ollama_model": "llama3.2",
            "strict_mode": True,
            "source_language": "Polish",
            "target_language": "English",
        },
        "learning_units": [
            {
                "id": 1,
                "text": "nowy",
                "type": "word",
                "translation": "new",
                "source_pdf": "import.pdf",
                "vocabulary_id": 1,
                "created_at": "2025-01-11T10:00:00Z",
            },
            {
                "id": 2,
                "text": "stary",
                "type": "word",
                "translation": "old",
                "source_pdf": "import.pdf",
                "created_at": "2025-01-11T10:00:01Z",
            },
        ],
        "learning_progress": [
            {
                "id": 1,
                "unit_id": 1,
                "times_seen": 3,
                "times_correct": 2,
                "times_failed": 1,
                "confidence_score": 0.67,
            },
        ],
        "learning_sessions": [
            {
                "id": 1,
                "created_at": "2025-01-11T11:00:00Z",
                "locked": True,
                "completed": False,
            },
        ],
        "session_units": [
            {
                "id": 1,
                "session_id": 1,
                "unit_id": 1,
                "position": 1,
                "answered": False,
            },
        ],
        "vocabulary_groups": [
            {
                "id": 1,
                "user_key": "default",
                "name": "Core",
                "description": "Core vocabulary",
                "display_order": 1,
                "created_at": "2025-01-01T09:00:00Z",
            },
        ],
        "vocabularies": [
            {
                "id": 1,
                "user_key": "default",
                "name": "Starter Set",
                "group_id": 1,
                "created_at": "2025-01-01T09:05:00Z",
            },
        ],
        "audio_assets": [
            {
                "id": 1,
                "unit_id": 1,
                "engine": "murf",
                "voice": "en-US-marcus",
                "language": "en-US",
                "audio_hash": "hash-1",
                "file_path": "data/audio/hash-1.mp3",
                "created_at": "2025-01-10T11:10:00Z",
            },
        ],
        "practice_events": [
            {
                "id": 1,
                "created_at": "2025-01-10T11:15:00Z",
                "event_type": "quiz_answer",
                "theme": "basics",
                "payload": {"unit_id": 1, "correct": True},
            },
        ],
    }


@pytest.fixture
def test_db():
    """Create a test database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    db = TestSessionLocal()
    yield db
    db.close()


@pytest.fixture
def populated_db(test_db):
    """Create a test database with existing data."""
    # Add existing data that will be replaced by import
    unit = LearningUnit(
        id=100,
        text="existing",
        type=UnitType.WORD,
        translation="istniejący",
        source_pdf="existing.pdf",
        created_at=datetime(2025, 1, 1, 10, 0, 0),
    )
    test_db.add(unit)
    
    progress = LearningProgress(
        id=100,
        unit_id=100,
        times_seen=10,
        times_correct=8,
        times_failed=2,
        confidence_score=0.8,
    )
    test_db.add(progress)
    
    test_db.commit()
    return test_db


class TestSuccessfulImport:
    """Tests for successful import operations."""
    
    def test_import_restores_all_data(self, test_db):
        """Verify successful import restores all data."""
        data = create_valid_import_data()
        
        result = import_all_data(test_db, data)
        
        assert result.success is True
        assert result.units_imported == 2
        assert result.sessions_imported == 1
    
    def test_import_creates_units(self, test_db):
        """Verify import creates learning units."""
        data = create_valid_import_data()
        
        import_all_data(test_db, data)
        
        units = test_db.query(LearningUnit).all()
        assert len(units) == 2
        
        unit_texts = [u.text for u in units]
        assert "nowy" in unit_texts
        assert "stary" in unit_texts
    
    def test_import_creates_progress(self, test_db):
        """Verify import creates progress records."""
        data = create_valid_import_data()
        
        import_all_data(test_db, data)
        
        progress = test_db.query(LearningProgress).all()
        assert len(progress) == 1
        assert progress[0].times_seen == 3
    
    def test_import_creates_sessions(self, test_db):
        """Verify import creates learning sessions."""
        data = create_valid_import_data()
        
        import_all_data(test_db, data)
        
        sessions = test_db.query(LearningSession).all()
        assert len(sessions) == 1
    
    def test_import_creates_session_units(self, test_db):
        """Verify import creates session units."""
        data = create_valid_import_data()
        
        import_all_data(test_db, data)
        
        session_units = test_db.query(SessionUnit).all()
        assert len(session_units) == 1
    
    def test_import_creates_settings(self, test_db):
        """Verify import creates settings."""
        data = create_valid_import_data()
        
        import_all_data(test_db, data)
        
        settings = test_db.query(Settings).first()
        assert settings is not None
        assert settings.source_language == "Polish"

    def test_import_creates_additional_entities(self, test_db):
        """Verify import restores additional exported entities."""
        data = create_valid_import_data()

        import_all_data(test_db, data)

        assert test_db.query(VocabularyGroup).count() == 1
        assert test_db.query(Vocabulary).count() == 1
        assert test_db.query(AudioAsset).count() == 1
        assert test_db.query(PracticeEvent).count() == 1

        imported_unit = test_db.query(LearningUnit).filter(LearningUnit.id == 1).one()
        assert imported_unit.vocabulary_id == 1


class TestImportReplacesExistingData:
    """Tests verifying import replaces existing data."""
    
    def test_import_clears_existing_units(self, populated_db):
        """Verify import clears existing units."""
        data = create_valid_import_data()
        
        # Verify existing data
        existing_units = populated_db.query(LearningUnit).all()
        assert len(existing_units) == 1
        assert existing_units[0].text == "existing"
        
        # Import new data
        import_all_data(populated_db, data)
        
        # Verify old data is gone
        units = populated_db.query(LearningUnit).all()
        unit_texts = [u.text for u in units]
        assert "existing" not in unit_texts
        assert len(units) == 2
    
    def test_import_clears_existing_progress(self, populated_db):
        """Verify import clears existing progress."""
        data = create_valid_import_data()
        
        import_all_data(populated_db, data)
        
        # Verify only imported progress exists
        progress = populated_db.query(LearningProgress).all()
        assert len(progress) == 1
        assert progress[0].unit_id == 1  # From import, not existing


class TestImportWithEmptyData:
    """Tests for importing empty datasets."""
    
    def test_import_empty_arrays(self, test_db):
        """Verify import with empty arrays succeeds."""
        data = {
            "metadata": {
                "schema_version": "1.0",
                "app_version": "0.1.0",
                "export_timestamp": "2025-01-11T12:00:00Z",
                "source_language": "Polish",
                "target_language": "English",
                "session_size": 50,
            },
            "settings": None,
            "learning_units": [],
            "learning_progress": [],
            "learning_sessions": [],
            "session_units": [],
            "vocabulary_groups": [],
            "vocabularies": [],
            "audio_assets": [],
            "practice_events": [],
        }
        
        result = import_all_data(test_db, data)
        
        assert result.success is True
        assert result.units_imported == 0
        assert result.sessions_imported == 0


class TestFailedImportRestoresPreviousState:
    """Tests verifying failed import restores previous state."""
    
    def test_invalid_data_preserves_existing(self, populated_db):
        """Verify invalid import data doesn't affect existing data."""
        # Get count before
        units_before = populated_db.query(LearningUnit).count()
        
        # Try import with invalid data
        invalid_data = {
            "metadata": {
                "app_version": "99.0.0",  # Future version - will be rejected
                "schema_version": "1.0",
                "export_timestamp": "2025-01-11T12:00:00Z",
                "source_language": "Polish",
                "target_language": "English",
                "session_size": 50,
            },
            "learning_units": [],
            "learning_progress": [],
            "learning_sessions": [],
            "session_units": [],
        }
        
        result = import_all_data(populated_db, invalid_data)
        
        assert result.success is False
        
        # Verify data is unchanged
        units_after = populated_db.query(LearningUnit).count()
        assert units_after == units_before


class TestBackupCreation:
    """Tests for backup creation."""
    
    def test_backup_created_on_import(self, test_db):
        """Verify backup is created during import."""
        data = create_valid_import_data()
        
        result = import_all_data(test_db, data)
        
        assert result.success is True
        assert result.backup_created is True
    
    def test_import_message_indicates_success(self, test_db):
        """Verify success message is informative."""
        data = create_valid_import_data()
        
        result = import_all_data(test_db, data)
        
        assert "success" in result.message.lower()


class TestValidationBeforeImport:
    """Tests verifying validation happens before import."""
    
    def test_validation_failure_prevents_import(self, test_db):
        """Verify validation failure prevents any data changes."""
        # Add some existing data
        unit = LearningUnit(
            id=1,
            text="test",
            type=UnitType.WORD,
            translation="test",
            source_pdf="test.pdf",
        )
        test_db.add(unit)
        test_db.commit()
        
        # Try to import invalid data (missing required fields)
        invalid_data = {
            "metadata": {
                "app_version": "0.1.0",
            },
            "learning_units": [],
        }
        
        result = import_all_data(test_db, invalid_data)
        
        assert result.success is False
        assert "Validation failed" in result.message
        
        # Existing data should still be there
        units = test_db.query(LearningUnit).all()
        assert len(units) == 1


class TestImportExportRoundtrip:
    """Tests for export-import round-trip."""
    
    def test_export_import_roundtrip(self, test_db):
        """Verify data survives export-import round-trip."""
        # Create original data
        unit = LearningUnit(
            id=1,
            text="roundtrip",
            type=UnitType.WORD,
            translation="podróż w obie strony",
            source_pdf="test.pdf",
        )
        test_db.add(unit)
        
        progress = LearningProgress(
            id=1,
            unit_id=1,
            times_seen=5,
            times_correct=4,
            times_failed=1,
            confidence_score=0.8,
        )
        test_db.add(progress)
        test_db.commit()
        
        # Export
        export_service = ExportService(test_db)
        export_data = export_service.export_all_data()
        
        # Convert to dict (simulating file save/load)
        json_str = export_data.model_dump_json()
        import_data = json.loads(json_str)
        
        # Clear and reimport
        test_db.query(LearningProgress).delete()
        test_db.query(LearningUnit).delete()
        test_db.commit()
        
        # Import
        result = import_all_data(test_db, import_data)
        
        assert result.success is True
        
        # Verify data is restored
        units = test_db.query(LearningUnit).all()
        assert len(units) == 1
        assert units[0].text == "roundtrip"
        
        progress_records = test_db.query(LearningProgress).all()
        assert len(progress_records) == 1
        assert progress_records[0].confidence_score == 0.8
