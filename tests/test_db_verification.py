import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db_verification import (
    CHECKSUM_MISMATCH,
    CHECKSUM_MISSING,
    CHECKSUM_OK,
    verify_database,
)
from app.database import CHECKSUM_SUFFIX, compute_db_checksum


REQUIRED_TABLES = {"learning_units", "learning_sessions", "learning_progress"}


def create_sqlite_engine(db_path):
    return create_engine(f"sqlite:///{db_path}")


def create_empty_db(db_path: Path) -> None:
    sqlite3.connect(db_path).close()


def write_checksum_file(db_path: Path) -> None:
    checksum = compute_db_checksum(str(db_path))
    db_path.with_name(db_path.name + CHECKSUM_SUFFIX).write_text(checksum)


def test_fresh_db(tmp_path):
    db_path = tmp_path / "fresh.db"
    create_empty_db(db_path)
    engine = create_sqlite_engine(db_path)

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.revision is None
    assert verification.tables == set()
    assert verification.missing_required_tables == REQUIRED_TABLES
    assert verification.checksum_status == CHECKSUM_MISSING


def test_db_with_required_tables_present(tmp_path):
    db_path = tmp_path / "required_tables.db"
    engine = create_sqlite_engine(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_sessions (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_progress (id INTEGER)"))

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.missing_required_tables == set()
    assert verification.integrity_ok is True


def test_alembic_version_only_schema_drift(tmp_path):
    db_path = tmp_path / "schema_drift.db"
    engine = create_sqlite_engine(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('abc123')"))

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.revision == "abc123"
    assert verification.missing_required_tables
    assert "alembic_version" in verification.tables


def test_partial_schema(tmp_path):
    db_path = tmp_path / "partial.db"
    engine = create_sqlite_engine(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.missing_required_tables == {
        "learning_sessions",
        "learning_progress",
    }


def test_checksum_missing(tmp_path):
    db_path = tmp_path / "checksum_missing.db"
    engine = create_sqlite_engine(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_sessions (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_progress (id INTEGER)"))

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.checksum_status == CHECKSUM_MISSING


def test_checksum_ok(tmp_path):
    db_path = tmp_path / "checksum_ok.db"
    engine = create_sqlite_engine(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_sessions (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_progress (id INTEGER)"))

    write_checksum_file(db_path)

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.checksum_status == CHECKSUM_OK


def test_checksum_mismatch(tmp_path):
    db_path = tmp_path / "checksum_mismatch.db"
    engine = create_sqlite_engine(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_sessions (id INTEGER)"))
        conn.execute(text("CREATE TABLE learning_progress (id INTEGER)"))

    write_checksum_file(db_path)

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE extra_table (id INTEGER)"))

    verification = verify_database(engine, str(db_path), REQUIRED_TABLES)

    assert verification.checksum_status == CHECKSUM_MISMATCH
