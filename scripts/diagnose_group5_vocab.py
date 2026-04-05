#!/usr/bin/env python3
"""
Temporary diagnostic script: List vocabularies in group_id=5 with sample words.

Read-only. Does NOT modify the database.
Do NOT commit this script.
"""

import sys
from pathlib import Path

# Ensure app is on path when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.learning_unit import LearningUnit
from app.models.vocabulary import Vocabulary


def main() -> None:
    db: Session = SessionLocal()
    try:
        vocabularies = (
            db.query(Vocabulary)
            .filter(Vocabulary.group_id == 5)
            .order_by(Vocabulary.name)
            .all()
        )

        if not vocabularies:
            print("No vocabularies found with group_id = 5")
            return

        print(f"Found {len(vocabularies)} vocabulary/ies in group_id=5\n")
        print("-" * 60)

        for vocab in vocabularies:
            # Fetch first 15 units by vocabulary_id (primary) or source_pdf (fallback)
            units = (
                db.query(LearningUnit.text)
                .filter(
                    (LearningUnit.vocabulary_id == vocab.id)
                    | (LearningUnit.source_pdf == vocab.name)
                )
                .limit(15)
                .all()
            )
            sample_texts = [u.text for u in units]

            print(f"\n{vocab.name}")
            print(f"  Sample words ({len(sample_texts)}): {sample_texts}")

        print("\n" + "-" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()
