import os
import subprocess
import sys
from pathlib import Path

from app.config import settings
from app.database import REQUIRED_TABLES, engine
from app.db_verification import (
    DB_STATE_FRESH,
    DB_STATE_INTEGRITY_ERROR,
    DB_STATE_PARTIAL,
    DB_STATE_SCHEMA_DRIFT,
    DB_STATE_VALID,
    verify_database,
)


def main() -> None:
    db_path = str(Path(settings.database_path).expanduser().resolve())
    verification = verify_database(engine, db_path, REQUIRED_TABLES)

    integrity = "OK" if verification.integrity_ok else "FAILED"
    tables = ", ".join(sorted(verification.tables))

    print("Database repair analysis")
    print("------------------------")
    print(f"Path: {db_path}")
    print(f"Revision: {verification.revision}")
    print(f"Integrity: {integrity}")
    print(f"Checksum: {verification.checksum_status}")
    print(f"Tables: {tables}")
    print()
    print(f"Detected state: {verification.state}")
    print()

    if verification.state == DB_STATE_VALID:
        print("Database is valid. No repair required.")
        sys.exit(0)

    if verification.state == DB_STATE_FRESH:
        print("Fresh database detected.")
        print("Running migrations to initialize schema...")
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        print("Database initialized successfully.")
        sys.exit(0)

    if verification.state == DB_STATE_INTEGRITY_ERROR:
        print("ERROR: SQLite integrity check failed.")
        print("Database may be corrupted.")
        print()
        print("Recommended action:")
        print("Restore database from a healthy backup.")
        sys.exit(1)

    if verification.state == DB_STATE_SCHEMA_DRIFT:
        print("ERROR: Schema drift detected.")
        print("Alembic revision exists but required tables are missing.")
        print()
        print("Recommended action:")
        print("Restore database from backup.")
        sys.exit(1)

    if verification.state == DB_STATE_PARTIAL:
        print("ERROR: Partial schema detected.")
        print("Database structure is incomplete.")
        print()
        print("Recommended action:")
        print("Restore database from backup or rebuild manually.")
        sys.exit(1)

    print("ERROR: Unknown or unsupported database state.")
    sys.exit(1)


if __name__ == "__main__":
    main()
