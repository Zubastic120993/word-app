"""Application configuration settings."""

import logging
import os
from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings with sensible defaults for local-first operation."""
    
    # Application info
    app_name: str = "Word App"
    app_version: str = "0.2.0"
    
    # Paths
    base_dir: Path = Path(__file__).parent.parent
    data_dir: Path = base_dir / "data"
    database_path: str = str(base_dir / "data" / "vocabulary.db")
    allow_database_path_override: bool = Field(
        False, validation_alias=AliasChoices("ALLOW_DB_PATH_OVERRIDE", "ALLOW_DATABASE_PATH_OVERRIDE")
    )

    # Database — NOTE: always derived from database_path by the model_validator below.
    # Do NOT set database_url directly; set database_path instead.
    # Keeping a settable field (not a @property) so Pydantic env-var override of
    # WORD_APP_DATABASE_URL still works (it will be overridden by the validator only
    # when database_path differs from the default, matching the intent).
    database_url: str = f"sqlite:///{database_path}"
    
    # Language settings
    source_language: str = "Polish"
    target_language: str = "English"
    
    # PDF parsing delimiters (in order of priority)
    # Includes: em dash (—), en dash (–), hyphen (-), and colon (:)
    pdf_delimiters: list[str] = [" — ", " – ", " - ", "—", "–", "-", ":"]
    
    # Learning session
    session_size: int = 50
    max_new_per_day: int = 60
    
    # AI Settings
    ai_provider: str = "ollama"  # "ollama" or "openai"
    
    # Ollama settings (primary, local-first)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    ollama_timeout: int = 60
    
    # OpenAI settings (optional, disabled by default)
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_enabled: bool = False
    
    # Vocabulary validation settings
    vocab_validation_enabled: bool = True  # Enable AI validation during upload
    vocab_validation_batch_size: int = 30  # Units to validate per API call (larger = faster)
    
    # Study Mode settings
    study_mode_strict: bool = True
    study_mode_include_learned: bool = True  # Include previously learned units
    study_mode_confidence_threshold: float = 0.7  # Units above this are "learned"
    
    # Murf TTS settings (audio pronunciation)
    murf_enabled: bool = False  # Disabled by default
    murf_api_key: Optional[str] = None
    murf_voice: str = "en-US-marcus"  # Default Murf voice
    murf_language: str = "en-US"  # Default language code
    
    # ElevenLabs TTS settings (audio pronunciation for Polish)
    elevenlabs_enabled: bool = False  # Disabled by default
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_model: str = "eleven_multilingual_v2"  # Default ElevenLabs model
    elevenlabs_voice_pl: Optional[str] = None  # Voice ID for Polish (required when enabled)
    
    # Audio storage path (relative to project root)
    audio_dir: Path = base_dir / "data" / "audio"
    
    # Server binding (used by main.py entry point)
    # Default: localhost only; set WORD_APP_HOST=0.0.0.0 for LAN access
    host: str = "127.0.0.1"
    port: int = 8000

    # iPad client access (learning-only, via Safari over LAN)
    # When enabled, requests identified as iPad get a restricted learner role.
    # When disabled (default), all iPad-role indicators are ignored → role = "mac".
    ipad_enabled: bool = False

    # Development/debug settings
    debug: bool = False  # Enable debug mode
    env: Optional[str] = None  # Environment name (e.g., "development", "production")
    
    # Startup maintenance controls (defaults preserve current behavior)
    # Explicit env mapping:
    # - WORD_APP_STARTUP_BACKUP
    # - WORD_APP_STARTUP_BACKFILL
    # - WORD_APP_STARTUP_RELINK_AUDIO
    startup_backup_enabled: bool = Field(True, validation_alias="STARTUP_BACKUP")
    startup_backfill_enabled: bool = Field(True, validation_alias="STARTUP_BACKFILL")
    startup_relink_audio_enabled: bool = Field(True, validation_alias="STARTUP_RELINK_AUDIO")

    # CORS (local-first, browser-correct)
    # NOTE: Must be explicit origins when allow_credentials=True (no wildcard "*")
    cors_allow_origins: list[str] = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ]

    # Audio cleanup settings
    audio_cleanup_on_startup: bool = False  # Run cleanup on startup (safe mode, disabled by default)

    # SRS debug tracing (development-only; disabled by default)
    srs_debug: bool = Field(False, validation_alias="SRS_DEBUG")

    # Due-load smoothing: when scheduling next_review_at, cap how many words are
    # due on the same calendar day so future days stay roughly even.
    smooth_due_load: bool = Field(True, validation_alias="SMOOTH_DUE_LOAD")
    max_due_per_day: int = Field(350, validation_alias="MAX_DUE_PER_DAY", ge=1, le=2000)

    # When overdue count exceeds this, automatically spread those reviews across
    # the next days (so learning doesn't degrade under a huge backlog).
    spread_overdue_when_above: int = Field(
        400, validation_alias="SPREAD_OVERDUE_WHEN_ABOVE", ge=1, le=10000
    )

    # Runtime-gated recall controller mode.
    recall_controller_mode: str = Field(
        "legacy_adaptive",
        validation_alias=AliasChoices("RECALL_CONTROLLER_MODE", "WORD_APP_RECALL_CONTROLLER_MODE"),
    )

    model_config = {"env_prefix": "WORD_APP_"}

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def parse_cors_allow_origins(cls, v):
        """
        Allow override via WORD_APP_CORS_ALLOW_ORIGINS as a comma-separated list.

        Example:
            WORD_APP_CORS_ALLOW_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
        """
        if v is None:
            return v
        if isinstance(v, str):
            items = [s.strip() for s in v.split(",")]
            items = [s for s in items if s]
            return items
        return v

    @model_validator(mode="after")
    def derive_database_url(self):
        """Ensure database_url is always consistent with database_path.

        WHY: database_url has a static f-string default computed at class-body
        evaluation time.  If database_path is overridden (e.g. via env var
        WORD_APP_DATABASE_PATH), database_url would still point to the old path,
        causing SQLite to silently create a fresh empty DB at the URL path while
        the path-existence check passes on the old file.  This validator keeps
        them in sync unconditionally.

        ALSO: Forces the path to an absolute, resolved form so that the URL is
        stable regardless of the process working directory.  A relative path
        (e.g. "data/vocabulary.db") would resolve differently depending on where
        `python main.py` vs `uvicorn app.main:app` is launched from.
        """
        _base_dir = Path(__file__).resolve().parent.parent
        _default_db_path = (_base_dir / "data" / "vocabulary.db").resolve()

        db_path = Path(self.database_path)
        if not db_path.is_absolute():
            db_path = _base_dir / db_path
        db_path = db_path.resolve()

        # Production guard: refuse unexpected DB path overrides unless explicitly allowed.
        # This prevents silently pointing production at a new/empty SQLite file.
        if (self.env or "").lower() in {"prod", "production"}:
            if db_path != _default_db_path and not self.allow_database_path_override:
                raise ValueError(
                    "Refusing to start in production with an overridden database path. "
                    f"Effective DB path would be: {db_path} (default: {_default_db_path}). "
                    "Set WORD_APP_ALLOW_DB_PATH_OVERRIDE=true to allow this intentionally."
                )

        self.database_path = str(db_path)
        self.database_url = f"sqlite:///{db_path}"
        return self

    @model_validator(mode="after")
    def validate_cors_settings(self):
        # Safety: never allow wildcard when credentials are enabled
        if any(origin.strip() == "*" for origin in (self.cors_allow_origins or [])):
            raise ValueError(
                "Invalid WORD_APP_CORS_ALLOW_ORIGINS: wildcard '*' is not allowed. "
                "Use explicit origins (e.g., http://localhost:8000)."
            )
        return self
    
    @model_validator(mode='after')
    def validate_elevenlabs_voice_pl(self):
        """Validate that elevenlabs_voice_pl is in the registry if ElevenLabs is enabled."""
        if self.elevenlabs_enabled and self.elevenlabs_voice_pl:
            # Inline validation to avoid circular import with app.services.__init__
            # These must match the registry in app.services.audio.elevenlabs_voices
            ALLOWED_POLISH_VOICE_IDS = {
                "H5xTcsAIeS5RAykjz57a": "Alex – Professional Narration",
                "zzBTsLBFM6AOJtkr1e9b": "Paweł Pro",
                "Sgu2YCTorC0ao3q8kFyk": "Marek – Neutral",
            }
            
            if self.elevenlabs_voice_pl not in ALLOWED_POLISH_VOICE_IDS:
                available_ids = ", ".join([
                    f"{voice_id} ({display_name})" 
                    for voice_id, display_name in ALLOWED_POLISH_VOICE_IDS.items()
                ])
                raise ValueError(
                    f"Invalid WORD_APP_ELEVENLABS_VOICE_PL='{self.elevenlabs_voice_pl}'. "
                    f"Voice ID must be one of the registered Polish voices: {available_ids}"
                )
        return self

    @field_validator("recall_controller_mode")
    @classmethod
    def validate_recall_controller_mode(cls, value: str) -> str:
        allowed = {"legacy_adaptive", "observe_only", "v3_experimental"}
        if value not in allowed:
            raise ValueError(
                "Invalid RECALL_CONTROLLER_MODE. "
                "Expected one of: legacy_adaptive, observe_only, v3_experimental."
            )
        return value


# Global settings instance
settings = Settings()
logger.info("Using database: %s", settings.database_path)

STRICT_VOCAB_INTEGRITY = os.getenv(
    "WORD_APP_STRICT_VOCAB_INTEGRITY", "false"
).lower() == "true"


def ensure_data_dir() -> None:
    """Ensure the data directory exists."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
