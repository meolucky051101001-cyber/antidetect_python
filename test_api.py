import atexit
import os
import shutil
import sys
import time
import tempfile
import threading
import asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

TEST_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-api-")
os.environ["ANTIDETECT_DATA_DIR"] = TEST_DATA_DIR
atexit.register(shutil.rmtree, TEST_DATA_DIR, True)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import web.app as app_module
from antidetect.fingerprint import get_host_device_memory

app = app_module.app

client = TestClient(app, base_url="http://127.0.0.1:8000")


def login():
    response = client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app.state.access_key},
    )
    assert response.status_code == 200


def test_api_routes():
    # 1. Home page must be the unlock page until /auth/login sets a cookie.
    client.cookies.clear()
    r = client.get("/")
    assert r.status_code == 200
    assert "Mở Antidetect Browser" in r.text

    # 2. API rejects unauthenticated calls, then works after login.
    r = client.get("/api/profiles")
    assert r.status_code == 401
    login()
    r = client.get("/")
    assert r.status_code == 200
    assert "Antidetect Python" in r.text

    # 3. List profiles
    r = client.get("/api/profiles")
    assert r.status_code == 200
    profiles = r.json()
    assert isinstance(profiles, list)
    # Field điều khiển chế độ stealth phải được expose cho UI/automation.
    assert all("stealth_disable" in p["fingerprint"] for p in profiles)

    # 4. Create profile
    r = client.post(
        "/api/profiles", json={"name": "API Test Profile", "os": "macos"}
    )
    assert r.status_code == 200
    p = r.json()
    assert p["name"] == "API Test Profile"
    assert p["fingerprint"]["os"] == "macos"
    p_id = p["id"]

    # 5. Get profile
    r = client.get(f"/api/profiles/{p_id}")
    assert r.status_code == 200
    assert r.json()["id"] == p_id

    # 6. Delete profile
    r = client.delete(f"/api/profiles/{p_id}")
    assert r.status_code == 200

    print("\n[OK] Toàn bộ API Endpoints hoạt động trơn tru!")


def test_api_rejects_cross_origin_browser_requests():
    login()
    response = client.get(
        "/api/profiles",
        headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403

    response = client.post(
        "/api/profiles",
        headers={"Origin": "http://127.0.0.1:3000", "Sec-Fetch-Site": "same-site"},
        json={"name": "Must not be created"},
    )
    assert response.status_code == 403


def test_cookie_api_runs_blocking_cdp_calls_off_event_loop():
    login()
    profile = app_module.profile_manager.create_profile(name="Cookie API Test")

    def assert_no_running_event_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        else:
            raise AssertionError("CDP call blocked the event loop")

    def export_from_worker(profile_id):
        assert_no_running_event_loop()
        return [{"name": "qa-cookie"}]

    def import_from_worker(profile_id, cookies):
        assert_no_running_event_loop()
        return True

    try:
        with patch.object(app_module.launcher, "is_running", return_value=True):
            with patch.object(
                app_module.launcher,
                "export_cookies",
                side_effect=export_from_worker,
            ):
                response = client.get(f"/api/profiles/{profile.id}/cookies/export")
                assert response.status_code == 403
                response = client.get(
                    f"/api/profiles/{profile.id}/cookies/export",
                    headers={"x-confirm-cookie-access": "yes"},
                )
                assert response.status_code == 200
                assert response.headers["cache-control"].startswith("no-store")
                assert response.json()["cookies"] == [{"name": "qa-cookie"}]

            with patch.object(
                app_module.launcher,
                "import_cookies",
                side_effect=import_from_worker,
            ):
                response = client.post(
                    f"/api/profiles/{profile.id}/cookies/import",
                    json={"cookies": [{"name": "qa-cookie"}]},
                )
                assert response.status_code == 200
                assert response.json()["success"] is True
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_start_all_api_defaults_to_eight_and_reports_queue():
    login()
    profiles = [
        app_module.profile_manager.create_profile(name=f"Queue API Test {i}")
        for i in range(10)
    ]
    called = threading.Event()
    captured = {}

    def fake_launch_all(**kwargs):
        captured.update(kwargs)
        called.set()
        return []

    try:
        with patch.object(app_module.launcher, "launch_all", side_effect=fake_launch_all):
            response = client.post("/api/profiles/start-all")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["total"] == 10
            assert data["starting_count"] == 10
            # Mặc định giờ là auto theo RAM máy (4/6/8), không cố định 8.
            expected = app_module._auto_max_concurrent()
            assert data["max_concurrent"] == expected
            assert called.wait(3), "start-all background thread did not call launch_all"
            assert captured["max_concurrent"] == expected
            assert captured["open_url"] is None

            # Client vẫn override được bằng query param.
            captured.clear()
            called.clear()
            r2 = client.post("/api/profiles/start-all?max_concurrent=3")
            if r2.status_code == 200:
                # job trước có thể còn "running" — nếu 409 thì bỏ qua phần này.
                assert called.wait(3)
                assert captured["max_concurrent"] == 3
    finally:
        for profile in profiles:
            app_module.profile_manager.delete_profile(profile.id)


def test_auto_max_concurrent_follows_host_ram():
    """Bucket độsong song theo RAM: 8GB→4, 16GB→6, ≥32GB→8."""
    assert app_module._auto_max_concurrent() in (4, 6, 8)
    # Sinh theo đúng bucket của RAM host.
    ram = get_host_device_memory()
    expected = 4 if ram <= 8 else (6 if ram <= 16 else 8)
    assert app_module._auto_max_concurrent() == expected


def test_proxy_password_is_redacted_by_api():
    login()
    response = client.post(
        "/api/profiles",
        json={
            "name": "Redaction Test",
            "os": "windows",
            "proxy": {
                "enabled": False,
                "protocol": "http",
                "host": "127.0.0.1",
                "port": 9,
                "username": "u",
                "password": "secret_password",
            },
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["proxy"]["password"] == "********"
    profile_id = data["id"]
    try:
        fetched = client.get(f"/api/profiles/{profile_id}").json()
        assert fetched["proxy"]["password"] == "********"
        # The update endpoint keeps the real password when the client sends
        # the redacted sentinel or leaves the field empty.
        update = client.put(
            f"/api/profiles/{profile_id}",
            json={
                "proxy": {
                    "enabled": False,
                    "protocol": "http",
                    "host": "127.0.0.1",
                    "port": 9,
                    "username": "u",
                    "password": "********",
                }
            },
        )
        assert update.status_code == 200
        stored = app_module.profile_manager.get_profile(profile_id)
        assert stored.proxy.password == "secret_password"
    finally:
        app_module.profile_manager.delete_profile(profile_id)


def test_browser_start_and_stop_run_off_event_loop():
    login()
    profile = app_module.profile_manager.create_profile(name="Launch API Test")

    def assert_no_running_event_loop(*_args, **_kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"success": True, "profile_id": profile.id}
        raise AssertionError("Browser process call blocked the event loop")

    try:
        with patch.object(
            app_module.launcher, "launch", side_effect=assert_no_running_event_loop
        ):
            response = client.post(f"/api/profiles/{profile.id}/start")
            assert response.status_code == 200
            assert response.json()["success"] is True

        with patch.object(
            app_module.launcher, "stop", side_effect=assert_no_running_event_loop
        ):
            response = client.post(f"/api/profiles/{profile.id}/stop")
            assert response.status_code == 200
            assert response.json()["success"] is True
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_p1_features():
    login()

    # 1. Non-existent profile start and stop return 404
    fake_id = "non-existent-profile-12345"
    res_start = client.post(f"/api/profiles/{fake_id}/start")
    assert res_start.status_code == 404, f"Expected 404, got {res_start.status_code}"

    res_stop = client.post(f"/api/profiles/{fake_id}/stop")
    assert res_stop.status_code == 404, f"Expected 404, got {res_stop.status_code}"

    # 2. switch-location-all when 0 profiles exist returns 404
    # Ensure no profiles exist temporarily
    all_profiles = app_module.profile_manager.list_profiles()
    for p in all_profiles:
        app_module.profile_manager.delete_profile(p.id)

    res_loc_empty = client.post(
        "/api/profiles/switch-location-all",
        json={"city": "hanoi", "timezone": "Asia/Ho_Chi_Minh"}
    )
    assert res_loc_empty.status_code == 404, f"Expected 404 on 0 profiles, got {res_loc_empty.status_code}"

    # 3. Create a profile and test invalid timezone returns 400
    p1 = app_module.profile_manager.create_profile(name="P1 Test Profile")
    try:
        res_bad_tz = client.post(
            "/api/profiles/switch-location-all",
            json={"city": "hanoi", "timezone": "Invalid/Fake_Zone"}
        )
        assert res_bad_tz.status_code == 400, f"Expected 400 for bad timezone, got {res_bad_tz.status_code}"

        # Valid switch-location-all
        res_good_loc = client.post(
            "/api/profiles/switch-location-all",
            json={"city": "hanoi", "timezone": "Asia/Ho_Chi_Minh"}
        )
        assert res_good_loc.status_code == 200
        loc_data = res_good_loc.json()
        assert loc_data["success"] is True
        assert loc_data["count"] == 1
        assert len(loc_data["profiles"]) == 1
        assert loc_data["profiles"][0]["timezone"] == "Asia/Ho_Chi_Minh"

        # 4. Start-All queue management and 409 conflict
        started_event = threading.Event()
        block_event = threading.Event()

        def blocking_launch_all(**kwargs):
            started_event.set()
            block_event.wait(timeout=2.0)
            return []

        with patch.object(app_module.launcher, "launch_all", side_effect=blocking_launch_all):
            # First start-all succeeds
            r1 = client.post("/api/profiles/start-all")
            assert r1.status_code == 200
            assert started_event.wait(timeout=1.0)

            # Concurrent start-all gets 409 Conflict
            r2 = client.post("/api/profiles/start-all")
            assert r2.status_code == 409, f"Expected 409 Conflict, got {r2.status_code}"

            # Status endpoint shows running
            r_status = client.get("/api/profiles/start-all/status")
            assert r_status.status_code == 200
            assert r_status.json()["active"] is True

            # Cancel endpoint sends cancel signal
            r_cancel = client.post("/api/profiles/start-all/cancel")
            assert r_cancel.status_code == 200
            assert r_cancel.json()["success"] is True

            block_event.set()
    finally:
        app_module.profile_manager.delete_profile(p1.id)


def test_p2_features():
    login()

    # 1. Test Content Security Policy header (P2-04)
    res = client.get("/api/profiles")
    assert res.status_code == 200
    assert "content-security-policy" in res.headers
    csp = res.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp

    # 2. Test RAM Cache (st_mtime_ns) performance (P2-05)
    # Consecutive list_profiles calls should use cache
    p = app_module.profile_manager.create_profile(name="Cache Test Profile")
    try:
        t0 = time.perf_counter()
        for _ in range(50):
            profiles = app_module.profile_manager.list_profiles()
            assert len(profiles) > 0
        t1 = time.perf_counter()
        elapsed = t1 - t0
        # 50 reads from RAM cache should finish in well under 50ms (< 1ms per call)
        assert elapsed < 0.5, f"Cache too slow: 50 calls took {elapsed:.4f}s"

        # 3. WebRTC leak protection is enforced by Chrome policy flags and
        # mDNS, not by JS hooks. Hooking RTCPeerConnection is itself a CreepJS
        # lie and made the constructor non-native.
        from antidetect.stealth import get_stealth_script
        fp = p.fingerprint
        fp.webrtc_mode = "disable_non_proxied"
        script = get_stealth_script(fp)
        assert "RTCPeerConnection" not in script
        assert "webrtcMode" in script
    finally:
        app_module.profile_manager.delete_profile(p.id)


def test_update_profile_expected_revision():
    """Opt-in strict concurrency: stale revision phải bị từ chối 409."""
    login()
    r = client.post("/api/profiles", json={"name": "Revision Test", "os": "windows"})
    assert r.status_code == 200
    p = r.json()
    try:
        # Revision đúng → 200.
        r = client.put(
            f"/api/profiles/{p['id']}",
            json={"name": "Rev OK", "expected_revision": p["revision"]},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Rev OK"
        new_rev = r.json()["revision"]

        # Revision cũ (stale) → 409, không ghi đè.
        r = client.put(
            f"/api/profiles/{p['id']}",
            json={"name": "Rev Stale", "expected_revision": p["revision"]},
        )
        assert r.status_code == 409

        # Không truyền expected_revision (mặc định) → vẫn ghi bình thường.
        r = client.put(
            f"/api/profiles/{p['id']}",
            json={"name": "Rev NoCheck"},
        )
        assert r.status_code == 200
        assert r.json()["revision"] == new_rev + 1
    finally:
        client.delete(f"/api/profiles/{p['id']}")


def test_delete_missing_profile_returns_404():
    login()
    r = client.delete("/api/profiles/does-not-exist-404")
    assert r.status_code == 404


def test_facebook_pages_endpoint(monkeypatch):
    """Fanpage listing: cache khi idle, live qua launcher khi refresh."""
    login()
    r = client.post("/api/profiles", json={"name": "FB Pages Test", "os": "windows"})
    assert r.status_code == 200
    p = r.json()
    try:
        # 1. Idle + không cache → trả cache rỗng kèm gợi ý, không lỗi.
        r = client.get(f"/api/profiles/{p['id']}/facebook/pages")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "cache" and body["pages"] == []
        assert body["detail"]

        # 2. Idle + refresh → 409 (cần profile đang chạy).
        r = client.get(f"/api/profiles/{p['id']}/facebook/pages?refresh=1")
        assert r.status_code == 409

        # 3. Running + refresh → live qua launcher, kết quả lưu vào profile.
        monkeypatch.setattr(
            app_module.launcher, "is_running", lambda pid: True, raising=False
        )
        fake_pages = [
            {"name": "Cửa Hàng ABC", "url": "https://www.facebook.com/cuahangabc", "id": "1234567890"},
            {"name": "Nhà Sách XYZ", "url": "https://www.facebook.com/pages/Nha-Sach-XYZ/9876543210", "id": "9876543210"},
        ]
        monkeypatch.setattr(
            app_module.launcher,
            "fetch_facebook_pages",
            lambda pid, timeout=40.0, deep=False: {"logged_in": True, "pages": fake_pages},
            raising=False,
        )
        r = client.get(f"/api/profiles/{p['id']}/facebook/pages?refresh=1")
        assert r.status_code == 200
        body = r.json()
        assert body["success"] and body["source"] == "live"
        # Endpoint enrich thêm status/ready/age_days cho từng page.
        for orig, got in zip(fake_pages, body["pages"]):
            assert all(got.get(k) == v for k, v in orig.items())
            assert {"status", "ready", "age_days"} <= set(got)
        assert body["synced_at"] > 0

        # 4. Lần gọi sau không refresh → cache đã lưu trong profile.
        monkeypatch.setattr(
            app_module.launcher, "is_running", lambda pid: False, raising=False
        )
        r = client.get(f"/api/profiles/{p['id']}/facebook/pages")
        body = r.json()
        assert body["source"] == "cache"
        for orig, got in zip(fake_pages, body["pages"]):
            assert all(got.get(k) == v for k, v in orig.items())

        # 5. Chưa đăng nhập FB → logged_in=false + hướng dẫn, không coi là lỗi server.
        monkeypatch.setattr(
            app_module.launcher, "is_running", lambda pid: True, raising=False
        )
        monkeypatch.setattr(
            app_module.launcher,
            "fetch_facebook_pages",
            lambda pid, timeout=40.0, deep=False: {"logged_in": False, "pages": []},
            raising=False,
        )
        r = client.get(f"/api/profiles/{p['id']}/facebook/pages?refresh=1")
        assert r.status_code == 200
        body = r.json()
        assert body["logged_in"] is False and body["pages"] == []
        assert "đăng nhập" in body["detail"]
    finally:
        client.delete(f"/api/profiles/{p['id']}")


def test_facebook_pages_ui_and_scraper_present():
    html = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "templates", "index.html"),
        encoding="utf-8",
    ).read()
    assert "fanpagesModal" in html
    assert "openFanpagesModal" in html
    assert "refreshFanpages" in html

    src = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "antidetect", "launcher.py"),
        encoding="utf-8",
    ).read()
    # Scrape đọc DOM bookmarks/pages; tab tool mở phải được đóng trong finally.
    assert "_FB_SCRAPE_JS" in src
    assert "bookmarks/pages" in src
    assert "/json/close/" in src
    # Tab được mở bằng session của chính profile (qua CDP của nó).
    assert "def fetch_facebook_pages" in src


def _wait_for_bulk_idle(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_module._bulk_launch_lock.acquire(blocking=False):
            app_module._bulk_launch_lock.release()
            return
        time.sleep(0.05)
    raise AssertionError("bulk launch lock vẫn bị giữ")


def test_schedule_partial_update_toggles_enabled():
    """UI chỉ gửi {enabled} — PUT không được đòi lại time/action (422)."""
    login()
    from antidetect.scheduler import ScheduleStore

    path = os.path.join(TEST_DATA_DIR, "schedules-partial-update.json")
    previous = app_module._schedule_store
    app_module._schedule_store = ScheduleStore(path)
    try:
        created = client.post("/api/schedules", json={
            "name": "Toggle", "time": "07:30", "action": "start_all", "enabled": True,
        })
        assert created.status_code == 200, created.text
        rule = created.json()["rule"]

        toggled = client.put(f"/api/schedules/{rule['id']}", json={"enabled": False})
        assert toggled.status_code == 200, toggled.text
        updated = toggled.json()["rule"]
        assert updated["enabled"] is False
        assert updated["time"] == "07:30"
        assert updated["action"] == "start_all"

        fresh = ScheduleStore(path)
        assert fresh.list()[0]["enabled"] is False

        bad_time = client.put(f"/api/schedules/{rule['id']}", json={"time": "25:99"})
        assert bad_time.status_code == 400
        missing = client.put("/api/schedules/does-not-exist", json={"enabled": False})
        assert missing.status_code == 404
    finally:
        app_module._schedule_store = previous


def test_start_all_held_bulk_lock_returns_409_without_stale_job():
    login()
    profile = app_module.profile_manager.create_profile(name="Bulk Lock Test")
    try:
        _wait_for_bulk_idle()
        app_module._active_start_all_job = None
        assert app_module._bulk_launch_lock.acquire(blocking=False)
        try:
            response = client.post("/api/profiles/start-all")
            assert response.status_code == 409, response.text
            # Không được tạo job "running" nếu chưa nắm được bulk lock.
            assert app_module._active_start_all_job is None
        finally:
            app_module._bulk_launch_lock.release()
    finally:
        app_module._active_start_all_job = None
        app_module.profile_manager.delete_profile(profile.id)


def test_start_all_launcher_exception_ends_job_as_failed():
    login()
    profile = app_module.profile_manager.create_profile(name="Bulk Failure Test")
    try:
        _wait_for_bulk_idle()
        app_module._active_start_all_job = None

        def boom(**_kwargs):
            raise RuntimeError("synthetic bulk failure")

        with patch.object(app_module.launcher, "launch_all", side_effect=boom):
            response = client.post("/api/profiles/start-all")
            assert response.status_code == 200, response.text
            job_id = response.json()["job_id"]

            deadline = time.time() + 5
            job = None
            while time.time() < deadline:
                status = client.get("/api/profiles/start-all/status").json()
                job = status["job"]
                if job and job["job_id"] == job_id and job["status"] != "running":
                    break
                time.sleep(0.05)

            assert job is not None and job["job_id"] == job_id
            assert job["status"] == "failed"
            assert "synthetic bulk failure" in job.get("error", "")
            assert client.get("/api/profiles/start-all/status").json()["active"] is False
    finally:
        app_module._active_start_all_job = None
        app_module.profile_manager.delete_profile(profile.id)


def test_profiles_with_status_exposes_uptime_and_ram():
    """Hàm status còn hiệu lực phải trả uptime/RAM, không bị bản trùng ghi đè."""
    login()
    profile = app_module.profile_manager.create_profile(name="Runtime Fields Test")
    try:
        fake_port = 6553
        started = int(time.time()) - 120
        with patch.object(app_module.launcher, "is_running", return_value=True), \
             patch.object(app_module.launcher, "active_ports", {profile.id: fake_port}), \
             patch.object(
                 app_module.launcher._runtime_store, "get",
                 return_value={"started_at": started},
             ), \
             patch.object(
                 app_module, "_ram_by_profile_dir",
                 return_value={profile.id: 321.5},
             ):
            items = {p.id: p for p in app_module._profiles_with_status()}
        item = items[profile.id]
        assert item.status == "running"
        assert item.port == fake_port
        assert item.ram_mb == 321.5
        assert item.uptime_seconds is not None and item.uptime_seconds >= 118
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_docs_and_openapi_require_session():
    client.cookies.clear()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 401, path
    login()
    for path in ("/docs", "/redoc"):
        assert client.get(path).status_code == 200, path
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "openapi" in schema.json()
    client.cookies.clear()
    login()


def test_docs_csp_allows_cdn_while_dashboard_stays_strict():
    login()
    docs_csp = client.get("/docs").headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in docs_csp
    assert "https://fonts.googleapis.com" in docs_csp
    assert "https://fonts.gstatic.com" in docs_csp
    dashboard_csp = client.get("/").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in dashboard_csp
    assert "fonts.googleapis.com" not in dashboard_csp
    assert "script-src 'self' 'unsafe-inline';" in dashboard_csp


def test_login_non_ascii_token_returns_401_not_500():
    class FakeRequest:
        headers = {"authorization": "Bearer khóa-mở-ứng-dụng-☃"}

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(app_module.login(FakeRequest()))
    assert excinfo.value.status_code == 401


def test_bulk_endpoints_reject_explicit_empty_selection():
    login()
    r = client.post("/api/profiles/start-all", json={"profile_ids": []})
    assert r.status_code == 400, r.text
    r = client.post("/api/profiles/stop-all", json={"profile_ids": []})
    assert r.status_code == 400, r.text


def test_fanpage_refresh_keeps_cache_when_live_result_empty():
    login()
    profile = app_module.profile_manager.create_profile(name="Fanpage Cache Test")
    old = {
        "name": "Old Page",
        "url": "https://www.facebook.com/oldpage",
        "id": "123456",
    }
    profile.facebook_pages = [old]
    profile.facebook_pages_synced_at = 1700000000
    app_module.profile_manager.update_profile(profile)
    try:
        with patch.object(app_module.launcher, "is_running", return_value=True), \
             patch.object(
                 app_module.launcher, "fetch_facebook_pages",
                 return_value={"logged_in": True, "pages": []},
             ):
            r = client.get(f"/api/profiles/{profile.id}/facebook/pages?refresh=1")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["stale"] is True
            assert data["source"] == "cache"
            assert [p["url"] for p in data["pages"]] == [old["url"]]
            assert data["synced_at"] == 1700000000

            r2 = client.get(
                f"/api/profiles/{profile.id}/facebook/pages?refresh=1&force=1"
            )
            assert r2.status_code == 200, r2.text
            data2 = r2.json()
            assert data2["stale"] is False
            assert data2["pages"] == []
        assert app_module.profile_manager.get_profile(profile.id).facebook_pages == []
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_fanpage_cache_filters_unsafe_urls():
    login()
    profile = app_module.profile_manager.create_profile(name="Fanpage URL Test")
    profile.facebook_pages = [
        {"name": "Bad", "url": "javascript:alert(1)"},
        {"name": "Good", "url": "https://www.facebook.com/goodpage"},
    ]
    app_module.profile_manager.update_profile(profile)
    try:
        r = client.get(f"/api/profiles/{profile.id}/facebook/pages")
        assert r.status_code == 200, r.text
        assert [p["url"] for p in r.json()["pages"]] == [
            "https://www.facebook.com/goodpage"
        ]
    finally:
        app_module.profile_manager.delete_profile(profile.id)


def test_import_profiles_strips_unsafe_fanpage_urls():
    login()
    r = client.post("/api/profiles/import", json={"profiles": [{
        "name": "Imported Fanpage URLs",
        "facebook_pages": [
            {"name": "Bad", "url": "javascript:alert(1)"},
            {
                "name": "Good",
                "url": "https://www.facebook.com/goodpage",
                "id": "123",
            },
        ],
    }]})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["imported_count"] == 1
    pid = data["imported"][0]["id"]
    try:
        stored = app_module.profile_manager.get_profile(pid)
        assert [p["url"] for p in stored.facebook_pages] == [
            "https://www.facebook.com/goodpage"
        ]
    finally:
        app_module.profile_manager.delete_profile(pid)


def test_worker_pool_is_sized_for_parallel_launch():
    pool = app_module._worker_pool()
    try:
        assert getattr(pool, "_max_workers") == 64
    finally:
        pool.shutdown(wait=False)


def test_dashboard_csp_blocks_plugins_and_foreign_forms():
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp


if __name__ == "__main__":
    test_api_routes()
