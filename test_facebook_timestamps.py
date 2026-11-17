"""F3 — mô hình khoảng thời gian bài đăng, migration legacy và cache.

Các test thuần Python (không cần Chrome/Facebook). Mọi mốc thời gian đều
được tiêm cố định để không phụ thuộc đồng hồ máy.
"""
import datetime

import pytest

from antidetect import facebook
from antidetect.facebook import (
    absolute_day_interval,
    classify_facebook_page,
    merge_page_details,
    normalize_last_post_fields,
    parse_relative_label,
    relative_label_interval,
    to_utc_iso,
)
from web.services import enrich_facebook_pages

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
VN = "Asia/Ho_Chi_Minh"


def _iso(dt):
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_relative_intervals_for_every_unit():
    obs = NOW
    cases = {
        (2, "s"): (obs - datetime.timedelta(seconds=3), obs - datetime.timedelta(seconds=2)),
        (2, "m"): (obs - datetime.timedelta(minutes=3), obs - datetime.timedelta(minutes=2)),
        (2, "h"): (obs - datetime.timedelta(hours=3), obs - datetime.timedelta(hours=2)),
        (2, "d"): (obs - datetime.timedelta(days=3), obs - datetime.timedelta(days=2)),
        (2, "w"): (obs - datetime.timedelta(days=21), obs - datetime.timedelta(days=14)),
        # Tháng: cận cũ (n+1)*31 ngày, cận mới n*28 ngày.
        (2, "mo"): (obs - datetime.timedelta(days=93), obs - datetime.timedelta(days=56)),
        (2, "y"): (obs - datetime.timedelta(days=1098), obs - datetime.timedelta(days=730)),
        (0, "now"): (obs - datetime.timedelta(seconds=60), obs),
    }
    for (n, unit), expected in cases.items():
        assert relative_label_interval(n, unit, obs) == expected, (n, unit)
    assert relative_label_interval(-1, "d", obs) is None
    assert relative_label_interval(2, "fortnight", obs) is None
    assert relative_label_interval(2, "d", "not-a-date") is None


def test_parse_relative_label_vietnamese_and_english():
    assert parse_relative_label("2 ngày trước")["u"] == "d"
    assert parse_relative_label("3 tuần trước")["u"] == "w"
    assert parse_relative_label("5 giờ trước")["u"] == "h"
    assert parse_relative_label("2 tháng trước")["u"] == "mo"
    assert parse_relative_label("1 năm trước")["u"] == "y"
    assert parse_relative_label("4 days ago")["n"] == 4
    assert parse_relative_label("vừa xong")["u"] == "now"
    assert parse_relative_label("just now")["u"] == "now"
    assert parse_relative_label("hôm qua") is None


def test_relative_labels_do_not_become_exact_timestamps():
    page = {"url": "https://www.facebook.com/page"}
    merge_page_details(
        {"pages": [page]},
        {
            page["url"]: {
                "times": [{"n": 2, "u": "d", "raw": "2 ngày trước"}],
                "observed_at": _iso(NOW),
                "completed": True,
            }
        },
        display_tz=VN,
        clock=lambda: NOW,
    )
    assert "last_post_at" not in page
    assert page["last_post_precision"] == "approximate"
    assert page["last_post_at_min"] == _iso(NOW - datetime.timedelta(days=3))
    assert page["last_post_at_max"] == _iso(NOW - datetime.timedelta(days=2))
    assert page["last_post_age_min_days"] == 2
    assert page["last_post_age_max_days"] == 3
    assert page["last_post_days"] == 2
    assert page["status"] == "active"


@pytest.mark.parametrize("label,timezone,expected_min,expected_max", [
    ("Thứ Hai, 29 Tháng 6, 2026 lúc 23:46", VN,
     "2026-06-29T16:46:00Z", "2026-06-29T16:47:00Z"),
    ("June 29, 2026 at 11:46 PM", VN,
     "2026-06-29T16:46:00Z", "2026-06-29T16:47:00Z"),
    ("Monday, 29 June 2026 at 11:46 PM", VN,
     "2026-06-29T16:46:00Z", "2026-06-29T16:47:00Z"),
    ("2026-06-29 at 23:46", VN,
     "2026-06-29T16:46:00Z", "2026-06-29T16:47:00Z"),
    ("November 1, 2026 at 1:30 AM", "America/New_York",
     "2026-11-01T05:30:00Z", "2026-11-01T06:31:00Z"),
])
def test_absolute_anchor_time_preserves_clock_precision(label, timezone, expected_min, expected_max):
    page = {"url": "https://www.facebook.com/fixture"}
    merge_page_details({"pages": [page]}, {
        page["url"]: {"times": [{"raw": label, "own": True}],
                      "observed_at": _iso(NOW), "completed": True}
    }, display_tz=timezone, clock=lambda: NOW)
    assert page["last_post_at_min"] == expected_min
    assert page["last_post_at_max"] == expected_max
    assert page["last_post_precision"] == "approximate"
    assert "last_post_at" not in page


@pytest.mark.parametrize("label,timezone", [
    ("29 Tháng 6 lúc 23:46", VN),
    ("29 Tháng 6, 2026", VN),
    ("29 Tháng 6, 2026 lúc 24:46", VN),
    ("29 Tháng 6, 2026 lúc 23:61", VN),
    ("June 29, 2026 at 11:46:30 PM", VN),
    ("June 29, 2026 at 11:46:30 AM", VN),
    ("June 29, 2026 at 11:46 PM UTC", VN),
    ("June 29, 2026 at 11:46 PM +07:00", VN),
    ("06/07/2026 at 11:46 PM", VN),
    ("06-07-2026 at 11:46 PM", VN),
    ("06.07.2026 at 11:46 PM", VN),
    ("29/06/2026 at 11:46 PM", VN),
    ("March 8, 2026 at 2:30 AM", "America/New_York"),
])
def test_incomplete_or_invalid_absolute_anchor_time_is_unknown(label, timezone):
    page = {"url": "https://www.facebook.com/fixture"}
    merge_page_details({"pages": [page]}, {
        page["url"]: {"times": [{"raw": label, "own": True}],
                      "observed_at": _iso(NOW), "completed": True}
    }, display_tz=timezone, clock=lambda: NOW)
    assert page["last_post_precision"] == "unknown"
    assert "last_post_at_min" not in page


def test_midnight_boundary_follows_display_timezone():
    day = datetime.date(2026, 1, 1)
    start, end = absolute_day_interval(day, VN)
    assert _iso(start) == "2025-12-31T17:00:00Z"
    assert _iso(end) == "2026-01-01T17:00:00Z"
    assert (end - start) == datetime.timedelta(days=1)

    ny_start, ny_end = absolute_day_interval(day, "America/New_York")
    assert _iso(ny_start) == "2026-01-01T05:00:00Z"
    assert _iso(ny_end) == "2026-01-02T05:00:00Z"

    # fallback timezone khi profile chưa có/không hợp lệ.
    fb_start, _ = absolute_day_interval(day, "Not/AZone")
    assert _iso(fb_start) == "2025-12-31T17:00:00Z"


def test_leap_year_and_month_year_units():
    assert facebook.parse_fb_created_date("29/02/2024") == datetime.date(2024, 2, 29)
    assert facebook.parse_fb_created_date("29/02/2023") is None
    start, end = absolute_day_interval("2024-02-29", VN)
    assert (end - start) == datetime.timedelta(days=1)
    # Năm nhuận không làm công thức "1 năm trước" thành điểm chính xác.
    obs = datetime.datetime(2024, 3, 1, tzinfo=UTC)
    low, high = relative_label_interval(1, "y", obs)
    assert (high - low).days == 367


def test_legacy_observation_0101_to_0511_is_about_37_days_not_active():
    page = {
        "name": "Legacy Page",
        "url": "https://www.facebook.com/legacy",
        "last_post_days": 2,
        "last_post_raw": "2 ngày trước",
        "detail_synced": True,
        "detail_synced_at": "2026-10-01",
    }
    normalized = normalize_last_post_fields(page, display_tz=VN, now=NOW)
    assert normalized["last_post_legacy"] is True
    assert normalized["last_post_precision"] == "approximate"
    assert normalized["last_post_at_min"] == "2026-09-27T17:00:00Z"
    assert normalized["last_post_at_max"] == "2026-09-29T17:00:00Z"
    assert normalized["last_post_observed_at"] == "2026-10-01T17:00:00Z"

    result = classify_facebook_page(normalized, now=NOW, timezone=VN)
    # Quan sát 01/10 đã quá 48h → trạng thái hiện tại unknown, nhưng vẫn
    # hiển thị tuổi đã ghi nhận ~36–38 ngày và badge dữ liệu cũ.
    assert result["status"] == "unknown"
    assert result["activity_stale"] is True
    assert result["activity_quality"] == "stale"
    assert result["last_post_age_min_days"] <= 37 <= result["last_post_age_max_days"]
    assert result["last_post_days"] == result["last_post_age_min_days"]
    assert result["status"] != "active"


def test_legacy_normalization_is_idempotent_and_keeps_observed_at():
    page = {
        "last_post_days": 2,
        "detail_synced": True,
        "detail_synced_at": "2026-10-01",
    }
    first = normalize_last_post_fields(page, display_tz=VN, now=NOW)
    later = NOW + datetime.timedelta(days=3)
    second = normalize_last_post_fields(first, display_tz=VN, now=later)
    for key in (
        "last_post_at_min",
        "last_post_at_max",
        "last_post_observed_at",
        "last_post_precision",
        "last_post_legacy",
    ):
        assert first[key] == second[key], key

    # Số last_post_days cũ không còn là nguồn sự thật sau khi đã chuẩn hóa.
    tampered = dict(first, last_post_days=999)
    third = normalize_last_post_fields(tampered, display_tz=VN, now=later)
    assert third["last_post_at_min"] == first["last_post_at_min"]
    assert third["last_post_at_max"] == first["last_post_at_max"]
    assert third["last_post_observed_at"] == first["last_post_observed_at"]


def test_legacy_age_grows_at_response_time():
    page = {
        "last_post_days": 2,
        "detail_synced": True,
        "detail_synced_at": "2026-10-01",
    }
    early = classify_facebook_page(
        page, now=datetime.datetime(2026, 10, 2, 12, tzinfo=UTC), timezone=VN
    )
    late = classify_facebook_page(page, now=NOW, timezone=VN)
    assert late["last_post_days"] > early["last_post_days"]
    assert early["status"] == "active"  # vẫn trong 48h kể từ cuối ngày 01/10
    assert late["status"] == "unknown"  # quá stale, không còn kết luận hoạt động
    assert late["activity_stale"] is True


@pytest.mark.parametrize(
    "entry",
    [
        {"last_post_days": 2},  # thiếu mốc quan sát
        {"last_post_days": 2, "detail_synced_at": "not-a-date"},
        {"last_post_days": -1, "detail_synced_at": "2026-10-01"},
        {"last_post_days": 2, "detail_synced_at": "2099-01-01"},  # tương lai
        {"facebook_pages_synced_at": 1790000000},
        {"detail_synced": True},
    ],
)
def test_missing_or_invalid_observation_is_unknown_without_fake_timestamp(entry):
    normalized = normalize_last_post_fields(entry, display_tz=VN, now=NOW)
    assert "last_post_at_min" not in normalized
    assert "last_post_at_max" not in normalized
    result = classify_facebook_page(normalized, now=NOW, timezone=VN)
    assert result["status"] in ("unknown", None)
    assert result["status"] != "active"


def test_invalid_new_timestamps_keep_raw_and_do_not_guess():
    page = {
        "last_post_at_min": "garbage",
        "last_post_at_max": "2026-11-01T00:00:00Z",
        "last_post_raw": "2 ngày trước",
        "detail_synced": True,
    }
    normalized = normalize_last_post_fields(page, display_tz=VN, now=NOW)
    assert normalized["last_post_precision"] == "unknown"
    assert normalized["last_post_quality"] == "invalid"
    assert normalized["last_post_raw"] == "2 ngày trước"
    result = classify_facebook_page(normalized, now=NOW, timezone=VN)
    assert result["status"] == "unknown"


def test_future_relative_candidate_is_rejected():
    page = {"url": "https://www.facebook.com/future"}
    observed = NOW + datetime.timedelta(days=10)
    merge_page_details(
        {"pages": [page]},
        {
            page["url"]: {
                "times": [{"n": 2, "u": "d"}],
                "observed_at": _iso(observed),
                "completed": True,
            }
        },
        display_tz=VN,
        clock=lambda: NOW,
    )
    assert "last_post_at_min" not in page
    assert page["activity_scan_complete"] is False
    assert page["status"] == "unknown"


def test_pinned_comments_suggested_and_foreign_posts_are_ignored():
    page = {"url": "https://www.facebook.com/own"}
    merge_page_details(
        {"pages": [page]},
        {
            page["url"]: {
                "times": [
                    {"n": 1, "u": "h", "pinned": True},
                    {"n": 30, "u": "m", "comment": True},
                    {"n": 10, "u": "m", "suggested": True},
                    {"n": 5, "u": "m", "sponsored": True},
                    {"n": 1, "u": "d", "own": False},
                    {"n": 2, "u": "d", "raw": "2 ngày trước", "own": True},
                ],
                "observed_at": _iso(NOW),
                "completed": True,
            }
        },
        display_tz=VN,
        clock=lambda: NOW,
    )
    assert page["last_post_raw"] == "2 ngày trước"
    assert page["last_post_age_min_days"] == 2
    assert page["status"] == "active"


def test_newest_candidate_is_selected_not_first_in_dom():
    page = {"url": "https://www.facebook.com/order"}
    merge_page_details(
        {"pages": [page]},
        {
            page["url"]: {
                "times": [
                    {"n": 3, "u": "w"},
                    {"n": 2, "u": "d"},
                    {"n": 4, "u": "w"},
                ],
                "observed_at": _iso(NOW),
                "completed": True,
            }
        },
        display_tz=VN,
        clock=lambda: NOW,
    )
    assert page["last_post_age_min_days"] == 2
    assert page["last_post_age_max_days"] == 3
    assert page["last_post_raw"] == "2 d"


def test_scan_without_post_evidence_keeps_previous_recorded_ages():
    page = {
        "url": "https://www.facebook.com/keep",
        "last_post_at_min": _iso(NOW - datetime.timedelta(days=9)),
        "last_post_at_max": _iso(NOW - datetime.timedelta(days=8)),
        "last_post_observed_at": _iso(NOW - datetime.timedelta(hours=1)),
        "last_post_precision": "approximate",
        "activity_scan_complete": True,
    }
    merge_page_details(
        {"pages": [page]},
        {
            page["url"]: {
                "times": [],
                "observed_at": _iso(NOW),
                "completed": False,
            }
        },
        display_tz=VN,
        clock=lambda: NOW,
    )
    # Không xóa mốc cũ, nhưng lần đọc thiếu phạm vi → trạng thái không xác minh.
    assert page["last_post_at_min"] == _iso(NOW - datetime.timedelta(days=9))
    assert page["last_post_age_min_days"] == 8
    assert page["activity_scan_complete"] is False
    assert page["status"] == "unknown"
    assert page["activity_quality"] == "incomplete"


def test_enrich_reclassifies_cache_without_freezing_ages():
    cached = [{
        "name": "Cached",
        "url": "https://www.facebook.com/cached",
        "last_post_days": 2,
        "detail_synced": True,
        "detail_synced_at": "2026-10-01",
    }]
    later = enrich_facebook_pages(
        cached, now=NOW, timezone=VN, stale_after_hours=48
    )[0]
    assert later["status"] == "unknown"
    assert later["activity_stale"] is True
    assert later["last_post_age_min_days"] <= 37 <= later["last_post_age_max_days"]
    assert later["last_post_legacy"] is True

    fresh_same_day = enrich_facebook_pages(
        cached,
        now=datetime.datetime(2026, 10, 1, 12, tzinfo=UTC),
        timezone=VN,
        stale_after_hours=48,
    )[0]
    assert fresh_same_day["status"] == "active"
    assert fresh_same_day["last_post_age_min_days"] == 1
    assert fresh_same_day["last_post_age_max_days"] == 3


def test_facebook_page_url_identity_check():
    assert facebook._fb_same_page(
        "https://www.facebook.com/pages/Shop/123456/posts/9",
        "https://www.facebook.com/pages/Shop/123456",
    )
    assert facebook._fb_same_page(
        "https://www.facebook.com/Shop-123456/",
        "https://www.facebook.com/pages/Shop/123456",
    )
    assert facebook._fb_same_page(
        "https://www.facebook.com/shop",
        "https://www.facebook.com/shop/",
    )
    assert not facebook._fb_same_page(
        "https://www.facebook.com/login",
        "https://www.facebook.com/shop",
    )
    assert not facebook._fb_same_page(
        "https://www.facebook.com/other-999999/",
        "https://www.facebook.com/pages/Shop/123456",
    )


def test_scraper_js_scopes_to_own_posts_and_excludes_comments():
    js = facebook._FB_PAGE_METRICS_JS
    assert "div[role=\"article\"]" in js
    assert "PINNED" in js and "PROMOTED" in js
    assert "isComment" in js and "comment_id=" in js
    assert "isPageLink" in js and "isPermalink" in js
    assert "own: true" in js
    assert "parserVersion" in js


def test_page_read_api_cache_returns_login_unknown(monkeypatch):
    from fastapi.testclient import TestClient

    import web.app as app_module

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    created = client.post("/api/profiles", json={"name": "F3 Cache API"})
    pid = created.json()["id"]
    try:
        monkeypatch.setattr(
            app_module.launcher, "is_running", lambda _pid: False, raising=False
        )
        profile = app_module.profile_manager.get_profile(pid)
        profile.facebook_pages = [{
            "name": "Cached Page",
            "url": "https://www.facebook.com/cachedpage",
            "last_post_days": 2,
            "last_post_raw": "2 ngày trước",
            "detail_synced": True,
            "detail_synced_at": "2026-10-01",
        }]
        profile.facebook_pages_synced_at = 1790000000
        assert app_module.profile_manager.update_profile(profile) is not None

        response = client.get(f"/api/profiles/{pid}/facebook/pages")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["source"] == "cache"
        assert body["login_status"] == "unknown"
        assert body["logged_in"] is None
        assert body["stale"] is True
        page = body["pages"][0]
        assert page["status"] == "unknown"
        assert page["activity_stale"] is True
        assert page["last_post_age_min_days"] >= 30
        assert page["last_post_age_max_days"] >= page["last_post_age_min_days"]
        assert page["last_post_at_min"] and page["last_post_at_max"]
    finally:
        client.delete(f"/api/profiles/{pid}")
