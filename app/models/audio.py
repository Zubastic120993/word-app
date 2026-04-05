"""SQLAlchemy model for audio assets."""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


class AudioAsset(Base):
    """
    Audio pronunciation asset for a learning unit.
    
    Stores metadata about generated TTS audio files.
    Audio files are stored on disk at data/audio/, NOT in the database.
    
    Caching strategy:
    - audio_hash is computed from (engine, voice, language, normalized_text)
    - UNIQUE constraint on (unit_id, engine, voice, language) prevents duplicates
    - file_path stores relative path like "data/audio/xxx_en_en-US-marcus.mp3"
    
    Cascade behavior:
    - When a LearningUnit is deleted, its AudioAsset records are also deleted
    - Note: The actual audio file on disk must be cleaned up separately
    """
    __tablename__ = "audio_assets"
    __table_args__ = (
        # Prevent duplicate audio generation for same unit+engine+voice+language
        UniqueConstraint(
            "unit_id",
            "engine",
            "voice",
            "language",
            name="uq_audio_asset_unit_engine_voice_language",
        ),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    unit_id = Column(
        Integer,
        ForeignKey("learning_units.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engine = Column(String, nullable=False, default="murf")  # TTS engine identifier
    voice = Column(String, nullable=False)  # Voice ID (e.g., "en-US-marcus")
    language = Column(String, nullable=False)  # Language code (e.g., "en-US")
    audio_hash = Column(String, nullable=False, index=True)  # Deterministic hash
    file_path = Column(String, nullable=False)  # Relative path: "data/audio/xxx.mp3"
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    
    # Relationship to learning unit
    unit = relationship("LearningUnit", back_populates="audio_assets")
    
    def __repr__(self) -> str:
        return f"<AudioAsset(id={self.id}, unit_id={self.unit_id}, engine={self.engine}, voice={self.voice})>"


class SentenceAudioAsset(Base):
    """
    Content-addressed audio cache for full context sentences (cloze mode TTS).

    Unlike AudioAsset, not tied to a specific LearningUnit — the same sentence
    may be shared across multiple units. Keyed on audio_hash computed from
    (engine, voice, language, normalized_text).

    Audio files are stored on disk at data/audio/, NOT in the database.
    """
    __tablename__ = "sentence_audio_assets"
    __table_args__ = (
        UniqueConstraint(
            "audio_hash",
            "engine",
            "voice",
            "language",
            name="uq_sentence_audio_hash_engine_voice_lang",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    audio_hash = Column(String, nullable=False, index=True)
    engine = Column(String, nullable=False)
    voice = Column(String, nullable=False)
    language = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<SentenceAudioAsset(id={self.id}, hash={self.audio_hash[:8]}, engine={self.engine})>"
