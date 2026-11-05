"""F1 regressions: CDP health is separate from Chrome process liveness."""
import pytest

from antidetect.browser_runtime import BrowserRuntimeStore
from antidetect.cdp_health import (
    CdpHealthRegistry,
    CdpUnavailable,
    HEARTBEAT_STALE_AFTER_SECONDS,
    REASON_DETACHED_OWNER_EXITED,
    REASON_HEARTBEAT_STALE,
    REASON_SOCKET_CLOSED,
    REASON_STARTUP_TIMEOUT,
    REASON_STOP_FAILED,
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_RECOVERY_REQUIRED,
    STATE_STARTING,
    STATE_STOPPED,
    STARTING_STALE_AFTER_SECONDS,
)
from antidetect.launcher import BrowserLauncher
from antidetect.storage import ProfileManager


class FakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _healthy(registry, profile_id="p1", owner="launcher:test", pid=1234):
    launch_id = registry.begin_launch(profile_id, owner)
    assert registry.set_process(profile_id, launch_id, pid) is True
    session = registry.bind_session(profile_id, launch_id)
    assert session is not None
    assert registry.mark_healthy(profile_id, launch_id) is True
    return launch_id, session


def test_launch_generation_isolation_rejects_old_callbacks():
    registry = CdpHealthRegistry()
    old_launch, old_session = _healthy(registry, "profile-old")

    new_launch = registry.begin_launch("profile-old", "launcher:test")

    assert new_launch != old_launch
    assert old_session.is_open() is False
    assert registry.snapshot("profile-old")["state"] == STATE_STARTING
    assert registry.mark_healthy("profile-old", old_launch) is False
    assert registry.mark_heartbeat("profile-old", old_launch) is False
    assert registry.mark_degraded(
        "profile-old", old_launch, REASON_SOCKET_CLOSED
    ) is False
    assert registry.snapshot("profile-old")["state"] == STATE_STARTING

    assert registry.mark_degraded(
        "profile-old", new_launch, REASON_SOCKET_CLOSED
    ) is True
    snapshot = registry.snapshot("profile-old")
    assert snapshot["state"] == STATE_DEGRADED
    assert snapshot["reason"] == REASON_SOCKET_CLOSED
    assert snapshot["restart_required"] is True


def test_heartbeat_stale_is_reported_without_mutating_state():
    clock = FakeClock()
    registry = CdpHealthRegistry(clock=clock)
    launch_id, _ = _healthy(registry, "heartbeat-profile")

    clock.advance(HEARTBEAT_STALE_AFTER_SECONDS + 0.5)
    stale = registry.snapshot("heartbeat-profile")
    assert stale["state"] == STATE_DEGRADED
    assert stale["reason"] == REASON_HEARTBEAT_STALE
    assert stale["restart_required"] is True

    assert registry.mark_heartbeat("heartbeat-profile", launch_id) is True
    fresh = registry.snapshot("heartbeat-profile")
    assert fresh["state"] == STATE_HEALTHY
    assert fresh["reason"] is None


def test_starting_state_times_out_after_startup_budget():
    clock = FakeClock()
    registry = CdpHealthRegistry(clock=clock)
    registry.begin_launch("slow-profile", "launcher:test")

    clock.advance(STARTING_STALE_AFTER_SECONDS + 1)
    snapshot = registry.snapshot("slow-profile")
    assert snapshot["state"] == STATE_DEGRADED
    assert snapshot["reason"] == REASON_STARTUP_TIMEOUT
    assert snapshot["restart_required"] is True


def test_stopping_ignores_heartbeat_and_regular_degradation():
    registry = CdpHealthRegistry()
    launch_id, _ = _healthy(registry, "stop-profile")

    assert registry.mark_stopping("stop-profile") is True
    assert registry.is_stopping("stop-profile") is True
    assert registry.mark_heartbeat("stop-profile", launch_id) is False
    assert registry.mark_degraded(
        "stop-profile", launch_id, REASON_SOCKET_CLOSED
    ) is False
    assert registry.mark_degraded(
        "stop-profile", launch_id, REASON_STOP_FAILED
    ) is True
    assert registry.snapshot("stop-profile")["reason"] == REASON_STOP_FAILED

    assert registry.mark_stopped("stop-profile", launch_id) is True
    stopped = registry.snapshot("stop-profile")
    assert stopped["state"] == STATE_STOPPED
    assert stopped["reason"] is None
    assert stopped["restart_required"] is False


def test_recovery_required_preserves_live_owner_unless_forced():
    registry = CdpHealthRegistry()
    launch_id, session = _healthy(registry, "owner-profile", owner="launcher:a")

    assert registry.mark_recovery_required(
        "owner-profile",
        REASON_SOCKET_CLOSED,
        owner_instance="launcher:a",
        launch_id=launch_id,
    ) is False
    assert registry.snapshot("owner-profile")["state"] == STATE_HEALTHY

    assert registry.mark_recovery_required(
        "owner-profile",
        REASON_DETACHED_OWNER_EXITED,
        owner_instance="launcher:a",
        launch_id=launch_id,
        force=True,
    ) is True
    snapshot = registry.snapshot("owner-profile")
    assert snapshot["state"] == STATE_RECOVERY_REQUIRED
    assert snapshot["reason"] == REASON_DETACHED_OWNER_EXITED
    assert snapshot["restart_required"] is True
    assert session.is_open() is False
    assert registry.mark_healthy("owner-profile", launch_id) is False


def test_degraded_records_survive_repeated_reason_only_once():
    registry = CdpHealthRegistry()
    launch_id, _ = _healthy(registry, "dedupe-profile")
    assert registry.mark_degraded(
        "dedupe-profile", launch_id, REASON_HEARTBEAT_STALE
    ) is True
    assert registry.mark_degraded(
        "dedupe-profile", launch_id, REASON_HEARTBEAT_STALE
    ) is False
    assert registry.mark_degraded(
        "dedupe-profile", launch_id, REASON_SOCKET_CLOSED
    ) is True


def test_snapshot_without_record_is_stopped():
    registry = CdpHealthRegistry()
    snapshot = registry.snapshot("never-launched")
    assert snapshot["state"] == STATE_STOPPED
    assert snapshot["launch_id"] is None
    assert snapshot["restart_required"] is False


def test_runtime_upsert_if_owner_guards_launch_pid_and_owner(tmp_path):
    store = BrowserRuntimeStore(str(tmp_path / "browser_runtime.json"))
    store.upsert(
        "owned-profile",
        pid=4242,
        launch_id="launch-1",
        owner_instance="launcher:a",
        port=9333,
    )

    assert store.upsert_if_owner(
        "owned-profile", launch_id="old-launch", cdp_health={"state": "degraded"}
    ) is None
    assert store.upsert_if_owner(
        "owned-profile", pid=9999, cdp_health={"state": "degraded"}
    ) is None
    assert store.upsert_if_owner(
        "owned-profile",
        launch_id="launch-1",
        owner_instance="launcher:b",
        cdp_health={"state": "degraded"},
    ) is None
    assert store.upsert_if_owner(
        "missing-profile", launch_id="launch-1", cdp_health={"state": "degraded"}
    ) is None

    updated = store.upsert_if_owner(
        "owned-profile",
        launch_id="launch-1",
        pid=4242,
        owner_instance="launcher:a",
        cdp_health={"state": "healthy"},
    )
    assert updated is not None
    assert updated["cdp_health"]["state"] == "healthy"
    assert updated["port"] == 9333


def _launcher():
    launcher = BrowserLauncher(ProfileManager())
    return launcher


def test_launcher_guard_allows_only_healthy_current_owner():
    launcher = _launcher()
    launcher.is_running = lambda _profile_id: True
    try:
        launch_id, _ = _healthy(
            launcher.cdp_health,
            "guard-profile",
            owner=launcher.instance_id,
        )
        allowed = launcher.cdp_guard("guard-profile", running=True)
        assert allowed["ok"] is True
        assert allowed["code"] is None

        launcher.cdp_health.mark_degraded(
            "guard-profile", launch_id, REASON_SOCKET_CLOSED
        )
        blocked = launcher.cdp_guard("guard-profile", running=True)
        assert blocked["ok"] is False
        assert blocked["code"] == "CDP_UNHEALTHY"
        assert blocked["health"]["reason"] == REASON_SOCKET_CLOSED
        with pytest.raises(CdpUnavailable) as exc:
            launcher.require_cdp("guard-profile")
        assert exc.value.code == "CDP_UNHEALTHY"
        assert exc.value.health["state"] == STATE_DEGRADED
    finally:
        launcher.cdp_health.clear()
        launcher.unregister_cleanup()


def test_launcher_guard_reports_detached_and_starting():
    launcher = _launcher()
    try:
        launcher.cdp_health.mark_recovery_required(
            "detached-profile",
            REASON_DETACHED_OWNER_EXITED,
            owner_instance=launcher.instance_id,
            pid=1234,
            launch_id="detached-launch",
            force=True,
        )
        detached = launcher.cdp_guard("detached-profile", running=True)
        assert detached["ok"] is False
        assert detached["code"] == "DETACHED_NO_CDP"
        result = launcher._running_launch_result("detached-profile")
        assert result["success"] is False
        assert result["recovery_required"] is True
        assert result["launched_now"] is False

        launcher.cdp_health.begin_launch("starting-profile", launcher.instance_id)
        starting = launcher.cdp_guard("starting-profile", running=True)
        assert starting["ok"] is False
        assert starting["code"] == "CDP_STARTING"
    finally:
        launcher.cdp_health.clear()
        launcher.unregister_cleanup()


def test_mark_detached_persists_recovery_in_runtime_store():
    launcher = _launcher()
    profile_id = "detach-persist-profile"
    try:
        launch_id = launcher.cdp_health.begin_launch(
            profile_id, launcher.instance_id
        )
        launcher.cdp_health.set_process(profile_id, launch_id, 4242)
        launcher.cdp_health.bind_session(profile_id, launch_id)
        assert launcher.cdp_health.mark_healthy(profile_id, launch_id) is True
        launcher._runtime_store.upsert(
            profile_id,
            pid=4242,
            port=9555,
            launch_id=launch_id,
            owner_instance=launcher.instance_id,
        )

        snapshot = launcher.mark_detached(profile_id)
        assert snapshot["state"] == STATE_RECOVERY_REQUIRED
        assert snapshot["reason"] == REASON_DETACHED_OWNER_EXITED

        record = launcher._runtime_store.get(profile_id) or {}
        assert record.get("detached") is True
        health = record.get("cdp_health") or {}
        assert health.get("state") == STATE_RECOVERY_REQUIRED
        assert health.get("reason") == REASON_DETACHED_OWNER_EXITED
    finally:
        launcher._runtime_store.remove(profile_id)
        launcher.cdp_health.clear()
        launcher.unregister_cleanup()


def test_start_api_returns_machine_readable_cdp_conflict(monkeypatch):
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    created = client.post("/api/profiles", json={"name": "CDP Conflict API"})
    profile_id = created.json()["id"]

    def fake_launch(*_args, **_kwargs):
        return {
            "success": False,
            "code": "DETACHED_NO_CDP",
            "error": "Chrome đang mở nhưng chạy detached nên không có CDP.",
            "cdp_health": {
                "state": "recovery_required",
                "reason": "DETACHED_OWNER_EXITED",
            },
            "launched_now": False,
        }

    monkeypatch.setattr(app_module.launcher, "launch", fake_launch)
    try:
        response = client.post(f"/api/profiles/{profile_id}/start")
        assert response.status_code == 409
        body = response.json()
        assert body["success"] is False
        assert body["code"] == "DETACHED_NO_CDP"
        assert body["cdp_health"]["reason"] == "DETACHED_OWNER_EXITED"
        assert "detached" in body["detail"]
    finally:
        client.delete(f"/api/profiles/{profile_id}")


def test_cookie_export_api_maps_cdp_unavailable_to_409(monkeypatch):
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    created = client.post("/api/profiles", json={"name": "Cookie Conflict API"})
    profile_id = created.json()["id"]

    monkeypatch.setattr(app_module.launcher, "is_running", lambda _pid: True)

    def unavailable(*_args, **_kwargs):
        raise CdpUnavailable(
            "CDP_UNHEALTHY",
            "Chrome đang mở nhưng CDP không khỏe.",
            {"state": "degraded", "reason": "SOCKET_CLOSED"},
        )

    monkeypatch.setattr(app_module.launcher, "export_cookies", unavailable)
    try:
        response = client.get(
            f"/api/profiles/{profile_id}/cookies/export",
            headers={"x-confirm-cookie-access": "yes"},
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "CDP_UNHEALTHY"
        assert body["cdp_health"]["state"] == "degraded"
    finally:
        client.delete(f"/api/profiles/{profile_id}")
