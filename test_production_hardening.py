"""Regression tests cho đợt hardening production 2026-10-24.

Che phủ các fix sau review toàn diện:
- B1: read-modify-write profiles.json nằm trong interprocess lock reentrant.
- H1: atexit chỉ dừng browser do chính process đó launch.
- H2: secrets.dpapi được fsync + backup timestamped trước khi ghi đè.
- H3: lỗi migrate secret không bị nuốt khi caller gửi plaintext password.
- M1: BrowserRuntimeStore re-load + lock khi upsert/remove (2 instance).
- stealth_disable per-profile ("authentic" mode) và native brands runtime.
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

TEST_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-prodhard-")
os.environ["ANTIDETECT_DATA_DIR"] = TEST_DATA_DIR
atexit.register(shutil.rmtree, TEST_DATA_DIR, True)

from antidetect.models import Profile, FingerprintConfig
from antidetect.storage import ProfileManager, ReentrantInterProcessLock
from antidetect.secrets import set_proxy_password, get_proxy_password, _write_all_secrets, _read_all_secrets
import antidetect.secrets as secrets_module
import antidetect.storage as storage_module
import antidetect.launcher as launcher_module
import antidetect.process_identity as process_identity
from antidetect.browser_runtime import BrowserRuntimeStore
from antidetect.launcher import BrowserLauncher
from antidetect.fingerprint import generate_fingerprint
from antidetect.stealth import get_stealth_script, get_worker_stealth_script


manager = ProfileManager()


@pytest.fixture(autouse=True)
def _pin_data_dir():
    data_dir = os.path.realpath(storage_module.DATA_DIR)
    repo_data = os.path.realpath(os.path.join(BASE_DIR, "data"))
    assert data_dir != repo_data, "Tests must not use the real data directory"
    old = os.environ.get("ANTIDETECT_DATA_DIR")
    os.environ["ANTIDETECT_DATA_DIR"] = storage_module.DATA_DIR
    yield
    if old is None:
        os.environ.pop("ANTIDETECT_DATA_DIR", None)
    else:
        os.environ["ANTIDETECT_DATA_DIR"] = old


# --- B1: interprocess lock quanh read-modify-write ---

def test_reentrant_ipc_lock_releases_once():
    lock = ReentrantInterProcessLock(os.path.join(TEST_DATA_DIR, "t.lock"))
    with lock:
        with lock:  # reentrant: không deadlock, không giữ 2 file handle
            pass
    # Sau khi thoát hết, lock phải lấy được lại từ "process" khác (handle mới).
    with ReentrantInterProcessLock(os.path.join(TEST_DATA_DIR, "t.lock"), timeout=1.0):
        pass


def test_create_waits_for_interprocess_lock_and_keeps_data():
    m1 = ProfileManager()
    m1.create_profile("B1-existing")
    m2 = ProfileManager()

    done = threading.Event()

    def creator():
        m2.create_profile("B1-added-under-lock")
        done.set()

    # Mô phỏng process khác đang giữa chuỗi RMW: create phải chờ, không ghi
    # song song rồi chép đè list stale.
    with m1._ipc_lock:
        t = threading.Thread(target=creator)
        t.start()
        blocked = not done.wait(0.4)
    t.join(timeout=5)
    assert blocked, "create_profile phải chờ interprocess lock"
    assert not t.is_alive()
    names = {p.name for p in m1.list_profiles()}
    assert {"B1-existing", "B1-added-under-lock"} <= names


def test_update_profiles_batch_updates_once_and_skips_missing():
    m = ProfileManager()
    p1 = m.create_profile("batch-1")
    p2 = m.create_profile("batch-2")
    other = m.create_profile("batch-other")
    rev_before = p1.revision

    p1.notes = "updated-1"
    p2.notes = "updated-2"
    ghost = Profile(id="nonexistent-id", fingerprint=generate_fingerprint())
    ghost.notes = "ghost"

    updated_ids = m.update_profiles_batch([p1, p2, ghost])
    assert set(updated_ids) == {p1.id, p2.id}

    fresh = {p.id: p for p in m.list_profiles()}
    assert fresh[p1.id].notes == "updated-1"
    assert fresh[p2.id].notes == "updated-2"
    assert fresh[other.id].notes == ""
    # revision tăng đúng một lần cho mỗi profile trong batch.
    assert fresh[p1.id].revision == rev_before + 1


# --- H1: atexit không giết browser của process khác ---

def test_cleanup_on_exit_only_stops_own_processes(monkeypatch):
    m = ProfileManager()
    launcher = BrowserLauncher(m)
    stopped = []

    def fake_stop(profile_id):
        stopped.append(profile_id)
        return {"success": True}

    monkeypatch.setattr(launcher, "stop", fake_stop)
    # Browser của "process khác" chỉ nằm trong runtime store.
    launcher._runtime_store.upsert("foreign-profile", pid=424242, port=1)

    launcher._cleanup_on_exit()
    assert stopped == [], "atexit không được dừng browser theo runtime store"

    launcher.active_processes["own-profile"] = object()  # đủ cho iteration
    launcher._cleanup_on_exit()
    assert stopped == ["own-profile"]
    launcher.unregister_cleanup()


# --- H2: secrets backup + fsync ---

def test_secret_store_backup_created_and_rotated():
    # Ghi 8 lần; backups/secrets_*.dpapi phải tồn tại và giữ tối đa 5 bản.
    for i in range(8):
        set_proxy_password("h2-profile", f"pw-{i}", identity="http://h:1|u")
    # Secret store ghi theo env được fixture pin về storage.DATA_DIR.
    backup_dir = os.path.join(storage_module.DATA_DIR, "backups")
    backups = [f for f in os.listdir(backup_dir) if f.startswith("secrets_") and f.endswith(".dpapi")]
    assert 0 < len(backups) <= 5
    assert get_proxy_password("h2-profile", identity="http://h:1|u") == "pw-7"


def test_secret_write_fsync_before_replace(monkeypatch):
    synced = []
    real_replace = os.replace

    def spy_replace(src, dst, *a, **kw):
        # fsync phải đã chạy trên file pending trước khi replace.
        assert synced, "os.replace chạy trước os.fsync trên file pending"
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(secrets_module.os, "replace", spy_replace)
    # os.fsync chỉ đếm khi gọi trên file pending của secret store.
    real_fsync = os.fsync

    def spy_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(secrets_module.os, "fsync", spy_fsync)
    set_proxy_password("h2-fsync", "pw", identity="http://h:2|u")


# --- H3: không nuốt lỗi migrate secret ---

def test_save_profiles_raises_when_secret_migrate_fails_with_plaintext(monkeypatch):
    m = ProfileManager()
    p = m.create_profile("h3-profile")
    p.proxy.host = "h3.proxy"
    p.proxy.port = 8080
    p.proxy.username = "u"
    p.proxy.password = "plaintext-secret"

    def broken_migrate(profiles):
        raise OSError("disk full")

    monkeypatch.setattr(storage_module, "migrate_plaintext_passwords", broken_migrate)
    with pytest.raises(RuntimeError, match="mật khẩu"):
        m.save_profiles([p])
    # File trên đĩa không bị ghi đè với password=null.
    on_disk = json.load(open(storage_module.PROFILES_JSON, encoding="utf-8"))
    entry = next(item for item in on_disk if item["id"] == p.id)
    assert entry.get("proxy", {}).get("password") in (None, "********")


# --- M1: runtime store 2 instance không xóa record của nhau ---

def test_runtime_store_two_instances_do_not_clobber():
    path = os.path.join(TEST_DATA_DIR, "runtime-shared.json")
    a = BrowserRuntimeStore(path)
    b = BrowserRuntimeStore(path)

    a.upsert("profile-a", pid=111, port=1)
    b.upsert("profile-b", pid=222, port=2)
    # Instance a xóa record của mình; record b ghi SAU lần load đầu của a
    # phải còn nguyên trên đĩa.
    a.remove("profile-a")

    fresh = BrowserRuntimeStore(path)
    assert fresh.get("profile-b") is not None
    assert fresh.get("profile-b").get("pid") == 222
    assert fresh.get("profile-a") is None


def test_runtime_store_long_lived_instance_sees_other_process_writes():
    """Server giữ store từ lúc khởi động phải thấy record CLI vừa ghi."""
    path = os.path.join(TEST_DATA_DIR, "runtime-cross-process.json")
    server = BrowserRuntimeStore(path)
    cli = BrowserRuntimeStore(path)

    cli.upsert("cli-profile", pid=333, port=3, user_data_dir="C:/tmp/cli-profile")
    record = server.get("cli-profile")
    assert record is not None
    assert record["pid"] == 333
    assert "cli-profile" in server.all()

    cli.remove("cli-profile")
    assert server.get("cli-profile") is None
    assert "cli-profile" not in server.all()


def test_identity_unknown_is_not_cached(monkeypatch):
    """Timeout CIM chỉ là lỗi tạm; cache nó làm stop chờ hết TTL vô ích."""
    if sys.platform != "win32":
        pytest.skip("Windows process identity")

    class FakeKernel32:
        def OpenProcess(self, *_args):
            return 12345

        def QueryFullProcessImageNameW(self, _handle, _flags, buf, _size):
            buf.value = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            return 1

        def CloseHandle(self, _handle):
            return 1

    class FakeWindll:
        kernel32 = FakeKernel32()

    user_data_dir = os.path.join(TEST_DATA_DIR, "identity-profile", "user_data")
    record = {
        "Name": "chrome.exe",
        "CommandLine": (
            f'"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            f'--user-data-dir="{user_data_dir}" --remote-debugging-port=9333'
        ),
        "CreationDate": "20261025000000",
    }

    process_identity._identity_cache.clear()
    monkeypatch.setattr(process_identity.ctypes, "windll", FakeWindll())

    def timeout_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=5)

    monkeypatch.setattr(process_identity.subprocess, "run", timeout_run)
    first = process_identity.read_browser_identity(424242, user_data_dir)
    assert first["state"] == "unknown"
    assert 424242 not in process_identity._identity_cache

    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(record), stderr=""
        ),
    )
    second = process_identity.read_browser_identity(424242, user_data_dir)
    assert second["state"] == "owned"
    assert second["port"] == 9333


def test_stop_retries_transient_unknown_identity(monkeypatch):
    m = ProfileManager()
    launcher = BrowserLauncher(m)
    p = m.create_profile("stop-retry-identity")
    user_data_dir = os.path.join(storage_module.DATA_DIR, "profiles", p.id, "user_data")
    launcher._runtime_store.upsert(
        p.id, pid=424242, port=9333, user_data_dir=user_data_dir
    )

    calls = []

    def fake_identity(pid, udd):
        calls.append((pid, udd))
        if len(calls) == 1:
            return {"state": "unknown", "port": None, "created": None}
        return {"state": "absent", "port": None, "created": None}

    monkeypatch.setattr(launcher_module, "read_browser_identity", fake_identity)
    try:
        result = launcher.stop(p.id)
        assert result["success"] is True
        assert result["message"] == "Phiên cũ đã kết thúc"
        assert len(calls) >= 2
        assert launcher._runtime_store.get(p.id) is None
    finally:
        m.delete_profile(p.id)
        launcher.unregister_cleanup()


# --- stealth_disable per-profile + native brands ---

def test_stealth_disable_per_profile_disables_only_listed_groups(monkeypatch):
    monkeypatch.delenv("ANTIDETECT_STEALTH_DISABLE", raising=False)
    fp = generate_fingerprint("windows")
    fp.stealth_disable = ["canvas", "webgl", "audio"]
    js = get_stealth_script(fp)
    worker_js = get_worker_stealth_script(fp)

    assert '"canvas"' in js and '"webgl"' in js and '"audio"' in js
    # Noise canvas bị tắt: chuỗi gate vẫn ở trong payload nhưng DISABLED có
    # đủ 3 nhóm; các hook khác vẫn cài.
    assert '"navigator"' not in js  # navigator không nằm trong disable list
    assert "installScreenWorkArea" in js
    # Worker payload cũng nhận cùng disabled set.
    assert '"canvas"' in worker_js


def test_stealth_disable_rejects_unknown_groups():
    # Validator chạy qua constructor/model_validate (đường API/JSON production);
    # gán attribute trực tiếp không validate theo mặc định của pydantic.
    fp = FingerprintConfig(stealth_disable=["canvas", "not-a-group"])
    assert fp.stealth_disable == ["canvas"]


def test_native_brands_override_payload_brands():
    fp = generate_fingerprint("windows")
    native = {
        "brands": [
            {"brand": "Google Chrome", "version": "155"},
            {"brand": "Chromium", "version": "155"},
            {"brand": "Not(A:Brand", "version": "24"},
        ],
        "fullVersionList": [
            {"brand": "Google Chrome", "version": "155.0.8059.85"},
            {"brand": "Chromium", "version": "155.0.8059.85"},
            {"brand": "Not(A:Brand", "version": "24.0.0.0"},
        ],
    }
    js = get_stealth_script(fp, native_brands=native)
    assert 'Not(A:Brand' in js
    assert '155.0.8059.85' in js
    # Bảng hardcode cũ không còn xuất hiện khi brands đo được thay thế.
    assert '"Not_A Brand", "version": "24"' not in js

    worker_js = get_worker_stealth_script(fp, native_brands=native)
    assert 'Not(A:Brand' in worker_js


def test_launcher_measures_native_brands_before_overrides():
    src = open(os.path.join(BASE_DIR, "antidetect", "launcher.py"), encoding="utf-8").read()
    # Đo bằng Chrome phụ riêng (setUserAgentOverride đè cả JS userAgentData,
    # nên phiên đã override không đo được) — phải chạy trước khi CDP thread
    # build stealth payload/ua_metadata.
    launch_fn = src[src.index("def launch("):]
    measure_pos = launch_fn.index("self._measure_native_brands_standalone(browser_path)")
    thread_pos = launch_fn.index("self._start_cdp_thread(")
    assert measure_pos < thread_pos
    assert "_NATIVE_BRANDS_JS" in src
    assert "self._native_brands_cache" in src
    # Payload dùng brands đo được khi có.
    assert "native_brands=native_brands" in src


def test_launch_response_has_degraded_flag():
    src = open(os.path.join(BASE_DIR, "antidetect", "launcher.py"), encoding="utf-8").read()
    assert '"degraded": not cdp_ready_final' in src


def test_ui_has_stealth_disable_controls():
    html = open(os.path.join(BASE_DIR, "web", "templates", "index.html"), encoding="utf-8").read()
    for element in (
        "edit-stealth-canvas",
        "edit-stealth-webgl",
        "edit-stealth-audio",
        "edit-stealth-media",
        "edit-stealth-worker",
    ):
        assert f'id="{element}"' in html
    assert "stealth_disable:" in html


def test_ui_badge_shows_authentic_mode():
    html = open(os.path.join(BASE_DIR, "web", "templates", "index.html"), encoding="utf-8").read()
    # Danh sách profile phải cho thấy chế độ stealth để không nhầm lẫn giữa
    # authentic (không noise) và unique (noise).
    assert "authenticMode" in html
    assert "Authentic</span>" in html
    assert "Partial" in html


def test_cli_delete_stops_browser_first():
    src = open(os.path.join(BASE_DIR, "cli.py"), encoding="utf-8").read()
    block = src[src.index('args.command == "delete"'):]
    stop_pos = block.index("launcher.stop(args.id)")
    delete_pos = block.index("manager.delete_profile(args.id)")
    assert stop_pos < delete_pos, "CLI phải dừng browser trước khi xóa profile"


def test_ensure_profiles_file_uses_unique_tmp():
    src = open(os.path.join(BASE_DIR, "antidetect", "storage.py"), encoding="utf-8").read()
    # Tmp name cố định khiến 2 process cùng init giành chung file tạm.
    assert "PROFILES_JSON}.tmp.{uuid.uuid4().hex}" in src
    assert '"{PROFILES_JSON}.tmp"' not in src.replace("f{", "{")


def test_launch_all_runs_launches_in_parallel_batches(monkeypatch):
    """start-all: launch chạy song song đúng limit, đủ kết quả, đúng thứ tự.

    Phiên bản cũ tuần tự từng launch và chờ "slot trống" theo số browser đang
    sống — kẹt vô hạn khi số profile > limit vì browser mở xong vẫn giữ mở.
    """
    m = ProfileManager()
    launcher = BrowserLauncher(m)
    created = [m.create_profile(f"bulk-{i}") for i in range(6)]
    try:
        state = {"active": 0, "peak": 0}
        state_lock = threading.Lock()

        def fake_launch(pid, open_url=None):
            with state_lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.3)
            with state_lock:
                state["active"] -= 1
            return {"success": True, "degraded": False, "profile_id": pid}

        monkeypatch.setattr(launcher, "launch", fake_launch)
        monkeypatch.setattr(launcher, "is_running", lambda pid: False)

        t0 = time.time()
        results = launcher.launch_all(delay=0.0, max_concurrent=3)
        elapsed = time.time() - t0

        # Data dir test dùng chung có thể còn profile của test trước —
        # chỉ đòi hỏi mọi profile mới tạo đều được launch và song song đúng
        # limit; các profile khác (nếu có) cũng success qua fake launch.
        ids = [r["profile_id"] for r in results]
        assert {p.id for p in created} <= set(ids)
        by_created = {r["profile_id"]: r for r in results if r["profile_id"] in {p.id for p in created}}
        assert all(r["result"]["success"] for r in by_created.values())
        # Không vượt quá limit launch đồng thời.
        assert state["peak"] <= 3
        # 6 launch x 0.3s với 3 worker: ~0.6-0.9s. Tuần tự cũ ≥ 1.8s.
        assert elapsed < 1.6, f"quá chậm: {elapsed:.2f}s"
    finally:
        for p in created:
            m.delete_profile(p.id)
    launcher.unregister_cleanup()


def test_launch_all_filters_selected_profile_ids(monkeypatch):
    """P0: multi-select chỉ launch đúng profile được chọn, không chạy cả farm."""
    m = ProfileManager()
    launcher = BrowserLauncher(m)
    created = [m.create_profile(f"bulk-select-{i}") for i in range(3)]
    try:
        launched = []

        def fake_launch(pid, open_url=None):
            launched.append(pid)
            return {"success": True, "profile_id": pid}

        monkeypatch.setattr(launcher, "launch", fake_launch)
        monkeypatch.setattr(launcher, "is_running", lambda pid: False)

        selected = [created[0].id, created[2].id]
        results = launcher.launch_all(
            delay=0.0, max_concurrent=2, profile_ids=selected
        )

        assert set(launched) == set(selected)
        assert {r["profile_id"] for r in results} == set(selected)
        assert created[1].id not in launched
    finally:
        for p in created:
            m.delete_profile(p.id)
    launcher.unregister_cleanup()


def test_launch_all_skips_running_profiles(monkeypatch):
    m = ProfileManager()
    launcher = BrowserLauncher(m)
    created = [m.create_profile(f"bulk-run-{i}") for i in range(3)]
    try:
        launched = []

        def fake_launch(pid, open_url=None):
            launched.append(pid)
            return {"success": True, "profile_id": pid}

        monkeypatch.setattr(launcher, "launch", fake_launch)
        monkeypatch.setattr(
            launcher, "is_running", lambda pid: pid == created[1].id
        )

        results = launcher.launch_all(delay=0.0, max_concurrent=2)
        by_id = {r["profile_id"]: r["result"] for r in results}
        assert by_id[created[1].id]["message"] == "Đang chạy"
        assert created[1].id not in launched
        assert {created[0].id, created[2].id} <= set(launched)
    finally:
        for p in created:
            m.delete_profile(p.id)
    launcher.unregister_cleanup()


def test_facebook_page_classification_rules():
    """Quy tắc farm fanpage: ≤7 ngày = đang đăng; tuổi ≥30 ngày = sẵn sàng."""
    import datetime
    from antidetect.launcher import classify_facebook_page, _parse_fb_created_date, _fb_unit_to_days

    today = datetime.date(2026, 10, 24)
    # Trạng thái đăng bài theo ngưỡng 7 ngày.
    assert classify_facebook_page({"last_post_days": 0}, today)["status"] == "active"
    assert classify_facebook_page({"last_post_days": 7}, today)["status"] == "active"
    assert classify_facebook_page({"last_post_days": 8, "detail_synced": True}, today)["status"] == "stopped"
    assert classify_facebook_page({"detail_synced": True}, today)["status"] == "unknown"
    assert classify_facebook_page({}, today)["status"] is None  # chưa quét chi tiết
    # Sẵn sàng khi ngâm đủ 30 ngày.
    assert classify_facebook_page({"created_date": "2026-09-24"}, today)["ready"] is True
    assert classify_facebook_page({"created_date": "2026-09-25"}, today)["ready"] is False
    assert classify_facebook_page({"last_post_days": 1}, today)["ready"] is False

    # Parse ngày tạo từ text Facebook (vi + en + số).
    assert _parse_fb_created_date("Ngày tạo trang: 15/03/2020") == datetime.date(2020, 3, 15)
    assert _parse_fb_created_date("Page created on March 5, 2019") == datetime.date(2019, 3, 5)
    assert _parse_fb_created_date("ngày 5 tháng 3, 2020") == datetime.date(2020, 3, 5)
    assert _parse_fb_created_date("2021-07-02") == datetime.date(2021, 7, 2)
    assert _parse_fb_created_date("13/25/2020") is None  # không hợp lệ
    # Đơn vị thời gian → ngày.
    assert _fb_unit_to_days(3, "d") == 3
    assert _fb_unit_to_days(2, "w") == 14
    assert _fb_unit_to_days(5, "h") == 0
    assert _fb_unit_to_days(2, "mo") == 60


def test_facebook_pages_deep_endpoint(monkeypatch):
    """Endpoint deep=1: fetch_facebook_pages(deep=True) và lưu metrics vào cache."""
    import web.app as app_module
    from fastapi.testclient import TestClient
    from antidetect.storage import ProfileManager

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    m = ProfileManager()
    r = client.post("/api/profiles", json={"name": "FB Deep Test", "os": "windows"})
    pid = r.json()["id"]
    try:
        monkeypatch.setattr(app_module.launcher, "is_running", lambda x: True, raising=False)
        fake_pages = [{
            "name": "Page A", "url": "https://www.facebook.com/pagea", "id": "111",
            "created_date": "2025-01-01", "last_post_days": 2, "last_post_raw": "2 ngày",
            "created_raw": "Ngày tạo trang: 01/01/2025", "detail_synced": True,
        }]
        captured = {}

        def fake_fetch(profile_id, timeout=40.0, deep=False):
            captured["deep"] = deep
            return {"logged_in": True, "pages": fake_pages}

        monkeypatch.setattr(app_module.launcher, "fetch_facebook_pages", fake_fetch, raising=False)

        r = client.get(f"/api/profiles/{pid}/facebook/pages?refresh=1&deep=1")
        assert r.status_code == 200
        body = r.json()
        assert captured["deep"] is True
        page = body["pages"][0]
        assert page["status"] == "active"
        assert page["ready"] is True
        assert page["age_days"] > 300

        # Cache (không refresh) tự enrich trạng thái theo quy tắc hiện tại.
        monkeypatch.setattr(app_module.launcher, "is_running", lambda x: False, raising=False)
        r = client.get(f"/api/profiles/{pid}/facebook/pages")
        page = r.json()["pages"][0]
        assert page["status"] == "active" and page["ready"] is True
    finally:
        client.delete(f"/api/profiles/{pid}")


def test_facebook_deep_ui_present():
    html = open(os.path.join(BASE_DIR, "web", "templates", "index.html"), encoding="utf-8").read()
    assert "refreshFanpages(true)" in html
    assert "deep=1" in html
    assert "Đang đăng" in html and "Dừng" in html and "Sẵn sàng" in html
    assert "ageText" in html


def test_stop_profile_with_deleted_metadata_cleans_stale_record():
    """Review-4 regression: record runtime stale của profile đã xóa từng
    chặn stop_all/shutdown vĩnh viễn ("Profile không tồn tại" → shutdown
    từ chối). stop phải xử lý record stale + dọn nó."""
    m = ProfileManager()
    launcher = BrowserLauncher(m)
    p = m.create_profile("ghost-stop-test")
    pid = p.id
    try:
        # Mô phỏng browser mồ côi: record runtime tồn tại (pid không sống),
        # metadata bị xóa sau đó.
        launcher._runtime_store.upsert(pid, pid=999999999, port=1, user_data_dir="")
        m.delete_profile(pid)
        assert m.get_profile(pid) is None
        assert launcher._runtime_store.get(pid) is not None

        res = launcher.stop(pid)
        assert res.get("success") is True, res
        # Record stale phải được dọn sạch sau stop.
        assert launcher._runtime_store.get(pid) is None
        # Lần stop tiếp theo: không còn gì → báo lỗi thân thiện.
        res2 = launcher.stop(pid)
        assert res2.get("success") is False and "không tồn tại" in res2.get("error", "")
    finally:
        launcher._runtime_store.remove(pid)
        try:
            m.delete_profile(pid)
        except Exception:
            pass
    launcher.unregister_cleanup()


def test_verify_live_connection_checks_are_structural():
    """Review-4 regression: connection native dao động theo thời gian/mạng —
    verify_live phải kiểm structural (bucket spec), không so exact với giá trị
    đo lúc T0."""
    src = open(os.path.join(BASE_DIR, "tools", "verify_live.py"), encoding="utf-8").read()
    assert "connection_rtt_bucket" in src
    assert "connection_downlink_sane" in src
    # Không còn phép so exact cho rtt/downlink.
    assert 'conn.get("rtt") == conn_exp["rtt"]' not in src
    assert 'conn.get("downlink") == conn_exp["downlink"]' not in src


def test_phase1_domrect_and_media_devices_hooks(monkeypatch):
    """GĐ1: DOMRect/MediaDevices per-profile — vector nối profile còn sót."""
    from antidetect.stealth import _domrect_offsets, _media_devices

    monkeypatch.delenv("ANTIDETECT_STEALTH_DISABLE", raising=False)
    fp = generate_fingerprint("windows")
    js = get_stealth_script(fp)
    # DOMRect hook: Element/Range + SVG + tag giữ nguyên.
    for token in ("getBoundingClientRect", "getClientRects", "getBBox",
                  "DOMRectList", "createSVGRect"):
        assert token in js
    # MediaDevices: trả bộ per-profile (kể cả khi chưa có permission).
    assert "permitted" in js and "CFG.mediaDevices" in js
    # Worker không mang hai hook này.
    worker_js = get_worker_stealth_script(fp)
    assert "getBBox" not in worker_js and "permitted" not in worker_js

    # Deterministic theo seed; khác seed → offsets khác.
    fp2 = generate_fingerprint("windows")
    fp2.canvas_noise_seed = fp.canvas_noise_seed
    assert _domrect_offsets(fp2) == _domrect_offsets(fp)
    assert _media_devices(fp2) == _media_devices(fp)
    fp3 = generate_fingerprint("windows")
    if fp3.canvas_noise_seed != fp.canvas_noise_seed:
        assert _domrect_offsets(fp3) != _domrect_offsets(fp)

    # Offsets trong khoảng ±0.5px, không all-zero.
    d = _domrect_offsets(fp)
    assert all(-0.5 <= v <= 0.5 for v in d.values())
    assert any(abs(v) >= 0.05 for v in d.values())

    # MediaDevices: đúng shape, audio share group, id 24-hex.
    md = _media_devices(fp)
    kinds = [x["kind"] for x in md]
    assert kinds.count("audioinput") in (1, 2) and kinds.count("videoinput") in (1, 2)
    assert len({x["groupId"] for x in md if x["kind"].startswith("audio")}) == 1
    assert all(len(x["deviceId"]) == 24 for x in md)

    # Authentic (tắt 'canvas') → guard DISABLED chặn DOMRect hook; tắt
    # 'media' → media hook cũng bị guard.
    fp_auth = generate_fingerprint("windows")
    fp_auth.stealth_disable = ["canvas", "webgl", "audio", "media", "worker"]
    js_auth = get_stealth_script(fp_auth)
    domrect_block = js_auth[js_auth.index("5b. DOMRect"):js_auth.index("// 6. MediaDevices")]
    assert "DISABLED.has('canvas')" in domrect_block
    media_block = js_auth[js_auth.index("// 6. MediaDevices"):js_auth.index("// 7. WebGPU")]
    assert "DISABLED.has('media')" in media_block


def test_phase1_shader_precision_cross_os_only(monkeypatch):
    """GĐ1: shaderPrecision bảng chuẩn chỉ cho cross-OS; same-OS pass-through."""
    from antidetect.stealth import _shader_precision, _host_os

    fp = generate_fingerprint("windows")
    if _host_os() == "windows":
        assert _shader_precision(fp) is None
        assert '"shaderPrecision": null' in get_stealth_script(fp)

    fp_mac = generate_fingerprint("windows")
    fp_mac.os = "macos"
    sp = _shader_precision(fp_mac)
    assert sp == {"float": [127, 127, 23], "int": [31, 31, 0]}
    js = get_stealth_script(fp_mac)
    assert "rangeMin" in js  # shadow own-property trên object native, không fake object


def test_phase1_sync_location_from_proxy_endpoint(monkeypatch):
    """GĐ1.4: sync timezone/geo/locale theo IP proxy qua GeoIP."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles", json={"name": "Sync Proxy Test", "os": "windows"})
    pid = r.json()["id"]
    try:
        # Không proxy → 400.
        r = client.post(f"/api/profiles/{pid}/sync-location-proxy")
        assert r.status_code == 400

        # Gắn proxy + mock GeoIP trả Nhật Bản.
        r = client.put(f"/api/profiles/{pid}", json={
            "proxy": {"enabled": True, "protocol": "http",
                      "host": "1.2.3.4", "port": 8080}})
        assert r.status_code == 200

        def fake_check(cfg, timeout=10):
            return {"success": True, "ip": "5.6.7.8", "country": "Japan",
                    "country_code": "JP", "city": "Tokyo",
                    "timezone": "Asia/Tokyo", "lat": 35.68, "lon": 139.69}

        monkeypatch.setattr(app_module, "check_proxy", fake_check, raising=False)
        r = client.post(f"/api/profiles/{pid}/sync-location-proxy")
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert set(body["changes"]) == {"timezone", "latitude", "longitude", "locale"}
        assert body["changes"]["timezone"] == ["Asia/Ho_Chi_Minh", "Asia/Tokyo"]
        assert body["changes"]["locale"][1] == "ja-JP"

        # Profile đã cập nhật thật.
        r = client.get(f"/api/profiles/{pid}")
        fp = r.json()["fingerprint"]
        assert fp["timezone"] == "Asia/Tokyo" and fp["locale"] == "ja-JP"
        assert abs(fp["latitude"] - 35.68) < 0.001

        # GeoIP fail → 502, fingerprint giữ nguyên.
        def broken_check(cfg, timeout=10):
            return {"success": False, "error": "timeout"}
        monkeypatch.setattr(app_module, "check_proxy", broken_check, raising=False)
        r = client.post(f"/api/profiles/{pid}/sync-location-proxy")
        assert r.status_code == 502

        # Đã khớp sẵn → success True, changes rỗng.
        monkeypatch.setattr(app_module, "check_proxy", fake_check, raising=False)
        r = client.post(f"/api/profiles/{pid}/sync-location-proxy")
        body = r.json()
        assert body["success"] is True and body["changes"] == {}
    finally:
        client.delete(f"/api/profiles/{pid}")


def test_phase1_ui_sync_button():
    html = open(os.path.join(BASE_DIR, "web", "templates", "index.html"), encoding="utf-8").read()
    assert "syncLocationFromProxy" in html
    assert "Đồng bộ vị trí theo IP proxy" in html
    assert "sync-location-proxy" in html


def test_browser_resolution_order(monkeypatch, tmp_path):
    """GĐ2: per-profile > env > browsers/ (mới nhất) > Chrome đã cài."""
    import antidetect.launcher as L

    m = ProfileManager()
    launcher = BrowserLauncher(m)
    p = m.create_profile("browser-res-test")
    try:
        browsers_dir = tmp_path / "browsers"
        # Hai bản pin; phiên bản cao hơn phải được chọn khi không chỉ định.
        for v in ("150.0.1.2", "155.0.2.3"):
            exe_dir = browsers_dir / f"chrome-{v}" / "chrome-win64"
            exe_dir.mkdir(parents=True)
            (exe_dir / "chrome.exe").write_text("fake")
        monkeypatch.setattr(L, "BROWSERS_DIR", str(browsers_dir))
        monkeypatch.setattr(launcher, "browser_path", r"C:\installed\chrome.exe")

        # 1. Không per-profile, không env → pin mới nhất.
        monkeypatch.delenv("ANTIDETECT_BROWSER_PATH", raising=False)
        p.browser_path = None
        assert launcher._resolve_browser_path(p).endswith(
            os.path.join("chrome-155.0.2.3", "chrome-win64", "chrome.exe")
        )

        # 2. Per-profile tên pin → bản đó.
        p.browser_path = "chrome-150.0.1.2"
        assert launcher._resolve_browser_path(p).endswith(
            os.path.join("chrome-150.0.1.2", "chrome-win64", "chrome.exe")
        )

        # 3. Per-profile đường dẫn tuyệt đối tồn tại → dùng nguyên.
        real_exe = browsers_dir / "chrome-150.0.1.2" / "chrome-win64" / "chrome.exe"
        p.browser_path = str(real_exe)
        assert launcher._resolve_browser_path(p) == str(real_exe)

        # 4. STRICT: per-profile pin không tồn tại → None (launch báo lỗi rõ),
        # KHÔNG âm thầm fallback sang browser khác.
        p.browser_path = "chrome-khong-ton-tai"
        assert launcher._resolve_browser_path(p) is None

        # 5. Env chọn browser khi per-profile không đặt.
        p.browser_path = None
        monkeypatch.setenv(
            "ANTIDETECT_BROWSER_PATH", str(browsers_dir / "chrome-150.0.1.2")
        )
        resolved = launcher._resolve_browser_path(p)
        assert resolved is not None and "chrome-150.0.1.2" in resolved

        # 6. Env STRICT: không resolve được → None.
        monkeypatch.setenv("ANTIDETECT_BROWSER_PATH", str(browsers_dir / "none"))
        assert launcher._resolve_browser_path(p) is None

        # 7. Không per-profile, không env, pin không khớp → fallback installed.
        monkeypatch.delenv("ANTIDETECT_BROWSER_PATH", raising=False)
        monkeypatch.setattr(L, "BROWSERS_DIR", str(tmp_path / "browsers-empty"))
        assert launcher._resolve_browser_path(p) == r"C:\installed\chrome.exe"
    finally:
        m.delete_profile(p.id)
    launcher.unregister_cleanup()


def test_profile_browser_path_persists_and_api():
    """browser_path lưu vào profiles.json + PUT API nhận field."""
    m = ProfileManager()
    p = m.create_profile("browser-persist-test")
    try:
        p.browser_path = "chrome-155.0.2.3"
        m.update_profile(p)
        fresh = m.get_profile(p.id)
        assert fresh.browser_path == "chrome-155.0.2.3"

        import web.app as app_module
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
        client.post(
            "/auth/login",
            headers={"Authorization": "Bearer " + app_module.app.state.access_key},
        )
        r = client.put(f"/api/profiles/{p.id}", json={"browser_path": ""})
        assert r.status_code == 200
        assert r.json()["browser_path"] is None  # "" = reset về mặc định
    finally:
        m.delete_profile(p.id)


def test_verify_live_drift_helpers(tmp_path, monkeypatch):
    """GĐ2: baseline save/load/drift — version đổi là tín hiệu máy thay đổi."""
    import tools.verify_live as V

    monkeypatch.setattr(V, "DATA_DIR", str(tmp_path))
    actual = {
        "ua": "Mozilla/5.0 ... Chrome/155.0.0.0 Safari/537.36",
        "platform": "Win32", "glVendor": "Google Inc. (Intel)",
        "glRenderer": "ANGLE (Intel)", "hc": 8, "dm": 16,
        "sw": 1920, "sh": 1080, "dpr": 1, "tz": "Asia/Saigon",
    }
    saved = V._save_baseline("drift-test-profile", actual)
    assert saved["browser_version"] == "155.0.0.0"

    loaded = V._load_baseline("drift-test-profile")
    assert loaded["glRenderer"] == "ANGLE (Intel)"

    # Không đổi → không drift.
    assert V._baseline_drift(loaded, actual) == {}

    # Chrome update → drift ua + browser_version.
    new_actual = dict(actual)
    new_actual["ua"] = "Mozilla/5.0 ... Chrome/156.0.0.0 Safari/537.36"
    drift = V._baseline_drift(loaded, new_actual)
    assert set(drift) == {"ua", "browser_version"}
    assert drift["browser_version"] == ["155.0.0.0", "156.0.0.0"]

    # GPU driver đổi → drift glRenderer.
    new_actual = dict(actual)
    new_actual["glRenderer"] = "ANGLE (NVIDIA)"
    assert "glRenderer" in V._baseline_drift(loaded, new_actual)


def test_detector_matrix_new_signals():
    """GĐ2: Iphey verdict + Fingerprint demo bot parse từ text."""
    from tools.detector_audit import extract_detector_signals

    iphey = extract_detector_signals("Trust Score ... Trustworthy", "https://iphey.com/")
    assert iphey["iphey_verdict"] == "Trustworthy"
    bad = extract_detector_signals("Suspicious activity", "https://iphey.com/")
    assert bad["iphey_verdict"] == "Suspicious"
    worst = extract_detector_signals("UNTRUSTED device", "https://iphey.com/")
    assert worst["iphey_verdict"] == "Untrusted"

    clean = extract_detector_signals("You are Not a Bot", "https://fingerprint.com/demo/")
    assert clean["fpdemo_bot"] == "not detected"
    bot = extract_detector_signals("Bot Detected — please verify", "https://fingerprint.com/demo/")
    assert bot["fpdemo_bot"] == "detected"

    # Site khác không bị nhầm signal.
    assert "iphey_verdict" not in extract_detector_signals("trustworthy", "https://creepjs/")
    assert "fpdemo_bot" not in extract_detector_signals("not a bot", "https://abrahamjuliot.github.io/creepjs/")


def test_install_browser_list_and_targets_in_defaults():
    """install_browser --list chạy không crash + 2 target mới trong default."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "tools", "install_browser.py"), "--list"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0

    from tools.detector_audit import DEFAULT_TARGETS
    names = {n for n, _ in DEFAULT_TARGETS}
    assert "iphey" in names and "fingerprint-demo" in names


def test_browser_version_detection_cft_and_installed_layout(tmp_path, monkeypatch):
    """P0: browser pin CfT giấu version trong tên thư mục chrome-<x.y.z.w>."""
    from antidetect import fingerprint as fpm

    pinned = (
        tmp_path / "browsers" / "chrome-155.0.8059.84"
        / "chrome-win64" / "chrome.exe"
    )
    pinned.parent.mkdir(parents=True)
    pinned.write_text("", encoding="utf-8")
    assert fpm.get_version_from_browser_path(str(pinned)) == (
        "155.0.0.0", "155.0.8059.84")

    # Bản zip Linux đặt chrome.exe ngay trong thư mục version.
    direct = tmp_path / "chrome-154.0.7712.90" / "chrome.exe"
    direct.parent.mkdir()
    direct.write_text("", encoding="utf-8")
    assert fpm.get_version_from_browser_path(str(direct)) == (
        "154.0.0.0", "154.0.7712.90")

    # Layout Chrome cài máy (Application/chrome.exe + sibling version) giữ nguyên.
    app_dir = tmp_path / "Google" / "Chrome" / "Application"
    (app_dir / "154.0.7712.90").mkdir(parents=True)
    (app_dir / "155.0.8059.85").mkdir()
    installed_exe = app_dir / "chrome.exe"
    installed_exe.write_text("", encoding="utf-8")
    assert fpm.get_version_from_browser_path(str(installed_exe)) == (
        "155.0.0.0", "155.0.8059.85")

    # Launch theo pinned phải dùng version/UA của pin, không phải Chrome host.
    monkeypatch.setattr(
        fpm, "get_installed_chrome_version", lambda: ("156.0.0.0", "156.0.1.1")
    )
    fp = fpm.generate_fingerprint("windows")
    fixed = fpm.normalize_fingerprint_for_launch(fp, str(pinned))
    assert fixed.browser_version == "155.0.8059.84"
    assert "Chrome/155.0.0.0" in fixed.user_agent


def test_phase3_audio_noise_no_periodic_pattern(monkeypatch):
    """GĐ3: audio noise per-index hash — bỏ pattern tuần hoàn 100/13."""
    monkeypatch.delenv("ANTIDETECT_STEALTH_DISABLE", raising=False)
    fp = generate_fingerprint("windows")
    js = get_stealth_script(fp)
    audio_block = js[js.index("11. AudioContext"):js.index("if (w.AnalyserNode)", js.index("11. AudioContext"))]
    # Hash per-index thay cho LFSR tuần tự.
    assert "audioNoiseAt" in js and "Math.imul" in audio_block
    # Không còn bước nhảy tuần hoàn trong buffer lẫn analyser.
    assert "i += 100" not in audio_block
    analyser_block = js[js.index("if (w.AnalyserNode)"):js.index("12. Web Worker") if "12. Web Worker" in js else len(js)]
    assert "i += 13" not in analyser_block
    # Noise MỖI sample của buffer, không phải mỗi 100th.
    assert "for (let i = 0; i < data.length; i++)" in audio_block
    # 2 seed → 2 payload audio khác nhau (audioSeed trong config).
    fp2 = generate_fingerprint("windows")
    fp2.audio_noise_seed = fp.audio_noise_seed + 1
    js2 = get_stealth_script(fp2)
    assert js != js2


def test_audio_seed_is_nonzero_int32_and_legacy_migrates():
    """P0: seed float bị JS >>> 0 cắt thành 0 → mọi profile cùng nhiễu audio."""
    from antidetect.models import FingerprintConfig

    legacy = FingerprintConfig(audio_noise_seed=0.0002127)
    assert isinstance(legacy.audio_noise_seed, int)
    assert legacy.audio_noise_seed > 0
    # Ổn định qua vòng load/save profile.
    assert (
        FingerprintConfig(**legacy.model_dump()).audio_noise_seed
        == legacy.audio_noise_seed
    )

    # Hai seed legacy sát nhau từng truncate về cùng 0.
    a = FingerprintConfig(audio_noise_seed=0.00005).audio_noise_seed
    b = FingerprintConfig(audio_noise_seed=0.0003).audio_noise_seed
    assert a != b

    assert FingerprintConfig(audio_noise_seed=0).audio_noise_seed == 12345
    assert FingerprintConfig(audio_noise_seed=-5).audio_noise_seed == 12345

    fp = generate_fingerprint("windows")
    assert isinstance(fp.audio_noise_seed, int)
    assert 1 <= fp.audio_noise_seed <= 2_147_483_647

    js = get_stealth_script(fp)
    assert "Number(CFG.audioSeed) >>> 0" in js
    assert "CFG.audioSeed || 12345" not in js


def test_phase3_tls_fingerprint_parser():
    """GĐ3: JA3/JA4 parse từ tls.peet.ws JSON."""
    from tools.leak_audit import fetch_tls_fingerprint

    class FakePage:
        def __init__(self, body):
            self._body = body
        def goto(self, *a, **k):
            return None
        def close(self):
            return None
        def locator(self, *_a, **_k):
            page = self
            class L:
                def inner_text(self, *_, **__):
                    return page._body
            return L()

    class FakeContext:
        def new_page(self):
            return FakePage(json.dumps({
                "user_agent": "Mozilla/5.0 ... Chrome/155",
                "tls": {"ja3_hash": "abc123", "ja4": "t13d1516h2_8daaf6152771_b186095e22b6",
                        "peetprint_hash": "pp1", "akamai_fingerprint_hash": "ak1"},
                "http2": {"akamai_fingerprint_hash": "h2a"},
            }))

    result = fetch_tls_fingerprint(FakeContext())
    assert result["ja3_hash"] == "abc123"
    assert result["ja4"].startswith("t13d")
    assert result["peetprint_hash"] == "pp1"
    assert result["h2_settings_hash"] == "h2a"

    class ErrContext:
        def new_page(self):
            raise RuntimeError("net down")
    err = fetch_tls_fingerprint(ErrContext())
    assert "error" in err


def test_phase4_human_input_samplers():
    """GĐ4: hành vi người — delay bounds, bezier endpoint, scroll tổng đúng."""
    import math
    import statistics
    from antidetect.human import HumanInput

    hi = HumanInput(port=0, seed=42)  # port 0: chỉ sampler, không connect

    # Keystroke flight time: lognormal quanh 90ms, space chậm hơn, bounded.
    delays = [hi.key_delay_ms("a") for _ in range(1500)]
    assert all(35 <= d <= 2000 for d in delays)
    med = statistics.median(delays)
    assert 60 <= med <= 140, med
    space_med = statistics.median([hi.key_delay_ms(" ") for _ in range(400)])
    assert space_med > med

    # Mouse path: endpoint chính xác, độ cong có thật, bước hợp lý.
    path = hi.mouse_path(100, 100, 700, 400)
    assert path[-1] == (700.0, 400.0)
    assert 8 <= len(path) <= 60

    def dist_to_line(px, py):
        x0, y0, x1, y1 = 100, 100, 700, 400
        t = max(0, min(1, ((px - x0) * (x1 - x0) + (py - y0) * (y1 - y0))
                       / ((x1 - x0) ** 2 + (y1 - y0) ** 2)))
        return math.hypot(px - (x0 + t * (x1 - x0)), py - (y0 + t * (y1 - y0)))

    assert max(dist_to_line(px, py) for px, py in path) > 5
    assert hi.mouse_path(10, 10, 12, 11) == [(12.0, 11.0)]

    # Scroll: tổng bước luôn đúng dy (dương và âm).
    for dy in (300, -500, 1200, 40):
        steps = hi.scroll_steps(dy)
        assert sum(steps) == dy
        assert 2 <= len(steps) <= 24

    # Seed → tái hiện (automation test replay được). Dùng cặp instance mới
    # cùng seed vì các sampler test trước đã tiêu thụ RNG của `hi`.
    hi_c = HumanInput(port=0, seed=99)
    hi_d = HumanInput(port=0, seed=99)
    assert hi_c.mouse_path(100, 100, 700, 400) == hi_d.mouse_path(100, 100, 700, 400)
    assert hi_c.scroll_steps(777) == hi_d.scroll_steps(777)

    # Không JS nào inject vào trang — toàn bộ qua Input domain (không bề mặt
    # detect mới). Static: module không chứa evaluate/addScript.
    src = open(os.path.join(BASE_DIR, "antidetect", "human.py"), encoding="utf-8").read()
    assert "Input.dispatchKeyEvent" in src and "Input.dispatchMouseEvent" in src
    assert "addScriptToEvaluateOnNewDocument" not in src
    assert "Runtime.evaluate" not in src


def test_dashboard_theme_and_management_ui():
    """Dashboard: dark/light theme + filter chips + logs viewer + fanpage badge."""
    html = open(os.path.join(BASE_DIR, "web", "templates", "index.html"), encoding="utf-8").read()
    # Theme: init trước render (không flash) + toggle + persist
    assert "localStorage.getItem('antidetect-theme')" in html
    assert "data-theme" in html and "toggleTheme" in html
    assert 'html[data-theme="light"]' in html  # override CSS
    assert "theme-icon-sun" in html and "theme-icon-moon" in html
    # Filter chips
    for chip in ("all", "running", "stopped", "proxy", "authentic"):
        assert f'data-filter-chip="{chip}"' in html
    assert "function setFilter" in html
    # Stats fanpage + badge trong row
    assert 'id="stat-pages"' in html and "stat-pages" in html
    assert "fbBadge" in html
    # Logs viewer
    assert "openLogsModal" in html and "/api/system/logs" in html
    # Fanpage href phải qua safeHref (chặn javascript:/data:) chứ không chỉ
    # escapeHtml — import JSON độc hại không thể tạo stored XSS.
    assert "function safeHref" in html
    assert "escapeHtml(safeHref(pg.url))" in html


def test_system_logs_endpoint(monkeypatch, tmp_path):
    """/api/system/logs: tail app.log read-only, giới hạn lines."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    log = tmp_path / "app.log"
    log.write_text("\n".join(f"line-{i}" for i in range(500)), encoding="utf-8")

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.get("/api/system/logs?lines=50")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 50
    assert body["lines"][-1] == "line-499"
    assert body["lines"][0] == "line-450"

    # File không tồn tại → trống, không crash.
    (tmp_path / "app.log").unlink()
    r = client.get("/api/system/logs")
    assert r.status_code == 200 and r.json()["count"] == 0

    # Không auth → 401.
    client2 = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    r = client2.get("/api/system/logs")
    assert r.status_code == 401


def test_batch_a_export_import_roundtrip():
    """Đợt A: export redact password + import tạo lại profiles đúng."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    # Tạo profile có proxy + password
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

    # Export đúng → password rỗng
    r = client.get("/api/profiles/export", headers={"x-confirm-export": "yes"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    exported = [p for p in data["profiles"] if p["name"] == "Export A"][0]
    assert exported["proxy"]["password"] is None
    assert exported["proxy"]["host"] == "1.2.3.4"
    assert exported["tags"] == ["farm-a"]
    assert exported["notes"] == "test note"

    # Import lại (id mới, password null)
    r = client.post("/api/profiles/import", json={"profiles": [exported]})
    assert r.status_code == 200
    body = r.json()
    assert body["imported_count"] == 1 and body["failed_count"] == 0
    new_id = body["imported"][0]["id"]
    assert new_id != pid

    fresh = client.get(f"/api/profiles/{new_id}").json()
    assert fresh["proxy"]["host"] == "1.2.3.4"
    assert fresh["fingerprint"]["timezone"] == exported["fingerprint"]["timezone"]

    # Cleanup
    client.delete(f"/api/profiles/{pid}")
    client.delete(f"/api/profiles/{new_id}")


def test_batch_a_uptime_ram_fields():
    """ProfileRuntime: field uptime/ram có trong API response (None khi dừng)."""
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
        assert entry["uptime_seconds"] is None  # đang dừng
    finally:
        client.delete(f"/api/profiles/{pid}")


def test_batch_b_bulk_create(monkeypatch):
    """Đợt B: bulk-create — fingerprint riêng, proxy theo thứ tự, authentic mode."""
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
    # 4 profile, 2 có proxy, 2 direct + warning
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
    hosts = {p["proxy"]["host"] for p in proxies_on}
    assert hosts == {"10.0.0.1", "10.0.0.2"}
    for p in created:
        client.delete(f"/api/profiles/{p['id']}")


def test_batch_b_authentic_bulk_gets_gpu_host(monkeypatch):
    """bulk-create authentic same-OS: GPU phải là GPU host (bảng dự phòng)."""
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    r = client.post("/api/profiles/bulk-create", json={
        "count": 1, "os": "windows", "stealth_mode": "authentic",
        "name_prefix": "AuthBulk",
    })
    assert r.status_code == 200
    pid = r.json()["created"][0]["id"]
    try:
        p = client.get(f"/api/profiles/{pid}").json()
        assert set(p["fingerprint"]["stealth_disable"]) == {
            "canvas", "webgl", "audio", "media", "worker"}
        assert p["fingerprint"]["webgl_vendor"].startswith("Google Inc.")
    finally:
        client.delete(f"/api/profiles/{pid}")


def test_batch_b_start_all_with_profile_ids(monkeypatch):
    """start-all body profile_ids: chỉ start đám được chọn."""
    import web.app as app_module
    from fastapi.testclient import TestClient
    import threading

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    p1 = client.post("/api/profiles", json={"name": "Sel A"}).json()
    p2 = client.post("/api/profiles", json={"name": "Sel B"}).json()
    p3 = client.post("/api/profiles", json={"name": "Sel C"}).json()
    called = threading.Event()
    captured = {}

    def fake_launch_all(**kwargs):
        captured.update(kwargs)
        called.set()
        return []

    from unittest.mock import patch
    try:
        # Job cũ (nếu có) phải kết thúc trước khi test tạo job mới, nếu không
        # endpoint trả 409 và test âm thầm bỏ qua đúng phần cần kiểm tra.
        deadline = time.time() + 5
        while time.time() < deadline:
            with app_module._start_all_lock:
                job = app_module._active_start_all_job
                if job is None or job.get("status") != "running":
                    break
            time.sleep(0.05)
        with patch.object(app_module.launcher, "launch_all", side_effect=fake_launch_all):
            r = client.post("/api/profiles/start-all", json={
                "profile_ids": [p1["id"], p2["id"]]})
            assert r.status_code == 200, r.text
            assert r.json()["starting_count"] == 2
            assert called.wait(3)
            assert set(captured.get("profile_ids") or []) == {p1["id"], p2["id"]}
    finally:
        for p in (p1, p2, p3):
            client.delete(f"/api/profiles/{p['id']}")


def test_scheduler_start_all_callback_receives_profile_ids(monkeypatch):
    """P0: Scheduler.run gọi cb(ids) — callback phải map vào profile_ids."""
    import tempfile
    from datetime import datetime
    import web.app as app_module
    from antidetect.scheduler import ScheduleStore, Scheduler

    path = os.path.join(tempfile.mkdtemp(prefix="sched-startall-"), "schedules.json")
    store = ScheduleStore(path)
    selected = ["profile-a", "profile-b"]
    store.add(
        "Mở đúng profile",
        datetime.now().strftime("%H:%M"),
        "start_all",
        profile_ids=selected,
    )

    captured = {}

    def fake_launch_all(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(app_module.launcher, "launch_all", fake_launch_all)

    sch = Scheduler(
        store,
        callbacks={"start_all": app_module._scheduler_start_all},
        check_interval=0.05,
    )
    sch.start()
    try:
        deadline = time.time() + 3
        while not captured and time.time() < deadline:
            time.sleep(0.02)
    finally:
        sch.stop()
        sch.join(timeout=2)
    assert captured.get("profile_ids") == selected


def test_batch_c_scheduler_store_and_due():
    """ScheduleStore CRUD + Scheduler due/catchup."""
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

    sch = Scheduler(store, callbacks={}, check_interval=0.2)
    # Ghim 12:00 để rule "5 phút trước" không vắt qua nửa đêm.
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    # Rule mới trong cửa sổ 60 phút → "due"
    r = {"id": "x", "enabled": True,
         "time": (now - timedelta(minutes=5)).strftime("%H:%M"),
         "last_run_date": None}
    assert sch._due(r, now) == "due"
    # Đã chạy hôm nay → không
    r["last_run_date"] = now.strftime("%Y-%m-%d")
    assert sch._due(r, now) is None
    # Chạy hôm qua, lỡ hôm nay nhưng còn trong cửa sổ catch-up → "catchup"
    r["last_run_date"] = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    assert sch._due(r, now) == "catchup"
    # Lỡ quá 60 phút → bỏ, chờ đúng giờ ngày mai
    r["time"] = "10:00"
    assert sch._due(r, now) is None
    # Rule tắt thì không chạy dù đúng giờ
    r["enabled"] = False
    r["time"] = "11:00"
    assert sch._due(r, now) is None


class _FakeCDPWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _human_with_fake_ws(seed=7):
    from antidetect.human import HumanInput

    hi = HumanInput(port=0, seed=seed)
    ws = _FakeCDPWebSocket()

    async def fake_connect():
        return ws

    hi._connect = fake_connect
    hi.mouse_path = lambda x0, y0, x1, y1: [(float(x1), float(y1))]
    hi.key_delay_ms = lambda ch="": 0.0
    return hi, ws


def test_phase4_human_click_release_uses_zero_buttons():
    hi, ws = _human_with_fake_ws()
    hi.click(30, 40, double=True)
    events = [
        m["params"] for m in ws.sent
        if m["method"] == "Input.dispatchMouseEvent"
    ]
    presses = [e for e in events if e["type"] == "mousePressed"]
    releases = [e for e in events if e["type"] == "mouseReleased"]
    assert [e["buttons"] for e in presses] == [1, 1]
    assert [e["buttons"] for e in releases] == [0, 0]
    assert [e["clickCount"] for e in presses] == [1, 2]
    assert [e["clickCount"] for e in releases] == [1, 2]


def test_phase4_human_key_events_include_code_and_vk():
    hi, ws = _human_with_fake_ws()
    hi.type_text("A1 ế", initial_delay=False)
    events = [
        m["params"] for m in ws.sent
        if m["method"] == "Input.dispatchKeyEvent"
    ]
    downs = [e for e in events if e["type"] == "keyDown"]
    ups = [e for e in events if e["type"] == "keyUp"]
    assert len(downs) == len(ups) == 4

    a = downs[0]
    assert (
        a["key"], a["code"], a["windowsVirtualKeyCode"], a["modifiers"]
    ) == ("A", "KeyA", 65, 8)
    one = downs[1]
    assert (one["code"], one["windowsVirtualKeyCode"]) == ("Digit1", 49)
    space = downs[2]
    assert (
        space["key"], space["code"], space["windowsVirtualKeyCode"]
    ) == (" ", "Space", 32)
    vn = downs[3]
    assert (
        vn["key"], vn["text"], vn["code"], vn["windowsVirtualKeyCode"]
    ) == ("ế", "ế", "", 0)

    for down, up in zip(downs, ups):
        assert down["key"] == up["key"]
        assert down["code"] == up["code"]
        assert up["windowsVirtualKeyCode"] == down["windowsVirtualKeyCode"]
        assert up["nativeVirtualKeyCode"] == down["nativeVirtualKeyCode"]


def test_phase4_launcher_empty_selection_is_noop():
    launcher = BrowserLauncher(manager)
    calls = []
    original_stop = launcher.stop
    launcher.stop = lambda pid: calls.append(pid) or {"success": True}
    launcher.active_processes["fake-profile"] = object()
    try:
        result = launcher.stop_all([])
        assert calls == []
        assert result["success"] is True

        launcher.stop_all(None)
        assert "fake-profile" in calls
    finally:
        launcher.stop = original_stop
        launcher.active_processes.pop("fake-profile", None)

    launched = []
    launcher.launch = lambda pid, open_url=None: launched.append(pid) or {
        "success": True
    }
    assert launcher.launch_all(profile_ids=[]) == []
    assert launched == []


def test_phase4_save_profiles_retries_transient_replace_error(monkeypatch):
    """Windows sharing violation (antivirus/backup) — retry thay vì 500 ngay."""
    profile = manager.create_profile(name="Replace Retry Test")
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(13, "sharing violation")
        return real_replace(src, dst)

    try:
        monkeypatch.setattr(storage_module.os, "replace", flaky_replace)
        manager.save_profiles(manager.list_profiles())
        assert attempts["n"] == 3
        assert manager.get_profile(profile.id) is not None
    finally:
        manager.delete_profile(profile.id)


def test_fanpage_deep_sync_marks_failed_scan_as_not_synced(monkeypatch):
    """Entry rỗng từ deep scan vẫn có key times/created_raw — không được coi
    là đã quét, nếu không UI báo 'Không rõ' thay vì 'Chưa quét'."""
    launcher = BrowserLauncher(manager)
    url = "https://www.facebook.com/deep-test"

    async def fake_pages(port, timeout=0):
        return {"logged_in": True, "pages": [{"name": "Deep", "url": url}]}

    async def fake_deep_empty(port, pages, timeout=0):
        return {url: {"times": [], "created_raw": None, "last_post_raw": None}}

    monkeypatch.setattr(launcher, "is_running", lambda pid: True)
    launcher.active_ports["deep-profile"] = 1234
    monkeypatch.setattr(launcher, "_facebook_pages_cdp", fake_pages)
    monkeypatch.setattr(launcher, "_facebook_pages_deep_cdp", fake_deep_empty)

    data = launcher.fetch_facebook_pages("deep-profile", timeout=1, deep=True)
    assert data["pages"][0]["detail_synced"] is False

    async def fake_deep_ok(port, pages, timeout=0):
        return {url: {
            "times": [{"n": 2, "u": "d"}],
            "created_raw": "Page created on January 1, 2024",
            "last_post_raw": None,
        }}

    monkeypatch.setattr(launcher, "_facebook_pages_deep_cdp", fake_deep_ok)
    data2 = launcher.fetch_facebook_pages("deep-profile", timeout=1, deep=True)
    assert data2["pages"][0]["detail_synced"] is True
