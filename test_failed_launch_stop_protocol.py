"""Failed launches use normal stop semantics on synthetic local processes."""
from types import SimpleNamespace

import pytest

import antidetect.launcher as launcher_module
from antidetect.cdp_health import REASON_SOCKET_CLOSED, REASON_STOP_FAILED
from test_launcher_directory_quiescence import directory_launcher  # noqa: F401


def test_failed_cleanup_marks_stopping_before_owned_browser_close(directory_launcher, monkeypatch):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a: set())
    calls, exited = [], []

    def close():
        assert launcher.cdp_health.snapshot(profile_id)["state"] == "stopping"
        assert not launcher.cdp_health.mark_degraded(profile_id, launch_id, REASON_SOCKET_CLOSED)
        calls.append("close")
        exited.append(True)
        return True

    session = launcher.cdp_health.bind_session(profile_id, launch_id)
    assert session.bind_browser_close(close)
    proc = SimpleNamespace(
        pid=424242, poll=lambda: 0 if exited else None,
        wait=lambda **kwargs: calls.append(("wait", kwargs["timeout"])),
        terminate=lambda: pytest.fail("graceful close should exit"),
        kill=lambda: pytest.fail("graceful close should exit"),
    )
    launcher.active_processes[profile_id] = proc
    diagnostics = {}
    # Launch already owns both locks when it enters failed-launch cleanup.
    with launcher._lifecycle_locks.acquire(profile_id):
        with launcher._launch_locks[profile_id]:
            assert launcher._cleanup_failed_launch(profile_id, proc, launch_id, diagnostics=diagnostics)
    assert calls == ["close", ("wait", 5)]
    assert diagnostics["protocol"] == "ordinary_stop"
    assert diagnostics["code"] is None and diagnostics["elapsed_ms"] >= 0
    assert launcher._runtime_store.get(profile_id) is None
    assert profile_id not in launcher.active_processes
    assert sentinel.exists()


@pytest.mark.parametrize("replacement", ["popen", "health", "runtime-owner"])
def test_failed_cleanup_rejects_replacement_before_process_operation(directory_launcher, monkeypatch, replacement):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    old = SimpleNamespace(pid=424242, poll=lambda: pytest.fail("stale process must not be polled"))
    launcher.active_processes[profile_id] = old
    if replacement == "popen":
        launcher.active_processes[profile_id] = SimpleNamespace(pid=424242)
    elif replacement == "health":
        launcher.cdp_health.begin_launch(profile_id, launcher.instance_id)
    else:
        launcher._runtime_store.upsert(profile_id, owner_instance="owner-B")
    expected_record = launcher._runtime_store.get(profile_id)
    expected_health = launcher.cdp_health.snapshot(profile_id)
    expected_proc = launcher.active_processes[profile_id]
    monkeypatch.setattr(launcher, "_stop_impl", lambda *_a: pytest.fail("replacement must not enter stop"))
    diagnostics = {}
    assert not launcher._cleanup_failed_launch(profile_id, old, launch_id, diagnostics=diagnostics)
    assert diagnostics["code"] == "SESSION_CHANGED"
    assert launcher._runtime_store.get(profile_id) == expected_record
    assert launcher.cdp_health.snapshot(profile_id)["state"] == expected_health["state"]
    assert launcher.active_processes[profile_id] is expected_proc
    assert sentinel.exists()


def test_runtime_replaced_during_close_survives_cleanup_cas(directory_launcher, monkeypatch):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a: set())
    exited, replacements = [], []

    def close():
        replacements.append(launcher._runtime_store.upsert(
            profile_id, launch_id="replacement", owner_instance="owner-B", pid=424243,
        ))
        exited.append(True)
        return True

    session = launcher.cdp_health.bind_session(profile_id, launch_id)
    assert session.bind_browser_close(close)
    proc = SimpleNamespace(
        pid=424242, poll=lambda: 0 if exited else None, wait=lambda **_k: 0,
        terminate=lambda: pytest.fail("old process already exited"),
        kill=lambda: pytest.fail("old process already exited"),
    )
    launcher.active_processes[profile_id] = proc
    diagnostics = {}
    assert not launcher._cleanup_failed_launch(profile_id, proc, launch_id, diagnostics=diagnostics)
    assert diagnostics["code"] == "SESSION_CHANGED"
    assert launcher._runtime_store.get(profile_id) == replacements[0]
    assert launcher.active_processes[profile_id] is proc
    assert sentinel.exists()


@pytest.mark.parametrize("observed", [None, {424243}, set()], ids=["unknown", "child-running", "idle"])
def test_missing_runtime_cleanup_still_checks_known_profile_directory(directory_launcher, monkeypatch, observed):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    launcher._runtime_store.remove(profile_id)
    proc = SimpleNamespace(pid=424242, poll=lambda: 0)
    launcher.active_processes[profile_id] = proc
    scans = []

    def scan(directory):
        scans.append(directory)
        return observed

    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", scan)
    monkeypatch.setattr(
        launcher, "_wait_directory_idle",
        lambda directory, timeout=5: launcher_module.BrowserLauncher._wait_directory_idle(directory, timeout=0),
    )
    cleaned = launcher._cleanup_failed_launch(profile_id, proc, launch_id)
    assert cleaned is (observed == set())
    assert str(sentinel.parent) in scans
    assert launcher._runtime_store.get(profile_id) is None
    if observed is None or observed:
        assert launcher.active_processes[profile_id] is proc
        assert launcher.cdp_health.snapshot(profile_id)["reason"] == REASON_STOP_FAILED
    else:
        assert profile_id not in launcher.active_processes
    assert sentinel.exists()


@pytest.mark.parametrize("observed,state", [(None, "unknown"), ({424243}, "known_processes_present")])
def test_directory_failure_log_reports_scan_kind_and_elapsed(monkeypatch, tmp_path, caplog, observed, state):
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a: observed)
    assert not launcher_module.BrowserLauncher._wait_directory_idle(str(tmp_path), timeout=0)
    assert "directory_state=" + state in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "424243" not in caplog.text


@pytest.mark.parametrize("failure", ["read", "persist"])
def test_cleanup_metadata_failure_returns_false_and_retains_handle(directory_launcher, monkeypatch, failure):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    proc = SimpleNamespace(pid=424242, poll=lambda: 0)
    launcher.active_processes[profile_id] = proc
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a: None)
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: False)

    def denied(*_a, **_k):
        raise PermissionError("synthetic metadata failure")

    monkeypatch.setattr(launcher._runtime_store, "get" if failure == "read" else "upsert_if_owner", denied)
    diagnostics = {}
    assert not launcher._cleanup_failed_launch(profile_id, proc, launch_id, diagnostics=diagnostics)
    assert diagnostics["code"] == ("CLEANUP_EXCEPTION" if failure == "read" else "STOP_FAILED")
    assert launcher.active_processes[profile_id] is proc
    assert launcher.cdp_health.snapshot(profile_id)["reason"] == REASON_STOP_FAILED
    assert sentinel.exists()
