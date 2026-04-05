"""Import service for data restoration."""

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.audio import AudioAsset
from app.models.learning_unit import LearningUnit, LearningProgress, Settings, UnitType
from app.models.practice_event import PracticeEvent
from app.models.session import LearningSession, SessionUnit
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.schemas.export_import import (
    ExportData,
    ImportValidationResult,
    ImportResponse,
)
from app.services.export_service import ExportService

logger = logging.getLogger(__name__)

# Supported import versions (current and backwards compatible)
SUPPORTED_VERSIONS = [settings.app_version]
SUPPORTED_SCHEMA_VERSIONS = ["1.0"]

# Backup directory
BACKUP_DIR = settings.data_dir / "backups"


class ImportValidator:
    """
    Validator for import data.
    
    Performs dry-run validation without any database writes.
    """
    
    def validate_import_payload(self, data: dict[str, Any]) -> ImportValidationResult:
        """
        Validate import data without writing to database.
        
        Validates:
        - Schema correctness
        - Required fields
        - Foreign key consistency
        - Version compatibility
        
        Args:
            data: Raw dictionary from import JSON.
            
        Returns:
            ImportValidationResult with validation status and any errors/warnings.
        """
        errors: list[str] = []
        warnings: list[str] = []
        
        # Validate basic structure
        structure_errors = self._validate_structure(data)
        errors.extend(structure_errors)
        
        if structure_errors:
            # Can't continue validation if structure is wrong
            return ImportValidationResult(
                valid=False,
                errors=errors,
                warnings=warnings,
                unit_count=0,
                session_count=0,
            )
        
        # Validate version compatibility
        version_result = self._validate_version(data.get("metadata", {}))
        errors.extend(version_result["errors"])
        warnings.extend(version_result["warnings"])
        
        # Validate referential integrity
        integrity_errors = self._validate_referential_integrity(data)
        errors.extend(integrity_errors)
        
        # Validate data types
        type_errors = self._validate_data_types(data)
        errors.extend(type_errors)
        
        # Count items
        unit_count = len(data.get("learning_units", []))
        session_count = len(data.get("learning_sessions", []))
        
        return ImportValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            unit_count=unit_count,
            session_count=session_count,
        )
    
    def _validate_structure(self, data: dict[str, Any]) -> list[str]:
        """Validate basic JSON structure and required fields."""
        errors: list[str] = []
        
        # Check top-level required fields
        required_fields = [
            "metadata",
            "learning_units",
            "learning_progress",
            "learning_sessions",
            "session_units",
        ]
        
        for field in required_fields:
            if field not in data:
                errors.append(f"Missing required field: {field}")
        
        # Validate metadata structure if present
        if "metadata" in data:
            metadata = data["metadata"]
            if not isinstance(metadata, dict):
                errors.append("metadata must be an object")
            else:
                metadata_required = [
                    "app_version",
                    "export_timestamp",
                    "source_language",
                    "target_language",
                    "session_size",
                ]
                for field in metadata_required:
                    if field not in metadata:
                        errors.append(f"Missing required metadata field: {field}")
        
        # Validate array fields are actually arrays
        array_fields = [
            "learning_units",
            "learning_progress",
            "learning_sessions",
            "session_units",
            "vocabularies",
            "vocabulary_groups",
            "audio_assets",
            "practice_events",
        ]
        for field in array_fields:
            if field in data and not isinstance(data[field], list):
                errors.append(f"{field} must be an array")
        
        return errors
    
    def _validate_version(self, metadata: dict[str, Any]) -> dict[str, list[str]]:
        """Validate version compatibility."""
        errors: list[str] = []
        warnings: list[str] = []
        
        schema_version = metadata.get("schema_version")
        app_version = metadata.get("app_version", "")

        if schema_version:
            if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                try:
                    import_major = int(str(schema_version).split(".")[0])
                    current_major = int(SUPPORTED_SCHEMA_VERSIONS[0].split(".")[0])
                    if import_major > current_major:
                        errors.append(
                            f"Import schema_version {schema_version} is newer than "
                            f"supported schema_version {SUPPORTED_SCHEMA_VERSIONS[0]}."
                        )
                    elif import_major < current_major:
                        warnings.append(
                            f"Import schema_version {schema_version} is older than "
                            f"supported schema_version {SUPPORTED_SCHEMA_VERSIONS[0]}. "
                            "Compatibility mode enabled."
                        )
                except (ValueError, IndexError):
                    warnings.append(f"Unable to parse schema_version: {schema_version}")

        if not app_version:
            errors.append("Missing app_version in metadata")
            return {"errors": errors, "warnings": warnings}
        
        if app_version not in SUPPORTED_VERSIONS:
            # Try to parse major.minor.patch
            try:
                import_parts = app_version.split(".")
                current_parts = settings.app_version.split(".")
                
                if len(import_parts) >= 2 and len(current_parts) >= 2:
                    import_major = int(import_parts[0])
                    current_major = int(current_parts[0])
                    
                    if import_major > current_major:
                        errors.append(
                            f"Import version {app_version} is newer than "
                            f"current app version {settings.app_version}. "
                            "Please update the app before importing."
                        )
                    elif import_major < current_major:
                        warnings.append(
                            f"Import version {app_version} is older than "
                            f"current version {settings.app_version}. "
                            "Some data may be migrated."
                        )
            except (ValueError, IndexError):
                warnings.append(f"Unable to parse version: {app_version}")
        
        return {"errors": errors, "warnings": warnings}
    
    def _validate_referential_integrity(self, data: dict[str, Any]) -> list[str]:
        """Validate foreign key relationships."""
        errors: list[str] = []
        
        # Build sets of valid IDs
        unit_ids = {u.get("id") for u in data.get("learning_units", []) if u.get("id")}
        session_ids = {s.get("id") for s in data.get("learning_sessions", []) if s.get("id")}
        vocabulary_ids = {v.get("id") for v in data.get("vocabularies", []) if v.get("id")}
        vocabulary_group_ids = {
            g.get("id") for g in data.get("vocabulary_groups", []) if g.get("id")
        }
        
        # Validate learning_progress references valid units
        for progress in data.get("learning_progress", []):
            unit_id = progress.get("unit_id")
            if unit_id and unit_id not in unit_ids:
                errors.append(
                    f"learning_progress references invalid unit_id: {unit_id}"
                )
        
        # Validate session_units references
        for session_unit in data.get("session_units", []):
            unit_id = session_unit.get("unit_id")
            session_id = session_unit.get("session_id")
            
            if unit_id and unit_id not in unit_ids:
                errors.append(
                    f"session_unit references invalid unit_id: {unit_id}"
                )
            
            if session_id and session_id not in session_ids:
                errors.append(
                    f"session_unit references invalid session_id: {session_id}"
                )

        for unit in data.get("learning_units", []):
            vocabulary_id = unit.get("vocabulary_id")
            if vocabulary_id and vocabulary_id not in vocabulary_ids:
                errors.append(
                    f"learning_unit references invalid vocabulary_id: {vocabulary_id}"
                )

        for vocabulary in data.get("vocabularies", []):
            group_id = vocabulary.get("group_id")
            if group_id and group_id not in vocabulary_group_ids:
                errors.append(
                    f"vocabulary references invalid group_id: {group_id}"
                )

        for asset in data.get("audio_assets", []):
            unit_id = asset.get("unit_id")
            if unit_id and unit_id not in unit_ids:
                errors.append(
                    f"audio_asset references invalid unit_id: {unit_id}"
                )
        
        return errors
    
    def _validate_data_types(self, data: dict[str, Any]) -> list[str]:
        """Validate data types for key fields."""
        errors: list[str] = []
        
        # Validate learning units
        for i, unit in enumerate(data.get("learning_units", [])):
            if not isinstance(unit, dict):
                errors.append(f"learning_units[{i}] must be an object")
                continue
            
            # Required fields
            if not unit.get("text"):
                errors.append(f"learning_units[{i}] missing required field: text")
            if not unit.get("translation"):
                errors.append(f"learning_units[{i}] missing required field: translation")
            if not unit.get("type"):
                errors.append(f"learning_units[{i}] missing required field: type")
            elif unit.get("type") not in ["word", "phrase", "sentence"]:
                errors.append(
                    f"learning_units[{i}] has invalid type: {unit.get('type')}. "
                    "Must be word, phrase, or sentence."
                )
        
        # Validate learning progress
        for i, progress in enumerate(data.get("learning_progress", [])):
            if not isinstance(progress, dict):
                errors.append(f"learning_progress[{i}] must be an object")
                continue
            
            if progress.get("unit_id") is None:
                errors.append(f"learning_progress[{i}] missing required field: unit_id")
        
        # Validate session units
        for i, su in enumerate(data.get("session_units", [])):
            if not isinstance(su, dict):
                errors.append(f"session_units[{i}] must be an object")
                continue
            
            if su.get("session_id") is None:
                errors.append(f"session_units[{i}] missing required field: session_id")
            if su.get("unit_id") is None:
                errors.append(f"session_units[{i}] missing required field: unit_id")
            if su.get("position") is None:
                errors.append(f"session_units[{i}] missing required field: position")

        for i, vocabulary in enumerate(data.get("vocabularies", [])):
            if not isinstance(vocabulary, dict):
                errors.append(f"vocabularies[{i}] must be an object")
                continue

            if not vocabulary.get("user_key"):
                errors.append(f"vocabularies[{i}] missing required field: user_key")
            if not vocabulary.get("name"):
                errors.append(f"vocabularies[{i}] missing required field: name")

        for i, group in enumerate(data.get("vocabulary_groups", [])):
            if not isinstance(group, dict):
                errors.append(f"vocabulary_groups[{i}] must be an object")
                continue

            if not group.get("user_key"):
                errors.append(f"vocabulary_groups[{i}] missing required field: user_key")
            if not group.get("name"):
                errors.append(f"vocabulary_groups[{i}] missing required field: name")

        for i, asset in enumerate(data.get("audio_assets", [])):
            if not isinstance(asset, dict):
                errors.append(f"audio_assets[{i}] must be an object")
                continue

            for field in ["unit_id", "engine", "voice", "language", "audio_hash", "file_path"]:
                if asset.get(field) is None:
                    errors.append(f"audio_assets[{i}] missing required field: {field}")

        for i, event in enumerate(data.get("practice_events", [])):
            if not isinstance(event, dict):
                errors.append(f"practice_events[{i}] must be an object")
                continue

            if not event.get("event_type"):
                errors.append(f"practice_events[{i}] missing required field: event_type")
            if "payload" not in event:
                errors.append(f"practice_events[{i}] missing required field: payload")
        
        return errors


def validate_import_payload(data: dict[str, Any]) -> ImportValidationResult:
    """
    Validate import payload without writing to database.
    
    Convenience function that creates an ImportValidator and runs validation.
    
    Args:
        data: Raw dictionary from import JSON.
        
    Returns:
        ImportValidationResult with validation status.
    """
    validator = ImportValidator()
    return validator.validate_import_payload(data)


class ImportService:
    """
    Service for importing data with automatic backup and rollback.
    
    Provides safe import functionality that:
    1. Creates a backup before any changes
    2. Validates data before import
    3. Wipes existing data and imports new data
    4. Rolls back to backup if any error occurs
    """
    
    def __init__(self, db: Session):
        """
        Initialize import service.
        
        Args:
            db: SQLAlchemy database session.
        """
        self.db = db
        self._backup_path: Path | None = None
    
    def import_all_data(self, data: dict[str, Any]) -> ImportResponse:
        """
        Import all data from export payload.
        
        Creates automatic backup before import. If any error occurs,
        restores from backup.
        
        Args:
            data: Dictionary containing export data.
            
        Returns:
            ImportResponse with result status.
        """
        logger.info("Starting data import")
        
        # Validate first
        validator = ImportValidator()
        validation = validator.validate_import_payload(data)
        
        if not validation.valid:
            logger.error(f"Import validation failed: {validation.errors}")
            return ImportResponse(
                success=False,
                message=f"Validation failed: {'; '.join(validation.errors)}",
                units_imported=0,
                sessions_imported=0,
                backup_created=False,
            )
        
        # Create backup before making any changes
        backup_created = self._create_backup()
        
        try:
            # Clear existing data in correct order (respecting FK constraints)
            self._clear_existing_data()
            
            # Import data in correct order (respecting FK constraints)
            self._import_vocabulary_groups(data.get("vocabulary_groups", []))
            self._import_vocabularies(data.get("vocabularies", []))
            units_imported = self._import_learning_units(data.get("learning_units", []))
            self._import_learning_progress(data.get("learning_progress", []))
            sessions_imported = self._import_learning_sessions(data.get("learning_sessions", []))
            self._import_session_units(data.get("session_units", []))
            self._import_audio_assets(data.get("audio_assets", []))
            self._import_practice_events(data.get("practice_events", []))
            self._import_settings(data.get("settings"))
            
            # Commit transaction
            self.db.commit()
            
            logger.info(
                f"Import successful: {units_imported} units, {sessions_imported} sessions"
            )
            
            return ImportResponse(
                success=True,
                message="Data imported successfully",
                units_imported=units_imported,
                sessions_imported=sessions_imported,
                backup_created=backup_created,
            )
            
        except Exception as e:
            logger.error(f"Import failed: {e}")
            
            # Rollback the transaction
            self.db.rollback()
            
            # Restore from backup
            if backup_created and self._backup_path:
                self._restore_backup()
            
            return ImportResponse(
                success=False,
                message=f"Import failed: {str(e)}. Previous data restored from backup.",
                units_imported=0,
                sessions_imported=0,
                backup_created=backup_created,
            )
    
    def _create_backup(self) -> bool:
        """
        Create a backup of the current database.
        
        Returns:
            True if backup was created, False otherwise.
        """
        try:
            # Ensure backup directory exists
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            
            # Create backup filename with timestamp
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self._backup_path = BACKUP_DIR / f"backup_{timestamp}.json"
            
            # Export current data
            export_service = ExportService(self.db)
            export_data = export_service.export_all_data()
            
            # Write to backup file
            with open(self._backup_path, "w", encoding="utf-8") as f:
                f.write(export_data.model_dump_json(indent=2))
            
            logger.info(f"Backup created: {self._backup_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create backup: {e}")
            return False
    
    def _restore_backup(self) -> bool:
        """
        Restore data from backup.
        
        Returns:
            True if restore was successful, False otherwise.
        """
        if not self._backup_path or not self._backup_path.exists():
            logger.error("No backup file found for restore")
            return False
        
        try:
            import json
            
            # Read backup data
            with open(self._backup_path, "r", encoding="utf-8") as f:
                backup_data = json.load(f)
            
            # Clear current data
            self._clear_existing_data()
            
            # Restore from backup
            self._import_vocabulary_groups(backup_data.get("vocabulary_groups", []))
            self._import_vocabularies(backup_data.get("vocabularies", []))
            self._import_learning_units(backup_data.get("learning_units", []))
            self._import_learning_progress(backup_data.get("learning_progress", []))
            self._import_learning_sessions(backup_data.get("learning_sessions", []))
            self._import_session_units(backup_data.get("session_units", []))
            self._import_audio_assets(backup_data.get("audio_assets", []))
            self._import_practice_events(backup_data.get("practice_events", []))
            self._import_settings(backup_data.get("settings"))
            
            self.db.commit()
            
            logger.info("Data restored from backup")
            return True
            
        except Exception as e:
            logger.error(f"Failed to restore backup: {e}")
            return False
    
    def _clear_existing_data(self) -> None:
        """Clear all existing data from database."""
        # Delete in reverse order of dependencies
        self.db.query(SessionUnit).delete()
        self.db.query(LearningSession).delete()
        self.db.query(LearningProgress).delete()
        self.db.query(AudioAsset).delete()
        self.db.query(LearningUnit).delete()
        self.db.query(Vocabulary).delete()
        self.db.query(VocabularyGroup).delete()
        self.db.query(PracticeEvent).delete()
        self.db.query(Settings).delete()
        self.db.flush()

    def _import_vocabulary_groups(self, groups_data: list[dict]) -> int:
        """Import vocabulary groups."""
        count = 0
        for group_data in groups_data:
            group = VocabularyGroup(
                id=group_data.get("id"),
                user_key=group_data["user_key"],
                name=group_data["name"],
                description=group_data.get("description"),
                display_order=group_data.get("display_order", 0),
            )
            if group_data.get("created_at"):
                group.created_at = self._parse_datetime(group_data["created_at"])

            self.db.add(group)
            count += 1

        self.db.flush()
        return count

    def _import_vocabularies(self, vocabularies_data: list[dict]) -> int:
        """Import vocabularies."""
        count = 0
        for vocabulary_data in vocabularies_data:
            vocabulary = Vocabulary(
                id=vocabulary_data.get("id"),
                user_key=vocabulary_data["user_key"],
                name=vocabulary_data["name"],
                group_id=vocabulary_data.get("group_id"),
            )
            if vocabulary_data.get("created_at"):
                vocabulary.created_at = self._parse_datetime(vocabulary_data["created_at"])

            self.db.add(vocabulary)
            count += 1

        self.db.flush()
        return count

    def _import_learning_units(self, units_data: list[dict]) -> int:
        """Import learning units."""
        count = 0
        for unit_data in units_data:
            unit = LearningUnit(
                id=unit_data.get("id"),
                text=unit_data["text"],
                type=UnitType(unit_data["type"]),
                part_of_speech=unit_data.get("part_of_speech"),
                translation=unit_data["translation"],
                source_pdf=unit_data.get("source_pdf", "imported"),
                vocabulary_id=unit_data.get("vocabulary_id"),
                page_number=unit_data.get("page_number"),
                lesson_title=unit_data.get("lesson_title"),
            )
            # Handle created_at if present
            if unit_data.get("created_at"):
                unit.created_at = self._parse_datetime(unit_data["created_at"])
            
            self.db.add(unit)
            count += 1
        
        self.db.flush()
        return count
    
    def _import_learning_progress(self, progress_data: list[dict]) -> int:
        """Import learning progress records."""
        count = 0
        for prog_data in progress_data:
            progress = LearningProgress(
                id=prog_data.get("id"),
                unit_id=prog_data["unit_id"],
                times_seen=prog_data.get("times_seen", 0),
                times_correct=prog_data.get("times_correct", 0),
                times_failed=prog_data.get("times_failed", 0),
                confidence_score=prog_data.get("confidence_score", 0.0),
            )
            if prog_data.get("last_seen"):
                progress.last_seen = self._parse_datetime(prog_data["last_seen"])
            
            self.db.add(progress)
            count += 1
        
        self.db.flush()
        return count
    
    def _import_learning_sessions(self, sessions_data: list[dict]) -> int:
        """Import learning sessions."""
        count = 0
        for session_data in sessions_data:
            session = LearningSession(
                id=session_data.get("id"),
                locked=session_data.get("locked", True),
                completed=session_data.get("completed", False),
            )
            if session_data.get("created_at"):
                session.created_at = self._parse_datetime(session_data["created_at"])
            if session_data.get("completed_at"):
                session.completed_at = self._parse_datetime(session_data["completed_at"])
            
            self.db.add(session)
            count += 1
        
        self.db.flush()
        return count
    
    def _import_session_units(self, session_units_data: list[dict]) -> int:
        """Import session units."""
        count = 0
        for su_data in session_units_data:
            session_unit = SessionUnit(
                id=su_data.get("id"),
                session_id=su_data["session_id"],
                unit_id=su_data["unit_id"],
                position=su_data["position"],
                answered=su_data.get("answered", False),
                is_correct=su_data.get("is_correct"),
            )
            if su_data.get("answered_at"):
                session_unit.answered_at = self._parse_datetime(su_data["answered_at"])
            
            self.db.add(session_unit)
            count += 1
        
        self.db.flush()
        return count
    
    def _import_settings(self, settings_data: dict | None) -> None:
        """Import settings."""
        if not settings_data:
            return
        
        db_settings = Settings(
            id=settings_data.get("id", 1),
            offline_mode=settings_data.get("offline_mode", True),
            ai_provider=settings_data.get("ai_provider", "ollama"),
            ollama_model=settings_data.get("ollama_model", "llama3.2"),
            strict_mode=settings_data.get("strict_mode", True),
            source_language=settings_data.get("source_language", "Polish"),
            target_language=settings_data.get("target_language", "English"),
        )
        self.db.add(db_settings)
        self.db.flush()

    def _import_audio_assets(self, audio_assets_data: list[dict]) -> int:
        """Import audio assets."""
        count = 0
        for asset_data in audio_assets_data:
            asset = AudioAsset(
                id=asset_data.get("id"),
                unit_id=asset_data["unit_id"],
                engine=asset_data["engine"],
                voice=asset_data["voice"],
                language=asset_data["language"],
                audio_hash=asset_data["audio_hash"],
                file_path=asset_data["file_path"],
            )
            if asset_data.get("created_at"):
                asset.created_at = self._parse_datetime(asset_data["created_at"])

            self.db.add(asset)
            count += 1

        self.db.flush()
        return count

    def _import_practice_events(self, practice_events_data: list[dict]) -> int:
        """Import practice events."""
        count = 0
        for event_data in practice_events_data:
            event = PracticeEvent(
                id=event_data.get("id"),
                event_type=event_data["event_type"],
                theme=event_data.get("theme"),
                payload=event_data.get("payload", {}),
            )
            if event_data.get("created_at"):
                event.created_at = self._parse_datetime(event_data["created_at"])

            self.db.add(event)
            count += 1

        self.db.flush()
        return count
    
    def _parse_datetime(self, dt_str: str) -> datetime:
        """Parse datetime string to datetime object."""
        # Handle various datetime formats
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        
        try:
            return datetime.fromisoformat(dt_str)
        except ValueError:
            # Fallback for other formats
            return datetime.now(timezone.utc)


def import_all_data(db: Session, data: dict[str, Any]) -> ImportResponse:
    """
    Import all data with automatic backup and rollback.
    
    Convenience function that creates an ImportService and runs import.
    
    Args:
        db: SQLAlchemy database session.
        data: Dictionary containing export data.
        
    Returns:
        ImportResponse with result status.
    """
    service = ImportService(db)
    return service.import_all_data(data)
