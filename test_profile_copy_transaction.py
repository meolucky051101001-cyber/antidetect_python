"""F2 regressions: duplicate publishes only a complete staged copy."""
import os
import threading
import time
import uuid

import pytest

import antidetect.storage as storage
from antidetect.models import ProxyConfig
from antidetect.profile_copy import (
    CopyJournal,
    CopyNoSpaceError,
    ProfileCopyError,
    journal_path,
    read_operation_result,
    remove_operation_files,
    result_path,
)
from antidetect.storage import DATA_DIR, PROFILES_DATA_DIR, ProfileManager


def _source(manager, name="Copy Source", with_data=True, with_secret=False):
    proxy = ProxyConfig(
        enabled=True,
        protocol="http",
        host="127.0.0.1",
        port=8080,
        username="tester",
    )
    profile = manager.create_profile(name=name, proxy=proxy)
    if with_data:
        user_data = os.path.join(PROFILES_DATA_DIR, profile.id, "user_data")
        os.makedirs(os.path.join(user_data, "Default"), exist_ok=True)
        with open(os.path.join(user_data, "Default", "Cookies"), "w", encoding="utf-8") as f:
            f.write("session-data")
        with open(os.path.join(user_data, "Local State"), "w", encoding="utf-8") as f:
            f.write("local-state")
    if with_secret:
        storage.set_proxy_password(
            profile.id, "source-secret", identity=profile.proxy.identity_key()
        )
    return profile


def _cleanup(manager, *profiles):
    for profile in profiles:
        if profile is None:
            continue
        try:
            manager.delete_profile(profile.id)
        except Exception:
            pass


def test_copy_data_publishes_complete_target_and_preserves_source_secret():
    manager = ProfileManager()
    source = _source(manager, with_secret=True)
    target = None
    operation_id = "op-success-0001"
    try:
        target = manager.duplicate_profile(
            source.id,
            "Bản sao hoàn chỉnh",
            copy_data=True,
            operation_id=operation_id,
            source_running=lambda _pid: False,
        )
        assert target is not None
        assert target.id != source.id

        source_user_data = os.path.join(PROFILES_DATA_DIR, source.id, "user_data")
        target_user_data = os.path.join(PROFILES_DATA_DIR, target.id, "user_data")
        assert os.path.isfile(os.path.join(target_user_data, "Default", "Cookies"))
        assert os.path.isfile(os.path.join(target_user_data, "Local State"))
        assert os.path.getsize(os.path.join(target_user_data, "Local State")) == len(
            "local-state"
        )
        assert os.path.isfile(os.path.join(source_user_data, "Default", "Cookies"))

        fetched = manager.get_profile(target.id)
        assert fetched is not None
        assert fetched.proxy.password == "source-secret"
        assert storage.get_proxy_password(
            source.id, identity=source.proxy.identity_key()
        ) == "source-secret"
        assert storage.get_proxy_password(
            target.id, identity=fetched.proxy.identity_key()
        ) == "source-secret"

        result = read_operation_result(DATA_DIR, operation_id)
        assert result["target_id"] == target.id
        assert result["source_id"] == source.id
        assert "password" not in result["profile"].get("proxy", {})
        assert os.path.exists(result_path(DATA_DIR, operation_id))
        assert os.path.exists(journal_path(DATA_DIR, operation_id))
        with open(journal_path(DATA_DIR, operation_id), "r", encoding="utf-8") as f:
            import json

            assert json.load(f)["phase"] == "committed"
    finally:
        _cleanup(manager, target, source)
        remove_operation_files(DATA_DIR, operation_id)


def test_copy_failure_publishes_nothing_and_cleans_staging(monkeypatch):
    manager = ProfileManager()
    source = _source(manager)
    operation_id = "op-failure-0001"

    def explode(_source_dir, _staging_dir):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(storage, "copy_user_data", explode)
    before = len(manager.list_profiles())
    try:
        with pytest.raises(ProfileCopyError) as exc:
            manager.duplicate_profile(
                source.id,
                copy_data=True,
                operation_id=operation_id,
                source_running=lambda _pid: False,
            )
        assert exc.value.code == "COPY_FAILED"
        assert len(manager.list_profiles()) == before
        staging_root = os.path.join(PROFILES_DATA_DIR, ".staging", operation_id)
        assert not os.path.exists(staging_root)
        assert not os.path.exists(journal_path(DATA_DIR, operation_id))
        assert not os.path.exists(result_path(DATA_DIR, operation_id))
    finally:
        _cleanup(manager, source)


def test_no_space_fails_before_staging_or_metadata(monkeypatch):
    manager = ProfileManager()
    source = _source(manager)
    operation_id = "op-nospace-0001"

    def no_space(*_args, **_kwargs):
        raise CopyNoSpaceError(free=10, required=1024)

    monkeypatch.setattr(storage, "check_disk_space", no_space)
    before = len(manager.list_profiles())
    try:
        with pytest.raises(CopyNoSpaceError) as exc:
            manager.duplicate_profile(
                source.id,
                copy_data=True,
                operation_id=operation_id,
                source_running=lambda _pid: False,
            )
        assert exc.value.code == "COPY_NO_SPACE"
        assert len(manager.list_profiles()) == before
        assert not os.path.exists(
            os.path.join(PROFILES_DATA_DIR, ".staging", operation_id)
        )
        assert not os.path.exists(journal_path(DATA_DIR, operation_id))
    finally:
        _cleanup(manager, source)


def test_source_running_rejects_before_journal_is_written():
    manager = ProfileManager()
    source = _source(manager)
    operation_id = "op-running-0001"
    before = len(manager.list_profiles())
    try:
        with pytest.raises(ProfileCopyError) as exc:
            manager.duplicate_profile(
                source.id,
                copy_data=True,
                operation_id=operation_id,
                source_running=lambda _pid: True,
            )
        assert exc.value.code == "SOURCE_RUNNING"
        assert not os.path.exists(journal_path(DATA_DIR, operation_id))
        assert len(manager.list_profiles()) == before
    finally:
        _cleanup(manager, source)


def test_config_only_copy_skips_source_stop_and_data_copy():
    manager = ProfileManager()
    source = _source(manager, with_secret=True)
    target = None

    def must_not_run(_pid):
        raise AssertionError("copy_data=False must not probe source liveness")

    try:
        target = manager.duplicate_profile(
            source.id,
            "Cấu hình",
            copy_data=False,
            operation_id="op-config-0001",
            source_running=must_not_run,
        )
        assert target is not None
        target_dir = os.path.join(PROFILES_DATA_DIR, target.id)
        assert not os.path.exists(os.path.join(target_dir, "user_data"))
        assert storage.get_proxy_password(
            target.id, identity=target.proxy.identity_key()
        ) == "source-secret"
    finally:
        _cleanup(manager, target, source)
        remove_operation_files(DATA_DIR, "op-config-0001")


def test_retry_with_same_operation_id_returns_one_target():
    manager = ProfileManager()
    source = _source(manager, with_data=False)
    target = None
    operation_id = "op-idem-0001"
    before = len(manager.list_profiles())
    try:
        first = manager.duplicate_profile(
            source.id, copy_data=False, operation_id=operation_id
        )
        second = manager.duplicate_profile(
            source.id, copy_data=False, operation_id=operation_id
        )
        assert first is not None and second is not None
        assert first.id == second.id
        assert len(manager.list_profiles()) == before + 1
        target = first

        other = manager.create_profile(name="Other source")
        try:
            with pytest.raises(ProfileCopyError) as exc:
                manager.duplicate_profile(
                    other.id, copy_data=False, operation_id=operation_id
                )
            assert exc.value.code == "COPY_OPERATION_CONFLICT"
        finally:
            _cleanup(manager, other)
    finally:
        _cleanup(manager, target, source)
        remove_operation_files(DATA_DIR, operation_id)


def test_busy_source_lifecycle_lock_returns_profile_busy():
    manager = ProfileManager()
    source = _source(manager, with_data=False)
    before = len(manager.list_profiles())
    ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        with manager._profile_locks.acquire(source.id):
            ready.set()
            release.wait(5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert ready.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(ProfileCopyError) as exc:
            manager.duplicate_profile(
                source.id,
                copy_data=False,
                operation_id="op-busy-0001",
                lock_timeout=0.1,
            )
        assert exc.value.code == "PROFILE_BUSY"
        assert time.monotonic() - started < 2
        assert len(manager.list_profiles()) == before
    finally:
        release.set()
        holder.join(5)
        _cleanup(manager, source)


def test_commit_failure_removes_promoted_target_and_secret(monkeypatch):
    manager = ProfileManager()
    source = _source(manager, with_secret=True)
    captured = {}

    def fail_commit(profile):
        captured["target_id"] = profile.id
        raise RuntimeError("metadata replace failed")

    monkeypatch.setattr(manager, "commit_staged_profile", fail_commit)
    try:
        with pytest.raises(ProfileCopyError) as exc:
            manager.duplicate_profile(
                source.id,
                copy_data=True,
                operation_id="op-commit-0001",
                source_running=lambda _pid: False,
            )
        assert exc.value.code == "COMMIT_FAILED"
        target_id = captured["target_id"]
        assert manager.get_profile(target_id) is None
        assert not os.path.exists(os.path.join(PROFILES_DATA_DIR, target_id))
        assert storage.get_proxy_password(
            target_id, identity=source.proxy.identity_key()
        ) is None
        assert not os.path.exists(journal_path(DATA_DIR, "op-commit-0001"))
    finally:
        _cleanup(manager, source)


def test_recovery_cleans_orphan_promoted_operation():
    manager = ProfileManager()
    source = _source(manager)
    target_id = str(uuid.uuid4())
    operation_id = "op-recover-orphan"
    target_dir = os.path.join(PROFILES_DATA_DIR, target_id)
    staging_root = os.path.join(PROFILES_DATA_DIR, ".staging", operation_id)
    try:
        os.makedirs(os.path.join(target_dir, "user_data"), exist_ok=True)
        with open(os.path.join(target_dir, "user_data", "partial"), "w") as f:
            f.write("partial")
        os.makedirs(os.path.join(staging_root, "user_data"), exist_ok=True)
        writer = CopyJournal(
            journal_path(DATA_DIR, operation_id),
            operation_id=operation_id,
            source_id=source.id,
            target_id=target_id,
            owner="2147483647:1",
            source_dir=os.path.join(PROFILES_DATA_DIR, source.id),
            staging_dir=os.path.join(staging_root, "user_data"),
            target_dir=os.path.join(target_dir, "user_data"),
        )
        writer.write("promoted", copy_data=True)

        handled = manager.recover_copy_operations(lock_timeout=0.2)
        assert handled == 1
        assert not os.path.exists(target_dir)
        assert not os.path.exists(staging_root)
        assert not os.path.exists(journal_path(DATA_DIR, operation_id))
        assert manager.get_profile(target_id) is None
    finally:
        remove_operation_files(DATA_DIR, operation_id)
        _cleanup(manager, source)


def test_recovery_finalizes_metadata_committed_but_journal_stale():
    manager = ProfileManager()
    source = _source(manager)
    target = manager.create_profile(name="Recovered target")
    operation_id = "op-recover-committed"
    try:
        writer = CopyJournal(
            journal_path(DATA_DIR, operation_id),
            operation_id=operation_id,
            source_id=source.id,
            target_id=target.id,
            owner="2147483647:1",
            source_dir=os.path.join(PROFILES_DATA_DIR, source.id),
            staging_dir=os.path.join(
                PROFILES_DATA_DIR, ".staging", operation_id, "user_data"
            ),
            target_dir=os.path.join(PROFILES_DATA_DIR, target.id, "user_data"),
        )
        writer.write("secret_written", copy_data=False)

        handled = manager.recover_copy_operations(lock_timeout=0.2)
        assert handled == 1
        assert manager.get_profile(target.id) is not None
        result = read_operation_result(DATA_DIR, operation_id)
        assert result["target_id"] == target.id
        with open(journal_path(DATA_DIR, operation_id), "r", encoding="utf-8") as f:
            import json

            assert json.load(f)["phase"] == "committed"
    finally:
        remove_operation_files(DATA_DIR, operation_id)
        _cleanup(manager, target, source)


def test_launch_and_delete_report_busy_while_copy_holds_lock():
    from antidetect.launcher import BrowserLauncher
    from antidetect.storage import ProfileBusyError

    manager = ProfileManager()
    source = _source(manager, with_data=False)
    launcher = BrowserLauncher(manager)
    ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        with manager._profile_locks.acquire(source.id):
            ready.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert ready.wait(5)
    try:
        launched = launcher.launch(source.id)
        assert launched["success"] is False
        assert launched["code"] == "PROFILE_BUSY"
        with pytest.raises(ProfileBusyError) as exc:
            manager.delete_profile(source.id)
        assert exc.value.code == "PROFILE_BUSY"
        assert manager.get_profile(source.id) is not None
    finally:
        release.set()
        holder.join(5)
        launcher.unregister_cleanup()
        _cleanup(manager, source)


def test_copy_does_not_block_metadata_for_other_profiles(monkeypatch):
    manager = ProfileManager()
    source = _source(manager)
    other = manager.create_profile(name="Other")
    target = None
    started = threading.Event()
    release = threading.Event()
    real_copy = storage.copy_user_data
    outcome = {}

    def slow_copy(source_dir, staging_dir):
        started.set()
        assert release.wait(5)
        return real_copy(source_dir, staging_dir)

    monkeypatch.setattr(storage, "copy_user_data", slow_copy)

    def run_copy():
        try:
            outcome["profile"] = manager.duplicate_profile(
                source.id,
                copy_data=True,
                operation_id="op-slow-0001",
                source_running=lambda _pid: False,
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertions
            outcome["error"] = exc

    worker = threading.Thread(target=run_copy, daemon=True)
    worker.start()
    try:
        assert started.wait(5)
        # Neither list nor update may wait for the GB copy.
        profiles = manager.list_profiles()
        assert any(p.id == other.id for p in profiles)
        edited = manager.get_profile(other.id)
        edited.notes = "edited while copying"
        assert manager.update_profile(edited) is not None
        assert manager.get_profile(other.id).notes == "edited while copying"
    finally:
        release.set()
        worker.join(10)
        target = outcome.get("profile")
        _cleanup(manager, target, other, source)
        remove_operation_files(DATA_DIR, "op-slow-0001")

    assert "error" not in outcome
    assert outcome["profile"] is not None


def _api_client():
    import web.app as app_module
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    client.post(
        "/auth/login",
        headers={"Authorization": "Bearer " + app_module.app.state.access_key},
    )
    return app_module, client


def test_duplicate_api_returns_operation_header_and_is_idempotent(monkeypatch):
    app_module, client = _api_client()
    source_id = client.post("/api/profiles", json={"name": "API Copy"}).json()["id"]
    target_id = None
    try:
        headers = {"Idempotency-Key": "api-idem-0001"}
        first = client.post(
            f"/api/profiles/{source_id}/duplicate", headers=headers
        )
        assert first.status_code == 200
        assert first.headers["x-operation-id"] == "api-idem-0001"
        target_id = first.json()["id"]
        assert target_id != source_id

        second = client.post(
            f"/api/profiles/{source_id}/duplicate", headers=headers
        )
        assert second.status_code == 200
        assert second.json()["id"] == target_id
        profiles = client.get("/api/profiles").json()
        assert len([p for p in profiles if p["id"] in (source_id, target_id)]) == 2
    finally:
        if target_id:
            client.delete(f"/api/profiles/{target_id}")
        client.delete(f"/api/profiles/{source_id}")
        remove_operation_files(DATA_DIR, "api-idem-0001")


def test_duplicate_api_maps_source_running_to_409(monkeypatch):
    app_module, client = _api_client()
    source_id = client.post("/api/profiles", json={"name": "API Running Copy"}).json()["id"]
    monkeypatch.setattr(app_module.launcher, "profile_data_in_use", lambda _pid: True)
    try:
        response = client.post(
            f"/api/profiles/{source_id}/duplicate?copy_data=true",
            headers={"Idempotency-Key": "api-run-0001"},
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "SOURCE_RUNNING"
        assert response.headers["x-operation-id"] == "api-run-0001"
    finally:
        client.delete(f"/api/profiles/{source_id}")


def test_duplicate_api_maps_no_space_to_507(monkeypatch):
    app_module, client = _api_client()
    source_id = client.post("/api/profiles", json={"name": "API No Space"}).json()["id"]
    user_data = os.path.join(PROFILES_DATA_DIR, source_id, "user_data")
    os.makedirs(user_data, exist_ok=True)
    with open(os.path.join(user_data, "Cookies"), "w", encoding="utf-8") as f:
        f.write("x")
    monkeypatch.setattr(app_module.launcher, "profile_data_in_use", lambda _pid: False)

    def no_space(*_args, **_kwargs):
        raise CopyNoSpaceError(free=10, required=1024)

    monkeypatch.setattr(storage, "check_disk_space", no_space)
    try:
        response = client.post(
            f"/api/profiles/{source_id}/duplicate?copy_data=true",
            headers={"Idempotency-Key": "api-nospace-0001"},
        )
        assert response.status_code == 507
        assert response.json()["code"] == "COPY_NO_SPACE"
    finally:
        client.delete(f"/api/profiles/{source_id}")
