"""Filesystem and copy boundary regressions; all files are synthetic TEMP data."""
import os
from types import SimpleNamespace

import pytest

import antidetect.launcher as launcher_module
import antidetect.storage as storage
from antidetect.launcher import BrowserLauncher
from antidetect.profile_copy import ProfileCopyError
from test_updater import _directory_link, _unlink_directory_link


@pytest.mark.parametrize("kind", ["junction", "wrong-name", "missing"])
def test_default_browser_fallback_enforces_candidate_guards(tmp_path, monkeypatch, kind):
    monkeypatch.delenv("ANTIDETECT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(launcher_module, "BROWSERS_DIR", str(tmp_path / "empty"))
    launcher = object.__new__(BrowserLauncher)
    outside = tmp_path / "synthetic-browser"
    outside.mkdir()
    (outside / "chrome.exe").write_bytes(b"not executable test fixture")
    link = None
    if kind == "junction":
        link = tmp_path / "browser-link"
        _directory_link(link, outside)
        candidate = link / "chrome.exe"
    elif kind == "wrong-name":
        candidate = outside / "other.exe"
        candidate.write_bytes(b"not executable test fixture")
    else:
        candidate = outside / "missing" / "chrome.exe"
    launcher.browser_path = str(candidate)
    try:
        assert launcher._resolve_browser_path(SimpleNamespace(browser_path=None)) is None
    finally:
        if link is not None:
            _unlink_directory_link(link)


def test_copy_data_requires_explicit_idle_proof():
    manager = storage.ProfileManager()
    source = manager.create_profile("synthetic-copy-check")
    before = {profile.id for profile in manager.list_profiles()}
    try:
        with pytest.raises(ProfileCopyError) as exc:
            manager.duplicate_profile(source.id, copy_data=True)
        assert exc.value.code == "SOURCE_RUNNING"
        assert {profile.id for profile in manager.list_profiles()} == before
    finally:
        for profile in manager.list_profiles():
            if profile.id not in before or profile.id == source.id:
                manager.delete_profile(profile.id)


@pytest.mark.parametrize("location", ["profiles-parent", "profile-root", "nested"])
def test_delete_rejects_reparse_paths_before_removing_any_file(tmp_path, monkeypatch, location):
    manager = storage.ProfileManager()
    source = manager.create_profile("synthetic-delete-check")
    profiles = tmp_path / "profiles"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = None
    if location == "profiles-parent":
        link = profiles
        _directory_link(link, outside)
        profile_dir = outside / source.id
        profile_dir.mkdir()
        sentinel = profile_dir / "session.txt"
    elif location == "profile-root":
        profiles.mkdir()
        link = profiles / source.id
        _directory_link(link, outside)
        sentinel = outside / "session.txt"
    else:
        profile_dir = profiles / source.id
        profile_dir.mkdir(parents=True)
        link = profile_dir / "linked"
        _directory_link(link, outside)
        sentinel = profile_dir / "session.txt"
    sentinel.write_text("synthetic session", encoding="utf-8")
    outside_file = outside / "keep.txt"
    outside_file.write_text("synthetic outside", encoding="utf-8")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage, "PROFILES_DATA_DIR", str(profiles))
            with pytest.raises(storage.ProfileDeletionError):
                manager.delete_profile(source.id)
            assert manager.get_profile(source.id) is not None
        assert sentinel.read_text(encoding="utf-8") == "synthetic session"
        assert outside_file.read_text(encoding="utf-8") == "synthetic outside"
    finally:
        if link is not None and os.path.lexists(link):
            _unlink_directory_link(link)
        manager.delete_profile(source.id)
