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
