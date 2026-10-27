import itertools
import os
import sys
import threading

import pytest
from fastapi.testclient import TestClient

import antidetect.fingerprint as fingerprint_module
import antidetect.profile_audit as audit_module
import antidetect.launch_gate as gate_module
import web.app as app_module
from antidetect.models import FingerprintConfig, Profile, ProxyConfig

CHROME_FULL = "155.0.8059.85"
UA_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
UA_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

_seed_counter = itertools.count(810001, 2)


@pytest.fixture(autouse=True)
def stable_host(monkeypatch):
    for module in (fingerprint_module, audit_module):
        monkeypatch.setattr(module, "get_host_device_memory", lambda: 16)
        monkeypatch.setattr(
            module, "get_host_screen_info", lambda: (1920, 1080, 1.0)
        )
        monkeypatch.setattr(
            module, "get_installed_chrome_version",
            lambda: ("155.0.0.0", CHROME_FULL),
        )
        monkeypatch.setattr(module, "host_os", lambda: "windows")


@pytest.fixture(autouse=True)
def gate_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gate_module, "SETTINGS_PATH", str(tmp_path / "launch_gate.json")
    )
    monkeypatch.delenv("ANTIDETECT_LAUNCH_GATE", raising=False)


@pytest.fixture
def api_client():
    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.cookies.clear()
    login = client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    assert login.status_code == 200
    yield client
    client.cookies.clear()


def fingerprint(**overrides) -> FingerprintConfig:
    seed = next(_seed_counter)
    data = {
        "os": "windows",
        "browser_brand": "chrome",
        "user_agent": UA_WINDOWS,
        "browser_version": CHROME_FULL,
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_color_depth": 24,
        "device_pixel_ratio": 1.0,
        "canvas_noise_seed": seed,
        "audio_noise_seed": seed + 1,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        "hardware_concurrency": 8,
        "device_memory": 16,
        "timezone": "Asia/Ho_Chi_Minh",
        "locale": "vi-VN",
        "languages": ["vi-VN", "vi", "en-US", "en"],
        "latitude": 21.0285,
        "longitude": 105.8542,
        "accuracy": 50,
        "webrtc_mode": "disable_non_proxied",
    }
    data.update(overrides)
    return FingerprintConfig(**data)


def make_profile(name="P", **fp_overrides) -> Profile:
    return Profile(
        name=name,
        fingerprint=fingerprint(**fp_overrides),
        proxy=ProxyConfig(),
    )


def test_off_mode_skips_the_audit(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("audit must not run when the gate is off")

    monkeypatch.setattr(gate_module, "audit_profiles", explode)
    profile = make_profile("off")
    result = gate_module.evaluate_profile(profile, [profile], None, "off")
    assert result.status == "off"
    assert result.allowed is True
    assert result.checked is False


def test_critical_blocks_and_force_overrides():
    profile = make_profile("bad-ua", user_agent=UA_MACOS)

    blocked = gate_module.evaluate_profile(
        profile, [profile], None, "block", force=False
    )
    assert blocked.status == "blocked"
    assert blocked.allowed is False
    assert blocked.critical_count >= 1
    assert any(issue["code"] == "ua_os_mismatch" for issue in blocked.issues)

    forced = gate_module.evaluate_profile(
        profile, [profile], None, "block", force=True
    )
    assert forced.status == "overridden"
    assert forced.allowed is True
    assert forced.critical_count == blocked.critical_count


def test_warn_mode_reports_critical_but_allows_launch():
    profile = make_profile("bad-ua", user_agent=UA_MACOS)
    result = gate_module.evaluate_profile(profile, [profile], None, "warn")
    assert result.status == "warning"
    assert result.allowed is True
    assert result.critical_count >= 1


def test_warning_only_does_not_block():
    profile = make_profile("warning-only", device_memory=8)
    result = gate_module.evaluate_profile(profile, [profile], None, "block")
    assert result.status == "warning"
    assert result.allowed is True
    assert result.critical_count == 0
    assert result.warning_count >= 1


def test_audit_failure_is_unverified_but_does_not_block(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(gate_module, "audit_profiles", explode)
    profile = make_profile("unverified")
    result = gate_module.evaluate_profile(profile, [profile], None, "block")
    assert result.status == "unverified"
    assert result.allowed is True
    assert result.checked is False
    assert result.error == "audit unavailable"


def test_settings_roundtrip_env_override_and_validation():
    assert gate_module.get_mode() == "block"
    gate_module.set_mode("warn")
    assert gate_module.get_mode() == "warn"
    assert gate_module.settings_snapshot()["mode"] == "warn"

    with pytest.raises(ValueError):
        gate_module.set_mode("sometimes")

    gate_module.set_mode("block")
    os.environ["ANTIDETECT_LAUNCH_GATE"] = "off"
    try:
        assert gate_module.get_mode() == "off"
        assert gate_module.settings_snapshot()["env_override"] == "off"
    finally:
        os.environ.pop("ANTIDETECT_LAUNCH_GATE", None)


def test_evaluate_profiles_uses_one_audit(monkeypatch):
    real_audit = gate_module.audit_profiles
    calls = []

    def counting_audit(*args, **kwargs):
        calls.append(1)
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(gate_module, "audit_profiles", counting_audit)
    one = make_profile("one")
    two = make_profile("two")
    results = gate_module.evaluate_profiles([one, two], None, "block")
    assert set(results) == {one.id, two.id}
    assert len(calls) == 1
    assert all(result.allowed for result in results.values())


def test_api_start_blocks_critical_and_force_opens(monkeypatch, api_client):
    launched = []
    monkeypatch.setattr(app_module.launcher, "is_running", lambda pid: False)
    monkeypatch.setattr(
        app_module.launcher,
        "launch",
        lambda profile_id, open_url=None: (
            launched.append(profile_id) or {"success": True, "port": 61000}
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_resolve_browser_paths",
        lambda profiles: {p.id: None for p in profiles},
    )
    gate_module.set_mode("block")
    profile = app_module.profile_manager.create_profile(
        name="API gate bad", fingerprint=fingerprint(user_agent=UA_MACOS)
    )
    try:
        response = api_client.post(f"/api/profiles/{profile.id}/start")
        assert response.status_code == 409
        body = response.json()
        assert body["gate"]["status"] == "blocked"
        assert body["gate"]["critical_count"] >= 1
        assert launched == []

        response = api_client.post(f"/api/profiles/{profile.id}/start?force=true")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["gate"]["status"] == "overridden"
        assert launched == [profile.id]
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_api_start_warning_is_reported_but_launches(monkeypatch, api_client):
    launched = []
    monkeypatch.setattr(app_module.launcher, "is_running", lambda pid: False)
    monkeypatch.setattr(
        app_module.launcher,
        "launch",
        lambda profile_id, open_url=None: (
            launched.append(profile_id) or {"success": True, "port": 61001}
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_resolve_browser_paths",
        lambda profiles: {p.id: None for p in profiles},
    )
    gate_module.set_mode("block")
    profile = app_module.profile_manager.create_profile(
        name="API gate warning", fingerprint=fingerprint(device_memory=8)
    )
    try:
        response = api_client.post(f"/api/profiles/{profile.id}/start")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["gate"]["status"] == "warning"
        assert launched == [profile.id]
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_api_start_all_skips_blocked_profiles(monkeypatch, api_client):
    launched = []
    finished = threading.Event()

    def fake_launch_all(*args, **kwargs):
        launched.extend(kwargs.get("profile_ids") or [])
        finished.set()
        return []

    monkeypatch.setattr(app_module.launcher, "is_running", lambda pid: False)
    monkeypatch.setattr(app_module.launcher, "launch_all", fake_launch_all)
    monkeypatch.setattr(
        app_module,
        "_resolve_browser_paths",
        lambda profiles: {p.id: None for p in profiles},
    )
    gate_module.set_mode("block")
    bad = app_module.profile_manager.create_profile(
        name="bulk gate bad", fingerprint=fingerprint(user_agent=UA_MACOS)
    )
    good = app_module.profile_manager.create_profile(
        name="bulk gate good", fingerprint=fingerprint()
    )
    try:
        response = api_client.post(
            "/api/profiles/start-all",
            json={"profile_ids": [bad.id, good.id]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["starting_count"] == 1
        assert [item["profile_id"] for item in body["blocked"]] == [bad.id]
        assert finished.wait(timeout=5), "bulk launch thread did not run"
        assert launched == [good.id]
    finally:
        app_module.profile_manager.delete_profile(bad.id)
        app_module.profile_manager.delete_profile(good.id)


def test_launch_gate_settings_api(api_client):
    response = api_client.get("/api/settings/launch-gate")
    assert response.status_code == 200
    assert response.json()["mode"] == "block"

    response = api_client.put(
        "/api/settings/launch-gate", json={"mode": "warn"}
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "warn"
    assert gate_module.get_mode() == "warn"

    response = api_client.put(
        "/api/settings/launch-gate", json={"mode": "invalid"}
    )
    assert response.status_code == 400


class _FakeManager:
    def __init__(self, profiles):
        self._profiles = profiles

    def get_profile(self, profile_id):
        return next((p for p in self._profiles if p.id == profile_id), None)

    def list_profiles(self):
        return list(self._profiles)


class _FakeLauncher:
    def __init__(self):
        self.active_ports = {}
        self.launched = []
        self.launch_all_ids = None

    def _resolve_browser_path(self, profile):
        return None

    def is_running(self, profile_id):
        return False

    def launch(self, profile_id, open_url=None):
        self.launched.append(profile_id)
        return {"success": True, "port": 61010}

    def launch_all(self, **kwargs):
        self.launch_all_ids = kwargs.get("profile_ids")
        return []

    def unregister_cleanup(self):
        pass


def test_bridge_start_blocks_and_force_overrides():
    import antidetect_bridge as bridge

    bad = make_profile("bridge bad", user_agent=UA_MACOS)
    manager = _FakeManager([bad])
    launcher = _FakeLauncher()

    blocked = bridge.action_start(manager, launcher, bad.id)
    assert blocked["success"] is False
    assert blocked["blocked"] is True
    assert blocked["gate"]["status"] == "blocked"
    assert launcher.launched == []

    forced = bridge.action_start(manager, launcher, bad.id, force=True)
    assert forced["success"] is True
    assert forced["gate"]["status"] == "overridden"
    assert launcher.launched == [bad.id]


def test_bridge_start_all_filters_blocked_profiles():
    import antidetect_bridge as bridge

    bad = make_profile("bridge bulk bad", user_agent=UA_MACOS)
    good = make_profile("bridge bulk good")
    manager = _FakeManager([bad, good])
    launcher = _FakeLauncher()

    result = bridge.action_start_all(manager, launcher)
    assert result["success"] is True
    assert [item["profile_id"] for item in result["blocked"]] == [bad.id]
    assert launcher.launch_all_ids == [good.id]

    only_bad = _FakeManager([bad])
    empty_launcher = _FakeLauncher()
    refused = bridge.action_start_all(only_bad, empty_launcher)
    assert refused["success"] is False
    assert empty_launcher.launch_all_ids is None
    assert [item["profile_id"] for item in refused["blocked"]] == [bad.id]


def test_cli_start_blocks_critical_without_force(monkeypatch, capsys):
    import cli

    bad = make_profile("cli bad", user_agent=UA_MACOS)
    launcher = _FakeLauncher()
    monkeypatch.setattr(cli, "ProfileManager", lambda: _FakeManager([bad]))
    monkeypatch.setattr(cli, "BrowserLauncher", lambda manager: launcher)
    monkeypatch.setattr(sys, "argv", ["cli.py", "start", bad.id])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert launcher.launched == []
    assert "Cổng fingerprint chặn" in capsys.readouterr().out


def test_cli_start_all_skips_blocked(monkeypatch, capsys):
    import cli

    bad = make_profile("cli bulk bad", user_agent=UA_MACOS)
    good = make_profile("cli bulk good")
    launcher = _FakeLauncher()
    monkeypatch.setattr(cli, "ProfileManager", lambda: _FakeManager([bad, good]))
    monkeypatch.setattr(cli, "BrowserLauncher", lambda manager: launcher)
    monkeypatch.setattr(sys, "argv", ["cli.py", "start-all", "--detach"])

    cli.main()
    assert launcher.launch_all_ids == [good.id]
    out = capsys.readouterr().out
    assert "Bỏ qua cli bulk bad" in out
