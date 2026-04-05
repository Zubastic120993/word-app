"""Tests for export/import schema validation."""

import pytest
from datetime import datetime

from app.schemas.export_import import (
    ExportAudioAsset,
    ExportLearningUnit,
    ExportLearningProgress,
    ExportLearningSession,
    ExportPracticeEvent,
    ExportSessionUnit,
    ExportSettings,
    ExportMetadata,
    ExportData,
    ExportVocabulary,
    ExportVocabularyGroup,
    ImportValidationResult,
)
from app.models.learning_unit import UnitType


class TestExportSchemaCompleteness:
    """Tests verifying export schema includes all required tables."""
    
    def test_export_data_contains_all_tables(self):
        """Verify ExportData schema has fields for all database tables."""
        # Check that ExportData has all required fields
        export_fields = ExportData.model_fields.keys()
        
        required_fields = [
            "metadata",
            "settings",
            "learning_units",
            "learning_progress",
            "learning_sessions",
            "session_units",
            "vocabularies",
            "vocabulary_groups",
            "audio_assets",
            "practice_events",
        ]
        
        for field in required_fields:
            assert field in export_fields, f"Missing field: {field}"
    
    def test_metadata_includes_app_version(self):
        """Verify metadata includes app version."""
        metadata_fields = ExportMetadata.model_fields.keys()
        assert "app_version" in metadata_fields
    
    def test_metadata_includes_timestamp(self):
        """Verify metadata includes export timestamp."""
        metadata_fields = ExportMetadata.model_fields.keys()
        assert "export_timestamp" in metadata_fields
    
    def test_metadata_includes_language_settings(self):
        """Verify metadata includes language configuration."""
        metadata_fields = ExportMetadata.model_fields.keys()
        assert "source_language" in metadata_fields
        assert "target_language" in metadata_fields
        assert "session_size" in metadata_fields


class TestExportSchemaReferentialIntegrity:
    """Tests verifying export schema preserves foreign key relationships."""
    
    def test_learning_progress_references_unit_id(self):
        """Verify learning progress schema has unit_id for FK reference."""
        progress_fields = ExportLearningProgress.model_fields.keys()
        assert "unit_id" in progress_fields
        assert "id" in progress_fields  # Own primary key
    
    def test_session_unit_references_session_id(self):
        """Verify session unit schema has session_id for FK reference."""
        session_unit_fields = ExportSessionUnit.model_fields.keys()
        assert "session_id" in session_unit_fields
        assert "unit_id" in session_unit_fields
        assert "id" in session_unit_fields  # Own primary key

    def test_additional_entity_reference_fields_present(self):
        """Verify additional export entities preserve linking fields."""
        vocabulary_fields = ExportVocabulary.model_fields.keys()
        audio_asset_fields = ExportAudioAsset.model_fields.keys()
        practice_event_fields = ExportPracticeEvent.model_fields.keys()
        vocabulary_group_fields = ExportVocabularyGroup.model_fields.keys()

        assert "group_id" in vocabulary_fields
        assert "unit_id" in audio_asset_fields
        assert "payload" in practice_event_fields
        assert "user_key" in vocabulary_group_fields
    
    def test_referential_integrity_preserved_in_full_export(self):
        """Verify a complete export maintains referential integrity."""
        # Create sample data with proper FK relationships
        export_data = ExportData(
            metadata=ExportMetadata(
                app_version="0.1.0",
                export_timestamp=datetime.now(),
                source_language="Polish",
                target_language="English",
                session_size=50,
            ),
            settings=ExportSettings(
                id=1,
                offline_mode=True,
                ai_provider="ollama",
                ollama_model="llama3.2",
                strict_mode=True,
                source_language="Polish",
                target_language="English",
            ),
            learning_units=[
                ExportLearningUnit(
                    id=1,
                    text="dom",
                    type=UnitType.WORD,
                    translation="house",
                    source_pdf="test.pdf",
                    created_at=datetime.now(),
                ),
                ExportLearningUnit(
                    id=2,
                    text="kot",
                    type=UnitType.WORD,
                    translation="cat",
                    source_pdf="test.pdf",
                    created_at=datetime.now(),
                ),
            ],
            learning_progress=[
                ExportLearningProgress(
                    id=1,
                    unit_id=1,  # References learning_unit id=1
                    times_seen=5,
                    times_correct=3,
                    times_failed=2,
                    confidence_score=0.6,
                ),
                ExportLearningProgress(
                    id=2,
                    unit_id=2,  # References learning_unit id=2
                    times_seen=10,
                    times_correct=8,
                    times_failed=2,
                    confidence_score=0.8,
                ),
            ],
            learning_sessions=[
                ExportLearningSession(
                    id=1,
                    created_at=datetime.now(),
                    locked=True,
                    completed=False,
                ),
            ],
            session_units=[
                ExportSessionUnit(
                    id=1,
                    session_id=1,  # References learning_session id=1
                    unit_id=1,  # References learning_unit id=1
                    position=1,
                    answered=False,
                ),
                ExportSessionUnit(
                    id=2,
                    session_id=1,  # References learning_session id=1
                    unit_id=2,  # References learning_unit id=2
                    position=2,
                    answered=False,
                ),
            ],
        )
        
        # Validate FK relationships
        unit_ids = {u.id for u in export_data.learning_units}
        session_ids = {s.id for s in export_data.learning_sessions}
        
        # Check progress references valid units
        for progress in export_data.learning_progress:
            assert progress.unit_id in unit_ids, \
                f"Progress references invalid unit_id: {progress.unit_id}"
        
        # Check session_units reference valid sessions and units
        for su in export_data.session_units:
            assert su.session_id in session_ids, \
                f"SessionUnit references invalid session_id: {su.session_id}"
            assert su.unit_id in unit_ids, \
                f"SessionUnit references invalid unit_id: {su.unit_id}"


class TestExportSchemaJsonSerialization:
    """Tests verifying export schema serializes correctly to JSON."""
    
    def test_export_data_json_serializable(self):
        """Verify ExportData can be serialized to JSON."""
        export_data = ExportData(
            metadata=ExportMetadata(
                app_version="0.1.0",
                export_timestamp=datetime.now(),
                source_language="Polish",
                target_language="English",
                session_size=50,
            ),
            learning_units=[],
            learning_progress=[],
            learning_sessions=[],
            session_units=[],
            vocabularies=[],
            vocabulary_groups=[],
            audio_assets=[],
            practice_events=[],
        )
        
        # Should not raise
        json_str = export_data.model_dump_json()
        assert isinstance(json_str, str)
        assert "app_version" in json_str
        assert "0.1.0" in json_str
    
    def test_export_data_human_readable(self):
        """Verify export JSON is human-readable with proper formatting."""
        export_data = ExportData(
            metadata=ExportMetadata(
                app_version="0.1.0",
                export_timestamp=datetime(2025, 1, 11, 12, 0, 0),
                source_language="Polish",
                target_language="English",
                session_size=50,
            ),
            learning_units=[
                ExportLearningUnit(
                    id=1,
                    text="dom",
                    type=UnitType.WORD,
                    translation="house",
                    source_pdf="test.pdf",
                    created_at=datetime(2025, 1, 1, 10, 0, 0),
                ),
            ],
            learning_progress=[],
            learning_sessions=[],
            session_units=[],
            vocabularies=[],
            vocabulary_groups=[],
            audio_assets=[],
            practice_events=[],
        )
        
        # Get formatted JSON with indentation
        json_str = export_data.model_dump_json(indent=2)
        
        # Should be human-readable (contains newlines and indentation)
        assert "\n" in json_str
        assert "  " in json_str  # Has indentation
        
        # Contains readable field names
        assert "learning_units" in json_str
        assert "dom" in json_str
        assert "house" in json_str


class TestImportValidationSchema:
    """Tests for import validation result schema."""
    
    def test_validation_result_structure(self):
        """Verify validation result has expected fields."""
        result_fields = ImportValidationResult.model_fields.keys()
        
        assert "valid" in result_fields
        assert "errors" in result_fields
        assert "warnings" in result_fields
        assert "unit_count" in result_fields
        assert "session_count" in result_fields
    
    def test_validation_result_valid_case(self):
        """Verify valid validation result."""
        result = ImportValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            unit_count=50,
            session_count=3,
        )
        
        assert result.valid is True
        assert len(result.errors) == 0
    
    def test_validation_result_invalid_case(self):
        """Verify invalid validation result with errors."""
        result = ImportValidationResult(
            valid=False,
            errors=["Missing required field: metadata", "Invalid unit type"],
            warnings=["Old version format"],
            unit_count=0,
            session_count=0,
        )
        
        assert result.valid is False
        assert len(result.errors) == 2
        assert len(result.warnings) == 1
