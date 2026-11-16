"""P2-13: signed upgrade/rollback — chỉ dùng thư mục tạm, không đụng data thật."""
import base64
import json
import os
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from antidetect import updater


def _keypair():
    key = Ed25519PrivateKey.generate()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return key, public_pem


def _write(path, data):
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": "\n"}
    with open(path, mode, **kwargs) as f:
        f.write(data)


def _source_tree(tmp_path, version="1.1.0", extra=None):
    root = tmp_path / "source"
    files = {
        "antidetect/__init__.py": f'__version__ = "{version}"\n',
        "antidetect/updater.py": "# new updater\n",
        "web/app.py": "# new app\n",
        "docs/README.md": "new doc\n",
    }
    files.update(extra or {})
    for rel, data in files.items():
        _write(root / rel, data)
    return root


def _install_tree(tmp_path, version="1.0.0"):
    root = tmp_path / "install"
    _write(root / "antidetect" / "__init__.py", f'__version__ = "{version}"\n')
    _write(root / "web" / "app.py", "# old app\n")
    _write(root / "tools" / "old_tool.py", "# old tool\n")
    _write(root / "data" / "profiles.json", '{"profiles": []}\n')
    return root


def _build(tmp_path, source_root, key=None, allow_unsigned=False, delete=None,
           version="1.1.0"):
    out = tmp_path / "dist" / f"release-{version}.zip"
    return updater.build_release(
        str(source_root),
        str(out),
        version,
        private_key=key,
        allow_unsigned=allow_unsigned,
        delete=delete or [],
    )


def _rewrite_zip(zip_path, replacements=None, drop=()):
    replacements = replacements or {}
    tmp = f"{zip_path}.tmp"
    with zipfile.ZipFile(zip_path, "r") as src, zipfile.ZipFile(
        tmp, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            dst.writestr(info, replacements.get(info.filename, src.read(info)))
    os.replace(tmp, zip_path)


def _craft_zip(path, manifest, payload, signature=None):
    raw = json.dumps(manifest, indent=2).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("release.json", raw)
        if signature is not None:
            zf.writestr("release.json.sig", signature)
        for arcname, data in payload.items():
            zf.writestr(arcname, data)


def _manifest(paths, version="1.1.0", delete=None):
    files = []
    for rel in paths:
        files.append({"path": rel, "sha256": "0" * 64, "size": 0})
    return {
        "schema": 1,
        "name": "antidetect-python",
        "version": version,
        "created_at": 1,
        "files": files,
        "delete": delete or [],
    }


def _apply(tmp_path, release, install_root, **kwargs):
    kwargs.setdefault("server_check", lambda: None)
    return updater.apply_update(
        release["zip"],
        str(install_root),
        updates_dir=str(tmp_path / "updates"),
        **kwargs,
    )


def test_signed_release_verifies(tmp_path):
    key, public_pem = _keypair()
    source = _source_tree(tmp_path, extra={"data/profiles.json": "must-not-ship"})
    release = _build(tmp_path, source, key=key)
    assert release["signature"] == "signed"
    verified = updater.verify_release(release["zip"], public_pem)
    paths = {entry["path"] for entry in verified["manifest"]["files"]}
    assert verified["signature"] == "verified"
    assert "antidetect/__init__.py" in paths
    assert "data/profiles.json" not in paths
    assert updater.read_release_manifest(release["zip"])["version"] == "1.1.0"


def test_raw_base64_public_key_works(tmp_path):
    key, _ = _keypair()
    source = _source_tree(tmp_path)
    release = _build(tmp_path, source, key=key)
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    verified = updater.verify_release(
        release["zip"], base64.b64encode(raw).decode("ascii")
    )
    assert verified["signature"] == "verified"


def test_signed_release_requires_public_key(tmp_path):
    key, _ = _keypair()
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    with pytest.raises(updater.SignatureError):
        updater.verify_release(release["zip"])


def test_release_rejects_wrong_public_key(tmp_path):
    key, _ = _keypair()
    _, other_pem = _keypair()
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    with pytest.raises(updater.SignatureError):
        updater.verify_release(release["zip"], other_pem)


def test_tampered_payload_rejected(tmp_path):
    key, public_pem = _keypair()
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    _rewrite_zip(
        release["zip"], {"payload/web/app.py": b"# tampered payload\n"}
    )
    with pytest.raises(updater.ManifestError):
        updater.verify_release(release["zip"], public_pem)


def test_tampered_manifest_rejected(tmp_path):
    key, public_pem = _keypair()
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    with zipfile.ZipFile(release["zip"], "r") as zf:
        raw = zf.read("release.json")
    _rewrite_zip(release["zip"], {"release.json": raw + b" "})
    with pytest.raises(updater.SignatureError):
        updater.verify_release(release["zip"], public_pem)


def test_unsigned_release_requires_opt_in(tmp_path):
    release = _build(tmp_path, _source_tree(tmp_path), allow_unsigned=True)
    with pytest.raises(updater.SignatureError):
        updater.verify_release(release["zip"])
    verified = updater.verify_release(release["zip"], allow_unsigned=True)
    assert verified["signature"] == "unsigned"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../evil.py",
        "/absolute.py",
        "C:/evil.py",
        "data/profiles.json",
        "browsers/chrome.exe",
        "web/__pycache__/x.pyc",
        "web/.env",
        "release.json",
        "CON.py",
        "web/trailing. ",
    ],
)
def test_unsafe_manifest_paths_rejected(tmp_path, bad_path):
    zip_path = tmp_path / "evil.zip"
    _craft_zip(
        zip_path,
        _manifest([bad_path]),
        {"payload/ok.py": b"x"},
    )
    with pytest.raises(updater.UpdateError):
        updater.verify_release(str(zip_path), allow_unsigned=True)


def test_extra_payload_file_rejected(tmp_path):
    zip_path = tmp_path / "extra.zip"
    _craft_zip(
        zip_path,
        _manifest(["ok.py"]),
        {"payload/ok.py": b"", "payload/extra.py": b"evil"},
    )
    with pytest.raises(updater.ZipSafetyError):
        updater.verify_release(str(zip_path), allow_unsigned=True)


def test_duplicate_manifest_entry_rejected(tmp_path):
    zip_path = tmp_path / "dup.zip"
    raw = json.dumps(_manifest(["ok.py"])).encode("utf-8")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("release.json", raw)
        zf.writestr("release.json", raw)
        zf.writestr("payload/ok.py", b"")
    with pytest.raises(updater.ZipSafetyError):
        updater.verify_release(str(zip_path), allow_unsigned=True)


def test_apply_and_rollback_roundtrip(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(
        tmp_path,
        _source_tree(tmp_path),
        key=key,
        delete=["tools/old_tool.py"],
    )
    result = _apply(
        tmp_path, release, install, public_key=public_pem
    )
    assert result["success"] is True
    assert result["from_version"] == "1.0.0"
    assert result["to_version"] == "1.1.0"
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# new app\n"
    assert (install / "docs" / "README.md").is_file()
    assert not (install / "tools" / "old_tool.py").exists()
    assert (install / "data" / "profiles.json").read_text(
        encoding="utf-8"
    ) == '{"profiles": []}\n'
    state = updater.read_update_state(result["state_path"])
    assert state["current_version"] == "1.1.0"
    assert state["previous_version"] == "1.0.0"
    assert os.path.isfile(os.path.join(result["backup_dir"], "backup.json"))

    rolled = updater.rollback_update(
        str(install),
        updates_dir=str(tmp_path / "updates"),
        server_check=lambda: None,
    )
    assert rolled["success"] is True
    assert rolled["to_version"] == "1.0.0"
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# old app\n"
    assert (install / "tools" / "old_tool.py").is_file()
    assert not (install / "docs" / "README.md").exists()
    assert (install / "data" / "profiles.json").read_text(
        encoding="utf-8"
    ) == '{"profiles": []}\n'
    state = updater.read_update_state(rolled["state_path"])
    assert state["current_version"] == "1.0.0"
    assert state["rollback"] is None


def test_downgrade_requires_opt_in(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path, version="2.0.0")
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    with pytest.raises(updater.DowngradeError):
        _apply(tmp_path, release, install, public_key=public_pem)
    assert updater.read_installed_version(str(install)) == "2.0.0"
    result = _apply(
        tmp_path, release, install, public_key=public_pem, allow_downgrade=True
    )
    assert result["to_version"] == "1.1.0"


def test_running_server_blocks_apply_unless_stop_requested(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    with pytest.raises(updater.ServerRunningError):
        _apply(
            tmp_path, release, install, public_key=public_pem,
            server_check=lambda: {"port": 8000, "pid": 123},
        )
    assert updater.read_installed_version(str(install)) == "1.0.0"

    calls = {"n": 0}

    def server_check():
        calls["n"] += 1
        return {"port": 8000, "pid": 123} if calls["n"] == 1 else None

    stopped = []
    result = _apply(
        tmp_path, release, install, public_key=public_pem,
        server_check=server_check, stop_server=True,
        stop_server_fn=lambda: stopped.append(True) or {
            "success": True, "message": "stopped"
        },
    )
    assert stopped == [True]
    assert result["to_version"] == "1.1.0"


def test_state_corruption_blocks_apply_before_writes(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    updates = tmp_path / "updates"
    _write(updates / "update_state.json", "{broken")
    with pytest.raises(updater.UpdateError):
        _apply(tmp_path, release, install, public_key=public_pem)
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# old app\n"


def test_tampered_backup_blocks_rollback_before_writes(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    result = _apply(tmp_path, release, install, public_key=public_pem)
    backup_app = os.path.join(result["backup_dir"], "files", "web", "app.py")
    with open(backup_app, "a", encoding="utf-8") as f:
        f.write("# tampered backup\n")
    with pytest.raises(updater.UpdateError):
        updater.rollback_update(
            str(install),
            updates_dir=str(tmp_path / "updates"),
            server_check=lambda: None,
        )
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# new app\n"


def test_authenticode_required_for_pe_payload(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    source = _source_tree(tmp_path, extra={"bin/tool.exe": b"MZ fake"})
    release = _build(tmp_path, source, key=key)
    monkeypatch.setattr(updater, "authenticode_status", lambda path: "NotSigned")
    with pytest.raises(updater.UpdateError):
        _apply(
            tmp_path, release, install, public_key=public_pem,
            require_authenticode=True,
        )
    assert updater.read_installed_version(str(install)) == "1.0.0"

    monkeypatch.setattr(updater, "authenticode_status", lambda path: "Valid")
    result = _apply(
        tmp_path, release, install, public_key=public_pem,
        require_authenticode=True,
    )
    assert result["to_version"] == "1.1.0"
    assert result["authenticode"].get("tool.exe") == "Valid"


def test_build_rejects_delete_overlap_and_protected(tmp_path):
    key, _ = _keypair()
    source = _source_tree(tmp_path)
    with pytest.raises(updater.UpdateError):
        _build(tmp_path, source, key=key, delete=["web/app.py"])
    with pytest.raises(updater.UpdateError):
        _build(tmp_path, source, key=key, delete=["data/profiles.json"])


def test_build_rejects_version_mismatch(tmp_path):
    key, _ = _keypair()
    source = _source_tree(tmp_path, version="1.1.0")
    with pytest.raises(updater.UpdateError):
        _build(tmp_path, source, key=key, version="1.2.0")
    release = updater.build_release(
        str(source),
        str(tmp_path / "dist" / "mismatch.zip"),
        "1.2.0",
        private_key=key,
        require_package_version=False,
    )
    assert release["manifest"]["version"] == "1.2.0"


def test_collect_excludes_sensitive_files(tmp_path):
    source = _source_tree(
        tmp_path,
        extra={
            "browsers/chrome/x.exe": b"bin",
            ".env": "SECRET=1",
            "secrets.dpapi": b"secret",
            "keys/release.pem": "pem",
            "old.zip": b"zip",
            "web/__pycache__/x.pyc": b"pyc",
            "data/update_state.json": "{}",
        },
    )
    files = updater.collect_release_files(str(source))
    joined = "\n".join(files)
    for banned in ("browsers/", ".env", ".dpapi", ".pem", ".zip", "__pycache__", "data/"):
        assert banned not in joined


def test_apply_dry_run_changes_nothing(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    result = _apply(
        tmp_path, release, install, public_key=public_pem, dry_run=True
    )
    assert result["dry_run"] is True
    assert result["to_version"] == "1.1.0"
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert not (tmp_path / "updates" / "update_state.json").exists()


def test_apply_failure_mid_write_auto_rolls_back(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(
        tmp_path,
        _source_tree(tmp_path),
        key=key,
        delete=["tools/old_tool.py"],
    )
    real_copy = updater._copy_verified
    failed = {"once": False}

    def flaky_copy(source, dest, digest):
        normalized = str(dest).replace("\\", "/")
        if not failed["once"] and normalized.endswith("docs/README.md"):
            failed["once"] = True
            raise OSError("disk full (injected)")
        return real_copy(source, dest, digest)

    monkeypatch.setattr(updater, "_copy_verified", flaky_copy)
    with pytest.raises(updater.UpdateError, match="tự rollback"):
        _apply(tmp_path, release, install, public_key=public_pem)

    assert failed["once"] is True
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# old app\n"
    assert (install / "tools" / "old_tool.py").is_file()
    assert not (install / "docs" / "README.md").exists()
    assert (install / "data" / "profiles.json").read_text(
        encoding="utf-8"
    ) == '{"profiles": []}\n'
    assert not (tmp_path / "updates" / "update_state.json").exists()
    backups = list((tmp_path / "updates" / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "backup.json").is_file()


def test_public_key_rotation_rejects_old_release_and_accepts_new(tmp_path):
    old_key, _old_public = _keypair()
    new_key, new_public = _keypair()
    install = _install_tree(tmp_path)
    old_release = _build(tmp_path, _source_tree(tmp_path), key=old_key)

    with pytest.raises(updater.SignatureError):
        _apply(tmp_path, old_release, install, public_key=new_public)
    assert updater.read_installed_version(str(install)) == "1.0.0"

    source = tmp_path / "source-new"
    _write(source / "antidetect" / "__init__.py", '__version__ = "1.2.0"\n')
    _write(source / "web" / "app.py", "# rotated app\n")
    new_release = updater.build_release(
        str(source),
        str(tmp_path / "dist" / "release-1.2.0.zip"),
        "1.2.0",
        private_key=new_key,
    )
    result = _apply(tmp_path, new_release, install, public_key=new_public)
    assert result["to_version"] == "1.2.0"
    assert updater.read_installed_version(str(install)) == "1.2.0"



def _directory_link(link, target):
    if os.name == "nt":
        import subprocess
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode:
            pytest.skip("Directory junction unavailable on this runner")
    else:
        os.symlink(target, link, target_is_directory=True)


def _unlink_directory_link(link):
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("location", ["install-root", "install-child", "updates-root"])
def test_signed_update_rejects_reparse_paths_before_install_writes(tmp_path, location, dry_run):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "README.md"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    link = None
    if location == "install-root":
        link = tmp_path / "install-link"
        _directory_link(link, install)
        chosen_install = link
    elif location == "install-child":
        link = install / "docs"
        _directory_link(link, outside)
        chosen_install = install
    else:
        link = tmp_path / "updates"
        _directory_link(link, outside)
        chosen_install = install
    try:
        with pytest.raises(updater.UpdateError):
            _apply(tmp_path, release, chosen_install, public_key=public_pem, dry_run=dry_run)
        assert updater.read_installed_version(str(install)) == "1.0.0"
        assert sentinel.read_text(encoding="utf-8") == "outside sentinel"
    finally:
        _unlink_directory_link(link)


def test_rollback_rejects_reparse_target_before_restoring_any_file(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    _apply(tmp_path, release, install, public_key=public_pem)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "README.md"
    sentinel.write_text("new doc\n", encoding="utf-8")
    (install / "docs").rename(install / "saved-docs")
    link = install / "docs"
    _directory_link(link, outside)
    try:
        with pytest.raises(updater.UpdateError):
            updater.rollback_update(
                str(install), updates_dir=str(tmp_path / "updates"),
                server_check=lambda: None,
            )
        assert updater.read_installed_version(str(install)) == "1.1.0"
        assert sentinel.read_text(encoding="utf-8") == "new doc\n"
    finally:
        _unlink_directory_link(link)



@pytest.mark.parametrize("location", ["staging", "backups", "state-parent"])
def test_update_operational_paths_reject_junctions(tmp_path, location):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    updates = tmp_path / "updates"
    updates.mkdir()
    kwargs = {}
    if location == "state-parent":
        link = tmp_path / "state-link"
        _directory_link(link, outside)
        kwargs["state_path"] = str(link / "state.json")
    else:
        link = updates / location
        _directory_link(link, outside)
    try:
        with pytest.raises(updater.UpdateError):
            _apply(tmp_path, release, install, public_key=public_pem, **kwargs)
        assert updater.read_installed_version(str(install)) == "1.0.0"
        assert list(outside.iterdir()) == [sentinel]
        assert sentinel.read_text(encoding="utf-8") == "keep"
    finally:
        _unlink_directory_link(link)


def test_rollback_rejects_backup_junction_before_install_writes(tmp_path):
    from pathlib import Path

    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    result = _apply(tmp_path, release, install, public_key=public_pem)
    backup_files = Path(result["backup_dir"]) / "files"
    saved = backup_files / "saved-web"
    (backup_files / "web").rename(saved)
    link = backup_files / "web"
    _directory_link(link, saved)
    try:
        with pytest.raises(updater.UpdateError):
            updater.rollback_update(
                str(install), updates_dir=str(tmp_path / "updates"),
                server_check=lambda: None,
            )
        assert updater.read_installed_version(str(install)) == "1.1.0"
    finally:
        _unlink_directory_link(link)


def test_copy_rechecks_parent_before_replace_and_does_not_cleanup_outside(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"new")
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "file.bin"
    sentinel.write_bytes(b"keep")
    original_hash = updater.sha256_file
    redirected = []

    def hash_then_redirect(path):
        digest = original_hash(path)
        if ".tmp." in str(path) and not redirected:
            from pathlib import Path
            saved = tmp_path / "saved-target"
            target_dir.rename(saved)
            _directory_link(target_dir, outside)
            # Cleanup must not remove an outside file with the temp basename.
            outside_temp = outside / Path(path).name
            outside_temp.write_bytes(b"outside-temp")
            redirected.append(outside_temp)
        return digest

    monkeypatch.setattr(updater, "sha256_file", hash_then_redirect)
    try:
        with pytest.raises(updater.UpdateError):
            updater._copy_verified(str(source), str(target_dir / "file.bin"), original_hash(source))
        assert sentinel.read_bytes() == b"keep"
        assert redirected[0].read_bytes() == b"outside-temp"
    finally:
        if redirected:
            _unlink_directory_link(target_dir)


def test_prune_preserves_backup_with_nested_junction(tmp_path):
    backup_root = tmp_path / "backups"
    old = backup_root / "old"
    new = backup_root / "new"
    for directory in (old, new):
        directory.mkdir(parents=True)
        (directory / "backup.json").write_text("{}", encoding="utf-8")
    os.utime(old, (1, 1))
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    link = old / "files"
    _directory_link(link, outside)
    try:
        updater._prune_backups(str(backup_root), keep=1)
        assert old.exists()
        assert sentinel.read_bytes() == b"keep"
    finally:
        _unlink_directory_link(link)



def test_interrupted_apply_is_recovered_before_next_update(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    original_copy = updater._copy_verified

    def crash_after_version_write(source, target, digest):
        original_copy(source, target, digest)
        if os.path.normcase(str(target)) == os.path.normcase(str(install / "antidetect" / "__init__.py")):
            raise SystemExit("synthetic process interruption")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_copy_verified", crash_after_version_write)
        with pytest.raises(SystemExit):
            _apply(tmp_path, release, install, public_key=public_pem)
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (tmp_path / "updates" / "pending_update.json").is_file()
    result = _apply(tmp_path, release, install, public_key=public_pem)
    # Recovery restored 1.0.0 before applying the new request.
    assert result["from_version"] == "1.0.0"
    assert result["recovery"]["action"] == "rollback_interrupted_apply"
    assert not (tmp_path / "updates" / "pending_update.json").exists()
    assert updater.read_installed_version(str(install)) == "1.1.0"


def test_apply_commit_marker_prevents_rollback_after_state_commit(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    original_write = updater._write_json_atomic

    def crash_after_commit(path, data):
        original_write(path, data)
        if os.path.basename(path) == "update_state.json":
            raise SystemExit("synthetic crash after durable commit")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_write_json_atomic", crash_after_commit)
        with pytest.raises(SystemExit):
            _apply(tmp_path, release, install, public_key=public_pem)
    pending = tmp_path / "updates" / "pending_update.json"
    assert pending.is_file()
    state = updater.read_update_state(str(tmp_path / "updates" / "update_state.json"))
    assert state["last_operation_id"]
    result = _apply(tmp_path, release, install, public_key=public_pem)
    assert result["from_version"] == "1.1.0"
    assert result["recovery"]["action"] == "finish_committed_update"
    assert not pending.exists()


def test_interrupted_rollback_is_finished_before_next_update(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    _apply(tmp_path, release, install, public_key=public_pem)
    original_copy = updater._copy_verified

    def crash_after_first_restore(source, target, digest):
        original_copy(source, target, digest)
        if os.path.normcase(str(target)) == os.path.normcase(str(install / "antidetect" / "__init__.py")):
            raise SystemExit("synthetic interrupted rollback")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_copy_verified", crash_after_first_restore)
        with pytest.raises(SystemExit):
            updater.rollback_update(str(install), updates_dir=str(tmp_path / "updates"), server_check=lambda: None)
    assert updater.read_installed_version(str(install)) == "1.0.0"
    result = _apply(tmp_path, release, install, public_key=public_pem)
    assert result["from_version"] == "1.0.0"
    assert result["recovery"]["action"] == "finish_interrupted_rollback"
    history = updater.read_update_state(result["state_path"])["history"]
    assert [item["action"] for item in history] == ["apply", "rollback", "apply"]
    assert not (tmp_path / "updates" / "pending_update.json").exists()


@pytest.mark.parametrize("pending_content", ["{invalid", '{"schema": 1, "action": "apply"}'])
def test_malformed_pending_update_blocks_new_install_writes(tmp_path, pending_content):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    pending = tmp_path / "updates" / "pending_update.json"
    _write(pending, pending_content)
    with pytest.raises(updater.UpdateError):
        _apply(tmp_path, release, install, public_key=public_pem)
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert pending.read_text(encoding="utf-8") == pending_content


def _make_interrupted_apply(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    original_copy = updater._copy_verified

    def interrupt(source, target, digest):
        original_copy(source, target, digest)
        if os.path.normcase(str(target)) == os.path.normcase(str(install / "antidetect" / "__init__.py")):
            raise SystemExit("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_copy_verified", interrupt)
        with pytest.raises(SystemExit):
            _apply(tmp_path, release, install, public_key=public_pem)
    return install, release, public_pem


@pytest.mark.parametrize("blocked_by", ["running-server", "dry-run", "backup-hash"])
def test_interrupted_update_recovery_preserves_journal_when_blocked(tmp_path, monkeypatch, blocked_by):
    install, release, public_pem = _make_interrupted_apply(tmp_path, monkeypatch)
    pending = tmp_path / "updates" / "pending_update.json"
    pending_before = pending.read_bytes()
    kwargs = {}
    if blocked_by == "running-server":
        kwargs["server_check"] = lambda: {"port": 8123}
    elif blocked_by == "dry-run":
        kwargs["dry_run"] = True
    else:
        from pathlib import Path
        backup = json.loads(pending_before)["backup_dir"]
        (Path(backup) / "files" / "web" / "app.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(updater.UpdateError):
        _apply(tmp_path, release, install, public_key=public_pem, **kwargs)
    assert pending.read_bytes() == pending_before
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# old app\n"
    assert not (tmp_path / "updates" / "update_state.json").exists()


@pytest.mark.parametrize("corrupt_field", ["install_root", "state_file", "backup_manifest_sha256", "action"])
def test_pending_journal_validation_prevents_recovery_writes(tmp_path, monkeypatch, corrupt_field):
    install, release, public_pem = _make_interrupted_apply(tmp_path, monkeypatch)
    pending = tmp_path / "updates" / "pending_update.json"
    payload = json.loads(pending.read_text(encoding="utf-8"))
    payload[corrupt_field] = (
        [] if corrupt_field == "action" else
        "0" * 64 if corrupt_field == "backup_manifest_sha256" else
        str(tmp_path / "wrong")
    )
    pending.write_text(json.dumps(payload), encoding="utf-8")
    pending_before = pending.read_bytes()
    with pytest.raises(updater.UpdateError):
        _apply(tmp_path, release, install, public_key=public_pem)
    assert pending.read_bytes() == pending_before
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# old app\n"


def test_recovery_interrupted_twice_is_idempotent(tmp_path, monkeypatch):
    install, release, public_pem = _make_interrupted_apply(tmp_path, monkeypatch)
    original_copy = updater._copy_verified
    pending = tmp_path / "updates" / "pending_update.json"
    pending_before = pending.read_bytes()

    def interrupt_recovery(source, target, digest):
        original_copy(source, target, digest)
        if os.path.normcase(str(target)) == os.path.normcase(str(install / "antidetect" / "__init__.py")):
            raise SystemExit("second synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_copy_verified", interrupt_recovery)
        with pytest.raises(SystemExit):
            _apply(tmp_path, release, install, public_key=public_pem)
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert pending.read_bytes() == pending_before
    result = _apply(tmp_path, release, install, public_key=public_pem)
    assert result["recovery"]["action"] == "rollback_interrupted_apply"
    assert result["from_version"] == "1.0.0"
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert not pending.exists()


@pytest.mark.parametrize("explicit_backup", [False, True])
@pytest.mark.parametrize("crash_point", ["first-file", "state-commit"])
def test_retry_interrupted_rollback_completes_once(tmp_path, monkeypatch, explicit_backup, crash_point):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    applied = _apply(tmp_path, release, install, public_key=public_pem)
    original_copy = updater._copy_verified
    original_write = updater._write_json_atomic
    kwargs = {"backup_dir": applied["backup_dir"]} if explicit_backup else {}

    def interrupt_copy(source, target, digest):
        original_copy(source, target, digest)
        if os.path.normcase(str(target)) == os.path.normcase(str(install / "antidetect" / "__init__.py")):
            raise SystemExit("synthetic rollback interruption")

    def interrupt_commit(path, data):
        original_write(path, data)
        if os.path.basename(path) == "update_state.json":
            raise SystemExit("synthetic rollback commit interruption")

    with monkeypatch.context() as patch:
        if crash_point == "first-file":
            patch.setattr(updater, "_copy_verified", interrupt_copy)
        else:
            patch.setattr(updater, "_write_json_atomic", interrupt_commit)
        with pytest.raises(SystemExit):
            updater.rollback_update(
                str(install), updates_dir=str(tmp_path / "updates"),
                server_check=lambda: None, **kwargs,
            )
    result = updater.rollback_update(
        str(install), updates_dir=str(tmp_path / "updates"),
        server_check=lambda: None, **kwargs,
    )
    assert result["success"]
    assert result["from_version"] == "1.1.0"
    assert result["to_version"] == "1.0.0"
    assert [entry["action"] for entry in updater.read_update_state(result["state_path"])["history"]] == ["apply", "rollback"]
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# old app\n"
    assert not (tmp_path / "updates" / "pending_update.json").exists()


def test_error_after_state_commit_does_not_auto_rollback_committed_files(tmp_path, monkeypatch):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    original_write = updater._write_json_atomic

    def fail_after_commit(path, data):
        original_write(path, data)
        if os.path.basename(path) == "update_state.json":
            raise OSError("synthetic post-commit error")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_write_json_atomic", fail_after_commit)
        with pytest.raises(updater.UpdateError, match="commit"):
            _apply(tmp_path, release, install, public_key=public_pem)
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# new app\n"
    assert (tmp_path / "updates" / "pending_update.json").is_file()
    result = _apply(tmp_path, release, install, public_key=public_pem)
    assert result["recovery"]["action"] == "finish_committed_update"


def test_legacy_state_without_pending_or_operation_marker_remains_compatible(tmp_path):
    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    _write(tmp_path / "updates" / "update_state.json", json.dumps({
        "schema": 1, "current_version": "1.0.0",
        "history": [{"action": "apply", "from": "0.9.0", "to": "1.0.0", "at": 1}],
    }))
    result = _apply(tmp_path, release, install, public_key=public_pem)
    assert result["recovery"] is None
    state = updater.read_update_state(result["state_path"])
    assert state["current_version"] == "1.1.0"
    assert [entry["to"] for entry in state["history"]] == ["1.0.0", "1.1.0"]


@pytest.mark.parametrize("liveness", ["alive", "unknown"])
def test_unresponsive_recorded_server_blocks_update(tmp_path, monkeypatch, liveness):
    from antidetect import server_control

    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    monkeypatch.setattr(server_control, "read_server_info", lambda: {"pid": 424242, "port": 8123})
    monkeypatch.setattr(server_control, "check_server_health", lambda _: None)
    monkeypatch.setattr(updater, "_server_pid_liveness", lambda _: liveness, raising=False)
    with pytest.raises(updater.ServerRunningError):
        _apply(tmp_path, release, install, public_key=public_pem, server_check=updater.detect_running_server)
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert not (tmp_path / "updates" / "pending_update.json").exists()


def test_unresponsive_recorded_server_blocks_interrupted_recovery(tmp_path, monkeypatch):
    from antidetect import server_control

    install, release, public_pem = _make_interrupted_apply(tmp_path, monkeypatch)
    pending = tmp_path / "updates" / "pending_update.json"
    before = pending.read_bytes()
    monkeypatch.setattr(server_control, "read_server_info", lambda: {"pid": 424242, "port": 8123})
    monkeypatch.setattr(server_control, "check_server_health", lambda _: None)
    monkeypatch.setattr(updater, "_server_pid_liveness", lambda _: "unknown", raising=False)
    with pytest.raises(updater.ServerRunningError):
        _apply(tmp_path, release, install, public_key=public_pem, server_check=updater.detect_running_server)
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert pending.read_bytes() == before


def test_proven_absent_recorded_server_does_not_block_update(monkeypatch):
    from antidetect import server_control

    monkeypatch.setattr(server_control, "read_server_info", lambda: {"pid": 424242, "port": 8123})
    monkeypatch.setattr(server_control, "check_server_health", lambda _: None)
    monkeypatch.setattr(updater, "_server_pid_liveness", lambda _: "absent", raising=False)
    assert updater.detect_running_server() is None


def test_corrupt_server_pid_file_cannot_prove_stopped(tmp_path, monkeypatch):
    from antidetect import server_control

    pid_file = tmp_path / "server.pid"
    pid_file.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(server_control, "PID_FILE", str(pid_file))
    monkeypatch.setattr(server_control, "check_server_health", lambda _: None)
    assert updater.detect_running_server() is not None


@pytest.mark.parametrize("condition,expected", [
    ("running", "alive"), ("exited", "absent"), ("denied", "unknown"),
    ("invalid-pid", "absent"), ("query-failed", "unknown"),
])
def test_server_pid_liveness_uses_native_handle_fail_closed(monkeypatch, condition, expected):
    from types import SimpleNamespace
    from antidetect import process_identity
    import ctypes

    closed = []
    def exit_code(handle, out):
        assert handle == 0x100000001
        out._obj.value = 0 if condition == "exited" else 259
        return condition != "query-failed"
    api = SimpleNamespace(
        OpenProcess=lambda *args: 0 if condition in ("denied", "invalid-pid") else 0x100000001,
        GetExitCodeProcess=exit_code,
        CloseHandle=lambda handle: closed.append(handle),
    )
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(process_identity, "_kernel32", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87 if condition == "invalid-pid" else 5, raising=False)
    assert updater._server_pid_liveness(424242) == expected
    assert closed == ([] if condition in ("denied", "invalid-pid") else [0x100000001])


@pytest.mark.parametrize("defect", ["truncated", "invalid-files", "invalid-delete", "invalid-added", "bad-hash", "bad-version", "wrong-root", "missing-restored-file"])
def test_malformed_backup_manifest_blocks_rollback_before_any_write(tmp_path, defect):
    from pathlib import Path

    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    applied = _apply(tmp_path, release, install, public_key=public_pem)
    manifest_path = Path(applied["backup_dir"]) / "backup.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if defect == "truncated":
        manifest = {"schema": 1}
    elif defect == "invalid-files":
        manifest["files"] = [False]
    elif defect == "invalid-delete":
        manifest["delete"] = "web/app.py"
    elif defect == "invalid-added":
        manifest["added"] = [{"path": "../outside", "sha256": "0" * 64}]
    elif defect == "bad-hash":
        manifest["files"][0]["sha256"] = 17
    elif defect == "bad-version":
        manifest["from_version"] = []
    elif defect == "wrong-root":
        manifest["install_root"] = str(tmp_path / "wrong-install")
    else:
        manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != "web/app.py"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state_path = Path(applied["state_path"])
    state_before = state_path.read_bytes()
    with pytest.raises(updater.UpdateError):
        updater.rollback_update(str(install), updates_dir=str(tmp_path / "updates"), server_check=lambda: None)
    assert updater.read_installed_version(str(install)) == "1.1.0"
    assert (install / "web" / "app.py").read_text(encoding="utf-8") == "# new app\n"
    assert state_path.read_bytes() == state_before
    assert not (tmp_path / "updates" / "pending_update.json").exists()


def test_legacy_backup_without_root_or_state_hash_remains_compatible(tmp_path):
    from pathlib import Path

    key, public_pem = _keypair()
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), key=key)
    applied = _apply(tmp_path, release, install, public_key=public_pem)
    manifest_path = Path(applied["backup_dir"]) / "backup.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("install_root", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state_path = Path(applied["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["rollback"].pop("backup_manifest_sha256", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = updater.rollback_update(str(install), updates_dir=str(tmp_path / "updates"), server_check=lambda: None)
    assert result["to_version"] == "1.0.0"
    assert updater.read_installed_version(str(install)) == "1.0.0"
