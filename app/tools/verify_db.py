import argparse
import sys
from pathlib import Path

from app.config import settings
from app.database import CHECKSUM_SUFFIX, REQUIRED_TABLES, compute_db_checksum, engine
from app.db_verification import DB_STATE_VALID, verify_database


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify database integrity, schema, and checksum status"
    )
    parser.add_argument(
        "--rebuild-checksum",
        action="store_true",
        help="Rebuild checksum file if DB integrity and schema are valid",
    )
    args = parser.parse_args()

    db_path = str(Path(settings.database_path).expanduser().resolve())

    if args.rebuild_checksum:
        verification = verify_database(engine, db_path, REQUIRED_TABLES)
        if verification.integrity_ok and not verification.missing_required_tables:
            checksum = compute_db_checksum(db_path)
            checksum_file = Path(db_path).with_name(Path(db_path).name + CHECKSUM_SUFFIX)
            checksum_file.write_text(checksum)
            print("Checksum rebuilt:", checksum)
        else:
            print("Cannot rebuild checksum: database not in safe state")
            sys.exit(1)

    verification = verify_database(engine, db_path, REQUIRED_TABLES)

    integrity = "OK" if verification.integrity_ok else "FAILED"
    tables = ", ".join(sorted(verification.tables))

    print("Database verification")
    print("---------------------")
    print(f"Path: {db_path}")
    print(f"Integrity: {integrity}")
    print(f"Checksum: {verification.checksum_status}")
    print(f"Revision: {verification.revision}")
    print(f"Tables: {tables}")
    print()
    print(f"Status: {verification.state}")

    if verification.missing_required_tables:
        print()
        print("ERROR: Required tables missing")
        print(f"Missing tables: {', '.join(sorted(verification.missing_required_tables))}")

    sys.exit(0 if verification.state == DB_STATE_VALID else 1)


if __name__ == "__main__":
    main()
