"""R07 observation provenance regression tests; all storage is temporary."""
import datetime
import json

import pytest

from antidetect.facebook import merge_page_details
from antidetect.fanpage_scan import merge_discovery_pages
from antidetect.fanpages.repository import FanpageRepository


PAGE_ID = "1000555666777"
URL = "https://www.facebook.com/" + PAGE_ID
OBS = "2026-11-01T10:00:00Z"
NOW = datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc)


def payload(profile="p", **fields):
    return {"id": profile, "facebook_pages_synced_at": 100,
            "facebook_pages": [{"id": PAGE_ID, "name": "Page", "url": URL, **fields}]}


def observed(group, at=OBS):
    return {group: {"observed_at": at, "source": "test"}}


def test_legacy_history_stays_raw_but_never_contributes_to_growth(tmp_path):
    repo = FanpageRepository(str(tmp_path / "history.sqlite"))
    repo.sync_profiles_snapshot([payload(followers=20)], now_iso=NOW.isoformat())
    with repo._connect() as conn:
        for index, source in enumerate(("legacy_cache", "live_scan", "field_observation")):
            conn.execute("""INSERT INTO page_metric_history
                            (page_id, scan_id, observed_at, source, followers_count, followers_precision)
                            VALUES (?, ?, ?, ?, ?, 'exact')""",
                         ("pg_" + PAGE_ID, str(index), OBS, source, index * 10))
    detail = repo.get_page_detail(PAGE_ID)
    assert len(detail["metric_history"]) == 3
    series = repo.get_page_metrics_series(PAGE_ID, now=NOW)
    assert len(series["points"]) == 1
    assert series["followers_delta"] is None
    recent = repo.list_all_pages_with_links()[0]["recent_metric_points"]
    assert len(recent) == 1 and recent[0]["source"] == "field_observation"


def test_same_second_observations_from_two_profiles_do_not_collide(tmp_path):
    repo = FanpageRepository(str(tmp_path / "history.sqlite"))
    repo.sync_profiles_snapshot([
        payload("a", followers=100, field_observations=observed("followers")),
        payload("b", likes=50, field_observations=observed("likes")),
    ], now_iso=NOW.isoformat())
    history = repo.get_page_detail(PAGE_ID)["metric_history"]
    assert len(history) == 2
    assert {row["profile_id"] for row in history} == {"a", "b"}
    assert any(row["followers_count"] == 100 for row in history)
    assert any(row["likes_count"] == 50 for row in history)


def test_same_source_can_fill_missing_metric_without_duplicating_retained_cache(tmp_path):
    repo = FanpageRepository(str(tmp_path / "history.sqlite"))
    first = payload(followers=100, field_observations=observed("followers"), seen_in_scan_id="first")
    second = payload(likes=50, field_observations=observed("likes"), seen_in_scan_id="second")
    repo.sync_profiles_snapshot([first], now_iso=NOW.isoformat())
    repo.sync_profiles_snapshot([second], now_iso=NOW.isoformat())
    first["facebook_pages"][0]["seen_in_scan_id"] = "later-discovery"
    repo.sync_profiles_snapshot([first], now_iso=NOW.isoformat())
    history = repo.get_page_detail(PAGE_ID)["metric_history"]
    assert len(history) == 2
    points = repo.get_page_metrics_series(PAGE_ID, now=NOW)["points"]
    assert len(points) == 1
    assert (points[0]["followers_count"], points[0]["likes_count"]) == (100, 50)


@pytest.mark.parametrize("bad", [[], "bad", None, {"followers": []}, {"followers": {"legacy_synced_at": {}}}])
def test_malformed_observation_metadata_is_readable(tmp_path, bad):
    repo = FanpageRepository(str(tmp_path / "metadata.sqlite"))
    repo.sync_profiles_snapshot([payload(followers=100, field_observations=bad)], now_iso=NOW.isoformat())
    with repo._connect() as conn:
        conn.execute("UPDATE pages SET observation_meta_json=?", (json.dumps(bad),))
    repo.sync_profiles_snapshot([payload(followers=90, field_observations=observed("followers"))], now_iso=NOW.isoformat())
    assert repo.get_page_detail(PAGE_ID)["followers_count"] == 90
    assert isinstance(repo.list_all_pages_with_links()[0]["field_observations"], dict)


def test_new_raw_observation_replaces_retained_count_and_precision(tmp_path):
    page = {"id": PAGE_ID, "name": "Page", "url": URL,
            "followers": 100, "followers_count": 100, "followers_precision": "rounded",
            "field_observations": ["invalid"]}
    data = {"scan_id": "actual-scan", "pages": [page]}
    merge_page_details(data, {URL: {"observed_at": OBS, "followers_raw": "90"}}, clock=NOW)
    assert "followers_count" not in page and "followers_precision" not in page
    assert page["field_observations"]["followers"]["scan_id"] == "actual-scan"
    repo = FanpageRepository(str(tmp_path / "metric.sqlite"))
    repo.sync_profiles_snapshot([payload(**{k: v for k, v in page.items() if k not in ("id", "name", "url")})], now_iso=NOW.isoformat())
    result = repo.get_page_detail(PAGE_ID)
    assert (result["followers_count"], result["followers_raw"], result["followers_precision"]) == (90, "90", "exact")


def test_repository_keeps_rounded_raw_and_rejects_stale_parsed_count(tmp_path):
    repo = FanpageRepository(str(tmp_path / "metric.sqlite"))
    repo.sync_profiles_snapshot([payload(followers_count=999, followers_raw="1,2K", followers_precision="exact",
                                            field_observations=observed("followers"))], now_iso=NOW.isoformat())
    result = repo.get_page_detail(PAGE_ID)
    assert (result["followers_count"], result["followers_raw"], result["followers_precision"]) == (1200, "1,2K", "rounded")


@pytest.mark.parametrize("field", ["followers_raw", "followers", "likes_raw", "likes"])
def test_zero_only_detail_counts_as_synced(field):
    page = {"id": PAGE_ID, "url": URL}
    merge_page_details({"pages": [page]}, {URL: {"observed_at": OBS, field: 0}}, clock=NOW)
    assert page["detail_synced"] is True
    group = field.split("_")[0]
    assert page[group] == 0
    assert page["field_observations"][group]["observed_at"] == OBS


def test_cache_merge_removes_old_derived_metric_and_tolerates_bad_metadata():
    old = {"id": PAGE_ID, "url": URL, "followers_count": 100, "followers_precision": "rounded", "field_observations": []}
    new = {"id": PAGE_ID, "url": URL, "followers_raw": "90", "field_observations": observed("followers")}
    pages, _ = merge_discovery_pages([old], [new], scan_id="new", seen_at=OBS)
    assert "followers_count" not in pages[0] and "followers_precision" not in pages[0]
    assert pages[0]["field_observations"]["followers"]["observed_at"] == OBS


def test_future_observations_are_not_growth_points(tmp_path):
    repo = FanpageRepository(str(tmp_path / "future.sqlite"))
    future = "2099-01-01T00:00:00Z"
    repo.sync_profiles_snapshot([payload(followers=100, field_observations=observed("followers", future))], now_iso=NOW.isoformat())
    assert repo.get_page_detail(PAGE_ID)["metric_history"] == []
    with repo._connect() as conn:
        conn.execute("""INSERT INTO page_metric_history(page_id, scan_id, observed_at, source, followers_count)
                        VALUES (?, 'future-existing', ?, 'field_observation', 200)""", ("pg_" + PAGE_ID, future))
    assert repo.get_page_metrics_series(PAGE_ID, now=NOW)["points"] == []
    assert repo.list_all_pages_with_links()[0]["recent_metric_points"] == []
    assert len(repo.get_page_detail(PAGE_ID)["metric_history"]) == 1


def test_prune_does_not_delete_old_cancelling_job(tmp_path):
    repo = FanpageRepository(str(tmp_path / "jobs.sqlite"))
    repo.create_sync_job("cancel-job", "discovery", [{"profile_id": "p"}])
    with repo._connect() as conn:
        conn.execute("UPDATE sync_jobs SET status='cancelling', created_at='2020-01-01T00:00:00Z'")
    result = repo.prune_old_data(job_retention_days=1, now=NOW)
    assert result["jobs_deleted"] == 0
    assert repo.get_sync_job("cancel-job")["status"] == "cancelling"


def test_same_time_conflicts_have_one_deterministic_derived_point(tmp_path):
    baseline = payload("baseline", followers=50, likes=10,
                       field_observations={**observed("followers", "2026-10-31T10:00:00Z"),
                                           **observed("likes", "2026-10-31T10:00:00Z")})
    # Source profile z wins the tie. Insertion order must not choose the last
    # SQLite row or invent a decrease between equal-time observations.
    a = payload("a", followers=200, field_observations=observed("followers"))
    z = payload("z", followers=100, field_observations=observed("followers"))
    likes = payload("likes", likes=40, field_observations=observed("likes"))
    outcomes = []
    for index, order in enumerate(([a, z, likes], [likes, z, a])):
        repo = FanpageRepository(str(tmp_path / f"ties-{index}.sqlite"))
        repo.sync_profiles_snapshot([baseline, *order], now_iso=NOW.isoformat())
        catalog = repo.get_page_detail(PAGE_ID)
        assert catalog["followers_count"] == 100
        assert len(catalog["metric_history"]) == 4  # Raw evidence is retained.
        series = repo.get_page_metrics_series(PAGE_ID, now=NOW)
        recent = repo.list_all_pages_with_links(now=NOW)[0]["recent_metric_points"]
        assert len(series["points"]) == 2
        assert len(recent) == 2
        assert series["counter_decreased"] is False
        assert series["followers_delta"] == 50
        assert series["likes_delta"] == 30
        assert (recent[0]["followers_count"], recent[0]["likes_count"]) == (100, 40)
        outcomes.append(series["points"])
    assert outcomes[0] == outcomes[1]
