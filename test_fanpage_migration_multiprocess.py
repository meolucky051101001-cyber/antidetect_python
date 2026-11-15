"""Upgrade/recovery and ownership checks using independent Windows processes."""
from datetime import datetime, timezone
import multiprocessing
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from antidetect.fanpages.repository import FanpageRepository, JobLeaseLost, SCHEMA_VERSION


# Original schema-v1 columns (260f3cc), deliberately independent of init_db().
# Optional aliases/links/alerts are created by the upgrader just as for an old DB.
V1_SCHEMA = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE pages (
 internal_id TEXT PRIMARY KEY, fb_page_id TEXT UNIQUE DEFAULT NULL,
 id_status TEXT NOT NULL DEFAULT 'url_only', name TEXT NOT NULL DEFAULT '',
 canonical_url TEXT NOT NULL DEFAULT '', avatar_url TEXT NOT NULL DEFAULT '',
 preferred_profile_id TEXT DEFAULT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
 note TEXT NOT NULL DEFAULT '', is_archived INTEGER NOT NULL DEFAULT 0,
 created_date TEXT DEFAULT NULL, created_raw TEXT DEFAULT NULL,
 followers_count INTEGER DEFAULT NULL, followers_raw TEXT DEFAULT NULL,
 followers_precision TEXT DEFAULT NULL, likes_count INTEGER DEFAULT NULL,
 likes_raw TEXT DEFAULT NULL, likes_precision TEXT DEFAULT NULL,
 last_post_raw TEXT DEFAULT NULL, last_post_at TEXT DEFAULT NULL,
 last_post_at_min TEXT DEFAULT NULL, last_post_at_max TEXT DEFAULT NULL,
 last_post_observed_at TEXT DEFAULT NULL, last_post_precision TEXT DEFAULT NULL,
 last_post_source TEXT DEFAULT NULL, last_post_url TEXT DEFAULT NULL,
 last_post_legacy INTEGER NOT NULL DEFAULT 0, last_post_days INTEGER DEFAULT NULL,
 detail_synced INTEGER NOT NULL DEFAULT 0, detail_synced_at TEXT DEFAULT NULL,
 activity_checked_at TEXT DEFAULT NULL, activity_scan_complete INTEGER DEFAULT NULL,
 activity_quality TEXT NOT NULL DEFAULT 'unknown', parser_version TEXT DEFAULT NULL,
 connection_status TEXT NOT NULL DEFAULT 'unknown',
 data_source TEXT NOT NULL DEFAULT 'legacy_cache', first_seen_at TEXT NOT NULL,
 last_seen_at TEXT DEFAULT NULL, last_synced_at INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE page_metric_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 page_id TEXT NOT NULL REFERENCES pages(internal_id) ON DELETE CASCADE,
 profile_id TEXT DEFAULT NULL, scan_id TEXT NOT NULL DEFAULT 'legacy',
 observed_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'legacy_cache',
 followers_count INTEGER DEFAULT NULL, followers_raw TEXT DEFAULT NULL,
 followers_precision TEXT DEFAULT NULL, likes_count INTEGER DEFAULT NULL,
 likes_raw TEXT DEFAULT NULL, likes_precision TEXT DEFAULT NULL,
 last_post_at_min TEXT DEFAULT NULL, last_post_at_max TEXT DEFAULT NULL,
 created_date TEXT DEFAULT NULL, UNIQUE(page_id, scan_id, observed_at)
);
CREATE TABLE sync_jobs (
 job_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'discovery',
 status TEXT NOT NULL DEFAULT 'queued', total_items INTEGER NOT NULL DEFAULT 0,
 completed_items INTEGER NOT NULL DEFAULT 0, failed_items INTEGER NOT NULL DEFAULT 0,
 partial_items INTEGER NOT NULL DEFAULT 0, skipped_items INTEGER NOT NULL DEFAULT 0,
 options_json TEXT NOT NULL DEFAULT '{}', owner_instance TEXT DEFAULT NULL,
 heartbeat_at TEXT DEFAULT NULL, created_at TEXT NOT NULL,
 started_at TEXT DEFAULT NULL, finished_at TEXT DEFAULT NULL, error TEXT DEFAULT NULL
);
CREATE TABLE sync_job_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 job_id TEXT NOT NULL REFERENCES sync_jobs(job_id) ON DELETE CASCADE,
 profile_id TEXT NOT NULL, profile_name TEXT NOT NULL DEFAULT '', page_id TEXT DEFAULT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 error_code TEXT DEFAULT NULL, error TEXT DEFAULT NULL, detail_json TEXT NOT NULL DEFAULT '{}',
 started_at TEXT DEFAULT NULL, finished_at TEXT DEFAULT NULL, updated_at TEXT NOT NULL
);
"""


def _seed_v1(path):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO schema_migrations VALUES(1, ?)", (now,))
        conn.execute("""INSERT INTO pages(internal_id, fb_page_id, name, canonical_url,
                     followers_count, followers_raw, followers_precision, note, tags_json,
                     first_seen_at, updated_at) VALUES ('pg_10000000001', '10000000001',
                     'Legacy page', 'https://www.facebook.com/10000000001',
                     1200, '1,2K', 'rounded', 'retain user note', '["saved-tag"]', ?, ?)""", (now, now))
        for index, source in enumerate(("legacy_cache", "live_scan")):
            conn.execute("""INSERT INTO page_metric_history(page_id, scan_id, observed_at,
                         source, followers_count, followers_raw, followers_precision)
                         VALUES ('pg_10000000001', ?, ?, ?, ?, ?, 'rounded')""",
                         (f"old-{index}", now, source, 1000 + index * 200, f"{1 + index / 5}K"))
        conn.execute("""INSERT INTO sync_jobs(job_id, status, owner_instance, heartbeat_at,
                     created_at, total_items, completed_items) VALUES ('legacy-job', 'running',
                     'legacy-owner', ?, ?, 2, 1)""", (now, now))
        conn.executemany("""INSERT INTO sync_job_items(job_id, profile_id, status, updated_at)
                         VALUES ('legacy-job', ?, ?, ?)""",
                         [("finished-profile", "completed", now), ("pending-profile", "pending", now)])


def _columns(path, table):
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _assert_v1_intact(path):
    assert "observation_meta_json" not in _columns(path, "pages")
    assert "execution_token" not in _columns(path, "sync_jobs")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute("SELECT followers_count, note FROM pages").fetchone() == (1200, "retain user note")
        assert conn.execute("SELECT count(*) FROM page_metric_history").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _assert_upgraded(path):
    assert "observation_meta_json" in _columns(path, "pages")
    assert {"execution_token", "owner_pid", "owner_created", "lease_expires_at", "cancel_requested_at", "idempotency_key"} <= _columns(path, "sync_jobs")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT followers_count, followers_raw, followers_precision, note, tags_json FROM pages").fetchone() == (1200, "1,2K", "rounded", "retain user note", '["saved-tag"]')
        assert conn.execute("SELECT count(*) FROM page_metric_history").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM sync_job_items WHERE status='completed'").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_original_v1_upgrade_is_idempotent_preserves_unknown_history(tmp_path):
    path = str(tmp_path / "old.sqlite")
    _seed_v1(path)
    _assert_v1_intact(path)
    repo = FanpageRepository(path)
    _assert_upgraded(path)
    detail = repo.get_page_detail("10000000001")
    assert detail["field_observations"] == {}
    FanpageRepository(path).init_db()
    _assert_upgraded(path)
    assert repo.get_sync_job("legacy-job")["status"] == "running"
    # Original fallback history is retained for audit, not relabelled verified.
    series = repo.get_page_metrics_series("10000000001", days=28)
    assert series["points"] == []


def test_migration_statement_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    path = str(tmp_path / "failed.sqlite")
    _seed_v1(path)
    original_connect = FanpageRepository._connect
    alters = []
    def failing_connect(repo):
        conn = original_connect(repo)
        def authorizer(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_ALTER_TABLE:
                alters.append(second)
                if len(alters) == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorizer)
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(FanpageRepository, "_connect", failing_connect)
        with pytest.raises(sqlite3.DatabaseError):
            FanpageRepository(path)
    assert len(alters) == 2
    _assert_v1_intact(path)
    FanpageRepository(path)
    _assert_upgraded(path)


def _constructor_worker(path, barrier, output):
    barrier.wait(15)
    repo = FanpageRepository(path)
    output.put(repo.get_page_detail("10000000001")["followers_count"])


def _finish_processes(processes):
    for process in processes:
        process.join(5)
        if process.is_alive():
            # These handles refer only to temporary fixture processes we created.
            process.terminate()
            process.join(5)


def test_two_processes_upgrade_the_same_v1_database(tmp_path):
    path = str(tmp_path / "concurrent.sqlite")
    _seed_v1(path)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(3), ctx.Queue()
    processes = [ctx.Process(target=_constructor_worker, args=(path, barrier, output)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        barrier.wait(15)
        assert sorted(output.get(timeout=20) for _ in processes) == [1200, 1200]
    finally:
        _finish_processes(processes)
        output.close()
    assert all(process.exitcode == 0 for process in processes)
    _assert_upgraded(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)).fetchone()[0] == 1


def _blocked_migration_worker(path, reached):
    original_connect = FanpageRepository._connect
    alters = []
    def blocked_connect(repo):
        conn = original_connect(repo)
        def authorizer(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_ALTER_TABLE:
                alters.append(second)
                if len(alters) == 2:
                    reached.set()
                    threading.Event().wait(30)
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorizer)
        return conn
    FanpageRepository._connect = blocked_connect
    FanpageRepository(path)


def test_killed_migrator_leaves_retryable_v1_database(tmp_path):
    path = str(tmp_path / "interrupted.sqlite")
    _seed_v1(path)
    ctx = multiprocessing.get_context("spawn")
    reached = ctx.Event()
    process = ctx.Process(target=_blocked_migration_worker, args=(path, reached))
    process.start()
    try:
        assert reached.wait(15), "migrator never reached injected crash boundary"
        process.terminate()
        process.join(10)
        assert not process.is_alive()
    finally:
        _finish_processes([process])
    _assert_v1_intact(path)
    FanpageRepository(path)
    _assert_upgraded(path)


def _owner_worker(path, pipe, marker):
    repo = FanpageRepository(path)
    job = repo.create_sync_job("owned-job", "discovery", [
        {"profile_id": "completed-profile"}, {"profile_id": "pending-profile"}],
        owner_instance="process-A", execution_token="token-A", lease_seconds=90)
    repo.update_sync_job_item(job["items"][0]["id"], job_id=job["id"],
                              status="completed", execution_token="token-A")
    repo.update_sync_job_status(job["id"], "running", execution_token="token-A")
    pipe.send("ready")
    if not pipe.poll(25):
        raise RuntimeError("parent did not release owner fixture")
    command = pipe.recv()
    if command == "publish":
        try:
            with repo.job_guard(job["id"], "token-A"):
                Path(marker).write_text("published by A", encoding="utf-8")
            pipe.send("published")
        except JobLeaseLost:
            pipe.send("fenced")
    pipe.close()


def _start_owner(tmp_path):
    path, marker = str(tmp_path / "jobs.sqlite"), str(tmp_path / "published.json")
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_owner_worker, args=(path, child, marker))
    process.start()
    child.close()
    try:
        assert parent.poll(15), "owner did not initialize"
        assert parent.recv() == "ready"
    except BaseException:
        _finish_processes([process])
        parent.close()
        raise
    return path, marker, process, parent


def test_real_owner_alive_is_preserved_then_dead_owner_is_recovered(tmp_path):
    path, marker, process, pipe = _start_owner(tmp_path)
    try:
        other = FanpageRepository(path)
        job = other.get_sync_job("owned-job")
        assert job["owner_pid"] == process.pid
        assert job["owner_created"] > 0
        assert process.is_alive()
        assert other.recover_interrupted_jobs(current_owner_instance="process-B") == 0
        assert other.get_sync_job("owned-job")["status"] == "running"
        pipe.send("exit")
        process.join(10)
        assert process.exitcode == 0
        assert other.recover_interrupted_jobs(current_owner_instance="process-B") == 1
        recovered = other.get_sync_job("owned-job")
        assert recovered["status"] == "interrupted"
        assert [item["status"] for item in recovered["items"]] == ["completed", "skipped"]
        assert not Path(marker).exists()
    finally:
        _finish_processes([process])
        pipe.close()


@pytest.mark.parametrize("loss", ["expired", "revoked", "replaced", "cancelled"])
def test_remote_owner_cannot_publish_after_losing_execution_rights(tmp_path, loss):
    path, marker, process, pipe = _start_owner(tmp_path)
    try:
        other = FanpageRepository(path)
        if loss == "revoked":
            other.revoke_job("owned-job", "token-A")
        elif loss == "cancelled":
            other.request_job_cancel("owned-job")
        elif loss == "expired":
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE sync_jobs SET lease_expires_at=0 WHERE job_id='owned-job'")
            assert other.recover_interrupted_jobs(current_owner_instance="process-B") == 1
        else:
            with other._job_locks.acquire("owned-job", timeout=5):
                with sqlite3.connect(path) as conn:
                    conn.execute("UPDATE sync_jobs SET execution_token='token-B', lease_expires_at=? WHERE job_id='owned-job'", (time.time() + 90,))
        assert process.is_alive()
        pipe.send("publish")
        assert pipe.poll(15)
        assert pipe.recv() == "fenced"
        assert not Path(marker).exists()
        assert other.get_sync_job("owned-job")["items"][0]["status"] == "completed"
    finally:
        _finish_processes([process])
        pipe.close()
    assert process.exitcode == 0
