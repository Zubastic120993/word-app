"""Tests for selection progress statistics computation."""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, LearningProgress, UnitType, RecallResult
from app.models.session import LearningSession, SessionUnit, StudyModeType
from app.services.progress_service import compute_selection_progress_stats
from app.utils.time import utc_now


@pytest.fixture
def db_session():
    """Create a test database session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def sample_units(db_session):
    """Create sample learning units for testing."""
    units = []
    for i in range(5):
        unit = LearningUnit(
            text=f"test{i}",
            type=UnitType.WORD,
            translation=f"test{i}",
            source_pdf="test1.pdf",
        )
        db_session.add(unit)
        units.append(unit)
    
    for i in range(5, 10):
        unit = LearningUnit(
            text=f"test{i}",
            type=UnitType.WORD,
            translation=f"test{i}",
            source_pdf="test2.pdf",
        )
        db_session.add(unit)
        units.append(unit)
    
    db_session.commit()
    return units


class TestComputeSelectionProgressStats:
    """Tests for compute_selection_progress_stats function."""
    
    def test_empty_selection_returns_zeros(self, db_session):
        """Empty selection returns zero statistics."""
        stats = compute_selection_progress_stats(db_session, source_pdfs=[])
        assert stats["total_units"] == 0
        assert stats["passive_pct"] == 0.0
        assert stats["recall_visual_pct"] == 0.0
        assert stats["recall_audio_pct"] == 0.0
        assert stats["mastered_pct"] == 0.0
    
    def test_single_source_stats(self, db_session, sample_units):
        """Single source stats are computed correctly."""
        now = utc_now()
        
        # Add progress to first 3 units from test1.pdf
        for i in range(3):
            progress = LearningProgress(
                unit_id=sample_units[i].id,
                introduced_at=now - timedelta(days=1),
                times_seen=1,
                times_correct=1,
                confidence_score=0.9,
            )
            db_session.add(progress)
        
        db_session.commit()
        
        stats = compute_selection_progress_stats(db_session, source_pdfs=["test1.pdf"])
        assert stats["total_units"] == 5
        assert stats["passive_pct"] == 60.0  # 3 out of 5
        assert stats["recall_visual_pct"] == 0.0
        assert stats["recall_audio_pct"] == 0.0
    
    def test_multiple_source_aggregation(self, db_session, sample_units):
        """Multiple sources are aggregated correctly."""
        now = utc_now()
        
        # Add progress to units from both sources
        for i in range(3):
            progress = LearningProgress(
                unit_id=sample_units[i].id,  # test1.pdf
                introduced_at=now - timedelta(days=1),
                times_seen=1,
                times_correct=1,
                confidence_score=0.9,
            )
            db_session.add(progress)
        
        for i in range(5, 7):
            progress = LearningProgress(
                unit_id=sample_units[i].id,  # test2.pdf
                introduced_at=now - timedelta(days=1),
                times_seen=1,
                times_correct=1,
                confidence_score=0.9,
            )
            db_session.add(progress)
        
        db_session.commit()
        
        stats = compute_selection_progress_stats(
            db_session, 
            source_pdfs=["test1.pdf", "test2.pdf"]
        )
        assert stats["total_units"] == 10
        assert stats["passive_pct"] == 50.0  # 5 out of 10
    
    def test_passive_vs_recall_separation(self, db_session, sample_units):
        """Passive and recall stats are tracked separately."""
        now = utc_now()
        
        # Unit 0: introduced but no recall
        progress0 = LearningProgress(
            unit_id=sample_units[0].id,
            introduced_at=now - timedelta(days=1),
            times_seen=1,
            times_correct=0,
            confidence_score=0.0,
        )
        db_session.add(progress0)
        
        # Unit 1: introduced and has visual recall
        progress1 = LearningProgress(
            unit_id=sample_units[1].id,
            introduced_at=now - timedelta(days=1),
            times_seen=2,
            times_correct=1,
            confidence_score=0.5,
            last_recall_result=RecallResult.CORRECT,
        )
        db_session.add(progress1)
        
        # Create a session with visual recall mode
        session = LearningSession(mode=StudyModeType.RECALL)
        db_session.add(session)
        db_session.flush()
        
        session_unit = SessionUnit(
            session_id=session.id,
            unit_id=sample_units[1].id,
            position=1,
            answered=True,
            is_correct=True,
            recall_result=RecallResult.CORRECT,
        )
        db_session.add(session_unit)
        
        db_session.commit()
        
        stats = compute_selection_progress_stats(db_session, source_pdfs=["test1.pdf"])
        assert stats["passive_pct"] == 40.0  # 2 out of 5 (units 0 and 1)
        assert stats["recall_visual_pct"] == 20.0  # 1 out of 5 (unit 1)
        assert stats["recall_audio_pct"] == 0.0
    
    def test_visual_vs_audio_recall_separation(self, db_session, sample_units):
        """Visual and audio recall are tracked separately."""
        now = utc_now()
        
        # Unit 0: has visual recall
        progress0 = LearningProgress(
            unit_id=sample_units[0].id,
            introduced_at=now - timedelta(days=1),
            times_seen=2,
            times_correct=1,
            confidence_score=0.5,
            last_recall_result=RecallResult.CORRECT,
        )
        db_session.add(progress0)
        
        # Unit 1: has audio recall
        progress1 = LearningProgress(
            unit_id=sample_units[1].id,
            introduced_at=now - timedelta(days=1),
            times_seen=2,
            times_correct=1,
            confidence_score=0.5,
            last_recall_result=RecallResult.CORRECT,
        )
        db_session.add(progress1)
        
        # Create visual recall session
        session_visual = LearningSession(mode=StudyModeType.RECALL)
        db_session.add(session_visual)
        db_session.flush()
        
        session_unit_visual = SessionUnit(
            session_id=session_visual.id,
            unit_id=sample_units[0].id,
            position=1,
            answered=True,
            is_correct=True,
            recall_result=RecallResult.CORRECT,
        )
        db_session.add(session_unit_visual)
        
        # Create audio recall session
        session_audio = LearningSession(mode=StudyModeType.RECALL_AUDIO)
        db_session.add(session_audio)
        db_session.flush()
        
        session_unit_audio = SessionUnit(
            session_id=session_audio.id,
            unit_id=sample_units[1].id,
            position=1,
            answered=True,
            is_correct=True,
            recall_result=RecallResult.CORRECT,
        )
        db_session.add(session_unit_audio)
        
        db_session.commit()
        
        stats = compute_selection_progress_stats(db_session, source_pdfs=["test1.pdf"])
        assert stats["recall_visual_pct"] == 20.0  # 1 out of 5 (unit 0)
        assert stats["recall_audio_pct"] == 20.0  # 1 out of 5 (unit 1)
    
    def test_all_sources_selected(self, db_session, sample_units):
        """When source_pdfs is None, all sources are included."""
        now = utc_now()
        
        # Add progress to some units
        for i in range(3):
            progress = LearningProgress(
                unit_id=sample_units[i].id,
                introduced_at=now - timedelta(days=1),
                times_seen=1,
                times_correct=1,
                confidence_score=0.9,
            )
            db_session.add(progress)
        
        db_session.commit()
        
        # When source_pdfs is None, should include all units
        stats = compute_selection_progress_stats(db_session, source_pdfs=None)
        assert stats["total_units"] == 10
        assert stats["passive_pct"] == 30.0  # 3 out of 10
    
    def test_mastered_pct_computation(self, db_session, sample_units):
        """Mastered percentage is computed using strict definition."""
        now = utc_now()
        future_date = now + timedelta(days=10)
        
        # Create a mastered unit
        progress = LearningProgress(
            unit_id=sample_units[0].id,
            introduced_at=now - timedelta(days=5),
            times_seen=5,
            times_correct=5,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=future_date,
        )
        db_session.add(progress)
        
        db_session.commit()
        
        stats = compute_selection_progress_stats(db_session, source_pdfs=["test1.pdf"], now=now)
        assert stats["mastered_pct"] == 20.0  # 1 out of 5
