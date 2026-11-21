"""F4 — hợp đồng discovery, tích lũy ngoài DOM và hợp nhất cache theo trường."""
import threading


from antidetect.fanpage_scan import (
    PageAccumulator,
    ensure_scan_contract,
    evaluate_discovery,
    merge_discovery_pages,
    scan_detail,
)
from antidetect.profile_locks import ProfileLockTimeout
from antidetect.storage import ProfileManager


def _page(index, **extra):
    entry = {
        "name": f"Page {index}",
        "url": f"https://www.facebook.com/page{index}",
        "id": str(100000 + index),
    }
    entry.update(extra)
    return entry


def _scan(**kwargs):
    kwargs.setdefault("pages", [])
    kwargs.setdefault("started_at", "2026-11-05T09:59:00Z")
    kwargs.setdefault("finished_at", "2026-11-05T10:00:00Z")
    return evaluate_discovery(**kwargs)


def test_completion_requires_evidence_not_just_scrolling():
    # 200 page, không tổng, không mốc kết thúc → partial + NO_END_SIGNAL.
    pages = [_page(i) for i in range(1, 201)]
    partial = _scan(pages=pages, stability="stable")
    assert partial["discovery_status"] == "partial"
    assert partial["completion_reason"] == "NO_END_SIGNAL"
    assert partial["observed_count"] == 200
    assert len({p["id"] for p in partial["pages"]}) == 200

    # Cùng 200 page nhưng tổng nguồn khớp → complete + TOTAL_MATCH.
    complete = _scan(pages=pages, total=200)
    assert complete["discovery_status"] == "complete"
    assert complete["completion_reason"] == "TOTAL_MATCH"
    assert complete["observed_count"] == 200


def test_completion_boundaries_zero_one_and_end_marker():
    empty = _scan(pages=[], empty_confirmed=True, ui_recognized=True)
    assert empty["discovery_status"] == "complete"
    assert empty["completion_reason"] == "EMPTY_CONFIRMED"
    assert empty["observed_count"] == 0

    one = _scan(pages=[_page(1)], total=1)
    assert one["discovery_status"] == "complete"
    assert one["completion_reason"] == "TOTAL_MATCH"

    marker = _scan(pages=[_page(i) for i in range(1, 102)], end_marker=True)
    assert marker["discovery_status"] == "complete"
    assert marker["completion_reason"] == "END_MARKER"
    assert marker["observed_count"] == 101


def test_resource_limits_and_failures_are_explicit():
    assert _scan(pages=[_page(1)], deadline_hit=True)["completion_reason"] == "DEADLINE"
    assert _scan(pages=[_page(1)], max_pages_hit=True)["completion_reason"] == "MAX_PAGES"
    assert (
        _scan(pages=[_page(1)], max_scrolls_hit=True)["completion_reason"]
        == "MAX_SCROLLS"
    )
    for scan, reason in (
        (_scan(pages=[], logged_in=False, login_lost=True), "LOGIN_LOST"),
        (_scan(pages=[], nav_error=True), "NAVIGATION_ERROR"),
        (_scan(pages=[], ui_recognized=False), "UI_UNRECOGNIZED"),
        (
            _scan(
                pages=[_page(1)],
                identity_status="verified",
                account_id_observed="22222",
                expected_account_id="11111",
            ),
            "LOGIN_IDENTITY_MISMATCH",
        ),
    ):
        assert scan["discovery_status"] == "failed"
        assert scan["completion_reason"] == reason
        # Failed scan không trả page để tránh merge nhầm.
        assert scan["pages"] == []
        assert scan["observed_count"] == 0


def test_accumulator_keeps_virtualized_pages_and_dedupes():
    acc = PageAccumulator()
    assert acc.add_many([_page(1), _page(2)]) == 2
    # Vòng sau DOM chỉ còn page 2 + page 3: page 1 vẫn nằm trong tập tích lũy.
    assert acc.add_many([_page(2), _page(3)]) == 1
    assert [p["name"] for p in acc.pages()] == ["Page 1", "Page 2", "Page 3"]
    assert len(acc) == 3


def test_same_name_different_ids_and_multiple_urls():
    acc = PageAccumulator()
    acc.add_many(
        [
            {
                "name": "Trùng Tên",
                "url": "https://www.facebook.com/alpha-11111",
                "id": "11111",
            },
            {
                "name": "Trùng Tên",
                "url": "https://www.facebook.com/beta-22222",
                "id": "22222",
            },
            {
                "name": "Trùng Tên",
                "url": "https://www.facebook.com/pages/Trung-Ten/33333",
                "id": "33333",
            },
            {
                "name": "Trùng Tên",
                "url": "https://www.facebook.com/33333",
                "id": "33333",
            },
        ]
    )
    assert len(acc) == 3
    assert {p["id"] for p in acc.pages()} == {"11111", "22222", "33333"}


def test_url_only_entry_is_promoted_when_id_appears():
    acc = PageAccumulator()
    assert acc.add({"name": "Shop", "url": "https://www.facebook.com/shop365"}) is True
    assert acc.pages()[0]["id_status"] == "url_only"
    assert (
        acc.add(
            {
                "name": "Shop",
                "url": "https://www.facebook.com/shop365",
                "id": "44444",
            }
        )
        is False
    )
    pages = acc.pages()
    assert len(pages) == 1
    assert pages[0]["id"] == "44444"
    assert pages[0]["id_status"] == "verified"


def test_merge_keeps_unseen_pages_and_deep_details():
    existing = [
        _page(
            1,
            created_date="2020-01-01",
            detail_synced=True,
            status="active",
            last_post_at_min="2026-11-01T00:00:00Z",
            last_post_at_max="2026-11-02T00:00:00Z",
        ),
        _page(2, created_date="2021-02-02"),
    ]
    observed = [_page(1, name="Page 1 renamed"), _page(3)]
    merged, retained = merge_discovery_pages(
        existing, observed, scan_id="scan-1", seen_at="2026-11-05T10:00:00Z"
    )
    assert len(merged) == 3
    assert retained == 1  # Page 2 vắng mặt vẫn giữ; chỉ 2 page thấy trong lượt.
    first = next(p for p in merged if p["id"] == "100001")
    assert first["name"] == "Page 1 renamed"
    assert first["created_date"] == "2020-01-01"
    assert first["status"] == "active"
    assert first["last_post_at_min"] == "2026-11-01T00:00:00Z"
    assert first["last_seen_at"] == "2026-11-05T10:00:00Z"
    assert first["seen_in_scan_id"] == "scan-1"


def test_older_scan_does_not_overwrite_newer_observation():
    existing = [
        _page(
            1,
            name="Newest name",
            last_seen_at="2026-11-05T12:00:00Z",
            seen_in_scan_id="scan-new",
        )
    ]
    merged, retained = merge_discovery_pages(
        existing,
        [_page(1, name="Old name")],
        scan_id="scan-old",
        seen_at="2026-11-05T10:00:00Z",
    )
    assert merged[0]["name"] == "Newest name"
    assert merged[0]["seen_in_scan_id"] == "scan-new"
    assert retained == 1


def test_ensure_contract_preserves_deep_fields_from_legacy_payload():
    page = _page(
        1,
        created_date="2020-01-01",
        created_raw="Ngày tạo trang: 01/01/2020",
        detail_synced=True,
        last_post_at_min="2026-11-01T00:00:00Z",
        last_post_at_max="2026-11-02T00:00:00Z",
        activity_scan_complete=True,
    )
    scan = ensure_scan_contract({"logged_in": True, "pages": [page]})
    assert scan["discovery_status"] == "partial"
    assert scan["completion_reason"] == "NO_END_SIGNAL"
    kept = scan["pages"][0]
    assert kept["created_date"] == "2020-01-01"
    assert kept["detail_synced"] is True
    assert kept["last_post_at_min"] == "2026-11-01T00:00:00Z"


def test_scan_detail_separates_observed_from_retained():
    partial = _scan(
        pages=[_page(i) for i in range(1, 13)],
    )
    partial["retained_count"] = 46
    message = scan_detail(partial, 58)
    assert "Đã đọc 12 page trong lượt này" in message
    assert "còn giữ 46 page từ cache" in message
    assert "chưa xác nhận đầy đủ" in message

    complete = _scan(
        pages=[_page(i) for i in range(1, 59)], total=58
    )
    assert scan_detail(complete, 58) == (
        "Đã đọc 58 page; kết thúc danh sách đã được xác nhận."
    )


def test_storage_partial_merge_keeps_cache_and_records_meta():
    manager = ProfileManager()
    profile = manager.create_profile(name="F4 Partial Merge")
    try:
        profile.facebook_pages = [
            _page(i, created_date="2020-01-01") for i in range(1, 59)
        ]
        profile.facebook_pages_synced_at = 1700000000
        manager.update_profile(profile)

        scan = _scan(pages=[_page(i) for i in range(1, 13)])
        result = manager.apply_facebook_scan(profile.id, scan)
        assert result is not None
        updated, stored_scan = result
        assert len(updated.facebook_pages) == 58
        assert stored_scan["observed_count"] == 12
        assert stored_scan["retained_count"] == 46
        # Page chưa thấy vẫn giữ chi tiết cũ.
        unseen = next(p for p in updated.facebook_pages if p["id"] == "100050")
        assert unseen["created_date"] == "2020-01-01"
        assert updated.facebook_pages_synced_at > 1700000000

        meta = manager.get_facebook_scan_meta(profile.id)
        assert meta["last_scan"]["discovery_status"] == "partial"
        assert meta["last_scan"]["observed_count"] == 12
        assert meta["last_scan"]["retained_count"] == 46
    finally:
        manager.delete_profile(profile.id)


def test_storage_failed_scan_never_touches_cache():
    manager = ProfileManager()
    profile = manager.create_profile(name="F4 Failed Scan")
    try:
        profile.facebook_pages = [_page(1), _page(2)]
        profile.facebook_pages_synced_at = 1700000000
        manager.update_profile(profile)
        revision = manager.get_profile(profile.id).revision

        failed = _scan(pages=[], logged_in=False, login_lost=True)
        updated, stored_scan = manager.apply_facebook_scan(profile.id, failed)
        assert [p["url"] for p in updated.facebook_pages] == [
            "https://www.facebook.com/page1",
            "https://www.facebook.com/page2",
        ]
        assert updated.facebook_pages_synced_at == 1700000000
        assert manager.get_profile(profile.id).revision == revision
        assert stored_scan["retained_count"] == 2
        meta = manager.get_facebook_scan_meta(profile.id)
        assert meta["last_scan"]["completion_reason"] == "LOGIN_LOST"
    finally:
        manager.delete_profile(profile.id)


def test_storage_older_scan_cannot_replace_newer_meta():
    manager = ProfileManager()
    profile = manager.create_profile(name="F4 Meta Order")
    try:
        newer = _scan(
            pages=[_page(1)],
            total=1,
            finished_at="2026-11-05T12:00:00Z",
        )
        manager.apply_facebook_scan(profile.id, newer)
        older = _scan(
            pages=[_page(2)],
            total=1,
            finished_at="2026-11-05T10:00:00Z",
        )
        manager.apply_facebook_scan(profile.id, older)
        meta = manager.get_facebook_scan_meta(profile.id)
        assert meta["last_scan"]["finished_at"] == "2026-11-05T12:00:00Z"
        assert meta["last_scan"]["scan_id"] == newer["scan_id"]
    finally:
        manager.delete_profile(profile.id)


def test_clear_cache_is_explicit_and_delete_removes_scan_meta():
    manager = ProfileManager()
    profile = manager.create_profile(name="F4 Clear Cache")
    try:
        profile.facebook_pages = [_page(1), _page(2)]
        manager.update_profile(profile)
        manager.apply_facebook_scan(profile.id, _scan(pages=[_page(1)]))
        assert (
            manager.get_facebook_scan_meta(profile.id)["last_scan"]["retained_count"]
            == 1
        )
        assert manager.clear_facebook_pages(profile.id) is not None
        assert manager.get_profile(profile.id).facebook_pages == []
        assert (
            manager.get_facebook_scan_meta(profile.id)["last_scan"]["retained_count"]
            == 0
        )
    finally:
        manager.delete_profile(profile.id)
    assert manager.get_facebook_scan_meta(profile.id) == {}


def test_apply_after_profile_deleted_does_not_recreate_it():
    manager = ProfileManager()
    profile = manager.create_profile(name="F4 Deleted Mid Scan")
    profile_id = profile.id
    manager.delete_profile(profile_id)
    assert manager.apply_facebook_scan(profile_id, _scan(pages=[_page(1)])) is None
    assert manager.get_profile(profile_id) is None


def test_scan_lock_does_not_block_lifecycle_lock():
    manager = ProfileManager()
    profile = manager.create_profile(name="F4 Lock Order")
    try:
        with manager.scan_lock(profile.id, timeout=0.5):
            # Stop/delete giữ lifecycle lock phải lấy được trong lúc scan chạy.
            with manager._profile_locks.acquire(profile.id, timeout=0.5):
                pass
        outcome = []

        def contender():
            try:
                with manager.scan_lock(profile.id, timeout=0.05):
                    outcome.append("acquired")
            except ProfileLockTimeout:
                outcome.append("timeout")

        with manager.scan_lock(profile.id, timeout=0.5):
            worker = threading.Thread(target=contender)
            worker.start()
            worker.join(2)
        assert outcome == ["timeout"]
    finally:
        manager.delete_profile(profile.id)
