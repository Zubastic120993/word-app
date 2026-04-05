"""SQLAlchemy models for vocabulary groups and vocabularies."""

from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import relationship
from sqlalchemy.sql.sqltypes import DateTime

from app.database import Base


class VocabularyGroup(Base):
    """
    A group of related vocabularies for organization.
    
    Examples:
    - "Czytaj Po Polsku - Level 1" (groups czytaj_01_* files)
    - "Polish-Ukrainian Dictionary" (groups polish_ukrainian_dictionary_* files)
    """

    __tablename__ = "vocabulary_groups"
    __table_args__ = (
        UniqueConstraint("user_key", "name", name="uq_vocabulary_groups_user_key_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_key = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    display_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationship to vocabularies in this group
    vocabularies = relationship("Vocabulary", back_populates="group")

    def __repr__(self) -> str:
        return f"<VocabularyGroup(id={self.id}, name='{self.name}')>"


class Vocabulary(Base):
    """
    A named vocabulary collection.

    This app is currently single-user/local-first, but we still model ownership
    with a stable `user_key` so "Chat Vocabulary" is created once per user_key
    and reused deterministically.
    """

    __tablename__ = "vocabularies"
    __table_args__ = (
        UniqueConstraint("user_key", "name", name="uq_vocabularies_user_key_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_key = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("vocabulary_groups.id"), nullable=True, index=True)
    # Curriculum: PL–UA lesson spine vs Czytaj reinforcement (see migration ck_vocab_track_type)
    track_type = Column(String, nullable=True)
    lesson_index = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    units = relationship("LearningUnit", back_populates="vocabulary")
    group = relationship("VocabularyGroup", back_populates="vocabularies")

    def __repr__(self) -> str:
        return f"<Vocabulary(id={self.id}, user_key='{self.user_key}', name='{self.name}')>"
