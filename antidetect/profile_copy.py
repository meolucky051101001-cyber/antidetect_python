"""Staging copy, verify, promote và journal cho duplicate profile (QĐ-3).

Module thuần filesystem: không đụng metadata, secret hay trạng thái profile.
Storage sẽ orchestrate các bước này ở M2.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

IGNORE_NAMES = frozenset(
    {
        "lockfile",
        "DevToolsActivePort",
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
    }
)
COPY_PHASES = (
    "staging",
    "copied",
    "promoted",
    "secret_written",
    "committed",
    "cleanup_pending",
)
OPERATIONS_DIRNAME = "copy_operations"
RESULT_TTL_SECONDS = 24 * 60 * 60
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SECRET_KEY_RE = re.compile(r"(password|passwd|secret|token|credential)", re.IGNORECASE)
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ProfileCopyError(RuntimeError):
    code = "COPY_FAILED"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class CopyNoSpaceError(ProfileCopyError):
    code = "COPY_NO_SPACE"

    def __init__(self, free: int, required: int):
        self.free = free
        self.required = required
        super().__init__(
            f"Không đủ dung lượng: cần {required} bytes, chỉ còn {free} bytes"
        )


class CopyReparsePointError(ProfileCopyError):
    code = "COPY_REPARSE_POINT"


class CopyVerificationError(ProfileCopyError):
    code = "COPY_VERIFY_FAILED"


class CopyTargetExistsError(ProfileCopyError):
    code = "COPY_TARGET_EXISTS"


class CopyJournalError(ProfileCopyError):
    code = "COPY_JOURNAL_FAILED"


@dataclass
class CopyStats:
    total_files: int = 0
    total_bytes: int = 0
    ignored_entries: int = 0


def new_operation_id() -> str:
    return uuid.uuid4().hex


def _validate_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not _OPERATION_ID_RE.match(operation_id):
        raise ValueError(f"operation_id không hợp lệ: {operation_id!r}")
    return operation_id


def validate_idempotency_key(value: str) -> str:
    """Accept the 8–64 character operation key clients send in Idempotency-Key."""
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_RE.match(value):
        raise ProfileCopyError(
            "Idempotency-Key phải dài 8–64 ký tự chữ, số, '_' hoặc '-'.",
            code="INVALID_IDEMPOTENCY_KEY",
        )
    return value


def validate_profile_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.match(value):
        raise ProfileCopyError(
            f"Profile ID không hợp lệ: {value!r}",
            code="COPY_JOURNAL_UNSAFE",
        )
    return value


def operations_dir(data_dir: str) -> str:
    return os.path.join(os.path.abspath(data_dir), OPERATIONS_DIRNAME)


def journal_path(data_dir: str, operation_id: str) -> str:
    return os.path.join(operations_dir(data_dir), f"{_validate_operation_id(operation_id)}.json")


def result_path(data_dir: str, operation_id: str) -> str:
    return os.path.join(
        operations_dir(data_dir),
        f"{_validate_operation_id(operation_id)}.result.json",
    )


def write_operation_result(data_dir: str, operation_id: str, payload: Dict[str, Any]) -> str:
    _reject_secret_fields(payload)
    path = result_path(data_dir, operation_id)
    _atomic_write_json(path, payload)
    return path


def read_operation_result(data_dir: str, operation_id: str) -> Optional[Dict[str, Any]]:
    path = result_path(data_dir, operation_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_operation_journals(data_dir: str) -> List[Dict[str, Any]]:
    """Load every journal file; corrupt/partial files are skipped, never deleted."""
    directory = operations_dir(data_dir)
    if not os.path.isdir(directory):
        return []
    journals: List[Dict[str, Any]] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(".json") or entry.name.endswith(".result.json"):
            continue
        if ".tmp." in entry.name:
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            with open(entry.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("operation_id"):
            journals.append(data)
    return journals


def remove_operation_files(data_dir: str, operation_id: str) -> None:
    for path in (
        journal_path(data_dir, operation_id),
        result_path(data_dir, operation_id),
    ):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CopyJournalError(
                f"Không xóa được file operation: {type(exc).__name__}"
            ) from exc


def purge_expired_operations(
    data_dir: str,
    ttl_seconds: int = RESULT_TTL_SECONDS,
    now: Optional[float] = None,
) -> int:
    """Drop committed results older than the TTL.

    Journals in a non-terminal phase are left for recovery; cleanup_pending is
    retained so the UI keeps reporting unfinished cleanup.
    """
    if ttl_seconds < 0:
        raise ValueError("ttl_seconds không được âm")
    directory = operations_dir(data_dir)
    if not os.path.isdir(directory):
        return 0
    cutoff = (time.time() if now is None else now) - ttl_seconds
    removed = 0
    results = {
        entry.name[: -len(".result.json")]
        for entry in os.scandir(directory)
        if entry.name.endswith(".result.json")
    }
    for entry in list(os.scandir(directory)):
        if ".tmp." in entry.name or not entry.name.endswith(".json"):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if entry.name.endswith(".result.json"):
            try:
                os.remove(entry.path)
                removed += 1
            except OSError:
                pass
            continue
        operation_id = entry.name[: -len(".json")]
        try:
            with open(entry.path, "r", encoding="utf-8") as handle:
                journal = json.load(handle)
        except (OSError, ValueError):
            continue
        if journal.get("phase") != "committed":
            continue
        try:
            os.remove(entry.path)
            removed += 1
        except OSError:
            continue
        if operation_id in results:
            try:
                os.remove(result_path(data_dir, operation_id))
                removed += 1
            except (OSError, ProfileCopyError):
                pass
    return removed


def pid_is_alive(pid: int) -> bool:
    """Liveness probe that never signals/kills the target process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        # Access denied still means the PID exists.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def journal_owner_alive(journal: Dict[str, Any]) -> bool:
    owner = str(journal.get("owner") or "")
    pid_text = owner.split(":", 1)[0]
    try:
        return pid_is_alive(int(pid_text))
    except (TypeError, ValueError):
        # Unknown owner format: be conservative and leave it to the TTL.
        return True


def _is_within(base: str, path: str) -> bool:
    base_norm = os.path.normcase(os.path.realpath(base))
    path_norm = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([base_norm, path_norm]) == base_norm
    except ValueError:
        return False


def operation_paths(
    data_dir: str, profiles_dir: str, journal: Dict[str, Any]
) -> Tuple[str, str, str, str]:
    """Return validated (source_dir, target_dir, staging_root, target_user_data).

    Recovery only accepts paths derived from the journal's IDs, so a tampered
    journal cannot redirect deletion outside the profile workspace.
    """
    operation_id = _validate_operation_id(str(journal.get("operation_id") or ""))
    source_id = validate_profile_id(str(journal.get("source_id") or ""))
    target_id = validate_profile_id(str(journal.get("target_id") or ""))
    if target_id == source_id:
        raise CopyJournalError(
            "Journal không được dùng cùng một ID cho nguồn và đích",
            code="COPY_JOURNAL_UNSAFE",
        )
    profiles_root = os.path.abspath(profiles_dir)
    source_dir = os.path.join(profiles_root, source_id)
    target_dir = os.path.join(profiles_root, target_id)
    staging_root = os.path.join(profiles_root, ".staging", operation_id)
    target_user_data = os.path.join(target_dir, "user_data")

    recorded_source = journal.get("source_dir")
    recorded_target = journal.get("target_dir")
    if recorded_source and os.path.normcase(os.path.realpath(str(recorded_source))) != os.path.normcase(
        os.path.realpath(source_dir)
    ):
        raise CopyJournalError("Đường dẫn nguồn trong journal không hợp lệ",
                               code="COPY_JOURNAL_UNSAFE")
    if recorded_target and os.path.normcase(os.path.realpath(str(recorded_target))) != os.path.normcase(
        os.path.realpath(target_user_data)
    ):
        raise CopyJournalError("Đường dẫn đích trong journal không hợp lệ",
                               code="COPY_JOURNAL_UNSAFE")
    recorded_staging = journal.get("staging_dir")
    if recorded_staging and not _is_within(staging_root, str(recorded_staging)):
        raise CopyJournalError("Đường dẫn staging trong journal không hợp lệ",
                               code="COPY_JOURNAL_UNSAFE")
    return source_dir, target_dir, staging_root, target_user_data


def remove_operation_target(profiles_dir: str, target_id: str) -> bool:
    target_id = validate_profile_id(target_id)
    target_dir = os.path.join(os.path.abspath(profiles_dir), target_id)
    _require_no_reparse(target_dir)
    if os.path.normcase(os.path.dirname(os.path.realpath(target_dir))) != os.path.normcase(
        os.path.realpath(profiles_dir)
    ):
        raise ProfileCopyError("Đường dẫn đích nằm ngoài profiles",
                               code="COPY_CLEANUP_UNSAFE")
    if not os.path.exists(target_dir):
        return False
    if not os.path.isdir(target_dir) or os.path.islink(target_dir):
        raise ProfileCopyError("Đích cần dọn không phải thư mục hợp lệ",
                               code="COPY_CLEANUP_UNSAFE")
    _remove_tree_checked(target_dir)
    return True


def cleanup_operation_root(profiles_dir: str, operation_id: str) -> bool:
    operation_id = _validate_operation_id(operation_id)
    root = os.path.join(os.path.abspath(profiles_dir), ".staging", operation_id)
    return cleanup_staging(root, profiles_dir)


def _is_reparse_stat(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG)


def _entry_is_reparse(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    return _is_reparse_stat(entry.stat(follow_symlinks=False))


def _require_no_reparse(path: str) -> None:
    """Validate lexical ancestors before reads, promotion or cleanup.

    This closes prepared junction paths; same-user path replacement races are
    outside a filesystem snapshot's isolation guarantee.
    """
    current = os.path.abspath(path)
    ancestors = []
    while True:
        ancestors.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for component in reversed(ancestors):
        try:
            value = os.lstat(component)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or _is_reparse_stat(value):
            raise CopyReparsePointError("Reparse point/symlink trong đường dữ liệu")


def _remove_tree_checked(path: str) -> None:
    _require_no_reparse(path)
    for directory, dirs, files in os.walk(path, followlinks=False):
        _require_no_reparse(directory)
        for name in dirs + files:
            _require_no_reparse(os.path.join(directory, name))
    _require_no_reparse(path)
    shutil.rmtree(path)


def _require_real_dir(path: str, label: str) -> None:
    _require_no_reparse(path)
    if not os.path.isdir(path):
        raise ProfileCopyError(f"{label} không tồn tại hoặc không phải thư mục")
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or _is_reparse_stat(st):
        raise CopyReparsePointError(f"{label} là reparse point/symlink: {path}")


def _measure_dir(path: str, stats: CopyStats) -> None:
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name in IGNORE_NAMES:
                stats.ignored_entries += 1
                continue
            if _entry_is_reparse(entry):
                raise CopyReparsePointError(
                    f"Reparse point trong nguồn: {entry.path}"
                )
            if entry.is_dir(follow_symlinks=False):
                _measure_dir(entry.path, stats)
            elif entry.is_file(follow_symlinks=False):
                stats.total_files += 1
                stats.total_bytes += entry.stat(follow_symlinks=False).st_size
            else:
                raise ProfileCopyError(f"Loại entry không hỗ trợ: {entry.path}")


def measure_tree(source_dir: str) -> CopyStats:
    _require_real_dir(source_dir, "Nguồn")
    stats = CopyStats()
    _measure_dir(source_dir, stats)
    return stats


def check_disk_space(
    source_size: int,
    target_dir: str,
    safety_ratio: float = 0.10,
    minimum_free: int = 512 * 1024 * 1024,
) -> int:
    if source_size < 0:
        raise ValueError("source_size không được âm")
    if safety_ratio < 0 or minimum_free < 0:
        raise ValueError("safety_ratio/minimum_free không được âm")
    if not os.path.isdir(target_dir):
        raise ProfileCopyError("Thư mục đích kiểm tra dung lượng không tồn tại")
    free = shutil.disk_usage(target_dir).free
    required = source_size + int(source_size * safety_ratio) + int(minimum_free)
    if free < required:
        raise CopyNoSpaceError(free=free, required=required)
    return free


def prepare_staging(profiles_dir: str, operation_id: str) -> str:
    _validate_operation_id(operation_id)
    staging_root = os.path.join(
        os.path.abspath(profiles_dir), ".staging", operation_id
    )
    _require_no_reparse(staging_root)
    if os.path.exists(staging_root):
        if not os.path.isdir(staging_root):
            raise CopyTargetExistsError("Đường dẫn staging đã tồn tại và không phải thư mục")
        if os.listdir(staging_root):
            raise CopyTargetExistsError("Staging của operation đã có dữ liệu")
    os.makedirs(staging_root, exist_ok=True)
    _require_no_reparse(staging_root)
    return os.path.join(staging_root, "user_data")


def _copy_dir_contents(source: str, target: str, stats: CopyStats) -> None:
    with os.scandir(source) as entries:
        for entry in entries:
            if entry.name in IGNORE_NAMES:
                stats.ignored_entries += 1
                continue
            if _entry_is_reparse(entry):
                raise CopyReparsePointError(
                    f"Reparse point trong nguồn: {entry.path}"
                )
            dest = os.path.join(target, entry.name)
            if entry.is_dir(follow_symlinks=False):
                os.mkdir(dest)
                _copy_dir_contents(entry.path, dest, stats)
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(entry.path, dest, follow_symlinks=False)
                stats.total_files += 1
                stats.total_bytes += entry.stat(follow_symlinks=False).st_size
            else:
                raise ProfileCopyError(f"Loại entry không hỗ trợ: {entry.path}")


@dataclass(frozen=True)
class _CopyEntry:
    kind: str
    size: int
    sha256: str
    identity: Tuple[int, int, int, int, int]


def _stat_identity(value: os.stat_result) -> Tuple[int, int, int, int, int]:
    # atime changes when we read; inode, size and write times must remain stable.
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _hash_stable_file(path: str, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stat_identity(opened) != _stat_identity(before):
            raise CopyVerificationError("File nguồn thay đổi trước khi đọc")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if _stat_identity(os.fstat(handle.fileno())) != _stat_identity(before):
            raise CopyVerificationError("File nguồn thay đổi trong khi đọc")
    after = os.lstat(path)
    if stat.S_ISLNK(after.st_mode) or _is_reparse_stat(after):
        raise CopyReparsePointError("File bị thay bằng reparse point khi đọc")
    if _stat_identity(after) != _stat_identity(before):
        raise CopyVerificationError("File nguồn bị thay trong khi đọc")
    return digest.hexdigest()


def _inventory(root: str) -> Dict[str, _CopyEntry]:
    """Hash copied entries, including empty directories, without storing secrets.

    Callers must already hold the lifecycle lock and prove the source is idle.
    Snapshots detect observed changes; they cannot lock out an external writer.
    """
    _require_real_dir(root, "Thư mục")
    inventory: Dict[str, _CopyEntry] = {}

    def walk(path: str) -> None:
        before_dir = os.lstat(path)
        if stat.S_ISLNK(before_dir.st_mode) or _is_reparse_stat(before_dir):
            raise CopyReparsePointError("Reparse point trong cây dữ liệu")
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name in IGNORE_NAMES:
                    continue
                before = os.lstat(entry.path)
                if stat.S_ISLNK(before.st_mode) or _is_reparse_stat(before):
                    raise CopyReparsePointError("Reparse point trong cây dữ liệu")
                rel = os.path.relpath(entry.path, root)
                if stat.S_ISDIR(before.st_mode):
                    walk(entry.path)
                elif stat.S_ISREG(before.st_mode):
                    inventory[rel] = _CopyEntry(
                        "file", before.st_size, _hash_stable_file(entry.path, before),
                        _stat_identity(before),
                    )
                else:
                    raise ProfileCopyError("Loại entry không hỗ trợ")
        after_dir = os.lstat(path)
        if _stat_identity(before_dir) != _stat_identity(after_dir):
            raise CopyVerificationError("Cây thư mục thay đổi trong khi kiểm tra")
        inventory[os.path.relpath(path, root)] = _CopyEntry(
            "directory", 0, "", _stat_identity(after_dir),
        )

    walk(root)
    return inventory


def verify_copy(
    source_user_data: str,
    staging_user_data: str,
    *,
    expected_source: Optional[Dict[str, _CopyEntry]] = None,
) -> CopyStats:
    source = _inventory(source_user_data)
    if expected_source is not None and source != expected_source:
        raise CopyVerificationError("Nguồn thay đổi trong quá trình sao chép")
    staged = _inventory(staging_user_data)
    missing = len(set(source) - set(staged))
    extra = len(set(staged) - set(source))
    if missing or extra:
        raise CopyVerificationError(
            f"Cây dữ liệu không khớp: thiếu {missing} entry, thừa {extra} entry"
        )
    mismatched = sum(
        1 for rel, entry in source.items()
        if (entry.kind, entry.size, entry.sha256) !=
           (staged[rel].kind, staged[rel].size, staged[rel].sha256)
    )
    if mismatched:
        raise CopyVerificationError(f"{mismatched} entry sai loại/kích thước/SHA-256")
    if _inventory(source_user_data) != source:
        raise CopyVerificationError("Nguồn thay đổi trong quá trình xác minh")
    files = [entry for entry in source.values() if entry.kind == "file"]
    return CopyStats(total_files=len(files), total_bytes=sum(entry.size for entry in files))


def copy_user_data(source_user_data: str, staging_user_data: str) -> CopyStats:
    _require_real_dir(source_user_data, "Nguồn user_data")
    _require_no_reparse(staging_user_data)
    if os.path.exists(staging_user_data):
        raise CopyTargetExistsError("Đích staging đã tồn tại")
    parent = os.path.dirname(os.path.abspath(staging_user_data))
    os.makedirs(parent, exist_ok=True)
    _require_no_reparse(staging_user_data)
    try:
        os.mkdir(staging_user_data)
    except FileExistsError as exc:
        raise CopyTargetExistsError("Đích staging đã tồn tại") from exc
    except OSError as exc:
        raise ProfileCopyError(f"Không tạo được staging: {exc}") from exc

    stats = CopyStats()
    try:
        source_snapshot = _inventory(source_user_data)
        _copy_dir_contents(source_user_data, staging_user_data, stats)
        verify_copy(source_user_data, staging_user_data, expected_source=source_snapshot)
    except BaseException:
        try:
            _remove_tree_checked(staging_user_data)
        except (OSError, ProfileCopyError):
            # Storage's operation recovery reports retained staging separately.
            pass
        raise
    return stats


def promote_staging(staging_user_data: str, target_user_data: str) -> None:
    _require_no_reparse(staging_user_data)
    _require_no_reparse(target_user_data)
    if not os.path.isdir(staging_user_data):
        raise ProfileCopyError("Staging không tồn tại để promote")
    if os.path.exists(target_user_data):
        raise CopyTargetExistsError("Đích đã tồn tại; không ghi đè")
    parent = os.path.dirname(os.path.abspath(target_user_data))
    os.makedirs(parent, exist_ok=True)
    delay = 0.05
    for attempt in range(3):
        try:
            _require_no_reparse(staging_user_data)
            _require_no_reparse(target_user_data)
            os.rename(staging_user_data, target_user_data)
            return
        except OSError as exc:
            if attempt == 2:
                raise ProfileCopyError(
                    f"Promote staging thất bại: {type(exc).__name__}",
                    code="COPY_PROMOTE_FAILED",
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def cleanup_staging(operation_dir: str, profiles_dir: str) -> bool:
    base = os.path.normcase(
        os.path.abspath(os.path.join(profiles_dir, ".staging"))
    )
    target = os.path.normcase(os.path.abspath(operation_dir))
    try:
        inside = os.path.commonpath([base, target]) == base
    except ValueError:
        inside = False
    if not inside or target == base or os.path.dirname(target) != base:
        raise ProfileCopyError(
            "Đường dẫn cleanup không thuộc .staging/<operation>",
            code="COPY_CLEANUP_UNSAFE",
        )
    _require_no_reparse(target)
    if not os.path.exists(target):
        return False
    if not os.path.isdir(target):
        raise ProfileCopyError("Đường dẫn cleanup không phải thư mục")
    _remove_tree_checked(target)
    return True


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                raise ValueError(f"Journal không được chứa secret: {key}")
            _reject_secret_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_fields(item)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception as exc:
        raise CopyJournalError(
            f"Không ghi được journal: {type(exc).__name__}"
        ) from exc
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


class CopyJournal:
    def __init__(
        self,
        path: str,
        *,
        operation_id: str,
        source_id: str,
        target_id: str,
        owner: Optional[str] = None,
        source_dir: str = "",
        staging_dir: str = "",
        target_dir: str = "",
    ):
        _validate_operation_id(operation_id)
        self.path = os.path.abspath(path)
        self._data: Dict[str, Any] = {
            "operation_id": operation_id,
            "source_id": source_id,
            "target_id": target_id,
            "owner": owner or f"{os.getpid()}:{threading.get_ident()}",
            "source_dir": source_dir,
            "staging_dir": staging_dir,
            "target_dir": target_dir,
            "phase": None,
        }

    def read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise CopyJournalError("Journal chưa tồn tại") from exc
        except (OSError, ValueError) as exc:
            raise CopyJournalError(
                f"Journal không đọc được: {type(exc).__name__}"
            ) from exc
        if not isinstance(data, dict):
            raise CopyJournalError("Journal phải là một object JSON")
        return data

    def write(self, phase: str = "staging", **extra: Any) -> Dict[str, Any]:
        if phase not in COPY_PHASES:
            raise ValueError(f"Phase không hợp lệ: {phase!r}")
        _reject_secret_fields(extra)
        now = _utc_now()
        data = dict(self._data)
        data.update(extra)
        data["phase"] = phase
        data["created_at"] = data.get("created_at") or now
        data["updated_at"] = now
        _atomic_write_json(self.path, data)
        self._data = data
        return dict(data)

    def update(self, phase: str, **extra: Any) -> Dict[str, Any]:
        if phase not in COPY_PHASES:
            raise ValueError(f"Phase không hợp lệ: {phase!r}")
        _reject_secret_fields(extra)
        current = self.read()
        if current.get("operation_id") != self._data["operation_id"]:
            raise CopyJournalError("operation_id trong journal không khớp")
        current.update(extra)
        current["phase"] = phase
        current["updated_at"] = _utc_now()
        _atomic_write_json(self.path, current)
        self._data = current
        return dict(current)
