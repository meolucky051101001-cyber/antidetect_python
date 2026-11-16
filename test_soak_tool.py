"""Regression tests cho retry và log lỗi 409 của soak runner."""
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pytest

import tools.soak_test as soak


class _Args:
    hours = 1.0
    profiles = 1
    min_active = 0
    port = 1
    data_dir = ""
    results_dir = ""
    sample_interval = 30.0
    tab_interval = 300.0
    cycle_interval = 600.0
    copy_interval = 21600.0
    shed_free_mb = 650.0


@pytest.fixture
def runner(tmp_path, monkeypatch):
    args = _Args()
    args.data_dir = str(tmp_path / "data")
    args.results_dir = str(tmp_path / "results")
    os.makedirs(args.results_dir, exist_ok=True)
    instance = soak.SoakRunner(args)
    instance.base = "http://127.0.0.1:1"
    instance.token = "token"
    instance.soak_base = "http://127.0.0.1:2"
    instance.profiles = [
        {"id": "profile-1", "name": "Soak 1", "active": True, "port": 9555}
    ]
    monkeypatch.setattr(soak, "OPEN_TAB_RETRY_DELAY", 0)
    return instance


def _response(status, body):
    return status, body, 0.01, None if status == 200 else f"HTTP {status}"


def test_open_tab_409_logs_code_and_detail(runner, monkeypatch):
    monkeypatch.setattr(
        soak,
        "_http",
        lambda *_a, **_k: _response(
            409,
            {
                "success": False,
                "code": "RECOVERY_REQUIRED",
                "detail": "CDP chưa được xác minh quyền điều khiển.",
            },
        ),
    )
    ok, failure, code = runner._open_soak_tab(runner.profiles[0], "http://soak/x")
    assert ok is False
    assert code == "RECOVERY_REQUIRED"
    assert "HTTP 409" in failure
    assert "CDP chưa được xác minh quyền điều khiển." in failure


def test_open_tab_retries_transient_conflict_once(runner, monkeypatch):
    calls = {"count": 0}

    def fake_http(*_a, **_k):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(
                409,
                {
                    "success": False,
                    "code": "CDP_UNHEALTHY",
                    "detail": "CDP không khỏe",
                },
            )
        return _response(200, {"success": True})

    monkeypatch.setattr(soak, "_http", fake_http)
    ok, failure, code = runner._open_soak_tab(runner.profiles[0], "http://soak/x")
    assert ok is True
    assert failure is None and code is None
    assert calls["count"] == 2
    events = [
        json.loads(line) for line in open(runner.events_path, encoding="utf-8")
    ]
    assert any(event.get("event") == "open_tab_retry_ok" for event in events)


def test_open_tab_does_not_retry_terminal_conflict(runner, monkeypatch):
    calls = {"count": 0}

    def fake_http(*_a, **_k):
        calls["count"] += 1
        return _response(
            409,
            {
                "success": False,
                "code": "DETACHED_NO_CDP",
                "detail": "Chrome chạy detached.",
            },
        )

    monkeypatch.setattr(soak, "_http", fake_http)
    ok, failure, code = runner._open_soak_tab(runner.profiles[0], "http://soak/x")
    assert ok is False
    assert code == "DETACHED_NO_CDP"
    assert calls["count"] == 1


def test_tab_cycle_records_error_code_after_retry_fails(runner, monkeypatch):
    monkeypatch.setattr(
        soak,
        "_http",
        lambda *_a, **_k: _response(
            409,
            {
                "success": False,
                "code": "CDP_UNHEALTHY",
                "detail": "CDP không khỏe",
            },
        ),
    )
    monkeypatch.setattr(soak, "_close_soak_tabs", lambda *_a, **_k: (0, 0, None))
    runner._tab_cycle()
    assert len(runner.errors) == 1
    record = runner.errors[0]
    assert record["scope"] == "open_tab"
    assert record["code"] == "CDP_UNHEALTHY"
    assert "CDP không khỏe" in record["message"]


@pytest.fixture
def finished_runner(runner, monkeypatch):
    runner.hours = 24.0
    runner._deadline_ts = time.time() - 1
    runner.profiles[0].update(active=False, starts=2)
    runner._sample_count = 10
    runner._healthy_active_samples = 10
    runner._tabs_opened = 2
    runner._lifecycle_cycles = 1
    runner._copy_cycles = 1
    monkeypatch.setattr(soak, "_chrome_processes", lambda *_a: {})
    return runner


def test_elapsed_deadline_without_browser_workload_fails(finished_runner):
    finished_runner.profiles = []
    finished_runner._sample_count = 0
    finished_runner._healthy_active_samples = 0
    finished_runner._tabs_opened = 0
    finished_runner._lifecycle_cycles = 0
    finished_runner._copy_cycles = 0

    assert finished_runner._finalize() == 1
    summary = finished_runner.state["summary"]
    assert summary["completed_duration"] is True
    assert summary["passed"] is False
    assert summary["workload_checks"]["profile_started"] is False
    assert summary["workload_checks"]["healthy_active_sample"] is False


@pytest.mark.parametrize("counter,check", [
    ("_healthy_active_samples", "healthy_active_sample"),
    ("_tabs_opened", "tab"),
    ("_lifecycle_cycles", "lifecycle"),
    ("_copy_cycles", "copy"),
])
def test_scheduled_workload_must_be_exercised(finished_runner, counter, check):
    setattr(finished_runner, counter, 0)
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["workload_checks"][check] is False


def test_memory_adapted_workload_can_pass_with_observed_browser(finished_runner):
    finished_runner.target_profiles = 2
    finished_runner.min_active = 1
    finished_runner.profiles.append({"id": "profile-2", "name": "Soak 2", "active": False})
    finished_runner._shed_count = 1

    assert finished_runner._finalize() == 0
    summary = finished_runner.state["summary"]
    assert summary["passed"] is True
    assert summary["memory_shed"] == 1
    assert all(summary["cleanup_checks"].values())


def test_short_run_reports_exercises_outside_its_duration(finished_runner):
    finished_runner.hours = 0.01
    finished_runner._tabs_opened = 0
    finished_runner._lifecycle_cycles = 0
    finished_runner._copy_cycles = 0

    assert finished_runner._finalize() == 0
    summary = finished_runner.state["summary"]
    assert summary["workload_required"] == {"tab": False, "lifecycle": False, "copy": False}
    assert summary["workload_checks"]["tab"] is None
    assert summary["workload_checks"]["lifecycle"] is None
    assert summary["workload_checks"]["copy"] is None


def test_unfinished_staging_prevents_soak_acceptance(finished_runner, monkeypatch):
    monkeypatch.setattr(finished_runner, "_artifact_counts", lambda: {"staging_files": 1})
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["cleanup_checks"]["staging_empty"] is False


@pytest.mark.parametrize("health,expected", [("healthy", 1), ("degraded", 0), (None, 0)])
def test_samples_require_verified_browser_health(runner, monkeypatch, health, expected):
    def fake_http(_base, _method, path, **_kwargs):
        if path == "/health":
            return _response(200, {"application": "antidetect-python"})
        return _response(200, [{"id": "profile-1", "status": "running", "cdp_health": {"state": health}}])

    monkeypatch.setattr(soak, "_http", fake_http)
    monkeypatch.setattr(soak, "_chrome_processes", lambda *_a: {
        "profile-1": {"pids": [123], "rss_mb": 10.0},
    })
    monkeypatch.setattr(runner, "_available_mb", lambda: 2000)

    runner._sample()

    assert runner.state["counters"]["samples"] == 1
    assert runner.state["counters"]["healthy_active_samples"] == expected
