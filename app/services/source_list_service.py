"""Service for listing PDF sources and vocabulary groups (iPad-accessible for study setup)."""

from collections import defaultdict
import unicodedata

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.learning_unit import LearningUnit
from app.models.vocabulary import Vocabulary, VocabularyGroup
from app.services.progress_metrics_service import compute_mastery_stats
from app.services.vocabulary_projection_service import get_effective_vocabularies
from app.utils.time import utc_now

DEFAULT_USER_KEY = "local"
USER_VOCAB_NAME = "Chat Vocabulary"


def _sync_vocabularies_from_sources(db: Session, *, user_key: str) -> None:
    """Keep vocabularies table in sync with LearningUnit.source_pdf values."""
    # Ensure per-user fallback exists
    existing = db.query(Vocabulary).filter(
        Vocabulary.user_key == user_key,
        Vocabulary.name == USER_VOCAB_NAME,
    ).first()
    if not existing:
        vocab = Vocabulary(user_key=user_key, name=USER_VOCAB_NAME)
        db.add(vocab)
        db.commit()

    sources = [r[0] for r in db.query(LearningUnit.source_pdf).distinct().all()]
    for name in sources:
        if not name:
            continue
        name = unicodedata.normalize("NFC", name)
        existing = db.query(Vocabulary).filter(
            Vocabulary.user_key == user_key,
            Vocabulary.name == name,
        ).first()
        if not existing:
            vocab = Vocabulary(user_key=user_key, name=name)
            db.add(vocab)
    db.commit()


def get_pdf_sources(db: Session) -> list[dict]:
    """Get list of available PDF sources with unit counts and mastery stats."""
    now = utc_now()
    units_by_source: dict[str, list[LearningUnit]] = defaultdict(list)
    units = (
        db.query(LearningUnit)
        .options(joinedload(LearningUnit.progress))
        .order_by(LearningUnit.source_pdf)
        .all()
    )
    for unit in units:
        units_by_source[unit.source_pdf].append(unit)

    sources = []

    for source_pdf in units_by_source:
        source_units = units_by_source[source_pdf]
        mastery_stats = compute_mastery_stats(source_units, now)
        sources.append({
            "filename": source_pdf,
            "unit_count": len(source_units),
            "mastered_pct": mastery_stats["mastered_pct"],
            "is_fully_mastered": mastery_stats["mastered_pct"] == 100.0,
        })

    return sources


def get_vocabulary_groups(db: Session) -> list[dict]:
    """List vocabulary groups with their vocabularies and unit counts."""
    groups = (
        db.query(VocabularyGroup)
        .filter(VocabularyGroup.user_key == DEFAULT_USER_KEY)
        .order_by(VocabularyGroup.display_order.asc())
        .all()
    )

    effective_vocabularies = get_effective_vocabularies(db, DEFAULT_USER_KEY)
    # Source totals are keyed by normalized source name so visually identical
    # variants (NFD/NFC) are counted together.
    raw_counts_by_source = dict(
        db.query(
            LearningUnit.source_pdf,
            func.count(LearningUnit.id).label("unit_count"),
        )
        .filter(LearningUnit.source_pdf.isnot(None))
        .group_by(LearningUnit.source_pdf)
        .all()
    )
    counts_by_normalized_source: dict[str, int] = defaultdict(int)
    for source_name, unit_count in raw_counts_by_source.items():
        normalized_source = unicodedata.normalize("NFC", source_name)
        counts_by_normalized_source[normalized_source] += int(unit_count)

    # Keep linked counts as a tie-breaker for normalized-name conflicts.
    linked_counts_by_vocabulary_id = dict(
        db.query(
            LearningUnit.vocabulary_id,
            func.count(LearningUnit.id).label("unit_count"),
        )
        .filter(LearningUnit.vocabulary_id.isnot(None))
        .group_by(LearningUnit.vocabulary_id)
        .all()
    )

    vocabularies_by_normalized_name: dict[str, list[dict]] = defaultdict(list)
    for vocabulary in effective_vocabularies:
        normalized_name = unicodedata.normalize("NFC", vocabulary["name"])
        vocabularies_by_normalized_name[normalized_name].append(vocabulary)

    assigned_counts: dict[tuple[int | None, str], int] = {}
    for normalized_name, candidates in vocabularies_by_normalized_name.items():
        source_total = counts_by_normalized_source.get(normalized_name, 0)
        if source_total == 0:
            continue

        def _candidate_priority(vocabulary: dict) -> tuple[int, int, int, int]:
            vocab_id = vocabulary.get("id")
            linked = linked_counts_by_vocabulary_id.get(vocab_id, 0) if vocab_id is not None else 0
            grouped = 1 if vocabulary.get("group_id") is not None else 0
            has_id = 1 if vocab_id is not None else 0
            # Prefer lower numeric IDs when all else is equal.
            stable_id = -(vocab_id if isinstance(vocab_id, int) else 10**9)
            return linked, grouped, has_id, stable_id

        owner = max(candidates, key=_candidate_priority)
        assigned_counts[(owner.get("id"), owner["name"])] = int(source_total)

    vocab_by_group: dict[int | None, list[dict]] = {}
    for vocabulary in effective_vocabularies:
        unit_count = assigned_counts.get((vocabulary.get("id"), vocabulary["name"]), 0)

        if unit_count == 0:
            continue
        group_id = vocabulary["group_id"]
        if group_id not in vocab_by_group:
            vocab_by_group[group_id] = []
        vocab_by_group[group_id].append({
            "id": vocabulary["id"],
            "name": vocabulary["name"],
            "unit_count": unit_count,
        })

    for group_id in vocab_by_group:
        vocab_by_group[group_id].sort(key=lambda v: v["name"])

    result = []
    for group in groups:
        group_vocabs = vocab_by_group.get(group.id, [])
        total_units = sum(v["unit_count"] for v in group_vocabs)
        result.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "display_order": group.display_order,
            "vocabularies": group_vocabs,
            "vocabulary_count": len(group_vocabs),
            "total_units": total_units,
        })

    ungrouped_vocabs = vocab_by_group.get(None, [])
    if ungrouped_vocabs:
        total_ungrouped_units = sum(v["unit_count"] for v in ungrouped_vocabs)
        result.append({
            "id": None,
            "name": "Ungrouped",
            "description": "Vocabularies not assigned to any group",
            "display_order": 999,
            "vocabularies": ungrouped_vocabs,
            "vocabulary_count": len(ungrouped_vocabs),
            "total_units": total_ungrouped_units,
        })

    return result
