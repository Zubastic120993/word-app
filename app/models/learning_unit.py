"""SQLAlchemy models for learning units and progress tracking."""

import re
import unicodedata
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Enum,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


def normalize_text(text: str) -> str:
    """
    Normalize text for duplicate detection.
    
    Normalization rules:
    - Lowercase
    - Strip leading/trailing whitespace
    - Unicode NFC normalization (compose characters)
    - Collapse multiple spaces to single space
    
    Args:
        text: Raw text to normalize.
        
    Returns:
        Normalized text for comparison/storage.
    """
    if not text:
        return ""
    
    # Unicode NFC normalization (compose characters)
    normalized = unicodedata.normalize("NFC", text)
    
    # Lowercase
    normalized = normalized.lower()
    
    # Strip leading/trailing whitespace
    normalized = normalized.strip()
    
    # Collapse multiple spaces to single space
    normalized = re.sub(r"\s+", " ", normalized)
    
    return normalized


class UnitType(str, PyEnum):
    """Type of learning unit."""
    WORD = "word"
    PHRASE = "phrase"
    SENTENCE = "sentence"


class RecallResult(str, PyEnum):
    """Result of active recall evaluation."""
    CORRECT = "correct"    # Exact or near-exact match
    PARTIAL = "partial"    # Minor typo (≤1 char), punctuation difference
    FAILED = "failed"      # Incorrect answer


class LearningUnit(Base):
    """
    A single learning unit extracted from a PDF.
    
    Can be a word, phrase, or sentence.
    
    Duplicate prevention:
    - normalized_text and normalized_translation store normalized versions
    - UNIQUE constraint on (normalized_text, normalized_translation) prevents duplicates
    - Same word with different translations is allowed (multiple meanings)
    - Same translation with different source words is allowed
    """
    __tablename__ = "learning_units"
    __table_args__ = (
        # Prevent duplicate vocabulary entries (same normalized text + translation)
        UniqueConstraint(
            "normalized_text",
            "normalized_translation",
            name="uq_learning_unit_normalized",
        ),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    text = Column(String, nullable=False)
    type = Column(Enum(UnitType), nullable=False)
    part_of_speech = Column(String, nullable=True)
    translation = Column(String, nullable=False)
    source_pdf = Column(String, nullable=False)
    vocabulary_id = Column(Integer, ForeignKey("vocabularies.id"), nullable=True, index=True)
    page_number = Column(Integer, nullable=True)
    lesson_title = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    
    # Normalized versions for duplicate detection (auto-populated on save)
    normalized_text = Column(String, nullable=True, index=True)
    normalized_translation = Column(String, nullable=True, index=True)

    # Optional example sentence (from AI or future PDF extraction) for cloze practice
    context_sentence = Column(String, nullable=True)
    context_sentence_translation = Column(String, nullable=True)
    
    # Relationship to progress tracking
    progress = relationship(
        "LearningProgress",
        back_populates="unit",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # Explicit vocabulary grouping (deterministic assignment)
    vocabulary = relationship(
        "Vocabulary",
        back_populates="units",
        uselist=False,
    )
    
    # Relationship to audio assets (cascade delete)
    audio_assets = relationship(
        "AudioAsset",
        back_populates="unit",
        cascade="all, delete-orphan",
    )
    
    def __repr__(self) -> str:
        return f"<LearningUnit(id={self.id}, text='{self.text[:20]}...', type={self.type})>"


class LearningProgress(Base):
    """
    Progress tracking for a learning unit.
    
    Tracks how many times a unit has been seen, correct/failed attempts,
    and calculates a confidence score.
    
    Two complementary scores:
    - confidence_score: Short-term, decaying retrievability signal (0.0-1.0).
      Modified by answers AND subject to time decay for scheduling.
    - stability_score: Long-term memory maturity (0.0-1.0).
      Modified ONLY by recall outcomes. Never decays over time.
      Higher stability makes confidence more resistant to single failures.
    
    The last_recall_result stores the most recent active recall evaluation:
    - correct: User typed the answer exactly (or near-exactly)
    - partial: Minor mistake (≤1 char typo, punctuation)
    - failed: Incorrect answer
    - null: Never tested in recall mode, or only seen in passive mode
    """
    __tablename__ = "learning_progress"
    
    id = Column(Integer, primary_key=True, index=True)
    unit_id = Column(Integer, ForeignKey("learning_units.id"), unique=True, nullable=False)
    times_seen = Column(Integer, default=0, nullable=False)
    times_correct = Column(Integer, default=0, nullable=False)
    times_failed = Column(Integer, default=0, nullable=False)
    confidence_score = Column(Float, default=0.0, nullable=False)
    last_seen = Column(DateTime, nullable=True)
    # Last active recall result - null if never tested in recall mode
    last_recall_result = Column(Enum(RecallResult), nullable=True, default=None)
    
    # SRS-lite: Next scheduled review time
    # Nullable for backward compatibility with existing rows
    # Will be computed from effective_confidence after each answer
    next_review_at = Column(DateTime, nullable=True, default=None)
    
    # Passive → Recall gating: Timestamp when unit was first introduced in Passive mode
    # NULL = never introduced (only appears in Passive mode)
    # NOT NULL = introduced (eligible for Recall modes)
    # Set on first correct answer in Passive mode ("I know"), idempotent.
    introduced_at = Column(DateTime, nullable=True, default=None)
    
    # ===================
    # SRS-lite: Consecutive failure tracking
    # ===================
    # Counts consecutive recall failures (resets on CORRECT)
    # Used to identify struggling words and adjust scheduling
    recall_fail_streak = Column(Integer, default=0, nullable=False)
    
    # Flag for words with persistent difficulty (streak >= 5)
    # Words marked as blocked need special attention but still follow normal scheduling
    is_blocked = Column(Boolean, default=False, nullable=False)
    
    # ===================
    # Stability score: Long-term memory maturity
    # ===================
    # Represents how well a word is entrenched in long-term memory.
    # - Modified ONLY by recall outcomes (correct/partial/failed).
    # - NOT affected by time decay, passive mode, or any other mechanism.
    # - Used to make confidence updates adaptive: mature words are more
    #   resistant to single failures (higher smoothing alpha, scaled penalties).
    stability_score = Column(Float, default=0.0, nullable=False)

    # ===================
    # FSRS fields (Phase 1: populated, not yet used for scheduling)
    # ===================
    # fsrs_stability: estimated days until 90% recall probability.
    #   Initialized from (next_review_at - last_seen) interval; updated by FSRS after each review.
    # fsrs_difficulty: inherent word hardness 1.0–10.0.
    #   Initialized from confidence_score + recall_fail_streak; updated by FSRS after each review.
    # fsrs_last_review: explicit timestamp of last FSRS-tracked review.
    #   Mirrors last_seen initially; decoupled once Phase 3 is live to protect FSRS from passive updates.
    fsrs_stability = Column(Float, nullable=True, default=None)
    fsrs_difficulty = Column(Float, nullable=True, default=None)
    fsrs_last_review = Column(DateTime, nullable=True, default=None)

    # Relationship to learning unit
    unit = relationship("LearningUnit", back_populates="progress")
    
    def __repr__(self) -> str:
        return f"<LearningProgress(unit_id={self.unit_id}, confidence={self.confidence_score})>"


class Settings(Base):
    """
    Application settings stored in database.
    
    Single-row table for persistent configuration.
    """
    __tablename__ = "settings"
    
    id = Column(Integer, primary_key=True, index=True)
    offline_mode = Column(Boolean, default=True, nullable=False)
    ai_provider = Column(String, default="ollama", nullable=False)
    ollama_model = Column(String, default="llama3.2", nullable=True)
    strict_mode = Column(Boolean, default=True, nullable=False)
    source_language = Column(String, default="Polish", nullable=False)
    target_language = Column(String, default="English", nullable=False)
    db_instance_id = Column(String, nullable=True)
    
    def __repr__(self) -> str:
        return f"<Settings(offline={self.offline_mode}, ai={self.ai_provider})>"
