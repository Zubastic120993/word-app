from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult, UnitType
from app.models.vocabulary import Vocabulary
from app.services.source_list_service import get_pdf_sources


def _count_selects(bind, fn):
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(bind, "before_cursor_execute", before_cursor_execute)
    try:
        return fn(), len(statements)
    finally:
        event.remove(bind, "before_cursor_execute", before_cursor_execute)


def test_get_pdf_sources_batches_unit_loading():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db_session = Session()

    try:
        now = datetime(2026, 1, 1, 12, 0, 0)
        db_session.add_all(
            [
                Vocabulary(id=1, user_key="test", name="a.pdf"),
                Vocabulary(id=2, user_key="test", name="b.pdf"),
            ]
        )
        db_session.flush()

        units = [
            LearningUnit(
                text="a1",
                translation="A1",
                type=UnitType.WORD,
                source_pdf="a.pdf",
                vocabulary_id=1,
            ),
            LearningUnit(
                text="a2",
                translation="A2",
                type=UnitType.WORD,
                source_pdf="a.pdf",
                vocabulary_id=1,
            ),
            LearningUnit(
                text="b1",
                translation="B1",
                type=UnitType.WORD,
                source_pdf="b.pdf",
                vocabulary_id=2,
            ),
        ]
        db_session.add_all(units)
        db_session.flush()
        db_session.add_all(
            [
                LearningProgress(
                    unit_id=units[0].id,
                    introduced_at=None,
                ),
                LearningProgress(
                    unit_id=units[1].id,
                    introduced_at=now,
                    confidence_score=0.9,
                    last_recall_result=RecallResult.CORRECT,
                    next_review_at=now,
                ),
                LearningProgress(
                    unit_id=units[2].id,
                    introduced_at=None,
                ),
            ]
        )
        db_session.commit()

        sources, select_count = _count_selects(db_session.bind, lambda: get_pdf_sources(db_session))

        assert select_count == 1
        assert [source["filename"] for source in sources] == ["a.pdf", "b.pdf"]
        assert [source["unit_count"] for source in sources] == [2, 1]
    finally:
        db_session.close()
