"""Backfill vocabulary_id for dictionary lesson units with NULL vocabulary_id."""

import re

from dotenv import load_dotenv
from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

load_dotenv()

from app.database import DATABASE_URL, SessionLocal
from app.models.learning_unit import LearningUnit
from app.models.vocabulary import Vocabulary

DEFAULT_USER_KEY = "local"
LESSON_RE = re.compile(r"lesson(\d+)", re.IGNORECASE)
CZYTAJ_GROUP_RE = re.compile(r"(czytaj_\d+_\d+)_", re.IGNORECASE)


def run_migration() -> None:
    db: Session = SessionLocal()

    created_vocabularies = 0
    assigned_units = 0
    skipped_sources = 0

    try:
        print("Using DATABASE_URL:", DATABASE_URL)

        inspector = inspect(db.bind)
        tables = inspector.get_table_names()
        if "learning_units" not in tables:
            print("ERROR: learning_units table not found in this database.")
            print("Aborting migration.")
            return

        grouped_sources = (
            db.query(
                LearningUnit.source_pdf,
                func.count(LearningUnit.id).label("unit_count"),
            )
            .filter(LearningUnit.vocabulary_id.is_(None))
            .group_by(LearningUnit.source_pdf)
            .all()
        )

        if not grouped_sources:
            print("No units with NULL vocabulary_id found. Nothing to migrate.")
            return

        print(f"Found {len(grouped_sources)} source groups with NULL vocabulary_id units.")

        existing_vocabularies = {
            v.name: v
            for v in db.query(Vocabulary)
            .filter(Vocabulary.user_key == DEFAULT_USER_KEY)
            .all()
        }

        for source_pdf, unit_count in grouped_sources:
            source_name = (source_pdf or "").strip()
            czytaj_match = CZYTAJ_GROUP_RE.search(source_name)
            if czytaj_match:
                vocab_name = czytaj_match.group(1).lower()
            else:
                match = LESSON_RE.search(source_name)
                if match:
                    lesson_number = int(match.group(1))
                    vocab_name = f"dictionary_lesson_{lesson_number}"
                else:
                    skipped_sources += 1
                    print(
                        f"SKIP source={source_pdf!r} units={unit_count} "
                        "(no lessonXX or czytaj_XX_YY pattern found)"
                    )
                    continue

            vocabulary = existing_vocabularies.get(vocab_name)
            if vocabulary is None:
                vocabulary = Vocabulary(user_key=DEFAULT_USER_KEY, name=vocab_name)
                db.add(vocabulary)
                db.flush()  # Get vocabulary.id without committing per-row
                existing_vocabularies[vocab_name] = vocabulary
                created_vocabularies += 1
                print(f"CREATE vocabulary name={vocab_name!r} id={vocabulary.id}")

            updated = (
                db.query(LearningUnit)
                .filter(LearningUnit.source_pdf == source_pdf)
                .filter(LearningUnit.vocabulary_id.is_(None))
                .update({LearningUnit.vocabulary_id: vocabulary.id}, synchronize_session=False)
            )
            assigned_units += updated
            print(
                f"ASSIGN source={source_pdf!r} -> vocabulary={vocab_name!r} "
                f"updated_units={updated}"
            )

        db.commit()

        print("\nMigration summary:")
        print(f"- vocabularies created: {created_vocabularies}")
        print(f"- units assigned: {assigned_units}")
        print(f"- sources skipped (no lessonXX/czytaj_XX_YY match): {skipped_sources}")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run_migration()
