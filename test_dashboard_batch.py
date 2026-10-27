"""Tests Đợt A/B/C — appended section."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def test_batch_a_export_import_roundtrip():
    """Đợt A: export redact password + import tạo lại profiles đúng."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles", json={"name": "Export A", "os": "windows"})
    pid = r.json()["id"]
    r = client.put(f"/api/profiles/{pid}", json={
        "proxy": {"enabled": True, "protocol": "http", "host": "1.2.3.4",
                  "port": 8080, "username": "u1", "password": "secret123"},
        "tags": ["farm-a"], "notes": "test note"})
    assert r.status_code == 200

    # Export thiếu confirm → 403
    r = client.get("/api/profiles/export")
    assert r.status_code == 403

    # Export đúng → password null, tags/notes giữ
    r = client.get("/api/profiles/export", headers={"x-confirm-export": "yes"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    exported = next(p for p in data["profiles"] if p["name"] == "Export A")
    assert exported["proxy"]["password"] is None
    assert exported["proxy"]["host"] == "1.2.3.4"
    assert exported["tags"] == ["farm-a"]
    assert exported["notes"] == "test note"

    # Import lại → id mới, fingerprint giữ
    r = client.post("/api/profiles/import", json={"profiles": [exported]})
    assert r.status_code == 200
    body = r.json()
    assert body["imported_count"] == 1 and body["failed_count"] == 0
    new_id = body["imported"][0]["id"]
    assert new_id != pid
    fresh = client.get(f"/api/profiles/{new_id}").json()
    assert fresh["proxy"]["host"] == "1.2.3.4"
    assert fresh["fingerprint"]["timezone"] == exported["fingerprint"]["timezone"]

    client.delete(f"/api/profiles/{pid}")
    client.delete(f"/api/profiles/{new_id}")


def test_batch_a_uptime_ram_fields():
    """ProfileRuntime: uptime/ram có trong API response (None khi dừng)."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles", json={"name": "Uptime Test", "os": "windows"})
    pid = r.json()["id"]
    try:
        profiles = client.get("/api/profiles").json()
        entry = next(p for p in profiles if p["id"] == pid)
        assert "uptime_seconds" in entry and "ram_mb" in entry
        assert entry["uptime_seconds"] is None
    finally:
        client.delete(f"/api/profiles/{pid}")


def test_batch_b_bulk_create():
    """Đợt B: bulk-create — fingerprint riêng, proxy theo thứ tự, thiếu = direct."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles/bulk-create", json={
        "count": 4, "os": "windows", "city": "hanoi",
        "stealth_mode": "unique", "name_prefix": "BTest",
        "proxies": ["http://u1:p1@10.0.0.1:8080", "http://u2:p2@10.0.0.2:8080"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created_count"] == 4
    assert len([w for w in body["warnings"] if "direct" in w]) == 2
    names = [c["name"] for c in body["created"]]
    assert names == ["BTest 01", "BTest 02", "BTest 03", "BTest 04"]

    profiles = client.get("/api/profiles").json()
    created = [p for p in profiles if p["name"].startswith("BTest ")]
    assert len(created) == 4
    seeds = {p["fingerprint"]["canvas_noise_seed"] for p in created}
    assert len(seeds) == 4  # fingerprint riêng từng profile
    proxies_on = [p for p in created if p["proxy"]["enabled"]]
    assert len(proxies_on) == 2
    assert {p["proxy"]["host"] for p in proxies_on} == {"10.0.0.1", "10.0.0.2"}
    for p in created:
        client.delete(f"/api/profiles/{p['id']}")


def test_batch_b_authentic_bulk_gets_host_gpu():
    """bulk-create authentic same-OS: stealth_disable 5 nhóm + GPU hợp lệ theo OS."""
    import web.app as app_module
    from fastapi.testclient import TestClient
    from antidetect.fingerprint import GPU_PROFILES, host_os

    target_os = host_os() if host_os() in GPU_PROFILES else "windows"
    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles/bulk-create", json={
        "count": 1, "os": target_os, "stealth_mode": "authentic",
        "name_prefix": "AuthBulk",
    })
    assert r.status_code == 200
    pid = r.json()["created"][0]["id"]
    try:
        p = client.get(f"/api/profiles/{pid}").json()
        assert set(p["fingerprint"]["stealth_disable"]) == {
            "canvas", "webgl", "audio", "media", "worker"}
        fp = p["fingerprint"]
        assert any(
            fp["webgl_vendor"] == gpu["vendor"]
            and fp["webgl_renderer"] == gpu["renderer"]
            for gpu in GPU_PROFILES[target_os]
        )
    finally:
        client.delete(f"/api/profiles/{pid}")


def test_batch_b_authentic_cross_os_rejected():
    """authentic + OS khác host phải bị từ chối thay vì tạo fingerprint mâu thuẫn."""
    import web.app as app_module
    from fastapi.testclient import TestClient
    from antidetect.fingerprint import GPU_PROFILES, host_os

    target_os = host_os() if host_os() in GPU_PROFILES else "windows"
    other_os = next(os_ for os_ in GPU_PROFILES if os_ != target_os)
    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles/bulk-create", json={
        "count": 1, "os": other_os, "stealth_mode": "authentic",
        "name_prefix": "AuthCross",
    })
    assert r.status_code == 400
    assert "cùng hệ điều hành" in r.json()["detail"]


def test_host_gpu_strings_matches_declared_table():
    from web.services import host_gpu_strings
    from antidetect.fingerprint import GPU_PROFILES, host_os

    gpus = GPU_PROFILES.get(host_os()) or GPU_PROFILES["windows"]
    vendor, renderer = host_gpu_strings()
    assert any(
        vendor == gpu["vendor"] and renderer == gpu["renderer"] for gpu in gpus
    )


def test_batch_c_scheduler_store_and_due():
    """ScheduleStore CRUD + Scheduler due/catchup/disabled logic."""
    import tempfile
    from datetime import datetime, timedelta
    from antidetect.scheduler import ScheduleStore, Scheduler

    path = os.path.join(tempfile.mkdtemp(prefix="sched-test-"), "schedules.json")
    store = ScheduleStore(path)
    rule = store.add("Mở farm", "07:30", "start_all", profile_ids=[])
    assert store.list()[0]["time"] == "07:30"
    store.update(rule["id"], enabled=False)
    assert store.list()[0]["enabled"] is False
    store.update(rule["id"], enabled=True, time="08:00")
    assert store.list()[0]["time"] == "08:00"
    try:
        store.add("bad", "25:99", "stop_all")
        raise AssertionError("phải raise giờ sai")
    except ValueError:
        pass

    sch = Scheduler(store, callbacks={}, check_interval=0.2)
    now = datetime.now()
    r = {"id": "x", "enabled": True,
         "time": (now - timedelta(minutes=5)).strftime("%H:%M"),
         "last_run_date": None}
    assert sch._due(r, now) == "due"
    r["last_run_date"] = now.strftime("%Y-%m-%d")
    assert sch._due(r, now) is None
    r["last_run_date"] = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    assert sch._due(r, now) == "catchup"
    r["last_run_date"] = None
    r["time"] = (now - timedelta(minutes=120)).strftime("%H:%M")
    assert sch._due(r, now) is None
    r["time"] = (now + timedelta(minutes=30)).strftime("%H:%M")
    assert sch._due(r, now) is None
    r["time"] = (now - timedelta(minutes=2)).strftime("%H:%M")
    r["enabled"] = False
    assert sch._due(r, now) is None


def test_batch_c_scheduler_api_endpoints():
    """Scheduler REST API qua TestClient."""
    import tempfile
    import web.app as app_module
    from fastapi.testclient import TestClient
    from antidetect.scheduler import ScheduleStore, Scheduler

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    if not app_module._schedule_store:
        app_module._schedule_store = ScheduleStore(
            os.path.join(tempfile.mkdtemp(prefix="sched-api-"), "schedules.json"))
        app_module._scheduler = Scheduler(app_module._schedule_store, callbacks={})

    r = client.post("/api/schedules", json={
        "name": "Test sched", "time": "07:30", "action": "start_all"})
    assert r.status_code == 200
    rid = r.json()["rule"]["id"]
    assert any(x["id"] == rid for x in client.get("/api/schedules").json()["rules"])

    r = client.put(f"/api/schedules/{rid}", json={
        "name": "Test sched", "time": "09:00", "action": "start_all", "enabled": False})
    assert r.status_code == 200
    r = client.put(f"/api/schedules/{rid}", json={
        "name": "bad", "time": "99:99", "action": "start_all"})
    assert r.status_code == 400
    assert client.delete(f"/api/schedules/{rid}").status_code == 200
    assert client.delete(f"/api/schedules/{rid}").status_code == 404


def test_batch_c_api_token_rotate_and_revoke():
    """API token: rotate → dùng được; revoke → 401; plaintext không nằm trên đĩa."""
    import web.app as app_module
    from fastapi.testclient import TestClient
    from antidetect.runtime_auth import read_api_token_hash, delete_api_token_hash

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    try:
        r = client.post("/api/auth/token/rotate")
        assert r.status_code == 200
        token = r.json()["token"]
        stored = read_api_token_hash()
        assert stored is not None and token not in str(stored)

        client2 = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
        r = client2.get("/api/profiles", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        r = client2.get("/api/profiles",
                        headers={"Authorization": "Bearer wrong-token-1234567890abcdefgh"})
        assert r.status_code == 401

        assert client.delete("/api/auth/token").status_code == 200
        r = client2.get("/api/profiles", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
    finally:
        delete_api_token_hash()


def test_dashboard_new_management_ui():
    """Dashboard: checkbox multi-select + bulk bar + wizard + import + schedules + token modals."""
    html = open(os.path.join(BASE_DIR, "web", "templates", "index.html"), encoding="utf-8").read()
    js = open(
        os.path.join(BASE_DIR, "web", "static", "js", "dashboard.js"), encoding="utf-8"
    ).read()
    assert 'id="select-all-chk"' in html and "toggleSelect" in js
    assert 'id="bulk-bar"' in html and "bulkStart" in js and "bulkDelete" in js
    assert "openWizardModal" in html and "/api/profiles/bulk-create" in js
    assert "openImportModal" in html and "/api/profiles/import" in js
    assert "exportProfiles" in html and "x-confirm-export" in js
    assert "openSchedulesModal" in html and "/api/schedules" in js
    assert "openTokenModal" in html and "/api/auth/token/rotate" in js
    assert "formatUptime" in js and "ram_mb" in js
    # Tags UI
    assert 'id="edit-tags"' in html and 'id="tag-filter"' in html


def test_edit_profile_sends_expected_revision_and_handles_conflict():
    """Edit modal phải gửi revision và không ghi đè khi server trả 409."""
    js = open(
        os.path.join(BASE_DIR, "web", "static", "js", "dashboard.js"), encoding="utf-8"
    ).read()
    assert "expected_revision: (editingProfile && Number.isInteger(editingProfile.revision))" in js
    assert "res.status === 409" in js
    assert "Profile đã bị sửa ở tab/phiên khác" in js
