from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from sqlalchemy import text

from app.database import (
    CHECKSUM_SUFFIX,
    _get_existing_tables,
    compute_db_checksum,
    verify_db_checksum,
)


CHECKSUM_OK = "OK"
CHECKSUM_MISSING = "MISSING"
CHECKSUM_MISMATCH = "MISMATCH"

DB_STATE_VALID = "VALID"
DB_STATE_INTEGRITY_ERROR = "INTEGRITY_ERROR"
DB_STATE_MISSING_TABLES = "MISSING_REQUIRED_TABLES"
DB_STATE_CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
DB_STATE_SCHEMA_DRIFT = "SCHEMA_DRIFT"
DB_STATE_FRESH = "FRESH"
DB_STATE_PARTIAL = "PARTIAL"


@dataclass
class DBVerificationResult:
    revision: Optional[str]
    tables: Set[str]
    integrity_ok: bool
    checksum_status: str
    missing_required_tables: Set[str]
    state: str


def classify_database_state(result: DBVerificationResult, required_tables: set[str]) -> str:
    application_tables = result.tables & required_tables

    if not result.integrity_ok:
        return DB_STATE_INTEGRITY_ERROR

    if result.checksum_status == CHECKSUM_MISMATCH:
        return DB_STATE_CHECKSUM_MISMATCH

    if result.revision is not None and result.missing_required_tables:
        return DB_STATE_SCHEMA_DRIFT

    if application_tables and result.missing_required_tables:
        return DB_STATE_PARTIAL

    if result.revision is None and not application_tables:
        return DB_STATE_FRESH

    if result.missing_required_tables:
        return DB_STATE_MISSING_TABLES

    return DB_STATE_VALID


def verify_database(engine, db_path: str, required_tables: set[str]) -> DBVerificationResult:
    tables = _get_existing_tables(engine)

    revision = None
    if "alembic_version" in tables:
        with engine.connect() as conn:
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()

    with engine.connect() as conn:
        integrity_result = conn.execute(text("PRAGMA integrity_check")).fetchone()
    integrity_ok = bool(integrity_result and integrity_result[0] == "ok")

    missing_required_tables = required_tables - tables

    checksum_path = Path(db_path + CHECKSUM_SUFFIX)
    checksum_status = CHECKSUM_MISSING
    if checksum_path.exists():
        try:
            compute_db_checksum(db_path)
            verify_db_checksum(db_path)
            checksum_status = CHECKSUM_OK
        except Exception:
            checksum_status = CHECKSUM_MISMATCH

    classified_state = classify_database_state(
        DBVerificationResult(
            revision=revision,
            tables=tables,
            integrity_ok=integrity_ok,
            checksum_status=checksum_status,
            missing_required_tables=missing_required_tables,
            state="",
        ),
        required_tables,
    )

    return DBVerificationResult(
        revision=revision,
        tables=tables,
        integrity_ok=integrity_ok,
        checksum_status=checksum_status,
        missing_required_tables=missing_required_tables,
        state=classified_state,
    )
