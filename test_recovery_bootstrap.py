"""Independent recovery exercises use synthetic installs and isolated subprocesses."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from antidetect import recovery_bootstrap as bootstrap
from antidetect import updater
from antidetect.storage import InterProcessLock
from test_updater import _apply, _build, _directory_link, _install_tree, _keypair, _source_tree, _unlink_directory_link, _write


def _interrupted(tmp_path, monkeypatch, action="apply", committed=False):
    install = _install_tree(tmp_path)
    monkeypatch.setenv("ANTIDETECT_DATA_DIR", str(install / "data"))
    # A dead synthetic PID and unused ephemeral control port avoid contacting
    # the developer's unrelated real server on the default port.
    dead = subprocess.run([sys.executable, "-I", "-S", "-c", "import os;print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    _write(install / "data" / "server.pid", json.dumps({"pid": int(dead.stdout), "port": port}))
    # All modules existed in the old install, allowing complete restoration.
    for module in ("updater", "storage", "applog"):
        _write(install / "antidetect" / f"{module}.py", f"# old {module}\n")
    key, public = _keypair()
    source = _source_tree(tmp_path, extra={
        "antidetect/storage.py": "# new storage\n",
        "antidetect/applog.py": "# new applog\n",
    })
    release = _build(tmp_path, source, key=key)
    captured = []
    if action == "rollback":
        _apply(tmp_path, release, install, public_key=public)
    original_copy, original_write = updater._copy_verified, updater._write_json_atomic

    def interrupt_copy(src, dst, sha):
        original_copy(src, dst, sha)
        if Path(dst) == install / "antidetect" / "__init__.py":
            raise SystemExit("synthetic process interruption")

    def interrupt_commit(path, value):
        original_write(path, value)
        if Path(path).name == "update_state.json":
            raise SystemExit("synthetic commit interruption")

    with monkeypatch.context() as patch:
        patch.setattr(updater, "_write_json_atomic" if committed else "_copy_verified",
                      interrupt_commit if committed else interrupt_copy)
        with pytest.raises(SystemExit):
            if action == "rollback":
                updater.rollback_update(str(install), updates_dir=str(tmp_path / "updates"),
                                        server_check=lambda: None, recovery_notify=captured.append)
            else:
                _apply(tmp_path, release, install, public_key=public, recovery_notify=captured.append)
    assert len(captured) == 1
    return install, captured[0]


def _run(install, descriptor):
    env = dict(os.environ, PYTHONPATH=str(install), PYTHONSTARTUP=str(install / "poison.py"))
    return subprocess.run(descriptor["command_argv"], cwd=install, env=env,
                          text=True, capture_output=True, timeout=20)


def _snapshot(install):
    return {str(p.relative_to(install)): p.read_bytes()
            for p in install.rglob("*") if p.is_file()}


@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_independent_subprocess_restores_broken_install(tmp_path, monkeypatch, action):
    install, descriptor = _interrupted(tmp_path, monkeypatch, action)
    # Even importing the package or any updater dependencies is impossible.
    for module in ("__init__", "updater", "storage", "applog"):
        _write(install / "antidetect" / f"{module}.py", "this is invalid Python syntax !!!\n")
    _write(install / "sitecustomize.py", "raise RuntimeError('must not import from target')\n")
    _write(install / "poison.py", "raise RuntimeError('must ignore startup')\n")
    result = _run(install, descriptor)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["action"] == ("rollback_interrupted_apply" if action == "apply" else "finish_interrupted_rollback")
    assert updater.read_installed_version(str(install)) == "1.0.0"
    assert (install / "antidetect" / "storage.py").read_text() == "# old storage\n"
    assert not (tmp_path / "updates" / "pending_update.json").exists()
    again = _run(install, descriptor)
    assert again.returncode == 0
    assert json.loads(again.stdout)["action"] == "nothing_pending"
    if action == "rollback":
        state = json.loads((tmp_path / "updates" / "update_state.json").read_text())
        assert [entry["action"] for entry in state["history"]] == ["apply", "rollback"]


@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_independent_subprocess_preserves_committed_operation(tmp_path, monkeypatch, action):
    install, descriptor = _interrupted(tmp_path, monkeypatch, action, committed=True)
    before = _snapshot(install)
    result = _run(install, descriptor)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["action"] == "finish_committed_update"
    assert before == _snapshot(install)


@pytest.mark.parametrize("damage", ["bootstrap", "capsule", "journal", "backup_manifest", "backup_file"])
def test_corruption_fails_before_restore_or_untrusted_execution(tmp_path, monkeypatch, damage):
    install, descriptor = _interrupted(tmp_path, monkeypatch)
    capsule_dir = Path(descriptor["capsule_dir"])
    journal = tmp_path / "updates" / "pending_update.json"
    pending = json.loads(journal.read_text())
    backup = Path(pending["backup_dir"])
    sentinel = tmp_path / "executed-untrusted-code"
    if damage == "bootstrap":
        (capsule_dir / "bootstrap.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")
    elif damage == "capsule":
        (capsule_dir / "capsule.json").write_text("{}")
    elif damage == "journal":
        pending["install_root"] = str(tmp_path)
        journal.write_text(json.dumps(pending))
    elif damage == "backup_manifest":
        (backup / "backup.json").write_text("{}")
    else:
        (backup / "files" / "antidetect" / "__init__.py").write_text("corrupt")
    before = _snapshot(install)
    result = _run(install, descriptor)
    assert result.returncode != 0
    assert not sentinel.exists()
    assert before == _snapshot(install)
    assert journal.exists()
    assert str(install) not in result.stdout + result.stderr


@pytest.mark.parametrize("server_state", ["live_pid", "unknown_pid", "malformed", "listening"])
def test_independent_recovery_blocks_live_or_unknown_server(tmp_path, monkeypatch, server_state):
    install, descriptor = _interrupted(tmp_path, monkeypatch)
    pid_file = install / "data" / "server.pid"
    listener = None
    if server_state == "live_pid":
        pid_file.write_text(json.dumps({"pid": os.getpid(), "port": 8000}))
    elif server_state == "unknown_pid":
        pid_file.write_text(json.dumps({"pid": -1, "port": 8000}))
    elif server_state == "malformed":
        pid_file.write_text("{not-json")
    else:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        child = subprocess.run([sys.executable, "-I", "-S", "-c", "import os;print(os.getpid())"], capture_output=True, text=True, check=True)
        pid_file.write_text(json.dumps({"pid": int(child.stdout), "port": listener.getsockname()[1]}))
    before = _snapshot(install)
    try:
        result = _run(install, descriptor)
        assert result.returncode != 0
        assert before == _snapshot(install)
        assert (tmp_path / "updates" / "pending_update.json").exists()
    finally:
        if listener:
            listener.close()


def test_independent_recovery_uses_same_cross_process_lock(tmp_path, monkeypatch):
    install, descriptor = _interrupted(tmp_path, monkeypatch)
    before = _snapshot(install)
    with InterProcessLock(str(tmp_path / "updates" / "update.lock"), timeout=1):
        result = _run(install, descriptor)
    assert result.returncode != 0
    assert "lock is busy" in result.stdout
    assert before == _snapshot(install)
    assert _run(install, descriptor).returncode == 0


@pytest.mark.parametrize("location", ["backup", "target", "capsule"])
def test_independent_recovery_rejects_junctions(tmp_path, monkeypatch, location):
    install, descriptor = _interrupted(tmp_path, monkeypatch)
    capsule = Path(descriptor["capsule_dir"])
    config = json.loads((capsule / "capsule.json").read_text())
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    if location == "backup":
        link = Path(config["backup_dir"]) / "files"
    elif location == "target":
        link = install / "web"
    else:
        link = capsule
    moved = link.with_name(link.name + "-original")
    link.rename(moved)
    # Capsule junction still exposes genuine hashed code, whose path guard rejects it.
    _directory_link(link, moved if location == "capsule" else outside)
    try:
        before = _snapshot(install)
        result = _run(install, descriptor)
        assert result.returncode != 0
        assert before == _snapshot(install)
        assert sentinel.read_bytes() == b"keep"
    finally:
        _unlink_directory_link(link)
        moved.rename(link)


@pytest.mark.parametrize("action", ["apply", "rollback"])
def test_independent_recovery_can_be_interrupted_twice(tmp_path, monkeypatch, action):
    install, descriptor = _interrupted(tmp_path, monkeypatch, action)
    # Test-only trusted wrapper executes the genuine bytes after checking their
    # digest, and exits after the first durable copy without production hooks.
    command = list(descriptor["command_argv"])
    command[4] = "\n".join([
        "import hashlib,os,pathlib,sys",
        "p=pathlib.Path(sys.argv[1]);b=p.read_bytes()",
        "assert hashlib.sha256(b).hexdigest()==sys.argv[2]",
        "scope={'__name__':'recovery_test','__file__':str(p)}",
        "exec(compile(b,str(p),'exec'),scope)",
        "original=scope['copy_verified']",
        "def interrupted(*args):",
        " original(*args)",
        " os._exit(71)",
        "scope['copy_verified']=interrupted",
        "scope['recover'](sys.argv[3],sys.argv[4])",
    ])
    for _ in range(2):
        result = subprocess.run(command, cwd=install, capture_output=True, text=True, timeout=20)
        assert result.returncode == 71, result.stdout + result.stderr
        assert (tmp_path / "updates" / "pending_update.json").exists()
    result = _run(install, descriptor)
    assert result.returncode == 0, result.stdout + result.stderr
    assert updater.read_installed_version(str(install)) == "1.0.0"


def test_staging_failure_aborts_before_pending_or_install_write(tmp_path, monkeypatch):
    install = _install_tree(tmp_path)
    release = _build(tmp_path, _source_tree(tmp_path), allow_unsigned=True)
    before = _snapshot(install)

    def fail(*_args):
        raise OSError("synthetic external disk failure")

    monkeypatch.setattr(updater, "_stage_recovery_bootstrap", fail)
    with pytest.raises(updater.UpdateError):
        _apply(tmp_path, release, install, allow_unsigned=True)
    assert before == _snapshot(install)
    assert not (tmp_path / "updates" / "pending_update.json").exists()


def test_recovery_notification_is_before_pending_and_install_mutation(tmp_path, monkeypatch):
    install = _install_tree(tmp_path)
    monkeypatch.setenv("ANTIDETECT_DATA_DIR", str(install / "data"))
    release = _build(tmp_path, _source_tree(tmp_path), allow_unsigned=True)
    before = _snapshot(install)
    notifications = []

    def notify(descriptor):
        assert before == _snapshot(install)
        assert not (tmp_path / "updates" / "pending_update.json").exists()
        assert not bootstrap.inside(install, descriptor["capsule_dir"])
        assert Path(descriptor["instructions_path"]).is_file()
        notifications.append(descriptor)

    result = _apply(tmp_path, release, install, allow_unsigned=True, recovery_notify=notify)
    assert result["recovery_bootstrap"] == notifications[0]
    assert json.loads(_run(install, notifications[0]).stdout)["action"] == "nothing_pending"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell and Windows command quoting contract")
def test_recovery_powershell_command_preserves_unicode_spaces_and_literal_quotes(tmp_path, monkeypatch):
    fixture = tmp_path / "Phục hồi 'quoted' space"
    fixture.mkdir()
    install, descriptor = _interrupted(fixture, monkeypatch)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", descriptor["command_powershell"]],
        cwd=install, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["action"] == "rollback_interrupted_apply"
    assert updater.read_installed_version(str(install)) == "1.0.0"


@pytest.mark.skipif(os.name != "nt", reason="Windows short path alias contract")
def test_independent_recovery_canonicalizes_short_capsule_alias(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    install, descriptor = _interrupted(tmp_path, monkeypatch)
    get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short_path.restype = wintypes.DWORD
    command = list(descriptor["command_argv"])
    changed = False
    for index in (5, 7):
        output = ctypes.create_unicode_buffer(32768)
        length = get_short_path(command[index], output, len(output))
        if not length or length >= len(output):
            pytest.skip("Filesystem does not expose a short-path alias")
        changed = changed or output.value.casefold() != command[index].casefold()
        command[index] = output.value
    if not changed:
        pytest.skip("Filesystem does not expose a distinct short-path alias")
    result = subprocess.run(command, cwd=install, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["action"] == "rollback_interrupted_apply"
    assert updater.read_installed_version(str(install)) == "1.0.0"
