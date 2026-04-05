from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.session_service as session_service_module
from app.database import Base
from app.models.learning_unit import LearningProgress, LearningUnit, RecallResult, UnitType
from app.models.session import LearningSession, SessionUnit, StudyModeType
from app.models.vocabulary import Vocabulary
from app.services.session_service import (
    DAILY_REVIEW_CAP,
    DUE_ITEMS_MAX_PERCENT,
    InsufficientUnitsError,
    MAX_DUPLICATES_PER_UNIT,
    NoDueUnitsError,
    RecallControllerSnapshot,
    SESSION_SIZE,
    SessionService,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def stable_selection_env(monkeypatch):
    monkeypatch.setattr(session_service_module, "ensure_overdue_spread", lambda db: None)


def _create_vocab(db_session, vocab_id: int, name: str | None = None) -> Vocabulary:
    vocab = Vocabulary(
        id=vocab_id,
        user_key="test",
        name=name or f"vocab_{vocab_id}.pdf",
    )
    db_session.add(vocab)
    db_session.flush()
    return vocab


def _create_unit(
    db_session,
    vocab: Vocabulary,
    idx: int,
    *,
    source_pdf: str | None = None,
) -> LearningUnit:
    unit = LearningUnit(
        text=f"word_{vocab.id}_{idx}",
        type=UnitType.WORD,
        translation=f"translation_{vocab.id}_{idx}",
        source_pdf=source_pdf or vocab.name,
        vocabulary_id=vocab.id,
        normalized_text=f"word_{vocab.id}_{idx}",
        normalized_translation=f"translation_{vocab.id}_{idx}",
    )
    db_session.add(unit)
    db_session.flush()
    return unit


def _create_progress(
    db_session,
    unit: LearningUnit,
    *,
    now,
    introduced: bool = True,
    due: bool = True,
    confidence: float = 0.2,
    times_correct: int = 0,
    times_failed: int = 3,
    last_recall_result=RecallResult.FAILED,
):
    progress = LearningProgress(
        unit_id=unit.id,
        times_seen=times_correct + times_failed,
        times_correct=times_correct,
        times_failed=times_failed,
        confidence_score=confidence,
        last_seen=now - timedelta(days=1),
        introduced_at=now - timedelta(days=2) if introduced else None,
        next_review_at=now - timedelta(hours=1) if due else now + timedelta(days=5),
        last_recall_result=last_recall_result,
        recall_fail_streak=times_failed,
        is_blocked=False,
        stability_score=0.0,
    )
    db_session.add(progress)
    db_session.flush()
    return progress


def _session_unit_ids(session: LearningSession) -> list[int]:
    return [session_unit.unit_id for session_unit in session.units]


def _configure_single_lesson(monkeypatch, vocab_ids: list[int]):
    monkeypatch.setattr(
        session_service_module,
        "build_lesson_to_vocab",
        lambda db: {1: vocab_ids},
    )
    monkeypatch.setattr(session_service_module, "_detect_current_lesson", lambda db: 1)


class TestDueOnlyProductionPath:
    def test_due_only_enforces_introduced_at_not_null(self, db_session, stable_selection_env):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)

        introduced_due = []
        for idx in range(5):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(db_session, unit, now=now, introduced=True, due=True)
            introduced_due.append(unit.id)

        for idx in range(5, 9):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(db_session, unit, now=now, introduced=False, due=True)

        db_session.commit()

        service = SessionService(db_session, random_seed=7)
        session = service.create_session(mode=StudyModeType.RECALL, due_only=True)

        assert _session_unit_ids(session) == introduced_due

    def test_due_only_ignores_lesson_filtering_in_production_path(
        self,
        db_session,
        stable_selection_env,
        monkeypatch,
    ):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)
        unit = _create_unit(db_session, vocab, 1)
        _create_progress(db_session, unit, now=now, introduced=True, due=True)
        db_session.commit()

        monkeypatch.setattr(
            session_service_module,
            "build_lesson_to_vocab",
            lambda db: (_ for _ in ()).throw(AssertionError("lesson mapping should not be used")),
        )

        service = SessionService(db_session, random_seed=7)
        session = service.create_session(
            mode=StudyModeType.RECALL,
            due_only=True,
            lesson_id=999,
        )

        assert _session_unit_ids(session) == [unit.id]

    def test_due_only_respects_daily_cap(self, db_session, stable_selection_env):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)

        due_unit = _create_unit(db_session, vocab, 1)
        _create_progress(db_session, due_unit, now=now, introduced=True, due=True)

        session = LearningSession(
            mode=StudyModeType.RECALL,
            locked=True,
            completed=False,
            due_only=True,
        )
        db_session.add(session)
        db_session.flush()

        for idx in range(DAILY_REVIEW_CAP):
            answered_unit = _create_unit(db_session, vocab, 1000 + idx)
            session_unit = SessionUnit(
                session_id=session.id,
                unit_id=answered_unit.id,
                position=idx + 1,
                answered=True,
                answered_at=now,
            )
            db_session.add(session_unit)

        db_session.commit()

        service = SessionService(db_session, random_seed=7)
        result = service.create_session(mode=StudyModeType.RECALL, due_only=True)

        assert result["daily_cap_reached"] is True
        assert result["reviewed_today"] == DAILY_REVIEW_CAP
        assert result["total_due"] == 1
        assert result["remaining_due"] == 1

    def test_due_only_short_session_note_when_few_due_in_scope(
        self, db_session, stable_selection_env, monkeypatch
    ):
        """Progress N / SESSION_SIZE with N < SESSION_SIZE: explain small due pool (not a bug)."""
        monkeypatch.setattr(session_service_module, "DAILY_REVIEW_CAP", 10_000)
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)
        unit = _create_unit(db_session, vocab, 1)
        _create_progress(db_session, unit, now=now, introduced=True, due=True)
        db_session.commit()

        service = SessionService(db_session, random_seed=7)
        session = service.create_session(mode=StudyModeType.RECALL, due_only=True)

        assert len(session.units) == 1
        note = getattr(session, "_short_session_note", "")
        assert "Only 1 word" in note
        assert "scope" in note.lower()

    def test_due_only_full_session_size_ignores_partial_daily_quota_headroom(
        self, db_session, stable_selection_env, monkeypatch
    ):
        """Due-only batch size is min(due, SESSION_SIZE), not clipped by remaining daily cap."""
        monkeypatch.setattr(session_service_module, "DAILY_REVIEW_CAP", 10)
        monkeypatch.setattr(session_service_module, "SESSION_SIZE", 12)
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)

        prior = LearningSession(
            mode=StudyModeType.RECALL,
            locked=True,
            completed=False,
            due_only=True,
        )
        db_session.add(prior)
        db_session.flush()
        for i in range(7):
            u = _create_unit(db_session, vocab, 900 + i)
            _create_progress(db_session, u, now=now, introduced=True, due=False)
            db_session.add(
                SessionUnit(
                    session_id=prior.id,
                    unit_id=u.id,
                    position=i + 1,
                    answered=True,
                    answered_at=now,
                )
            )

        for idx in range(20):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(db_session, unit, now=now, introduced=True, due=True)
        db_session.commit()

        service = SessionService(db_session, random_seed=7)
        session = service.create_session(mode=StudyModeType.RECALL, due_only=True)

        assert len(session.units) == 12
        assert getattr(session, "_short_session_note", None) is None

    def test_due_only_empty_pool_raises(self, db_session, stable_selection_env):
        vocab = _create_vocab(db_session, 1)
        _create_unit(db_session, vocab, 1)
        db_session.commit()

        service = SessionService(db_session, random_seed=7)

        with pytest.raises(NoDueUnitsError, match="No words are due for review right now."):
            service.create_session(mode=StudyModeType.RECALL, due_only=True)

    def test_due_only_base_ordering_is_next_review_at_ascending(self, db_session, stable_selection_env):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)
        review_offsets = [4, 1, 3, 2]

        for idx, hours in enumerate(review_offsets):
            unit = _create_unit(db_session, vocab, idx)
            progress = LearningProgress(
                unit_id=unit.id,
                times_seen=3,
                times_correct=1,
                times_failed=2,
                confidence_score=0.2,
                last_seen=now - timedelta(days=1),
                introduced_at=now - timedelta(days=2),
                next_review_at=now - timedelta(hours=hours),
                last_recall_result=RecallResult.FAILED,
                recall_fail_streak=2,
                is_blocked=False,
                stability_score=0.0,
            )
            db_session.add(progress)

        db_session.commit()

        service = SessionService(db_session, random_seed=7)
        session = service.create_session(mode=StudyModeType.RECALL, due_only=True)

        selected_times = [
            db_session.get(LearningUnit, unit_id).progress.next_review_at
            for unit_id in _session_unit_ids(session)
        ]

        assert selected_times == sorted(selected_times)

    def test_due_only_uses_balanced_path_when_pool_exceeds_session_size_and_accepts_duplicates(
        self,
        db_session,
        stable_selection_env,
        monkeypatch,
    ):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)

        due_ids = []
        for idx in range(SESSION_SIZE + 25):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(db_session, unit, now=now, introduced=True, due=True)
            due_ids.append(unit.id)
        db_session.commit()

        service = SessionService(db_session, random_seed=7)
        calls = {"count": 0}

        def fake_balanced(due_units, session_size, now, **kwargs):
            calls["count"] += 1
            assert len(due_units) == len(due_ids)
            assert session_size == SESSION_SIZE
            return [due_units[0], due_units[0]] + due_units[1:session_size - 1]

        monkeypatch.setattr(service, "_select_balanced_units", fake_balanced)

        session = service.create_session(mode=StudyModeType.RECALL, due_only=True)
        selected_ids = _session_unit_ids(session)

        assert calls["count"] == 1
        assert len(selected_ids) == SESSION_SIZE
        assert len(selected_ids) > len(set(selected_ids))


class TestWeakOnlyCharacterization:
    def test_weak_only_case_a_all_selected_ids_come_from_weak_pool(
        self,
        db_session,
        stable_selection_env,
        monkeypatch,
    ):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)
        weak_ids = []

        for idx in range(SESSION_SIZE + 10):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(
                db_session,
                unit,
                now=now,
                introduced=True,
                due=True,
                confidence=0.1,
                times_correct=0,
                times_failed=4,
                last_recall_result=RecallResult.FAILED,
            )
            weak_ids.append(unit.id)

        db_session.commit()
        _configure_single_lesson(monkeypatch, [vocab.id])

        service = SessionService(db_session, random_seed=11)
        session = service.create_session(mode=StudyModeType.RECALL, weak_only=True)
        selected_ids = _session_unit_ids(session)

        assert set(selected_ids).issubset(set(weak_ids))

    def test_weak_only_without_padding_never_includes_non_weak_units(
        self,
        db_session,
        stable_selection_env,
        monkeypatch,
    ):
        """Weak-only sessions never pad with review/new; few weak → repeats to fill SESSION_SIZE."""
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)
        weak_ids = []

        for idx in range(5):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(
                db_session,
                unit,
                now=now,
                introduced=True,
                due=False,
                confidence=0.1,
                times_correct=0,
                times_failed=5,
                last_recall_result=RecallResult.FAILED,
            )
            weak_ids.append(unit.id)

        for idx in range(5, SESSION_SIZE + 20):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(
                db_session,
                unit,
                now=now,
                introduced=True,
                due=False,
                confidence=0.9,
                times_correct=5,
                times_failed=0,
                last_recall_result=RecallResult.CORRECT,
            )

        db_session.commit()
        _configure_single_lesson(monkeypatch, [vocab.id])

        service = SessionService(db_session, random_seed=13)
        session = service.create_session(mode=StudyModeType.PASSIVE, weak_only=True)
        selected_ids = _session_unit_ids(session)
        assert set(selected_ids).issubset(set(weak_ids))
        assert len(selected_ids) == SESSION_SIZE

    def test_weak_mode_can_insert_reinforcement_duplicates(self, db_session, stable_selection_env, monkeypatch):
        now = session_service_module._utc_now_naive()
        vocab = _create_vocab(db_session, 1)

        for idx in range(SESSION_SIZE + 10):
            unit = _create_unit(db_session, vocab, idx)
            _create_progress(
                db_session,
                unit,
                now=now,
                introduced=True,
                due=False,
                confidence=0.1,
                times_correct=0,
                times_failed=5,
                last_recall_result=RecallResult.FAILED,
            )

        db_session.commit()
        _configure_single_lesson(monkeypatch, [vocab.id])

        service = SessionService(db_session, random_seed=21)
        monkeypatch.setattr(service, "_compute_reinforcement_depth", lambda *args, **kwargs: 5)

        session = service.create_session(mode=StudyModeType.RECALL, weak_only=True)
        selected_ids = _session_unit_ids(session)
        counts = {}
        for unit_id in selected_ids:
            counts[unit_id] = counts.get(unit_id, 0) + 1

        assert len(selected_ids) > len(set(selected_ids))
        n_uniq = len(set(selected_ids))
        max_allowed = max(
            MAX_DUPLICATES_PER_UNIT,
            (SESSION_SIZE + n_uniq - 1) // max(n_uniq, 1),
        )
        assert max(counts.values()) <= max_allowed


class TestNormalCharacterization:
    def test_normal_path_caps_due_at_seventy_percent_and_uses_weighted_sampling_without_duplicates(
        self,
        monkeypatch,
    ):
        service = SessionService(SimpleNamespace(), random_seed=17)
        due_units = [(SimpleNamespace(id=idx), 1.0) for idx in range(1, 51)]
        new_units = [(SimpleNamespace(id=100 + idx), 1.0) for idx in range(1, 31)]
        weak_units = [(SimpleNamespace(id=200 + idx), 1.0) for idx in range(1, 31)]
        review_units = [(SimpleNamespace(id=300 + idx), 1.0) for idx in range(1, 31)]
        sample_calls = []

        monkeypatch.setattr(service, "_get_due_units_weighted", lambda *args, **kwargs: due_units)
        monkeypatch.setattr(service, "_get_new_units_weighted", lambda *args, **kwargs: new_units)
        monkeypatch.setattr(service, "_get_weak_units_weighted", lambda *args, **kwargs: weak_units)
        monkeypatch.setattr(service, "_get_review_units_weighted", lambda *args, **kwargs: review_units)
        monkeypatch.setattr(
            service,
            "_select_balanced_units",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("balanced helper should not be used")),
        )
        monkeypatch.setattr(
            service,
            "_compute_reinforcement_depth",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reinforcement depth should not be used")),
        )
        monkeypatch.setattr(
            service,
            "_compute_gain_adjustment",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("gain adjustment should not be used")),
        )

        def sample_spy(units_with_weights, count, selected_ids):
            sample_calls.append((tuple(unit.id for unit, _ in units_with_weights), count))
            return SessionService._weighted_random_sample(service, units_with_weights, count, selected_ids)

        monkeypatch.setattr(service, "_weighted_random_sample", sample_spy)

        selected = service._select_units_weighted_random(mode=StudyModeType.RECALL)
        selected_ids = [unit.id for unit in selected]
        due_cutoff = int(SESSION_SIZE * DUE_ITEMS_MAX_PERCENT)

        assert len(selected_ids) == SESSION_SIZE
        assert len(set(selected_ids)) == SESSION_SIZE
        assert len([unit_id for unit_id in selected_ids if unit_id < 100]) == due_cutoff
        assert sample_calls[0][1] == due_cutoff
        assert len(sample_calls) >= 4


class TestSelectionMechanics:
    def test_weighted_random_sample_is_without_replacement(self):
        service = SessionService(SimpleNamespace(), random_seed=19)
        units = [(SimpleNamespace(id=idx), 1.0) for idx in range(10)]

        selected = service._weighted_random_sample(units, 10, selected_ids=set())
        selected_ids = [unit.id for unit in selected]

        assert len(selected_ids) == len(set(selected_ids))

    def test_select_balanced_units_may_create_duplicates(self, monkeypatch):
        service = SessionService(SimpleNamespace(), random_seed=23)
        now = session_service_module._utc_now_naive()

        due_units = []
        for idx in range(12):
            progress = SimpleNamespace(
                confidence_score=0.1,
                last_seen=now - timedelta(days=1),
                recall_fail_streak=5,
                stability_score=0.0,
            )
            due_units.append(SimpleNamespace(id=idx + 1, progress=progress))

        monkeypatch.setattr(service, "_compute_difficulty_score", lambda progress, now: 0.9)
        monkeypatch.setattr(service, "_compute_gain_adjustment", lambda gain: (1, 0.0))
        monkeypatch.setattr(service, "_compute_reinforcement_depth", lambda *args, **kwargs: 3)

        selected = service._select_balanced_units(due_units=due_units, session_size=10, now=now)
        selected_ids = [unit.id for unit in selected]

        assert len(selected_ids) > len(set(selected_ids))

    def test_observe_only_captures_instrumentation_without_affecting_selection(self, monkeypatch):
        legacy_service = SessionService(SimpleNamespace(), random_seed=31)
        observe_service = SessionService(SimpleNamespace(), random_seed=31)
        observe_service._recall_controller_mode = "observe_only"

        due_units = [(SimpleNamespace(id=idx), 1.0) for idx in range(1, 9)]
        new_units = [(SimpleNamespace(id=100 + idx), 1.0) for idx in range(1, 20)]
        weak_units = [(SimpleNamespace(id=200 + idx), 1.0) for idx in range(1, 15)]
        review_units = [(SimpleNamespace(id=300 + idx), 1.0) for idx in range(1, 20)]

        for service in (legacy_service, observe_service):
            monkeypatch.setattr(service, "_get_due_units_weighted", lambda *args, **kwargs: due_units)
            monkeypatch.setattr(service, "_get_new_units_weighted", lambda *args, **kwargs: new_units)
            monkeypatch.setattr(service, "_get_weak_units_weighted", lambda *args, **kwargs: weak_units)
            monkeypatch.setattr(service, "_get_review_units_weighted", lambda *args, **kwargs: review_units)
            monkeypatch.setattr(
                service,
                "_record_recall_controller_snapshot",
                lambda request, pool, selected_units=None, svc=service: RecallControllerSnapshot(
                    mode=svc._recall_controller_mode,
                    recall_depth=1.5,
                    weak_ratio=0.6,
                    due_ratio=0.4,
                    reinforcement_depth_bias=1,
                    session_difficulty_signal=0.75,
                ),
            )

        request = legacy_service._build_selection_request(
            mode="normal",
            study_mode=StudyModeType.PASSIVE,
            session_size=SESSION_SIZE,
            pool_kind="normal",
            use_due_first_split=True,
            now=session_service_module._utc_now_naive(),
        )
        legacy = legacy_service._execute_selection_pipeline(request)
        observe = observe_service._execute_selection_pipeline(request)

        assert [unit.id for unit in legacy] == [unit.id for unit in observe]

    def test_v3_experimental_only_applies_bounded_reinforcement_depth_bias(self, monkeypatch):
        service = SessionService(SimpleNamespace(), random_seed=37)
        service._recall_controller_mode = "v3_experimental"

        progress = SimpleNamespace(
            confidence_score=0.2,
            last_seen=session_service_module._utc_now_naive() - timedelta(days=2),
            recall_fail_streak=4,
            stability_score=0.1,
        )
        weak_units = [(SimpleNamespace(id=idx, progress=progress), 1.0) for idx in range(1, 21)]
        request = service._build_selection_request(
            mode="weak_only",
            study_mode=StudyModeType.RECALL,
            session_size=SESSION_SIZE,
            pool_kind="weak",
            apply_reinforcement_only=True,
            now=session_service_module._utc_now_naive(),
        )
        selected_units = [unit for unit, _ in weak_units[:SESSION_SIZE]]
        depths = []

        monkeypatch.setattr(
            service,
            "_record_recall_controller_snapshot",
            lambda request, pool, selected_units=None: RecallControllerSnapshot(
                mode="v3_experimental",
                recall_depth=2.0,
                weak_ratio=1.0,
                due_ratio=0.0,
                reinforcement_depth_bias=1,
                session_difficulty_signal=0.8,
            ),
        )

        def depth_spy(*args, **kwargs):
            depths.append(kwargs.get("depth_bias", 0))
            return 2 + kwargs.get("depth_bias", 0)

        monkeypatch.setattr(service, "_compute_reinforcement_depth", depth_spy)

        result = service._apply_balancing_if_needed(request, weak_units, selected_units)

        assert result
        assert set(depths) == {1}

    def test_reinforcement_insertion_is_confined_to_balanced_helper(self, monkeypatch):
        service = SessionService(SimpleNamespace(), random_seed=29)
        due_units = [(SimpleNamespace(id=idx), 1.0) for idx in range(1, 6)]

        monkeypatch.setattr(service, "_get_due_units_weighted", lambda *args, **kwargs: due_units)
        monkeypatch.setattr(service, "_select_balanced_units", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
        monkeypatch.setattr(service, "_compute_reinforcement_depth", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
        monkeypatch.setattr(service, "_compute_gain_adjustment", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

        selected = service._select_units_weighted_random(mode=StudyModeType.RECALL, due_only=True)

        assert {unit.id for unit in selected} == {unit.id for unit, _ in due_units}
        assert len({unit.id for unit in selected}) == len(selected)

    def test_helper_due_only_path_uses_weighted_sampling_not_production_balanced_branch(self, monkeypatch):
        service = SessionService(SimpleNamespace(), random_seed=31)
        due_units = [(SimpleNamespace(id=idx), 1.0) for idx in range(1, 9)]
        sample_calls = {"count": 0}

        monkeypatch.setattr(service, "_get_due_units_weighted", lambda *args, **kwargs: due_units)
        monkeypatch.setattr(
            service,
            "_select_balanced_units",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("helper due_only should not use balanced path")),
        )

        def sample_spy(units_with_weights, count, selected_ids):
            sample_calls["count"] += 1
            return SessionService._weighted_random_sample(service, units_with_weights, count, selected_ids)

        monkeypatch.setattr(service, "_weighted_random_sample", sample_spy)

        selected = service._select_units_weighted_random(mode=StudyModeType.RECALL, due_only=True)
        selected_ids = [unit.id for unit in selected]

        assert sample_calls["count"] == 1
        assert len(selected_ids) == len(due_units)
        assert len(selected_ids) == len(set(selected_ids))


class TestSeededFrequencyHarness:
    def test_seeded_weighted_sampling_shows_relative_weight_dominance(self):
        units = [
            (SimpleNamespace(id=1), 25.0),
            (SimpleNamespace(id=2), 20.0),
        ] + [
            (SimpleNamespace(id=idx), 1.0) for idx in range(3, 11)
        ]

        counts = {unit.id: 0 for unit, _ in units}
        service = SessionService(SimpleNamespace(), random_seed=37)

        for _ in range(200):
            selected = service._weighted_random_sample(units, 3, selected_ids=set())
            for unit in selected:
                counts[unit.id] += 1

        heavy_total = counts[1] + counts[2]
        light_totals = [counts[idx] for idx in range(3, 11)]

        assert counts[1] > max(light_totals)
        assert counts[2] > max(light_totals)
        assert heavy_total > sum(light_totals) / 2

    def test_overdue_multiplier_increases_selection_probability(self):
        now = session_service_module._utc_now_naive()
        service = SessionService(SimpleNamespace(), random_seed=41)

        def make_unit(unit_id: int, overdue_days: int):
            progress = SimpleNamespace(
                times_failed=1,
                times_correct=1,
                last_seen=now - timedelta(days=1),
                next_review_at=now - timedelta(days=overdue_days),
                last_recall_result=RecallResult.CORRECT,
                recall_fail_streak=1,
                stability_score=0.5,
            )
            return SimpleNamespace(id=unit_id, progress=progress)

        unit_a = make_unit(1, overdue_days=1)
        unit_b = make_unit(2, overdue_days=5)
        weighted_units = [
            (unit_a, service._compute_unit_weight(unit_a, "review", now)),
            (unit_b, service._compute_unit_weight(unit_b, "review", now)),
        ]

        counts = {1: 0, 2: 0}
        for _ in range(1000):
            selected = service._weighted_random_sample(weighted_units, 1, selected_ids=set())
            counts[selected[0].id] += 1

        assert counts[2] > counts[1]
