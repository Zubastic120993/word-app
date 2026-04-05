"""Audio cleanup service for removing orphaned audio files."""

import logging
from pathlib import Path
from typing import Set

from sqlalchemy.orm import Session

from app.config import settings
from app.models.audio import AudioAsset

logger = logging.getLogger(__name__)


def cleanup_orphaned_audio_files(db: Session) -> dict[str, int]:
    """
    Remove orphaned audio files that are not referenced by any AudioAsset.
    
    Logic:
    - Reads all file paths from AudioAsset table
    - Scans data/audio/ directory
    - Deletes files that exist on disk but are NOT referenced by any AudioAsset
    - Ignores missing files silently (they may have been manually deleted)
    
    This function is idempotent and safe to run multiple times.
    
    Args:
        db: Database session to query AudioAsset records.
        
    Returns:
        Dictionary with:
        - files_deleted: Number of files deleted
        - bytes_freed: Total bytes freed
        
    Raises:
        OSError: If audio directory cannot be accessed (will be logged but not raised)
    """
    audio_dir = settings.audio_dir
    bytes_freed = 0
    files_deleted = 0
    
    # Get all referenced file paths from database
    referenced_paths: Set[Path] = set()
    audio_assets = db.query(AudioAsset.file_path).all()
    
    for (file_path_str,) in audio_assets:
        # Convert relative path to absolute path for comparison
        referenced_paths.add(settings.base_dir / file_path_str)
    
    logger.info(f"Found {len(referenced_paths)} audio files referenced in database")
    
    # Scan audio directory
    if not audio_dir.exists():
        logger.info(f"Audio directory does not exist: {audio_dir}, skipping cleanup")
        return {"files_deleted": 0, "bytes_freed": 0}
    
    if not audio_dir.is_dir():
        logger.warning(f"Audio path exists but is not a directory: {audio_dir}, skipping cleanup")
        return {"files_deleted": 0, "bytes_freed": 0}
    
    # Get all files in audio directory
    try:
        all_files = list(audio_dir.glob("*.mp3"))
    except OSError as e:
        logger.error(f"Failed to scan audio directory {audio_dir}: {e}")
        return {"files_deleted": 0, "bytes_freed": 0}
    
    logger.info(f"Scanned {len(all_files)} audio files in directory")
    
    # Find orphaned files (exist on disk but not in database)
    orphaned_files = []
    for file_path in all_files:
        if file_path not in referenced_paths:
            orphaned_files.append(file_path)
    
    logger.info(f"Found {len(orphaned_files)} orphaned audio files")
    
    # Delete orphaned files
    for file_path in orphaned_files:
        try:
            file_size = file_path.stat().st_size
            file_path.unlink()
            bytes_freed += file_size
            files_deleted += 1
            logger.debug(f"Deleted orphaned audio file: {file_path.name}")
        except OSError as e:
            logger.warning(f"Failed to delete orphaned file {file_path}: {e}")
    
    logger.info(
        f"Audio cleanup complete: deleted {files_deleted} files, "
        f"freed {bytes_freed} bytes ({bytes_freed / 1024 / 1024:.2f} MB)"
    )
    
    return {"files_deleted": files_deleted, "bytes_freed": bytes_freed}
