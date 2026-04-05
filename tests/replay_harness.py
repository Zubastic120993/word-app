from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_service_module
from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult, UnitType
from app.models.session import SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import SessionService


FIXTURES_DIR = Path(__file__).parent / "replay_fixtures"


@dataclass(frozen=True)
class ReplaySnapshot:
    now: datetime
    seed: int
    progress_state: dict[str, Any]
    due_state: dict[str, Any]
    weak_state: dict[str, Any]
    vocabularies: list[dict[str, Any]]
    units: list[dict[str, Any]]


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def load_snapshot(name: str = "example_snapshot.json") -> ReplaySnapshot:
    snapshot_path = FIXTURES_DIR / name
    payload = json.loads(snapshot_path.read_text())
    return ReplaySnapshot(
        now=datetime.fromisoformat(payload["snapshot_time"]),
        seed=payload["seed"],
        progress_state=payload["progress_state"],
        due_state=payload["due_state"],
        weak_state=payload["weak_state"],
        vocabularies=payload["vocabularies"],
        units=payload["units"],
    )


def _seed_snapshot_data(db_session, snapshot: ReplaySnapshot) -> None:
    for vocab in snapshot.vocabularies:
        db_session.add(
            Vocabulary(
                id=vocab["id"],
                user_key=vocab["user_key"],
                name=vocab["name"],
            )
        )
    db_session.flush()

    for unit_payload in snapshot.units:
        unit = LearningUnit(
            id=unit_payload["id"],
            text=unit_payload["text"],
            type=UnitType(unit_payload["type"]),
            translation=unit_payload["translation"],
            source_pdf=unit_payload["source_pdf"],
            vocabulary_id=unit_payload["vocabulary_id"],
            normalized_text=unit_payload["normalized_text"],
            normalized_translation=unit_payload["normalized_translation"],
        )
        db_session.add(unit)
        db_session.flush()

        progress_payload = unit_payload.get("progress")
        if progress_payload is None:
            continue

        db_session.add(
            LearningProgress(
                unit_id=unit.id,
                times_seen=progress_payload["times_seen"],
                times_correct=progress_payload["times_correct"],
                times_failed=progress_payload["times_failed"],
                confidence_score=progress_payload["confidence_score"],
                last_seen=_parse_datetime(progress_payload.get("last_seen")),
                last_recall_result=(
                    RecallResult(progress_payload["last_recall_result"])
                    if progress_payload.get("last_recall_result") is not None
                    else None
                ),
                next_review_at=_parse_datetime(progress_payload.get("next_review_at")),
                introduced_at=_parse_datetime(progress_payload.get("introduced_at")),
                recall_fail_streak=progress_payload["recall_fail_streak"],
                is_blocked=progress_payload["is_blocked"],
                stability_score=progress_payload["stability_score"],
            )
        )

    db_session.commit()


def run_replay(seed: int, snapshot_name: str = "example_snapshot.json") -> list[int]:
    snapshot = load_snapshot(snapshot_name)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db_session = session_factory()
    original_ensure_overdue_spread = session_service_module.ensure_overdue_spread
    original_build_lesson_to_vocab = session_service_module.build_lesson_to_vocab
    original_detect_current_lesson = session_service_module._detect_current_lesson
    original_utc_now_naive = session_service_module._utc_now_naive

    try:
        _seed_snapshot_data(db_session, snapshot)
        session_service_module.ensure_overdue_spread = lambda db: None
        session_service_module.build_lesson_to_vocab = lambda db: {1: [v["id"] for v in snapshot.vocabularies]}
        session_service_module._detect_current_lesson = lambda db: 1
        session_service_module._utc_now_naive = lambda: snapshot.now

        service = SessionService(db_session, random_seed=seed)
        session = service.create_session(mode=StudyModeType.PASSIVE)
        return [
            row.unit_id
            for row in db_session.query(SessionUnit)
            .filter(SessionUnit.session_id == session.id)
            .order_by(SessionUnit.position.asc())
            .all()
        ]
    finally:
        session_service_module.ensure_overdue_spread = original_ensure_overdue_spread
        session_service_module.build_lesson_to_vocab = original_build_lesson_to_vocab
        session_service_module._detect_current_lesson = original_detect_current_lesson
        session_service_module._utc_now_naive = original_utc_now_naive
        db_session.close()
        engine.dispose()
