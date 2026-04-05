"""FastAPI application for vocabulary learning."""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# ===================
# .env loading — MUST happen before ANY app.* import so that pydantic-settings
# picks up env vars from .env when it constructs `settings = Settings()`.
# ===================
def _load_dotenv() -> bool:
    """Attempt to load .env file if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            return True
    except ImportError:
        pass  # python-dotenv not installed, skip silently
    return False

_dotenv_loaded = _load_dotenv()

# All app.* imports come AFTER .env is loaded so Settings() reads the correct env.
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.dependencies import get_client_role
from app.config import STRICT_VOCAB_INTEGRITY, settings
from app.database import (
    REQUIRED_TABLES,
    auto_backup_sqlite_db,
    backfill_next_review_at,
    engine,
    get_db,
    perform_safe_shutdown,
    prune_backups_keep_recent_valid,
    scan_backup_health,
)
from app.db_verification import (
    CHECKSUM_MISMATCH,
    CHECKSUM_MISSING,
    verify_database,
)
from app.routers.upload import router as upload_router
from app.routers.sessions import router as sessions_router
from app.routers.ai import router as ai_router
from app.routers.data import router as data_router
from app.routers.ui import router as ui_router
from app.routers.audio import router as audio_router
from app.routers.progress import router as progress_router
from app.routers import themes
from app.routers import analytics

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DB_STATE_FILE_VERSION = 1


def _now_iso_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_startup_state(state_path: Path) -> dict | None:
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning(
            "DB startup state file unreadable; ignoring: %s",
            state_path,
            exc_info=True,
        )
        return None


def _save_startup_state_atomic(state_path: Path, payload: dict) -> None:
    """
    Atomic state write (POSIX): write tmp then replace.
    Prevents partial/corrupt JSON if the process crashes mid-write.
    """
    try:
        tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(state_path)
    except Exception:
        logger.warning(
            "Failed to write DB startup state file: %s",
            state_path,
            exc_info=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.
    
    Creates database tables on startup.
    """
    logger.info("Starting Word App...")

    # ---------------------------------------------------------------
    # LOG DB PATH IMMEDIATELY — before anything touches the database.
    # This makes it trivial to confirm which file the app is using.
    # ---------------------------------------------------------------
    from urllib.parse import urlparse as _urlparse
    _parsed_db = _urlparse(settings.database_url)
    _db_file = Path(_parsed_db.path).expanduser().resolve()
    _db_state_file = _db_file.with_suffix(".db.startup_state.json")
    logger.info("DATABASE_URL  = %s", settings.database_url)
    logger.info("DB file path  = %s", _db_file)
    logger.info("DB file exists= %s", _db_file.exists())
    logger.info("DB state file = %s", _db_state_file)
    if not _db_file.exists():
        raise RuntimeError(
            f"DB FILE NOT FOUND at startup: {_db_file}\n"
            "Restore a backup before starting the application.\n"
            "Run: python -m app.tools.recover_db"
        )

    # Log .env status
    if _dotenv_loaded:
        logger.info("Loaded .env file for local development.")
    
    # Log AI configuration
    if settings.openai_enabled:
        logger.info(f"OpenAI enabled (model: {settings.openai_model})")
    else:
        logger.info("OpenAI disabled (using Ollama or no AI)")
    
    # Log Murf TTS configuration
    if settings.murf_enabled:
        logger.info(f"Murf TTS enabled (voice: {settings.murf_voice})")
    else:
        logger.info("Murf TTS disabled")

    # Log network access mode
    if settings.host == "0.0.0.0":
        logger.info("LAN access enabled — server bound to all interfaces (0.0.0.0)")
    else:
        logger.info(f"Local-only access — server bound to {settings.host}")

    # Log iPad client access
    if settings.ipad_enabled:
        logger.info("iPad client access enabled (WORD_APP_IPAD_ENABLED=true)")
    else:
        logger.info("iPad client access disabled")

    # Log CORS configuration
    logger.info(f"CORS enabled for origins: {settings.cors_allow_origins}")
    
    # Schema is managed by Alembic migrations only
    # Developer should manually run: alembic upgrade head
    # create_tables() is disabled to prevent schema recreation on app updates
    logger.info("Database schema managed by Alembic migrations.")

    # ---------------------------------------------------------------
    # STARTUP SAFETY GUARD  (added after Feb 9 2026 incident)
    # WHY: Verify the DB has all required tables BEFORE any
    # maintenance task touches it.  If the schema is incomplete the
    # process exits immediately — this prevents backfill, relink,
    # and backup from running against a broken DB.
    # Skipped when WORD_APP_TESTING=1 so pytest can use TestClient
    # with an overridden get_db (in-memory DB) without touching real DB.
    # ---------------------------------------------------------------
    if os.environ.get("WORD_APP_TESTING") != "1":
        # Cross-run change detection state (loaded once per startup)
        prev_state = _load_startup_state(_db_state_file)
        if prev_state is None:
            logger.info("No previous DB state file — treating as first run")

        current_instance_id: str | None = None
        current_revision: str | None = None
        pre_fingerprint: str | None = None
        db_path: str = str(_db_file)
        try:
            # Single source of truth: the parsed SQLite file path from the URL.
            verification = verify_database(engine, db_path, REQUIRED_TABLES)
            current_revision = verification.revision
            logger.info("DB revision: %s", verification.revision)
            logger.info("Detected tables: %s", sorted(verification.tables))
            logger.info("Checksum status: %s", verification.checksum_status)
            checksum_mismatch = verification.checksum_status == CHECKSUM_MISMATCH

            if verification.revision is not None and verification.missing_required_tables:
                logger.error(
                    "Schema drift detected: revision=%s missing_tables=%s",
                    verification.revision,
                    sorted(verification.missing_required_tables),
                )

            if not verification.integrity_ok:
                logger.error("SQLite integrity check failed.")
                raise RuntimeError("SQLite integrity check failed.")

            if verification.missing_required_tables:
                raise RuntimeError(
                    f"""
Database schema invalid.

Missing tables: {sorted(verification.missing_required_tables)}

You can fix this in two ways:

1) Run migrations:
   alembic upgrade head

2) Restore the newest backup automatically:
   python -m app.tools.recover_db
"""
                )

            if verification.checksum_status == CHECKSUM_MISSING:
                logger.warning("No checksum file found. Skipping verification.")

            # Fast tripwire for the classic catastrophic state:
            # the DB exists and even has alembic_version, but none of the app tables.
            # (e.g. a blank file got stamped "at head".)
            if verification.tables == {"alembic_version"}:
                raise RuntimeError(
                    "Database is effectively empty (only 'alembic_version' table found). "
                    "Refusing to start — this usually means the wrong DB file is being used "
                    "or migrations did not run."
                )

            from sqlalchemy import func
            from app.models.learning_unit import LearningUnit
            from app.models.learning_unit import Settings as DbSettings
            from app.database import SessionLocal, compute_db_checksum

            db = SessionLocal()
            try:
                # DB identity marker: create once, then log every startup.
                row = db.query(DbSettings).order_by(DbSettings.id.asc()).first()
                if row is None:
                    row = DbSettings()
                    db.add(row)
                    db.commit()
                    db.refresh(row)
                if not row.db_instance_id:
                    import uuid

                    row.db_instance_id = str(uuid.uuid4())
                    db.add(row)
                    db.commit()
                current_instance_id = row.db_instance_id
                logger.info("DB instance id: %s", current_instance_id)

                if checksum_mismatch:
                    known_instance = (
                        prev_state is not None
                        and prev_state.get("instance_id")
                        and prev_state.get("instance_id") == current_instance_id
                    )
                    if known_instance:
                        logger.warning(
                            "Database checksum mismatch detected for known DB instance %s. "
                            "Integrity/schema checks passed, so startup will continue and the checksum "
                            "will be refreshed on the next clean shutdown.",
                            current_instance_id,
                        )
                    else:
                        logger.error("Database checksum mismatch detected.")
                        raise RuntimeError("Database checksum mismatch.")

                # -----------------------------------------------------------
                # Cross-run change detection (pre-maintenance fingerprint)
                # Compare prev.post_fingerprint -> current.pre_fingerprint.
                # -----------------------------------------------------------
                pre_fingerprint = compute_db_checksum(db_path)[:8]
                logger.info("DB pre-fingerprint: %s", pre_fingerprint)

                if prev_state:
                    prev_instance_id = prev_state.get("instance_id")
                    prev_revision = prev_state.get("revision")
                    prev_post = prev_state.get("post_fingerprint")

                    if prev_instance_id and prev_instance_id != current_instance_id:
                        logger.warning(
                            "DB instance changed since last startup: %s -> %s",
                            prev_instance_id,
                            current_instance_id,
                        )

                    if prev_post and prev_post != pre_fingerprint:
                        if prev_revision and current_revision and prev_revision != current_revision:
                            logger.info(
                                "DB revision changed since last startup (%s -> %s) — content change expected",
                                prev_revision,
                                current_revision,
                            )
                        else:
                            logger.warning(
                                "DB content changed since last run: %s -> %s",
                                prev_post,
                                pre_fingerprint,
                            )

                null_count = db.query(func.count(LearningUnit.id))\
                    .filter(LearningUnit.vocabulary_id.is_(None))\
                    .scalar() or 0

                if null_count > 0:
                    logger.error(
                        "Vocabulary integrity warning: %d learning_units have NULL vocabulary_id.",
                        null_count
                    )

                    if STRICT_VOCAB_INTEGRITY:
                        logger.error("Strict vocabulary integrity enabled — aborting startup.")
                        raise RuntimeError(
                            "Vocabulary integrity violation: NULL vocabulary_id detected."
                        )
                else:
                    logger.info("Vocabulary integrity check passed (no NULL vocabulary_id).")

            finally:
                db.close()
            logger.info("Database integrity check passed.")
        except RuntimeError as exc:
            _backup_dir = str(Path(settings.database_path).resolve().parent / "backups")
            backups = scan_backup_health(_backup_dir)
            healthy = [b for b in backups if b["status"] == "HEALTHY"]
            manual = [b for b in healthy if b["file"].startswith("vocabulary_manual")]
            auto = [b for b in healthy if b["file"].startswith("vocabulary_auto")]

            logger.error(
                "Backup health scan: total=%d healthy=%d",
                len(backups),
                len(healthy),
            )
            if manual:
                logger.error("Latest healthy manual backup: %s", manual[0]["file"])
            else:
                logger.error("Latest healthy manual backup: none")
            if auto:
                logger.error("Latest healthy auto backup: %s", auto[0]["file"])
            else:
                logger.error("Latest healthy auto backup: none")
            logger.error("Startup aborted: %s", exc)
            raise
        allow_empty_db = os.environ.get("WORD_APP_ALLOW_EMPTY_DB", "").lower() == "true"
        if not allow_empty_db:
            from app.database import SessionLocal

            db = SessionLocal()
            try:
                learning_units_count = db.execute(
                    text("SELECT COUNT(*) FROM learning_units")
                ).scalar() or 0
            finally:
                db.close()

            if learning_units_count == 0:
                logger.error(
                    "Startup aborted: empty database detected (learning_units=0). "
                    "Refusing to start. To override intentionally, set "
                    "WORD_APP_ALLOW_EMPTY_DB=true."
                )
                raise RuntimeError("Empty database detected. Refusing to start.")

    # Auto-backup SQLite DB before any startup DB activity
    if settings.startup_backup_enabled:
        logger.info("Startup maintenance: SQLite auto-backup enabled")
        auto_backup_sqlite_db(settings.database_url)
        backup_dir = Path(settings.database_path).resolve().parent / "backups"
        prune_backups_keep_recent_valid(backup_dir, keep=5)
    else:
        logger.info("Startup maintenance: SQLite auto-backup skipped (WORD_APP_STARTUP_BACKUP=false)")
    
    # Backfill next_review_at for existing data
    if settings.startup_backfill_enabled:
        logger.info("Startup maintenance: backfill next_review_at enabled")
        backfilled = backfill_next_review_at()
        if backfilled > 0:
            logger.info(f"Backfilled next_review_at for {backfilled} existing records.")
    else:
        logger.info("Startup maintenance: backfill next_review_at skipped (WORD_APP_STARTUP_BACKFILL=false)")

    # Best-effort audio relinking (reuse existing global audio files)
    if settings.startup_relink_audio_enabled:
        logger.info("Startup maintenance: audio relinking enabled")
        try:
            from app.database import SessionLocal
            from app.services.audio.audio_relink_service import relink_existing_audio_assets

            db = SessionLocal()
            try:
                relink_existing_audio_assets(db)
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Startup audio relinking failed: {e}", exc_info=True)
    else:
        logger.info("Startup maintenance: audio relinking skipped (WORD_APP_STARTUP_RELINK_AUDIO=false)")
    
    # Optional audio cleanup on startup (safe mode, disabled by default)
    if settings.audio_cleanup_on_startup:
        logger.info("Running audio cleanup on startup (WORD_APP_AUDIO_CLEANUP_ON_STARTUP=true)")
        from app.database import SessionLocal
        from app.services.audio.audio_cleanup_service import cleanup_orphaned_audio_files
        
        db = SessionLocal()
        try:
            result = cleanup_orphaned_audio_files(db)
            if result["files_deleted"] > 0:
                logger.info(
                    f"Startup cleanup: deleted {result['files_deleted']} orphaned audio files, "
                    f"freed {result['bytes_freed']} bytes"
                )
            else:
                logger.info("Startup cleanup: no orphaned audio files found")
        except Exception as e:
            logger.error(f"Startup audio cleanup failed: {e}", exc_info=True)
        finally:
            db.close()
    
    # -----------------------------------------------------------
    # Cross-run change detection (post-maintenance fingerprint)
    # Stored AFTER startup maintenance so next run can compare:
    # prev.post_fingerprint -> current.pre_fingerprint.
    # -----------------------------------------------------------
    if os.environ.get("WORD_APP_TESTING") != "1":
        try:
            from app.database import compute_db_checksum

            post_fingerprint = compute_db_checksum(db_path)[:8]
            logger.info("DB post-fingerprint: %s", post_fingerprint)
            _save_startup_state_atomic(
                _db_state_file,
                {
                    "version": DB_STATE_FILE_VERSION,
                    "instance_id": current_instance_id,
                    "revision": current_revision,
                    "pre_fingerprint": pre_fingerprint,
                    "post_fingerprint": post_fingerprint,
                    "timestamp": _now_iso_z(),
                },
            )
        except Exception:
            logger.warning("Failed to persist DB startup state", exc_info=True)

    yield
    perform_safe_shutdown()
    logger.info("Shutting down Word App...")


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Local-first vocabulary learning application with PDF import.",
    lifespan=lifespan,
)

# Middleware to disable caching for development
class NoCacheMiddleware(BaseHTTPMiddleware):
    """Disable browser caching for static files during development."""
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


class ClientRoleMiddleware(BaseHTTPMiddleware):
    """
    Set ``request.state.client_role`` ("mac" | "ipad") on every request.

    Templates access this via ``{{ request.state.client_role }}`` so the
    navigation bar can hide admin-only items for iPad clients without
    relying on CSS-only hiding.
    """

    async def dispatch(self, request: Request, call_next):
        request.state.client_role = get_client_role(request)
        return await call_next(request)


app.add_middleware(NoCacheMiddleware)
app.add_middleware(ClientRoleMiddleware)

# Add CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include API routers
app.include_router(upload_router)
app.include_router(sessions_router)
app.include_router(ai_router)
app.include_router(data_router)
app.include_router(audio_router)
app.include_router(progress_router)
app.include_router(themes.router)
app.include_router(analytics.router)
app.include_router(analytics.api_router)

# Include UI router (must be last to avoid conflicts)
app.include_router(ui_router)


@app.get("/api")
def api_root():
    """API root endpoint with basic app info."""
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "source_language": settings.source_language,
        "target_language": settings.target_language,
    }


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """
    Health check endpoint.

    Reports:
    - DB connectivity (fast SELECT 1)
    - Alembic schema state (current vs head), without running migrations
    """
    database_ok = True
    database_error: str | None = None

    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        database_ok = False
        database_error = str(e)

    # Alembic revision info (best-effort, no side effects)
    alembic_current: str | None = None
    alembic_head: str | None = None
    alembic_at_head = False

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext

        alembic_ini_path = Path(__file__).parent.parent / "alembic.ini"
        cfg = Config(str(alembic_ini_path))

        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        if heads:
            alembic_head = heads[0] if len(heads) == 1 else ",".join(heads)

        # If DB is reachable, also determine current revision
        if database_ok:
            conn = db.connection()
            ctx = MigrationContext.configure(conn)
            alembic_current = ctx.get_current_revision()
            alembic_at_head = alembic_current in heads if alembic_current else False
    except Exception:
        # Health endpoint must stay quiet; return best-effort info only.
        pass

    return {
        "status": "healthy" if database_ok else "unhealthy",
        "database": {
            "ok": database_ok,
            **({"error": database_error} if database_error else {}),
        },
        "alembic": {
            "current": alembic_current,
            "head": alembic_head,
            "at_head": alembic_at_head,
        },
    }
