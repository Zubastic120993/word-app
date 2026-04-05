from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.curriculum.themes_service import get_all_themes, get_theme_by_id
from app.curriculum.themes_config import CURRICULUM_VERSION
from app.database import get_db
from app.models.learning_unit import LearningUnit
from app.services.vocab_service import fetch_vocab_bias

router = APIRouter(prefix="/api/themes", tags=["themes"])


@router.get("/")
def list_themes():
    return {
        "curriculum_version": CURRICULUM_VERSION,
        "themes": get_all_themes()
    }


@router.get("/{theme_id}/weak-words")
def get_theme_weak_words(theme_id: str, db: Session = Depends(get_db)):
    theme = get_theme_by_id(theme_id)
    if not theme:
        return {"words": []}

    weak_words = fetch_vocab_bias(db, limit=25)
    theme_vocab_ids = set(theme["vocabulary_ids"])
    theme_words = set(
        db.execute(
            select(LearningUnit.text).where(LearningUnit.vocabulary_id.in_(theme_vocab_ids))
        ).scalars().all()
    )
    filtered = [w for w in weak_words if w in theme_words][:5]

    return {"words": filtered}
