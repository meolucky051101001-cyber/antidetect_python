"""Tests cho Fanpage Manager Giai đoạn 1 (Catalog SQLite, Deduplication, F3/F4, REST API, CSV Export)."""
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
