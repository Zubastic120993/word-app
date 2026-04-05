from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import SessionLocal
from app.models import LearningUnit, Vocabulary


def run_audit():
    db: Session = SessionLocal()

    print("\n=== VOCABULARY TABLE ===")
    vocabularies = db.query(Vocabulary).all()
    print(f"Total vocabularies: {len(vocabularies)}")

    print("\nVocabulary IDs:")
    for v in vocabularies:
        print(f"- id={v.id} | name={v.name}")

    print("\n=== LEARNING UNIT ANALYSIS ===")

    total_units = db.query(LearningUnit).count()
    print(f"Total LearningUnit rows: {total_units}")

    null_vocab = (
        db.query(LearningUnit)
        .filter(LearningUnit.vocabulary_id == None)
        .count()
    )
    print(f"Units with NULL vocabulary_id: {null_vocab}")

    distinct_vocab_ids = (
        db.query(LearningUnit.vocabulary_id)
        .filter(LearningUnit.vocabulary_id != None)
        .distinct()
        .all()
    )

    distinct_vocab_ids = sorted([row[0] for row in distinct_vocab_ids])
    print(f"Distinct vocabulary_ids in LearningUnit: {distinct_vocab_ids}")

    print("\nUnits per vocabulary_id:")
    for vid in distinct_vocab_ids:
        count = (
            db.query(LearningUnit)
            .filter(LearningUnit.vocabulary_id == vid)
            .count()
        )
        print(f"- vocabulary_id={vid} -> {count} units")

    print("\n=== ORPHAN CHECK ===")

    vocab_ids_in_table = {v.id for v in vocabularies}
    orphan_vocab_ids = [
        vid for vid in distinct_vocab_ids if vid not in vocab_ids_in_table
    ]

    if orphan_vocab_ids:
        print("Orphan vocabulary_ids found:", orphan_vocab_ids)
    else:
        print("No orphan vocabulary references found.")

    print("\nAudit complete.\n")

    db.close()


if __name__ == "__main__":
    run_audit()
