"""Tests for passive mode downgrade behavior.

These tests ensure that:
1. Passive mode always increments times_seen
2. Passive success after recall failure does NOT increase confidence
3. Active recall results are authoritative
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.models.learning_unit import LearningProgress, RecallResult
from app.services.session_service import SessionService


class TestPassiveModeDowngrade:
    """Tests for passive mode not overriding recall failures."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 0
        return db
    
    @pytest.fixture
    def service(self, mock_db):
        """Create a SessionService with mock db."""
        return SessionService(mock_db)
    
    def test_passive_success_blocked_after_recall_failure(self, service, mock_db):
        """
        CRITICAL: Passive success MUST NOT increase confidence if last_recall_result == failed.
        """
        # Setup: progress with a previous recall failure
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=RecallResult.FAILED,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Store original values
        original_confidence = progress.confidence_score
        original_times_correct = progress.times_correct
        
        # Act: passive mode success
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,  # Passive mode
            is_recall_mode=False,
        )
        
        # Assert: confidence should NOT increase
        assert progress.confidence_score == original_confidence, \
            "Passive success should NOT increase confidence after recall failure"
        
        # times_seen should still increment
        assert progress.times_seen == 6
        
        # times_correct should NOT increment when blocked
        assert progress.times_correct == original_times_correct
    
    def test_passive_failure_still_increments_failed(self, service, mock_db):
        """Passive failure should still increment times_failed."""
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=RecallResult.FAILED,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: passive mode failure
        service._update_progress(
            unit_id=1,
            is_correct=False,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Assert: times_failed should increment
        assert progress.times_failed == 4
        assert progress.times_seen == 6
    
    def test_passive_success_works_normally_without_recall_failure(self, service, mock_db):
        """Passive success should work normally if no prior recall failure.
        
        Passive correct never reduces confidence (max clamp), so for
        old_confidence=0.4 the smoothed value (0.37) is clamped to 0.4.
        """
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=None,  # Never tested in recall mode
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: passive mode success
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Assert: smoothed = 0.4 * 0.7 + 0.3 * 0.3 = 0.37, clamped to max(0.4, 0.37) = 0.4
        assert progress.times_correct == 3
        assert progress.times_seen == 6
        assert progress.confidence_score == pytest.approx(0.4)
    
    def test_passive_success_works_after_recall_correct(self, service, mock_db):
        """Passive success after recall correct must not reduce confidence.
        
        Smoothed value (0.65) is below old (0.8), so max clamp keeps 0.8.
        """
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=4,
            times_failed=1,
            confidence_score=0.8,
            stability_score=0.0,
            last_recall_result=RecallResult.CORRECT,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: passive mode success
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Assert: smoothed = 0.8*0.7 + 0.3*0.3 = 0.65, clamped to max(0.8, 0.65) = 0.8
        assert progress.times_correct == 5
        assert progress.times_seen == 6
        assert progress.confidence_score == pytest.approx(0.8)
    
    def test_passive_success_works_after_recall_partial(self, service, mock_db):
        """Passive success after recall partial must not reduce confidence."""
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=4,
            times_failed=1,
            confidence_score=0.8,
            stability_score=0.0,
            last_recall_result=RecallResult.PARTIAL,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: passive mode success
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Assert: confidence clamped at previous value (0.8)
        assert progress.times_correct == 5
        assert progress.times_seen == 6
        assert progress.confidence_score == pytest.approx(0.8)


    def test_passive_correct_never_reduces_high_confidence(self, service, mock_db):
        """Passive correct on a mature word (0.9) must not reduce confidence.
        
        This is the key regression test: previously, passive correct with
        raw_score=0.3 would pull a 0.9-confidence word down to ~0.72,
        eventually destroying learned status.
        """
        progress = LearningProgress(
            unit_id=1,
            times_seen=20,
            times_correct=18,
            times_failed=2,
            confidence_score=0.9,
            stability_score=0.1,
            last_recall_result=RecallResult.CORRECT,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: passive mode success
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Assert: confidence must not decrease from 0.9
        assert progress.confidence_score >= 0.9, (
            f"Passive correct must never reduce confidence; "
            f"got {progress.confidence_score}, expected >= 0.9"
        )
        assert progress.times_correct == 19
        assert progress.times_seen == 21


class TestRecallOverridesPassive:
    """Tests that recall mode clears the failure block."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 0
        return db
    
    @pytest.fixture
    def service(self, mock_db):
        """Create a SessionService with mock db."""
        return SessionService(mock_db)
    
    def test_recall_correct_clears_failure_block(self, service, mock_db):
        """Recall correct should clear the failure block for future passive."""
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=RecallResult.FAILED,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: recall mode correct
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=RecallResult.CORRECT,
            is_recall_mode=True,
        )
        
        # Assert: last_recall_result should be CORRECT now
        assert progress.last_recall_result == RecallResult.CORRECT
        assert progress.times_correct == 3
        
        # Now passive should work
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Should increment normally
        assert progress.times_correct == 4
    
    def test_recall_partial_clears_failure_block(self, service, mock_db):
        """Recall partial should clear the failure block for future passive."""
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=RecallResult.FAILED,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: recall mode partial
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=RecallResult.PARTIAL,
            is_recall_mode=True,
        )
        
        # Assert: last_recall_result should be PARTIAL now
        assert progress.last_recall_result == RecallResult.PARTIAL


class TestTimesSeenAlwaysIncrements:
    """Tests that times_seen always increments regardless of mode."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 0
        return db
    
    @pytest.fixture
    def service(self, mock_db):
        """Create a SessionService with mock db."""
        return SessionService(mock_db)
    
    def test_passive_always_increments_times_seen(self, service, mock_db):
        """times_seen should increment even when confidence is blocked."""
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=RecallResult.FAILED,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: blocked passive success
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=None,
            is_recall_mode=False,
        )
        
        # Assert: times_seen should still increment
        assert progress.times_seen == 6
    
    def test_recall_always_increments_times_seen(self, service, mock_db):
        """times_seen should increment for recall mode."""
        progress = LearningProgress(
            unit_id=1,
            times_seen=5,
            times_correct=2,
            times_failed=3,
            confidence_score=0.4,
            stability_score=0.0,
            last_recall_result=None,
        )
        mock_db.query.return_value.filter.return_value.first.return_value = progress
        
        # Act: recall mode
        service._update_progress(
            unit_id=1,
            is_correct=True,
            recall_result=RecallResult.CORRECT,
            is_recall_mode=True,
        )
        
        # Assert
        assert progress.times_seen == 6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
