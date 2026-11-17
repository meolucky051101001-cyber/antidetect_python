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
    monkeypatch.setattr(soak, "_chrome_processes", lambda directories: {
        profile_id: {"pids": [], "rss_mb": 0.0, "observation_complete": True}
        for profile_id in directories
    })
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
        "profile-1": {"pids": [123], "rss_mb": 10.0, "observation_complete": True},
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


def test_failed_stop_keeps_active_state_unconfirmed(runner, monkeypatch):
    monkeypatch.setattr(soak, "_http", lambda *a, **k: _response(
        409, {"success": False, "code": "STOP_FAILED", "error": "synthetic unconfirmed exit"}))
    monkeypatch.setattr(runner, "_wait_no_chrome", lambda *a, **k: pytest.fail("API failure is not confirmed"))
    profile = runner.profiles[0]
    assert not runner._stop_profile(profile, "cycle")
    assert profile["active"] is True
    assert profile["stop_unconfirmed"] is True
    assert profile.get("stops", 0) == 0


def test_successful_api_stop_with_remaining_children_preserves_state_and_evidence(runner, monkeypatch):
    monkeypatch.setattr(soak, "_http", lambda *a, **k: _response(200, {"success": True}))
    def remains(profile):
        profile["last_observed_pids"] = [424242, 424243]
        return False
    monkeypatch.setattr(runner, "_wait_no_chrome", remains)
    profile = runner.profiles[0]
    assert not runner._stop_profile(profile, "cycle")
    assert profile["active"] is True and profile["stop_unconfirmed"] is True
    assert profile.get("stops", 0) == 0
    assert runner.critical
    assert runner.errors[-1]["scope"] == "orphan_after_stop"
    assert "[424242, 424243]" in runner.errors[-1]["message"]


def test_lifecycle_never_restarts_after_failed_quiescence(runner, monkeypatch):
    def failed_stop(profile, reason):
        # Even a legacy observer that incorrectly cleared active cannot
        # authorize a new launch after the stop failed.
        profile["active"] = False
        runner.critical = True
        return False
    monkeypatch.setattr(runner, "_stop_profile", failed_stop)
    monkeypatch.setattr(runner, "_start_profile", lambda *a: pytest.fail("must not restart unconfirmed stop"))
    runner._lifecycle_cycle()
    assert runner._lifecycle_cycles == 0


def test_confirmed_stop_clears_unknown_state_and_counts_once(runner, monkeypatch):
    monkeypatch.setattr(soak, "_http", lambda *a, **k: _response(200, {"success": True}))
    monkeypatch.setattr(runner, "_wait_no_chrome", lambda *a: True)
    profile = runner.profiles[0]
    profile["stop_unconfirmed"] = True
    assert runner._stop_profile(profile, "cycle")
    assert profile["active"] is False
    assert profile["stop_unconfirmed"] is False
    assert profile["stops"] == 1


def test_missing_process_observer_cannot_confirm_stop(runner, monkeypatch):
    monkeypatch.setattr(soak, "psutil", None)
    profile = runner.profiles[0]
    assert not runner._wait_no_chrome(profile)
    assert profile["last_observed_pids"] is None


def test_samples_measure_bound_server_instead_of_venv_launcher(runner, monkeypatch):
    from types import SimpleNamespace
    runner.server = SimpleNamespace(pid=100)
    runner._server_observer = object()
    measured = []
    def metrics(process):
        measured.append(process)
        return {"pid": 101, "rss_mb": 80, "handles": 45, "threads": 8}
    monkeypatch.setattr(soak, "_process_metrics", metrics)
    monkeypatch.setattr(soak, "_http", lambda *a, **k: _response(200, {"application": "antidetect-python"}))
    monkeypatch.setattr(soak, "_chrome_processes", lambda directories: {
        profile_id: {"pids": [], "rss_mb": 0.0, "observation_complete": True}
        for profile_id in directories
    })
    monkeypatch.setattr(runner, "_available_mb", lambda: 2000)
    runner._sample()
    assert measured == [runner._server_observer]
    assert runner.state["last_sample"]["server"]["pid"] == 101


@pytest.mark.parametrize("cooldown,passed", [
    ({"rss_mb": 90, "handles": 55, "threads": 10}, True),
    ({"rss_mb": 900, "handles": 55, "threads": 10}, False),
    ({"rss_mb": 90, "handles": 900, "threads": 10}, False),
    ({"rss_mb": 90, "handles": 55, "threads": 90}, False),
    (None, False),
])
def test_soak_cooldown_requires_predeclared_server_budget(finished_runner, monkeypatch, cooldown, passed):
    finished_runner._server_baseline = {"rss_mb": 70, "handles": 40, "threads": 8}
    finished_runner.state["resource_budget"] = {"rss_mb_delta": 128, "handles_delta": 128, "threads_delta": 16}
    monkeypatch.setattr(soak, "_process_metrics", lambda *a: cooldown)
    monkeypatch.setattr(soak.time, "sleep", lambda *a: None)
    assert finished_runner._finalize() == (0 if passed else 1)
    assert finished_runner.state["summary"]["server_cooldown"]["passed"] == passed


def test_unknown_directory_scan_cannot_confirm_profile_idle(runner, monkeypatch):
    monkeypatch.setattr(soak, "browser_pids_using_dir", lambda directory: None)
    observation = soak._chrome_processes({"profile-1": "synthetic-directory"})["profile-1"]
    assert observation["observation_complete"] is False
    assert observation["pids"] == []
    assert not runner._wait_no_chrome(runner.profiles[0])
    assert runner.profiles[0]["last_observed_pids"] is None


def test_partial_directory_scan_prevents_soak_acceptance(finished_runner, monkeypatch):
    monkeypatch.setattr(soak, "_chrome_processes", lambda directories: {
        profile_id: {"pids": [], "rss_mb": 0.0, "observation_complete": False}
        for profile_id in directories
    })
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["cleanup_checks"]["process_observation_complete"] is False


@pytest.mark.parametrize("boundary", ["process", "children", "memory"])
def test_browser_resource_access_denied_keeps_observation_incomplete(monkeypatch, boundary):
    from types import SimpleNamespace
    monkeypatch.setattr(soak, "browser_pids_using_dir", lambda directory: {11})

    def denied(*args, **kwargs):
        raise soak.psutil.AccessDenied(pid=11)

    process = SimpleNamespace(pid=11, is_running=lambda: True,
                              children=denied if boundary == "children" else lambda **kwargs: [],
                              memory_info=denied if boundary == "memory" else lambda: SimpleNamespace(rss=1048576))
    monkeypatch.setattr(soak.psutil, "Process", denied if boundary == "process" else lambda pid: process)
    observation = soak._chrome_processes({"profile-1": "synthetic-directory"})["profile-1"]
    assert observation["observation_complete"] is False


def test_browser_rss_includes_descendants_once_and_omits_unmeasured_cpu(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(soak, "browser_pids_using_dir", lambda directory: {11, 12})
    child = SimpleNamespace(pid=12, is_running=lambda: True,
                            children=lambda **kwargs: [], memory_info=lambda: SimpleNamespace(rss=2 * 1048576))
    parent = SimpleNamespace(pid=11, is_running=lambda: True,
                             children=lambda **kwargs: [child], memory_info=lambda: SimpleNamespace(rss=3 * 1048576))
    monkeypatch.setattr(soak.psutil, "Process", lambda pid: {11: parent, 12: child}[pid])
    observation = soak._chrome_processes({"profile-1": "synthetic-directory"})["profile-1"]
    assert observation["observation_complete"] is True
    assert observation["pids"] == [11, 12]
    assert observation["rss_mb"] == 5
    assert observation["cpu_percent"] is None


def test_bound_process_pid_reuse_never_reads_replacement_metrics():
    from types import SimpleNamespace

    def must_not_measure():
        pytest.fail("Reused PID must not be read as the original bound process")

    process = SimpleNamespace(pid=11, is_running=lambda: False, memory_info=must_not_measure)
    assert soak._process_metrics(process) is None


def test_dashboard_exit_requires_real_server_after_wrapper_exits(runner):
    from types import SimpleNamespace
    runner.server = SimpleNamespace(poll=lambda: 0)
    runner._server_observer = SimpleNamespace(is_running=lambda: True)
    assert runner._dashboard_exited() is False
    runner._server_observer = SimpleNamespace(is_running=lambda: False)
    assert runner._dashboard_exited() is True


def test_dashboard_unknown_bound_process_is_not_stopped(runner):
    from types import SimpleNamespace

    def denied():
        raise soak.psutil.AccessDenied(pid=11)

    runner.server = SimpleNamespace(poll=lambda: 0)
    runner._server_observer = SimpleNamespace(is_running=denied)
    assert runner._dashboard_exited() is False


def test_finalize_shuts_down_real_server_when_wrapper_already_exited(finished_runner, monkeypatch):
    from types import SimpleNamespace
    state = {"running": True, "shutdowns": 0, "waited": False}

    def shutdown(base, method, path, **kwargs):
        assert path == "/api/system/shutdown"
        state["shutdowns"] += 1
        state["running"] = False
        return _response(200, {"success": True})

    def observed_wait(timeout):
        state["waited"] = True
        assert state["running"] is False

    finished_runner.server = SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    finished_runner._server_observer = SimpleNamespace(is_running=lambda: state["running"], wait=observed_wait)
    monkeypatch.setattr(soak, "_http", shutdown)
    assert finished_runner._finalize() == 0
    assert state["shutdowns"] == 1 and state["waited"] is True
    assert finished_runner.state["summary"]["cleanup_checks"]["dashboard_exited"] is True


def test_missing_profile_observation_never_passes_complete_cleanup(finished_runner, monkeypatch):
    monkeypatch.setattr(soak, "_chrome_processes", lambda directories: {})
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["cleanup_checks"]["process_observation_complete"] is False


def test_bound_server_cleanup_denied_preserves_failed_summary(finished_runner, monkeypatch):
    from types import SimpleNamespace

    def wait(timeout):
        raise soak.psutil.TimeoutExpired(timeout, pid=11)

    def terminate():
        raise soak.psutil.AccessDenied(pid=11)

    finished_runner.server = SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    finished_runner._server_observer = SimpleNamespace(is_running=lambda: True, wait=wait, terminate=terminate)
    monkeypatch.setattr(soak, "_http", lambda *args, **kwargs: _response(200, {"success": True}))
    assert finished_runner._finalize() == 1
    assert finished_runner.state["summary"]["cleanup_checks"]["dashboard_exited"] is False
    assert any(record["scope"] == "server_cleanup" for record in finished_runner.errors)
