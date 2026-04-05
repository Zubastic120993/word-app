"""Tests for mastery progress computation."""

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningUnit, LearningProgress, UnitType, RecallResult
from app.services.progress_service import is_word_mastered, compute_mastery_stats
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


class TestIsWordMastered:
    """Test is_word_mastered function with strict mastery definition."""
    
    def test_no_progress_not_mastered(self, db_session):
        """Word with no progress is not mastered."""
        assert is_word_mastered(None) is False
    
    def test_not_introduced_not_mastered(self, db_session):
        """Word not introduced (introduced_at is NULL) is not mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=utc_now() + timedelta(days=1),
            introduced_at=None,  # Not introduced
        )
        db_session.add(progress)
        db_session.commit()
        
        assert is_word_mastered(progress) is False
    
    def test_recall_not_correct_not_mastered(self, db_session):
        """Word with last_recall_result != correct is not mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.FAILED,  # Not correct
            next_review_at=utc_now() + timedelta(days=1),
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        assert is_word_mastered(progress) is False
    
    def test_confidence_too_low_not_mastered(self, db_session):
        """Word with confidence < 0.85 is not mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.84,  # Below 0.85
            last_recall_result=RecallResult.CORRECT,
            next_review_at=utc_now() + timedelta(days=1),
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        assert is_word_mastered(progress) is False
    
    def test_due_for_review_not_mastered(self, db_session):
        """Word with next_review_at <= now is not mastered (due for review)."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        now = utc_now()
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now - timedelta(days=1),  # Past due
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        assert is_word_mastered(progress, now=now) is False
    
    def test_all_conditions_met_is_mastered(self, db_session):
        """Word meeting all conditions is mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        now = utc_now()
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.85,  # Exactly 0.85
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now + timedelta(days=1),  # Not due
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        assert is_word_mastered(progress, now=now) is True
    
    def test_high_confidence_mastered(self, db_session):
        """Word with high confidence and all conditions is mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        now = utc_now()
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.95,  # High confidence
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now + timedelta(days=7),  # Not due
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        assert is_word_mastered(progress, now=now) is True


class TestComputeMasteryStats:
    """Test compute_mastery_stats function."""
    
    def test_empty_list_returns_zeros(self):
        """Empty list returns all zeros."""
        stats = compute_mastery_stats([])
        
        assert stats["passive_pct"] == 0.0
        assert stats["recall_pct"] == 0.0
        assert stats["mastered_pct"] == 0.0
        assert stats["mastered_count"] == 0
        assert stats["total_count"] == 0
    
    def test_passive_not_mastered(self, db_session):
        """Passive (introduced) but not mastered counts as passive only."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.5,  # Low confidence
            last_recall_result=None,  # No recall yet
            next_review_at=None,
            introduced_at=utc_now(),  # Introduced
        )
        db_session.add(progress)
        db_session.commit()
        
        unit.progress = progress
        stats = compute_mastery_stats([unit])
        
        assert stats["passive_pct"] == 100.0
        assert stats["recall_pct"] == 0.0
        assert stats["mastered_pct"] == 0.0
        assert stats["mastered_count"] == 0
        assert stats["total_count"] == 1
    
    def test_recall_but_due_not_mastered(self, db_session):
        """Recall correct but due for review is not mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        now = utc_now()
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now - timedelta(days=1),  # Due
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        unit.progress = progress
        stats = compute_mastery_stats([unit], now=now)
        
        assert stats["passive_pct"] == 100.0
        assert stats["recall_pct"] == 100.0
        assert stats["mastered_pct"] == 0.0  # Not mastered (due)
        assert stats["mastered_count"] == 0
        assert stats["total_count"] == 1
    
    def test_confidence_too_low_not_mastered(self, db_session):
        """Recall correct but confidence < 0.85 is not mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        now = utc_now()
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.8,  # Below 0.85
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now + timedelta(days=1),
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        unit.progress = progress
        stats = compute_mastery_stats([unit], now=now)
        
        assert stats["passive_pct"] == 100.0
        assert stats["recall_pct"] == 100.0
        assert stats["mastered_pct"] == 0.0  # Not mastered (low confidence)
        assert stats["mastered_count"] == 0
        assert stats["total_count"] == 1
    
    def test_all_conditions_met_is_mastered(self, db_session):
        """All conditions met → mastered."""
        unit = LearningUnit(
            text="test",
            translation="test",
            type=UnitType.WORD,
            source_pdf="test.pdf",
        )
        db_session.add(unit)
        db_session.flush()
        
        now = utc_now()
        progress = LearningProgress(
            unit_id=unit.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now + timedelta(days=1),
            introduced_at=utc_now(),
        )
        db_session.add(progress)
        db_session.commit()
        
        unit.progress = progress
        stats = compute_mastery_stats([unit], now=now)
        
        assert stats["passive_pct"] == 100.0
        assert stats["recall_pct"] == 100.0
        assert stats["mastered_pct"] == 100.0
        assert stats["mastered_count"] == 1
        assert stats["total_count"] == 1
    
    def test_source_reaches_100_percent(self, db_session):
        """Source with all words mastered reaches exactly 100%."""
        source_pdf = "test.pdf"
        units = []
        
        # Create 5 units, all mastered
        for i in range(5):
            unit = LearningUnit(
                text=f"test{i}",
                translation=f"test{i}",
                type=UnitType.WORD,
                source_pdf=source_pdf,
            )
            db_session.add(unit)
            db_session.flush()
            
            now = utc_now()
            progress = LearningProgress(
                unit_id=unit.id,
                confidence_score=0.9,
                last_recall_result=RecallResult.CORRECT,
                next_review_at=now + timedelta(days=1),
                introduced_at=utc_now(),
            )
            db_session.add(progress)
            db_session.commit()
            
            unit.progress = progress
            units.append(unit)
        
        stats = compute_mastery_stats(units)
        
        assert stats["mastered_pct"] == 100.0
        assert stats["mastered_count"] == 5
        assert stats["total_count"] == 5
    
    def test_mixed_progress_stats(self, db_session):
        """Mixed progress correctly computes percentages."""
        source_pdf = "test.pdf"
        units = []
        now = utc_now()
        
        # Unit 1: Not introduced
        unit1 = LearningUnit(
            text="test1",
            translation="test1",
            type=UnitType.WORD,
            source_pdf=source_pdf,
        )
        db_session.add(unit1)
        db_session.flush()
        unit1.progress = None
        units.append(unit1)
        
        # Unit 2: Introduced but no recall
        unit2 = LearningUnit(
            text="test2",
            translation="test2",
            type=UnitType.WORD,
            source_pdf=source_pdf,
        )
        db_session.add(unit2)
        db_session.flush()
        progress2 = LearningProgress(
            unit_id=unit2.id,
            confidence_score=0.5,
            last_recall_result=None,
            next_review_at=None,
            introduced_at=utc_now(),
        )
        db_session.add(progress2)
        db_session.commit()
        unit2.progress = progress2
        units.append(unit2)
        
        # Unit 3: Recall correct but due
        unit3 = LearningUnit(
            text="test3",
            translation="test3",
            type=UnitType.WORD,
            source_pdf=source_pdf,
        )
        db_session.add(unit3)
        db_session.flush()
        progress3 = LearningProgress(
            unit_id=unit3.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now - timedelta(days=1),  # Due
            introduced_at=utc_now(),
        )
        db_session.add(progress3)
        db_session.commit()
        unit3.progress = progress3
        units.append(unit3)
        
        # Unit 4: Mastered
        unit4 = LearningUnit(
            text="test4",
            translation="test4",
            type=UnitType.WORD,
            source_pdf=source_pdf,
        )
        db_session.add(unit4)
        db_session.flush()
        progress4 = LearningProgress(
            unit_id=unit4.id,
            confidence_score=0.9,
            last_recall_result=RecallResult.CORRECT,
            next_review_at=now + timedelta(days=1),
            introduced_at=utc_now(),
        )
        db_session.add(progress4)
        db_session.commit()
        unit4.progress = progress4
        units.append(unit4)
        
        stats = compute_mastery_stats(units, now=now)
        
        assert stats["total_count"] == 4
        assert stats["passive_pct"] == 75.0  # 3 out of 4 introduced
        assert stats["recall_pct"] == 50.0  # 2 out of 4 have correct recall
        assert stats["mastered_pct"] == 25.0  # 1 out of 4 mastered
        assert stats["mastered_count"] == 1
