"""Offline release rehearsal with disposable data and real same-user Windows DPAPI.

This is a test fixture, not a production backup/restore command. Every application
read happens in a fresh child with ANTIDETECT_DATA_DIR set before its imports.
The child exits before snapshots are made, so there are no fixture writers alive.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parent
CORE_FILES = (
    "profiles.json", "secrets.dpapi", "api_token.dpapi",
    "schedules.json", "schedule_runs.json", "fanpages.sqlite",
)
EPHEMERAL_FILES = ("runtime.dpapi", "browser_runtime.json", "server_info.json")
PROFILE_IDS = ("restore-fixture-one", "restore-fixture-two")
# Deliberately fake credentials: never connect to a proxy or print these values.
FAKE_PASSWORDS = ("fixture-only:@/%+ mật khẩu A", "fixture-only:@/%+ mật khẩu B")
FAKE_TOKEN_HASH = hashlib.sha256(b"restore-fixture-only-api-token").hexdigest()
SENTINELS = {
    "user_data/Default/Bookmarks": b'{"fixture": "saved bookmarks"}\n',
    "user_data/Default/Preferences": b'{"fixture": "saved preferences"}\n',
    "user_data/Default/Local Storage/fixture.bin": b"\x00fixture-browser-data\xff",
}


def _require(condition, message):
    # Avoid pytest assertion introspection or child tracebacks exposing secrets.
    if not condition:
        raise AssertionError(message)


def _hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _durable_files(data):
    result = {name: data / name for name in CORE_FILES}
    for path in sorted((data / "profiles").rglob("*")):
        if path.is_file():
            result[path.relative_to(data).as_posix()] = path
    _require(all(path.is_file() for path in result.values()), "Missing durable fixture file")
    return result


def _file_manifest(data):
    return {name: _hash_file(path) for name, path in _durable_files(data).items()}


def _logical_db(path):
    """Read all rows, columns and user schema without triggering migration."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        _require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failure")
        _require(conn.execute("PRAGMA foreign_key_check").fetchall() == [], "SQLite foreign-key failure")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        content = {}
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")]
            rows = [list(row) for row in conn.execute(f"SELECT * FROM {quoted}")]
            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=True, sort_keys=True))
            content[name] = {"columns": columns, "rows": rows}
        schema = [list(row) for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )]
        return {"tables": content, "schema": schema}


def _snapshot(data, destination):
    """Fixture is offline; use SQLite backup API and verify both sides."""
    _require(not destination.exists(), "Snapshot destination must be new")
    before = _file_manifest(data)
    logical_before = _logical_db(data / "fanpages.sqlite")
    destination.mkdir()
    for name, source in _durable_files(data).items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "fanpages.sqlite":
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
        else:
            shutil.copy2(source, target)
    _require(_file_manifest(data) == before, "Source changed during offline snapshot")
    _require(_logical_db(destination / "fanpages.sqlite") == logical_before, "SQLite backup lost data")
    manifest = _file_manifest(destination)
    _require(all(manifest[name] == digest for name, digest in before.items()
                 if name != "fanpages.sqlite"), "File backup lost data")
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _restore(snapshot, destination):
    """Verify the complete fixture before creating a distinct restore directory."""
    _require(not destination.exists(), "Restore destination must be new")
    expected = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    _require(_file_manifest(snapshot) == expected, "Snapshot integrity mismatch")
    _logical_db(snapshot / "fanpages.sqlite")
    destination.mkdir()
    for name, source in _durable_files(snapshot).items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _require(_file_manifest(destination) == expected, "Restored file integrity mismatch")
    _require(not any((destination / name).exists() for name in EPHEMERAL_FILES),
             "Stale runtime state must not be restored")


def _child(data, action):
    env = os.environ.copy()
    env["ANTIDETECT_DATA_DIR"] = str(data.resolve())
    env["ANTIDETECT_LOG_CONSOLE"] = "0"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--fixture-worker", action],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    if result.returncode:
        try:
            failure = json.loads(result.stderr)
            reason = failure.get("check") or failure.get("error_type")
        except ValueError:
            reason = "child exited without sanitized diagnostic"
        raise AssertionError(f"Restore fixture child failed: {action}: {reason}")
    return json.loads(result.stdout)


def _seed(data):
    from antidetect.models import Profile, ProxyConfig
    from antidetect.runtime_auth import write_api_token_hash, write_runtime
    from antidetect.scheduler import ScheduleRunHistory, ScheduleStore
    from antidetect.storage import ProfileManager
    from test_fanpage_migration_multiprocess import _seed_v1

    manager = ProfileManager()
    for index, profile_id in enumerate(PROFILE_IDS):
        profile = Profile(
            id=profile_id, name=f"Khôi phục {index + 1}", notes="preserve fixture note",
            tags=["restore", "fixture"], created_at=1700000000, updated_at=1700000000,
            proxy=ProxyConfig(enabled=True, protocol="http", host="proxy.invalid", port=8080,
                              username=f"fixture-user-{index}", password=FAKE_PASSWORDS[index]),
            facebook_pages=[{"id": "10000000001", "name": "Legacy page", "followers": "1,2K"}],
        )
        manager.commit_staged_profile(profile)
        for relative, content in SENTINELS.items():
            path = data / "profiles" / profile_id / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    schedules = ScheduleStore(str(data / "schedules.json"))
    first = schedules.add("Fixture morning", "09:30", "start_all", profile_ids=[PROFILE_IDS[0]])
    schedules.add("Fixture disabled", "18:00", "stop_all", profile_ids=list(PROFILE_IDS), enabled=False)
    history = ScheduleRunHistory(str(data / "schedule_runs.json"))
    history.append({"run_id": "fixture-run-1", "schedule_id": first["id"], "status": "succeeded",
                    "profile_ids": [PROFILE_IDS[0]], "at": "2026-11-15T01:00:00Z"})
    history.append({"run_id": "fixture-run-2", "schedule_id": first["id"], "status": "failed",
                    "code": "FIXTURE_FAILURE", "at": "2026-11-15T02:00:00Z"})
    write_api_token_hash(FAKE_TOKEN_HASH)
    # Presence in the source makes exclusion of stale runtime state observable.
    write_runtime({"instance_id": "fixture-stale-instance", "pid": 99999999})
    (data / "browser_runtime.json").write_text('{"fixture": "stale-runtime"}', encoding="utf-8")
    (data / "server_info.json").write_text('{"fixture": "stale-server"}', encoding="utf-8")
    _seed_v1(data / "fanpages.sqlite")
    with closing(sqlite3.connect(data / "fanpages.sqlite")) as conn:
        # A completed job represents the required drained, offline checkpoint.
        conn.execute("UPDATE sync_jobs SET status='completed', owner_instance=NULL, heartbeat_at=NULL")
        conn.execute("UPDATE sync_job_items SET profile_id=? WHERE profile_id='finished-profile'", (PROFILE_IDS[0],))
        conn.execute("UPDATE sync_job_items SET profile_id=?, status='completed' WHERE profile_id='pending-profile'", (PROFILE_IDS[1],))
        conn.execute("UPDATE sync_jobs SET completed_items=total_items")
        conn.commit()
    return _verify_components(data, 1)


def _verify_components(data, version):
    from antidetect.runtime_auth import read_api_token_hash
    from antidetect.scheduler import ScheduleRunHistory, ScheduleStore
    from antidetect.secrets import get_proxy_password
    from antidetect.storage import ProfileManager

    manager = ProfileManager()
    profiles = manager.list_profiles()
    _require([profile.id for profile in profiles] == list(PROFILE_IDS), "Profile identities changed")
    raw = json.loads((data / "profiles.json").read_text(encoding="utf-8"))
    for index, profile_id in enumerate(PROFILE_IDS):
        profile = manager.get_profile(profile_id)
        _require(profile.name == f"Khôi phục {index + 1}" and profile.notes == "preserve fixture note",
                 "Profile text changed")
        _require(profile.tags == ["restore", "fixture"], "Profile tags changed")
        _require(profile.facebook_pages == [{"id": "10000000001", "name": "Legacy page", "followers": "1,2K"}],
                 "Profile Fanpage cache changed")
        _require(profile.proxy.password == FAKE_PASSWORDS[index], "DPAPI credential resolution failed")
        _require(get_proxy_password(profile_id, identity="mismatched-fixture-identity") is None,
                 "Proxy identity binding lost")
        _require(raw[index]["proxy"]["password"] is None, "Plaintext credential persisted")
        _require(FAKE_PASSWORDS[index].encode() not in (data / "secrets.dpapi").read_bytes(),
                 "Secret store contains plaintext")
        for relative, content in SENTINELS.items():
            _require((data / "profiles" / profile_id / relative).read_bytes() == content,
                     "Browser sentinel changed")
    _require(read_api_token_hash() == FAKE_TOKEN_HASH, "DPAPI API-token hash resolution failed")
    schedules = ScheduleStore(str(data / "schedules.json")).list()
    history = ScheduleRunHistory(str(data / "schedule_runs.json")).recent()
    _require(len(schedules) == 2 and schedules[0]["profile_ids"] == [PROFILE_IDS[0]]
             and schedules[1]["enabled"] is False, "Schedule content changed")
    _require([event["status"] for event in history] == ["succeeded", "failed"], "Run history changed")
    logical = _logical_db(data / "fanpages.sqlite")
    tables = logical["tables"]
    versions = [row[0] for row in tables["schema_migrations"]["rows"]]
    _require(max(versions) == version, "Unexpected SQLite schema version")
    _require(len(tables["pages"]["rows"]) == 1 and len(tables["page_metric_history"]["rows"]) == 2,
             "Fanpage/history rows changed")
    _require(len(tables["sync_jobs"]["rows"]) == 1 and len(tables["sync_job_items"]["rows"]) == 2,
             "Job rows changed")
    _require(("observation_meta_json" in tables["pages"]["columns"]) == (version == 3),
             "Observation schema mismatch")
    _require(("execution_token" in tables["sync_jobs"]["columns"]) == (version == 3),
             "Ownership schema mismatch")
    # Hash every logical value without printing profiles, credentials or absolute paths.
    return {
        "schema_version": version,
        "metadata_sha256": _hash_file(data / "profiles.json"),
        "schedule_sha256": _hash_file(data / "schedules.json"),
        "history_sha256": _hash_file(data / "schedule_runs.json"),
        "sqlite_logical_sha256": hashlib.sha256(json.dumps(logical, sort_keys=True).encode()).hexdigest(),
        "profiles": 2, "proxy_credentials_resolved": 2, "api_token_hash_resolved": True,
        "browser_sentinels": 6, "schedules": 2, "run_history_events": 2,
        "pages": 1, "metric_history_rows": 2, "jobs": 1, "job_items": 2,
    }


def _assert_original_columns_preserved(before, after):
    for name, table in before["tables"].items():
        if name == "schema_migrations":
            continue
        upgraded = after["tables"][name]
        positions = [upgraded["columns"].index(column) for column in table["columns"]]
        projection = [[row[index] for index in positions] for row in upgraded["rows"]]
        projection.sort(key=lambda row: json.dumps(row, ensure_ascii=True, sort_keys=True))
        _require(projection == table["rows"], f"Migration changed legacy content in {name}")


@pytest.mark.skipif(sys.platform != "win32", reason="Requires real Windows DPAPI under current user")
def test_offline_v1_v3_backup_restore_round_trip(tmp_path):
    started = time.monotonic()
    (tmp_path / ".restore-rehearsal-fixture").write_text("synthetic-data-only", encoding="utf-8")
    source = tmp_path / "source-v1"
    source.mkdir()
    original = _child(source, "seed")
    before_files = _file_manifest(source)
    before_db = _logical_db(source / "fanpages.sqlite")
    pre = tmp_path / "backup-before-upgrade"
    pre_manifest = _snapshot(source, pre)

    upgraded = tmp_path / "upgrade-candidate"
    _restore(pre, upgraded)
    first_upgrade = _child(upgraded, "migrate")
    upgraded_files = _file_manifest(upgraded)
    _require(all(upgraded_files[name] == digest for name, digest in pre_manifest.items()
                 if name != "fanpages.sqlite"), "Migration changed non-SQLite durable data")
    after_db = _logical_db(upgraded / "fanpages.sqlite")
    _assert_original_columns_preserved(before_db, after_db)
    second_upgrade = _child(upgraded, "migrate")
    _require(first_upgrade == second_upgrade, "Second migration changed component contents")
    _require(after_db == _logical_db(upgraded / "fanpages.sqlite"), "Second migration changed schema/data")
    post = tmp_path / "backup-after-upgrade"
    post_manifest = _snapshot(upgraded, post)

    restored_pre = tmp_path / "restore-pre-upgrade"
    _restore(pre, restored_pre)
    # No current FanpageRepository is instantiated: use the original v1 columns,
    # raw SQL and compatible JSON/DPAPI readers. Do not silently migrate rollback.
    restored_v1 = _child(restored_pre, "verify-v1")
    _require(restored_v1 == original, "Pre-upgrade restore differs from original")
    _require(_logical_db(restored_pre / "fanpages.sqlite") == before_db, "Rollback schema/data changed")
    _require(_file_manifest(restored_pre) == pre_manifest, "Rollback reader rewrote durable files")

    restored_post = tmp_path / "restore-post-upgrade"
    _restore(post, restored_post)
    restored_v3 = _child(restored_post, "verify-v3")
    _require(restored_v3 == first_upgrade, "Post-upgrade restore differs from upgraded copy")
    _require(_logical_db(restored_post / "fanpages.sqlite") == after_db, "Restored v3 schema/data changed")
    _require(_file_manifest(restored_post) == post_manifest, "Reopening v3 rewrote durable files")
    _require(_file_manifest(source) == before_files, "Original v1 fixture changed")
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "passed", "duration_seconds": round(time.monotonic() - started, 3),
        "test": "test_release_restore.py::test_offline_v1_v3_backup_restore_round_trip",
        "test_file_sha256": _hash_file(Path(__file__)),
        "python_version": sys.version.split()[0],
        "data": "synthetic fixture only; all data directories inside pytest temporary directory",
        "dpapi": "real Windows CryptProtectData/CryptUnprotectData; same current Windows user",
        "snapshot": "offline after fixture child exit; sqlite3.Connection.backup plus SHA-256 manifest",
        "migration": {"from": 1, "to": 3, "runs": 2, "idempotent": True,
                      "all_original_columns_and_rows_preserved": True},
        "restored": {"before_upgrade": restored_v1, "after_upgrade": restored_v3},
        "source_fixture_unchanged": True,
        "runtime_files_excluded": list(EPHEMERAL_FILES),
        "limitations": [
            "No production data or actual user credentials were accessed.",
            "Browser files are byte sentinels, not real Chrome cookies, sessions, or multi-GB profiles.",
            "Same-user DPAPI restore only; cross-user/machine/Windows-account recovery is not certified.",
            "Offline fixture has no concurrent writers; live coordinated backup was not exercised.",
            "Pre-upgrade restore uses legacy v1 SQL shape and compatible JSON/DPAPI readers, not an old full application binary.",
            "Manifest checks accidental corruption; it is not a signed or authenticated backup format.",
        ],
    }
    report_path = Path(os.environ.get("ANTIDETECT_RESTORE_REPORT", str(tmp_path / "restore-rehearsal.json")))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("damaged_file", [
    "profiles.json", "secrets.dpapi", "fanpages.sqlite",
    "profiles/fixture/user_data/Default/sentinel.bin",
])
def test_restore_refuses_damaged_snapshot_before_creating_destination(tmp_path, damaged_file):
    source = tmp_path / "synthetic-source"
    source.mkdir()
    for name in CORE_FILES:
        (source / name).write_bytes(b"fixture")
    (source / "fanpages.sqlite").unlink()
    with closing(sqlite3.connect(source / "fanpages.sqlite")) as conn:
        conn.execute("CREATE TABLE fixture(value TEXT)")
        conn.execute("INSERT INTO fixture VALUES ('persisted')")
        conn.commit()
    sentinel = source / "profiles/fixture/user_data/Default/sentinel.bin"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"fixture-browser-sentinel")
    backup = tmp_path / "snapshot"
    _snapshot(source, backup)
    with (backup / damaged_file).open("ab") as stream:
        stream.write(b"damaged")
    destination = tmp_path / "must-not-exist"
    with pytest.raises(AssertionError, match="Snapshot integrity mismatch"):
        _restore(backup, destination)
    assert not destination.exists()


def _worker_main(action):
    # The caller provides the environment before this file imports app modules.
    data = Path(os.environ["ANTIDETECT_DATA_DIR"]).resolve()
    _require(data.is_relative_to(Path(tempfile.gettempdir()).resolve()), "Worker requires temporary data")
    _require((data.parent / ".restore-rehearsal-fixture").is_file(), "Worker requires rehearsal fixture marker")
    _require(data.is_dir(), "Fixture data directory must already exist")
    if action == "seed":
        return _seed(data)
    if action in ("migrate", "verify-v3"):
        from antidetect.fanpages.repository import FanpageRepository, SCHEMA_VERSION
        _require(SCHEMA_VERSION == 3, "Update rehearsal for a new schema version")
        FanpageRepository(str(data / "fanpages.sqlite"))
        return _verify_components(data, 3)
    if action == "verify-v1":
        return _verify_components(data, 1)
    raise ValueError("Unknown fixture action")


if __name__ == "__main__":
    try:
        _require(len(sys.argv) == 3 and sys.argv[1] == "--fixture-worker", "Fixture worker only")
        print(json.dumps(_worker_main(sys.argv[2]), sort_keys=True))
    except Exception as error:
        # Worker failure output is intentionally scrubbed; pytest reports action.
        print(json.dumps({"error_type": type(error).__name__,
                          "check": str(error) if isinstance(error, AssertionError) else None}), file=sys.stderr)
        raise SystemExit(1) from None
