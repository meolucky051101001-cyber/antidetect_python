"""Step 1 integration tests: safe transient tab bridge & require_healthy gate."""
import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from antidetect.cdp_health import CdpUnavailable, REASON_SOCKET_CLOSED, REASON_VERIFICATION_FAILED
from antidetect.facebook import TRANSIENT_TARGET_IDS
from test_cdp_health import _healthy
from test_runtime_generation_cleanup import fixture_launcher


def test_open_transient_tab_waits_for_stealth_configuration_before_returning(tmp_path, monkeypatch):
    import antidetect.launcher as launcher_module

    launcher = fixture_launcher(tmp_path)
    profile_id = "comment-bridge-profile"
    launcher.is_running = lambda _pid: True
    launcher.active_ports[profile_id] = 9444
    _healthy(launcher.cdp_health, profile_id, owner=launcher.instance_id, pid=5150)

    calls = []

    class Resp:
        def __init__(self, payload, status=200):
            self._raw = json.dumps(payload).encode("utf-8")
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._raw

    def fake_open_loopback(req_or_url, timeout=3):
        url = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
        method = getattr(req_or_url, "method", "GET")
        calls.append((method, url))
        if "/json/new?about%3Ablank" in url:
            # Simulate configure_page finishing 80ms AFTER /json/new returns.
            def mark_ready():
                time.sleep(0.08)
                launcher._mark_target_configured(
                    profile_id,
                    launcher.cdp_health.current_launch_id(profile_id),
                    "transient-tab-1",
                )

            threading.Thread(target=mark_ready, daemon=True).start()
            return Resp({
                "id": "transient-tab-1",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9444/devtools/page/transient-tab-1",
            })
        if "/json/close/transient-tab-1" in url:
            return Resp({"closed": True})
        raise AssertionError(f"Unexpected loopback call: {url}")

    monkeypatch.setattr(launcher_module, "open_loopback", fake_open_loopback)
    started = time.monotonic()
    tab = launcher.open_transient_tab(profile_id, timeout=2.0)
    elapsed = time.monotonic() - started
    try:
        assert elapsed >= 0.06
        assert tab["success"] is True
        assert tab["target_id"] == "transient-tab-1"
        assert tab["ws_url"] == "ws://127.0.0.1:9444/devtools/page/transient-tab-1"
        assert "transient-tab-1" in TRANSIENT_TARGET_IDS
    finally:
        assert launcher.close_transient_tab(profile_id, "transient-tab-1") is True
        assert "transient-tab-1" not in TRANSIENT_TARGET_IDS


def test_open_transient_tab_closes_and_fails_closed_when_unconfigured_or_degraded(tmp_path, monkeypatch):
    import antidetect.launcher as launcher_module

    launcher = fixture_launcher(tmp_path)
    profile_id = "unconfigured-tab-profile"
    launcher.is_running = lambda _pid: True
    launcher.active_ports[profile_id] = 9445
    launch_id, _ = _healthy(launcher.cdp_health, profile_id, owner=launcher.instance_id, pid=5151)

    closed_targets = []

    class Resp:
        def __init__(self, payload):
            self._raw = json.dumps(payload).encode("utf-8")
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._raw

    def fake_open_loopback(req_or_url, timeout=3):
        url = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
        if "/json/new?about%3Ablank" in url:
            return Resp({
                "id": "unconfigured-tab",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9445/devtools/page/unconfigured-tab",
            })
        if "/json/close/unconfigured-tab" in url:
            closed_targets.append("unconfigured-tab")
            return Resp({})
        return Resp([])

    monkeypatch.setattr(launcher_module, "open_loopback", fake_open_loopback)
    with pytest.raises(CdpUnavailable):
        launcher.open_transient_tab(profile_id, timeout=1.0)
    assert closed_targets == ["unconfigured-tab"]
    assert "unconfigured-tab" not in TRANSIENT_TARGET_IDS

    # Degraded profile must be rejected BEFORE calling /json/new.
    launcher.cdp_health.mark_degraded(profile_id, launch_id, REASON_SOCKET_CLOSED)
    monkeypatch.setattr(
        launcher_module,
        "open_loopback",
        lambda *a, **k: pytest.fail("Degraded profile must never open a new tab"),
    )
    with pytest.raises(CdpUnavailable) as exc_info:
        launcher.open_transient_tab(profile_id, timeout=1.0)
    assert exc_info.value.code == "CDP_UNHEALTHY"


def test_api_require_healthy_stops_degraded_owned_launch_and_transient_tab_endpoints(monkeypatch):
    import web.app as app_module

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    created = client.post("/api/profiles", json={"name": "Comment Bridge API"})
    profile_id = created.json()["id"]
    stopped_calls = []

    def fake_degraded_launch(*_a, **_kw):
        return {
            "success": True,
            "profile_id": profile_id,
            "port": 9555,
            "pid": 7777,
            "launch_id": "degraded-gen-1",
            "owner_instance": app_module.launcher.instance_id,
            "cdp_ready": False,
            "degraded": True,
            "code": "CDP_UNHEALTHY",
            "launched_now": True,
            "warning": "Stealth verification failed",
            "cdp_health": {
                "launch_id": "degraded-gen-1",
                "owner_instance": app_module.launcher.instance_id,
                "state": "degraded",
                "reason": REASON_VERIFICATION_FAILED,
            },
        }

    def fake_stop(pid, *, expected_launch_id=None, expected_owner_instance=None):
        stopped_calls.append((pid, expected_launch_id, expected_owner_instance))
        return {"success": True}

    monkeypatch.setattr(app_module.launcher, "launch", fake_degraded_launch)
    monkeypatch.setattr(app_module.launcher, "stop", fake_stop)
    try:
        # Default UI call stays backward-compatible (200 with degraded=True warning).
        res_default = client.post(f"/api/profiles/{profile_id}/start")
        assert res_default.status_code == 200
        assert res_default.json()["degraded"] is True
        assert stopped_calls == []

        # Automation call with require_healthy=true rejects with 409 and stops owned degraded browser.
        res_strict = client.post(f"/api/profiles/{profile_id}/start?require_healthy=true")
        assert res_strict.status_code == 409
        body = res_strict.json()
        assert body["success"] is False
        assert body["code"] == "CDP_UNHEALTHY"
        assert stopped_calls == [(profile_id, "degraded-gen-1", app_module.launcher.instance_id)]

        # Transient tab endpoints delegate cleanly and map CdpUnavailable -> 409.
        monkeypatch.setattr(
            app_module.launcher,
            "open_transient_tab",
            lambda pid, timeout=8.0: {
                "success": True,
                "profile_id": pid,
                "port": 9555,
                "target_id": "tab-xyz",
                "ws_url": "ws://127.0.0.1:9555/devtools/page/tab-xyz",
            },
        )
        monkeypatch.setattr(
            app_module.launcher,
            "close_transient_tab",
            lambda pid, tid: pid == profile_id and tid == "tab-xyz",
        )
        opened = client.post(f"/api/profiles/{profile_id}/tabs/transient")
        assert opened.status_code == 200
        assert opened.json()["target_id"] == "tab-xyz"

        closed = client.delete(f"/api/profiles/{profile_id}/tabs/transient/tab-xyz")
        assert closed.status_code == 200
        assert closed.json() == {"success": True, "closed": True, "target_id": "tab-xyz"}
    finally:
        client.delete(f"/api/profiles/{profile_id}")
