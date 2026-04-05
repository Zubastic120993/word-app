from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult, UnitType
from app.models.practice_event import PracticeEvent
from app.models.vocabulary import Vocabulary
from app.services.progress_service import get_progress_page_data
from app.utils.time import utc_now


def test_source_stats_characterization():
    engine = create_engine("sqlite:///:memory:")
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()

    try:
        source_name = "source_a.pdf"
        now = utc_now()

        db.add(Vocabulary(id=1, user_key="test", name=source_name))

        units = [
            LearningUnit(
                id=1,
                text="dom",
                translation="house",
                type=UnitType.WORD,
                source_pdf=source_name,
                vocabulary_id=1,
            ),
            LearningUnit(
                id=2,
                text="kot",
                translation="cat",
                type=UnitType.WORD,
                source_pdf=source_name,
                vocabulary_id=1,
            ),
            LearningUnit(
                id=3,
                text="pies",
                translation="dog",
                type=UnitType.WORD,
                source_pdf=source_name,
                vocabulary_id=1,
            ),
            LearningUnit(
                id=4,
                text="ptak",
                translation="bird",
                type=UnitType.WORD,
                source_pdf=source_name,
                vocabulary_id=1,
            ),
        ]
        db.add_all(units)

        db.add_all(
            [
                LearningProgress(
                    unit_id=1,
                    introduced_at=now - timedelta(days=5),
                    last_seen=now - timedelta(days=1),
                    next_review_at=now + timedelta(days=2),
                    times_seen=3,
                    times_correct=2,
                    times_failed=0,
                    confidence_score=0.9,
                    last_recall_result=RecallResult.CORRECT,
                ),
                LearningProgress(
                    unit_id=2,
                    introduced_at=now - timedelta(days=3),
                    last_seen=now - timedelta(days=1),
                    next_review_at=now - timedelta(hours=1),
                    times_seen=2,
                    times_correct=1,
                    times_failed=1,
                    confidence_score=0.6,
                    last_recall_result=RecallResult.FAILED,
                ),
                LearningProgress(
                    unit_id=3,
                    introduced_at=now - timedelta(days=2),
                    last_seen=now - timedelta(hours=6),
                    next_review_at=now + timedelta(days=1),
                    times_seen=1,
                    times_correct=0,
                    times_failed=1,
                    confidence_score=0.2,
                    last_recall_result=RecallResult.FAILED,
                ),
            ]
        )

        db.add_all(
            [
                PracticeEvent(
                    event_type="study_answer",
                    theme="basics",
                    payload={"unit_id": 1, "source_pdf": source_name},
                ),
                PracticeEvent(
                    event_type="study_answer",
                    theme="basics",
                    payload={"unit_id": 2, "source_pdf": source_name},
                ),
            ]
        )
        db.commit()

        data = get_progress_page_data(db)

        assert data["stats"]["total_units"] == 4
        assert data["stats"]["mastered_count"] == 1
        assert data["stats"]["learning"] == 2
        assert data["stats"]["due_words_count"] == 1

        assert data["source_stats"][source_name]["total_count"] == 4
        assert data["source_stats"][source_name]["mastered_count"] == 1

        assert data["source_stats"][source_name]["due_count"] == 1
        assert data["source_stats"][source_name]["weak_count"] == 1
        assert data["source_stats"]["__all__"]["due_count"] == data["stats"]["due_words_count"]
        assert data["source_stats"]["__all__"]["weak_count"] == data["stats"]["weak_words_count"]
    finally:
        db.close()


def test_per_source_due_weak_counts_two_sources():
    engine = create_engine("sqlite:///:memory:")
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()

    try:
        now = utc_now()
        db.add(Vocabulary(id=1, user_key="test", name="file1.pdf"))
        db.add(Vocabulary(id=2, user_key="test", name="file2.pdf"))

        db.add_all(
            [
                LearningUnit(
                    id=1,
                    text="a",
                    translation="A",
                    type=UnitType.WORD,
                    source_pdf="file1.pdf",
                    vocabulary_id=1,
                ),
                LearningUnit(
                    id=2,
                    text="b",
                    translation="B",
                    type=UnitType.WORD,
                    source_pdf="file2.pdf",
                    vocabulary_id=2,
                ),
            ]
        )
        db.add_all(
            [
                LearningProgress(
                    unit_id=1,
                    introduced_at=now - timedelta(days=1),
                    last_seen=now - timedelta(hours=1),
                    next_review_at=now - timedelta(hours=1),
                    times_seen=2,
                    times_correct=0,
                    times_failed=2,
                    confidence_score=0.3,
                    last_recall_result=RecallResult.FAILED,
                ),
                LearningProgress(
                    unit_id=2,
                    introduced_at=now - timedelta(days=1),
                    last_seen=now - timedelta(hours=1),
                    next_review_at=now + timedelta(days=3),
                    times_seen=5,
                    times_correct=5,
                    times_failed=0,
                    confidence_score=0.95,
                    last_recall_result=RecallResult.CORRECT,
                ),
            ]
        )
        db.commit()

        data = get_progress_page_data(db)

        assert data["source_stats"]["file1.pdf"]["due_count"] == 1
        assert data["source_stats"]["file1.pdf"]["weak_count"] == 1
        assert data["source_stats"]["file2.pdf"]["due_count"] == 0
        assert data["source_stats"]["file2.pdf"]["weak_count"] == 0
        assert "due_count" in data["source_stats"]["file1.pdf"]
        assert "weak_count" in data["source_stats"]["file1.pdf"]
    finally:
        db.close()
