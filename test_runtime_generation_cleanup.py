"""A stopped local process must never erase another instance's replacement."""
from collections import defaultdict
from types import SimpleNamespace
import threading

import pytest

from antidetect.browser_runtime import BrowserRuntimeStore
from antidetect.cdp_health import CdpHealthRegistry
from antidetect.launcher import BrowserLauncher
from antidetect.profile_lifecycle import stop_and_delete
from antidetect.profile_locks import ProfileLocks


def fixture_launcher(tmp_path):
    launcher = BrowserLauncher.__new__(BrowserLauncher)
    launcher.instance_id = "owner-A"
    launcher._runtime_store = BrowserRuntimeStore(str(tmp_path / "runtime.json"))
    launcher._lifecycle_locks = ProfileLocks(str(tmp_path))
    launcher._launch_locks = defaultdict(threading.RLock)
    launcher.cdp_health = CdpHealthRegistry()
    launcher.active_processes = {}
    launcher.active_ports = {}
    launcher._requested_ports = {}
    launcher._recovery_required = set()
    launcher._proxy_credentials = {}
    launcher._cdp_ready_events = {}
    launcher._core_verified_events = {}
    launcher._navigation_events = {}
    launcher._health_persist_warned = set()
    launcher.manager = SimpleNamespace(get_profile=lambda _: SimpleNamespace(pid=None), get_profile_dir=lambda _: str(tmp_path))
    return launcher


def set_replacement(launcher, profile_id="profile-1"):
    return launcher._runtime_store.upsert(profile_id, launch_id="generation-B", owner_instance="owner-B", pid=200, port=9200, user_data_dir="fixture-dir")


def test_exited_local_process_cannot_make_replacement_stop_succeed(tmp_path):
    launcher = fixture_launcher(tmp_path)
    old = launcher.cdp_health.begin_launch("profile-1", "owner-A")
    launcher.cdp_health.set_process("profile-1", old, 100)
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=100, poll=lambda: 0)
    replacement = set_replacement(launcher)
    result = launcher.stop("profile-1")
    assert result["code"] == "SESSION_CHANGED"
    assert not result["success"]
    assert launcher._runtime_store.get("profile-1") == replacement
    assert "profile-1" not in launcher.active_processes


def test_is_running_recovers_replacement_after_local_process_exits(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    old = launcher.cdp_health.begin_launch("profile-1", "owner-A")
    launcher.cdp_health.set_process("profile-1", old, 100)
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=100, poll=lambda: 0)
    set_replacement(launcher)
    monkeypatch.setattr("antidetect.launcher.read_browser_identity", lambda *a: {"state": "owned", "port": 9200, "created": "birth-B"})
    assert launcher.is_running("profile-1")
    assert launcher._runtime_store.get("profile-1")["launch_id"] == "generation-B"
    health = launcher.cdp_health.snapshot("profile-1")
    assert health["launch_id"] == "generation-B"
    assert health["owner_instance"] == "owner-B"
    assert launcher.active_ports["profile-1"] == 9200


def test_stop_snapshot_cannot_remove_runtime_replaced_during_identity_query(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    launcher._runtime_store.upsert("profile-1", launch_id="generation-A", owner_instance="owner-A", pid=100)

    def identity(*a):
        set_replacement(launcher)
        return {"state": "absent"}

    monkeypatch.setattr(launcher, "_identity_for_stop", identity)
    result = launcher.stop("profile-1")
    assert result["code"] == "SESSION_CHANGED"
    assert launcher._runtime_store.get("profile-1")["pid"] == 200


def test_recovery_snapshot_cannot_remove_runtime_replaced_during_query(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    launcher._runtime_store.upsert("profile-1", launch_id="generation-A", owner_instance="owner-A", pid=100)

    def identity(*a):
        set_replacement(launcher)
        return {"state": "absent"}

    monkeypatch.setattr("antidetect.launcher.read_browser_identity", identity)
    assert launcher.is_running("profile-1")
    assert launcher._runtime_store.get("profile-1")["pid"] == 200


def test_delete_preserves_session_when_local_process_is_stale(tmp_path):
    launcher = fixture_launcher(tmp_path)
    marker = tmp_path / "session.txt"
    marker.write_text("preserve")
    launcher.cdp_health.begin_launch("profile-1", "owner-A")
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=100, poll=lambda: 0)
    set_replacement(launcher)
    launcher.manager._profile_locks = launcher._lifecycle_locks
    launcher.manager._validate_profile_id = lambda _: None
    launcher.manager.delete_profile = lambda _: pytest.fail("Must not delete")
    result = stop_and_delete(launcher.manager, launcher, "profile-1")
    assert result["code"] == "SESSION_CHANGED"
    assert marker.read_text() == "preserve"


def test_runtime_remove_compare_and_swap_preserves_owner_and_process_birth(tmp_path):
    store = BrowserRuntimeStore(str(tmp_path / "runtime.json"))
    original = store.upsert("profile-1", launch_id="A", owner_instance="owner-A", pid=100, created="old")
    newer = store.upsert("profile-1", created="new")
    assert not store.remove_if_matches("profile-1", original)
    assert store.get("profile-1") == newer
    assert not store.remove_if_matches("profile-1", {})
    assert store.remove_if_matches("profile-1", newer)
    assert store.get("profile-1") is None


def test_generation_forget_does_not_drop_newer_health():
    registry = CdpHealthRegistry()
    old = registry.begin_launch("p1", "A")
    current = registry.begin_launch("p1", "B")
    assert not registry.forget_if_launch("p1", old)
    assert registry.snapshot("p1")["launch_id"] == current


@pytest.mark.parametrize("query_raises", [False, True])
def test_delete_rechecks_liveness_after_successful_stop(tmp_path, query_raises):
    launcher = fixture_launcher(tmp_path)
    launcher.manager._profile_locks = launcher._lifecycle_locks
    launcher.manager._validate_profile_id = lambda _: None
    launcher.manager.delete_profile = lambda _: pytest.fail("Must not delete")
    launcher.stop = lambda _: {"success": True}

    def query(_):
        if query_raises:
            raise RuntimeError("process lookup unavailable")
        return True

    launcher.is_running = query
    result = stop_and_delete(launcher.manager, launcher, "profile-1")
    assert result["code"] == "STOP_FAILED"


def test_retry_after_stale_local_stop_checks_current_process(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    launcher.cdp_health.begin_launch("profile-1", "owner-A")
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=100, poll=lambda: 0)
    set_replacement(launcher)
    assert launcher.stop("profile-1")["code"] == "SESSION_CHANGED"
    checked = []

    def identity(pid, directory):
        checked.append(pid)
        return {"state": "absent"}

    monkeypatch.setattr(launcher, "_identity_for_stop", identity)
    assert launcher.stop("profile-1")["success"]
    assert checked == [200]
    assert launcher._runtime_store.get("profile-1") is None


def test_shutdown_refuses_new_launch_before_impl(tmp_path):
    launcher = fixture_launcher(tmp_path)
    launcher.begin_shutdown()
    launcher._launch_impl = lambda *a, **k: pytest.fail("Must not start")
    assert launcher.launch("profile-1")["code"] == "SERVER_SHUTTING_DOWN"


def test_inflight_launch_cleans_exact_generation_after_shutdown_snapshot(tmp_path):
    launcher = fixture_launcher(tmp_path)
    entered = threading.Event()
    finish = threading.Event()
    stopped = []
    result = []

    def launch_impl(*a, **k):
        entered.set()
        assert finish.wait(5)
        # Runtime did not exist yet when shutdown's stop_owned took a snapshot.
        launcher._runtime_store.upsert("profile-1", launch_id="late-A", owner_instance="owner-A", pid=100)
        return {"success": True, "launched_now": True, "cdp_health": {"launch_id": "late-A"}}

    def stop(profile_id, **expected):
        stopped.append((profile_id, expected))
        return {"success": True}

    launcher._launch_impl = launch_impl
    launcher.stop = stop
    worker = threading.Thread(target=lambda: result.append(launcher.launch("profile-1")))
    worker.start()
    assert entered.wait(5)
    launcher.begin_shutdown()
    assert launcher.stop_owned()["results"] == {}
    finish.set()
    worker.join(5)
    assert not worker.is_alive()
    assert result[0]["code"] == "SERVER_SHUTTING_DOWN"
    assert stopped == [("profile-1", {"expected_launch_id": "late-A", "expected_owner_instance": "owner-A"})]


def test_status_cleanup_cannot_erase_concurrently_launched_generation(tmp_path):
    launcher = fixture_launcher(tmp_path)
    old = launcher.cdp_health.begin_launch("profile-1", "owner-A")
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=100, poll=lambda: 0)
    launcher._runtime_store.upsert("profile-1", launch_id=old, owner_instance="owner-A", pid=100)
    before_clear, release_clear, launch_waiting, launched = (threading.Event() for _ in range(4))
    read_result = []
    clear = launcher._clear_process

    def paused_clear(*args, **kwargs):
        before_clear.set()
        assert release_clear.wait(5)
        return clear(*args, **kwargs)

    def replacement_launch():
        launch_waiting.set()
        with launcher._launch_locks["profile-1"]:
            generation = launcher.cdp_health.begin_launch("profile-1", "owner-A")
            launcher.cdp_health.set_process("profile-1", generation, 200)
            launcher.active_processes["profile-1"] = SimpleNamespace(pid=200, poll=lambda: None)
            launcher._runtime_store.upsert("profile-1", launch_id=generation, owner_instance="owner-A", pid=200)
            launched.set()

    launcher._clear_process = paused_clear
    reader = threading.Thread(target=lambda: read_result.append(launcher.is_running("profile-1")))
    writer = threading.Thread(target=replacement_launch)
    reader.start()
    assert before_clear.wait(5)
    writer.start()
    try:
        assert launch_waiting.wait(5)
        # The reader must still own the lock after polling A and before clear.
        acquired = launcher._launch_locks["profile-1"].acquire(blocking=False)
        if acquired:
            launcher._launch_locks["profile-1"].release()
        assert not acquired
        assert not launched.is_set()
    finally:
        release_clear.set()
        reader.join(5)
        writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert read_result == [False]
    assert launched.is_set()
    assert launcher.active_processes["profile-1"].pid == 200
    assert launcher._runtime_store.get("profile-1")["pid"] == 200
    assert launcher.cdp_health.snapshot("profile-1")["state"] == "starting"
