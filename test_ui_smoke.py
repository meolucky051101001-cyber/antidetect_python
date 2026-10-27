"""Playwright UI smoke cho dashboard thật (P1-9).

Chạy thủ công vì test mở Chrome thật để kiểm tra start/stop:

    $env:ANTIDETECT_RUN_UI_SMOKE = "1"
    python -m pytest test_ui_smoke.py -q

Playwright không nằm trong requirements-dev (tùy chọn):

    pip install playwright
    playwright install chromium   # hoặc để test dùng Chrome hệ thống

Server được khởi động với ``ANTIDETECT_DATA_DIR`` là thư mục TẠM và
``ANTIDETECT_NO_BROWSER=1``; test không bao giờ chạm ``data/`` thật của repo.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
PlaywrightError = playwright_api.Error
sync_playwright = playwright_api.sync_playwright

if os.name != "nt":
    pytest.skip("Antidetect chỉ chạy trên Windows (DPAPI).", allow_module_level=True)

if os.environ.get("ANTIDETECT_RUN_UI_SMOKE") != "1":
    pytest.skip(
        "Đặt ANTIDETECT_RUN_UI_SMOKE=1 để chạy Playwright UI smoke (mở Chrome thật).",
        allow_module_level=True,
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REAL_DATA_DIR = os.path.realpath(os.path.join(BASE_DIR, "data"))
READ_KEY_CODE = (
    "import json;"
    "from antidetect.runtime_auth import read_runtime;"
    "print(json.dumps(read_runtime() or {}))"
)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(method, base, path, token=None, timeout=10):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return -1, {}


def _read_access_key(data_dir):
    env = dict(os.environ)
    env["ANTIDETECT_DATA_DIR"] = data_dir
    env["PYTHONPATH"] = BASE_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.check_output(
        [sys.executable, "-c", READ_KEY_CODE],
        cwd=BASE_DIR,
        env=env,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return (json.loads(line) or {}).get("access_key")
    return None


def _tail(path, lines=80):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "\n".join(fh.read().splitlines()[-lines:])
    except OSError:
        return ""


def _stop_process(proc):
    try:
        proc.wait(timeout=25)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def live_server():
    data_dir = tempfile.mkdtemp(prefix="antidetect-ui-smoke-")
    assert os.path.realpath(data_dir) != REAL_DATA_DIR, "UI smoke không được dùng data/ thật"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = os.path.join(tempfile.gettempdir(), f"antidetect-ui-smoke-{port}.log")

    env = dict(os.environ)
    env.update(
        {
            "ANTIDETECT_DATA_DIR": data_dir,
            "ANTIDETECT_NO_BROWSER": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "run.py"), f"--port={port}"],
        cwd=BASE_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    token = None
    try:
        deadline = time.time() + 45
        healthy = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            status, body = _http("GET", base, "/health", timeout=3)
            if status == 200 and body.get("application") == "antidetect-python":
                healthy = True
                break
            time.sleep(0.4)
        if not healthy:
            pytest.fail(f"Server không khởi động được.\n--- log ---\n{_tail(log_path)}")

        deadline = time.time() + 30
        while time.time() < deadline and not token:
            try:
                token = _read_access_key(data_dir)
            except Exception:
                token = None
            if not token:
                time.sleep(0.3)
        if not token:
            pytest.fail(f"Không đọc được access key từ runtime DPAPI.\n--- log ---\n{_tail(log_path)}")

        yield SimpleNamespace(
            base=base, token=token, data_dir=data_dir, proc=proc, log_path=log_path
        )
    finally:
        _http("POST", base, "/api/system/shutdown", token=token, timeout=120)
        _stop_process(proc)
        log_file.close()
        shutil.rmtree(data_dir, ignore_errors=True)


def _launch_browser(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except PlaywrightError:
        pass
    try:
        return playwright.chromium.launch(channel="chrome", headless=True)
    except PlaywrightError as exc:
        pytest.skip(f"Không có Chromium/Chrome cho Playwright: {exc}")


def _profile_row(page, profile_id):
    return page.locator(f"tr[data-profile-id='{profile_id}']")


def _profile_status(page, profile_id):
    return page.evaluate(
        """async (pid) => {
            const res = await fetch('/api/profiles');
            if (!res.ok) return null;
            const profiles = await res.json();
            const found = profiles.find(p => p.id === pid);
            return found ? found.status : null;
        }""",
        profile_id,
    )


def test_dashboard_ui_smoke(live_server):
    created_name = "UI Smoke"
    edited_name = "UI Smoke Edited"
    imported_name = "UI Smoke Imported"
    page_errors = []

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(25000)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            # --- Đăng nhập bằng unlock hash (đúng luồng shortcut của run.py) ---
            page.goto(f"{live_server.base}/unlock#{live_server.token}", wait_until="domcontentloaded")
            page.wait_for_url(f"{live_server.base}/", timeout=25000)
            page.wait_for_function(
                """() => {
                    const empty = document.getElementById('empty-state');
                    return !!empty && !empty.classList.contains('hidden');
                }""",
                timeout=25000,
            )

            # --- Tạo profile qua modal ---
            with page.expect_response(
                lambda r: "/api/fingerprint/random" in r.url and r.request.method == "GET"
            ):
                page.click("button[onclick='openCreateModal()']")
            page.wait_for_selector("#createModal:not(.hidden)")
            page.wait_for_function(
                "() => document.getElementById('create-adv-canvas').value !== ''"
            )
            page.fill("#p-name", created_name)
            with page.expect_response(
                lambda r: r.url.rstrip("/").endswith("/api/profiles")
                and r.request.method == "POST"
            ) as create_response:
                page.click("#createModal button[onclick='submitCreateProfile()']")
            assert create_response.value.status == 200, "Tạo profile qua UI thất bại"

            page.locator("tr[data-profile-id]").filter(has_text=created_name).wait_for()
            row = page.locator("tr[data-profile-id]").filter(has_text=created_name).first
            profile_id = row.get_attribute("data-profile-id")
            assert profile_id, "Không lấy được id profile từ bảng"

            # --- Sửa profile (Edit + expected_revision) ---
            row.locator("button[onclick^='openEditModal']").click()
            page.wait_for_selector("#editModal:not(.hidden)")
            page.fill("#edit-name", edited_name)
            with page.expect_response(
                lambda r: r.request.method == "PUT" and r.url.endswith(f"/api/profiles/{profile_id}")
            ) as update_response:
                page.click("#editModal button[onclick='submitSaveProfileEdit()']")
            assert update_response.value.status == 200, "Lưu profile qua UI thất bại"
            _profile_row(page, profile_id).filter(has_text=edited_name).wait_for()

            # --- Export qua nút dashboard (tải file thật) ---
            export_path = os.path.join(live_server.data_dir, "ui-export.json")
            with page.expect_download(timeout=30000) as download_info:
                page.click("#btn-export")
            download = download_info.value
            assert download.suggested_filename.endswith(".json"), download.suggested_filename
            download.save_as(export_path)
            with open(export_path, encoding="utf-8") as fh:
                exported = json.load(fh)
            assert exported.get("version") == 1
            exported_names = [p.get("name") for p in exported.get("profiles", [])]
            assert edited_name in exported_names, exported_names
            for item in exported.get("profiles", []):
                assert not (item.get("proxy") or {}).get("password"), "Export làm lộ mật khẩu proxy"

            # --- Import qua modal (JSON lấy từ file export) ---
            entry = json.loads(json.dumps(exported["profiles"][0]))
            entry["name"] = imported_name
            page.click("button[onclick='openImportModal()']")
            page.wait_for_selector("#importModal:not(.hidden)")
            page.fill("#import-text", json.dumps({"version": 1, "profiles": [entry]}))
            with page.expect_response(
                lambda r: r.url.rstrip("/").endswith("/api/profiles/import")
                and r.request.method == "POST"
            ) as import_response:
                page.click("#importModal button[onclick=\"doImport('text')\"]")
            assert import_response.value.status == 200, "Import qua UI thất bại"
            import_payload = import_response.value.json() or {}
            assert import_payload.get("imported_count") == 1, import_payload
            page.locator("#import-result").filter(has_text="Đã import 1").wait_for()
            imported_row = (
                page.locator("tr[data-profile-id]").filter(has_text=imported_name).first
            )
            imported_row.wait_for()
            imported_id = imported_row.get_attribute("data-profile-id")
            assert imported_id, "Không lấy được id profile import"
            page.click("#importModal button[onclick='closeImportModal()']")

            # --- Lịch tự động: tạo + tắt qua checkbox ---
            page.click("button[onclick='openSchedulesModal()']")
            page.wait_for_selector("#schedulesModal:not(.hidden)")
            schedule_time = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
            page.fill("#sched-time", schedule_time)
            page.fill("#sched-name", "UI Smoke Schedule")
            with page.expect_response(
                lambda r: r.url.rstrip("/").endswith("/api/schedules")
                and r.request.method == "POST"
            ) as schedule_response:
                page.click("#schedulesModal button[onclick='createSchedule()']")
            assert schedule_response.value.status == 200, "Tạo lịch qua UI thất bại"
            schedule_row = (
                page.locator("#schedules-list > div").filter(has_text="UI Smoke Schedule").first
            )
            schedule_row.wait_for()
            with page.expect_response(
                lambda r: "/api/schedules/" in r.url and r.request.method == "PUT"
            ) as toggle_response:
                schedule_row.locator("input[type='checkbox']").uncheck()
            assert toggle_response.value.status == 200, "Tắt lịch qua UI thất bại"
            rules = page.evaluate(
                "async () => (await (await fetch('/api/schedules')).json()).rules"
            )
            ours = [rule for rule in rules if rule.get("name") == "UI Smoke Schedule"]
            assert ours and ours[0]["enabled"] is False, ours
            page.click("#schedulesModal button[onclick='closeSchedulesModal()']")

            # --- Fingerprint Health Center: audit tĩnh + sửa an toàn qua UI ---
            with page.expect_response(
                lambda r: r.url.rstrip("/").endswith("/api/profiles/audit")
                and r.request.method == "GET"
            ) as audit_response:
                page.click("button[onclick='openAuditModal()']")
            assert audit_response.value.status == 200, "Audit qua UI thất bại"
            audit_payload = audit_response.value.json() or {}
            assert audit_payload.get("summary", {}).get("total") == 2, audit_payload
            page.wait_for_selector("#audit-content:not(.hidden)")
            page.locator("#audit-list").filter(has_text=edited_name).wait_for()
            page.locator("#audit-list").filter(has_text=imported_name).wait_for()

            fix_button = page.locator(
                f"#audit-list button[onclick=\"fixAuditProfile('{imported_id}')\"]"
            )
            if not fix_button.count():
                fix_button = page.locator(
                    f"#audit-list button[onclick=\"fixAuditProfile('{profile_id}')\"]"
                )
            assert fix_button.count(), "Không có nút sửa an toàn dù có profile trùng dấu vân tay"
            page.once("dialog", lambda dialog: dialog.accept())
            with page.expect_response(
                lambda r: r.url.rstrip("/").endswith("/api/profiles/audit/fix")
                and r.request.method == "POST"
            ) as fix_response:
                fix_button.first.click()
            assert fix_response.value.status == 200, "Sửa an toàn qua UI thất bại"
            fix_payload = fix_response.value.json() or {}
            assert fix_payload.get("dry_run") is False, fix_payload
            assert fix_payload.get("changed"), fix_payload
            assert fix_payload.get("report", {}).get("summary", {}).get("total") == 2, fix_payload

            fresh_audit = page.evaluate(
                "async () => await (await fetch('/api/profiles/audit')).json()"
            )
            for item in fresh_audit.get("profiles", []):
                codes = {issue.get("code") for issue in item.get("issues", [])}
                assert "duplicate_canvas_seed" not in codes, item
                assert "duplicate_audio_seed" not in codes, item
            page.click("#auditModal button[onclick='closeAuditModal()']")

            # --- Start/stop browser thật qua nút trong bảng ---
            row = _profile_row(page, profile_id)
            with page.expect_response(
                lambda r: r.request.method == "POST"
                and r.url.endswith(f"/api/profiles/{profile_id}/start"),
                timeout=180000,
            ) as start_response:
                row.locator("button[onclick^='startProfile']").click()
            assert start_response.value.status == 200, "Start qua UI thất bại"
            start_payload = start_response.value.json() or {}
            assert start_payload.get("success") is True, start_payload
            assert start_payload.get("port"), start_payload
            assert start_payload.get("degraded") is False, start_payload

            page.wait_for_function(
                """async (pid) => {
                    const res = await fetch('/api/profiles');
                    if (!res.ok) return false;
                    const profiles = await res.json();
                    const found = profiles.find(p => p.id === pid);
                    return !!found && found.status === 'running';
                }""",
                arg=profile_id,
                timeout=60000,
            )
            _profile_row(page, profile_id).locator(
                "button[onclick^='stopProfile']"
            ).wait_for(timeout=60000)

            with page.expect_response(
                lambda r: r.request.method == "POST"
                and r.url.endswith(f"/api/profiles/{profile_id}/stop"),
                timeout=90000,
            ) as stop_response:
                _profile_row(page, profile_id).locator(
                    "button[onclick^='stopProfile']"
                ).click()
            assert stop_response.value.status == 200, "Stop qua UI thất bại"
            assert (stop_response.value.json() or {}).get("success") is True
            page.wait_for_function(
                """async (pid) => {
                    const res = await fetch('/api/profiles');
                    if (!res.ok) return false;
                    const profiles = await res.json();
                    const found = profiles.find(p => p.id === pid);
                    return !!found && found.status !== 'running';
                }""",
                arg=profile_id,
                timeout=60000,
            )
            _profile_row(page, profile_id).locator(
                "button[onclick^='startProfile']"
            ).wait_for(timeout=30000)

            assert _profile_status(page, profile_id) == "stopped"
            assert not page_errors, f"Dashboard ném lỗi JavaScript: {page_errors[:5]}"
        except Exception:
            try:
                shot = os.path.join(tempfile.gettempdir(), "antidetect-ui-smoke-failure.png")
                page.screenshot(path=shot, full_page=True)
                print(f"\n[ui-smoke] screenshot: {shot}")
            except Exception:
                pass
            print(f"\n[ui-smoke] server log tail:\n{_tail(live_server.log_path)}")
            raise
        finally:
            context.close()
            browser.close()
