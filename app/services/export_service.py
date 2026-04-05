"""Export service for full data backup."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.audio import AudioAsset
from app.models.learning_unit import LearningUnit, LearningProgress, Settings
from app.models.practice_event import PracticeEvent
from app.models.session import LearningSession, SessionUnit
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.schemas.export_import import (
    ExportAudioAsset,
    ExportData,
    ExportMetadata,
    ExportLearningUnit,
    ExportLearningProgress,
    ExportLearningSession,
    ExportPracticeEvent,
    ExportSessionUnit,
    ExportSettings,
    ExportVocabulary,
    ExportVocabularyGroup,
)

logger = logging.getLogger(__name__)


class ExportService:
    """
    Service for exporting all application data.
    
    Exports data in a deterministic, complete format suitable
    for backup and restoration.
    """
    
    def __init__(self, db: Session):
        """
        Initialize export service.
        
        Args:
            db: SQLAlchemy database session.
        """
        self.db = db
    
    def export_all_data(self) -> ExportData:
        """
        Export all application data.
        
        Reads from database only (no mutations).
        Ordering is stable (by ID) for deterministic output.
        
        Returns:
            ExportData containing complete application state.
        """
        logger.info("Starting full data export")
        
        # Create metadata
        metadata = self._create_metadata()
        
        # Export settings
        db_settings = self._export_settings()
        
        # Export all tables in stable order
        learning_units = self._export_learning_units()
        learning_progress = self._export_learning_progress()
        learning_sessions = self._export_learning_sessions()
        session_units = self._export_session_units()
        vocabularies = self._export_vocabularies()
        vocabulary_groups = self._export_vocabulary_groups()
        audio_assets = self._export_audio_assets()
        practice_events = self._export_practice_events()
        
        export_data = ExportData(
            metadata=metadata,
            settings=db_settings,
            learning_units=learning_units,
            learning_progress=learning_progress,
            learning_sessions=learning_sessions,
            session_units=session_units,
            vocabularies=vocabularies,
            vocabulary_groups=vocabulary_groups,
            audio_assets=audio_assets,
            practice_events=practice_events,
        )
        
        logger.info(
            f"Export complete: {len(learning_units)} units, "
            f"{len(learning_progress)} progress records, "
            f"{len(learning_sessions)} sessions, "
            f"{len(session_units)} session units, "
            f"{len(vocabularies)} vocabularies, "
            f"{len(vocabulary_groups)} vocabulary groups, "
            f"{len(audio_assets)} audio assets, "
            f"{len(practice_events)} practice events"
        )
        
        return export_data
    
    def _create_metadata(self) -> ExportMetadata:
        """Create export metadata."""
        # Try to get settings from DB first
        db_settings = self.db.query(Settings).first()
        
        return ExportMetadata(
            app_version=settings.app_version,
            export_timestamp=datetime.now(timezone.utc),
            source_language=db_settings.source_language if db_settings else settings.source_language,
            target_language=db_settings.target_language if db_settings else settings.target_language,
            session_size=settings.session_size,
        )
    
    def _export_settings(self) -> ExportSettings | None:
        """Export application settings."""
        db_settings = self.db.query(Settings).first()
        
        if not db_settings:
            return None
        
        return ExportSettings.model_validate(db_settings)
    
    def _export_learning_units(self) -> list[ExportLearningUnit]:
        """
        Export all learning units.
        
        Ordered by ID for stable, deterministic output.
        """
        units = (
            self.db.query(LearningUnit)
            .order_by(LearningUnit.id)
            .all()
        )
        
        return [ExportLearningUnit.model_validate(unit) for unit in units]
    
    def _export_learning_progress(self) -> list[ExportLearningProgress]:
        """
        Export all learning progress records.
        
        Ordered by ID for stable, deterministic output.
        """
        progress_records = (
            self.db.query(LearningProgress)
            .order_by(LearningProgress.id)
            .all()
        )
        
        return [ExportLearningProgress.model_validate(p) for p in progress_records]
    
    def _export_learning_sessions(self) -> list[ExportLearningSession]:
        """
        Export all learning sessions.
        
        Ordered by ID for stable, deterministic output.
        """
        sessions = (
            self.db.query(LearningSession)
            .order_by(LearningSession.id)
            .all()
        )
        
        return [ExportLearningSession.model_validate(s) for s in sessions]
    
    def _export_session_units(self) -> list[ExportSessionUnit]:
        """
        Export all session units.
        
        Ordered by session_id, then position for stable output.
        """
        session_units = (
            self.db.query(SessionUnit)
            .order_by(SessionUnit.session_id, SessionUnit.position)
            .all()
        )
        
        return [ExportSessionUnit.model_validate(su) for su in session_units]

    def _export_vocabularies(self) -> list[ExportVocabulary]:
        """Export all vocabularies ordered by ID."""
        vocabularies = (
            self.db.query(Vocabulary)
            .order_by(Vocabulary.id)
            .all()
        )

        return [ExportVocabulary.model_validate(v) for v in vocabularies]

    def _export_vocabulary_groups(self) -> list[ExportVocabularyGroup]:
        """Export all vocabulary groups ordered by ID."""
        groups = (
            self.db.query(VocabularyGroup)
            .order_by(VocabularyGroup.id)
            .all()
        )

        return [ExportVocabularyGroup.model_validate(g) for g in groups]

    def _export_audio_assets(self) -> list[ExportAudioAsset]:
        """Export all audio assets ordered by ID."""
        audio_assets = (
            self.db.query(AudioAsset)
            .order_by(AudioAsset.id)
            .all()
        )

        return [ExportAudioAsset.model_validate(a) for a in audio_assets]

    def _export_practice_events(self) -> list[ExportPracticeEvent]:
        """Export all practice events ordered by ID."""
        practice_events = (
            self.db.query(PracticeEvent)
            .order_by(PracticeEvent.id)
            .all()
        )

        return [ExportPracticeEvent.model_validate(e) for e in practice_events]
