"""Tests for export service."""

import pytest
import json
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.audio import AudioAsset
from app.models.learning_unit import LearningUnit, LearningProgress, Settings, UnitType
from app.models.practice_event import PracticeEvent
from app.models.session import LearningSession, SessionUnit
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.services.export_service import ExportService
from app.schemas.export_import import ExportData


@pytest.fixture
def test_db():
    """Create a test database with sample data."""
    # Create in-memory SQLite database
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    db = TestSessionLocal()
    
    # Create settings
    db_settings = Settings(
        id=1,
        offline_mode=True,
        ai_provider="ollama",
        ollama_model="llama3.2",
        strict_mode=True,
        source_language="Polish",
        target_language="English",
    )
    db.add(db_settings)
    
    # Create learning units
    units = [
        LearningUnit(
            id=1,
            text="dom",
            type=UnitType.WORD,
            translation="house",
            source_pdf="test.pdf",
            created_at=datetime(2025, 1, 1, 10, 0, 0),
        ),
        LearningUnit(
            id=2,
            text="kot",
            type=UnitType.WORD,
            translation="cat",
            source_pdf="test.pdf",
            created_at=datetime(2025, 1, 1, 10, 0, 1),
        ),
        LearningUnit(
            id=3,
            text="pies",
            type=UnitType.WORD,
            translation="dog",
            source_pdf="test.pdf",
            created_at=datetime(2025, 1, 1, 10, 0, 2),
        ),
    ]
    for unit in units:
        db.add(unit)

    vocabulary_group = VocabularyGroup(
        id=1,
        user_key="default",
        name="Core",
        description="Core vocabulary",
        display_order=1,
        created_at=datetime(2025, 1, 1, 9, 0, 0),
    )
    db.add(vocabulary_group)

    vocabulary = Vocabulary(
        id=1,
        user_key="default",
        name="Starter Set",
        group_id=1,
        created_at=datetime(2025, 1, 1, 9, 5, 0),
    )
    db.add(vocabulary)
    
    # Create progress records
    progress_records = [
        LearningProgress(
            id=1,
            unit_id=1,
            times_seen=5,
            times_correct=3,
            times_failed=2,
            confidence_score=0.6,
            last_seen=datetime(2025, 1, 10, 12, 0, 0),
        ),
        LearningProgress(
            id=2,
            unit_id=2,
            times_seen=10,
            times_correct=8,
            times_failed=2,
            confidence_score=0.8,
            last_seen=datetime(2025, 1, 10, 12, 30, 0),
        ),
    ]
    for progress in progress_records:
        db.add(progress)
    
    # Create a learning session
    session = LearningSession(
        id=1,
        created_at=datetime(2025, 1, 10, 11, 0, 0),
        locked=True,
        completed=False,
    )
    db.add(session)
    
    # Create session units
    session_units = [
        SessionUnit(
            id=1,
            session_id=1,
            unit_id=1,
            position=1,
            answered=True,
            is_correct=True,
            answered_at=datetime(2025, 1, 10, 11, 5, 0),
        ),
        SessionUnit(
            id=2,
            session_id=1,
            unit_id=2,
            position=2,
            answered=False,
        ),
    ]
    for su in session_units:
        db.add(su)

    audio_asset = AudioAsset(
        id=1,
        unit_id=1,
        engine="murf",
        voice="en-US-marcus",
        language="en-US",
        audio_hash="hash-1",
        file_path="data/audio/hash-1.mp3",
        created_at=datetime(2025, 1, 10, 11, 10, 0),
    )
    db.add(audio_asset)

    practice_event = PracticeEvent(
        id=1,
        created_at=datetime(2025, 1, 10, 11, 15, 0),
        event_type="quiz_answer",
        theme="basics",
        payload={"unit_id": 1, "correct": True},
    )
    db.add(practice_event)
    
    db.commit()
    
    yield db
    
    db.close()


@pytest.fixture
def empty_db():
    """Create an empty test database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    db = TestSessionLocal()
    yield db
    db.close()


class TestExportServiceCounts:
    """Tests verifying export contains correct counts."""
    
    def test_export_contains_all_units(self, test_db):
        """Verify export contains all learning units."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert len(export_data.learning_units) == 3
    
    def test_export_contains_all_progress(self, test_db):
        """Verify export contains all progress records."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert len(export_data.learning_progress) == 2
    
    def test_export_contains_all_sessions(self, test_db):
        """Verify export contains all sessions."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert len(export_data.learning_sessions) == 1
    
    def test_export_contains_all_session_units(self, test_db):
        """Verify export contains all session units."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert len(export_data.session_units) == 2
    
    def test_export_contains_settings(self, test_db):
        """Verify export contains settings."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert export_data.settings is not None
        assert export_data.settings.source_language == "Polish"
        assert export_data.settings.target_language == "English"

    def test_export_contains_additional_entities(self, test_db):
        """Verify export contains additional related entities."""
        service = ExportService(test_db)
        export_data = service.export_all_data()

        assert len(export_data.vocabularies) == 1
        assert len(export_data.vocabulary_groups) == 1
        assert len(export_data.audio_assets) == 1
        assert len(export_data.practice_events) == 1
    
    def test_export_empty_db(self, empty_db):
        """Verify export works with empty database."""
        service = ExportService(empty_db)
        export_data = service.export_all_data()
        
        assert len(export_data.learning_units) == 0
        assert len(export_data.learning_progress) == 0
        assert len(export_data.learning_sessions) == 0
        assert len(export_data.session_units) == 0
        assert len(export_data.vocabularies) == 0
        assert len(export_data.vocabulary_groups) == 0
        assert len(export_data.audio_assets) == 0
        assert len(export_data.practice_events) == 0
        assert export_data.settings is None


class TestExportServiceMetadata:
    """Tests for export metadata."""
    
    def test_metadata_includes_app_version(self, test_db):
        """Verify metadata contains app version."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert export_data.metadata.app_version is not None
        assert len(export_data.metadata.app_version) > 0
    
    def test_metadata_includes_timestamp(self, test_db):
        """Verify metadata contains export timestamp."""
        service = ExportService(test_db)
        before = datetime.now(timezone.utc)
        export_data = service.export_all_data()
        after = datetime.now(timezone.utc)
        
        assert export_data.metadata.export_timestamp >= before
        assert export_data.metadata.export_timestamp <= after
    
    def test_metadata_includes_languages(self, test_db):
        """Verify metadata contains language settings."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        assert export_data.metadata.source_language == "Polish"
        assert export_data.metadata.target_language == "English"


class TestExportServiceJsonSerialization:
    """Tests verifying export is JSON serializable."""
    
    def test_export_is_json_serializable(self, test_db):
        """Verify export can be serialized to JSON."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        # Should not raise
        json_str = export_data.model_dump_json()
        assert isinstance(json_str, str)
    
    def test_export_json_roundtrip(self, test_db):
        """Verify export can be serialized and deserialized."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        # Serialize to JSON
        json_str = export_data.model_dump_json()
        
        # Parse back
        parsed = json.loads(json_str)
        
        # Verify structure
        assert "metadata" in parsed
        assert "learning_units" in parsed
        assert len(parsed["learning_units"]) == 3
    
    def test_export_contains_unit_data(self, test_db):
        """Verify export contains actual unit data."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        json_str = export_data.model_dump_json()
        
        # Should contain actual data
        assert "dom" in json_str
        assert "house" in json_str
        assert "kot" in json_str


class TestExportServiceOrdering:
    """Tests verifying export ordering is stable."""
    
    def test_units_ordered_by_id(self, test_db):
        """Verify learning units are ordered by ID."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        unit_ids = [u.id for u in export_data.learning_units]
        assert unit_ids == sorted(unit_ids)
    
    def test_progress_ordered_by_id(self, test_db):
        """Verify progress records are ordered by ID."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        progress_ids = [p.id for p in export_data.learning_progress]
        assert progress_ids == sorted(progress_ids)
    
    def test_sessions_ordered_by_id(self, test_db):
        """Verify sessions are ordered by ID."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        session_ids = [s.id for s in export_data.learning_sessions]
        assert session_ids == sorted(session_ids)
    
    def test_session_units_ordered_by_session_and_position(self, test_db):
        """Verify session units are ordered by session_id and position."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        # Get (session_id, position) tuples
        positions = [(su.session_id, su.position) for su in export_data.session_units]
        assert positions == sorted(positions)
    
    def test_export_is_deterministic(self, test_db):
        """Verify multiple exports produce identical output."""
        service = ExportService(test_db)
        
        export1 = service.export_all_data()
        export2 = service.export_all_data()
        
        # Compare JSON output (excluding timestamp)
        json1 = export1.model_dump_json()
        json2 = export2.model_dump_json()
        
        # Parse and compare everything except timestamp
        dict1 = json.loads(json1)
        dict2 = json.loads(json2)
        
        # Remove timestamp for comparison
        del dict1["metadata"]["export_timestamp"]
        del dict2["metadata"]["export_timestamp"]
        
        assert dict1 == dict2


class TestExportServiceDataIntegrity:
    """Tests verifying data integrity in export."""
    
    def test_unit_data_matches_db(self, test_db):
        """Verify exported unit data matches database."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        # Find specific unit
        unit = next(u for u in export_data.learning_units if u.id == 1)
        
        assert unit.text == "dom"
        assert unit.translation == "house"
        assert unit.source_pdf == "test.pdf"
    
    def test_progress_data_matches_db(self, test_db):
        """Verify exported progress data matches database."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        # Find specific progress
        progress = next(p for p in export_data.learning_progress if p.unit_id == 1)
        
        assert progress.times_seen == 5
        assert progress.times_correct == 3
        assert progress.confidence_score == 0.6
    
    def test_session_unit_references_valid(self, test_db):
        """Verify session units reference valid sessions and units."""
        service = ExportService(test_db)
        export_data = service.export_all_data()
        
        unit_ids = {u.id for u in export_data.learning_units}
        session_ids = {s.id for s in export_data.learning_sessions}
        
        for su in export_data.session_units:
            assert su.unit_id in unit_ids
            assert su.session_id in session_ids
