"""Clock corrections cannot shorten or extend synthetic acceptance time."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tools import soak_test as soak


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(wall=1700000000.0, mono=1000.0)
    monkeypatch.setattr(soak.time, "time", lambda: value.wall)
    monkeypatch.setattr(soak.time, "monotonic", lambda: value.mono)
    return value


def runner_at_clock(tmp_path, hours):
    args = SimpleNamespace(hours=hours, profiles=1, min_active=1, port=1,
                           data_dir=str(tmp_path / "synthetic-data"),
                           results_dir=str(tmp_path / "results"), sample_interval=30,
                           tab_interval=1000000, cycle_interval=1000000, copy_interval=1000000,
                           min_free_mb=900, shed_free_mb=650)
    return soak.SoakRunner(args)


@pytest.mark.parametrize("hours", [2, 24])
@pytest.mark.parametrize("wall_jump", [7 * 86400, -7 * 86400])
def test_workload_deadline_uses_only_elapsed_monotonic_time(tmp_path, monkeypatch, clock, hours, wall_jump):
    runner = runner_at_clock(tmp_path, hours)
    original_deadline = runner.state["deadline_at"]
    assert original_deadline == datetime.fromtimestamp(clock.wall + hours * 3600, tz=timezone.utc).isoformat(timespec="seconds")
    assert runner._deadline_mono == clock.mono + hours * 3600
    sleeps, samples = [], []
    step = hours * 3600 / 4

    def advance(seconds):
        sleeps.append(seconds)
        clock.mono += step
        clock.wall += step + (wall_jump if len(sleeps) == 1 else 0)
        assert len(sleeps) <= 4, "A backward wall-clock correction must not extend the workload"

    monkeypatch.setattr(soak.time, "sleep", advance)
    monkeypatch.setattr(runner, "_check_stop_requested", lambda: None)
    monkeypatch.setattr(runner, "_sample", lambda: samples.append(clock.mono - runner._started_mono))
    monkeypatch.setattr(runner, "_tab_cycle", lambda: None)
    monkeypatch.setattr(runner, "_lifecycle_cycle", lambda: None)
    monkeypatch.setattr(runner, "_copy_cycle", lambda: None)
    runner._loop()
    assert sleeps == [1, 1, 1, 1], "A forward wall-clock correction must not end the workload early"
    assert samples[0] == 0
    assert clock.mono - runner._started_mono == hours * 3600
    assert runner.state["deadline_at"] == original_deadline


@pytest.mark.parametrize("hours", [2, 24])
@pytest.mark.parametrize("elapsed_fraction,wall_jump,completed", [
    (.5, 7 * 86400, False),
    (1.0, -7 * 86400, True),
])
def test_completion_and_duration_ignore_clock_corrections(
        tmp_path, monkeypatch, clock, hours, elapsed_fraction, wall_jump, completed):
    runner = runner_at_clock(tmp_path, hours)
    clock.wall += wall_jump
    elapsed = hours * 3600 * elapsed_fraction
    clock.mono += elapsed
    monkeypatch.setattr(runner, "_save", lambda: None)
    monkeypatch.setattr(runner, "_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_artifact_counts", lambda: {"staging_files": 0})
    monkeypatch.setattr(soak, "_chrome_processes", lambda directories: {})
    monkeypatch.setattr(soak, "_atomic_json", lambda *args: None)
    runner._finalize()
    summary = runner.state["summary"]
    assert summary["completed_duration"] is completed
    assert summary["duration_seconds"] == elapsed
    assert summary["passed"] is False, "Missing workload must still fail independent of elapsed time"


@pytest.mark.parametrize("wall_jump", [7 * 86400, -7 * 86400])
def test_sample_elapsed_seconds_uses_monotonic_clock(tmp_path, monkeypatch, clock, wall_jump):
    runner = runner_at_clock(tmp_path, 2)
    clock.wall += wall_jump
    clock.mono += 75.5
    monkeypatch.setattr(soak, "_http", lambda base, method, path, **kwargs: (
        200, {"application": "antidetect-python"} if path == "/health" else [], 0.0, None))
    monkeypatch.setattr(soak, "_chrome_processes", lambda directories: {})
    monkeypatch.setattr(soak, "_append_jsonl", lambda *args: None)
    monkeypatch.setattr(runner, "_save", lambda: None)
    monkeypatch.setattr(runner, "_available_mb", lambda: 2000)
    monkeypatch.setattr(runner, "_artifact_counts", lambda: {"staging_files": 0})
    runner._sample()
    assert runner.state["last_sample"]["elapsed_seconds"] == 75.5
