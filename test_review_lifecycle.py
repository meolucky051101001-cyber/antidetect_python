"""Regression tests for deletion and automatic browser/server ownership."""
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import threading

import pytest

from antidetect.browser_runtime import BrowserRuntimeStore
from antidetect.cdp_health import CdpHealthRegistry
from antidetect.launcher import BrowserLauncher
from antidetect.profile_lifecycle import stop_and_delete
from antidetect.profile_locks import ProfileLocks


class DeleteFixture:
    def __init__(self, root):
        self._profile_locks = ProfileLocks(root)
        self.root = root
        self.file = root / "session.txt"
        self.file.write_text("session", encoding="utf-8")
        self.deleted = False

    def _validate_profile_id(self, profile_id):
        assert profile_id == "profile-1"

    def get_profile(self, profile_id):
        return None if self.deleted else SimpleNamespace(id=profile_id)

    def delete_profile(self, profile_id):
        with self._profile_locks.acquire(profile_id):
            self.file.unlink()
            self.deleted = True
            return True


@pytest.mark.parametrize("raises", [False, True])
def test_delete_never_touches_session_after_stop_failure(tmp_path, raises):
    manager = DeleteFixture(tmp_path)

    def stop(_):
        if raises:
            raise RuntimeError("stop failed")
        return {"success": False, "code": "STOP_FAILED"}

    result = stop_and_delete(manager, SimpleNamespace(stop=stop), "profile-1")
    assert not result["success"]
    assert result["code"] == "STOP_FAILED"
    assert manager.file.read_text() == "session"
    assert not manager.deleted


def test_bridge_uses_failed_stop_result(tmp_path):
    from antidetect_bridge import action_delete

    manager = DeleteFixture(tmp_path)
    result = action_delete(manager, SimpleNamespace(stop=lambda _: {"success": False}), "profile-1")
    assert not result["success"]
    assert manager.file.exists()


def test_delete_holds_lock_across_confirmed_stop_and_delete(tmp_path):
    manager = DeleteFixture(tmp_path)
    observations = []
    original_delete = manager.delete_profile

    def delete(profile_id):
        # A separate process attempts to start a lifecycle operation at the
        # exact point between stop success and deletion.
        code = """
import sys
from antidetect.profile_locks import ProfileLocks, ProfileLockTimeout
try:
    with ProfileLocks(sys.argv[1]).acquire('profile-1', timeout=0.1):
        print('acquired')
except ProfileLockTimeout:
    print('blocked')
"""
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                                cwd=str(Path(__file__).parent), capture_output=True,
                                text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        observations.append(result.stdout.strip())
        return original_delete(profile_id)

    manager.delete_profile = delete
    result = stop_and_delete(manager, SimpleNamespace(stop=lambda _: {"success": True}), "profile-1")
    assert result["success"]
    assert observations == ["blocked"]
    assert not manager.file.exists()


def bare_launcher(tmp_path):
    launcher = object.__new__(BrowserLauncher)
    launcher.instance_id = "owner-A"
    launcher._runtime_store = BrowserRuntimeStore(str(tmp_path / "runtime.json"))
    launcher._lifecycle_locks = ProfileLocks(tmp_path)
    launcher._launch_locks = defaultdict(threading.RLock)
    launcher.cdp_health = CdpHealthRegistry()
    launcher.active_processes = {}
    return launcher


def test_conditional_stop_rechecks_persisted_generation(tmp_path):
    launcher = bare_launcher(tmp_path)
    old = launcher.cdp_health.begin_launch("p1", "owner-A")
    launcher._runtime_store.upsert("p1", launch_id="new-generation", owner_instance="owner-B", pid=999)
    called = []
    launcher._stop_impl = lambda pid: called.append(pid) or {"success": True}
    result = launcher.stop("p1", expected_launch_id=old)
    assert result["code"] == "SESSION_CHANGED"
    assert not called
    assert launcher._runtime_store.get("p1")["launch_id"] == "new-generation"


def test_shutdown_only_closes_owned_generations(tmp_path):
    launcher = bare_launcher(tmp_path)
    launcher._runtime_store.upsert("own", launch_id="a1", owner_instance="owner-A", pid=100)
    launcher._runtime_store.upsert("foreign", launch_id="b1", owner_instance="owner-B", pid=200)
    launcher._runtime_store.upsert("legacy", pid=123)
    calls = []
    launcher.stop = lambda pid, **kwargs: calls.append((pid, kwargs)) or {"success": True}
    assert launcher.stop_owned()["success"]
    assert calls == [("own", {"expected_launch_id": "a1", "expected_owner_instance": "owner-A"})]


def test_owned_cleanup_rejects_changed_owner_even_with_same_generation(tmp_path):
    launcher = bare_launcher(tmp_path)
    launcher._runtime_store.upsert("p1", launch_id="a1", owner_instance="owner-B", pid=200)
    launcher._stop_impl = lambda _: pytest.fail("must not stop a foreign owner")
    result = launcher.stop("p1", expected_launch_id="a1", expected_owner_instance="owner-A")
    assert result["code"] == "SESSION_CHANGED"


def test_server_info_cleanup_preserves_replacement(tmp_path, monkeypatch):
    import antidetect.server_control as sc

    monkeypatch.setattr(sc, "PID_FILE", str(tmp_path / "server.pid"))
    monkeypatch.setattr(sc, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sc, "get_process_details", lambda _: {})
    sc.write_server_info(100, 8000, "A")
    old = sc.read_server_info()
    sc.write_server_info(200, 8001, "B")
    assert not sc.remove_server_info(expected_info=old)
    assert not sc.remove_server_info(expected_instance_id="A")
    assert sc.read_server_info()["instance_id"] == "B"
    assert sc.remove_server_info(expected_instance_id="B")


def test_runtime_auth_cleanup_preserves_replacement(tmp_path, monkeypatch):
    import antidetect.runtime_auth as auth

    monkeypatch.setattr(auth, "RUNTIME_FILE", tmp_path / "runtime.dpapi")
    monkeypatch.setattr(auth, "_crypt", lambda payload, encrypt: payload)
    auth.write_runtime({"instance_id": "B", "access_key": "dummy"})
    assert not auth.delete_runtime(expected_instance_id="A")
    assert auth.read_runtime()["instance_id"] == "B"
    assert auth.delete_runtime(expected_instance_id="B")
