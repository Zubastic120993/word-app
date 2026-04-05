"""Tests for weighted random session generation.

These tests ensure that:
1. Session generation uses weighted random sampling
2. Failed words are biased (more likely to appear)
3. Bucket composition is approximately correct
4. No duplicates in session
5. Deterministic seed produces same results
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from app.models.learning_unit import LearningUnit, LearningProgress, UnitType, RecallResult
from app.utils.time import utc_now
from app.services.session_service import (
    LAST_SESSION_DIVERSITY_FACTOR,
    SessionService,
    BUCKET_NEW_PERCENT,
    BUCKET_WEAK_PERCENT,
    BUCKET_REVIEW_PERCENT,
    SESSION_SIZE,
    WEAK_THRESHOLD,
)


class TestWeightedRandomSampling:
    """Tests for weighted random sampling mechanics."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return MagicMock()
    
    def test_deterministic_with_seed(self, mock_db):
        """Same seed should produce same results."""
        service1 = SessionService(mock_db, random_seed=42)
        service2 = SessionService(mock_db, random_seed=42)
        
        # Create units with weights
        units = [
            (MagicMock(id=i), 1.0) for i in range(10)
        ]
        
        selected_ids1 = set()
        selected_ids2 = set()
        
        result1 = service1._weighted_random_sample(units.copy(), 5, selected_ids1)
        result2 = service2._weighted_random_sample(units.copy(), 5, selected_ids2)
        
        # Same seed = same selection order
        assert [u.id for u in result1] == [u.id for u in result2]
    
    def test_different_seeds_produce_different_results(self, mock_db):
        """Different seeds should (likely) produce different results."""
        service1 = SessionService(mock_db, random_seed=42)
        service2 = SessionService(mock_db, random_seed=123)
        
        units = [
            (MagicMock(id=i), 1.0) for i in range(20)
        ]
        
        selected_ids1 = set()
        selected_ids2 = set()
        
        result1 = service1._weighted_random_sample(units.copy(), 10, selected_ids1)
        result2 = service2._weighted_random_sample(units.copy(), 10, selected_ids2)
        
        # Different seeds should (likely) produce different order
        # Note: There's a tiny chance they could be the same
        ids1 = [u.id for u in result1]
        ids2 = [u.id for u in result2]
        assert ids1 != ids2 or len(ids1) == 0
    
    def test_no_seed_produces_random_results(self, mock_db):
        """No seed should produce (likely) different results each time."""
        # This test is inherently probabilistic
        results = []
        
        for _ in range(5):
            service = SessionService(mock_db, random_seed=None)
            units = [(MagicMock(id=i), 1.0) for i in range(20)]
            selected_ids = set()
            result = service._weighted_random_sample(units, 10, selected_ids)
            results.append([u.id for u in result])
        
        # At least some should be different (very high probability)
        unique_results = set(tuple(r) for r in results)
        assert len(unique_results) >= 2 or len(results) == 0


class TestWeightCalculation:
    """Tests for unit weight calculation."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return MagicMock()
    
    @pytest.fixture
    def service(self, mock_db):
        """Create a SessionService with mock db."""
        return SessionService(mock_db, random_seed=42)
    
    def test_new_unit_has_base_weight(self, service):
        """New units should have base weight for their bucket."""
        unit = MagicMock()
        unit.progress = None  # No progress = new unit
        
        weight = service._compute_unit_weight(unit, "new")
        
        # Should be base weight (3.0 for new)
        assert weight == 3.0
    
    def test_failed_unit_gets_failure_multiplier(self, service):
        """Units with more failures than successes get boosted."""
        unit = MagicMock()
        unit.progress = MagicMock()
        unit.progress.times_failed = 5
        unit.progress.times_correct = 2
        unit.progress.last_seen = utc_now()
        unit.progress.last_recall_result = None
        
        weight = service._compute_unit_weight(unit, "weak")
        
        # Should have failure multiplier applied
        assert weight > 2.0  # Base weak weight is 2.0
    
    def test_recall_failed_gets_penalty_boost(self, service):
        """Units with last_recall_result=FAILED get boosted."""
        unit = MagicMock()
        unit.progress = MagicMock()
        unit.progress.times_failed = 1
        unit.progress.times_correct = 5
        unit.progress.last_seen = utc_now()
        unit.progress.last_recall_result = RecallResult.FAILED
        
        weight = service._compute_unit_weight(unit, "weak")
        
        # Should have recall penalty applied
        assert weight > 2.0
    
    def test_old_units_get_time_boost(self, service):
        """Units not seen recently get boosted."""
        now = utc_now()
        unit = MagicMock()
        unit.progress = MagicMock()
        unit.progress.times_failed = 0
        unit.progress.times_correct = 1
        unit.progress.last_seen = now - timedelta(days=7)  # 7 days ago
        unit.progress.last_recall_result = None
        
        weight = service._compute_unit_weight(unit, "review", now=now)
        
        # Should have time boost applied
        assert weight > 1.0


class TestNoDuplicatesInSession:
    """Tests ensuring no duplicates in session selection."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return MagicMock()
    
    def test_weighted_sample_excludes_selected_ids(self, mock_db):
        """Weighted sample should exclude already-selected IDs."""
        service = SessionService(mock_db, random_seed=42)
        
        units = [(MagicMock(id=i), 1.0) for i in range(10)]
        
        # Pre-select some IDs
        selected_ids = {0, 1, 2}
        
        result = service._weighted_random_sample(units, 5, selected_ids)
        
        # Result should not contain pre-selected IDs
        result_ids = {u.id for u in result}
        assert result_ids.isdisjoint({0, 1, 2})
    
    def test_no_duplicates_in_selected(self, mock_db):
        """Selected units should have no duplicates."""
        service = SessionService(mock_db, random_seed=42)
        
        units = [(MagicMock(id=i), 1.0) for i in range(20)]
        selected_ids = set()
        
        result = service._weighted_random_sample(units, 10, selected_ids)
        
        # All IDs should be unique
        result_ids = [u.id for u in result]
        assert len(result_ids) == len(set(result_ids))


class TestBiasTowardFailedWords:
    """Tests verifying bias toward failed words."""
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return MagicMock()
    
    def test_failed_words_selected_more_often(self, mock_db):
        """
        Failed words should be selected more often than passing words.
        
        This is a statistical test - run multiple samples and check distribution.
        """
        service = SessionService(mock_db, random_seed=42)
        
        # Create units: 5 failed, 5 passing
        failed_counts = {i: 0 for i in range(5)}
        passing_counts = {i: 0 for i in range(5, 10)}
        
        now = utc_now()
        
        def create_unit(id, is_failed):
            unit = MagicMock()
            unit.id = id
            unit.progress = MagicMock()
            if is_failed:
                unit.progress.times_failed = 5
                unit.progress.times_correct = 1
                unit.progress.last_recall_result = RecallResult.FAILED
            else:
                unit.progress.times_failed = 1
                unit.progress.times_correct = 5
                unit.progress.last_recall_result = RecallResult.CORRECT
            unit.progress.last_seen = now
            return unit
        
        failed_units = [create_unit(i, True) for i in range(5)]
        passing_units = [create_unit(i, False) for i in range(5, 10)]
        
        # Compute weights
        failed_with_weights = [(u, service._compute_unit_weight(u, "weak", now)) for u in failed_units]
        passing_with_weights = [(u, service._compute_unit_weight(u, "weak", now)) for u in passing_units]
        
        # Failed weights should be higher
        avg_failed_weight = sum(w for _, w in failed_with_weights) / len(failed_with_weights)
        avg_passing_weight = sum(w for _, w in passing_with_weights) / len(passing_with_weights)
        
        assert avg_failed_weight > avg_passing_weight


class TestBucketComposition:
    """Tests for bucket composition targets."""
    
    def test_bucket_percentages_sum_to_100(self):
        """Bucket percentages should sum to 100%."""
        total = BUCKET_NEW_PERCENT + BUCKET_WEAK_PERCENT + BUCKET_REVIEW_PERCENT
        assert total == pytest.approx(1.0, abs=0.01)
    
    def test_session_size_is_50(self):
        """Session size matches settings default."""
        assert SESSION_SIZE == 50
    
    def test_bucket_targets(self):
        """Verify bucket target counts."""
        target_new = int(SESSION_SIZE * BUCKET_NEW_PERCENT)
        target_weak = int(SESSION_SIZE * BUCKET_WEAK_PERCENT)
        target_review = SESSION_SIZE - target_new - target_weak
        
        assert target_new == 15
        assert target_weak == 20
        assert target_review == 15


class TestLastSessionDiversity:
    """Last completed session down-weights non-due units for cross-session variety."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    def test_last_session_factor_non_due(self, mock_db):
        service = SessionService(mock_db, random_seed=42)
        now = utc_now()
        unit = MagicMock()
        unit.id = 99
        unit.progress = MagicMock()
        unit.progress.times_failed = 0
        unit.progress.times_correct = 1
        unit.progress.last_seen = now - timedelta(days=2)
        unit.progress.last_recall_result = None
        unit.progress.recall_fail_streak = 0
        unit.progress.next_review_at = now + timedelta(days=1)

        service._last_session_unit_ids = frozenset()
        w_no = service._compute_unit_weight(unit, "review", now=now)
        service._last_session_unit_ids = frozenset({99})
        w_yes = service._compute_unit_weight(unit, "review", now=now)
        assert w_yes == pytest.approx(w_no * LAST_SESSION_DIVERSITY_FACTOR)

    def test_last_session_skipped_when_due(self, mock_db):
        service = SessionService(mock_db, random_seed=42)
        now = utc_now()
        unit = MagicMock()
        unit.id = 99
        unit.progress = MagicMock()
        unit.progress.times_failed = 0
        unit.progress.times_correct = 1
        unit.progress.last_seen = now - timedelta(days=2)
        unit.progress.last_recall_result = None
        unit.progress.recall_fail_streak = 0
        unit.progress.next_review_at = now - timedelta(hours=1)

        service._last_session_unit_ids = frozenset()
        w_empty = service._compute_unit_weight(unit, "review", now=now)
        service._last_session_unit_ids = frozenset({99})
        w_with_ids = service._compute_unit_weight(unit, "review", now=now)
        assert w_with_ids == pytest.approx(w_empty)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
