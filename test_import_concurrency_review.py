"""R01/R05 regressions: reject unpublished imports and stale geo updates."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ.setdefault("ANTIDETECT_DATA_DIR", tempfile.mkdtemp(prefix="ad-review-import-"))
import antidetect.storage as storage
import antidetect.secrets as secrets_store
import web.app as web
from antidetect.models import Profile, ProxyConfig
from antidetect.profile_import import prepare_import_profile


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIDETECT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "PROFILES_JSON", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(storage, "PROFILES_DATA_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(secrets_store, "LOCK_FILE", tmp_path / "secrets.lock")
    m = storage.ProfileManager()
    monkeypatch.setattr(web, "profile_manager", m)
    return m


def _profile(manager, name="existing", proxy=False):
    p = Profile(name=name, proxy=ProxyConfig(enabled=proxy, host="proxy.example", port=8080))
    return manager.commit_staged_profile(p)


@pytest.mark.parametrize("field,value", [
    ("browser_path", {"bad": "type"}), ("browser_path", []),
    ("facebook_pages_synced_at", "invalid-integer"), ("facebook_pages_synced_at", True),
    ("tags", "not-a-list"), ("tags", [42]), ("name", {"secret": "ignored"}),
])
def test_invalid_import_publishes_nothing(manager, field, value):
    existing = _profile(manager)
    before = Path(storage.PROFILES_JSON).read_bytes()
    result = asyncio.run(web.import_profiles(web.ImportProfilesRequest(profiles=[{field: value}])))
    assert result["failed_count"] == 1
    assert result["imported_count"] == 0
    assert Path(storage.PROFILES_JSON).read_bytes() == before
    assert [p.id for p in storage.ProfileManager().list_profiles()] == [existing.id]
    assert not list(Path(storage.PROFILES_DATA_DIR).iterdir())


def test_mixed_import_commits_each_entry_once(manager):
    result = asyncio.run(web.import_profiles(web.ImportProfilesRequest(profiles=[
        {"name": "first", "id": "untrusted", "pid": 20, "status": "running",
         "revision": 55, "proxy": {"password": "never-import"}},
        None, {"browser_path": {}}, {"name": "last"},
    ])))
    assert result["imported_count"] == 2
    assert [item["index"] for item in result["failed"]] == [1, 2]
    profiles = storage.ProfileManager().list_profiles()
    assert [p.name for p in profiles] == ["first", "last"]
    assert all(p.id != "untrusted" and p.pid is None and p.revision == 1 and p.status == "stopped" for p in profiles)
    assert all(p.proxy.password is None for p in profiles)
    assert not list(Path(storage.PROFILES_DATA_DIR).iterdir())


def test_import_failed_replace_keeps_prior_metadata(manager, monkeypatch):
    _profile(manager)
    before = Path(storage.PROFILES_JSON).read_bytes()
    monkeypatch.setattr(storage, "_replace_with_retry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    result = asyncio.run(web.import_profiles(web.ImportProfilesRequest(profiles=[{"name": "must-not-exist"}])))
    assert result["failed_count"] == 1
    assert Path(storage.PROFILES_JSON).read_bytes() == before
    assert [p.name for p in manager.list_profiles()] == ["existing"]


def test_invalid_model_rejected_before_secret_side_effect(manager, monkeypatch):
    p = _profile(manager)
    p.browser_path = {"invalid": True}
    calls = []
    monkeypatch.setattr(storage, "migrate_plaintext_passwords", lambda *a: calls.append("migrate"))
    monkeypatch.setattr(storage, "set_proxy_password", lambda *a, **k: calls.append("secret"))
    with pytest.raises(ValidationError):
        manager.update_profile(p)
    with pytest.raises(ValidationError):
        manager.save_profiles([p])
    assert calls == []
    assert storage.ProfileManager().list_profiles()[0].browser_path is None


@pytest.mark.parametrize("change", ["name", "proxy", "disabled", "deleted"])
def test_geo_lookup_concurrent_edit_is_preserved(manager, monkeypatch, change):
    p = _profile(manager, proxy=True)
    def lookup(proxy, timeout):
        current = manager.get_profile(p.id)
        if change == "deleted":
            manager.delete_profile(p.id)
        else:
            if change == "name":
                current.name = "new-name"
            elif change == "proxy":
                current.proxy.host = "new.example"
            else:
                current.proxy.enabled = False
            manager.update_profile(current, expected_revision=current.revision)
        return {"success": True, "timezone": "America/New_York", "lat": 40.7, "lon": -74.0, "country_code": "US"}
    monkeypatch.setattr(web, "check_proxy", lookup)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(web.sync_location_from_proxy(p.id))
    assert exc.value.status_code == (404 if change == "deleted" else 409)
    current = manager.get_profile(p.id)
    if change == "deleted":
        assert current is None
    else:
        assert current.fingerprint.timezone == p.fingerprint.timezone
        assert current.revision == 2
        if change == "name":
            assert current.name == "new-name"
        elif change == "proxy":
            assert current.proxy.host == "new.example"
        else:
            assert current.proxy.enabled is False


def test_geo_unchanged_result_still_checks_revision(manager):
    p = _profile(manager, proxy=True)
    edited = manager.get_profile(p.id)
    edited.name = "changed"
    manager.update_profile(edited)
    with pytest.raises(storage.ProfileRevisionConflict):
        web._commit_geo_result(p, {"timezone": p.fingerprint.timezone})


def test_patch_preserves_secret_and_unrelated_fields(manager, monkeypatch):
    p = _profile(manager, proxy=True)
    before = json.loads(Path(storage.PROFILES_JSON).read_text())[0]
    def forbidden(*args, **kwargs):
        raise AssertionError("fingerprint patch must not touch secrets")
    monkeypatch.setattr(storage, "get_proxy_password", forbidden)
    monkeypatch.setattr(storage, "set_proxy_password", forbidden)
    monkeypatch.setattr(storage, "migrate_plaintext_passwords", forbidden)
    updated = manager.patch_fingerprint(p.id, {"timezone": "UTC"}, expected_revision=p.revision)
    after = json.loads(Path(storage.PROFILES_JSON).read_text())[0]
    assert updated.revision == p.revision + 1
    assert after["proxy"] == before["proxy"]
    assert after["name"] == before["name"]
    assert after["fingerprint"]["canvas_noise_seed"] == before["fingerprint"]["canvas_noise_seed"]


def test_batch_patch_skips_conflict_and_deleted(manager):
    first, second, third = [_profile(manager, str(i)) for i in range(3)]
    edited = manager.get_profile(second.id)
    edited.name = "changed"
    manager.update_profile(edited)
    manager.delete_profile(third.id)
    changed = manager.patch_fingerprints_batch(
        {p.id: {"timezone": "UTC"} for p in (first, second, third)},
        {p.id: p.revision for p in (first, second, third)},
    )
    assert changed == [first.id]
    assert manager.get_profile(second.id).name == "changed"
    assert manager.get_profile(second.id).fingerprint.timezone != "UTC"


def test_external_process_update_conflicts_with_stale_patch(manager):
    p = _profile(manager)
    command = "from antidetect.storage import ProfileManager; import sys; m=ProfileManager(); p=m.get_profile(sys.argv[1]); p.name='external'; m.update_profile(p, expected_revision=p.revision)"
    result = subprocess.run([sys.executable, "-c", command, p.id], cwd=str(Path(__file__).parent),
                            env=dict(os.environ), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with pytest.raises(storage.ProfileRevisionConflict):
        manager.patch_fingerprint(p.id, {"timezone": "UTC"}, expected_revision=p.revision)
    assert manager.get_profile(p.id).name == "external"


def test_two_process_imports_preserve_all_profiles(manager):
    existing = _profile(manager)
    command = (
        "from antidetect.storage import ProfileManager; "
        "from antidetect.profile_import import prepare_import_profile; import sys; "
        "m=ProfileManager(); "
        "[m.commit_staged_profile(prepare_import_profile({'name':sys.argv[1]+str(i)},i,lambda _:[])) for i in range(4)]"
    )
    processes = [subprocess.Popen([sys.executable, "-c", command, prefix],
                                 cwd=str(Path(__file__).parent), env=dict(os.environ),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for prefix in ("A-", "B-")]
    for process in processes:
        _, error = process.communicate(timeout=30)
        assert process.returncode == 0, error.decode(errors="replace")
    profiles = storage.ProfileManager().list_profiles()
    assert len(profiles) == 9
    assert existing.id in {p.id for p in profiles}
    assert len({p.id for p in profiles}) == 9


def test_reset_fingerprint_geo_lookup_does_not_overwrite_edit(manager, monkeypatch):
    p = _profile(manager, proxy=True)
    def lookup(proxy, timeout):
        edited = manager.get_profile(p.id)
        edited.name = "keep-this-name"
        manager.update_profile(edited, expected_revision=edited.revision)
        return {"success": True, "timezone": "UTC"}
    monkeypatch.setattr(web, "check_proxy", lookup)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(web.reset_fingerprint_endpoint(p.id))
    assert exc.value.status_code == 409
    assert manager.get_profile(p.id).name == "keep-this-name"
    assert manager.get_profile(p.id).fingerprint == p.fingerprint


def test_scheduler_geo_conflict_is_failed_not_skipped(manager, monkeypatch):
    p = _profile(manager, proxy=True)
    def lookup(proxy, timeout):
        edited = manager.get_profile(p.id)
        edited.name = "changed-in-lookup"
        manager.update_profile(edited)
        return {"success": True, "timezone": "UTC"}
    monkeypatch.setattr(web, "check_proxy", lookup)
    result = web._scheduler_sync_all_locations([p.id])
    assert result["success"] is False
    assert result["failed_profile_ids"] == [p.id]
    assert result["results"][p.id]["code"] == "PROFILE_REVISION_CONFLICT"
    assert result["skipped"] == 0


def test_proxy_request_validation_does_not_echo_secret(manager):
    client = TestClient(web.app, base_url="http://127.0.0.1:8000")
    client.post("/auth/login", headers={"Authorization": "Bearer " + web.app.state.access_key})
    result = client.post("/api/profiles", json={"proxy": {
        "enabled": True, "host": "host", "port": 0, "password": "secret-do-not-echo",
    }})
    assert result.status_code == 422
    assert "secret-do-not-echo" not in result.text


def test_bulk_invalid_proxy_does_not_create_direct(manager):
    result = asyncio.run(web.bulk_create_profiles(web.BulkCreateRequest(count=1, proxies=["http://host:0"])))
    assert result["created_count"] == 0
    assert manager.list_profiles() == []
