"""Audio relinking service (best-effort, non-destructive)."""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AudioAsset, LearningUnit
from app.services.audio import (
    compute_audio_hash,
    get_tts_service_for_source_language,
    normalize_text_for_audio,
)

logger = logging.getLogger(__name__)


def _sanitize_for_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


def relink_existing_audio_assets(db: Session) -> dict[str, int]:
    """
    Link existing content-addressed audio files on disk to LearningUnit rows.

    - Idempotent: does not recreate existing AudioAsset rows
    - Non-destructive: does not delete files, does not generate audio
    - Best-effort: exceptions are handled by caller (recommended)
    """
    tts_service = get_tts_service_for_source_language(settings.source_language)
    engine = tts_service.engine
    voice = tts_service.voice
    language = tts_service.language

    safe_voice = _sanitize_for_filename(voice)
    safe_language = _sanitize_for_filename(language)

    units_scanned = 0
    audio_reused = 0
    audio_missing = 0

    # Iterate in deterministic order for stable logs.
    units = db.query(LearningUnit).order_by(LearningUnit.id.asc()).all()
    for unit in units:
        units_scanned += 1

        normalized_text = normalize_text_for_audio(unit.text)
        audio_hash = compute_audio_hash(
            engine=engine,
            voice=voice,
            language=language,
            normalized_text=normalized_text,
        )
        filename = f"{audio_hash}_{safe_language}_{safe_voice}.mp3"
        file_path = settings.audio_dir / filename

        if not file_path.exists():
            audio_missing += 1
            continue

        existing = (
            db.query(AudioAsset)
            .filter(AudioAsset.unit_id == unit.id)
            .filter(AudioAsset.engine == engine)
            .filter(AudioAsset.voice == voice)
            .filter(AudioAsset.language == language)
            .first()
        )
        if existing:
            audio_reused += 1
            continue

        relative_path = f"data/audio/{filename}"
        db.add(
            AudioAsset(
                unit_id=unit.id,
                engine=engine,
                voice=voice,
                language=language,
                audio_hash=audio_hash,
                file_path=relative_path,
            )
        )
        audio_reused += 1

    db.commit()

    logger.info(
        "Audio relinking summary: units_scanned=%s audio_reused=%s audio_missing=%s",
        units_scanned,
        audio_reused,
        audio_missing,
    )
    return {
        "units_scanned": units_scanned,
        "audio_reused": audio_reused,
        "audio_missing": audio_missing,
    }

