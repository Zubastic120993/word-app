"""Read-only vocabulary projection service."""

from sqlalchemy.orm import Session

from app.models.learning_unit import LearningUnit
from app.models.vocabulary import Vocabulary

USER_VOCAB_NAME = "Chat Vocabulary"


def get_effective_vocabularies(db: Session, user_key: str) -> list[dict]:
    """Return the effective vocabulary projection for a user without mutating state."""
    vocabularies = (
        db.query(Vocabulary)
        .filter(Vocabulary.user_key == user_key)
        .order_by(Vocabulary.name.asc())
        .all()
    )
    vocab_by_name = {
        vocabulary.name: {
            "id": vocabulary.id,
            "name": vocabulary.name,
            "group_id": vocabulary.group_id,
        }
        for vocabulary in vocabularies
    }

    if USER_VOCAB_NAME not in vocab_by_name:
        vocab_by_name[USER_VOCAB_NAME] = {
            "id": None,
            "name": USER_VOCAB_NAME,
            "group_id": None,
        }

    source_names = [
        name for (name,) in db.query(LearningUnit.source_pdf).distinct().all() if name
    ]
    for name in source_names:
        if name not in vocab_by_name:
            vocab_by_name[name] = {
                "id": None,
                "name": name,
                "group_id": None,
            }

    return sorted(vocab_by_name.values(), key=lambda vocabulary: vocabulary["name"])
