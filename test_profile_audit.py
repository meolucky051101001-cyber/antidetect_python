import json
import os

import pytest
from fastapi.testclient import TestClient

import antidetect.fingerprint as fingerprint_module
import antidetect.profile_audit as audit_module
import antidetect.storage as storage_module
import web.app as app_module
from antidetect.models import FingerprintConfig, Profile, ProxyConfig
from antidetect.profile_audit import audit_profiles, build_fix_plan

CHROME_FULL = "155.0.8059.85"
UA_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
UA_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
WINDOWS_GPU = {
    "vendor": "Google Inc. (NVIDIA)",
    "renderer": (
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    ),
}
APPLE_GPU = {
    "vendor": "Google Inc. (Apple)",
    "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Version 15.0)",
}


@pytest.fixture(autouse=True)
def stable_host(monkeypatch):
    """Pin host capabilities so audit results do not depend on the runner."""
    for module in (fingerprint_module, audit_module):
        monkeypatch.setattr(module, "get_host_device_memory", lambda: 16)
        monkeypatch.setattr(
            module, "get_host_screen_info", lambda: (1920, 1080, 1.0)
        )
        monkeypatch.setattr(
            module,
            "get_installed_chrome_version",
            lambda: ("155.0.0.0", CHROME_FULL),
        )
        monkeypatch.setattr(module, "host_os", lambda: "windows")
    yield


def windows_fp(**overrides) -> FingerprintConfig:
    data = {
        "os": "windows",
        "browser_brand": "chrome",
        "user_agent": UA_WINDOWS,
        "browser_version": CHROME_FULL,
        "screen_width": 1920,
        "screen_height": 1080,
        "screen_color_depth": 24,
        "device_pixel_ratio": 1.0,
        "canvas_noise_seed": 100001,
        "audio_noise_seed": 200001,
        "webgl_vendor": WINDOWS_GPU["vendor"],
        "webgl_renderer": WINDOWS_GPU["renderer"],
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


def macos_fp(**overrides) -> FingerprintConfig:
    data = {
        "os": "macos",
        "browser_brand": "chrome",
        "user_agent": UA_MACOS,
        "browser_version": CHROME_FULL,
        "screen_width": 1440,
        "screen_height": 900,
        "screen_color_depth": 24,
        "device_pixel_ratio": 2.0,
        "canvas_noise_seed": 300001,
        "audio_noise_seed": 400001,
        "webgl_vendor": APPLE_GPU["vendor"],
        "webgl_renderer": APPLE_GPU["renderer"],
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


def make_profile(name="P", fp=None, proxy=None, **kwargs) -> Profile:
    return Profile(
        name=name,
        fingerprint=fp or windows_fp(),
        proxy=proxy or ProxyConfig(),
        **kwargs,
    )


def proxy(host="10.1.2.3", port=8080) -> ProxyConfig:
    return ProxyConfig(enabled=True, protocol="http", host=host, port=port)


def issue_codes(report, profile_id) -> set:
    item = next(p for p in report["profiles"] if p["profile_id"] == profile_id)
    return {issue["code"] for issue in item["issues"]}


def test_healthy_profile_scores_100():
    p = make_profile("Healthy", proxy=proxy())
    report = audit_profiles([p], {p.id: None})
    item = report["profiles"][0]
    assert item["status"] == "healthy", item["issues"]
    assert item["score"] == 100
    assert report["summary"]["average_score"] == 100.0


def test_macos_fingerprint_detects_and_fixes_retina_violations():
    fp = macos_fp(
        screen_width=1366,
        screen_height=768,
        device_pixel_ratio=1.0,
    )
    p = make_profile("Mac", fp=fp)
    report = audit_profiles([p], {p.id: None})
    codes = issue_codes(report, p.id)
    assert {"macos_dpr_invalid", "macos_windows_resolution"} <= codes

    plan = build_fix_plan([p], {p.id: None})
    assert len(plan) == 1
    changes = {change["field"]: change for change in plan[0]["changes"]}
    assert changes["device_pixel_ratio"]["after"] == 2.0
    assert (changes["screen_width"]["after"], changes["screen_height"]["after"]) == (
        1440,
        900,
    )


def test_windows_profile_with_apple_gpu_is_critical_and_repairable():
    p = make_profile(
        "Frankenstein",
        fp=windows_fp(
            webgl_vendor=APPLE_GPU["vendor"],
            webgl_renderer=APPLE_GPU["renderer"],
        ),
    )
    report = audit_profiles([p], {p.id: None})
    item = report["profiles"][0]
    assert "gpu_os_mismatch" in issue_codes(report, p.id)
    assert item["status"] == "critical"

    plan = build_fix_plan([p], {p.id: None})
    fixed = FingerprintConfig.model_validate(plan[0]["fingerprint"])
    assert "Direct3D" in fixed.webgl_renderer


def test_outdated_ua_and_browser_version_are_flagged_and_fixed():
    old_ua = UA_WINDOWS.replace("Chrome/155.0.0.0", "Chrome/124.0.0.0")
    p = make_profile(
        "Old Chrome",
        fp=windows_fp(browser_version="124.0.6367.91", user_agent=old_ua),
    )
    report = audit_profiles([p], {p.id: None})
    codes = issue_codes(report, p.id)
    assert {"browser_version_mismatch", "ua_version_mismatch"} <= codes

    plan = build_fix_plan([p], {p.id: None})
    fixed = FingerprintConfig.model_validate(plan[0]["fingerprint"])
    assert fixed.browser_version == CHROME_FULL
    assert "Chrome/155.0.0.0" in fixed.user_agent


def test_duplicate_seeds_are_critical_and_only_duplicates_are_reassigned():
    first = make_profile("First", fp=windows_fp(canvas_noise_seed=111, audio_noise_seed=222), proxy=proxy(port=8080))
    second = make_profile(
        "Second",
        fp=windows_fp(
            canvas_noise_seed=111,
            audio_noise_seed=222,
            screen_width=1536,
            screen_height=864,
        ),
        proxy=proxy(port=8081),
    )
    report = audit_profiles([first, second], {first.id: None, second.id: None})
    for profile in (first, second):
        codes = issue_codes(report, profile.id)
        assert {"duplicate_canvas_seed", "duplicate_audio_seed"} <= codes

    plan = build_fix_plan([first, second], {first.id: None, second.id: None})
    assert len(plan) == 1, plan
    assert plan[0]["profile_id"] == second.id
    fixed = FingerprintConfig.model_validate(plan[0]["fingerprint"])
    assert fixed.canvas_noise_seed != 111
    assert fixed.audio_noise_seed != 222
    assert fixed.canvas_noise_seed != first.fingerprint.canvas_noise_seed
    assert fixed.audio_noise_seed != first.fingerprint.audio_noise_seed


def test_duplicate_seed_is_ignored_when_canvas_stealth_is_disabled():
    disabled = make_profile(
        "Authentic Canvas",
        fp=windows_fp(canvas_noise_seed=111, stealth_disable=["canvas"]),
        proxy=proxy(port=8080),
    )
    active = make_profile(
        "Unique Canvas",
        fp=windows_fp(
            canvas_noise_seed=111,
            audio_noise_seed=999,
            screen_width=1536,
            screen_height=864,
        ),
        proxy=proxy(port=8081),
    )
    report = audit_profiles([disabled, active], {disabled.id: None, active.id: None})
    assert "duplicate_canvas_seed" not in issue_codes(report, active.id)
    plan = build_fix_plan([disabled, active], {disabled.id: None, active.id: None})
    assert plan == []


def test_shared_proxy_endpoint_is_reported():
    first = make_profile("A", proxy=proxy(host="10.9.9.9", port=9000))
    second = make_profile(
        "B",
        fp=windows_fp(canvas_noise_seed=555555, audio_noise_seed=666666),
        proxy=proxy(host="10.9.9.9", port=9000),
    )
    report = audit_profiles([first, second], {first.id: None, second.id: None})
    for profile in (first, second):
        assert "shared_proxy_endpoint" in issue_codes(report, profile.id)


def test_unresolved_browser_pin_is_reported_and_not_rewritten():
    p = make_profile("Pinned")
    p.browser_path = "chrome-0.0.0.0"
    report = audit_profiles([p], {p.id: None})
    assert "browser_path_unresolved" in issue_codes(report, p.id)
    assert build_fix_plan([p], {p.id: None}) == []


def test_fix_plan_keeps_timezone_geolocation_and_proxy_untouched():
    fp = windows_fp(screen_width=2560, screen_height=1440, webrtc_mode="auto")
    p = make_profile("Keep Location", fp=fp, proxy=proxy())
    plan = build_fix_plan([p], {p.id: None})
    assert plan, "screen/webrtc should be repairable"
    fixed = FingerprintConfig.model_validate(plan[0]["fingerprint"])
    assert fixed.timezone == p.fingerprint.timezone
    assert fixed.latitude == p.fingerprint.latitude
    assert fixed.longitude == p.fingerprint.longitude
    assert fixed.locale == p.fingerprint.locale
    assert fixed.webrtc_mode == "disable_non_proxied"
    assert p.proxy == proxy()


def test_geo_timezone_mismatch_is_reported():
    p = make_profile(
        "Wrong TZ",
        fp=windows_fp(
            timezone="America/New_York",
            latitude=21.0285,
            longitude=105.8542,
        ),
    )
    report = audit_profiles([p], {p.id: None})
    assert "geo_timezone_mismatch" in issue_codes(report, p.id)


@pytest.fixture
def temp_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        storage_module, "PROFILES_JSON", str(tmp_path / "profiles.json")
    )
    monkeypatch.setattr(
        storage_module, "PROFILES_DATA_DIR", str(tmp_path / "profiles")
    )
    monkeypatch.setattr(storage_module, "LOCK_FILE", str(tmp_path / "profiles.lock"))
    return storage_module.ProfileManager()


def test_update_fingerprint_only_preserves_other_fields_and_secret_flow(
    temp_manager, monkeypatch
):
    migrate_calls = []
    monkeypatch.setattr(
        storage_module,
        "migrate_plaintext_passwords",
        lambda profiles: migrate_calls.append(profiles),
    )
    manager = temp_manager
    created = manager.create_profile(
        name="Keep Me",
        fingerprint=windows_fp(),
        notes="notes",
        tags=["tag1"],
    )
    migrate_calls.clear()

    new_fp = windows_fp(screen_width=1600, canvas_noise_seed=777777)
    updated = manager.update_fingerprint_only(
        created.id, new_fp, expected_revision=created.revision
    )
    assert updated is not None
    assert migrate_calls == [], "fingerprint-only update must not touch DPAPI secrets"
    assert updated.fingerprint.screen_width == 1600
    assert updated.name == "Keep Me"
    assert updated.notes == "notes"
    assert updated.tags == ["tag1"]
    assert updated.revision == created.revision + 1

    with open(storage_module.PROFILES_JSON, encoding="utf-8") as fh:
        raw = json.load(fh)
    item = next(entry for entry in raw if entry["id"] == created.id)
    assert item["proxy"]["password"] is None
    assert item["fingerprint"]["screen_width"] == 1600
    assert item["name"] == "Keep Me"


def test_update_fingerprint_only_rejects_stale_revision(temp_manager):
    manager = temp_manager
    created = manager.create_profile(name="Conflict", fingerprint=windows_fp())
    with pytest.raises(RuntimeError, match="Conflict"):
        manager.update_fingerprint_only(
            created.id,
            windows_fp(screen_width=1600),
            expected_revision=created.revision + 5,
        )
    assert manager.get_profile(created.id).revision == created.revision


def test_update_fingerprints_batch_updates_all(temp_manager):
    manager = temp_manager
    first = manager.create_profile(name="One", fingerprint=windows_fp())
    second = manager.create_profile(
        name="Two", fingerprint=windows_fp(canvas_noise_seed=888888)
    )
    changed = manager.update_fingerprints_batch(
        {
            first.id: windows_fp(screen_width=1600),
            second.id: windows_fp(screen_width=1280, canvas_noise_seed=888888),
        },
        expected_revisions={first.id: first.revision, second.id: second.revision},
    )
    assert set(changed) == {first.id, second.id}
    assert manager.get_profile(first.id).revision == first.revision + 1
    assert manager.get_profile(second.id).fingerprint.screen_width == 1280


def test_audit_api_dry_run_apply_and_conflict(temp_manager, monkeypatch):
    manager = temp_manager
    monkeypatch.setattr(app_module, "profile_manager", manager)
    monkeypatch.setattr(
        app_module.launcher, "_resolve_browser_path", lambda profile: None
    )
    migrate_calls = []
    monkeypatch.setattr(
        storage_module,
        "migrate_plaintext_passwords",
        lambda profiles: migrate_calls.append(profiles),
    )

    old_ua = UA_WINDOWS.replace("Chrome/155.0.0.0", "Chrome/124.0.0.0")
    broken = manager.create_profile(
        name="API Broken",
        fingerprint=windows_fp(
            browser_version="124.0.6367.91",
            user_agent=old_ua,
            screen_width=2560,
            screen_height=1440,
        ),
    )
    migrate_calls.clear()

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    login = client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    assert login.status_code == 200

    report_response = client.get("/api/profiles/audit")
    assert report_response.status_code == 200
    report = report_response.json()
    item = next(p for p in report["profiles"] if p["profile_id"] == broken.id)
    assert item["can_fix"] is True
    assert item["issue_counts"]["critical"] + item["issue_counts"]["warning"] > 0
    assert '"password"' not in report_response.text

    dry_run = client.post(
        "/api/profiles/audit/fix",
        json={"profile_ids": [broken.id], "dry_run": True},
    )
    assert dry_run.status_code == 200
    payload = dry_run.json()
    assert payload["dry_run"] is True
    assert payload["changed"] == []
    assert manager.get_profile(broken.id).revision == broken.revision
    assert migrate_calls == []

    conflict = client.post(
        "/api/profiles/audit/fix",
        json={
            "profile_ids": [broken.id],
            "dry_run": False,
            "expected_revisions": {broken.id: broken.revision + 9},
        },
    )
    assert conflict.status_code == 409

    applied = client.post(
        "/api/profiles/audit/fix",
        json={
            "profile_ids": [broken.id],
            "dry_run": False,
            "expected_revisions": {broken.id: broken.revision},
        },
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["changed"] == [broken.id]
    assert migrate_calls == []
    updated = manager.get_profile(broken.id)
    assert updated.revision == broken.revision + 1
    assert updated.fingerprint.browser_version == CHROME_FULL
    assert updated.fingerprint.screen_width == 1920

    refreshed = next(
        p for p in result["report"]["profiles"] if p["profile_id"] == broken.id
    )
    assert refreshed["issue_counts"]["critical"] == 0
    assert refreshed["issue_counts"]["warning"] == 0

    bad = client.post(
        "/api/profiles/audit/fix",
        json={"profile_ids": ["does-not-exist"], "dry_run": True},
    )
    assert bad.status_code == 404
    empty = client.post(
        "/api/profiles/audit/fix",
        json={"profile_ids": [], "dry_run": True},
    )
    assert empty.status_code == 400


def test_audit_fix_subset_still_resolves_seed_collision(temp_manager, monkeypatch):
    """Fixing one selected profile must consider the profiles not selected."""
    manager = temp_manager
    monkeypatch.setattr(app_module, "profile_manager", manager)
    monkeypatch.setattr(
        app_module.launcher, "_resolve_browser_path", lambda profile: None
    )

    first = manager.create_profile(
        name="Seed A",
        fingerprint=windows_fp(canvas_noise_seed=424242, audio_noise_seed=535353),
    )
    second = manager.create_profile(
        name="Seed B",
        fingerprint=windows_fp(canvas_noise_seed=424242, audio_noise_seed=535353),
    )

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    login = client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    assert login.status_code == 200

    report = client.get("/api/profiles/audit").json()
    fixable_ids = {item["profile_id"] for item in report["profiles"] if item["can_fix"]}
    assert fixable_ids == {second.id}, fixable_ids

    applied = client.post(
        "/api/profiles/audit/fix",
        json={
            "profile_ids": [second.id],
            "dry_run": False,
            "expected_revisions": {second.id: second.revision},
        },
    )
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["changed"] == [second.id], result

    kept = manager.get_profile(first.id).fingerprint
    assert kept.canvas_noise_seed == 424242
    assert kept.audio_noise_seed == 535353
    changed = manager.get_profile(second.id).fingerprint
    assert changed.canvas_noise_seed != 424242
    assert changed.audio_noise_seed != 535353

    fresh = client.get("/api/profiles/audit").json()
    for profile_id in (first.id, second.id):
        codes = issue_codes(fresh, profile_id)
        assert "duplicate_canvas_seed" not in codes, codes
        assert "duplicate_audio_seed" not in codes, codes

