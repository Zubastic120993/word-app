import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app.config import settings


REQUIRED_TABLES = {"learning_units", "learning_progress", "learning_sessions"}
TIMESTAMP_PATTERN = re.compile(r"(\d{8}_\d{6})")


def backup_sort_key(path: Path) -> tuple[datetime, float, str]:
    match = TIMESTAMP_PATTERN.search(path.stem)
    if match:
        timestamp = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    else:
        timestamp = datetime.fromtimestamp(path.stat().st_mtime)
    return (timestamp, path.stat().st_mtime, path.name)


def is_valid_backup(path: Path) -> bool:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check;").fetchone()
        if not integrity or integrity[0] != "ok":
            return False

        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()
        tables = {row[0] for row in rows}
        return REQUIRED_TABLES.issubset(tables)
    finally:
        connection.close()


def find_newest_valid_backup(backups_dir: Path) -> Path | None:
    backups = sorted(
        backups_dir.glob("*.db"),
        key=backup_sort_key,
        reverse=True,
    )
    for backup in backups:
        if is_valid_backup(backup):
            return backup
    return None


def backup_current_database(db_path: Path, backups_dir: Path) -> Path | None:
    if not db_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"{db_path.stem}_pre_recovery_{timestamp}{db_path.suffix}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore the newest valid database backup and rebuild its checksum"
    )
    parser.parse_args()

    db_path = Path(settings.database_path).expanduser().resolve()
    backups_dir = db_path.parent / "backups"

    if not backups_dir.exists():
        print(f"No backup directory found: {backups_dir}")
        sys.exit(1)

    candidate = find_newest_valid_backup(backups_dir)
    if candidate is None:
        print(f"No valid backup files found in: {backups_dir}")
        sys.exit(1)

    print("Newest valid backup candidate:")
    print(candidate)
    confirm = input(f"Restore this backup to {db_path}? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Recovery cancelled.")
        sys.exit(1)

    backups_dir.mkdir(parents=True, exist_ok=True)
    current_backup = backup_current_database(db_path, backups_dir)
    if current_backup is not None:
        print(f"Current database backed up to: {current_backup}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate, db_path)
    print(f"Backup restored to: {db_path}")

    result = subprocess.run(
        [sys.executable, "-m", "app.tools.verify_db", "--rebuild-checksum"],
        check=False,
    )
    if result.returncode != 0:
        print("Recovery failed: restored database did not verify cleanly.")
        sys.exit(result.returncode)

    print(f"Database recovery completed successfully from: {candidate}")


if __name__ == "__main__":
    main()
