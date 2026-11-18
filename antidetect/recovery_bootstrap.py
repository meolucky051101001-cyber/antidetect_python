"""Independent updater recovery. Stdlib only; never import the target install.

Run only through the digest-checking isolated command staged before mutation.
Path checks do not isolate against hostile code running as the same OS user.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import sys
import time
import uuid

MAX_JSON = 8 * 1024 * 1024
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_FILES = 20000
HEX = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
PROTECTED = {".git", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache",
             "backups", "browsers", "data", "dist", "exports", "htmlcov", "node_modules",
             "temp_dir", "test_out_dir", "venv", "workspace", "workspace_backup_safety"}
SUFFIXES = (".dpapi", ".key", ".lock", ".log", ".p12", ".pem", ".pfx", ".pid", ".pyc", ".pyo", ".zip")
NAMES = {".env", ".coverage", "coverage.xml", "thumbs.db", ".ds_store", "release.json", "release.json.sig"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class RecoveryError(RuntimeError):
    pass


def checked(path):
    if not isinstance(path, (str, os.PathLike)) or not os.path.isabs(path):
        raise RecoveryError("An absolute path is required")
    absolute = Path(os.path.abspath(path))
    for part in reversed((absolute, *absolute.parents)):
        try:
            value = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            raise RecoveryError("Reparse paths are not allowed")
    return Path(os.path.realpath(absolute))


def same_path(left, right):
    return os.path.normcase(str(checked(left))) == os.path.normcase(str(checked(right)))


def inside(root, target):
    root, target = checked(root), checked(target)
    try:
        return os.path.normcase(os.path.commonpath([root, target])) == os.path.normcase(str(root))
    except ValueError:
        return False


def digest(path):
    path = checked(path)
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path, expected=None, optional=False):
    path = checked(path)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_JSON + 1)
    except FileNotFoundError:
        if optional:
            return {}
        raise RecoveryError("Required recovery metadata is missing") from None
    if len(raw) > MAX_JSON or (expected is not None and hashlib.sha256(raw).hexdigest() != expected):
        raise RecoveryError("Recovery metadata hash or size is invalid")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RecoveryError("Recovery metadata is not valid JSON") from None
    if not isinstance(value, dict):
        raise RecoveryError("Recovery metadata must be an object")
    return value


def relative(raw):
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise RecoveryError("Invalid backup path")
    parts = raw.split("/")
    if any(part in ("", ".", "..") or part.rstrip(" .") != part
           or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in part)
           or part.split(".")[0].upper() in RESERVED for part in parts):
        raise RecoveryError("Unsafe backup path")
    low = raw.lower()
    if parts[0].lower() in PROTECTED or any(p.lower() == "__pycache__" for p in parts) or low.endswith(SUFFIXES) or parts[-1].lower() in NAMES:
        raise RecoveryError("Protected backup path")
    return raw


def target(root, rel):
    root = checked(root)
    path = checked(root.joinpath(*relative(rel).split("/")))
    if not inside(root, path) or path.is_dir():
        raise RecoveryError("Backup destination is outside root or a directory")
    return path


def version(root):
    path = target(root, "antidetect/__init__.py")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RecoveryError("Cannot read installed version") from None
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
    if not match or not VERSION.fullmatch(match[1]):
        raise RecoveryError("Installed version is invalid")
    return match[1]


def write_json(path, value):
    path = checked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    checked(path.parent)
    pending = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    try:
        with pending.open("x", encoding="utf-8") as destination:
            json.dump(value, destination, ensure_ascii=True, sort_keys=True)
            destination.flush()
            os.fsync(destination.fileno())
        checked(pending)
        checked(path)
        os.replace(pending, path)
    finally:
        try:
            checked(pending).unlink(missing_ok=True)
        except (OSError, RecoveryError):
            pass


@contextmanager
def update_lock(path, timeout=5):
    path = checked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    checked(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RecoveryError("Updater lock is busy") from None
                time.sleep(0.05)
        checked(path)
        yield
    finally:
        if acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def pid_state(pid):
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return "unknown"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "alive"
        except ProcessLookupError:
            return "absent"
        except OSError:
            return "unknown"
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return "absent" if ctypes.get_last_error() == 87 else "unknown"
    try:
        status = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(status)):
            return "unknown"
        return "alive" if status.value == 259 else "absent"
    finally:
        api.CloseHandle(handle)


def require_server_stopped(pid_file):
    # No requests/capabilities or target install imports. Any listening service
    # on the configured control port blocks recovery, even when health is dead.
    path = checked(pid_file)
    port = 8000
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as source:
                raw = source.read(4097).strip()
            if len(raw) > 4096:
                raise ValueError()
            info = json.loads(raw)
            if type(info) is int:
                info = {"pid": info, "port": 8000}
            if not isinstance(info, dict):
                raise ValueError()
            if pid_state(info.get("pid")) != "absent":
                raise RecoveryError("Recorded server is alive or unverified")
            port = info.get("port", 8000)
            if type(port) is not int or not 0 < port <= 65535:
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            raise RecoveryError("Server state is unreadable or invalid") from None
    # Binding an exclusive socket proves that no control listener owns the
    # endpoint, without contacting any running application. Permission errors,
    # TIME_WAIT or a conflicting bind are conservatively treated as unknown.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        raise RecoveryError("Server endpoint is occupied or unverified; stop it before recovery") from None


def validate_backup(backup_dir, root, expected_hash):
    backup = read_json(backup_dir / "backup.json", expected_hash)
    if type(backup.get("schema")) is not int or backup["schema"] != 1:
        raise RecoveryError("Unsupported backup schema")
    for field in ("from_version", "to_version"):
        if not isinstance(backup.get(field), str) or not VERSION.fullmatch(backup[field]):
            raise RecoveryError("Invalid backup version")
    if not isinstance(backup.get("release_manifest_sha256"), str) or not HEX.fullmatch(backup["release_manifest_sha256"]):
        raise RecoveryError("Invalid release digest")
    if backup.get("install_root") is not None and not same_path(backup["install_root"], root):
        raise RecoveryError("Backup belongs to a different installation")
    entries, additions, seen, total = [], [], set(), 0
    for group in ("files", "delete", "added"):
        rows = backup.get(group)
        if not isinstance(rows, list):
            raise RecoveryError("Backup entries are missing")
        for entry in rows:
            if not isinstance(entry, dict):
                raise RecoveryError("Invalid backup entry")
            rel = relative(entry.get("path"))
            if rel.casefold() in seen or len(seen) >= MAX_FILES:
                raise RecoveryError("Duplicate or excessive backup entries")
            seen.add(rel.casefold())
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not HEX.fullmatch(sha):
                raise RecoveryError("Invalid backup file digest")
            destination = target(root, rel)
            if group == "added":
                additions.append((destination, sha))
                continue
            size = entry.get("size")
            if entry.get("existed") is not True or type(size) is not int or not 0 <= size <= MAX_FILE:
                raise RecoveryError("Invalid backup size or existence")
            total += size
            if total > MAX_TOTAL:
                raise RecoveryError("Backup exceeds size limit")
            source = target(backup_dir / "files", rel)
            if not source.is_file() or source.stat().st_size != size or digest(source) != sha:
                raise RecoveryError("Backup file is missing or corrupt")
            entries.append((source, destination, sha))
    version_source = backup_dir / "files" if any(
        same_path(destination, root / "antidetect" / "__init__.py") for _, destination, _ in entries
    ) else root
    if version(version_source) != backup["from_version"]:
        raise RecoveryError("Backup cannot restore the declared version")
    return backup, entries, additions


def copy_verified(source, destination, sha):
    source, destination = checked(source), checked(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    checked(destination.parent)
    pending = destination.with_name(destination.name + ".recover." + uuid.uuid4().hex)
    try:
        with source.open("rb") as src, pending.open("xb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if digest(pending) != sha:
            raise RecoveryError("Restore copy failed its digest check")
        checked(source)
        checked(destination)
        checked(pending)
        os.replace(pending, destination)
    finally:
        try:
            checked(pending).unlink(missing_ok=True)
        except (OSError, RecoveryError):
            pass


def recover(capsule_file, capsule_sha):
    if not HEX.fullmatch(capsule_sha):
        raise RecoveryError("Invalid capsule digest")
    capsule_file = checked(capsule_file)
    config = read_json(capsule_file, capsule_sha)
    if type(config.get("schema")) is not int or config["schema"] != 1 or not isinstance(config.get("pending"), dict):
        raise RecoveryError("Unsupported recovery capsule")
    expected = config["pending"]
    operation = expected.get("operation_id")
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise RecoveryError("Invalid operation identity")
    if type(expected.get("schema")) is not int or expected["schema"] != 1 or expected.get("action") not in ("apply", "rollback"):
        raise RecoveryError("Invalid operation")
    root, updates, state_path, backup_dir, pid_file = (
        checked(config[key]) for key in ("install_root", "updates_path", "state_file", "backup_dir", "pid_file")
    )
    if inside(root, capsule_file) or not same_path(capsule_file.parent, config.get("capsule_dir")):
        raise RecoveryError("Recovery must be staged outside the installation")
    for field, path in (("install_root", root), ("state_file", state_path), ("backup_dir", backup_dir)):
        if not same_path(expected.get(field), path):
            raise RecoveryError("Recovery path binding failed")
    journal = checked(updates / "pending_update.json")
    if same_path(journal, state_path) or same_path(updates / "update.lock", state_path):
        raise RecoveryError("Recovery metadata paths collide")
    backup_sha = expected.get("backup_manifest_sha256")
    if not isinstance(backup_sha, str) or not HEX.fullmatch(backup_sha):
        raise RecoveryError("Invalid backup binding")
    with update_lock(updates / "update.lock"):
        if not journal.exists():
            # Never restore an old capsule after its operation was cleared.
            return {"success": True, "action": "nothing_pending", "operation_id": operation}
        pending = read_json(journal)
        descriptor = pending.pop("bootstrap", None)
        if pending != expected or descriptor != {"capsule_sha256": capsule_sha, "capsule_dir": str(capsule_file.parent)}:
            raise RecoveryError("Journal does not match this recovery capsule")
        state = read_json(state_path, optional=True)
        backup, entries, additions = validate_backup(backup_dir, root, backup_sha)
        if inside(backup_dir, state_path):
            raise RecoveryError("State collides with backup")
        for destination in [entry[1] for entry in entries] + [entry[0] for entry in additions]:
            if any(same_path(destination, meta) for meta in (state_path, journal, updates / "update.lock", pid_file)):
                raise RecoveryError("Recovery metadata collides with install files")
        if state.get("last_operation_id") == operation:
            wanted = backup["to_version"] if expected["action"] == "apply" else backup["from_version"]
            if state.get("current_version") != wanted or version(root) != wanted:
                raise RecoveryError("Committed state and install version disagree")
            action, restored, removed, kept = "finish_committed_update", 0, 0, 0
        else:
            require_server_stopped(pid_file)
            if expected["action"] == "rollback":
                after = expected.get("state_after")
                if not isinstance(after, dict) or after.get("schema") != 1 or after.get("last_operation_id") != operation or after.get("current_version") != backup["from_version"]:
                    raise RecoveryError("Rollback state binding failed")
            # Recheck all paths and liveness after potentially lengthy hashing.
            require_server_stopped(pid_file)
            for source, destination, sha in entries:
                copy_verified(source, destination, sha)
            removed, kept = 0, 0
            for destination, sha in additions:
                checked(destination)
                if destination.exists():
                    if digest(destination) == sha:
                        checked(destination).unlink()
                        removed += 1
                    else:
                        kept += 1
            if version(root) != backup["from_version"]:
                raise RecoveryError("Restored version is inconsistent")
            if expected["action"] == "rollback":
                write_json(state_path, expected["state_after"])
                action = "finish_interrupted_rollback"
            else:
                action = "rollback_interrupted_apply"
            restored = len(entries)
        current = read_json(journal)
        if current.get("operation_id") != operation or current.get("bootstrap") != descriptor:
            raise RecoveryError("Journal changed; recovery was not finalized")
        checked(journal).unlink()
        return {"success": True, "action": action, "operation_id": operation,
                "restored_count": restored, "removed_added_count": removed, "kept_modified_added_count": kept}


def main():
    try:
        if len(sys.argv) != 3:
            raise RecoveryError("Recovery requires capsule path and trusted digest")
        result = recover(sys.argv[1], sys.argv[2])
    except (RecoveryError, OSError, ValueError, TypeError) as exc:
        # Do not echo raw paths, server metadata, file contents or credentials.
        print(json.dumps({"success": False, "error": str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
