"""Tests for time-decayed effective confidence."""

from datetime import datetime, timedelta

import pytest

from app.utils.time import utc_now
from app.services.session_service import (
    compute_time_decay,
    compute_effective_confidence,
    TIME_DECAY_RATE,
    TIME_DECAY_MIN,
    WEAK_THRESHOLD,
)


class TestComputeTimeDecay:
    """Test time decay calculation."""
    
    def test_never_seen_no_decay(self):
        """Units never seen should have no decay (factor = 1.0)."""
        decay = compute_time_decay(last_seen=None)
        assert decay == 1.0
    
    def test_just_seen_no_decay(self):
        """Units just seen (0 days ago) should have no decay."""
        now = utc_now()
        decay = compute_time_decay(last_seen=now, now=now)
        assert decay == 1.0
    
    def test_one_day_decay(self):
        """After 1 day, decay should be 1.0 - 0.08 = 0.92."""
        now = utc_now()
        one_day_ago = now - timedelta(days=1)
        decay = compute_time_decay(last_seen=one_day_ago, now=now)
        assert decay == pytest.approx(0.92, abs=0.01)
    
    def test_three_days_decay(self):
        """After 3 days, decay should be 1.0 - 0.24 = 0.76."""
        now = utc_now()
        three_days_ago = now - timedelta(days=3)
        decay = compute_time_decay(last_seen=three_days_ago, now=now)
        assert decay == pytest.approx(0.76, abs=0.01)
    
    def test_seven_days_decay(self):
        """After 7 days, decay should be 1.0 - 0.56 = 0.44."""
        now = utc_now()
        seven_days_ago = now - timedelta(days=7)
        decay = compute_time_decay(last_seen=seven_days_ago, now=now)
        assert decay == pytest.approx(0.44, abs=0.01)
    
    def test_minimum_decay_floor(self):
        """Decay should not go below TIME_DECAY_MIN (0.4)."""
        now = utc_now()
        thirty_days_ago = now - timedelta(days=30)
        decay = compute_time_decay(last_seen=thirty_days_ago, now=now)
        assert decay == TIME_DECAY_MIN  # 0.4
    
    def test_very_old_still_minimum(self):
        """Even very old items should have minimum decay, not zero."""
        now = utc_now()
        year_ago = now - timedelta(days=365)
        decay = compute_time_decay(last_seen=year_ago, now=now)
        assert decay == TIME_DECAY_MIN  # 0.4
    
    def test_half_day_decay(self):
        """Partial day should be calculated correctly."""
        now = utc_now()
        half_day_ago = now - timedelta(hours=12)
        decay = compute_time_decay(last_seen=half_day_ago, now=now)
        # 0.5 days * 0.08 = 0.04 decay
        assert decay == pytest.approx(0.96, abs=0.01)
    
    def test_injectable_now_for_testing(self):
        """The 'now' parameter should be injectable for deterministic testing."""
        fixed_now = datetime(2026, 1, 1, 12, 0, 0)
        last_seen = datetime(2025, 12, 31, 12, 0, 0)  # 1 day before
        
        decay = compute_time_decay(last_seen=last_seen, now=fixed_now)
        assert decay == pytest.approx(0.92, abs=0.01)


class TestComputeEffectiveConfidence:
    """Test effective confidence calculation with time decay."""
    
    def test_never_seen_full_confidence(self):
        """Never seen + high stored confidence = full effective confidence."""
        effective = compute_effective_confidence(
            confidence_score=0.9,
            last_seen=None,
        )
        assert effective == 0.9
    
    def test_just_seen_no_change(self):
        """Just seen = no decay applied."""
        now = utc_now()
        effective = compute_effective_confidence(
            confidence_score=0.8,
            last_seen=now,
            now=now,
        )
        assert effective == 0.8
    
    def test_confidence_decays_over_time(self):
        """Confidence should decay over time."""
        now = utc_now()
        one_week_ago = now - timedelta(days=7)
        
        effective = compute_effective_confidence(
            confidence_score=0.8,
            last_seen=one_week_ago,
            now=now,
        )
        # 0.8 * 0.44 ≈ 0.35
        assert effective == pytest.approx(0.35, abs=0.02)
    
    def test_zero_confidence_stays_zero(self):
        """Zero confidence should stay zero regardless of decay."""
        now = utc_now()
        effective = compute_effective_confidence(
            confidence_score=0.0,
            last_seen=now,
            now=now,
        )
        assert effective == 0.0
    
    def test_high_confidence_becomes_weak_over_time(self):
        """
        A unit with high stored confidence can become 'weak' over time.
        
        Example: 0.8 confidence after 7 days = 0.8 * 0.44 = 0.35 < 0.5
        """
        now = utc_now()
        week_ago = now - timedelta(days=7)
        
        stored_confidence = 0.8  # High - would normally be "known"
        effective = compute_effective_confidence(
            confidence_score=stored_confidence,
            last_seen=week_ago,
            now=now,
        )
        
        # Should now be below weak threshold
        assert effective < WEAK_THRESHOLD
    
    def test_recently_seen_high_confidence_stays_known(self):
        """
        Recently seen high confidence should stay 'known'.
        
        Example: 0.8 confidence after 1 day = 0.8 * 0.92 = 0.74 >= 0.5
        """
        now = utc_now()
        yesterday = now - timedelta(days=1)
        
        stored_confidence = 0.8
        effective = compute_effective_confidence(
            confidence_score=stored_confidence,
            last_seen=yesterday,
            now=now,
        )
        
        # Should still be above weak threshold
        assert effective >= WEAK_THRESHOLD


class TestTimeDecayConstants:
    """Test that constants are set correctly."""
    
    def test_decay_rate(self):
        """Decay rate should be 8% per day."""
        assert TIME_DECAY_RATE == 0.08
    
    def test_minimum_decay(self):
        """Minimum decay should be 40%."""
        assert TIME_DECAY_MIN == 0.4
    
    def test_weak_threshold(self):
        """Weak threshold should be 50%."""
        assert WEAK_THRESHOLD == 0.5
    
    def test_days_until_minimum(self):
        """
        Calculate how many days until minimum decay is reached.
        
        1.0 - (days * 0.08) = 0.4
        days * 0.08 = 0.6
        days = 7.5
        """
        days_to_minimum = (1.0 - TIME_DECAY_MIN) / TIME_DECAY_RATE
        assert days_to_minimum == 7.5


class TestEffectiveConfidenceEdgeCases:
    """Edge cases for effective confidence calculation."""
    
    def test_deterministic_same_inputs_same_output(self):
        """Same inputs should always produce same output."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0)
        last_seen = datetime(2026, 1, 7, 12, 0, 0)  # 3 days before
        
        result1 = compute_effective_confidence(0.7, last_seen, fixed_now)
        result2 = compute_effective_confidence(0.7, last_seen, fixed_now)
        result3 = compute_effective_confidence(0.7, last_seen, fixed_now)
        
        assert result1 == result2 == result3
    
    def test_boundary_at_weak_threshold(self):
        """Test behavior right at the weak threshold boundary."""
        now = utc_now()
        
        # Find confidence that will be exactly at threshold after 3 days
        # effective = stored * decay
        # 0.5 = stored * 0.76
        # stored = 0.5 / 0.76 ≈ 0.658
        
        three_days_ago = now - timedelta(days=3)
        stored = 0.658
        
        effective = compute_effective_confidence(stored, three_days_ago, now)
        assert effective == pytest.approx(WEAK_THRESHOLD, abs=0.01)
    
    def test_future_last_seen_no_negative_decay(self):
        """If last_seen is in future (clock skew), don't boost confidence."""
        now = utc_now()
        future = now + timedelta(days=1)
        
        # Should treat as "just seen" (decay = 1.0), not boost
        decay = compute_time_decay(last_seen=future, now=now)
        assert decay == 1.0
