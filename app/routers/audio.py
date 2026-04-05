"""Audio API router for TTS pronunciation."""

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import LearningUnit, AudioAsset, SentenceAudioAsset
from app.models.session import SessionUnit
from app.schemas.audio import VoiceOverrideRequest, SentenceAudioRequest
from app.services.audio import (
    MurfTTSService,
    ElevenLabsTTSService,
    AudioGenerationError,
    MurfDisabledError,
    MurfInvalidConfigurationError,
    ElevenLabsDisabledError,
    ElevenLabsInvalidConfigurationError,
    normalize_text_for_audio,
    compute_audio_hash,
    get_tts_service_for_source_language,
)
from app.services.audio.audio_cleanup_service import cleanup_orphaned_audio_files
from app.services.audio.elevenlabs_voices import POLISH_VOICES, get_all_voice_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])

def _sanitize_for_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


def get_murf_service() -> MurfTTSService:
    """Dependency to get Murf TTS service instance."""
    return MurfTTSService()


@router.get("/status")
def get_audio_status():
    """
    Check if audio pronunciation is available.
    
    Returns:
        enabled: Whether any TTS engine is enabled and configured.
        engine: Active engine name ("murf", "elevenlabs", or None).
    """
    # Check which engine would be used for the current source language
    service = get_tts_service_for_source_language(settings.source_language)
    enabled = service.is_enabled()
    engine = service.engine if enabled else None
    return {
        "enabled": enabled,
        "engine": engine,
    }


@router.get("/voices")
def get_available_voices(
    murf_service: MurfTTSService = Depends(get_murf_service),
):
    """
    Get available Murf voices filtered by configured language.
    
    Returns a list of available voices matching the configured language
    (settings.murf_language). Each voice includes voice_id, gender, and style.
    
    Returns:
        List of voice dictionaries:
        [
            {
                "voice_id": str,
                "gender": str | None,
                "style": str | None
            },
            ...
        ]
        
    Raises:
        403: Murf TTS is disabled.
        400: API key is missing or invalid.
        500: Failed to fetch voices from Murf API.
    """
    # Check if Murf is enabled
    if not settings.murf_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio pronunciation is not enabled",
        )
    
    # Check if API key is missing
    if not murf_service.api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Murf API key is missing",
        )
    
    try:
        voices = murf_service.get_available_voices()
        return voices
    except MurfDisabledError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio pronunciation is not enabled",
        )
    except MurfInvalidConfigurationError as e:
        logger.error(f"Murf configuration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except AudioGenerationError as e:
        logger.error(f"Failed to fetch available voices: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch available voices: {str(e)}",
        )


@router.get("/voices/polish")
def get_polish_voices():
    """
    Get available Polish ElevenLabs voices for override.
    
    Returns:
        List of allowed Polish voice dictionaries with:
        - id: Voice ID
        - display_name: Human-readable name
        - is_default: True if this is the configured default voice
    """
    default_voice_id = settings.elevenlabs_voice_pl
    
    return [
        {
            "id": voice.voice_id,
            "display_name": voice.display_name,
            "is_default": voice.voice_id == default_voice_id,
        }
        for voice in POLISH_VOICES
    ]


@router.post("/sentence")
def get_sentence_audio(
    request: SentenceAudioRequest,
    db: Session = Depends(get_db),
):
    """
    Generate or serve cached TTS audio for a cloze context sentence.

    Accepts session_unit_id (not raw text) to prevent arbitrary TTS abuse.
    Returns 204 when the unit has no context_sentence (recall-fallback units).
    Returns 403 when TTS is disabled.
    Audio is content-addressed and cached in sentence_audio_assets.
    """
    session_unit = db.query(SessionUnit).filter(
        SessionUnit.id == request.session_unit_id
    ).first()
    if not session_unit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SessionUnit {request.session_unit_id} not found",
        )

    context_sentence = session_unit.unit.context_sentence
    if not context_sentence:
        # Recall-fallback unit — no sentence available, silent no-op for frontend
        return Response(status_code=204)

    tts_service = get_tts_service_for_source_language(settings.source_language)
    if not tts_service.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio pronunciation is not enabled",
        )

    # Hash and generate always use the same normalized string — guarantees cache consistency
    normalized = normalize_text_for_audio(context_sentence)
    audio_hash = compute_audio_hash(
        engine=tts_service.engine,
        voice=tts_service.voice,
        language=tts_service.language,
        normalized_text=normalized,
    )

    # Cache lookup
    asset = (
        db.query(SentenceAudioAsset)
        .filter_by(
            audio_hash=audio_hash,
            engine=tts_service.engine,
            voice=tts_service.voice,
            language=tts_service.language,
        )
        .first()
    )
    if asset:
        file_path = settings.base_dir / asset.file_path
        if file_path.exists():
            logger.debug(
                "[TTS CACHE HIT] sentence hash=%s, session_unit=%s",
                audio_hash[:8], request.session_unit_id,
            )
            return FileResponse(str(file_path), media_type="audio/mpeg",
                                filename="sentence.mp3")
        # Stale record — file deleted externally; flush and regenerate atomically
        logger.warning("[TTS STALE] sentence hash=%s, regenerating", audio_hash[:8])
        db.delete(asset)
        db.flush()  # flush only — commit happens together with the new insert below

    logger.info(
        "[TTS GENERATED] sentence hash=%s, session_unit=%s, engine=%s",
        audio_hash[:8], request.session_unit_id, tts_service.engine,
    )

    try:
        audio_bytes = tts_service.generate_audio(normalized)
    except (MurfDisabledError, ElevenLabsDisabledError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio pronunciation is not enabled",
        )
    except (MurfInvalidConfigurationError, ElevenLabsInvalidConfigurationError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except AudioGenerationError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate sentence audio: {e}",
        )

    safe_voice = _sanitize_for_filename(tts_service.voice)
    safe_language = _sanitize_for_filename(tts_service.language)
    filename = f"sentence_{audio_hash}_{safe_language}_{safe_voice}.mp3"
    relative_path = f"data/audio/{filename}"
    file_path = settings.audio_dir / filename

    file_path.write_bytes(audio_bytes)

    new_asset = SentenceAudioAsset(
        audio_hash=audio_hash,
        engine=tts_service.engine,
        voice=tts_service.voice,
        language=tts_service.language,
        file_path=relative_path,
    )
    db.add(new_asset)
    db.commit()  # single commit covers flush'd delete (if any) + this insert

    return FileResponse(str(file_path), media_type="audio/mpeg", filename="sentence.mp3")


@router.get("/{unit_id}")
def get_audio(
    unit_id: int,
    db: Session = Depends(get_db),
):
    """
    Get audio pronunciation for a learning unit.
    
    Auto-selects TTS engine based on source language:
    - ElevenLabs for Polish (when enabled)
    - Murf for all other languages (or when ElevenLabs disabled)
    
    Checks for existing AudioAsset (including overridden voices) first.
    If not cached, generates via selected TTS API with default voice, caches, then returns.
    
    Args:
        unit_id: ID of the learning unit.
        
    Returns:
        Audio file (audio/mpeg).
        
    Raises:
        403: TTS is disabled.
        404: Learning unit not found.
        400: Configuration error.
        500: Audio generation failed.
    """
    # Load learning unit
    unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
    if not unit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Learning unit {unit_id} not found",
        )
    
    # Auto-select TTS service based on source language
    tts_service = get_tts_service_for_source_language(settings.source_language)
    
    # Check if service is enabled
    if not tts_service.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio pronunciation is not enabled",
        )
    
    # Fast path: look for an AudioAsset for the DEFAULT engine+voice+language first.
    # This makes repeated prefetch calls cheap when the unit is already linked.
    existing_asset = (
        db.query(AudioAsset)
        .filter(
            AudioAsset.unit_id == unit_id,
            AudioAsset.engine == tts_service.engine,
            AudioAsset.voice == tts_service.voice,
            AudioAsset.language == tts_service.language,
        )
        .first()
    )

    # Fallback: look for ANY existing AudioAsset for this unit+engine+language (could be overridden)
    if not existing_asset:
        existing_asset = (
        db.query(AudioAsset)
        .filter(
            AudioAsset.unit_id == unit_id,
            AudioAsset.engine == tts_service.engine,
            AudioAsset.language == tts_service.language,
        )
        .first()
    )
    
    # If we have an existing asset, use it (whether it's default or overridden)
    if existing_asset:
        file_path = settings.base_dir / existing_asset.file_path
        if file_path.exists():
            logger.debug(f"Cache hit for unit {unit_id}: {existing_asset.file_path} (voice: {existing_asset.voice})")
            return FileResponse(
                path=str(file_path),
                media_type="audio/mpeg",
                filename=f"pronunciation_{unit_id}.mp3",
            )
        else:
            # File missing, delete stale AudioAsset record
            logger.warning(f"Audio file missing for asset {existing_asset.id}, regenerating")
            db.delete(existing_asset)
            db.commit()
            existing_asset = None
    
    # No cached asset found, generate with default voice
    normalized_text = normalize_text_for_audio(unit.text)
    audio_hash = compute_audio_hash(
        engine=tts_service.engine,
        voice=tts_service.voice,
        language=tts_service.language,
        normalized_text=normalized_text,
    )

    # Global content-addressed dedup: reuse existing file if present (no regeneration).
    safe_voice = _sanitize_for_filename(tts_service.voice)
    safe_language = _sanitize_for_filename(tts_service.language)
    dedup_filename = f"{audio_hash}_{safe_language}_{safe_voice}.mp3"
    dedup_relative_path = f"data/audio/{dedup_filename}"
    dedup_file_path = settings.audio_dir / dedup_filename
    if dedup_file_path.exists():
        # Create AudioAsset if missing, then return file.
        reused_asset = AudioAsset(
            unit_id=unit_id,
            engine=tts_service.engine,
            voice=tts_service.voice,
            language=tts_service.language,
            audio_hash=audio_hash,
            file_path=dedup_relative_path,
        )
        db.add(reused_asset)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        logger.info(f"Reused existing audio file for unit {unit_id}: {dedup_relative_path}")
        return FileResponse(
            path=str(dedup_file_path),
            media_type="audio/mpeg",
            filename=f"pronunciation_{unit_id}.mp3",
        )
    
    # Generate audio via selected TTS API with default voice
    logger.info(f"Generating audio for unit {unit_id} using {tts_service.engine} (default voice): '{unit.text[:50]}...'")
    
    try:
        audio_bytes = tts_service.generate_audio(unit.text)
    except (MurfDisabledError, ElevenLabsDisabledError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio pronunciation is not enabled",
        )
    except (MurfInvalidConfigurationError, ElevenLabsInvalidConfigurationError) as e:
        logger.error(f"TTS configuration error for unit {unit_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except AudioGenerationError as e:
        logger.error(f"Audio generation failed for unit {unit_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate audio pronunciation: {str(e)}",
        )
    
    # Save audio file
    relative_path = tts_service.save_audio_file(audio_bytes, audio_hash)
    
    # Create AudioAsset record
    new_audio_asset = AudioAsset(
        unit_id=unit_id,
        engine=tts_service.engine,
        voice=tts_service.voice,
        language=tts_service.language,
        audio_hash=audio_hash,
        file_path=relative_path,
    )
    db.add(new_audio_asset)
    db.commit()
    
    logger.info(f"Cached audio for unit {unit_id}: {relative_path}")
    
    # Return the generated audio
    file_path = settings.base_dir / relative_path
    return FileResponse(
        path=str(file_path),
        media_type="audio/mpeg",
        filename=f"pronunciation_{unit_id}.mp3",
    )


@router.post("/{unit_id}/override")
def override_voice(
    unit_id: int,
    request: VoiceOverrideRequest,
    db: Session = Depends(get_db),
):
    """
    Override voice for a learning unit's audio pronunciation.
    
    If confirm=false: Generate preview audio ONLY (no DB write, no file save).
    If confirm=true: Delete existing AudioAsset, generate new audio with selected voice,
                     save file, and persist new AudioAsset.
    
    Only works for ElevenLabs engine (Polish voices).
    
    Args:
        unit_id: ID of the learning unit.
        request: Voice override request with voice ID and confirm flag.
        
    Returns:
        Audio file (audio/mpeg) - preview if confirm=false, saved audio if confirm=true.
        
    Raises:
        403: ElevenLabs TTS is disabled.
        404: Learning unit not found.
        400: Invalid voice ID or configuration error.
        500: Audio generation failed.
    """
    # Load learning unit
    unit = db.query(LearningUnit).filter(LearningUnit.id == unit_id).first()
    if not unit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Learning unit {unit_id} not found",
        )
    
    # Only allow override for ElevenLabs (Polish voices)
    if settings.source_language.lower() != "polish" or not settings.elevenlabs_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Voice override is only available for Polish ElevenLabs voices",
        )
    
    # Validate voice ID is in allowed list
    allowed_voice_ids = get_all_voice_ids()
    if request.voice not in allowed_voice_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Voice ID '{request.voice}' is not in the allowed list",
        )
    
    # Create ElevenLabs service with overridden voice
    tts_service = ElevenLabsTTSService(voice=request.voice)
    
    if not tts_service.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ElevenLabs TTS is not enabled or configured",
        )
    
    # Generate audio with overridden voice
    normalized_text = normalize_text_for_audio(unit.text)
    audio_hash = compute_audio_hash(
        engine=tts_service.engine,
        voice=request.voice,
        language=tts_service.language,
        normalized_text=normalized_text,
    )
    
    try:
        audio_bytes = tts_service.generate_audio(unit.text)
    except (ElevenLabsDisabledError, ElevenLabsInvalidConfigurationError) as e:
        logger.error(f"TTS configuration error for unit {unit_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except AudioGenerationError as e:
        logger.error(f"Audio generation failed for unit {unit_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate audio: {str(e)}",
        )
    
    if not request.confirm:
        # Preview mode: reuse existing file if present, otherwise generate bytes without saving.
        safe_voice = _sanitize_for_filename(request.voice)
        safe_language = _sanitize_for_filename(tts_service.language)
        preview_filename = f"{audio_hash}_{safe_language}_{safe_voice}.mp3"
        preview_file_path = settings.audio_dir / preview_filename
        if preview_file_path.exists():
            logger.info(f"Preview reused existing audio for unit {unit_id} with voice {request.voice}")
            return Response(
                content=preview_file_path.read_bytes(),
                media_type="audio/mpeg",
                headers={"Content-Disposition": f'attachment; filename="preview_{unit_id}.mp3"'},
            )

        logger.info(f"Preview audio generated for unit {unit_id} with voice {request.voice}")
        return Response(
            content=audio_bytes,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="preview_{unit_id}.mp3"'},
        )
    
    # Confirm mode: delete existing AudioAssets for this unit+engine, then save new one.
    # IMPORTANT: Do NOT delete audio files here. Audio is global/content-addressed and may be shared.
    existing_assets = (
        db.query(AudioAsset)
        .filter(
            AudioAsset.unit_id == unit_id,
            AudioAsset.engine == tts_service.engine,
        )
        .all()
    )
    
    # Delete AudioAsset records (do not delete files; cleanup service can remove orphans safely).
    for asset in existing_assets:
        db.delete(asset)
    
    # Global content-addressed dedup: if target file already exists, reuse it (no regeneration).
    safe_voice = _sanitize_for_filename(request.voice)
    safe_language = _sanitize_for_filename(tts_service.language)
    dedup_filename = f"{audio_hash}_{safe_language}_{safe_voice}.mp3"
    dedup_relative_path = f"data/audio/{dedup_filename}"
    dedup_file_path = settings.audio_dir / dedup_filename
    if dedup_file_path.exists():
        new_audio_asset = AudioAsset(
            unit_id=unit_id,
            engine=tts_service.engine,
            voice=request.voice,
            language=tts_service.language,
            audio_hash=audio_hash,
            file_path=dedup_relative_path,
        )
        db.add(new_audio_asset)
        db.commit()
        logger.info(
            f"Voice override reused existing audio for unit {unit_id}: voice={request.voice}, file={dedup_relative_path}"
        )
        return FileResponse(
            path=str(dedup_file_path),
            media_type="audio/mpeg",
            filename=f"pronunciation_{unit_id}.mp3",
        )

    # Save new audio file
    relative_path = tts_service.save_audio_file(audio_bytes, audio_hash)
    
    # Create new AudioAsset record with overridden voice
    new_audio_asset = AudioAsset(
        unit_id=unit_id,
        engine=tts_service.engine,
        voice=request.voice,
        language=tts_service.language,
        audio_hash=audio_hash,
        file_path=relative_path,
    )
    db.add(new_audio_asset)
    db.commit()
    
    logger.info(f"Voice override saved for unit {unit_id}: voice={request.voice}, file={relative_path}")
    
    # Return the saved audio file
    file_path = settings.base_dir / relative_path
    return FileResponse(
        path=str(file_path),
        media_type="audio/mpeg",
        filename=f"pronunciation_{unit_id}.mp3",
    )


@router.post("/cleanup")
def cleanup_audio(
    db: Session = Depends(get_db),
):
    """
    Clean up orphaned audio files (DEV-ONLY endpoint).
    
    Removes audio files from data/audio/ that are not referenced by any AudioAsset.
    This endpoint is only available in development mode.
    
    Protected by:
    - settings.debug == True OR
    - settings.env == "development"
    
    Returns:
        Dictionary with:
        - files_deleted: Number of files deleted
        - bytes_freed: Total bytes freed
        
    Raises:
        403: Endpoint not available in production mode.
    """
    # Check if endpoint is available (dev-only)
    if not (settings.debug or settings.env == "development"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audio cleanup endpoint is only available in development mode",
        )
    
    result = cleanup_orphaned_audio_files(db)
    return result
