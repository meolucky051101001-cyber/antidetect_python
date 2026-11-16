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
    runner._lifecycle_cycles = 2
    runner._copy_cycles = 2
    runner.workload.register("profile-1")
    for _ in range(2):
        for realm in soak.CONTRACT_REALMS:
            runner.workload.record({
                "profile_id": "profile-1", "realm": realm,
                "checks": {name: True for name in soak.CONTRACT_CHECKS},
            })
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


def test_short_run_still_requires_explicit_minimum_cycles(finished_runner):
    finished_runner.hours = 0.01
    finished_runner._tabs_opened = 0
    finished_runner._lifecycle_cycles = 0
    finished_runner._copy_cycles = 0

    assert finished_runner._finalize() == 1
    summary = finished_runner.state["summary"]
    assert summary["workload_required"] == {"tab": False, "lifecycle": False, "copy": False}
    assert summary["workload_checks"]["tab"] is None
    assert summary["workload_checks"]["lifecycle"] is False
    assert summary["workload_checks"]["copy"] is False


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


def _contract_report(realm="page", profile_id="profile-1"):
    return {
        "profile_id": profile_id, "realm": realm,
        "checks": {name: True for name in soak.CONTRACT_CHECKS},
    }


def test_elapsed_soak_cannot_pass_without_canvas_and_worker_execution(finished_runner):
    finished_runner.workload = soak._WorkloadCollector()
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["workload_checks"]["canvas_workers"] is False


def test_each_started_profile_needs_every_contract_realm(finished_runner):
    finished_runner.profiles.append({"id": "profile-2", "name": "Soak 2", "active": False, "starts": 1})
    finished_runner.workload.register("profile-2")
    for _ in range(2):
        finished_runner.workload.record(_contract_report(profile_id="profile-2"))
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["workload_checks"]["canvas_workers"] is False


def test_contract_failure_prevents_pass_and_reports_only_aggregate(finished_runner):
    report = _contract_report("nested_worker")
    report["checks"]["drawPreserved"] = False
    assert finished_runner.workload.record(report)
    assert finished_runner._finalize() == 1
    workload = finished_runner.state["summary"]["canvas_worker_workload"]
    assert workload["executions"] == 7
    assert workload["failures"] == 1
    assert workload["profiles"]["profile-1"]["nested_worker"]["failed_checks"] == {"drawPreserved": 1}


@pytest.mark.parametrize("defect", ["pixels", "unknown-profile", "unknown-realm", "missing-check", "nonboolean"])
def test_collector_rejects_noncontract_or_raw_payloads(defect):
    collector = soak._WorkloadCollector()
    collector.register("profile-1")
    report = _contract_report()
    if defect == "pixels":
        report["pixels"] = [123, 234]
    elif defect == "unknown-profile":
        report["profile_id"] = "foreign-profile"
    elif defect == "unknown-realm":
        report["realm"] = "foreign-realm"
    elif defect == "missing-check":
        report["checks"].pop("readStable")
    else:
        report["checks"]["readStable"] = 1
    assert not collector.record(report)
    snapshot = collector.snapshot()
    assert snapshot["executions"] == 0
    assert snapshot["invalid_reports"] == 1
    assert "pixels" not in json.dumps(snapshot)


def test_stalled_contract_is_not_a_recent_execution(monkeypatch):
    collector = soak._WorkloadCollector()
    collector.register("profile-1")
    monkeypatch.setattr(soak.time, "monotonic", lambda: 100)
    for realm in soak.CONTRACT_REALMS:
        collector.record(_contract_report(realm))
    monkeypatch.setattr(soak.time, "monotonic", lambda: 221)
    assert set(collector.stale_realms("profile-1", 100)) == set(soak.CONTRACT_REALMS)
    assert collector.stale_realms("profile-1", 220) == []


def test_lifecycle_counter_counts_only_successful_restart(runner, monkeypatch):
    monkeypatch.setattr(runner, "_stop_profile", lambda *_args: True)
    monkeypatch.setattr(runner, "_start_profile", lambda *_args: False)
    runner._lifecycle_cycle()
    assert runner._lifecycle_cycles == 0


def test_workload_http_requires_same_origin_and_accepts_compact_report(tmp_path):
    import threading
    import urllib.error
    import urllib.request

    collector = soak._WorkloadCollector()
    collector.register("profile-1")
    server = soak._QuietSoakServer(("127.0.0.1", 0), soak._SoakPageHandler)
    server.workload = collector
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    try:
        body = json.dumps(_contract_report()).encode()
        request = urllib.request.Request(base + "/workload", data=body, headers={
            "Content-Type": "application/json", "Origin": "https://example.invalid",
        })
        with pytest.raises(urllib.error.HTTPError) as denied:
            soak.open_loopback(request, timeout=2)
        assert denied.value.code == 403
        request = urllib.request.Request(base + "/workload", data=body, headers={
            "Content-Type": "application/json", "Origin": base,
        })
        with soak.open_loopback(request, timeout=2) as response:
            assert response.status == 204
        with soak.open_loopback(base + "/soak?profile=profile-1", timeout=2) as response:
            html = response.read()
            assert b"/soak-workload.js" in html and b"/contract.js" in html
            assert response.headers["Cache-Control"] == "no-store"
        assert collector.snapshot()["executions"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
