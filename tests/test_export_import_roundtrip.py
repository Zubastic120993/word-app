from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.audio import AudioAsset
from app.models.learning_unit import LearningUnit, Settings, UnitType
from app.models.practice_event import PracticeEvent
from app.models.session import LearningSession, SessionUnit
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.services.export_service import ExportService
from app.services.import_service import import_all_data


def test_export_import_roundtrip_restores_extended_entities():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestSessionLocal()

    try:
        db.add(
            Settings(
                id=1,
                offline_mode=True,
                ai_provider="ollama",
                ollama_model="llama3.2",
                strict_mode=True,
                source_language="Polish",
                target_language="English",
            )
        )

        group = VocabularyGroup(
            id=1,
            user_key="default",
            name="Core",
            description="Roundtrip test group",
            display_order=1,
            created_at=datetime(2025, 1, 1, 9, 0, 0),
        )
        db.add(group)

        vocabulary = Vocabulary(
            id=1,
            user_key="default",
            name="Starter Set",
            group_id=1,
            created_at=datetime(2025, 1, 1, 9, 5, 0),
        )
        db.add(vocabulary)

        unit = LearningUnit(
            id=1,
            text="dom",
            type=UnitType.WORD,
            translation="house",
            source_pdf="roundtrip.pdf",
            vocabulary_id=1,
            created_at=datetime(2025, 1, 1, 10, 0, 0),
        )
        db.add(unit)

        practice_event = PracticeEvent(
            id=1,
            created_at=datetime(2025, 1, 10, 11, 15, 0),
            event_type="quiz_answer",
            theme="basics",
            payload={"unit_id": 1, "correct": True},
        )
        db.add(practice_event)

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

        session = LearningSession(
            id=1,
            created_at=datetime(2025, 1, 10, 11, 0, 0),
            locked=True,
            completed=False,
        )
        db.add(session)

        session_unit = SessionUnit(
            id=1,
            session_id=1,
            unit_id=1,
            position=1,
            answered=False,
        )
        db.add(session_unit)

        db.commit()

        export_data = ExportService(db).export_all_data()
        payload = export_data.model_dump(mode="json")

        db.query(SessionUnit).delete()
        db.query(LearningSession).delete()
        db.query(AudioAsset).delete()
        db.query(PracticeEvent).delete()
        db.query(LearningUnit).delete()
        db.query(Vocabulary).delete()
        db.query(VocabularyGroup).delete()
        db.query(Settings).delete()
        db.commit()

        result = import_all_data(db, payload)

        assert result.success is True
        assert db.query(VocabularyGroup).count() == 1
        assert db.query(Vocabulary).count() == 1
        assert db.query(PracticeEvent).count() == 1
        assert db.query(LearningSession).count() == 1
        assert db.query(AudioAsset).count() == 1

        restored_vocabulary = db.query(Vocabulary).one()
        restored_audio_asset = db.query(AudioAsset).one()
        restored_event = db.query(PracticeEvent).one()
        restored_session = db.query(LearningSession).one()

        assert restored_vocabulary.group_id == 1
        assert restored_audio_asset.unit_id == 1
        assert restored_event.payload == {"unit_id": 1, "correct": True}
        assert restored_session.id == 1
    finally:
        db.close()
