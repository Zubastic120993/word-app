#!/usr/bin/env python3
"""
Restore data/vocabulary.db from the latest backup in data/backups/.
Prefers backups that actually contain data (learning_units > 0).

Run from project root:
    python scripts/restore_data.py
"""
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = ROOT / "data" / "backups"
TARGET = ROOT / "data" / "vocabulary.db"


def count_units(path: Path) -> int:
    try:
        with sqlite3.connect(path) as conn:
            return conn.execute("SELECT COUNT(*) FROM learning_units").fetchone()[0]
    except Exception:
        return 0


def main():
    if not BACKUPS_DIR.exists():
        print("No data/backups directory found.", file=sys.stderr)
        sys.exit(1)

    candidates = list(BACKUPS_DIR.glob("vocabulary*.db"))
    if not candidates:
        print("No vocabulary*.db backups found in data/backups.", file=sys.stderr)
        sys.exit(1)

    # Prefer newest backup that has data
    with_counts = [(p, count_units(p), p.stat().st_mtime) for p in candidates]
    with_data = [(p, n, m) for p, n, m in with_counts if n > 0]
    if with_data:
        chosen = max(with_data, key=lambda x: (x[1], x[2]))  # most units, then newest
        backup, units, _ = chosen
    else:
        backup = max(candidates, key=lambda p: p.stat().st_mtime)
        units = count_units(backup)

    print(f"Restoring from: {backup.name} ({units} learning units)")
    shutil.copy2(backup, TARGET)
    print(f"Restored to: {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
