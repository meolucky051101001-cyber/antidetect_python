"""Pure parsing/merge helpers cho fanpage Facebook, không cần Chrome/Facebook."""
import datetime

from antidetect.facebook import (
    classify_facebook_page,
    fb_unit_to_days,
    merge_page_details,
    parse_fb_created_date,
)


def test_fb_unit_to_days_known_and_unknown_units():
    assert fb_unit_to_days(2, "d") == 2
    assert fb_unit_to_days(3, "w") == 21
    assert fb_unit_to_days(2, "mo") == 60
    assert fb_unit_to_days(1, "y") == 365
    assert fb_unit_to_days(5, "m") == 0
    assert fb_unit_to_days(5, "unknown") == 0


def test_parse_fb_created_date_formats():
    assert parse_fb_created_date("Ngày tạo trang: 15/03/2020") == datetime.date(2020, 3, 15)
    assert parse_fb_created_date("Page created on March 5, 2019") == datetime.date(2019, 3, 5)
    assert parse_fb_created_date("ngày 5 tháng 3, 2020") == datetime.date(2020, 3, 5)
    assert parse_fb_created_date("2021-07-02") == datetime.date(2021, 7, 2)


def test_parse_fb_created_date_invalid_values_return_none():
    assert parse_fb_created_date(None) is None
    assert parse_fb_created_date("") is None
    assert parse_fb_created_date("13/25/2020") is None
    assert parse_fb_created_date("31/02/2020") is None
    assert parse_fb_created_date("2020-13-01") is None
    assert parse_fb_created_date("no date here") is None


def test_classify_facebook_page_status_and_ready():
    today = datetime.date(2026, 10, 26)
    active = classify_facebook_page({"last_post_days": 7}, today)
    assert active["status"] == "active"
    assert active["ready"] is False

    stopped = classify_facebook_page(
        {"last_post_days": 8, "detail_synced": True}, today
    )
    assert stopped["status"] == "stopped"

    unknown = classify_facebook_page({"detail_synced": True}, today)
    assert unknown["status"] == "unknown"

    never_synced = classify_facebook_page({}, today)
    assert never_synced["status"] is None
    assert never_synced["ready"] is False

    ready = classify_facebook_page({"created_date": "2026-09-24"}, today)
    assert ready["age_days"] == 32
    assert ready["ready"] is True

    not_ready = classify_facebook_page({"created_date": "2026-09-27"}, today)
    assert not_ready["age_days"] == 29
    assert not_ready["ready"] is False

    bad_created = classify_facebook_page({"created_date": "not-a-date"}, today)
    assert bad_created["age_days"] is None
    assert bad_created["ready"] is False


def test_merge_page_details_sets_created_date_and_last_post():
    data = {
        "pages": [
            {"url": "https://www.facebook.com/one", "name": "One"},
            {"url": "https://www.facebook.com/two", "name": "Two"},
        ]
    }
    details = {
        "https://www.facebook.com/one": {
            "created_raw": "Ngày tạo trang: 15/03/2020",
            "times": [{"n": 2, "u": "d"}, {"n": 3, "u": "w"}],
        },
        "https://www.facebook.com/two": {},
    }
    merge_page_details(data, details)
    one, two = data["pages"]
    assert one["created_date"] == "2020-03-15"
    assert one["last_post_days"] == 2
    assert one["last_post_raw"] == "2 d"
    assert one["detail_synced"] is True
    assert one["status"] == "active"
    assert one["detail_synced_at"] == datetime.date.today().isoformat()

    assert two["created_raw"] is None
    assert two["last_post_raw"] is None
    assert two["detail_synced"] is False
    assert two["status"] is None


def test_merge_page_details_keeps_raw_when_date_parse_fails():
    data = {"pages": [{"url": "https://www.facebook.com/x"}]}
    details = {
        "https://www.facebook.com/x": {
            "created_raw": "some unknown format",
            "times": [],
        }
    }
    merge_page_details(data, details)
    page = data["pages"][0]
    assert page["created_raw"] == "some unknown format"
    assert "created_date" not in page
    assert page["detail_synced"] is True
    assert page["status"] == "unknown"
