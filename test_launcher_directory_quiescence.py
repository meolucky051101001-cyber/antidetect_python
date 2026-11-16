"""Synthetic parent/child exit and graceful-close regression boundaries."""
from types import SimpleNamespace

import pytest

import antidetect.launcher as launcher_module
from antidetect.launcher import BrowserLauncher
from test_runtime_generation_cleanup import fixture_launcher


@pytest.fixture
def directory_launcher(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    profile_id = "profile-1"
    data_dir = tmp_path / "profiles" / profile_id / "user_data"
    data_dir.mkdir(parents=True)
    sentinel = data_dir / "synthetic-session.txt"
    sentinel.write_text("preserve synthetic data", encoding="utf-8")
    monkeypatch.setattr(launcher_module, "PROFILES_DATA_DIR", str(tmp_path / "profiles"))
    launch_id = launcher.cdp_health.begin_launch(profile_id, launcher.instance_id)
    launcher.cdp_health.set_process(profile_id, launch_id, 424242)
    launcher._runtime_store.upsert(
        profile_id, pid=424242, launch_id=launch_id, owner_instance=launcher.instance_id,
        user_data_dir=str(data_dir), port=9333,
    )
    launcher.active_ports[profile_id] = 9333
    launcher._snapshot_tabs_via_http = lambda *a: []
    return launcher, profile_id, launch_id, sentinel


def _immediate_scan(launcher, monkeypatch, observed):
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a, **_k: observed)
    monkeypatch.setattr(
        launcher, "_wait_directory_idle",
        lambda directory, timeout=5: BrowserLauncher._wait_directory_idle(directory, timeout=0),
    )


@pytest.mark.parametrize("observed", [{424243}, None], ids=["child-running", "scan-unknown"])
@pytest.mark.parametrize("branch", ["parent-exited", "identity-absent", "missing-pid"])
def test_stop_refuses_success_without_directory_quiescence(directory_launcher, monkeypatch, observed, branch):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    _immediate_scan(launcher, monkeypatch, observed)
    if branch == "parent-exited":
        launcher.active_processes[profile_id] = SimpleNamespace(pid=424242, poll=lambda: 0)
    elif branch == "identity-absent":
        monkeypatch.setattr(launcher, "_identity_for_stop", lambda *a: {"state": "absent"})
    else:
        launcher._runtime_store.remove(profile_id)
    result = launcher.stop(profile_id)
    assert not result["success"]
    assert result["code"] == "STOP_FAILED"
    if branch != "missing-pid":
        record = launcher._runtime_store.get(profile_id)
        assert record["pid"] == 424242 and record["launch_id"] == launch_id
    assert sentinel.read_text(encoding="utf-8") == "preserve synthetic data"


@pytest.mark.parametrize("observed", [{424243}, None], ids=["child-running", "scan-unknown"])
def test_clear_preserves_runtime_and_local_handles_while_directory_not_idle(directory_launcher, monkeypatch, observed):
    launcher, profile_id, _launch_id, sentinel = directory_launcher
    proc = SimpleNamespace(pid=424242, poll=lambda: 0)
    launcher.active_processes[profile_id] = proc
    record = launcher._runtime_store.get(profile_id)
    _immediate_scan(launcher, monkeypatch, observed)
    assert not launcher._clear_process(profile_id, expected_record=record)
    assert launcher._runtime_store.get(profile_id) == record
    assert launcher.active_processes[profile_id] is proc
    assert launcher.active_ports[profile_id] == 9333
    assert sentinel.exists()


def test_late_child_exit_allows_clear_after_fresh_scan(directory_launcher, monkeypatch):
    launcher, profile_id, _launch_id, sentinel = directory_launcher
    scans = []
    def scan(_directory):
        scans.append(True)
        return {424243} if len(scans) == 1 else set()
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", scan)
    monkeypatch.setattr(launcher_module.time, "sleep", lambda *_a: None)
    launcher.active_processes[profile_id] = SimpleNamespace(pid=424242, poll=lambda: 0)
    assert launcher._clear_process(profile_id)
    assert len(scans) == 2
    assert launcher._runtime_store.get(profile_id) is None
    assert profile_id not in launcher.active_processes
    assert profile_id not in launcher.active_ports
    assert sentinel.exists()


@pytest.mark.parametrize("results", [[None, set()], [None, {424243}, set()]])
def test_transient_unknown_scan_requires_later_confirmed_empty(tmp_path, monkeypatch, results):
    observations = iter(results)
    calls = []
    def scan(directory):
        calls.append(directory)
        return next(observations)
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", scan)
    monkeypatch.setattr(launcher_module.time, "sleep", lambda *_a: None)
    assert BrowserLauncher._wait_directory_idle(str(tmp_path))
    assert len(calls) == len(results)


@pytest.mark.parametrize("observed", [{424243}, None], ids=["child-running", "scan-unknown"])
def test_failed_launch_cleanup_retains_evidence_until_children_exit(directory_launcher, monkeypatch, observed):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    _immediate_scan(launcher, monkeypatch, observed)
    proc = SimpleNamespace(pid=424242, poll=lambda: 0)
    launcher.active_processes[profile_id] = proc
    assert not launcher._cleanup_failed_launch(profile_id, proc, launch_id)
    assert launcher._runtime_store.get(profile_id)["cleanup_failed"] is True
    assert launcher.active_processes[profile_id] is proc
    assert sentinel.exists()


@pytest.mark.parametrize("operation", ["stop", "failed-launch-cleanup"])
def test_graceful_close_waits_and_skips_native_termination(directory_launcher, monkeypatch, operation):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a, **_k: set())
    calls = []
    exited = []
    def graceful():
        calls.append("close")
        exited.append(True)
        return True
    session = launcher.cdp_health.bind_session(profile_id, launch_id)
    assert session.bind_browser_close(graceful)
    proc = SimpleNamespace(
        pid=424242, poll=lambda: 0 if exited else None,
        wait=lambda **kwargs: calls.append("wait"),
        terminate=lambda: pytest.fail("graceful exit must not terminate"),
        kill=lambda: pytest.fail("graceful exit must not kill"),
    )
    launcher.active_processes[profile_id] = proc
    if operation == "stop":
        assert launcher.stop(profile_id)["success"]
    else:
        assert launcher._cleanup_failed_launch(profile_id, proc, launch_id)
    assert calls == ["close", "wait"]
    assert launcher._runtime_store.get(profile_id) is None
    assert profile_id not in launcher.active_processes
    assert sentinel.exists()
