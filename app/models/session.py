"""SQLAlchemy models for learning sessions."""

from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.learning_unit import RecallResult


class StudyModeType(str, PyEnum):
    """Type of study mode for a session."""
    PASSIVE = "passive"       # Show PL, user self-assesses (existing behavior)
    RECALL = "recall"         # Show EN, user types PL, backend evaluates (visual recall)
    RECALL_AUDIO = "recall_audio"  # Hear PL audio, user types PL, backend evaluates
    CLOZE = "cloze"           # Sentence with blank; type target word (recall-like progress)


def is_recall_like_study_mode(mode: StudyModeType) -> bool:
    """Active retrieval modes that share recall progress, gating, and scoring."""
    return mode in (
        StudyModeType.RECALL,
        StudyModeType.RECALL_AUDIO,
        StudyModeType.CLOZE,
    )


class SessionLifecycleStatus(str, PyEnum):
    """Lifecycle state for a learning session."""
    CREATED = "created"
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class LearningSession(Base):
    """
    A learning session containing 50 learning units by default.
    
    Sessions are immutable once created (locked=True).
    
    Mode types:
    - passive: User sees source text, self-assesses knowledge
    - recall: User sees translation, types source text, backend evaluates (visual recall)
    - recall_audio: User hears audio, types source text, backend evaluates (audio recall)
    - cloze: User sees a sentence with a blank, types source text, backend evaluates
    """
    __tablename__ = "learning_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    mode = Column(Enum(StudyModeType), default=StudyModeType.PASSIVE, nullable=False)
    status = Column(
        Enum(SessionLifecycleStatus),
        default=SessionLifecycleStatus.CREATED,
        nullable=False,
    )
    locked = Column(Boolean, default=True, nullable=False)
    completed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    abandoned_at = Column(DateTime, nullable=True)
    # True when session was created as global due-only review (daily cap applies to these only).
    due_only = Column(Boolean, default=False, nullable=False)
    # Passive→recall chain hint: "weak" / "lesson" (mirrors ephemeral _SESSION_PASSIVE_RECALL_AFTER).
    passive_recall_chain = Column(String(16), nullable=True)

    # Session summary metrics (persisted on completion)
    # These are stored values, not computed, for historical accuracy
    summary_total_units = Column(Integer, nullable=True)
    summary_answered_units = Column(Integer, nullable=True)
    summary_correct_count = Column(Integer, nullable=True)
    summary_partial_count = Column(Integer, nullable=True)
    summary_failed_count = Column(Integer, nullable=True)
    
    # Relationship to session units
    units = relationship(
        "SessionUnit",
        back_populates="session",
        order_by="SessionUnit.position",
        cascade="all, delete-orphan",
    )
    
    @property
    def started_at(self) -> datetime:
        """Alias for created_at - when the session was started."""
        return self.created_at
    
    @property
    def total_units(self) -> int:
        """Total number of units in session."""
        return len(self.units)
    
    @property
    def answered_units(self) -> int:
        """Number of units that have been answered."""
        return sum(1 for u in self.units if u.answered)
    
    @property
    def correct_count(self) -> int:
        """
        Number of correct answers (including partials) in session.
        
        Counts units with is_correct == True.
        This includes both exact matches and partial matches (minor typos).
        For recall mode: CORRECT and PARTIAL both count as correct.
        For passive mode: counts units with is_correct == True.
        """
        return sum(1 for u in self.units if u.is_correct)
    
    @property
    def partial_count(self) -> int:
        """
        Number of partial answers (minor typos) in session.
        
        Only applicable to recall mode. Passive mode always returns 0 partials.
        """
        return sum(1 for u in self.units if u.recall_result == RecallResult.PARTIAL)
    
    @property
    def failed_count(self) -> int:
        """Number of failed/wrong answers in session."""
        return sum(1 for u in self.units if u.answered and not u.is_correct)
    
    @property
    def is_complete(self) -> bool:
        """Check if all units have been answered."""
        return all(u.answered for u in self.units)
    
    def __repr__(self) -> str:
        return (
            f"<LearningSession(id={self.id}, status={self.status}, "
            f"locked={self.locked}, complete={self.completed})>"
        )


class SessionUnit(Base):
    """
    A learning unit within a session.
    
    Tracks position in session and answer status.
    For recall mode, stores user's typed input.
    """
    __tablename__ = "session_units"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("learning_sessions.id"), nullable=False)
    unit_id = Column(Integer, ForeignKey("learning_units.id"), nullable=False)
    position = Column(Integer, nullable=False)  # 1 to 50 by default
    answered = Column(Boolean, default=False, nullable=False)
    is_correct = Column(Boolean, nullable=True)  # None = not answered yet
    recall_result = Column(Enum(RecallResult), nullable=True)  # correct/partial/failed (recall mode)
    user_input = Column(String, nullable=True)  # User's typed answer (recall mode)
    answered_at = Column(DateTime, nullable=True)
    # Creation-time selection label (due, new, weak, …). Used when process cache is cold.
    selection_reason = Column(String(32), nullable=True)
    exercise_type = Column(String(16), nullable=False, default="recall")
    cloze_prompt = Column(String, nullable=True)
    context_sentence_translation = Column(String, nullable=True)

    # Relationships
    session = relationship("LearningSession", back_populates="units")
    unit = relationship("LearningUnit")
    
    def __repr__(self) -> str:
        return f"<SessionUnit(session={self.session_id}, unit={self.unit_id}, pos={self.position})>"
