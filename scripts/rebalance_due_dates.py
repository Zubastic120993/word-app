#!/usr/bin/env python3
"""
Rebalance due-word counts so the next N days have (roughly) the same number of
words due per day. Fetches all learning_progress rows with next_review_at in
the window, sorts by current next_review_at, and reassigns dates so each day
gets an equal share. Times are set to 09:00 UTC with a small per-row offset.

Run from project root:
  python scripts/rebalance_due_dates.py [--days 5] [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow importing app when run as script
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Even out due-word counts across the next N days.")
    parser.add_argument("--days", type=int, default=5, help="Number of days to rebalance (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without updating DB")
    args = parser.parse_args()

    if args.days < 2:
        print("--days must be at least 2")
        sys.exit(1)

    db_path = Path(settings.database_path)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    start_date = now.date()
    day_list = [start_date + timedelta(days=i) for i in range(args.days)]
    window_end = day_list[-1] + timedelta(days=1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Counts before
    counts_before = {}
    for d in day_list:
        c = conn.execute(
            """
            SELECT COUNT(*) AS n FROM learning_progress
            WHERE next_review_at IS NOT NULL AND date(next_review_at) = ?
            """,
            (str(d),),
        ).fetchone()["n"]
        counts_before[str(d)] = c

    # All rows with next_review_at in the window (only introduced if we want; here we use all)
    rows = conn.execute(
        """
        SELECT id, next_review_at
        FROM learning_progress
        WHERE next_review_at IS NOT NULL
          AND date(next_review_at) >= ?
          AND date(next_review_at) < ?
        ORDER BY next_review_at ASC
        """,
        (str(start_date), str(window_end)),
    ).fetchall()

    total = len(rows)
    if total == 0:
        print("No rows in the selected window. Nothing to rebalance.")
        conn.close()
        return

    # Bucket sizes: first (args.days - 1) buckets get base, last gets base + remainder
    base, remainder = divmod(total, args.days)
    sizes = [base + (1 if i < remainder else 0) for i in range(args.days)]
    assert sum(sizes) == total

    # Assign new date (and time) for each row
    idx = 0
    updates = []
    for day_idx, size in enumerate(sizes):
        d = day_list[day_idx]
        # 09:00 UTC + up to 1 minute spread per row so timestamps stay distinct
        base_dt = datetime(d.year, d.month, d.day, 9, 0, 0, tzinfo=timezone.utc)
        for i in range(size):
            row = rows[idx]
            new_dt = base_dt + timedelta(seconds=min(i, 59))
            updates.append((new_dt.isoformat(), row["id"]))
            idx += 1

    # Show before/after
    print("Before rebalance (due per day):")
    for d in day_list:
        print(f"  {d}: {counts_before[str(d)]}")
    print(f"  Total in window: {total}")

    counts_after = {str(d): 0 for d in day_list}
    for (new_iso, _) in updates:
        d = new_iso[:10]
        if d in counts_after:
            counts_after[d] += 1
    print("After rebalance (target):")
    for d in day_list:
        print(f"  {d}: {counts_after[str(d)]}")

    if args.dry_run:
        print("\n[DRY RUN] No changes written. Run without --dry-run to apply.")
        conn.close()
        return

    conn.executemany(
        "UPDATE learning_progress SET next_review_at = ? WHERE id = ?",
        updates,
    )
    conn.commit()
    conn.close()
    print(f"\nUpdated {len(updates)} rows. Due counts are now even across the next {args.days} days.")


if __name__ == "__main__":
    main()
