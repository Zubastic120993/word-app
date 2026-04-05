"""
One-time migration: normalize all LearningUnit.source_pdf values to NFC.

macOS / Safari may store or transmit filenames in NFD form (decomposed
Unicode), causing SQL WHERE source_pdf = ? comparisons to fail silently.
This script converts every source_pdf to NFC so that the DB and all API
consumers use a consistent representation.

Usage:
    python scripts/normalize_filenames_to_nfc.py
"""

import sys
import unicodedata
from pathlib import Path

# Allow running from project root or from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models.learning_unit import LearningUnit


def main() -> None:
    db = SessionLocal()
    try:
        units = db.query(LearningUnit).all()
        updated = 0

        for unit in units:
            if not unit.source_pdf:
                continue
            nfc = unicodedata.normalize("NFC", unit.source_pdf)
            if nfc != unit.source_pdf:
                unit.source_pdf = nfc
                updated += 1

        db.commit()
        print(f"Done. {updated} row(s) updated out of {len(units)} total.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
