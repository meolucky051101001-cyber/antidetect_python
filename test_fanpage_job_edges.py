"""Regression coverage for replay, unexpected workers, and cleanup outcomes."""
import pytest

from test_review_fanpage_scheduler import make_manager


def test_explicit_empty_targets_never_expand_to_current_profiles(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path)
    assert jobs.resolve_target_profiles(profile_ids=[]) == []
    with pytest.raises(ValueError, match="Không có profile"):
        jobs.enqueue_job(profile_ids=[], auto_start=True, run_async=False)
    launcher.launch.assert_not_called()
    assert jobs.repo.list_sync_jobs() == []


def test_occurrence_replay_returns_original_after_profile_deleted(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path)
    first, _ = jobs.enqueue_job(auto_start=True, run_async=False, idempotency_key="schedule:1")
    manager.list_profiles.side_effect = AssertionError("Replay must not re-resolve targets")
    again, deduplicated = jobs.enqueue_job(
        profile_ids=[], auto_start=True, run_async=False, idempotency_key="schedule:1",
    )
    assert deduplicated
    assert again["id"] == first["id"]
    assert again["status"] == "completed"
    launcher.fetch_facebook_pages.assert_called_once()


def test_unexpected_profile_lookup_failure_marks_item_and_job_failed(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path)
    manager.get_profile.side_effect = RuntimeError("lookup failed")
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "failed"
    assert job["items"][0]["status"] == "failed"
    assert job["items"][0]["error_code"] == "WORKER_ERROR"
    assert "lookup failed" in job["items"][0]["error"]


def test_occurrence_cannot_accept_active_job_with_different_key(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path)
    existing = jobs.repo.create_sync_job(
        "unrelated", "discovery", [{"profile_id": "p"}],
        options={"mode": "discovery", "auto_start": True, "only_running": False,
                 "profile_ids": ["p"], "page_ids": []},
        execution_token="other-token", owner_instance="other", idempotency_key="other-key",
    )
    with pytest.raises(RuntimeError, match="FANPAGE_JOB_BUSY"):
        jobs.enqueue_job(auto_start=True, run_async=False, idempotency_key="schedule:1")
    jobs.repo.update_sync_job_status(existing["id"], "completed", execution_token="other-token")
    created, deduplicated = jobs.enqueue_job(
        auto_start=True, run_async=False, idempotency_key="schedule:1",
    )
    assert not deduplicated
    assert created["id"] != existing["id"]
    assert created["idempotency_key"] == "schedule:1"


def test_returning_worker_without_terminal_result_cannot_complete_job(tmp_path, monkeypatch):
    jobs, manager, launcher = make_manager(tmp_path)
    monkeypatch.setattr(jobs, "_execute_profile_item", lambda **kwargs: None)
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "failed"
    assert job["items"][0]["error_code"] == "WORKER_INCOMPLETE"


@pytest.mark.parametrize("raises", [False, True])
def test_cleanup_failure_preserves_scan_and_reports_warning(tmp_path, raises):
    jobs, manager, launcher = make_manager(tmp_path, launched_now=True)
    if raises:
        launcher.stop.side_effect = RuntimeError("fixture stop failure")
    else:
        launcher.stop.return_value = {"success": False, "code": "STOP_FAILED", "error": "still running"}
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "completed_with_warnings"
    item = job["items"][0]
    assert item["status"] == "completed"
    assert item["detail"]["discovery_status"] == "complete"
    assert item["detail"]["cleanup"]["status"] == "failed"
    assert item["detail"]["cleanup"]["launch_id"] == "generation-A"
    manager.apply_facebook_scan.assert_called_once()


def test_replaced_session_is_benign_cleanup_outcome(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path, launched_now=True)
    launcher.stop.return_value = {"success": False, "code": "SESSION_CHANGED"}
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "completed"
    assert job["items"][0]["detail"]["cleanup"]["status"] == "session_changed"


def test_revoked_worker_cleanup_cannot_overwrite_persisted_result(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path, launched_now=True)

    def stop(*args, **kwargs):
        active = jobs.repo.find_active_sync_job()
        jobs.repo.revoke_job(active["id"], active["execution_token"])
        return {"success": False, "code": "STOP_FAILED"}

    launcher.stop.side_effect = stop
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "interrupted"
    assert "cleanup" not in job["items"][0]["detail"]


def test_remote_cancel_at_cleanup_is_reflected_in_final_job(tmp_path):
    jobs, manager, launcher = make_manager(tmp_path, launched_now=True)

    def stop(*args, **kwargs):
        active = jobs.repo.find_active_sync_job()
        jobs.repo.request_job_cancel(active["id"])
        return {"success": True}

    launcher.stop.side_effect = stop
    job, _ = jobs.enqueue_job(auto_start=True, run_async=False)
    assert job["status"] == "cancelled"
