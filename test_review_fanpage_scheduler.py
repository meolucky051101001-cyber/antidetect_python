"""Regression coverage for review R04, R07, R08 and R09, using only temp data."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import multiprocessing
import threading
import time

import pytest

from antidetect.fanpages.jobs import FanpageSyncJobManager
from antidetect.fanpages.repository import FanpageRepository, JobLeaseLost
from antidetect.scheduler import ActionResult, ScheduleStore, Scheduler, normalize_action_result


def snapshot(profile, followers, when, *, synced=1, likes=None, scan="s1"):
    page = {"id": "1000111222333", "url": "https://www.facebook.com/1000111222333",
            "name": "Page", "followers": followers, "seen_in_scan_id": scan,
            "field_observations": {"followers": {"observed_at": when, "source": "test"}}}
    if likes is not None:
        page["likes"] = likes
    return {"id": profile, "facebook_pages_synced_at": synced, "facebook_pages": [page]}


def test_catalog_freshness_is_order_independent_and_accepts_decrease(tmp_path):
    old = snapshot("old", 1000, "2026-11-01T09:00:00Z", synced=999)
    new = snapshot("new", 2000, "2026-11-01T10:00:00Z", synced=1)
    for index, order in enumerate(([old, new], [new, old])):
        repo = FanpageRepository(str(tmp_path / f"{index}.db"))
        repo.sync_profiles_snapshot(order, now_iso="2026-11-02T00:00:00Z")
        detail = repo.get_page_detail("1000111222333")
        assert detail["followers_count"] == 2000
        assert detail["field_observations"]["followers"]["observed_at"] == "2026-11-01T10:00:00Z"
        repo.sync_profiles_snapshot([snapshot("new", 0, "2026-11-01T11:00:00Z")], now_iso="2026-11-02T00:00:00Z")
        assert repo.get_page_detail("1000111222333")["followers_count"] == 0


def test_retained_metric_does_not_create_history_on_new_discovery(tmp_path):
    repo = FanpageRepository(str(tmp_path / "catalog.db"))
    original = snapshot("a", 2000, "2026-11-01T10:00:00Z", scan="first")
    repo.sync_profiles_snapshot([original], now_iso="2026-11-02T00:00:00Z")
    original["facebook_pages_synced_at"] = 12345
    original["facebook_pages"][0]["seen_in_scan_id"] = "later-discovery"
    repo.sync_profiles_snapshot([original], now_iso="2026-11-03T00:00:00Z")
    detail = repo.get_page_detail("1000111222333")
    assert len(detail["metric_history"]) == 1
    assert detail["field_observations"]["followers"]["observed_at"] == "2026-11-01T10:00:00Z"


def test_metric_groups_have_independent_observation_times(tmp_path):
    repo = FanpageRepository(str(tmp_path / "catalog.db"))
    first = snapshot("a", 2000, "2026-11-01T10:00:00Z", likes=50)
    first["facebook_pages"][0]["field_observations"]["likes"] = {"observed_at": "2026-11-01T09:00:00Z"}
    second = snapshot("b", 1000, "2026-11-01T09:00:00Z", likes=60)
    second["facebook_pages"][0]["field_observations"]["likes"] = {"observed_at": "2026-11-01T11:00:00Z"}
    repo.sync_profiles_snapshot([first, second], now_iso="2026-11-02T00:00:00Z")
    detail = repo.get_page_detail("1000111222333")
    assert (detail["followers_count"], detail["likes_count"]) == (2000, 60)


def test_legacy_snapshot_ordered_without_fabricating_observation(tmp_path):
    repo = FanpageRepository(str(tmp_path / "catalog.db"))
    newer = snapshot("a", 2000, None, synced=20)
    older = snapshot("b", 1000, None, synced=10)
    repo.sync_profiles_snapshot([newer, older])
    detail = repo.get_page_detail("1000111222333")
    assert detail["followers_count"] == 2000
    assert detail["field_observations"]["followers"]["observed_at"] is None
    assert detail["metric_history"] == []


def create_job(repo, *, token="token", job_id="job-one", force_new=True, key=None):
    return repo.create_sync_job(job_id, "discovery", [{"profile_id": "p"}], owner_instance="owner-A",
                                execution_token=token, force_new=force_new, idempotency_key=key)


def test_live_owner_not_recovered_and_expired_worker_cannot_write(tmp_path):
    repo = FanpageRepository(str(tmp_path / "jobs.db"))
    job = create_job(repo)
    assert repo.recover_interrupted_jobs(current_owner_instance="owner-B") == 0
    with repo._connect() as conn:
        conn.execute("UPDATE sync_jobs SET lease_expires_at=0 WHERE job_id=?", (job["id"],))
    assert repo.recover_interrupted_jobs(current_owner_instance="owner-B") == 1
    with pytest.raises(JobLeaseLost):
        repo.update_sync_job_item(job["items"][0]["id"], job_id=job["id"], status="completed", execution_token="token")
    assert repo.get_sync_job(job["id"])["status"] == "interrupted"


def test_job_updates_require_correct_token_and_cancel_fences_publish(tmp_path):
    repo = FanpageRepository(str(tmp_path / "jobs.db"))
    job = create_job(repo)
    for wrong in (None, "wrong"):
        with pytest.raises(JobLeaseLost):
            repo.update_sync_job_status(job["id"], "completed", execution_token=wrong)
    repo.request_job_cancel(job["id"])
    with pytest.raises(JobLeaseLost):
        with repo.job_guard(job["id"], "token"):
            pytest.fail("Cancelled job may not publish")
    repo.update_sync_job_status(job["id"], "cancelled", execution_token="token")
    assert repo.get_sync_job(job["id"])["status"] == "cancelled"


def test_idempotent_enqueue_returns_completed_job_after_restart(tmp_path):
    path = str(tmp_path / "jobs.db")
    repo = FanpageRepository(path)
    job = create_job(repo, key="occurrence-1")
    repo.update_sync_job_status(job["id"], "completed", execution_token="token")
    again = create_job(FanpageRepository(path), token="new-token", job_id="replacement", key="occurrence-1")
    assert again["id"] == job["id"]
    assert again["status"] == "completed"


def _enqueue_process(path, start, output, number):
    repo = FanpageRepository(path)
    start.wait(10)
    job = create_job(repo, token=f"token-{number}", job_id=f"job-{number}", force_new=False)
    output.put(job["id"])


def test_enqueue_dedup_is_atomic_across_processes(tmp_path):
    path = str(tmp_path / "jobs.db")
    FanpageRepository(path)
    ctx = multiprocessing.get_context("spawn")
    start, output = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_enqueue_process, args=(path, start, output, i)) for i in range(2)]
    for process in processes:
        process.start()
    try:
        start.set()
        ids = [output.get(timeout=15) for _ in processes]
        assert len(set(ids)) == 1
    finally:
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        output.close()
    assert all(process.exitcode == 0 for process in processes)


def make_manager(tmp_path, *, launched_now=False):
    profile = SimpleNamespace(id="p", name="Profile", facebook_pages=[])
    manager = MagicMock()
    manager.list_profiles.return_value = [profile]
    manager.get_profile.return_value = profile
    manager.scan_lock.side_effect = lambda *a, **kw: nullcontext()
    manager.get_facebook_scan_meta.return_value = {}
    manager.apply_facebook_scan.return_value = None
    launcher = MagicMock()
    launcher.is_running.return_value = False
    launcher.launch.return_value = {"success": True, "launched_now": launched_now, "launch_id": "generation-A"}
    launcher.stop.return_value = {"success": True}
    launcher.fetch_facebook_pages.return_value = {"discovery_status": "complete", "login_status": True, "pages": [], "completion_reason": "END_OF_LIST"}
    repo = FanpageRepository(str(tmp_path / "jobs.db"))
    jobs = FanpageSyncJobManager(repo=repo, profile_manager=manager, launcher=launcher, heartbeat_interval=.03, lease_seconds=.15)
    return jobs, manager, launcher


@pytest.mark.parametrize("launched_now", [False, True])
def test_job_cleanup_uses_launch_ownership_and_generation(tmp_path, launched_now):
    jobs, manager, launcher = make_manager(tmp_path, launched_now=launched_now)
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "completed"
    if launched_now:
        launcher.stop.assert_called_once_with("p", expected_launch_id="generation-A")
    else:
        launcher.stop.assert_not_called()


def test_slow_scan_heartbeat_and_remote_cancel_prevent_publish(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def scan(*_args):
        entered.set()
        assert release.wait(5)
        return {"discovery_status": "complete", "login_status": True, "pages": []}
    launcher.fetch_facebook_pages.side_effect = scan
    job, _ = jobs.enqueue_job(auto_start=True)
    try:
        assert entered.wait(3)
        # Real heartbeat runs independently of the blocked network scan.
        time.sleep(.22)
        assert jobs.repo.recover_interrupted_jobs(current_owner_instance="B") == 0
        other_repo = FanpageRepository(jobs.repo.db_path)
        other_repo.request_job_cancel(job["id"])
        release.set()
        jobs.shutdown(5)
        manager.apply_facebook_scan.assert_not_called()
        assert jobs.repo.get_sync_job(job["id"])["status"] == "cancelled"
    finally:
        release.set()
        jobs.shutdown(5)


def test_shutdown_deadline_revokes_late_worker(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def scan(*_args):
        entered.set()
        release.wait(5)
        return {"discovery_status": "complete", "login_status": True, "pages": []}
    launcher.fetch_facebook_pages.side_effect = scan
    job, _ = jobs.enqueue_job(auto_start=True)
    assert entered.wait(3)
    try:
        jobs.shutdown(0)
        assert jobs.repo.get_sync_job(job["id"])["status"] == "interrupted"
        release.set()
        jobs.shutdown(5)
        manager.apply_facebook_scan.assert_not_called()
    finally:
        release.set()


def test_sync_bulk_job_id_does_not_hide_failures():
    result = normalize_action_result({"job_id": "bulk", "success": False, "results": [
        {"profile_id": "good", "result": {"success": True}},
        {"profile_id": "bad", "result": {"success": False, "code": "PROFILE_BUSY"}},
    ]})
    assert result.status == "partial"
    assert result.retryable_profile_ids == ["bad"]
    assert normalize_action_result({"job_id": "queued", "deduplicated": False}).status == "accepted"
    assert normalize_action_result({"job_id": "busy", "skipped": True, "reason": "remote_bulk_job"}).status == "skipped"


def test_explicit_nonretryable_overrides_code_and_none_is_not_success():
    result = normalize_action_result({"success": False, "results": {"bad": {"success": False, "code": "PROFILE_BUSY", "retryable": False}}})
    assert not result.retryable
    assert normalize_action_result(None).status == "failed"


def test_scheduler_budget_survives_restart_and_does_not_complete_failure(tmp_path):
    path = str(tmp_path / "schedules.json")
    now = datetime.now().replace(second=0, microsecond=0)
    store = ScheduleStore(path)
    rule = store.add("rule", now.strftime("%H:%M"), "stop_all")
    for attempt, seconds in enumerate((0, 30, 150), 1):
        store = ScheduleStore(path)
        current = now + timedelta(seconds=seconds)
        assert store.claim_due(rule["id"], current, 60, str(attempt))
        state = store.finish_result(rule["id"], str(attempt), ActionResult("failed", "NETWORK_ERROR", retryable=True), current)
        assert state["attempts"] == attempt
    assert state["terminal"] is True
    assert store.list()[0]["last_run_date"] is None
    assert store.claim_due(rule["id"], now + timedelta(minutes=5), 60, "fourth") is None


def test_scheduler_partial_retry_keeps_terminal_failures(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    now = datetime.now().replace(second=0, microsecond=0)
    rule = store.add("rule", now.strftime("%H:%M"), "start_all", ["good", "busy", "invalid"])
    store.claim_due(rule["id"], now, 60, "1")
    first = normalize_action_result({"success": False, "results": {
        "good": {"success": True}, "busy": {"success": False, "code": "PROFILE_BUSY"},
        "invalid": {"success": False, "code": "INVALID_PROXY_CONFIG"}}})
    store.finish_result(rule["id"], "1", first, now)
    claimed, _ = store.claim_due(rule["id"], now + timedelta(seconds=30), 60, "2")
    assert claimed["_occurrence"]["profile_ids"] == ["busy"]
    second = normalize_action_result({"success": True, "results": {"busy": {"success": True}}})
    final = store.finish_result(rule["id"], "2", second, now + timedelta(seconds=30))
    assert final["status"] == "partial"
    assert final["terminal_failed_profile_ids"] == ["invalid"]
    assert final["succeeded_profile_ids"] == ["busy", "good"]


def test_runner_records_false_return_as_failure(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    store.add("failure", datetime.now().strftime("%H:%M"), "stop_all")
    called = threading.Event()
    def callback(_ids):
        called.set()
        return {"success": False}
    runner = Scheduler(store, {"stop_all": callback}, check_interval=.01)
    runner.start()
    try:
        assert called.wait(3)
    finally:
        runner.stop()
        runner.join(3)
    assert runner.last_run_info["ran"][0]["status"] == "failed"
    assert store.list()[0]["last_run_date"] is None
