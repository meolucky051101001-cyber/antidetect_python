"""Independent regression tests for durable scheduler outcomes."""
from datetime import datetime, timedelta

import pytest

from antidetect.scheduler import ActionResult, ScheduleStore, normalize_action_result


def test_prior_successes_remain_partial_when_retry_is_exhausted(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "start_all", ["good", "busy"])
    store.claim_due(rule["id"], now, 60, "first")
    first = normalize_action_result({"success": False, "results": {
        "good": {"success": True}, "busy": {"success": False, "code": "PROFILE_BUSY"},
    }})
    store.finish_result(rule["id"], "first", first, now)
    for token, delay in (("second", 30), ("third", 150)):
        when = now + timedelta(seconds=delay)
        claimed, _ = store.claim_due(rule["id"], when, 60, token)
        assert claimed["_occurrence"]["profile_ids"] == ["busy"]
        failure = normalize_action_result({"success": False, "results": {"busy": {"success": False, "code": "PROFILE_BUSY"}}})
        state = store.finish_result(rule["id"], token, failure, when)
    assert state["terminal"] is True
    assert state["status"] == "partial"
    assert state["succeeded_profile_ids"] == ["good"]
    assert state["terminal_failed_profile_ids"] == ["busy"]


@pytest.mark.parametrize("status", ["failed", "partial", "cancelled"])
def test_explicit_failed_job_result_is_never_accepted(status):
    result = normalize_action_result({"status": status, "job_id": "job"})
    assert result.status in ("failed", "partial")


def test_stale_claim_cannot_finish_new_occurrence(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "stop_all", ["a"])
    first, _ = store.claim_due(rule["id"], now, 60, "first", lease_seconds=1)
    store.update(rule["id"], profile_ids=["b"])
    second, _ = store.claim_due(rule["id"], now, 60, "second", lease_seconds=1)
    assert first["_occurrence"]["key"] != second["_occurrence"]["key"]
    assert store.finish_result(rule["id"], "first", normalize_action_result({"success": True}), now) is None
    assert store.list()[0]["_occurrence"]["profile_ids"] == ["b"]


def test_all_target_snapshot_survives_restart_and_new_profiles(tmp_path):
    path = str(tmp_path / "schedules.json")
    store = ScheduleStore(path)
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "stop_all")
    first, _ = store.claim_due(rule["id"], now, 60, "first", resolved_profile_ids=["a", "b"])
    assert first["_occurrence"]["profile_ids"] == ["a", "b"]
    store.finish_result(rule["id"], "first", ActionResult("failed", "NETWORK_ERROR", retryable=True), now)
    second, _ = ScheduleStore(path).claim_due(rule["id"], now + timedelta(seconds=30), 60, "second", resolved_profile_ids=["a", "b", "new"])
    assert second["_occurrence"]["profile_ids"] == ["a", "b"]


def test_empty_resolved_target_stays_empty(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "stop_all")
    first, _ = store.claim_due(rule["id"], now, 60, "first", resolved_profile_ids=[])
    assert first["_occurrence"]["targets_resolved"] is True
    store.finish_result(rule["id"], "first", ActionResult("failed", "NETWORK_ERROR", retryable=True), now)
    second, _ = store.claim_due(rule["id"], now + timedelta(seconds=30), 60, "second", resolved_profile_ids=["new"])
    assert second["_occurrence"]["profile_ids"] == []


def test_abandoned_third_attempt_becomes_terminal_without_fourth_callback(tmp_path):
    path = str(tmp_path / "schedules.json")
    store = ScheduleStore(path)
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "stop_all", ["p"])
    for number in range(3):
        assert store.claim_due(rule["id"], now + timedelta(seconds=number * 2), 60, str(number), lease_seconds=1)
    # Restart after third callback was claimed; its outcome is unknown.
    store = ScheduleStore(path)
    events = store.expire_occurrences(now + timedelta(seconds=6), lease_seconds=1)
    assert len(events) == 1
    state = store.list()[0]["_occurrence"]
    assert state["terminal"] is True
    assert state["status"] == "failed"
    assert state["outcome_unknown"] is True
    assert state["attempts"] == 3
    assert store.expire_occurrences(now + timedelta(seconds=7), lease_seconds=1) == []
    assert store.claim_due(rule["id"], now + timedelta(seconds=8), 60, "fourth") is None


def test_expired_catchup_terminalizes_pending_retry_but_not_live_callback(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "stop_all", ["p"])
    store.claim_due(rule["id"], now, 60, "first")
    store.finish_result(rule["id"], "first", ActionResult("failed", "NETWORK_ERROR", retryable=True), now)
    events = store.expire_occurrences(now + timedelta(minutes=61))
    assert events[0]["error"] == "CATCHUP_WINDOW_EXPIRED"
    assert store.list()[0]["_occurrence"]["terminal"] is True
    other = store.add("long", "10:00", "stop_all", ["other"])
    store.claim_due(other["id"], now + timedelta(minutes=59), 60, "live", lease_seconds=600)
    assert store.expire_occurrences(now + timedelta(minutes=61)) == []
    assert not next(r for r in store.list() if r["id"] == other["id"])["_occurrence"]["terminal"]


def test_accepted_requires_job_and_explicit_failure_wins():
    assert normalize_action_result(ActionResult("accepted")).status == "failed"
    assert normalize_action_result({"job_id": "job", "status": "accepted", "success": False}).status == "failed"
    assert normalize_action_result({"success": True, "synced": 0, "skipped": 2, "total": 2}).status == "skipped"


@pytest.mark.parametrize("has_success", [False, True])
@pytest.mark.parametrize("has_terminal_failure", [False, True])
def test_busy_retries_preserve_previous_profile_outcomes(tmp_path, has_success, has_terminal_failure):
    path = str(tmp_path / "schedules.json")
    store = ScheduleStore(path)
    now = datetime(2026, 11, 15, 10, 0)
    results = {"busy": {"success": False, "code": "PROFILE_BUSY"}}
    if has_success:
        results["good"] = {"success": True}
    if has_terminal_failure:
        results["invalid"] = {"success": False, "code": "INVALID_PROXY_CONFIG"}
    rule = store.add("run", "10:00", "start_all", list(results))
    store.claim_due(rule["id"], now, 60, "first")
    store.finish_result(rule["id"], "first", normalize_action_result({"success": False, "results": results}), now)
    for token, seconds in (("second", 30), ("third", 150)):
        store = ScheduleStore(path)
        current = now + timedelta(seconds=seconds)
        claimed, _ = store.claim_due(rule["id"], current, 60, token)
        assert claimed["_occurrence"]["profile_ids"] == ["busy"]
        busy = normalize_action_result({"success": False, "skipped": True, "reason": "bulk_busy"})
        state = store.finish_result(rule["id"], token, busy, current)
        assert state["status"] == ("partial" if has_success else "failed")
        assert busy.status == state["status"]  # History uses this result too.
        assert "busy" in state["failed_profile_ids"]
    expected_failed = ["busy", "invalid"] if has_terminal_failure else ["busy"]
    assert state["terminal"] is True
    assert state["error"] == "bulk_busy"
    assert state["terminal_failed_profile_ids"] == expected_failed
    assert state["succeeded_profile_ids"] == (["good"] if has_success else [])


def test_only_busy_attempts_remain_skipped_without_invented_failures(tmp_path):
    store = ScheduleStore(str(tmp_path / "schedules.json"))
    now = datetime(2026, 11, 15, 10, 0)
    rule = store.add("run", "10:00", "start_all", ["a"])
    for attempt, seconds in enumerate((0, 30, 150)):
        current = now + timedelta(seconds=seconds)
        token = str(attempt)
        store.claim_due(rule["id"], current, 60, token)
        state = store.finish_result(rule["id"], token, normalize_action_result({"skipped": True, "reason": "bulk_busy"}), current)
    assert state["terminal"] is True
    assert state["status"] == "skipped"
    assert state["terminal_failed_profile_ids"] == []
