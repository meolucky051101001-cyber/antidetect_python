"""Real dashboard error paths, opt-in through ANTIDETECT_RUN_UI_SMOKE=1.

Uses the existing temporary-data live server and one headless browser. No
responses, scheduler history, or renderer functions are mocked. The fanpage
schedule is deliberately blocked by the real static launch gate before Chrome
or Facebook can be opened.
"""
import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path

import pytest

if os.name != "nt" or os.environ.get("ANTIDETECT_RUN_UI_SMOKE") != "1":
    pytest.skip(
        "Windows only: set ANTIDETECT_RUN_UI_SMOKE=1 to run real dashboard error tests.",
        allow_module_level=True,
    )

from test_ui_smoke import (
    _launch_browser,
    _profile_row,
    _tail,
    live_server,  # noqa: F401 - pytest fixture reused by this module
    sync_playwright,
)


@pytest.fixture(scope="module")
def release_browser(live_server):
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def dashboard(release_browser, live_server, record_property):
    context = release_browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.set_default_timeout(25000)
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    record_property("server_pid", live_server.proc.pid)
    record_property("temporary_data_dir", live_server.data_dir)
    record_property("server_log", live_server.log_path)
    try:
        page.goto(f"{live_server.base}/unlock#{live_server.token}", wait_until="domcontentloaded")
        page.wait_for_url(f"{live_server.base}/", timeout=25000)
        page.wait_for_function("() => typeof currentProfiles !== 'undefined' && Array.isArray(currentProfiles)")
        yield page
        assert not errors, f"Unhandled dashboard JavaScript errors: {errors}"
    finally:
        context.close()


def _api(page, server, method, path, data=None):
    response = page.request.fetch(server.base + path, method=method, data=data)
    assert response.ok, f"{method} {path}: HTTP {response.status}: {response.text()}"
    return response.json()


def _metadata(server):
    path = Path(server.data_dir) / "profiles.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _create_profile(page, server, name):
    with page.expect_response(
        lambda r: "/api/fingerprint/random" in r.url and r.request.method == "GET"
    ):
        page.click("button[onclick='openCreateModal()']")
    page.wait_for_selector("#createModal:not(.hidden)")
    page.wait_for_function("() => document.getElementById('create-adv-canvas').value !== ''")
    page.fill("#p-name", name)
    with page.expect_response(
        lambda r: r.url.endswith("/api/profiles") and r.request.method == "POST"
    ) as result:
        page.click("#createModal button[onclick='submitCreateProfile()']")
    assert result.value.status == 200, result.value.text()
    row = page.locator("tr[data-profile-id]").filter(has_text=name)
    row.wait_for()
    profile_id = row.get_attribute("data-profile-id")
    return _api(page, server, "GET", f"/api/profiles/{profile_id}")


def test_malformed_batch_import_has_no_partial_profiles(dashboard, live_server, record_property):
    page = dashboard
    before = _api(page, live_server, "GET", "/api/profiles")
    before_ids = {profile["id"] for profile in before}
    profile_dir = Path(live_server.data_dir) / "profiles"
    before_dirs = {item.name for item in profile_dir.iterdir()}
    entries = [
        {"name": "UI valid batch entry", "notes": "This complete entry must survive."},
        {"name": "UI invalid browser path", "browser_path": {"path": "not a string"}},
        {"name": "UI invalid sync time", "facebook_pages_synced_at": "not a timestamp"},
        ["not an object"],
    ]
    page.click("button[onclick='openImportModal()']")
    page.wait_for_selector("#importModal:not(.hidden)")
    page.fill("#import-text", json.dumps({"version": 1, "profiles": entries}))
    with page.expect_response(
        lambda r: r.url.endswith("/api/profiles/import") and r.request.method == "POST"
    ) as response:
        page.click("#importModal button[onclick=\"doImport('text')\"]")
    assert response.value.status == 200
    result = response.value.json()
    assert result["success"] is False
    assert (result["imported_count"], result["failed_count"]) == (1, 3), result
    assert [item["index"] for item in result["failed"]] == [1, 2, 3]
    assert all(item["code"] == "INVALID_PROFILE_IMPORT" for item in result["failed"])
    page.locator("#import-result").filter(has_text="Đã import 1 profile, 3 lỗi").wait_for()
    for name in ("UI invalid browser path", "UI invalid sync time"):
        page.locator("#import-result").filter(has_text=name).wait_for()
    page.locator("#toast-message").filter(has_text="Import: 1 thành công, 3 lỗi.").wait_for()

    valid_id = result["imported"][0]["id"]
    expected_ids = before_ids | {valid_id}
    fresh = _api(page, live_server, "GET", "/api/profiles")
    assert {profile["id"] for profile in fresh} == expected_ids
    persisted = _metadata(live_server)
    assert {profile["id"] for profile in persisted} == expected_ids
    assert next(p for p in persisted if p["id"] == valid_id)["notes"] == entries[0]["notes"]
    assert {item.name for item in profile_dir.iterdir()} == before_dirs
    page.click("#importModal button[onclick='closeImportModal()']")
    _profile_row(page, valid_id).filter(has_text=entries[0]["name"]).wait_for()
    record_property("observed", "HTTP 200 batch: 1 committed, 3 rejected; UI counts and errors; no orphan directories")


def test_stale_edit_shows_conflict_and_preserves_newer_data(dashboard, live_server, record_property):
    page = dashboard
    original = _create_profile(page, live_server, "UI revision original")
    profile_id = original["id"]
    _profile_row(page, profile_id).locator("button[onclick^='openEditModal']").click()
    page.wait_for_selector("#editModal:not(.hidden)")
    page.fill("#edit-name", "UI stale edit must not win")
    page.fill("#edit-notes", "Stale notes must not overwrite.")

    latest = _api(page, live_server, "PUT", f"/api/profiles/{profile_id}", {
        "name": "UI revision newer client",
        "notes": "Concurrent update must remain intact.",
        "tags": ["newer-client"],
        "expected_revision": original["revision"],
    })
    assert latest["revision"] > original["revision"]
    with page.expect_response(
        lambda r: r.url.endswith(f"/api/profiles/{profile_id}") and r.request.method == "PUT"
    ) as response:
        page.click("#editModal button[onclick='submitSaveProfileEdit()']")
    assert response.value.status == 409, response.value.text()
    stale_payload = response.value.request.post_data_json
    assert stale_payload["expected_revision"] == original["revision"]
    page.locator("#toast-message").filter(has_text="Profile đã bị sửa ở tab/phiên khác.").wait_for()
    page.wait_for_selector("#editModal", state="hidden")
    _profile_row(page, profile_id).filter(has_text=latest["name"]).wait_for()
    fresh = _api(page, live_server, "GET", f"/api/profiles/{profile_id}")
    persisted = next(p for p in _metadata(live_server) if p["id"] == profile_id)
    for key in ("name", "notes", "tags", "revision", "fingerprint"):
        assert fresh[key] == latest[key], key
        assert persisted[key] == latest[key], key
    _profile_row(page, profile_id).locator("button[onclick^='openEditModal']").click()
    page.wait_for_selector("#editModal:not(.hidden)")
    assert page.input_value("#edit-name") == latest["name"]
    assert page.input_value("#edit-notes") == latest["notes"]
    record_property("observed", "Actual concurrent API write; stale UI PUT 409; warning visible; newer metadata and reopened edit preserved")


def test_fingerprint_revision_and_verification_controls_preserve_then_migrate(
    dashboard, live_server, record_property,
):
    page = dashboard
    created = _create_profile(page, live_server, "UI explicit fingerprint migration")
    assert created["fingerprint"]["fingerprint_algorithm_version"] == 2
    assert created["fingerprint"]["verification_mode"] == "strict"
    profile_id = created["id"]
    fingerprint = dict(created["fingerprint"], fingerprint_algorithm_version=1,
                       verification_mode="compatibility", canvas_noise_seed=0,
                       audio_noise_seed=7654321)
    legacy = _api(page, live_server, "PUT", f"/api/profiles/{profile_id}", {
        "fingerprint": fingerprint, "expected_revision": created["revision"],
    })
    page.click("button[onclick='refreshProfiles()']")
    page.wait_for_function("""([id, revision]) => currentProfiles.some(
        p => p.id === id && p.revision === revision)""", arg=[profile_id, legacy["revision"]])

    def edit_and_save(algorithm, mode, expected_revision, *, migrate=False):
        _profile_row(page, profile_id).locator("button[onclick^='openEditModal']").click()
        page.wait_for_selector("#editModal:not(.hidden)")
        assert page.input_value("#edit-fp-algorithm") == "1"
        assert page.input_value("#edit-fp-verification") == "compatibility"
        if migrate:
            page.select_option("#edit-fp-algorithm", algorithm)
            page.select_option("#edit-fp-verification", mode)
        else:
            page.fill("#edit-notes", "An unrelated note must preserve identity settings.")
        with page.expect_response(
            lambda r: r.url.endswith(f"/api/profiles/{profile_id}") and r.request.method == "PUT"
        ) as response:
            page.click("#editModal button[onclick='submitSaveProfileEdit()']")
        assert response.value.status == 200, response.value.text()
        payload = response.value.request.post_data_json
        assert payload["expected_revision"] == expected_revision
        assert payload["fingerprint"]["fingerprint_algorithm_version"] == int(algorithm)
        assert payload["fingerprint"]["verification_mode"] == mode
        assert payload["fingerprint"]["canvas_noise_seed"] == 0
        assert payload["fingerprint"]["audio_noise_seed"] == 7654321
        fresh = _api(page, live_server, "GET", f"/api/profiles/{profile_id}")
        page.wait_for_selector("#editModal", state="hidden")
        page.wait_for_function("""([id, revision]) => currentProfiles.some(
            p => p.id === id && p.revision === revision)""", arg=[profile_id, fresh["revision"]])
        return fresh

    preserved = edit_and_save("1", "compatibility", legacy["revision"])
    migrated = edit_and_save("2", "strict", preserved["revision"], migrate=True)
    stored = next(p for p in _metadata(live_server) if p["id"] == profile_id)
    assert stored["fingerprint"] == migrated["fingerprint"]
    assert migrated["fingerprint"]["fingerprint_algorithm_version"] == 2
    assert migrated["fingerprint"]["verification_mode"] == "strict"
    assert migrated["status"] == "stopped"
    record_property("observed", "New profile v2/strict; unrelated edit preserves legacy mode and zero canvas seed; explicit migration preserves seeds and uses revision CAS")


def _create_due_schedule(page, name, action, profile_id):
    page.fill("#sched-time", datetime.now().strftime("%H:%M"))
    page.fill("#sched-name", name)
    page.select_option("#sched-action", action)
    page.uncheck("#sched-all")
    page.check(f"#sched-profiles input[value='{profile_id}']")
    with page.expect_response(
        lambda r: r.url.endswith("/api/schedules") and r.request.method == "POST"
    ) as response:
        page.click("#sched-submit")
    assert response.value.status == 200, response.value.text()
    page.locator("#schedules-list > div").filter(has_text=name).wait_for()
    return response.value.json()["rule"]["id"]


def test_real_scheduler_failure_and_accepted_job_are_distinct(dashboard, live_server, record_property):
    page = dashboard
    settings = _api(page, live_server, "PUT", "/api/settings/launch-gate", {"mode": "block"})
    assert settings["mode"] == "block", "This test requires the real launch gate to block profile launch"
    # Reserve a non-listening localhost port: actual GeoIP connections fail
    # locally, with no possibility of reaching an external proxy or Facebook.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed_proxy:
        closed_proxy.bind(("127.0.0.1", 0))
        proxy_profile = _api(page, live_server, "POST", "/api/profiles", {
            "name": "UI scheduler refused proxy",
            "proxy": {"enabled": True, "protocol": "http", "host": "127.0.0.1",
                      "port": closed_proxy.getsockname()[1]},
        })
        blocked_profile = _api(page, live_server, "POST", "/api/profiles", {
            "name": "UI scheduler blocked fanpage",
        })
        fingerprint = dict(blocked_profile["fingerprint"])
        fingerprint.update({
            "os": "windows",
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        })
        _api(page, live_server, "PUT", f"/api/profiles/{blocked_profile['id']}", {
            "fingerprint": fingerprint, "expected_revision": blocked_profile["revision"],
            "browser_path": str(Path(live_server.data_dir) / "never-created-browser.exe"),
        })
        audit = _api(page, live_server, "GET", "/api/profiles/audit")
        audited = next(p for p in audit["profiles"] if p["profile_id"] == blocked_profile["id"])
        assert any(issue["code"] == "ua_os_mismatch" and issue["severity"] == "critical"
                   for issue in audited["issues"]), audited

        page.click("button[onclick='refreshProfiles()']")
        _profile_row(page, blocked_profile["id"]).wait_for()
        page.click("button[onclick='openSchedulesModal()']")
        page.wait_for_selector("#schedulesModal:not(.hidden)")
        names = ("UI real proxy failure", "UI real accepted fanpage")
        rule_ids = []
        try:
            rule_ids.append(_create_due_schedule(page, names[0], "sync_location_proxy", proxy_profile["id"]))
            rule_ids.append(_create_due_schedule(page, names[1], "sync_fanpages", blocked_profile["id"]))
            deadline = time.monotonic() + 75
            runs = {}
            while time.monotonic() < deadline:
                state = _api(page, live_server, "GET", "/api/schedules")
                runs = {event["rule"]: event for event in state["recent_runs"] if event.get("rule") in names}
                if all(name in runs for name in names) and runs[names[1]].get("job_status") == "failed":
                    break
                page.wait_for_timeout(500)
            assert set(runs) == set(names), f"{runs}\n{_tail(live_server.log_path)}"
            failed, accepted = runs[names[0]], runs[names[1]]
            assert failed["status"] == "failed", failed
            assert failed["failed_profile_ids"] == [proxy_profile["id"]]
            assert failed["next_retry_at"], failed
            assert accepted["status"] == "accepted" and accepted["job_id"], accepted
            assert accepted["job_status"] == "failed", accepted
            job = _api(page, live_server, "GET", f"/api/fanpage-sync/jobs/{accepted['job_id']}")["job"]
            assert job["status"] == "failed", job
            assert len(job["items"]) == 1
            assert job["items"][0]["error_code"] == "LAUNCH_GATE_BLOCKED", job
            assert job["items"][0]["detail"]["opened_by_job"] is False
            assert _api(page, live_server, "GET", f"/api/profiles/{blocked_profile['id']}")["status"] == "stopped"

            # Reopen through UI so the real GET /api/schedules drives rendering.
            page.click("#schedulesModal button[onclick='closeSchedulesModal()']")
            with page.expect_response(lambda r: r.url.endswith("/api/schedules") and r.request.method == "GET"):
                page.click("button[onclick='openSchedulesModal()']")
            failed_row = page.locator("#schedule-runs > div").filter(has_text=names[0])
            failed_row.filter(has_text="Thất bại").wait_for()
            assert "Có lịch thử lại" in failed_row.inner_text()
            accepted_row = page.locator("#schedule-runs > div").filter(has_text=names[1])
            accepted_row.filter(has_text="Đã xếp hàng").wait_for()
            assert f"Job: {accepted['job_id']} (thất bại)" in accepted_row.inner_text()
            assert "Thành công" not in accepted_row.inner_text()
            record_property("observed", "Real scheduler tick: proxy callback failed with retry; fanpage enqueue accepted, linked job failed at static gate; both UI labels verified")
        finally:
            for rule_id in rule_ids:
                _api(page, live_server, "DELETE", f"/api/schedules/{rule_id}")
