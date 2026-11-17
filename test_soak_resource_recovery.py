"""Synthetic hysteresis tests for re-ramping after memory shedding."""
from types import SimpleNamespace

import pytest

from tools import soak_test as soak


def make_runner(tmp_path, monkeypatch):
    args = SimpleNamespace(hours=1, profiles=3, min_active=1, port=1,
                           data_dir=str(tmp_path / "data"), results_dir=str(tmp_path / "results"),
                           sample_interval=15, tab_interval=60, cycle_interval=180,
                           copy_interval=240, min_free_mb=900, shed_free_mb=650)
    runner = soak.SoakRunner(args)
    runner.profiles = [
        {"id": "profile-1", "name": "One", "active": True, "starts": 1},
        {"id": "profile-2", "name": "Two", "active": False, "starts": 1},
        {"id": "profile-3", "name": "Three", "active": False, "starts": 0},
    ]
    monkeypatch.setattr(runner, "_event", lambda *args, **kwargs: None)
    return runner


def consider_recovery(runner, *, healthy_sample=True, available_mb=1400):
    api_profiles = {
        profile["id"]: {"status": "running" if profile["active"] else "stopped"}
        for profile in runner.profiles
    }
    observations = {
        profile["id"]: {
            "observation_complete": True,
            "pids": [index + 10] if profile["active"] else [],
        }
        for index, profile in enumerate(runner.profiles)
    }
    return runner._consider_resource_recovery(
        healthy_sample=healthy_sample,
        available_mb=available_mb,
        api_profiles=api_profiles,
        process_observations=observations,
    )


def test_samples_recover_shed_capacity_so_required_cycles_can_run(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    runner.target_profiles = 2
    runner.profiles = runner.profiles[:2]
    runner.profiles[1]["active"] = True
    runner.soak_base = "http://127.0.0.1:2"
    now, memory = [100.0], [600.0]
    monkeypatch.setattr(soak.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner, "_available_mb", lambda: memory[0])
    monkeypatch.setattr(runner, "_save", lambda: None)
    monkeypatch.setattr(soak, "_append_jsonl", lambda *args: None)
    monkeypatch.setattr(runner, "_artifact_counts", lambda: {"staging_files": 0})
    monkeypatch.setattr(soak, "_chrome_processes", lambda dirs: {
        profile["id"]: {"pids": [index + 10] if profile["active"] else [],
                        "rss_mb": index + 1, "observation_complete": True}
        for index, profile in enumerate(runner.profiles)})

    def stop(profile, reason):
        profile["active"] = False
        return True

    def start(profile, **kwargs):
        profile["active"] = True
        profile["starts"] += 1
        return True

    def http(base, method, path, **kwargs):
        if path == "/health":
            result = {"application": "antidetect-python"}
        elif path == "/api/profiles":
            result = [{"id": profile["id"], "status": "running" if profile["active"] else "stopped",
                       "cdp_health": {"state": "healthy" if profile["active"] else "stopped"}}
                      for profile in runner.profiles]
        elif "/duplicate?" in path:
            result = {"id": "copy-fixture"}
        elif method == "DELETE":
            result = {"success": True}
        else:
            raise AssertionError("Unexpected synthetic request")
        return 200, result, 0.0, None

    monkeypatch.setattr(runner, "_stop_profile", stop)
    monkeypatch.setattr(runner, "_start_profile", start)
    monkeypatch.setattr(soak, "_http", http)
    runner._shed_one()
    assert runner._shed_count == 1
    assert sum(profile["active"] for profile in runner.profiles) == 1
    memory[0] = 1400
    now[0] = 160
    for _ in range(3):
        runner._sample()
        now[0] += 15
    assert sum(profile["active"] for profile in runner.profiles) == 2
    runner._lifecycle_cycle()
    assert runner._lifecycle_cycles == 1
    fixture = tmp_path / "data" / "profiles" / "copy-fixture" / "user_data" / "synthetic"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"synthetic copy fixture")
    runner._copy_cycle()
    assert runner._copy_cycles == 1


def test_recovered_capacity_restores_one_profile_after_hysteresis(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    now = [200.0]
    monkeypatch.setattr(soak.time, "monotonic", lambda: now[0])
    runner._last_memory_shed_monotonic = 140.0
    started = []

    def start(profile, **kwargs):
        started.append((profile["id"], kwargs))
        profile["active"] = True
        profile["starts"] += 1
        return True

    monkeypatch.setattr(runner, "_start_profile", start)
    assert consider_recovery(runner) is False
    assert consider_recovery(runner) is False
    assert started == []
    assert consider_recovery(runner) is True
    assert started == [("profile-2", {"require_resources": True})]
    assert sum(profile["active"] for profile in runner.profiles) == 2
    assert consider_recovery(runner) is False
    assert len(started) == 1


@pytest.mark.parametrize("healthy,available", [
    (False, 1400), (True, None), (True, float("nan")), (True, float("inf")),
    (True, -float("inf")), (True, True), (True, 899.9),
])
def test_unhealthy_or_unmeasured_capacity_resets_recovery_streak(tmp_path, monkeypatch, healthy, available):
    runner = make_runner(tmp_path, monkeypatch)
    runner._last_memory_shed_monotonic = 100.0
    consider_recovery(runner)
    consider_recovery(runner)
    assert consider_recovery(runner, healthy_sample=healthy, available_mb=available) is False
    assert runner._recovery_healthy_samples == 0
    assert all(not profile["active"] for profile in runner.profiles[1:])


def test_cooldown_and_incomplete_stop_block_recovery(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    now = [200.0]
    monkeypatch.setattr(soak.time, "monotonic", lambda: now[0])
    runner._last_memory_shed_monotonic = 180.0
    runner.profiles[1]["stop_unconfirmed"] = True
    calls = []
    monkeypatch.setattr(runner, "_start_profile", lambda profile, **kwargs: calls.append(profile["id"]))
    for _ in range(3):
        consider_recovery(runner)
    assert calls == []
    assert runner._recovery_healthy_samples == 0
    runner.profiles[1]["stop_unconfirmed"] = False
    for _ in range(3):
        consider_recovery(runner)
    assert calls == [], "A shed needs the full cooldown"
    now[0] = 240
    consider_recovery(runner)
    consider_recovery(runner)
    consider_recovery(runner)
    assert calls == ["profile-2"]


def test_fresh_memory_drop_after_sample_prevents_recovery_launch(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    fresh = [1400.0]
    monkeypatch.setattr(runner, "_available_mb", lambda: fresh[0])
    calls = []

    def http(*args, **kwargs):
        calls.append((args, kwargs))
        return 200, {"success": True, "pid": 99, "port": 9333, "cdp_health": {"state": "healthy"}}, 1, None

    monkeypatch.setattr(soak, "_http", http)
    fresh[0] = 649.9
    assert runner._start_profile(runner.profiles[1], require_resources=True) is False
    assert calls == []
    assert not runner.profiles[1]["active"]


def test_active_minimum_is_never_stopped_for_lifecycle_or_copy(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runner, "_stop_profile", lambda *args: calls.append(args))
    runner._lifecycle_cycle()
    runner._copy_cycle()
    assert calls == []
    assert runner._lifecycle_cycles == 0
    assert runner._copy_cycles == 0
