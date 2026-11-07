"""Tests cho Fanpage Manager Giai đoạn 1 & 2 (Catalog SQLite, Deduplication, F3/F4, Basic Collector, Async Sync Jobs, REST API, CSV Export)."""
import datetime
import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.fanpages.repository import (  # noqa: E402
    FanpageRepository,
    FanpageRevisionConflictError,
    parse_compact_metric,
)
from antidetect.fanpages.service import (  # noqa: E402
    FanpageService,
    sanitize_csv_cell,
)
from antidetect.storage import ProfileManager  # noqa: E402


# --- 1. Unit tests cho bộ phân tích số viết tắt & chống CSV Formula Injection ---


@pytest.mark.parametrize(
    "raw,expected_val,expected_prec",
    [
        (1250, 1250, "exact"),
        ("1.234", 1234, "exact"),
        ("1,234", 1234, "exact"),
        ("1.234.567 người theo dõi", 1234567, "exact"),
        ("0", 0, "exact"),
        ("1,2K", 1200, "rounded"),
        ("1.2K followers", 1200, "rounded"),
        ("1,25 nghìn người thích", 1250, "rounded"),
        ("2,5 Tr người theo dõi", 2500000, "rounded"),
        ("1.5M", 1500000, "rounded"),
        ("3,4 vạn", 34000, "rounded"),
        ("", None, None),
        (None, None, None),
    ],
)
def test_parse_compact_metric(raw, expected_val, expected_prec):
    val, _raw_str, prec = parse_compact_metric(raw)
    assert val == expected_val
    assert prec == expected_prec


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("Page bình thường", "Page bình thường"),
        ("=CMD|' /C calc'!A0", "'=CMD|' /C calc'!A0"),
        ("+SUM(A1:A10)", "'+SUM(A1:A10)"),
        ("-2+3", "'-2+3"),
        ("@import", "'@import"),
        ("\t=1+1", "'\t=1+1"),
        ("\r=1+1", "'\r=1+1"),
        ("  =evil()", "'  =evil()"),
        (123, "123"),
        (None, ""),
    ],
)
def test_sanitize_csv_cell_prevents_formula_injection(cell, expected):
    assert sanitize_csv_cell(cell) == expected


# --- 2. Repository & Service: Chống trùng đa profile, thăng cấp ID, Idempotency ---


def _make_isolated_pm(tmp_path, monkeypatch) -> ProfileManager:
    import antidetect.storage as storage_mod

    data_dir = str(tmp_path)
    monkeypatch.setattr(storage_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        storage_mod, "PROFILES_JSON", os.path.join(data_dir, "profiles.json")
    )
    monkeypatch.setattr(
        storage_mod, "PROFILES_DATA_DIR", os.path.join(data_dir, "profiles")
    )
    monkeypatch.setattr(
        storage_mod, "LOCK_FILE", os.path.join(data_dir, "profiles.lock")
    )
    pm = ProfileManager()
    pm.data_dir = data_dir
    return pm


def _seed_profile_pages(pm: ProfileManager, profile_id: str, pages: list, synced_at: int = 1762400000):
    p = pm.get_profile(profile_id)
    assert p is not None
    p.facebook_pages = pages
    p.facebook_pages_synced_at = synced_at
    pm.update_profile(p)


def test_deduplication_across_multiple_profiles_and_same_name_separation(tmp_path, monkeypatch):
    pm = _make_isolated_pm(tmp_path, monkeypatch)
    launcher = MagicMock()
    launcher.is_running.return_value = False
    svc = FanpageService(
        profile_manager=pm,
        launcher=launcher,
        db_path=str(tmp_path / "fanpages.sqlite"),
    )

    p1 = pm.create_profile(name="Via 01")
    p2 = pm.create_profile(name="Via 02")
    p3 = pm.create_profile(name="Via 03")

    shared_page = {
        "id": "1000888999111",
        "name": "Cộng Đồng Reup Việt",
        "url": "https://www.facebook.com/CongDongReupViet",
        "followers": "12.500",
        "likes": "10.200",
        "created_date": "2024-01-15",
        "created_raw": "15 tháng 1, 2024",
        "last_post_at_min": "2026-11-05T00:00:00+00:00",
        "last_post_at_max": "2026-11-05T12:00:00+00:00",
        "last_post_observed_at": "2026-11-06T00:00:00+00:00",
        "last_post_precision": "hour",
        "last_post_confidence": "high",
        "activity_scan_complete": True,
        "activity_checked_at": "2026-11-06T00:00:00+00:00",
    }
    # Hai page trùng tên nhưng khác ID và URL không được phép gộp nhầm!
    same_name_page_a = {
        "id": "1000111222333",
        "name": "Tin Tức 24h",
        "url": "https://www.facebook.com/profile.php?id=1000111222333",
        "followers": "1,5K",
    }
    same_name_page_b = {
        "id": "1000444555666",
        "name": "Tin Tức 24h",
        "url": "https://www.facebook.com/profile.php?id=1000444555666",
        "followers": "2.000",
    }

    _seed_profile_pages(pm, p1.id, [shared_page, same_name_page_a], synced_at=1762400100)
    _seed_profile_pages(pm, p2.id, [shared_page, same_name_page_b], synced_at=1762400200)
    _seed_profile_pages(pm, p3.id, [shared_page], synced_at=1762400300)

    # Đồng bộ lặp lại 4 lần phải hoàn toàn idempotent (không nhân bản bản ghi)
    for _ in range(4):
        svc.sync_from_profiles()

    summary = svc.get_summary()
    assert summary["total_pages"] == 3
    assert summary["multi_profile_pages"] == 1
    assert summary["verified_id_pages"] == 3
    assert summary["unverified_id_pages"] == 0
    # Tổng follower không cộng trùng shared_page 3 lần: 12500 + 1500 + 2000 = 16000
    assert summary["total_followers"] == 16000
    assert summary["followers_has_rounded"] is True
    assert summary["coverage"]["followers"] == {"available": 3, "total": 3}

    detail = svc.get_page("fb_1000888999111")
    assert detail is not None
    assert detail["profile_count"] == 3
    assert len(detail["linked_profiles"]) == 3
    assert detail["internal_age_threshold_met"] is True


def test_url_only_page_promoted_to_verified_id_preserves_tags_notes_and_links(tmp_path, monkeypatch):
    pm = _make_isolated_pm(tmp_path, monkeypatch)
    launcher = MagicMock()
    launcher.is_running.return_value = False
    svc = FanpageService(
        profile_manager=pm,
        launcher=launcher,
        db_path=str(tmp_path / "fanpages.sqlite"),
    )

    p1 = pm.create_profile(name="Via Vanity")
    # Ban đầu chỉ có URL vanity, chưa có numeric ID
    _seed_profile_pages(
        pm,
        p1.id,
        [
            {
                "id": "",
                "name": "Shop Thời Trang Nữ",
                "url": "https://www.facebook.com/ShopThoiTrangNuVanity",
            }
        ],
        synced_at=1762400100,
    )

    listing = svc.list_pages()
    assert listing["total"] == 1
    url_only_item = listing["items"][0]
    old_internal_id = url_only_item["internal_page_id"]
    assert old_internal_id.startswith("pg_url_")
    assert url_only_item["id_verification_status"] == "url_only"

    # Người dùng gắn tag và ghi chú cho page chờ xác minh ID
    updated = svc.update_page(
        old_internal_id,
        tags=["thời trang", "ưu tiên"],
        note="Page đang chạy thử nghiệm",
        preferred_profile_id=p1.id,
        expected_revision=url_only_item["revision"],
    )
    assert updated is not None
    assert updated["tags"] == ["thời trang", "ưu tiên"]
    assert updated["note"] == "Page đang chạy thử nghiệm"

    # Lượt quét sâu sau đó phát hiện numeric ID cho cùng URL vanity
    _seed_profile_pages(
        pm,
        p1.id,
        [
            {
                "id": "61559988776655",
                "name": "Shop Thời Trang Nữ",
                "url": "https://www.facebook.com/ShopThoiTrangNuVanity",
                "created_date": "2025-05-01",
            }
        ],
        synced_at=1762400900,
    )

    listing_after = svc.list_pages()
    assert listing_after["total"] == 1
    promoted = listing_after["items"][0]
    assert promoted["internal_page_id"] == "pg_61559988776655"
    assert promoted["fb_page_id"] == "61559988776655"
    assert promoted["id_verification_status"] == "verified_id"
    # Phải bảo toàn nguyên vẹn tags, note và preferred_profile_id!
    assert promoted["tags"] == ["thời trang", "ưu tiên"]
    assert promoted["note"] == "Page đang chạy thử nghiệm"
    assert promoted["preferred_profile_id"] == p1.id


def test_missing_in_scan_and_profile_deletion_preserve_page_and_history(tmp_path, monkeypatch):
    pm = _make_isolated_pm(tmp_path, monkeypatch)
    launcher = MagicMock()
    launcher.is_running.return_value = False
    svc = FanpageService(
        profile_manager=pm,
        launcher=launcher,
        db_path=str(tmp_path / "fanpages.sqlite"),
    )

    p1 = pm.create_profile(name="Profile Tam")
    _seed_profile_pages(
        pm,
        p1.id,
        [
            {
                "id": "1000777888999",
                "name": "Page Giữ Lịch Sử",
                "url": "https://www.facebook.com/profile.php?id=1000777888999",
                "seen_in_scan_id": "scan_old",
                "last_seen_at": "2026-11-05T10:00:00+00:00",
            }
        ],
        synced_at=1762400500,
    )
    # Giả lập lượt quét mới của p1 là scan_new nhưng page trên chỉ thấy ở scan_old
    pm.apply_facebook_scan(
        p1.id,
        {
            "scan_id": "scan_new",
            "discovery_status": "complete",
            "completion_reason": "END_REACHED",
            "login_status": True,
            "observed_count": 0,
            "pages": [],
        },
    )

    detail_missing = svc.get_page("fb_1000777888999")
    assert detail_missing is not None
    assert detail_missing["linked_profiles"][0]["link_status"] == "missing_in_latest_scan"
    assert detail_missing["connection_status"] == "missing_in_latest_scan"

    # Xóa profile p1: page và lịch sử vẫn phải còn trong fanpages.sqlite
    assert pm.delete_profile(p1.id) is True
    detail_after_del = svc.get_page("fb_1000777888999")
    assert detail_after_del is not None
    assert detail_after_del["linked_profiles"][0]["link_status"] == "profile_deleted"
    assert detail_after_del["connection_status"] == "profile_deleted"
    assert len(detail_after_del["metric_history"]) >= 1


# --- 3. REST API & CSP Integration Tests ---


def test_fanpage_manager_api_and_csp(tmp_path, monkeypatch):
    import web.app as app_module

    pm = _make_isolated_pm(tmp_path, monkeypatch)
    mock_launcher = MagicMock()
    mock_launcher.is_running.return_value = False
    mock_launcher.launch.return_value = {"success": True, "port": 55111}

    monkeypatch.setattr(app_module, "profile_manager", pm)
    monkeypatch.setattr(app_module, "launcher", mock_launcher)
    app_module._fanpage_service_cache.clear()

    p1 = pm.create_profile(name="Via API 01")
    _seed_profile_pages(
        pm,
        p1.id,
        [
            {
                "id": "1000555666777",
                "name": "=Page Công Thức Độc Hại",
                "url": "https://www.facebook.com/profile.php?id=1000555666777",
                "followers": "4,2K",
                "likes": "3.800",
                "created_date": "2026-09-01",
                "last_post_at_min": "2026-11-06T00:00:00+00:00",
                "last_post_at_max": "2026-11-06T12:00:00+00:00",
                "last_post_observed_at": "2026-11-06T12:00:00+00:00",
                "last_post_precision": "hour",
                "last_post_confidence": "high",
                "activity_scan_complete": True,
                "activity_checked_at": "2026-11-06T12:00:00+00:00",
            }
        ],
        synced_at=1762450000,
    )

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")

    # 1. Chưa đăng nhập: /fanpages trả trang unlock.html, /api/fanpages trả 401
    client.cookies.clear()
    r_unauth_html = client.get("/fanpages")
    assert r_unauth_html.status_code == 200
    assert "Mở Antidetect Browser" in r_unauth_html.text

    r_unauth_api = client.get("/api/fanpages")
    assert r_unauth_api.status_code == 401

    # 2. Đăng nhập
    r_login = client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    assert r_login.status_code == 200

    # 3. GET /fanpages sau đăng nhập: kiểm tra CSP header và không có inline <script>
    r_html = client.get("/fanpages")
    assert r_html.status_code == 200
    assert "Fanpage Manager" in r_html.text
    assert '/static/js/fanpages.js' in r_html.text
    csp = r_html.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp

    # 4. GET /api/fanpages/summary
    r_sum = client.get("/api/fanpages/summary")
    assert r_sum.status_code == 200
    sum_data = r_sum.json()
    assert sum_data["total_pages"] == 1
    assert sum_data["total_followers"] == 4200
    assert sum_data["internal_age_ready_pages"] == 1

    # 5. GET /api/fanpages
    r_list = client.get("/api/fanpages")
    assert r_list.status_code == 200
    list_data = r_list.json()
    assert list_data["total"] == 1
    item = list_data["items"][0]
    page_id = item["internal_page_id"]
    assert page_id == "pg_1000555666777"
    assert item["internal_age_threshold_met"] is True

    # 6. PATCH /api/fanpages/{page_id} + kiểm tra chống ghi đè 409
    r_patch = client.patch(
        f"/api/fanpages/{page_id}",
        json={
            "tags": ["reup", "chủ lực"],
            "note": "+Ghi chú bắt đầu bằng dấu cộng",
            "preferred_profile_id": p1.id,
            "expected_revision": item["revision"],
        },
    )
    assert r_patch.status_code == 200
    patched_page = r_patch.json()["page"]
    assert patched_page["tags"] == ["reup", "chủ lực"]
    assert patched_page["revision"] == item["revision"] + 1

    # Gửi lại expected_revision cũ -> phải trả 409 Conflict
    r_conflict = client.patch(
        f"/api/fanpages/{page_id}",
        json={
            "note": "Ghi đè lỗi thời",
            "expected_revision": item["revision"],
        },
    )
    assert r_conflict.status_code == 409
    assert r_conflict.json()["code"] == "REVISION_CONFLICT"

    # 7. POST /api/fanpages/bulk
    r_bulk = client.post(
        "/api/fanpages/bulk",
        json={
            "page_ids": [page_id],
            "add_tags": ["bulk-tag"],
            "remove_tags": ["reup"],
        },
    )
    assert r_bulk.status_code == 200
    assert r_bulk.json()["updated_count"] == 1
    r_detail = client.get(f"/api/fanpages/{page_id}")
    assert set(r_detail.json()["page"]["tags"]) == {"chủ lực", "bulk-tag"}

    # 8. GET /api/fanpages/export: UTF-8 BOM + chống CSV formula injection
    r_csv = client.get("/api/fanpages/export")
    assert r_csv.status_code == 200
    assert r_csv.content.startswith(b"\xef\xbb\xbf")
    csv_text = r_csv.content.decode("utf-8-sig")
    assert "'=Page Công Thức Độc Hại" in csv_text
    assert "'+Ghi chú bắt đầu bằng dấu cộng" in csv_text

    # 9. POST /api/fanpages/{page_id}/open
    r_open = client.post(
        f"/api/fanpages/{page_id}/open",
        json={"target": "transparency", "force": True},
    )
    assert r_open.status_code == 200
    open_data = r_open.json()
    assert open_data["profile_id"] == p1.id
    assert "about_profile_transparency" in open_data["opened_url"]
    mock_launcher.launch.assert_called_once_with(
        p1.id, open_url=open_data["opened_url"]
    )

    # Profile không liên kết với page phải bị từ chối 400
    r_bad_open = client.post(
        f"/api/fanpages/{page_id}/open",
        json={"profile_id": "profile-khong-lien-ket"},
    )
    assert r_bad_open.status_code == 400


# ==============================================================================
# STAGE 2 TESTS — Basic Collector & Async Sync Job Queue / API / Scheduler
# ==============================================================================


def test_stage2_merge_page_details_extracts_basic_metrics_and_promotes_id():
    from antidetect.facebook import merge_page_details
    from antidetect.fanpages.repository import parse_compact_metric

    now = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)
    url = "https://www.facebook.com/VanityBrandPage"
    data = {
        "pages": [
            {
                "id": None,
                "name": "Page Vanity Chưa Có ID",
                "url": url,
                "created_raw": "15 tháng 1, 2024",
                "created_date": "2024-01-15",
            }
        ]
    }
    details = {
        url: {
            "created_raw": "",  # Không đọc được ngày tạo ở lần quét này -> phải giữ nguyên created_raw cũ
            "times": [{"n": 3, "u": "h", "raw": "3 giờ", "own": True}],
            "followers_raw": "12,5K người theo dõi",
            "likes_raw": "9.800 lượt thích",
            "category": "Trang tin tức & truyền thông",
            "observed_page_id": "1000888999111",
            "access_state": "ok",
            "completed": True,
            "feed_ready": True,
        }
    }

    merge_page_details(data, details, clock=now)
    item = data["pages"][0]
    assert item["id"] == "1000888999111"
    assert item["id_status"] == "verified"
    assert item["created_raw"] == "15 tháng 1, 2024"
    assert item["created_date"] == "2024-01-15"
    assert item["followers_raw"] == "12,5K người theo dõi"
    assert parse_compact_metric(item["followers"]) == (12500, "12,5K người theo dõi", "rounded")
    assert item["likes_raw"] == "9.800 lượt thích"
    assert parse_compact_metric(item["likes"]) == (9800, "9.800 lượt thích", "exact")
    assert item["category"] == "Trang tin tức & truyền thông"
    assert item["access_state"] == "ok"


def test_stage2_job_browser_ownership_never_stops_user_opened_browser(tmp_path, monkeypatch):
    """Quy tắc cốt lõi Stage 2:
    - Profile do người dùng mở sẵn (is_running=True) -> tuyệt đối KHÔNG được gọi launcher.stop() sau khi quét.
    - Profile đang đóng + auto_start=False -> đánh dấu skipped (PROFILE_CLOSED).
    - Profile đang đóng + auto_start=True -> job tự launch() VÀ bắt buộc tự stop() trong finally.
    """
    pm = _make_isolated_pm(tmp_path, monkeypatch)
    p_open = pm.create_profile(name="User Opened Profile", target_os="windows")
    p_closed = pm.create_profile(name="Closed Profile", target_os="windows")

    running_state = {p_open.id: True, p_closed.id: False}
    mock_launcher = MagicMock()
    mock_launcher.is_running.side_effect = lambda pid: running_state.get(pid, False)

    def _fake_launch(pid, open_url=None):
        running_state[pid] = True
        return {"success": True, "port": 9222}

    def _fake_stop(pid):
        running_state[pid] = False
        return {"success": True}

    mock_launcher.launch.side_effect = _fake_launch
    mock_launcher.stop.side_effect = _fake_stop
    mock_launcher.fetch_facebook_pages.return_value = {
        "logged_in": True,
        "login_status": True,
        "discovery_status": "complete",
        "completion_reason": "END_OF_LIST",
        "account_id_observed": "1000111222",
        "observed_count": 1,
        "pages": [
            {
                "id": "1000999888777",
                "name": "Page Test Ownership",
                "url": "https://www.facebook.com/profile.php?id=1000999888777",
                "followers": 3400,
                "followers_raw": "3,4K người theo dõi",
                "likes": 3100,
                "likes_raw": "3,1K lượt thích",
                "category": "Sáng tạo số",
                "detail_synced": True,
            }
        ],
    }

    svc = FanpageService(
        profile_manager=pm,
        launcher=mock_launcher,
        db_path=str(tmp_path / "fanpages.sqlite"),
        retry_delay_s=0.01,
    )

    # 1. Chạy với auto_start=False: p_open phải completed và KHÔNG bị stop; p_closed phải skipped
    job1, dedup1 = svc.enqueue_sync_job(
        mode="basic",
        profile_ids=[p_open.id, p_closed.id],
        auto_start=False,
        run_async=False,
    )
    assert dedup1 is False
    assert job1["status"] == "completed_with_warnings"
    by_pid = {it["profile_id"]: it for it in job1["items"]}
    assert by_pid[p_open.id]["status"] == "completed"
    assert by_pid[p_open.id]["detail"]["opened_by_job"] is False
    assert by_pid[p_closed.id]["status"] == "skipped"
    assert by_pid[p_closed.id]["error_code"] == "PROFILE_CLOSED"
    mock_launcher.launch.assert_not_called()
    mock_launcher.stop.assert_not_called()

    # 2. Retry job1 với auto_start=True -> chỉ chạy lại p_closed, tự launch và tự stop p_closed
    job2, dedup2 = svc.retry_sync_job(job1["id"], auto_start=True, run_async=False)
    assert dedup2 is False
    assert job2["status"] == "completed"
    assert len(job2["items"]) == 1
    item_closed = job2["items"][0]
    assert item_closed["profile_id"] == p_closed.id
    assert item_closed["status"] == "completed"
    assert item_closed["detail"]["opened_by_job"] is True
    mock_launcher.launch.assert_called_once_with(p_closed.id)
    mock_launcher.stop.assert_called_once_with(p_closed.id)
    # p_open vẫn đang mở, tuyệt đối không bị stop
    assert running_state[p_open.id] is True
    assert running_state[p_closed.id] is False


def test_stage2_job_error_classification_and_transient_retry(tmp_path, monkeypatch):
    """Kiểm chứng phân loại lỗi và retry:
    - LOGIN_LOST -> dừng ngay ở attempt 1 (không retry).
    - Lỗi tạm thời (RuntimeError / CDP) -> retry tối đa 1 + max_retries lần; nếu lần thử thứ 2 thành công thì báo completed.
    """
    pm = _make_isolated_pm(tmp_path, monkeypatch)
    p_login_lost = pm.create_profile(name="Login Lost Profile", target_os="windows")
    p_flaky = pm.create_profile(name="Flaky Profile", target_os="windows")

    mock_launcher = MagicMock()
    mock_launcher.is_running.return_value = True

    attempts_counter = {p_login_lost.id: 0, p_flaky.id: 0}

    def _side_effect(pid, timeout, deep):
        attempts_counter[pid] += 1
        if pid == p_login_lost.id:
            return {
                "logged_in": False,
                "login_status": False,
                "discovery_status": "failed",
                "completion_reason": "LOGIN_LOST",
                "pages": [],
            }
        if attempts_counter[pid] == 1:
            raise RuntimeError("CDP websocket timeout tạm thời")
        return {
            "logged_in": True,
            "login_status": True,
            "discovery_status": "complete",
            "completion_reason": "END_OF_LIST",
            "observed_count": 1,
            "pages": [
                {
                    "id": "1000222333444",
                    "name": "Page Flaky Recovered",
                    "url": "https://www.facebook.com/profile.php?id=1000222333444",
                }
            ],
        }

    mock_launcher.fetch_facebook_pages.side_effect = _side_effect

    svc = FanpageService(
        profile_manager=pm,
        launcher=mock_launcher,
        db_path=str(tmp_path / "fanpages.sqlite"),
        max_retries=2,
        retry_delay_s=0.01,
    )

    job, _ = svc.enqueue_sync_job(
        mode="discovery",
        profile_ids=[p_login_lost.id, p_flaky.id],
        run_async=False,
    )
    assert job["status"] == "completed_with_errors"
    by_pid = {it["profile_id"]: it for it in job["items"]}

    # LOGIN_LOST không được retry (attempts == 1)
    assert by_pid[p_login_lost.id]["status"] == "failed"
    assert by_pid[p_login_lost.id]["error_code"] == "LOGIN_LOST"
    assert by_pid[p_login_lost.id]["attempts"] == 1

    # Flaky thành công ở lần thử thứ 2
    assert by_pid[p_flaky.id]["status"] == "completed"
    assert by_pid[p_flaky.id]["attempts"] == 2


def test_stage2_sync_job_api_endpoints_and_scheduler_actions(tmp_path, monkeypatch):
    """Kiểm chứng các endpoint /api/fanpage-sync/jobs*, deduplication, cancel, retry và scheduler VALID_ACTIONS."""
    from fastapi.testclient import TestClient
    import web.app as app_mod
    from antidetect.scheduler import ScheduleStore, VALID_ACTIONS

    assert "sync_fanpages" in VALID_ACTIONS
    assert "sync_fanpages_deep" in VALID_ACTIONS

    sched_store = ScheduleStore(str(tmp_path / "schedules.json"))
    rule = sched_store.add("Đồng bộ Fanpage sáng", "08:30", "sync_fanpages_deep")
    assert rule["action"] == "sync_fanpages_deep"

    pm = _make_isolated_pm(tmp_path, monkeypatch)
    p1 = pm.create_profile(name="API Sync Profile 1", target_os="windows")

    mock_launcher = MagicMock()
    mock_launcher.is_running.return_value = False
    mock_launcher.active_ports = {}

    monkeypatch.setattr(app_mod, "profile_manager", pm)
    monkeypatch.setattr(app_mod, "launcher", mock_launcher)
    monkeypatch.setattr(app_mod, "DATA_DIR", str(tmp_path))
    app_mod._fanpage_service_cache.clear()

    client = TestClient(app_mod.app, base_url="http://127.0.0.1:8000")
    r_login = client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_mod.app.state.access_key},
    )
    assert r_login.status_code == 200

    # 1. Tạo job với auto_start=False khi profile đang đóng -> item sẽ skipped
    r_create = client.post(
        "/api/fanpage-sync/jobs",
        json={"mode": "basic", "profile_ids": [p1.id], "auto_start": False},
    )
    assert r_create.status_code == 202
    create_data = r_create.json()
    assert create_data["success"] is True
    job_id = create_data["job"]["id"]

    # Chờ worker thread hoàn tất (rất nhanh vì profile đang đóng -> skipped ngay)
    final_job = None
    for _ in range(30):
        r_get = client.get(f"/api/fanpage-sync/jobs/{job_id}")
        assert r_get.status_code == 200
        final_job = r_get.json()["job"]
        if final_job["status"] not in ("queued", "running"):
            break
        import time
        time.sleep(0.02)

    assert final_job is not None
    assert final_job["status"] == "completed_with_warnings"
    assert final_job["items"][0]["status"] == "skipped"
    assert final_job["items"][0]["error_code"] == "PROFILE_CLOSED"

    # 2. GET /api/fanpage-sync/jobs
    r_list = client.get("/api/fanpage-sync/jobs")
    assert r_list.status_code == 200
    assert any(j["id"] == job_id for j in r_list.json()["jobs"])

    # 3. Chuẩn bị mock launch + fetch để retry job thành công
    mock_launcher.launch.return_value = {"success": True, "port": 9333}
    mock_launcher.stop.return_value = {"success": True}
    mock_launcher.fetch_facebook_pages.return_value = {
        "logged_in": True,
        "login_status": True,
        "discovery_status": "complete",
        "completion_reason": "END_OF_LIST",
        "observed_count": 1,
        "pages": [
            {
                "id": "1000777666555",
                "name": "Page Từ Retry Job",
                "url": "https://www.facebook.com/profile.php?id=1000777666555",
                "followers": 8900,
                "followers_raw": "8,9K người theo dõi",
                "detail_synced": True,
            }
        ],
    }

    r_retry = client.post(
        f"/api/fanpage-sync/jobs/{job_id}/retry",
        json={"auto_start": True},
    )
    assert r_retry.status_code == 202
    retry_job_id = r_retry.json()["job"]["id"]
    assert retry_job_id != job_id

    retry_final = None
    for _ in range(40):
        r_get2 = client.get(f"/api/fanpage-sync/jobs/{retry_job_id}")
        assert r_get2.status_code == 200
        retry_final = r_get2.json()["job"]
        if retry_final["status"] not in ("queued", "running"):
            break
        import time
        time.sleep(0.02)

    assert retry_final is not None
    assert retry_final["status"] == "completed"
    mock_launcher.launch.assert_called_once_with(p1.id)
    mock_launcher.stop.assert_called_once_with(p1.id)

    # Kiểm tra page đã được đồng bộ vào catalog SQLite sau khi job hoàn tất
    r_pages = client.get("/api/fanpages")
    assert r_pages.status_code == 200
    items = r_pages.json()["items"]
    assert len(items) == 1
    assert items[0]["fb_page_id"] == "1000777666555"
    assert items[0]["followers_count"] == 8900

    # Retry lại một job đã completed hoàn toàn -> phải trả 400
    r_retry_bad = client.post(f"/api/fanpage-sync/jobs/{retry_job_id}/retry")
    assert r_retry_bad.status_code == 400


def test_stage2_job_deduplication_and_cancellation(tmp_path, monkeypatch):
    """Kiểm chứng deduplication khi đã có job đang queued/running và hủy job qua /cancel."""
    from fastapi.testclient import TestClient
    import web.app as app_mod

    pm = _make_isolated_pm(tmp_path, monkeypatch)
    p1 = pm.create_profile(name="Cancelable Profile 1", target_os="windows")
    p2 = pm.create_profile(name="Cancelable Profile 2", target_os="windows")

    mock_launcher = MagicMock()
    mock_launcher.is_running.return_value = True
    mock_launcher.active_ports = {}

    monkeypatch.setattr(app_mod, "profile_manager", pm)
    monkeypatch.setattr(app_mod, "launcher", mock_launcher)
    monkeypatch.setattr(app_mod, "DATA_DIR", str(tmp_path))
    app_mod._fanpage_service_cache.clear()

    svc = app_mod.get_fanpage_service()
    # Tạo sẵn 1 job ở trạng thái queued trong DB để mô phỏng job đang chạy/chờ
    queued_job = svc.repo.create_sync_job(
        job_id="fjob_active_test01",
        mode="discovery",
        profile_items=[
            {"profile_id": p1.id, "profile_name": p1.name},
            {"profile_id": p2.id, "profile_name": p2.name},
        ],
    )
    assert queued_job["status"] == "queued"

    client = TestClient(app_mod.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_mod.app.state.access_key},
    )

    # Gọi tạo job mới khi đang có job active (force_new=False) -> trả về job cũ với deduplicated=True
    r_dup = client.post(
        "/api/fanpage-sync/jobs",
        json={"mode": "discovery", "force_new": False},
    )
    assert r_dup.status_code == 202
    dup_data = r_dup.json()
    assert dup_data["deduplicated"] is True
    assert dup_data["job"]["id"] == "fjob_active_test01"

    # Hủy job đang active qua POST /api/fanpage-sync/jobs/{job_id}/cancel
    r_cancel = client.post("/api/fanpage-sync/jobs/fjob_active_test01/cancel")
    assert r_cancel.status_code == 200
    canceled_job = r_cancel.json()["job"]
    assert canceled_job["status"] == "cancelled"
    assert all(it["status"] == "cancelled" for it in canceled_job["items"])


