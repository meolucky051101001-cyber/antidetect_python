"""Recovery requires complete health, idle candidates and fresh headroom."""
from pathlib import Path

import pytest

from tools import soak_test as soak
from test_soak_resource_recovery import consider_recovery, make_runner


@pytest.mark.parametrize("defect", ["degraded", "process_unknown", "memory_unknown", "status_unknown"])
def test_sample_recovery_needs_complete_health(tmp_path, monkeypatch, defect):
    runner = make_runner(tmp_path, monkeypatch)
    runner._last_memory_shed_monotonic = 0
    runner._recovery_healthy_samples = 2
    monkeypatch.setattr(soak.time, "monotonic", lambda: 100)
    monkeypatch.setattr(runner, "_available_mb", lambda: None if defect == "memory_unknown" else 1400)
    monkeypatch.setattr(runner, "_save", lambda: None)
    monkeypatch.setattr(soak, "_append_jsonl", lambda *args: None)
    monkeypatch.setattr(runner, "_artifact_counts", lambda: {})
    monkeypatch.setattr(runner, "_start_profile", lambda *args, **kwargs: pytest.fail("incomplete health"))
    profiles = [{"id": p["id"], "status": "running" if p["active"] else "stopped",
                 "cdp_health": {"state": "healthy" if p["active"] else "stopped"}}
                for p in runner.profiles]
    if defect == "degraded":
        profiles[0]["cdp_health"]["state"] = "degraded"
    if defect == "status_unknown":
        profiles[0].pop("status")
    monkeypatch.setattr(soak, "_http", lambda base, method, path, **kwargs:
                        (200, {"application": "antidetect-python"} if path == "/health" else profiles, 0, None))
    monkeypatch.setattr(soak, "_chrome_processes", lambda dirs: {
        p["id"]: {"pids": [10] if p["active"] else [], "rss_mb": 1,
                   "observation_complete": defect != "process_unknown"}
        for p in runner.profiles})
    runner._sample()
    assert runner._recovery_healthy_samples == 0
    assert not any(p["active"] for p in runner.profiles[1:])


@pytest.mark.parametrize("defect", ["api_unknown", "process_unknown", "process_present", "stop_unknown"])
def test_recovery_needs_confirmed_idle_candidate(tmp_path, monkeypatch, defect):
    runner = make_runner(tmp_path, monkeypatch)
    runner.target_profiles = 2
    runner.profiles = runner.profiles[:2]
    runner._last_memory_shed_monotonic = 0
    monkeypatch.setattr(soak.time, "monotonic", lambda: 100)
    monkeypatch.setattr(runner, "_start_profile", lambda *args, **kwargs: pytest.fail("candidate not idle"))
    api = {"profile-2": {"status": "stopped"}}
    processes = {"profile-2": {"pids": [], "observation_complete": True}}
    if defect == "api_unknown":
        api.clear()
    elif defect == "process_unknown":
        processes["profile-2"]["observation_complete"] = False
    elif defect == "process_present":
        processes["profile-2"]["pids"] = [42]
    else:
        runner.profiles[1]["stop_unconfirmed"] = True
    for _ in range(3):
        assert not runner._consider_resource_recovery(
            healthy_sample=True, available_mb=1400, api_profiles=api,
            process_observations=processes)
    assert runner._recovery_healthy_samples == 0


def test_failed_attempts_have_cooldown_and_never_fan_out(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    runner._last_memory_shed_monotonic = 0
    now, calls = [100.0], []
    monkeypatch.setattr(soak.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner, "_start_profile", lambda p, **kwargs: calls.append(p["id"]) or False)
    for _ in range(10):
        consider_recovery(runner)
    assert calls == ["profile-2"]
    assert runner._recovery_healthy_samples == 0
    now[0] += 60
    for _ in range(3):
        consider_recovery(runner)
    assert calls == ["profile-2", "profile-2"]


@pytest.mark.parametrize("guard", ["stop_file", "critical", "stop_unknown_after_memory", "stop_file_after_memory"])
def test_launch_rechecks_stop_and_mutated_state(tmp_path, monkeypatch, guard):
    runner = make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(soak, "_http", lambda *args, **kwargs: pytest.fail("launch blocked"))
    monkeypatch.setattr(runner, "_available_mb", lambda: 1400)
    if guard == "stop_file":
        Path(runner.stop_path).parent.mkdir(parents=True)
        Path(runner.stop_path).touch()
    elif guard == "critical":
        runner.critical = True
    else:
        def memory():
            if guard == "stop_file_after_memory":
                Path(runner.stop_path).parent.mkdir(parents=True)
                Path(runner.stop_path).touch()
            else:
                runner.profiles[1]["stop_unconfirmed"] = True
            return 1400
        monkeypatch.setattr(runner, "_available_mb", memory)
    assert not runner._start_profile(runner.profiles[1], require_resources=True)
    assert not runner.profiles[1]["active"]


@pytest.mark.parametrize("available", [None, float("nan"), float("inf"), True, 899.9])
def test_launch_rejects_fresh_unverified_memory(tmp_path, monkeypatch, available):
    runner = make_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_available_mb", lambda: available)
    monkeypatch.setattr(soak, "_http", lambda *args, **kwargs: pytest.fail("no resource headroom"))
    assert not runner._start_profile(runner.profiles[1], require_resources=True)


def test_stop_discovered_in_sample_blocks_all_scheduled_work(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    runner._deadline_mono = 10000
    clock = iter([0, 1000])
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runner, "_sample", lambda: setattr(runner, "stop_requested", True))
    for operation in ("_tab_cycle", "_lifecycle_cycle", "_copy_cycle"):
        monkeypatch.setattr(runner, operation, lambda: pytest.fail("stop precedes scheduled work"))
    monkeypatch.setattr(soak.time, "sleep", lambda delay: pytest.fail("must exit immediately"))
    runner._loop()


def test_initial_memory_paused_ramp_recovers_without_a_shed(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    now, memory = [100.0], [800.0]
    monkeypatch.setattr(soak.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner, "_available_mb", lambda: memory[0])
    monkeypatch.setattr(soak, "_http", lambda *args, **kwargs: pytest.fail("initial low RAM"))
    assert not runner._start_profile(runner.profiles[1])
    assert runner._last_memory_shed_monotonic is None
    now[0], memory[0] = 160, 1400
    started = []
    monkeypatch.setattr(runner, "_start_profile", lambda profile, **kwargs: started.append(profile["id"]) or True)
    for _ in range(3):
        consider_recovery(runner)
    assert started == ["profile-2"]
