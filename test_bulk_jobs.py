"""P2-12: bulk job state dùng chung giữa nhiều instance server."""
import json
import threading
import time

import pytest

from antidetect.bulk_jobs import (
    BulkJobHeartbeat,
    BulkJobStore,
    BulkJobStoreCorruption,
)


def _store(tmp_path, stale_seconds=90.0):
    return BulkJobStore(str(tmp_path / "bulk_jobs.json"), stale_seconds=stale_seconds)


def test_live_job_blocks_other_instances(tmp_path):
    store = _store(tmp_path)
    ok, job = store.try_claim("job-a", "instance-a", "start_all", total=3)
    assert ok and job["job_id"] == "job-a"
    ok_other, existing = store.try_claim(
        "job-b", "instance-b", "start_all", total=3
    )
    assert ok_other is False
    assert existing["job_id"] == "job-a"
    assert existing["owner_instance"] == "instance-a"
    assert store.read()["job_id"] == "job-a"


def test_same_owner_replaces_live_job(tmp_path):
    store = _store(tmp_path)
    assert store.try_claim("job-a", "instance-a", "start_all", 1)[0]
    ok, job = store.try_claim("job-b", "instance-a", "scheduled_start_all", 1)
    assert ok and job["job_id"] == "job-b"
    assert job["kind"] == "scheduled_start_all"


def test_stale_lease_can_be_taken_over(tmp_path):
    path = tmp_path / "bulk_jobs.json"
    store = BulkJobStore(str(path), stale_seconds=1.0)
    assert store.try_claim("job-a", "instance-a", "start_all", 1)[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["current"]["heartbeat_at"] -= 60
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.is_active(store.read()) is False
    ok, job = store.try_claim("job-b", "instance-b", "start_all", 1)
    assert ok and job["job_id"] == "job-b"


def test_update_cancel_finish_lifecycle(tmp_path):
    store = _store(tmp_path)
    assert store.try_claim(
        "job-a", "instance-a", "start_all", 2,
        starting_count=1, max_concurrent=4,
    )[0]
    updated = store.update(
        "job-a", "instance-a",
        completed_count=1,
        results=[{"profile_id": "p1", "result": {"success": True}}],
    )
    assert updated["completed_count"] == 1
    assert updated["max_concurrent"] == 4
    assert store.heartbeat("job-a", "instance-a") is not None
    assert store.update("job-a", "instance-b", completed_count=9) is None
    assert store.heartbeat("job-a", "instance-b") is None
    cancelling = store.request_cancel("job-a")
    assert cancelling["status"] == "cancelling"
    assert cancelling["cancel_requested"] is True
    assert store.request_cancel("job-b") is None
    assert store.finish("job-a", "instance-a", "cancelled") is True
    finished = store.read()
    assert finished["status"] == "cancelled"
    assert finished["cancel_requested"] is False
    assert store.update("job-a", "instance-a", completed_count=99) is None
    assert store.finish("job-a", "instance-a", "completed") is False


def test_results_are_capped(tmp_path):
    store = _store(tmp_path)
    assert store.try_claim("job-a", "instance-a", "start_all", 1)[0]
    results = [{"profile_id": f"p{i}", "result": {"success": True}} for i in range(600)]
    updated = store.update("job-a", "instance-a", results=results)
    assert len(updated["results"]) == 500
    assert updated["results"][-1]["profile_id"] == "p599"


def test_heartbeat_renews_lease_and_surfaces_cancel(tmp_path):
    store = _store(tmp_path, stale_seconds=1.0)
    assert store.try_claim("job-a", "instance-a", "start_all", 1)[0]
    initial = store.read()["heartbeat_at"]
    cancel_event = threading.Event()
    beat = BulkJobHeartbeat(
        store, "job-a", "instance-a", cancel_event, interval=0.05
    ).start()
    try:
        time.sleep(0.15)
        assert store.read()["heartbeat_at"] > initial
        assert cancel_event.is_set() is False
        assert store.request_cancel("job-a") is not None
        assert cancel_event.wait(1.0)
    finally:
        beat.stop()
    assert store.finish("job-a", "instance-a", "cancelled")


def test_corrupt_store_fails_closed(tmp_path):
    path = tmp_path / "bulk_jobs.json"
    path.write_text("{not json", encoding="utf-8")
    store = BulkJobStore(str(path))
    with pytest.raises(BulkJobStoreCorruption):
        store.read()
    with pytest.raises(BulkJobStoreCorruption):
        store.try_claim("job-a", "instance-a", "start_all", 1)


def _client():
    from fastapi.testclient import TestClient

    import web.app as app_module

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    return client, app_module


def _wait_no_local_job(app_module, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app_module._start_all_lock:
            job = app_module._active_start_all_job
            if job is None or job.get("status") not in ("running", "cancelling"):
                return
        time.sleep(0.02)
    raise AssertionError("Local Start-All job did not finish in time")


def test_api_reports_and_cancels_remote_job():
    client, app_module = _client()
    _wait_no_local_job(app_module)
    remote = BulkJobStore(app_module._bulk_job_store.path)
    assert remote.try_claim("remote-job", "instance-remote", "start_all", 5)[0]
    try:
        status = client.get("/api/profiles/start-all/status").json()
        assert status["active"] is True
        assert status["job"]["job_id"] == "remote-job"
        assert status["job"]["remote"] is True
        cancel = client.post("/api/profiles/start-all/cancel").json()
        assert cancel["success"] is True
        assert cancel["remote"] is True
        assert cancel["job_id"] == "remote-job"
        assert remote.read()["status"] == "cancelling"
    finally:
        remote.finish("remote-job", "instance-remote", "cancelled")
        remote.clear()


def test_api_marks_stale_remote_job(tmp_path):
    client, app_module = _client()
    _wait_no_local_job(app_module)
    remote = BulkJobStore(app_module._bulk_job_store.path)
    assert remote.try_claim("stale-job", "instance-remote", "start_all", 1)[0]
    try:
        path = app_module._bulk_job_store.path
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["current"]["heartbeat_at"] -= 600
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        status = client.get("/api/profiles/start-all/status").json()
        assert status["active"] is False
        assert status["job"]["job_id"] == "stale-job"
        assert status["job"]["stale"] is True
    finally:
        remote.finish("stale-job", "instance-remote", "failed", error="test")
        remote.clear()


def test_api_rejects_start_all_when_remote_job_is_live():
    client, app_module = _client()
    _wait_no_local_job(app_module)
    profile = client.post("/api/profiles", json={"name": "P2-12 slot"}).json()
    remote = BulkJobStore(app_module._bulk_job_store.path)
    assert remote.try_claim("remote-job", "instance-remote", "start_all", 1)[0]
    try:
        r = client.post(
            "/api/profiles/start-all", json={"profile_ids": [profile["id"]]}
        )
        assert r.status_code == 409, r.text
        assert "remote-job" in r.json()["detail"]
        assert remote.read()["job_id"] == "remote-job"
    finally:
        remote.finish("remote-job", "instance-remote", "cancelled")
        remote.clear()
        client.delete(f"/api/profiles/{profile['id']}")


def test_scheduler_skips_when_slot_busy(monkeypatch):
    import web.app as app_module

    calls = []
    monkeypatch.setattr(
        app_module.launcher, "launch_all", lambda **kw: calls.append(kw) or []
    )
    remote = BulkJobStore(app_module._bulk_job_store.path)
    assert remote.try_claim("remote-job", "instance-remote", "start_all", 1)[0]
    try:
        result = app_module._scheduler_start_all([])
        assert result["skipped"] is True
        assert result["reason"] == "remote_bulk_job"
        assert calls == []
    finally:
        remote.finish("remote-job", "instance-remote", "cancelled")
        remote.clear()


def test_scheduler_claims_slot_and_finishes_job(monkeypatch):
    import web.app as app_module

    _wait_no_local_job(app_module)
    captured = {}

    def fake_launch_all(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(app_module.launcher, "launch_all", fake_launch_all)
    store = BulkJobStore(app_module._bulk_job_store.path)
    store.clear()
    try:
        result = app_module._scheduler_start_all(["profile-a"])
        assert result["success"] is True
        assert captured["profile_ids"] == ["profile-a"]
        job = store.read()
        assert job["job_id"] == result["job_id"]
        assert job["kind"] == "scheduled_start_all"
        assert job["status"] == "completed"
        with app_module._start_all_lock:
            local = dict(app_module._active_start_all_job)
        assert local["job_id"] == result["job_id"]
        assert local["status"] == "completed"
    finally:
        store.clear()


def test_stale_owner_cannot_update_or_finish_after_takeover(tmp_path):
    path = tmp_path / "bulk_jobs.json"
    store = BulkJobStore(str(path), stale_seconds=1.0)
    assert store.try_claim("job-a", "instance-a", "start_all", 2)[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["current"]["heartbeat_at"] -= 60
    path.write_text(json.dumps(data), encoding="utf-8")

    assert store.try_claim("job-b", "instance-b", "start_all", 2)[0]
    assert store.update("job-a", "instance-a", completed_count=9) is None
    assert store.heartbeat("job-a", "instance-a") is None
    assert store.finish("job-a", "instance-a", "completed") is False

    current = store.read()
    assert current["job_id"] == "job-b"
    assert current["owner_instance"] == "instance-b"
    assert current["status"] == "running"
    assert current["completed_count"] == 0


def test_record_bulk_progress_counts_25_profiles():
    import web.app as app_module

    _wait_no_local_job(app_module)
    store = BulkJobStore(app_module._bulk_job_store.path)
    store.clear()
    job_id = "local-25"
    owner = app_module.app.state.instance_id
    assert store.try_claim(job_id, owner, "start_all", total=25)[0]
    try:
        with app_module._start_all_lock:
            app_module._active_start_all_job = {
                "job_id": job_id,
                "status": "running",
                "completed_count": 0,
                "failed_count": 0,
                "results": [],
            }
        for i in range(25):
            app_module._record_bulk_progress(
                job_id, f"p{i}", {"success": i % 5 != 0}
            )
        with app_module._start_all_lock:
            local = dict(app_module._active_start_all_job)
        assert local["completed_count"] == 20
        assert local["failed_count"] == 5
        assert len(local["results"]) == 25

        remote = store.read()
        assert remote["completed_count"] == 20
        assert remote["failed_count"] == 5
        assert len(remote["results"]) == 25
        assert remote["total"] == 25
    finally:
        with app_module._start_all_lock:
            app_module._active_start_all_job = None
        store.clear()


def test_scheduler_heartbeat_start_failure_finishes_job_and_releases_lock(monkeypatch):
    import web.app as app_module

    _wait_no_local_job(app_module)
    store = BulkJobStore(app_module._bulk_job_store.path)
    store.clear()
    launched = []

    def fail_start(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(app_module.BulkJobHeartbeat, "start", fail_start)
    monkeypatch.setattr(
        app_module.launcher,
        "launch_all",
        lambda **kwargs: launched.append(kwargs) or [],
    )
    try:
        with pytest.raises(RuntimeError, match="thread start failed"):
            app_module._scheduler_start_all(["profile-a"])
        assert launched == []
        assert app_module._bulk_launch_lock.locked() is False

        job = store.read()
        assert job is not None
        assert job["status"] == "failed"
        assert "thread start failed" in job["error"]
        with app_module._start_all_lock:
            local = dict(app_module._active_start_all_job)
        assert local["job_id"] == job["job_id"]
        assert local["status"] == "failed"
    finally:
        store.clear()
        with app_module._start_all_lock:
            app_module._active_start_all_job = None
        if app_module._bulk_launch_lock.locked():
            app_module._bulk_launch_lock.release()
