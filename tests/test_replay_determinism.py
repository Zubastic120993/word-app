from __future__ import annotations

import pytest

from app.config import settings
from tests.replay_harness import load_snapshot, run_replay


def test_replay_same_snapshot_same_seed_is_identical():
    snapshot = load_snapshot()

    first = run_replay(snapshot.seed)
    second = run_replay(snapshot.seed)

    assert first == second


def test_replay_same_snapshot_same_seed_is_stable_across_100_runs():
    snapshot = load_snapshot()

    results = [tuple(run_replay(snapshot.seed)) for _ in range(100)]

    assert len(set(results)) == 1


def test_replay_same_snapshot_different_seed_changes_selection():
    snapshot = load_snapshot()

    baseline = run_replay(snapshot.seed)
    variant = run_replay(snapshot.seed + 1)

    assert baseline != variant


@pytest.mark.parametrize(
    "controller_mode",
    ["legacy_adaptive", "observe_only", "v3_experimental"],
)
def test_replay_same_snapshot_same_seed_is_identical_across_controller_modes(monkeypatch, controller_mode):
    snapshot = load_snapshot()
    monkeypatch.setattr(settings, "recall_controller_mode", controller_mode)

    first = run_replay(snapshot.seed)
    second = run_replay(snapshot.seed)

    assert first == second
