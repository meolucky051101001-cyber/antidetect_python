"""Secret store for sensitive credentials (e.g. proxy passwords), protected by Windows DPAPI."""
import ctypes
import json
import os
import shutil
import sys
import time
import threading
import uuid
from pathlib import Path
from ctypes import wintypes
from typing import Optional, Dict, Any, List

from .applog import get_logger

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ANTIDETECT_DATA_DIR") or os.path.join(BASE_DIR, "data")
SECRETS_FILE = Path(DATA_DIR) / "secrets.dpapi"
LOCK_FILE = Path(DATA_DIR) / "secrets.lock"

log = get_logger("secrets")

_lock = threading.RLock()
_store_corrupted = False


class SecretStoreCorruption(RuntimeError):
    """Raised when secrets.dpapi exists but cannot be decrypted/parsed.

    Callers must not overwrite the store in this state; the file may still be
    recoverable with the user's Windows credentials.
    """


def _safe_log(message: str) -> None:
    log.warning(message)


class _StoreLock:
    """Small cross-process lock dedicated to secrets.dpapi."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        start = time.monotonic()
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        while time.monotonic() - start < self.timeout:
            try:
                self.fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, IOError):
                if self.fd is not None:
                    try:
                        os.close(self.fd)
                    except OSError:
                        pass
                    self.fd = None
                time.sleep(0.05)
        raise TimeoutError("Không lấy được khóa secrets.dpapi sau thời gian chờ.")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for secret storage")
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    func = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    func.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    func.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def _target_file() -> Path:
    return Path(os.environ.get("ANTIDETECT_DATA_DIR") or DATA_DIR) / "secrets.dpapi"


def _backup_corrupt_store(target: Path) -> Optional[Path]:
    try:
        backup = target.with_name(f"{target.name}.corrupt.{time.time_ns()}")
        shutil.copy2(target, backup)
        return backup
    except Exception:
        return None


def _backup_secret_store(target: Path) -> None:
    """H2: backup timestamped trước mỗi lần ghi đè secrets.dpapi (giữ 5 bản).

    Nội dung đã được DPAPI encrypt nên bản backup an toàn như file gốc. Nếu
    một lần ghi bị đứt giữa chừng (mất điện/BSOD), bản backup gần nhất vẫn
    giải mã được — secrets.dpapi trước đó không có cơ chế backup nào.
    """
    try:
        if not target.exists() or target.stat().st_size == 0:
            return
        backup_dir = target.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup = backup_dir / f"secrets_{ts}_{uuid.uuid4().hex[:6]}.dpapi"
        shutil.copy2(target, backup)
        backups = sorted(
            (
                p for p in backup_dir.iterdir()
                if p.name.startswith("secrets_") and p.name.endswith(".dpapi")
            ),
            key=lambda p: p.stat().st_mtime,
        )
        while len(backups) > 5:
            oldest = backups.pop(0)
            try:
                oldest.unlink()
            except OSError:
                pass
    except Exception as e:
        # Backup fail không được chặn ghi: file gốc vẫn còn tới os.replace.
        _safe_log(f"[Secrets] Cảnh báo backup kho secret: {type(e).__name__}: {e}")


def _read_all_secrets() -> Dict[str, Any]:
    """Read and decrypt the store.

    A missing/empty store is a valid empty state. A present but undecryptable
    file raises SecretStoreCorruption after making a recovery copy.
    """
    global _store_corrupted
    with _lock:
        target = _target_file()
        if not target.exists():
            _store_corrupted = False
            return {}
        try:
            raw = target.read_bytes()
            if not raw:
                _store_corrupted = False
                return {}
            decrypted = _crypt(raw, False)
            data = json.loads(decrypted.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Secret store root must be an object")
            _store_corrupted = False
            return data
        except Exception as e:
            # Chỉ backup một lần cho mỗi đợt hỏng; các lần đọc fail kế tiếp
            # không tạo thêm bản sao .corrupt. trùng lặp.
            backup = None
            if not _store_corrupted:
                backup = _backup_corrupt_store(target)
            _store_corrupted = True
            hint = f" Bản sao: {backup}." if backup else ""
            raise SecretStoreCorruption(
                "secrets.dpapi bị hỏng hoặc không giải mã được."
                + hint
                + " Không ghi đè kho secret cho tới khi khôi phục xong."
            ) from e


def _write_all_secrets(store: Dict[str, Any]):
    global _store_corrupted
    if _store_corrupted:
        raise SecretStoreCorruption(
            "secrets.dpapi đang ở trạng thái hỏng; từ chối ghi đè."
        )
    with _lock:
        data_dir = Path(os.environ.get("ANTIDETECT_DATA_DIR") or DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "secrets.dpapi"
        pending = target.with_name(f"{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            payload = json.dumps(store, ensure_ascii=False).encode("utf-8")
            encrypted = _crypt(payload, True)
            # H2: fsync trước replace — mất điện giữa chừng không để lại
            # file rỗng/cụt ciphertext cho kho mật khẩu duy nhất.
            with open(pending, "wb") as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            _backup_secret_store(target)
            os.replace(pending, target)
        except Exception as e:
            if pending.exists():
                try:
                    pending.unlink()
                except OSError:
                    pass
            raise e


def _entry_for(store: Dict[str, Any], profile_id: str) -> Dict[str, Any]:
    entry = store.get(str(profile_id))
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        # Legacy format: {profile_id: "password"}.
        return {"proxy_password": entry}
    return {}


def is_corrupted() -> bool:
    return _store_corrupted


def get_proxy_password(profile_id: str, identity: Optional[str] = None) -> Optional[str]:
    """Get a proxy password, optionally bound to a proxy identity.

    When the stored identity differs from the requested identity, the password
    is stale and must not be returned. This is the last line of defence behind
    ProfileManager.update_profile().
    """
    if not profile_id:
        return None
    try:
        store = _read_all_secrets()
    except SecretStoreCorruption as e:
        _safe_log(f"[Secrets] Không đọc được kho secret: {e}")
        return None
    entry = _entry_for(store, str(profile_id))
    if not entry:
        return None
    stored_identity = entry.get("proxy_identity")
    if identity and stored_identity and stored_identity != identity:
        return None
    password = entry.get("proxy_password")
    return password if isinstance(password, str) and password else None


def set_proxy_password(profile_id: str, password: Optional[str], identity: Optional[str] = None):
    """Store a proxy password bound to its proxy identity.

    ``password=None`` removes the entry. A blank string is treated as None;
    callers that mean "keep existing" must read it first.
    """
    if not profile_id:
        return
    with _lock, _StoreLock():
        store = _read_all_secrets()
        if password is None or not str(password).strip():
            if str(profile_id) in store:
                store.pop(str(profile_id), None)
                _write_all_secrets(store)
            return

        entry = _entry_for(store, str(profile_id))
        entry["proxy_password"] = str(password)
        if identity:
            entry["proxy_identity"] = identity
        else:
            entry.pop("proxy_identity", None)
        store[str(profile_id)] = entry
        _write_all_secrets(store)


def delete_proxy_password(profile_id: str):
    """Remove a profile's secret only when the store can be read safely."""
    if not profile_id:
        return
    with _lock, _StoreLock():
        store = _read_all_secrets()
        if str(profile_id) in store:
            store.pop(str(profile_id), None)
            _write_all_secrets(store)


def migrate_plaintext_passwords(profiles: List[Any]) -> bool:
    """Move plaintext passwords from profile objects into DPAPI.

    Returns True when at least one password was migrated.
    """
    from .models import REDACTED_SECRET

    migrated_any = False
    with _lock, _StoreLock():
        store = _read_all_secrets()
        for p in profiles:
            p_id = getattr(p, "id", None)
            proxy = getattr(p, "proxy", None)
            if not p_id or not proxy:
                continue
            pwd = getattr(proxy, "password", None)
            if pwd and pwd != REDACTED_SECRET and str(pwd).strip():
                entry = _entry_for(store, str(p_id))
                entry["proxy_password"] = str(pwd)
                identity_fn = getattr(proxy, "identity_key", None)
                if callable(identity_fn):
                    entry["proxy_identity"] = identity_fn()
                store[str(p_id)] = entry
                migrated_any = True

        if migrated_any:
            _write_all_secrets(store)
    return migrated_any
