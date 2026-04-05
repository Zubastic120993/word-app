from datetime import timedelta
from urllib.parse import quote

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.routers.analytics import _compute_study_wow, build_study_activity_insight


TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=TEST_ENGINE)
    app.dependency_overrides[get_db] = override_get_db

    yield

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_dashboard_uses_clear_labels_and_compact_formatting(client, monkeypatch):
    def fake_get_theme_summary(_db, days):
        if days == 30:
            return {
                "themes": [
                    {
                        "theme": "core_communication",
                        "total_attempts": 20,
                        "first_try_success_rate": 0.05,
                        "average_attempts": 1.0,
                        "reveal_rate": 0.15,
                        "retry_rate": 0.95,
                        "resolved_attempt_count": 20,
                        "has_resolved_attempts": True,
                    }
                ]
            }
        if days == 7:
            return {
                "themes": [
                    {
                        "theme": "core_communication",
                        "total_attempts": 7,
                        "first_try_success_rate": 0.2,
                        "reveal_rate": 0.1,
                        "retry_rate": 0.8,
                    }
                ]
            }
        return {"themes": []}

    monkeypatch.setattr("app.routers.analytics.get_theme_summary", fake_get_theme_summary)

    def fake_study_metrics_since(_db, _since):
        return {
            "total_answers": 0,
            "correct_answers": 0,
            "incorrect_answers": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
        }

    monkeypatch.setattr(
        "app.routers.analytics.get_study_answer_metrics_since",
        fake_study_metrics_since,
    )

    def fake_cal_week(_db, anchor, week_offset=0):
        end_d = anchor.date() - timedelta(days=7 * week_offset)
        start_d = end_d - timedelta(days=6)
        return {
            "per_day": [0] * 7,
            "start_day": start_d,
            "end_day": end_d,
            "total_answers": 0,
            "correct_answers": 0,
            "incorrect_answers": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
        }

    monkeypatch.setattr(
        "app.routers.analytics.get_study_calendar_week_activity",
        fake_cal_week,
    )

    response = client.get("/analytics/dashboard")

    assert response.status_code == 200
    html = response.text
    assert "Your study activity" in html
    assert "All time:" in html
    assert "last 7" in html.lower()
    assert "calendar" in html.lower()
    assert "mini-chart" in html
    assert "Answers (30 days)" in html
    assert "Chat attempts (30 days)" in html
    assert "Themes with data" in html
    assert "Chat hint usage" in html
    assert "Hint use" not in html
    assert "Avg attempts" in html
    assert "Study more themes to unlock richer analytics insights." in html
    assert "Your recall accuracy is currently low." in html
    assert "Focus on repeating Core Communication before studying new themes." in html
    assert "5%" in html
    assert "15%" in html
    assert "95%" in html
    assert "1.0" in html
    assert "5.0%" not in html
    assert "15.0%" not in html
    assert "95.0%" not in html
    assert "1.00" not in html
    assert "1st try: ↑ Improving" in html
    assert "Hints: ↑ Improving" in html
    assert "Retry: ↑ Improving" in html
    assert "Composite: ↑ Improving" in html
    assert "Insufficient data" not in html


def test_compute_study_wow_volume_up_and_accuracy_flat():
    wow = _compute_study_wow(
        {"total_answers": 12, "success_rate": 0.5},
        {"total_answers": 10, "success_rate": 0.51},
    )
    assert wow["show_volume_line"] is True
    assert wow["volume_mode"] == "percent"
    assert wow["volume_arrow"] == "up"
    assert wow["show_accuracy_line"] is True
    assert wow["accuracy_insufficient"] is False
    assert wow["accuracy_arrow"] == "flat"


def test_compute_study_wow_volume_percent_clamped():
    wow = _compute_study_wow(
        {"total_answers": 310, "success_rate": 0.5},
        {"total_answers": 10, "success_rate": 0.5},
    )
    assert wow["volume_mode"] == "percent"
    assert wow["volume_pct"] == 200.0
    assert wow["volume_arrow"] == "up"
    assert "capped" in wow["volume_title"]


def test_compute_study_wow_volume_absolute_when_prior_small():
    wow = _compute_study_wow(
        {"total_answers": 8, "success_rate": 0.5},
        {"total_answers": 3, "success_rate": 0.5},
    )
    assert wow["volume_mode"] == "absolute"
    assert wow["volume_delta_abs"] == 5
    assert wow["volume_arrow"] == "up"


def test_compute_study_wow_accuracy_insufficient_when_min_sample_low():
    wow = _compute_study_wow(
        {"total_answers": 10, "success_rate": 0.8},
        {"total_answers": 4, "success_rate": 0.5},
    )
    assert wow["show_accuracy_line"] is True
    assert wow["accuracy_insufficient"] is True
    assert wow["accuracy_arrow"] == "insufficient"


def test_compute_study_wow_new_activity_vs_prior_empty_week():
    wow = _compute_study_wow(
        {"total_answers": 3, "success_rate": 0.66},
        {"total_answers": 0, "success_rate": 0.0},
    )
    assert wow["volume_arrow"] == "new"
    assert wow["show_accuracy_line"] is False


def test_compute_study_wow_no_line_when_both_weeks_empty():
    wow = _compute_study_wow(
        {"total_answers": 0, "success_rate": 0.0},
        {"total_answers": 0, "success_rate": 0.0},
    )
    assert wow["show_volume_line"] is False


def _wow_accuracy_down():
    return {
        "show_accuracy_line": True,
        "accuracy_insufficient": False,
        "accuracy_arrow": "down",
    }


def test_build_study_activity_insight_overdue_first():
    ins = build_study_activity_insight(
        answers_7d=0,
        answers_30d=5,
        answers_all_time=10,
        overdue_word_count=24,
        study_wow=_wow_accuracy_down(),
        ai_chat_attempts_30d=0,
        per_source_counts={
            "a.pdf": {"due_count": 12, "weak_count": 0},
            "b.pdf": {"due_count": 12, "weak_count": 0},
        },
    )
    assert ins is not None
    assert "24 overdue words" in ins["body"]
    assert "multiple sources" in ins["body"]
    assert "clearing your backlog" in ins["body"]
    assert ins["cta_href"] == "/study?due_only=true"


def test_build_study_activity_insight_overdue_concentrated_falls_back_if_source_blank():
    """Empty filename should not emit a broken source_pdfs link."""
    ins = build_study_activity_insight(
        answers_7d=0,
        answers_30d=1,
        answers_all_time=1,
        overdue_word_count=5,
        study_wow={},
        ai_chat_attempts_30d=10,
        per_source_counts={"": {"due_count": 5, "weak_count": 0}},
    )
    assert ins is not None
    assert "multiple sources" in ins["body"]
    assert ins["cta_href"] == "/study?due_only=true"


def test_build_study_activity_insight_per_source_overdue_when_concentrated():
    ins = build_study_activity_insight(
        answers_7d=0,
        answers_30d=5,
        answers_all_time=10,
        overdue_word_count=18,
        study_wow={},
        ai_chat_attempts_30d=10,
        per_source_counts={
            "file3.pdf": {"due_count": 18, "weak_count": 0},
            "other.pdf": {"due_count": 0, "weak_count": 0},
        },
    )
    assert ins is not None
    assert "Focus on" in ins["body"]
    assert "file3.pdf" in ins["body"]
    assert "18 overdue words need review" in ins["body"]
    assert ins["cta_href"] == (
        "/study?source_pdfs=" + quote("file3.pdf", safe="") + "&due_only=true"
    )


def test_build_study_activity_insight_overdue_singular_word():
    ins = build_study_activity_insight(
        answers_7d=1,
        answers_30d=1,
        answers_all_time=1,
        overdue_word_count=1,
        study_wow={},
        ai_chat_attempts_30d=10,
        per_source_counts={"deck.pdf": {"due_count": 1, "weak_count": 0}},
    )
    assert "Focus on" in ins["body"]
    assert "deck.pdf" in ins["body"]
    assert "1 overdue word needs review" in ins["body"]


def test_build_study_activity_insight_accuracy_drop_when_no_overdue():
    ins = build_study_activity_insight(
        answers_7d=10,
        answers_30d=20,
        answers_all_time=100,
        overdue_word_count=0,
        study_wow=_wow_accuracy_down(),
        ai_chat_attempts_30d=5,
        per_source_counts={
            "x.pdf": {"due_count": 0, "weak_count": 10},
        },
    )
    assert ins is not None
    assert "accuracy dropped" in ins["body"].lower()
    assert ins["cta_href"] == "/study?weak_only=true"


def test_build_study_activity_insight_per_source_weak_url_encodes_filename():
    name = "my vocab file.pdf"
    ins = build_study_activity_insight(
        answers_7d=1,
        answers_30d=2,
        answers_all_time=2,
        overdue_word_count=0,
        study_wow={"show_accuracy_line": False},
        ai_chat_attempts_30d=10,
        per_source_counts={name: {"due_count": 0, "weak_count": 5}},
    )
    assert ins is not None
    assert name in ins["body"]
    assert ins["cta_href"] == (
        "/study?source_pdfs=" + quote(name, safe="") + "&weak_only=true"
    )


def test_build_study_activity_insight_per_source_weak():
    ins = build_study_activity_insight(
        answers_7d=2,
        answers_30d=5,
        answers_all_time=5,
        overdue_word_count=0,
        study_wow={"show_accuracy_line": False},
        ai_chat_attempts_30d=10,
        per_source_counts={
            "file2.pdf": {"due_count": 0, "weak_count": 12},
            "z.pdf": {"due_count": 0, "weak_count": 3},
        },
    )
    assert ins is not None
    assert "file2.pdf" in ins["body"]
    assert "needs attention" in ins["body"]
    assert "12 weak words" in ins["body"]
    assert ins["cta_href"] == (
        "/study?source_pdfs=" + quote("file2.pdf", safe="") + "&weak_only=true"
    )


def test_build_study_activity_insight_quiet_week():
    ins = build_study_activity_insight(
        answers_7d=0,
        answers_30d=3,
        answers_all_time=0,
        overdue_word_count=0,
        study_wow={},
        ai_chat_attempts_30d=10,
        per_source_counts={"any.pdf": {"due_count": 0, "weak_count": 1}},
    )
    assert ins is not None
    assert "haven't studied this week" in ins["body"].lower()
    assert ins["cta_href"] == "/study"


def test_build_study_activity_insight_study_without_ai_chat_keep_going():
    ins = build_study_activity_insight(
        answers_7d=4,
        answers_30d=10,
        answers_all_time=10,
        overdue_word_count=0,
        study_wow={"show_accuracy_line": False},
        ai_chat_attempts_30d=0,
        per_source_counts={"u.pdf": {"due_count": 0, "weak_count": 1}},
    )
    assert ins is not None
    assert "4 answers" in ins["body"]
    assert "keep going" in ins["body"].lower()


def test_build_study_activity_insight_returns_none_when_no_signals():
    assert (
        build_study_activity_insight(
            answers_7d=5,
            answers_30d=5,
            answers_all_time=5,
            overdue_word_count=0,
            study_wow={"show_accuracy_line": False},
            ai_chat_attempts_30d=10,
            per_source_counts={"u.pdf": {"due_count": 0, "weak_count": 1}},
        )
        is None
    )


def test_dashboard_marks_all_trends_insufficient_when_7d_attempts_too_low(client, monkeypatch):
    def fake_get_theme_summary(_db, days):
        if days == 30:
            return {
                "themes": [
                    {
                        "theme": "core_communication",
                        "total_attempts": 12,
                        "first_try_success_rate": 0.5,
                        "average_attempts": 1.4,
                        "reveal_rate": 0.25,
                        "retry_rate": 0.5,
                        "resolved_attempt_count": 12,
                        "has_resolved_attempts": True,
                    }
                ]
            }
        if days == 7:
            return {
                "themes": [
                    {
                        "theme": "core_communication",
                        "total_attempts": 4,
                        "first_try_success_rate": 0.0,
                        "reveal_rate": 0.0,
                        "retry_rate": 0.0,
                    }
                ]
            }
        return {"themes": []}

    monkeypatch.setattr("app.routers.analytics.get_theme_summary", fake_get_theme_summary)

    monkeypatch.setattr(
        "app.routers.analytics.get_study_answer_metrics_since",
        lambda _db, _since: {
            "total_answers": 0,
            "correct_answers": 0,
            "incorrect_answers": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
        },
    )

    def fake_cal_week(_db, anchor, week_offset=0):
        end_d = anchor.date() - timedelta(days=7 * week_offset)
        start_d = end_d - timedelta(days=6)
        return {
            "per_day": [0] * 7,
            "start_day": start_d,
            "end_day": end_d,
            "total_answers": 0,
            "correct_answers": 0,
            "incorrect_answers": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
        }

    monkeypatch.setattr(
        "app.routers.analytics.get_study_calendar_week_activity",
        fake_cal_week,
    )

    response = client.get("/analytics/dashboard")

    assert response.status_code == 200
    html = response.text
    assert "1st try: Insufficient data" in html
    assert "Hints: Insufficient data" in html
    assert "Retry: Insufficient data" in html
    assert "Composite: Insufficient data" in html
    assert "1st try: ↑ Improving" not in html
    assert "Hints: ↑ Improving" not in html
    assert "Retry: ↑ Improving" not in html
    assert "Composite: ↑ Improving" not in html
