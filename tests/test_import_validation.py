"""Tests for import validation (dry-run)."""

import pytest
from datetime import datetime

from app.services.import_service import validate_import_payload, ImportValidator


def create_valid_export_data() -> dict:
    """Create valid export data for testing."""
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
                "text": "dom",
                "type": "word",
                "translation": "house",
                "source_pdf": "test.pdf",
                "vocabulary_id": 1,
                "created_at": "2025-01-01T10:00:00Z",
            },
            {
                "id": 2,
                "text": "kot",
                "type": "word",
                "translation": "cat",
                "source_pdf": "test.pdf",
                "created_at": "2025-01-01T10:00:01Z",
            },
        ],
        "learning_progress": [
            {
                "id": 1,
                "unit_id": 1,
                "times_seen": 5,
                "times_correct": 3,
                "times_failed": 2,
                "confidence_score": 0.6,
            },
        ],
        "learning_sessions": [
            {
                "id": 1,
                "created_at": "2025-01-10T11:00:00Z",
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


class TestValidImportData:
    """Tests for valid import data."""
    
    def test_valid_data_passes_validation(self):
        """Verify valid export data passes validation."""
        data = create_valid_export_data()
        result = validate_import_payload(data)
        
        assert result.valid is True
        assert len(result.errors) == 0
    
    def test_valid_data_counts(self):
        """Verify validation returns correct counts."""
        data = create_valid_export_data()
        result = validate_import_payload(data)
        
        assert result.unit_count == 2
        assert result.session_count == 1
    
    def test_empty_arrays_valid(self):
        """Verify empty data arrays are valid."""
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
        result = validate_import_payload(data)
        
        assert result.valid is True
        assert result.unit_count == 0


class TestMissingFieldsRejected:
    """Tests verifying missing fields are rejected."""
    
    def test_missing_metadata_rejected(self):
        """Verify missing metadata is rejected."""
        data = create_valid_export_data()
        del data["metadata"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("metadata" in e for e in result.errors)
    
    def test_missing_learning_units_rejected(self):
        """Verify missing learning_units is rejected."""
        data = create_valid_export_data()
        del data["learning_units"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("learning_units" in e for e in result.errors)
    
    def test_missing_learning_progress_rejected(self):
        """Verify missing learning_progress is rejected."""
        data = create_valid_export_data()
        del data["learning_progress"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("learning_progress" in e for e in result.errors)
    
    def test_missing_learning_sessions_rejected(self):
        """Verify missing learning_sessions is rejected."""
        data = create_valid_export_data()
        del data["learning_sessions"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("learning_sessions" in e for e in result.errors)
    
    def test_missing_session_units_rejected(self):
        """Verify missing session_units is rejected."""
        data = create_valid_export_data()
        del data["session_units"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("session_units" in e for e in result.errors)
    
    def test_missing_metadata_app_version_rejected(self):
        """Verify missing app_version in metadata is rejected."""
        data = create_valid_export_data()
        del data["metadata"]["app_version"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("app_version" in e for e in result.errors)


class TestInvalidSchemaRejected:
    """Tests verifying invalid schema is rejected."""
    
    def test_metadata_not_object_rejected(self):
        """Verify non-object metadata is rejected."""
        data = create_valid_export_data()
        data["metadata"] = "invalid"
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("metadata must be an object" in e for e in result.errors)
    
    def test_learning_units_not_array_rejected(self):
        """Verify non-array learning_units is rejected."""
        data = create_valid_export_data()
        data["learning_units"] = "invalid"
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("must be an array" in e for e in result.errors)
    
    def test_invalid_unit_type_rejected(self):
        """Verify invalid unit type is rejected."""
        data = create_valid_export_data()
        data["learning_units"][0]["type"] = "invalid_type"
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("invalid type" in e for e in result.errors)
    
    def test_unit_missing_text_rejected(self):
        """Verify unit missing text is rejected."""
        data = create_valid_export_data()
        del data["learning_units"][0]["text"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("missing required field: text" in e for e in result.errors)
    
    def test_unit_missing_translation_rejected(self):
        """Verify unit missing translation is rejected."""
        data = create_valid_export_data()
        del data["learning_units"][0]["translation"]
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("missing required field: translation" in e for e in result.errors)


class TestReferentialIntegrity:
    """Tests for foreign key validation."""
    
    def test_progress_invalid_unit_id_rejected(self):
        """Verify progress with invalid unit_id is rejected."""
        data = create_valid_export_data()
        data["learning_progress"][0]["unit_id"] = 999  # Invalid reference
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("invalid unit_id: 999" in e for e in result.errors)
    
    def test_session_unit_invalid_session_id_rejected(self):
        """Verify session_unit with invalid session_id is rejected."""
        data = create_valid_export_data()
        data["session_units"][0]["session_id"] = 999  # Invalid reference
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("invalid session_id: 999" in e for e in result.errors)
    
    def test_session_unit_invalid_unit_id_rejected(self):
        """Verify session_unit with invalid unit_id is rejected."""
        data = create_valid_export_data()
        data["session_units"][0]["unit_id"] = 999  # Invalid reference
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("invalid unit_id: 999" in e for e in result.errors)


class TestVersionCompatibility:
    """Tests for version compatibility checks."""
    
    def test_current_version_valid(self):
        """Verify current version passes validation."""
        data = create_valid_export_data()
        data["metadata"]["schema_version"] = "1.0"
        
        result = validate_import_payload(data)
        
        assert result.valid is True

    def test_missing_schema_version_uses_backward_compatibility(self):
        """Verify older exports without schema_version still validate."""
        data = create_valid_export_data()
        del data["metadata"]["schema_version"]

        result = validate_import_payload(data)

        assert result.valid is True

    def test_future_schema_version_rejected(self):
        """Verify future schema version is rejected."""
        data = create_valid_export_data()
        data["metadata"]["schema_version"] = "99.0"

        result = validate_import_payload(data)

        assert result.valid is False
        assert any("schema_version" in e for e in result.errors)
    
    def test_future_major_version_rejected(self):
        """Verify future major version is rejected."""
        data = create_valid_export_data()
        data["metadata"]["app_version"] = "99.0.0"
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("newer than current" in e for e in result.errors)
    
    def test_missing_version_rejected(self):
        """Verify missing version is rejected."""
        data = create_valid_export_data()
        data["metadata"]["app_version"] = ""
        
        result = validate_import_payload(data)
        
        assert result.valid is False
        assert any("app_version" in e for e in result.errors)


class TestValidationNoSideEffects:
    """Tests verifying validation has no side effects."""
    
    def test_validation_does_not_modify_input(self):
        """Verify validation does not modify input data."""
        data = create_valid_export_data()
        original_units_count = len(data["learning_units"])
        original_first_unit = data["learning_units"][0].copy()
        
        validate_import_payload(data)
        
        # Input should be unchanged
        assert len(data["learning_units"]) == original_units_count
        assert data["learning_units"][0] == original_first_unit
    
    def test_multiple_validations_consistent(self):
        """Verify multiple validations produce same result."""
        data = create_valid_export_data()
        
        result1 = validate_import_payload(data)
        result2 = validate_import_payload(data)
        
        assert result1.valid == result2.valid
        assert result1.unit_count == result2.unit_count
        assert result1.errors == result2.errors
