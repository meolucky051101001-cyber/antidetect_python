"""Deletion fault injection uses synthetic TEMP profiles and DPAPI capsules."""
import json
from pathlib import Path

import pytest

from antidetect import profile_delete as deletion
from antidetect import secrets as secret_store
from antidetect import storage
from antidetect.models import ProxyConfig
from test_updater import _directory_link, _unlink_directory_link


@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("ANTIDETECT_DATA_DIR", str(data))
    monkeypatch.setattr(storage, "DATA_DIR", str(data))
    monkeypatch.setattr(storage, "PROFILES_DATA_DIR", str(data / "profiles"))
    monkeypatch.setattr(storage, "PROFILES_JSON", str(data / "profiles.json"))
    monkeypatch.setattr(secret_store, "LOCK_FILE", data / "secrets.lock")
    monkeypatch.setattr(secret_store, "_store_corrupted", False)
    return storage.ProfileManager()


def make_profile(manager, name="Synthetic deletion"):
    profile = manager.create_profile(name, proxy=ProxyConfig(
        enabled=True, host="127.0.0.1", port=8123, username="synthetic-user",
        password="synthetic-delete-secret",
    ))
    root = Path(storage.PROFILES_DATA_DIR) / profile.id
    (root / "user_data" / "Default").mkdir(parents=True)
    (root / "user_data" / "Default" / "fixture.bin").write_bytes(b"synthetic browser session")
    (root / "second.bin").write_bytes(b"second synthetic file")
    return profile, root


def assert_restored(manager, profile, root):
    assert manager.get_profile(profile.id) is not None
    assert (root / "user_data" / "Default" / "fixture.bin").read_bytes() == b"synthetic browser session"
    assert (root / "second.bin").read_bytes() == b"second synthetic file"
    assert secret_store.get_proxy_password(profile.id, profile.proxy.identity_key()) == "synthetic-delete-secret"


def pending(profile):
    return deletion.read_journal(storage.DATA_DIR, storage.PROFILES_DATA_DIR, profile.id)


@pytest.mark.parametrize("phase", ["prepared", "quarantined", "metadata_removed", "secret_removed", "committed"])
def test_crash_after_each_durable_phase_recovers_deterministically(isolated_manager, monkeypatch, phase):
    manager = isolated_manager
    profile, root = make_profile(manager)
    real = deletion.write_phase
    def crash_after_write(data, journal, next_phase):
        result = real(data, journal, next_phase)
        if next_phase == phase:
            raise SystemExit("synthetic interruption")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "write_phase", crash_after_write)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    journal = pending(profile)
    assert journal["phase"] == phase
    journal_bytes = Path(deletion.journal_path(storage.DATA_DIR, profile.id)).read_bytes()
    assert b"synthetic-delete-secret" not in journal_bytes
    assert b"synthetic-user" not in journal_bytes
    capsule = Path(storage.DATA_DIR) / "delete_operations" / (journal["operation_id"] + ".dpapi")
    assert b"synthetic-delete-secret" not in capsule.read_bytes()
    recovered = storage.ProfileManager()
    if phase == "committed":
        assert recovered.get_profile(profile.id) is None
        assert not root.exists()
        assert secret_store.get_proxy_password(profile.id) is None
    else:
        assert_restored(recovered, profile, root)
    assert pending(profile) is None
    assert recovered.recover_delete_operations() == 0


@pytest.mark.parametrize("boundary", ["rename", "metadata-write", "secret-write"])
def test_crash_between_mutation_and_journal_restores(isolated_manager, monkeypatch, boundary):
    manager = isolated_manager
    profile, root = make_profile(manager)
    target, name = ((deletion, "quarantine") if boundary == "rename" else
                    (manager, "save_profiles") if boundary == "metadata-write" else
                    (deletion, "remove_entry"))
    real = getattr(target, name)
    def crash(*args, **kwargs):
        real(*args, **kwargs)
        raise SystemExit("synthetic mutation boundary")
    with monkeypatch.context() as patch:
        patch.setattr(target, name, crash)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    recovered = storage.ProfileManager()
    assert_restored(recovered, profile, root)
    assert pending(profile) is None


@pytest.mark.parametrize("failure", ["rename", "metadata", "secret"])
def test_precommit_errors_preserve_whole_profile_and_secret(isolated_manager, monkeypatch, failure):
    manager = isolated_manager
    profile, root = make_profile(manager)
    target, name = ((deletion, "quarantine") if failure == "rename" else
                    (manager, "save_profiles") if failure == "metadata" else
                    (deletion, "remove_entry"))
    with monkeypatch.context() as patch:
        patch.setattr(target, name, lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic disk error")))
        with pytest.raises(storage.ProfileDeletionError):
            manager.delete_profile(profile.id)
    assert_restored(manager, profile, root)
    assert pending(profile) is None


def test_postcommit_partial_purge_retries_without_resurrection(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    def partial_purge(path):
        (Path(path) / "second.bin").unlink()
        raise PermissionError("synthetic locked remaining file")
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "_remove_tree_checked", partial_purge)
        assert manager.delete_profile(profile.id)
    journal = pending(profile)
    assert journal["phase"] == "committed"
    quarantine = Path(storage.PROFILES_DATA_DIR) / ".quarantine" / journal["operation_id"]
    assert (quarantine / "user_data" / "Default" / "fixture.bin").exists()
    assert not (quarantine / "second.bin").exists()
    assert not root.exists() and manager.get_profile(profile.id) is None
    assert secret_store.get_proxy_password(profile.id) is None
    assert manager.delete_profile(profile.id)
    assert not quarantine.exists() and pending(profile) is None


def test_postcommit_write_error_never_rolls_back(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    real = deletion.write_phase
    def fail_after_commit(data, journal, phase):
        result = real(data, journal, phase)
        if phase == "committed":
            raise OSError("synthetic postcommit error")
        return result
    monkeypatch.setattr(deletion, "write_phase", fail_after_commit)
    assert manager.delete_profile(profile.id)
    assert not root.exists() and manager.get_profile(profile.id) is None
    assert pending(profile) is None


def test_rollback_terminal_marker_survives_capsule_cleanup_crash(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    real_finish = deletion.finish
    def crash_after_finish(*args, **kwargs):
        if not kwargs["purge"]:
            journal = args[2]
            capsule = Path(storage.DATA_DIR) / "delete_operations" / (journal["operation_id"] + ".dpapi")
            capsule.unlink()
            raise SystemExit("synthetic rollback cleanup interruption")
        return real_finish(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "remove_entry", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
        patch.setattr(deletion, "finish", crash_after_finish)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    assert pending(profile)["phase"] == "rolled_back"
    recovered = storage.ProfileManager()
    assert_restored(recovered, profile, root)
    assert pending(profile) is None


@pytest.mark.parametrize("location", ["quarantine-parent", "operations-parent"])
def test_junction_blocks_before_rename(isolated_manager, tmp_path, location):
    manager = isolated_manager
    profile, root = make_profile(manager)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"outside")
    link = (Path(storage.PROFILES_DATA_DIR) / ".quarantine" if location == "quarantine-parent"
            else Path(storage.DATA_DIR) / "delete_operations")
    if link.exists():
        link.rmdir()
    _directory_link(link, outside)
    try:
        with pytest.raises(storage.ProfileDeletionError):
            manager.delete_profile(profile.id)
        assert_restored(manager, profile, root)
        assert sentinel.read_bytes() == b"outside"
    finally:
        _unlink_directory_link(link)


def _interrupt_after_quarantine(manager, profile, monkeypatch):
    real = deletion.write_phase
    def interrupt(data, journal, phase):
        result = real(data, journal, phase)
        if phase == "quarantined":
            raise SystemExit("synthetic")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "write_phase", interrupt)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)


def test_recreated_directory_and_secret_are_never_overwritten(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    _interrupt_after_quarantine(manager, profile, monkeypatch)
    root.mkdir()
    replacement = root / "replacement.bin"
    replacement.write_bytes(b"replacement")
    secret_store.set_proxy_password(profile.id, "replacement-secret", profile.proxy.identity_key())
    assert manager.recover_delete_operations() == 0
    assert pending(profile) is not None
    assert replacement.read_bytes() == b"replacement"
    assert secret_store.get_proxy_password(profile.id) == "replacement-secret"


@pytest.mark.parametrize("defect", ["malformed", "root", "capsule-hash"])
def test_corrupt_recovery_blocks_launch_and_writes(isolated_manager, monkeypatch, defect):
    manager = isolated_manager
    profile, root = make_profile(manager)
    _interrupt_after_quarantine(manager, profile, monkeypatch)
    path = Path(deletion.journal_path(storage.DATA_DIR, profile.id))
    journal = json.loads(path.read_text(encoding="utf-8"))
    if defect == "malformed":
        path.write_text("{broken", encoding="utf-8")
    else:
        journal["data_root" if defect == "root" else "capsule_sha256"] = (
            str(root) if defect == "root" else "0" * 64)
        path.write_text(json.dumps(journal), encoding="utf-8")
    assert manager.recover_delete_operations() == 0
    with pytest.raises((storage.ProfileDeletionError, deletion.DeletionTransactionError)):
        manager.get_profile_dir(profile.id)
    with pytest.raises((storage.ProfileDeletionError, deletion.DeletionTransactionError)):
        manager.update_profile(profile)
    assert not root.exists()


def test_synthetic_restore_preserves_unrelated_profile_and_secret(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    other, other_root = make_profile(manager, "Unrelated synthetic profile")
    _interrupt_after_quarantine(manager, profile, monkeypatch)
    secret_store.set_proxy_password(other.id, "new-unrelated-secret", other.proxy.identity_key())
    recovered = storage.ProfileManager()
    assert_restored(recovered, profile, root)
    assert other_root.exists() and recovered.get_profile(other.id) is not None
    assert secret_store.get_proxy_password(other.id) == "new-unrelated-secret"
    assert recovered.delete_profile(profile.id)
    assert other_root.exists()
    assert secret_store.get_proxy_password(other.id) == "new-unrelated-secret"


def test_committed_capsule_cleanup_crash_resumes_without_capsule(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    real_remove = deletion.os.remove
    journal_file = deletion.journal_path(storage.DATA_DIR, profile.id)
    def crash_before_journal_remove(path):
        if str(path) == journal_file:
            raise SystemExit("synthetic cleanup crash")
        return real_remove(path)
    with monkeypatch.context() as patch:
        patch.setattr(deletion.os, "remove", crash_before_journal_remove)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    journal = pending(profile)
    assert journal["phase"] == "committed" and not root.exists()
    capsule = Path(storage.DATA_DIR) / "delete_operations" / (journal["operation_id"] + ".dpapi")
    assert not capsule.exists()
    recovered = storage.ProfileManager()
    assert recovered.get_profile(profile.id) is None
    assert pending(profile) is None


def test_quarantine_junction_at_purge_never_touches_outside(isolated_manager, monkeypatch, tmp_path):
    manager = isolated_manager
    profile, root = make_profile(manager)
    real = deletion.write_phase
    def crash_after_commit(data, journal, phase):
        result = real(data, journal, phase)
        if phase == "committed":
            raise SystemExit("synthetic")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "write_phase", crash_after_commit)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    journal = pending(profile)
    quarantine = Path(storage.PROFILES_DATA_DIR) / ".quarantine" / journal["operation_id"]
    saved = quarantine.with_name(quarantine.name + "-retained")
    quarantine.rename(saved)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"outside fixture")
    _directory_link(quarantine, outside)
    try:
        assert manager.recover_delete_operations() == 0
        assert sentinel.read_bytes() == b"outside fixture"
        assert (saved / "second.bin").exists()
        assert not root.exists()
    finally:
        _unlink_directory_link(quarantine)
        saved.rename(quarantine)
    assert manager.recover_delete_operations() == 1
    assert pending(profile) is None


def test_secret_store_corruption_blocks_before_quarantine(isolated_manager):
    manager = isolated_manager
    profile, root = make_profile(manager)
    secret_path = secret_store._target_file()
    original = secret_path.read_bytes()
    secret_path.write_bytes(b"synthetic invalid ciphertext")
    try:
        with pytest.raises(storage.ProfileDeletionError):
            manager.delete_profile(profile.id)
        assert root.exists()
        assert manager.get_profile(profile.id) is not None
        assert pending(profile) is None
    finally:
        secret_path.write_bytes(original)
        secret_store._store_corrupted = False
    assert_restored(manager, profile, root)


def test_recovery_failure_retains_capsule_and_blocks_new_directory(isolated_manager, monkeypatch):
    manager = isolated_manager
    profile, root = make_profile(manager)
    _interrupt_after_quarantine(manager, profile, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "restore_directory", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
        assert manager.recover_delete_operations() == 0
        assert pending(profile)["phase"] == "quarantined"
        with pytest.raises(storage.ProfileDeletionError):
            manager.get_profile_dir(profile.id)
    assert manager.recover_delete_operations() == 1
    assert_restored(manager, profile, root)


def test_quarantine_rename_never_falls_back_to_recursive_copy(isolated_manager, monkeypatch):
    import errno
    manager = isolated_manager
    profile, root = make_profile(manager)
    real_replace = deletion.os.replace
    def cross_device(source, dest):
        if Path(source) == root:
            raise OSError(errno.EXDEV, "synthetic volume mismatch")
        return real_replace(source, dest)
    monkeypatch.setattr(deletion.os, "replace", cross_device)
    with pytest.raises(storage.ProfileDeletionError):
        manager.delete_profile(profile.id)
    assert_restored(manager, profile, root)


def test_purge_does_not_hold_global_metadata_lock(isolated_manager, monkeypatch):
    import threading
    manager = isolated_manager
    profile, _root = make_profile(manager)
    real_purge = deletion._remove_tree_checked
    updates = []
    failures = []
    def probe_purge(path):
        def create_unrelated():
            try:
                updates.append(manager.create_profile("created during purge").id)
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=create_unrelated, daemon=True)
        thread.start()
        thread.join(3)
        assert not thread.is_alive(), "large purge must not hold global metadata lock"
        real_purge(path)
    monkeypatch.setattr(deletion, "_remove_tree_checked", probe_purge)
    assert manager.delete_profile(profile.id)
    assert len(updates) == 1 and not failures
    assert manager.get_profile(updates[0]) is not None


def test_lifecycle_lock_blocks_concurrent_recovery(isolated_manager, monkeypatch):
    import threading
    manager = isolated_manager
    profile, root = make_profile(manager)
    _interrupt_after_quarantine(manager, profile, monkeypatch)
    outcomes = []
    with manager._profile_locks.acquire(profile.id):
        thread = threading.Thread(target=lambda: outcomes.append(manager.recover_delete_operations(lock_timeout=0)), daemon=True)
        thread.start()
        thread.join(3)
        assert not thread.is_alive()
    assert outcomes == [0]
    assert pending(profile)["phase"] == "quarantined"
    assert manager.recover_delete_operations() == 1
    assert_restored(manager, profile, root)


@pytest.mark.parametrize("phase", ["quarantined", "metadata_removed", "secret_removed"])
def test_legacy_plaintext_metadata_secret_is_not_lost_on_rollback(isolated_manager, monkeypatch, phase):
    manager = isolated_manager
    profile, root = make_profile(manager)
    secret_store.delete_proxy_password(profile.id)
    path = Path(storage.PROFILES_JSON)
    items = json.loads(path.read_text(encoding="utf-8"))
    items[0]["proxy"]["password"] = "legacy-synthetic-secret"
    path.write_text(json.dumps(items), encoding="utf-8")
    manager._cache = None
    real = deletion.write_phase
    def interrupt(data, journal, next_phase):
        result = real(data, journal, next_phase)
        if next_phase == phase:
            raise SystemExit("synthetic legacy recovery")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "write_phase", interrupt)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    recovered = storage.ProfileManager()
    assert recovered.get_profile(profile.id).proxy.password == "legacy-synthetic-secret"
    assert root.exists()


@pytest.mark.parametrize("legacy", ["plaintext", "string-store"])
@pytest.mark.parametrize("boundary", ["secret", "directory", "metadata", "terminal-marker"])
def test_interrupted_legacy_rollback_can_itself_be_recovered(isolated_manager, monkeypatch, legacy, boundary):
    manager = isolated_manager
    profile, root = make_profile(manager)
    expected_secret = "legacy-synthetic-secret"
    if legacy == "plaintext":
        secret_store.delete_proxy_password(profile.id)
        path = Path(storage.PROFILES_JSON)
        items = json.loads(path.read_text(encoding="utf-8"))
        items[0]["proxy"]["password"] = expected_secret
        path.write_text(json.dumps(items), encoding="utf-8")
    else:
        with secret_store._lock, secret_store._StoreLock():
            entries = secret_store._read_all_secrets()
            entries[profile.id] = expected_secret
            secret_store._write_all_secrets(entries)
    manager._cache = None
    real_phase = deletion.write_phase
    def initial_crash(data, journal, phase):
        result = real_phase(data, journal, phase)
        if phase == "secret_removed":
            raise SystemExit("synthetic initial delete crash")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(deletion, "write_phase", initial_crash)
        with pytest.raises(SystemExit):
            manager.delete_profile(profile.id)
    target, method = ((deletion, "restore_entry") if boundary == "secret" else
                      (deletion, "restore_directory") if boundary == "directory" else
                      (manager, "save_profiles") if boundary == "metadata" else
                      (deletion, "write_phase"))
    real = getattr(target, method)
    def rollback_crash(*args, **kwargs):
        if boundary == "terminal-marker" and args[2] == "rolled_back":
            raise SystemExit("synthetic before terminal marker")
        result = real(*args, **kwargs)
        if boundary != "terminal-marker":
            raise SystemExit("synthetic rollback crash")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(target, method, rollback_crash)
        with pytest.raises(SystemExit):
            manager.recover_delete_operations()
    assert pending(profile)["phase"] == "secret_removed"
    recovered = storage.ProfileManager()
    assert pending(profile) is None
    assert recovered.recover_delete_operations() == 0
    assert recovered.get_profile(profile.id).proxy.password == expected_secret
    assert secret_store.get_proxy_password(profile.id, profile.proxy.identity_key()) == expected_secret
    assert (root / "second.bin").read_bytes() == b"second synthetic file"
    metadata = Path(storage.PROFILES_JSON).read_bytes()
    assert expected_secret.encode() not in metadata
