"""Database connection and session management."""

import hashlib
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "vocabulary.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

db_path = Path(settings.database_path)

_is_testing = sys.modules.get("pytest") is not None or os.environ.get("WORD_APP_TESTING") == "1"

# Hard safety guard: tests must never use the production vocabulary.db by default.
# Only fires when WORD_APP_DATABASE_PATH was NOT explicitly set — meaning the
# default path slipped through without conftest.py overriding it.
# Tests that intentionally pass a custom path (e.g. test_verify_db_cli.py) are fine.
_db_path_overridden = "WORD_APP_DATABASE_PATH" in os.environ
if _is_testing and not _db_path_overridden and db_path.name == "vocabulary.db":
    raise RuntimeError(
        "SAFETY VIOLATION: Tests are pointing at production vocabulary.db. "
        "Set WORD_APP_DATABASE_PATH before importing app modules "
        "(conftest.py must run first)."
    )

if not db_path.exists() and not _is_testing and Path(sys.argv[0]).name != "alembic":
    raise RuntimeError(
        f"Database file not found: {db_path}\n"
        "Restore a backup before starting the application."
    )

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    future=True,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """
    Dependency that provides a database session.
    
    Yields:
        Session: SQLAlchemy database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Database integrity guard
# ---------------------------------------------------------------------------
# WHY: On Feb 9 2026 the app silently started against a corrupt / empty DB,
# which caused maintenance tasks (backfill, relink, backup) to run on
# invalid data and propagate the damage into backups.  The guard below
# ensures the process exits immediately if the DB schema is incomplete.
# ---------------------------------------------------------------------------

# Tables that MUST exist for the app to function correctly.
REQUIRED_TABLES = {"learning_units", "learning_progress", "learning_sessions"}
CHECKSUM_SUFFIX = ".sha256"


def _get_existing_tables(engine_to_check=None) -> set[str]:
    """Return the set of table names present in the SQLite database."""
    eng = engine_to_check or engine
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    return {row[0] for row in rows}


def verify_required_tables() -> None:
    """
    Startup safety guard — abort the process if the DB is missing core tables.

    WHY THIS EXISTS:
    On 2026-02-09 the app started against a DB that had lost its schema.
    Maintenance tasks (backfill, audio relink, auto-backup) ran against the
    broken DB without complaint, silently destroying data and backing up the
    broken state.  This guard prevents that scenario by halting the process
    before any maintenance work can begin.

    If tables are missing, startup fails immediately. We do not auto-repair
    schema during startup.
    """
    db_path_resolved = Path(settings.database_path).expanduser().resolve()
    logger.info("Database path: %s", db_path_resolved)

    existing = _get_existing_tables()
    logger.debug("Tables detected in database: %s", sorted(existing))

    missing = REQUIRED_TABLES - existing
    if missing:
        logger.error(
            "FATAL: Database is INVALID — required tables missing: %s. "
            "DB path: %s. "
            "Run: alembic upgrade head",
            sorted(missing),
            db_path_resolved,
        )
        raise RuntimeError("Required tables missing. Run: alembic upgrade head")

    logger.info(
        "Database schema OK — all %d required tables present.",
        len(REQUIRED_TABLES),
    )


def verify_sqlite_integrity() -> None:
    """
    Run PRAGMA integrity_check.
    Raise RuntimeError if result is not 'ok'.
    """
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA integrity_check;")).fetchone()

    if not result or result[0] != "ok":
        raise RuntimeError("SQLite integrity check failed.")


def compute_db_checksum(db_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(db_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def write_db_checksum(db_path: str) -> None:
    checksum = compute_db_checksum(db_path)
    checksum_path = Path(db_path + CHECKSUM_SUFFIX)
    checksum_path.write_text(checksum)
    logging.getLogger(__name__).info("Database checksum written.")


def verify_db_checksum(db_path: str) -> None:
    checksum_path = Path(db_path + CHECKSUM_SUFFIX)
    if not checksum_path.exists():
        logging.getLogger(__name__).info("No checksum file found. Skipping verification.")
        return

    stored = checksum_path.read_text().strip()
    current = compute_db_checksum(db_path)

    if stored != current:
        logging.getLogger(__name__).error("Database checksum mismatch detected.")
        raise RuntimeError("Database checksum mismatch.")

    logging.getLogger(__name__).info("Database checksum verified successfully.")


def perform_safe_shutdown() -> None:
    """
    Ensure WAL checkpoint and integrity before shutdown.
    """
    from app.database import engine
    import logging

    logger = logging.getLogger(__name__)

    with engine.connect() as conn:
        # Force WAL checkpoint (no-op in DELETE journal mode but harmless)
        conn.execute(text("PRAGMA wal_checkpoint(FULL);"))

        # Run integrity check
        result = conn.execute(text("PRAGMA integrity_check;")).fetchone()

    if not result or result[0] != "ok":
        logger.error("Integrity check failed during shutdown.")
        raise RuntimeError("Database integrity failed at shutdown.")

    # Dispose the engine BEFORE writing the checksum.
    #
    # WHY: engine.dispose() closes all pooled SQLite connections, which causes
    # SQLite to flush its final file-header write (change counter at offset 24).
    # If we write the checksum while connections are still open in the pool,
    # Python GC later calls engine.__del__ → dispose() → SQLite writes the
    # header → the file hash changes → checksum mismatch on the next startup.
    engine.dispose()

    write_db_checksum(str(Path(settings.database_path).expanduser().resolve()))
    logger.info("Safe shutdown complete. WAL checkpoint + integrity OK.")


def scan_backup_health(backup_dir: str) -> list[dict]:
    """
    Scan all .db backups and classify health.
    Returns list of dict results sorted by newest first.
    """

    results = []
    backup_path = Path(backup_dir)

    if not backup_path.exists():
        return results

    for db_file in sorted(
        backup_path.glob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        status = "HEALTHY"
        reason = ""

        try:
            backup_engine = create_engine(f"sqlite:///{db_file}")
            inspector = inspect(backup_engine)

            tables = inspector.get_table_names()

            required_tables = {"learning_units", "learning_sessions", "learning_progress"}

            if not required_tables.issubset(set(tables)):
                status = "STRUCTURE_INVALID"
                reason = "Missing required tables"

            else:
                with backup_engine.connect() as conn:
                    integrity = conn.execute(text("PRAGMA integrity_check;")).fetchone()

                    if not integrity or integrity[0] != "ok":
                        status = "CORRUPTED"
                        reason = "Integrity check failed"
                    else:
                        count = conn.execute(
                            text("SELECT COUNT(*) FROM learning_units;")
                        ).scalar()

                        if not count or count == 0:
                            status = "EMPTY"
                            reason = "No learning_units"
            backup_engine.dispose()
        except Exception as e:
            status = "CORRUPTED"
            reason = str(e)

        results.append(
            {
                "file": db_file.name,
                "status": status,
                "reason": reason,
                "modified": db_file.stat().st_mtime,
            }
        )

    return results


def prune_backups_keep_recent_valid(backup_dir: str | Path, keep: int = 5) -> tuple[int, int]:
    """
    Keep only the `keep` most recent valid (HEALTHY) .db backups and the `keep` most recent .json
    backups; delete the rest (and all invalid .db).
    Returns (kept_db_count, removed_count) where removed_count includes both db and json removed.
    """
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return (0, 0)

    removed_count = 0

    # Prune .db: keep only `keep` most recent HEALTHY
    results = scan_backup_health(str(backup_path))
    healthy = [r for r in results if r["status"] == "HEALTHY"]
    to_keep = {r["file"] for r in healthy[:keep]}
    for r in results:
        fname = r["file"]
        path = backup_path / fname
        if path.is_file() and fname not in to_keep:
            try:
                path.unlink()
                removed_count += 1
                logger.info("Pruned backup: %s (%s)", fname, r["status"])
            except OSError as e:
                logger.warning("Could not remove backup %s: %s", fname, e)
    kept_count = len(to_keep)
    if removed_count:
        logger.info("Backup pruning (.db): kept %d valid, removed %d", kept_count, removed_count)

    # Prune .json: keep only `keep` most recent by mtime
    json_files = sorted(
        backup_path.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    json_removed = 0
    for old in json_files[keep:]:
        try:
            old.unlink()
            json_removed += 1
            removed_count += 1
            logger.info("Pruned backup (json): %s", old.name)
        except OSError as e:
            logger.warning("Could not remove backup %s: %s", old.name, e)
    if json_removed:
        logger.info(
            "Backup pruning (.json): kept %d, removed %d",
            min(len(json_files), keep),
            json_removed,
        )
    return (kept_count, removed_count)


def auto_backup_sqlite_db(db_url: str) -> None:
    """Create a timestamped backup for absolute-path SQLite databases.

    Includes a sanity filter: refuses to back up a DB that is missing
    required tables, so that broken state is never propagated into backups.
    """
    if not db_url or not db_url.startswith("sqlite:////"):
        return

    try:
        parsed = urlparse(db_url)
        db_path = Path(parsed.path).expanduser().resolve()
        if not db_path.exists():
            return

        # -----------------------------------------------------------------
        # Backup sanity filter  (added after Feb 9 2026 incident)
        # WHY: A corrupt or empty DB should never overwrite healthy backups.
        # If required tables are missing we skip the backup entirely.
        # -----------------------------------------------------------------
        existing_tables = _get_existing_tables()
        missing = REQUIRED_TABLES - existing_tables
        if missing:
            logger.warning(
                "Auto-backup SKIPPED — database is missing required tables %s. "
                "Existing backups will NOT be overwritten.",
                sorted(missing),
            )
            return

        backups_dir = db_path.parent / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = db_path.suffix if db_path.suffix else ".db"
        backup_name = f"{db_path.stem}_auto_{timestamp}{suffix}"
        backup_path = backups_dir / backup_name
        if backup_path.exists():
            return

        shutil.copy2(db_path, backup_path)

        auto_backups = sorted(
            backups_dir.glob("vocabulary_auto_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        MAX_BACKUPS = 10
        removed_count = 0
        for old_file in auto_backups[MAX_BACKUPS:]:
            try:
                old_file.unlink()
                removed_count += 1
            except Exception:
                pass

        logger.info(
            "Auto-backup retention: kept %d, removed %d",
            min(len(auto_backups), MAX_BACKUPS),
            removed_count,
        )
    except Exception:
        logger.exception("Automatic SQLite backup failed")


def create_manual_backup(db_url: str | None = None) -> str | None:
    """
    Create a timestamped manual backup of the SQLite database (data + progress/scores).
    Same sanity checks as auto_backup_sqlite_db; uses filename vocabulary_manual_YYYYMMDD_HHMMSS.db.
    Returns the backup filename (e.g. vocabulary_manual_20260216_123456.db) on success, None on failure.
    """
    url = db_url or settings.database_url
    if not url or not url.startswith("sqlite:///"):
        return None
    try:
        parsed = urlparse(url)
        db_path = Path(parsed.path).expanduser().resolve()
        if not db_path.exists():
            return None
        existing_tables = _get_existing_tables()
        missing = REQUIRED_TABLES - existing_tables
        if missing:
            logger.warning(
                "Manual backup SKIPPED — database is missing required tables %s.",
                sorted(missing),
            )
            return None
        backups_dir = db_path.parent / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = db_path.suffix if db_path.suffix else ".db"
        backup_name = f"{db_path.stem}_manual_{timestamp}{suffix}"
        backup_path = backups_dir / backup_name
        shutil.copy2(db_path, backup_path)
        logger.info("Manual backup created: %s", backup_path)
        return backup_name
    except Exception:
        logger.exception("Manual backup failed")
        return None


def create_tables() -> None:
    """
    Create all database tables.
    
    DEPRECATED: Schema is now managed by Alembic migrations only.
    This function is kept for backward compatibility but should not be called.
    Run 'alembic upgrade head' to manage database schema.
    """
    # Schema managed by Alembic migrations only
    # Do not use Base.metadata.create_all() in production
    pass


def backfill_next_review_at() -> int:
    """
    Backfill next_review_at for existing LearningProgress rows where it is NULL.
    
    Uses current effective_confidence to compute the next review time.
    This is a one-time, safe operation that preserves existing data.
    
    Returns:
        Number of rows updated.
    """
    # Import here to avoid circular imports
    from app.models.learning_unit import LearningProgress
    from app.services.session_service import (
        compute_effective_confidence,
        compute_next_review_at,
    )
    
    db = SessionLocal()
    try:
        # Find all progress records with NULL next_review_at
        rows_to_update = (
            db.query(LearningProgress)
            .filter(LearningProgress.next_review_at.is_(None))
            .all()
        )
        
        if not rows_to_update:
            logger.info("No LearningProgress rows need next_review_at backfill")
            return 0
        
        now = utc_now()
        updated_count = 0
        
        for progress in rows_to_update:
            # Compute effective confidence with time decay
            effective_conf = compute_effective_confidence(
                progress.confidence_score,
                progress.last_seen,
                now,
            )
            
            # Compute next review time
            progress.next_review_at = compute_next_review_at(effective_conf, now)
            updated_count += 1
        
        db.commit()
        logger.info(f"Backfilled next_review_at for {updated_count} LearningProgress rows")
        return updated_count
        
    except Exception as exc:
        # Table may not exist yet (fresh DB before migrations)
        logger.warning(f"backfill_next_review_at skipped: {exc}")
        db.rollback()
        return 0
    finally:
        db.close()
