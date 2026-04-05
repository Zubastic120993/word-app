import os
import pytest
from pathlib import Path
from sqlalchemy import create_engine, text

from app.database import (
    verify_required_tables,
    verify_sqlite_integrity,
    compute_db_checksum,
    write_db_checksum,
    verify_db_checksum,
    scan_backup_health,
)

# ------------------------------------------------------------
# 1. Corrupted DB file test
# ------------------------------------------------------------

def test_corrupted_db_detected(tmp_path, monkeypatch):
    db_path = tmp_path / "corrupted.db"
    db_path.write_bytes(b"not a real sqlite file")

    engine = create_engine(f"sqlite:///{db_path}")

    with pytest.raises(Exception):
        with engine.connect() as conn:
            conn.execute(text("PRAGMA integrity_check;")).fetchone()

# ------------------------------------------------------------
# 2. Checksum mismatch detection
# ------------------------------------------------------------

def test_checksum_mismatch_detected(tmp_path):
    db_path = tmp_path / "test.db"

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE learning_units (id INTEGER);"))

    write_db_checksum(str(db_path))

    # Modify DB after checksum
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE x (id INTEGER);"))

    with pytest.raises(RuntimeError):
        verify_db_checksum(str(db_path))

# ------------------------------------------------------------
# 3. Backup scanner classifies corrupted file
# ------------------------------------------------------------

def test_backup_scanner_detects_corruption(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    bad_db = backup_dir / "bad.db"
    bad_db.write_bytes(b"corrupt")

    results = scan_backup_health(str(backup_dir))

    assert results[0]["status"] in ("CORRUPTED", "STRUCTURE_INVALID")


def test_partial_wal_copy_detected(tmp_path):
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "wal_test.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # Open connection and begin explicit transaction
    conn = engine.connect()
    trans = conn.begin()

    conn.execute(text("PRAGMA journal_mode=WAL;"))
    conn.execute(text("CREATE TABLE learning_units (id INTEGER);"))
    conn.execute(text("INSERT INTO learning_units VALUES (1);"))

    # DO NOT commit
    # Copy only main DB file while transaction open
    copied_path = tmp_path / "copied.db"
    copied_path.write_bytes(db_path.read_bytes())

    # Now simulate crash by closing connection without commit
    trans.rollback()
    conn.close()

    bad_engine = create_engine(f"sqlite:///{copied_path}")

    # Integrity should fail or missing table
    with bad_engine.connect() as bad_conn:
        result = bad_conn.execute(text("PRAGMA integrity_check;")).fetchone()

    # We accept either:
    # - integrity != ok
    # - or learning_units table missing
    if result and result[0] == "ok":
        with bad_engine.connect() as check_conn:
            tables = check_conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table';")
            ).fetchall()
        assert ("learning_units",) not in tables
