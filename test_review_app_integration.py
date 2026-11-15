"""Read-only history and shutdown integration contracts."""
import asyncio
import sqlite3
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def isolated_app_import(tmp_path, monkeypatch):
    # Set before the first import: web.app constructs its default manager.
    monkeypatch.setenv("ANTIDETECT_DATA_DIR", str(tmp_path))


def test_accepted_history_shows_current_job_failure_without_starting_manager(tmp_path, monkeypatch):
    import web.app as api

    db = tmp_path / "fanpages.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sync_jobs (job_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO sync_jobs VALUES ('job-1', 'failed')")
    original = {"status": "accepted", "job_id": "job-1", "action": "sync_fanpages"}
    monkeypatch.setattr(api, "profile_manager", SimpleNamespace(data_dir=str(tmp_path)))
    monkeypatch.setattr(api, "_scheduler", SimpleNamespace(run_history=SimpleNamespace(recent=lambda _: [original])))
    monkeypatch.setattr(api, "get_fanpage_service", lambda: (_ for _ in ()).throw(AssertionError("must not start a worker")))
    result = api._schedule_history_with_jobs()
    assert result[0]["status"] == "accepted"
    assert result[0]["job_status"] == "failed"
    assert "job_status" not in original


def test_history_missing_database_does_not_create_it(tmp_path, monkeypatch):
    import web.app as api

    monkeypatch.setattr(api, "profile_manager", SimpleNamespace(data_dir=str(tmp_path)))
    monkeypatch.setattr(api, "_scheduler", SimpleNamespace(run_history=SimpleNamespace(recent=lambda _: [{"status": "accepted", "job_id": "job-1"}])))
    assert api._schedule_history_with_jobs()[0]["job_status"] == "unavailable"
    assert not (tmp_path / "fanpages.sqlite").exists()


def test_shutdown_stops_new_launches_before_draining_and_survives_job_error(monkeypatch):
    import web.app as api

    calls = []
    launcher = SimpleNamespace(
        begin_shutdown=lambda: calls.append("closing"),
        stop_owned=lambda: calls.append("stop-owned") or {"success": True},
    )
    def bad_drain(timeout):
        calls.append("drain")
        raise RuntimeError("fixture cleanup failure")
    monkeypatch.setattr(api, "launcher", launcher)
    monkeypatch.setattr(api, "_scheduler", SimpleNamespace(stop=lambda: calls.append("scheduler-stop")))
    monkeypatch.setattr(api, "_fanpage_service_cache", {"test": SimpleNamespace(jobs=SimpleNamespace(shutdown=bad_drain))})
    monkeypatch.setattr(api, "_start_all_cancel_event", SimpleNamespace(set=lambda: calls.append("bulk-cancel")))
    monkeypatch.setattr(api.app.state, "server", SimpleNamespace(should_exit=False), raising=False)
    result = asyncio.run(api.shutdown_system())
    assert result["success"]
    assert calls == ["closing", "scheduler-stop", "bulk-cancel", "drain", "stop-owned"]


def test_openapi_operation_ids_are_unique():
    import web.app as api

    schema = api.app.openapi()
    ids = [operation["operationId"] for path in schema["paths"].values()
           for method, operation in path.items() if method in ("get", "post", "put", "delete", "patch")]
    assert len(ids) == len(set(ids))
