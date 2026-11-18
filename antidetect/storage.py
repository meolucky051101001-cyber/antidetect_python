import hashlib
import os
import sys
import json
import shutil
import time
import threading
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
from contextlib import contextmanager
from . import profile_delete
from .models import Profile, FingerprintConfig, ProxyConfig
from .fingerprint import generate_fingerprint
from .applog import get_logger
from .fanpage_scan import (
    REASON_LOGIN_IDENTITY_MISMATCH,
    SCAN_META_FILENAME,
    SCAN_META_VERSION,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    empty_scan_meta,
    merge_discovery_pages,
    parse_utc,
    scan_meta_last_scan,
    to_utc_iso,
    utc_now,
)
from .profile_locks import ProfileLockTimeout, ProfileLocks
from .profile_copy import (
    CopyJournal,
    CopyJournalError,
    ProfileCopyError,
    check_disk_space,
    cleanup_operation_root,
    copy_user_data,
    journal_path,
    journal_owner_alive,
    list_operation_journals,
    measure_tree,
    new_operation_id,
    operation_paths,
    prepare_staging,
    promote_staging,
    purge_expired_operations,
    read_operation_result,
    remove_operation_files,
    remove_operation_target,
    validate_idempotency_key,
    validate_profile_id,
    write_operation_result,
)
from .secrets import (
    SecretStoreCorruption,
    get_proxy_password,
    set_proxy_password,
    delete_proxy_password,
    migrate_plaintext_passwords,
)

# Regex kiểm tra profile_id hợp lệ (UUID v4 hoặc chuỗi alphanumeric-dash)
_VALID_PROFILE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")

log = get_logger("storage")


class ProfileDeletionError(RuntimeError):
    """Directory/metadata deletion failed; state must stay retryable."""


class ProfileBusyError(ProfileDeletionError):
    """Another process/thread holds the profile lifecycle lock."""

    code = "PROFILE_BUSY"


class ProfileRevisionConflict(RuntimeError):
    code = "PROFILE_REVISION_CONFLICT"

    def __init__(self, profile_id, current_revision, expected_revision):
        self.profile_id = profile_id
        self.current_revision = current_revision
        self.expected_revision = expected_revision
        super().__init__(
            f"Conflict: Profile đã bị sửa đổi bởi phiên khác "
            f"(revision hiện tại {current_revision} != {expected_revision})"
        )


class ProfileProxyChanged(ProfileRevisionConflict):
    code = "PROXY_CHANGED"


def _safe_log(message: str) -> None:
    """Log without crashing on legacy cp1252 consoles (Vietnamese text)."""
    log.warning(message)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Tests and portable deployments can point data/ elsewhere without touching
# the user's real browser profiles.
DATA_DIR = os.environ.get("ANTIDETECT_DATA_DIR") or os.path.join(BASE_DIR, "data")
PROFILES_JSON = os.path.join(DATA_DIR, "profiles.json")
PROFILES_DATA_DIR = os.path.join(DATA_DIR, "profiles")
LOCK_FILE = os.path.join(DATA_DIR, "profiles.lock")


def _replace_with_retry(src: str, dst: str, attempts: int = 6) -> None:
    """os.replace với backoff ngắn cho sharing violation trên Windows.

    Antivirus/backup agent có thể giữ handle đích vài trăm ms khiến replace
    ném PermissionError (WinError 5/32/33) dù file nguồn hợp lệ. Retry có
    giới hạn; thất bại lần cuối thì dọn file tạm và ném lại lỗi gốc.
    """
    delay = 0.05
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(
                exc, "winerror", None
            ) in (5, 32, 33)
            if not transient or attempt == attempts - 1:
                try:
                    os.remove(src)
                except OSError:
                    pass
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.8)


class InterProcessLock:
    """P1-07: Khóa liên tiến trình sử dụng file lock của hệ điều hành (msvcrt / fcntl)."""
    def __init__(self, lock_path: str = LOCK_FILE, timeout: float = 8.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        start = time.monotonic()
        os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)), exist_ok=True)
        while time.monotonic() - start < self.timeout:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT)
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT)
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
        # Fail closed: callers must not write metadata without a real lock.
        raise TimeoutError(
            f"Không lấy được khóa dữ liệu sau {self.timeout:.1f}s: {self.lock_path}"
        )

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


class ReentrantInterProcessLock:
    """InterProcessLock reentrant theo thread.

    B1: các method mutate giữ file lock quanh toàn bộ chuỗi
    read-modify-write; save_profiles gọi bên trong cũng cần vào cùng file
    lock, nên depth-counter chỉ khoá thật ở lần ngoài cùng.
    """

    def __init__(self, lock_path: str, timeout: float = 8.0):
        self._local = threading.local()
        self._inner = InterProcessLock(lock_path, timeout)

    def __enter__(self):
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            self._inner.__enter__()
        self._local.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        depth = getattr(self._local, "depth", 1) - 1
        self._local.depth = depth
        if depth == 0:
            self._inner.__exit__(exc_type, exc_val, exc_tb)
        return False


class ProfileManager:

    def __init__(self):
        self._lock = threading.RLock()
        # B1: file lock reentrant dùng chung cho mọi chuỗi read-modify-write.
        self._ipc_lock = ReentrantInterProcessLock(
            os.path.join(DATA_DIR, "profiles.lock")
        )
        # QĐ-6: lifecycle lock per-profile, dùng chung với launcher/CLI.
        self._profile_locks = ProfileLocks(DATA_DIR)
        # Scan lock riêng để stop/delete không phải chờ một lượt quét dài.
        self._scan_locks = ProfileLocks(DATA_DIR, suffix=".scan")
        self._corrupted = False  # Cờ bảo vệ chống ghi đè khi file hỏng
        self._cache: Optional[List[Profile]] = None
        self._cache_mtime: float = 0.0
        self._delete_write_local = threading.local()
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(PROFILES_DATA_DIR, exist_ok=True)
        self._ensure_profiles_file()
        try:
            self.recover_delete_operations()
        except Exception as e:
            _safe_log(f"[Storage] Deletion recovery retained: {type(e).__name__}")
        try:
            self.recover_copy_operations()
        except Exception as e:
            # Recovery must never keep the dashboard from starting.
            _safe_log(f"[Storage] Bỏ qua phục hồi copy_operations: {type(e).__name__}: {e}")

    def _ensure_profiles_file(self):
        # Tạo file dưới cùng IPC lock với save_profiles: hai process cùng
        # khởi động sẽ không ghi đè/đọc phải file tạm của nhau.
        with self._lock, self._ipc_lock:
            if not os.path.exists(PROFILES_JSON):
                # Tmp name uuid: 2 process cùng init không giành chung file tạm.
                temp_file = f"{PROFILES_JSON}.tmp.{uuid.uuid4().hex}"
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump([], f)
                    f.flush()
                    os.fsync(f.fileno())
                _replace_with_retry(temp_file, PROFILES_JSON)
                self._cache = []
                self._cache_mtime = os.stat(PROFILES_JSON).st_mtime_ns

    def _validate_profile_id(self, profile_id: str):
        """Kiểm tra profile_id hợp lệ để chống path traversal."""
        if not profile_id or not _VALID_PROFILE_ID.match(profile_id):
            raise ValueError(f"Profile ID không hợp lệ: {profile_id}")

    def list_profiles(self) -> List[Profile]:
        with self._lock:
            if not os.path.exists(PROFILES_JSON):
                self._ensure_profiles_file()
                return []
            try:
                # P2-05: Kiểm tra cache trong bộ nhớ qua mtime_ns của file
                mtime = os.stat(PROFILES_JSON).st_mtime_ns
                if self._cache is not None and getattr(self, "_cache_mtime", None) == mtime:
                    return [p.model_copy(deep=True) for p in self._cache]

                with open(PROFILES_JSON, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        raise ValueError("Empty metadata file")
                    data = json.loads(content)
                    if not isinstance(data, list):
                        raise ValueError("Metadata root must be a list")
                    if any(not isinstance(item, dict) or not item.get('id') for item in data):
                        raise ValueError("Profile entry must have a persistent ID")
                    profiles = [Profile.model_validate(item) for item in data]
                    for profile in profiles:
                        self._validate_profile_id(profile.id)
                        if profile.proxy and not profile.proxy.password:
                            pwd = get_proxy_password(
                                profile.id,
                                identity=profile.proxy.identity_key(),
                            )
                            if pwd:
                                profile.proxy.password = pwd
                    if len({p.id for p in profiles}) != len(profiles):
                        raise ValueError("Duplicate profile IDs")
                    self._corrupted = False
                    self._cache = profiles
                    self._cache_mtime = mtime
                    return [p.model_copy(deep=True) for p in profiles]
            except (ValueError, UnicodeError) as e:
                self._cache = None
                backup_path = f"{PROFILES_JSON}.corrupt.{time.time_ns()}"
                backed_up = False
                try:
                    if not self._corrupted:
                        shutil.copy2(PROFILES_JSON, backup_path)
                        backed_up = True
                except Exception:
                    pass
                self._corrupted = True  # Đặt cờ chống ghi đè
                raise RuntimeError(
                    "profiles.json bị hỏng hoặc sai cấu trúc. Đã khóa ghi và giữ nguyên file gốc. "
                    + (f"Bản sao: {backup_path}. " if backed_up else "")
                    + "Hãy khôi phục từ bản sao hợp lệ trước khi tiếp tục."
                ) from e
            except Exception as e:
                _safe_log(f"[Storage] Lỗi đọc profiles.json: {type(e).__name__}: {e}")
                raise e

    def _create_timestamped_backup(self):
        """Tạo bản sao lưu tự động trước khi ghi đè, giữ tối đa 10 bản sao gần nhất."""
        if not os.path.exists(PROFILES_JSON):
            return
        try:
            if os.path.getsize(PROFILES_JSON) == 0:
                return
            backup_dir = os.path.join(DATA_DIR, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"profiles_{ts}_{uuid.uuid4().hex[:6]}.json")
            shutil.copy2(PROFILES_JSON, backup_path)
            # Dọn dẹp giữ tối đa 10 bản sao
            backups = sorted(
                [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.startswith("profiles_") and f.endswith(".json")],
                key=os.path.getmtime
            )
            while len(backups) > 10:
                oldest = backups.pop(0)
                try:
                    os.remove(oldest)
                except OSError:
                    pass
        except Exception as e:
            _safe_log(f"[Storage] Cảnh báo tạo backup: {type(e).__name__}: {e}")

    def save_profiles(self, profiles: List[Profile]):
        # B1: lock reentrant — các method mutate đã giữ cùng lock này quanh
        # read-modify-write; gọi save_profiles trực tiếp vẫn khoá đúng.
        with self._lock, self._ipc_lock:
            self._assert_no_pending_deletions({profile.id for profile in profiles})
            # Revalidate dictionaries, not model instances: assignment validation
            # is intentionally disabled for legacy readers. Do this before secrets.
            for profile in profiles:
                Profile.model_validate(profile.model_dump(context={"include_secrets": True}, warnings=False))
            # Re-read the current file even when callers invoke save directly.
            self.list_profiles()
            # Chống ghi đè khi file đang bị hỏng
            if self._corrupted:
                raise RuntimeError(
                    "Không thể lưu: profiles.json đang bị hỏng. "
                    "Khôi phục file backup thủ công trước."
                )
            # P0-02: Tự động di chuyển mật khẩu plaintext vào kho DPAPI an toàn
            has_plaintext_secret = any(
                p.proxy
                and p.proxy.password
                and p.proxy.password != "********"
                and str(p.proxy.password).strip()
                for p in profiles
            )
            try:
                migrate_plaintext_passwords(profiles)
            except SecretStoreCorruption as e:
                # Never silently drop a password the caller asked us to save.
                if has_plaintext_secret:
                    raise RuntimeError(
                        "Không thể lưu mật khẩu proxy vì secrets.dpapi đang hỏng. "
                        "Hãy khôi phục kho secret trước khi lưu profile."
                    ) from e
                _safe_log(f"[Storage] Cảnh báo kho secret hỏng: {e}")
            except Exception as e:
                # H3: nuốt lỗi ở đây sẽ ghi password=null xuống đĩa trong khi
                # caller tưởng mật khẩu đã lưu — mất credential thầm lặng.
                if has_plaintext_secret:
                    raise RuntimeError(
                        "Không di chuyển được mật khẩu proxy vào kho DPAPI "
                        f"({type(e).__name__}: {e}). Đã hủy ghi để không mất mật khẩu."
                    ) from e
                _safe_log(f"[Storage] Di chuyển mật khẩu proxy sang DPAPI: {type(e).__name__}: {e}")

            # P0-01: Tạo bản sao lưu timestamped trước khi replace
            self._create_timestamped_backup()

            dump_list = []
            for p in profiles:
                d = p.model_dump(context={"include_secrets": False})
                if d.get("proxy"):
                    d["proxy"]["password"] = None  # Luôn để null trên đĩa
                dump_list.append(d)

            temp_file = f"{PROFILES_JSON}.tmp.{uuid.uuid4().hex}"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(
                    dump_list,
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(temp_file, PROFILES_JSON)
            self._cache = [p.model_copy(deep=True) for p in profiles]
            try:
                self._cache_mtime = os.stat(PROFILES_JSON).st_mtime_ns
            except Exception:
                self._cache_mtime = 0

    def get_profile(self, profile_id: str) -> Optional[Profile]:
        profiles = self.list_profiles()
        for p in profiles:
            if p.id == profile_id:
                # Nạp mật khẩu proxy từ DPAPI nếu có, nhưng chỉ khi identity khớp.
                if p.proxy and not p.proxy.password:
                    pwd = get_proxy_password(p.id, identity=p.proxy.identity_key())
                    if pwd:
                        p.proxy.password = pwd
                return p
        return None

    def get_profile_dir(self, profile_id: str) -> str:
        self._validate_profile_id(profile_id)
        if profile_delete.read_journal(DATA_DIR, PROFILES_DATA_DIR, profile_id) is not None:
            raise ProfileDeletionError("Profile deletion recovery is pending; directory access refused.")
        p_dir = os.path.join(PROFILES_DATA_DIR, profile_id)
        # Kiểm tra thêm: đường dẫn phải nằm trong PROFILES_DATA_DIR
        real_dir = os.path.realpath(p_dir)
        if not real_dir.startswith(os.path.realpath(PROFILES_DATA_DIR)):
            raise ValueError(f"Path traversal detected: {profile_id}")
        os.makedirs(p_dir, exist_ok=True)
        return p_dir

    def create_profile(
        self,
        name: str = "Profile Mới",
        target_os: str = "windows",
        proxy: Optional[ProxyConfig] = None,
        fingerprint: Optional[FingerprintConfig] = None,
        notes: str = "",
        tags: Optional[List[str]] = None,
    ) -> Profile:
        # B1: giữ file lock quanh cả chuỗi read-modify-write để hai process
        # (server + cli) cùng ghi không mất profile của nhau.
        with self._lock, self._ipc_lock:
            self._assert_no_pending_deletions()
            if fingerprint is None:
                fingerprint = generate_fingerprint(target_os=target_os)

            if proxy is None:
                proxy = ProxyConfig()

            new_p = Profile(
                name=name,
                fingerprint=fingerprint,
                proxy=proxy,
                notes=notes,
                tags=tags or [],
            )

            # Tạo thư mục dữ liệu riêng cho profile
            self.get_profile_dir(new_p.id)

            profiles = self.list_profiles()
            profiles.append(new_p)
            self.save_profiles(profiles)
            return new_p

    def update_profile(self, updated: Profile, expected_revision: Optional[int] = None) -> Optional[Profile]:
        from .models import REDACTED_SECRET
        Profile.model_validate(updated.model_dump(context={"include_secrets": True}, warnings=False))
        # B1: read-modify-write trong file lock; list_profiles() bên trong đọc
        # lại từ đĩa (mtime đổi khi process khác vừa ghi).
        with self._lock, self._ipc_lock:
            self._assert_no_pending_deletions({updated.id})
            profiles = self.list_profiles()
            found = False
            for i, p in enumerate(profiles):
                if p.id == updated.id:
                    # P0-01: Kiểm tra xung đột phiên bản nếu caller yêu cầu
                    current_rev = getattr(p, "revision", 1) or 1
                    if expected_revision is not None and current_rev != expected_revision:
                        raise ProfileRevisionConflict(updated.id, current_rev, expected_revision)
                    updated.revision = current_rev + 1
                    updated.updated_at = int(time.time())

                    old_identity = p.proxy.identity_key() if p.proxy else None
                    new_identity = (
                        updated.proxy.identity_key() if updated.proxy else None
                    )
                    identity_changed = old_identity != new_identity
                    supplied = (
                        updated.proxy.password
                        if updated.proxy and updated.proxy.password
                        else None
                    )
                    supplied_is_real = bool(
                        supplied
                        and supplied != REDACTED_SECRET
                        and str(supplied).strip()
                    )

                    if supplied_is_real:
                        # A new password is only ever bound to the new identity.
                        set_proxy_password(
                            updated.id, str(supplied), identity=new_identity
                        )
                    elif identity_changed:
                        # Blank password + changed proxy = do not reuse the old
                        # credential for a different host/port/user.
                        try:
                            delete_proxy_password(updated.id)
                        except SecretStoreCorruption as e:
                            _safe_log(
                                f"[Storage] Không xóa được secret cũ của {updated.id}: {e}"
                            )
                        updated.proxy.password = None
                    else:
                        # Same endpoint: blank/redacted means "keep existing".
                        pwd = get_proxy_password(updated.id, identity=new_identity)
                        updated.proxy.password = pwd

                    profiles[i] = updated
                    found = True
                    break
            if found:
                self.save_profiles(profiles)
                return updated
            return None

    def scan_lock(self, profile_id: str, timeout: float = 0.0):
        """Facebook scan lock (QĐ-6): chỉ chặn scan khác, không chặn stop/delete."""
        self._validate_profile_id(profile_id)
        return self._scan_locks.acquire(profile_id, timeout=timeout)

    def _scan_meta_path(self) -> str:
        return os.path.join(DATA_DIR, SCAN_META_FILENAME)

    def _read_scan_meta(self) -> Optional[Dict[str, Any]]:
        """Đọc scan meta; ``None`` khi file hỏng để caller không ghi đè mù."""
        path = self._scan_meta_path()
        if not os.path.exists(path):
            return empty_scan_meta()
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError, UnicodeError):
            return None
        if (
            not isinstance(meta, dict)
            or meta.get("version") != SCAN_META_VERSION
            or not isinstance(meta.get("profiles"), dict)
        ):
            return None
        return meta

    def _write_scan_meta(self, meta: Dict[str, Any]) -> None:
        meta["version"] = SCAN_META_VERSION
        path = self._scan_meta_path()
        os.makedirs(DATA_DIR, exist_ok=True)
        temp_file = f"{path}.tmp.{uuid.uuid4().hex}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(temp_file, path)

    def get_facebook_scan_meta(self, profile_id: str) -> Dict[str, Any]:
        """Metadata cấp scan của một profile (rỗng nếu chưa có/hỏng)."""
        self._validate_profile_id(profile_id)
        with self._lock:
            meta = self._read_scan_meta()
        if not meta:
            return {}
        entry = (meta.get("profiles") or {}).get(profile_id)
        return dict(entry) if isinstance(entry, dict) else {}

    def _update_scan_meta_locked(
        self, profile_id: str, scan: Dict[str, Any]
    ) -> None:
        """Ghi scan meta atomic dưới metadata lock, bỏ qua lượt cũ hơn."""
        meta = self._read_scan_meta()
        if meta is None:
            _safe_log(
                "[Storage] facebook_scan_meta.json hỏng; giữ file và bỏ ghi meta."
            )
            return
        profiles_meta = meta.setdefault("profiles", {})
        entry = profiles_meta.get(profile_id)
        if not isinstance(entry, dict):
            entry = {}
        last_scan = entry.get("last_scan")
        if not isinstance(last_scan, dict):
            last_scan = {}
        finished = parse_utc(scan.get("finished_at"))
        previous = parse_utc(last_scan.get("finished_at"))
        if finished and previous and finished < previous:
            return

        status = scan.get("discovery_status")
        reason = scan.get("completion_reason")
        account_id = str(scan.get("account_id_observed") or "").strip()
        trusted = str(entry.get("account_id_observed") or "").strip()
        if account_id and not (
            status == STATUS_FAILED and reason == REASON_LOGIN_IDENTITY_MISMATCH
        ):
            # Chỉ ID đã xác minh thành công mới trở thành mốc so sánh.
            entry["account_id_observed"] = account_id
            entry["identity_status"] = "verified"
        elif not trusted:
            entry["identity_status"] = scan.get("identity_status") or "unknown"

        entry["last_scan"] = scan_meta_last_scan(
            scan, int(scan.get("retained_count") or 0)
        )
        entry["updated_at"] = to_utc_iso(utc_now())
        profiles_meta[profile_id] = entry
        self._write_scan_meta(meta)

    def apply_facebook_scan(
        self, profile_id: str, scan: Dict[str, Any]
    ) -> Optional[Tuple[Profile, Dict[str, Any]]]:
        """Hợp nhất một lượt quét vào cache theo từng trường (không thay list).

        Failed scan chỉ ghi bằng chứng, không đụng ``facebook_pages``. Page
        vắng mặt trong lượt này vẫn được giữ, kèm ``retained_count``. Trả
        ``None`` nếu profile đã bị xóa trong lúc quét.
        """
        self._validate_profile_id(profile_id)
        scan = dict(scan or {})
        status = scan.get("discovery_status")
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            target = next((p for p in profiles if p.id == profile_id), None)
            if target is None:
                return None
            current = list(target.facebook_pages or [])
            retained = len(current)
            if status in (STATUS_COMPLETE, STATUS_PARTIAL):
                merged, retained = merge_discovery_pages(
                    current,
                    scan.get("pages") or [],
                    scan_id=scan.get("scan_id"),
                    seen_at=scan.get("finished_at") or utc_now(),
                )
                if merged != current:
                    target.facebook_pages = merged
                    target.revision = (getattr(target, "revision", 1) or 1) + 1
                    target.updated_at = int(time.time())
                    finished = parse_utc(scan.get("finished_at"))
                    observed_at = (
                        int(finished.timestamp()) if finished else int(time.time())
                    )
                    target.facebook_pages_synced_at = max(
                        int(target.facebook_pages_synced_at or 0), observed_at
                    )
                    self.save_profiles(profiles)
            scan["retained_count"] = retained
            try:
                self._update_scan_meta_locked(profile_id, scan)
            except OSError as e:
                _safe_log(
                    f"[Storage] Không ghi được scan meta: {type(e).__name__}"
                )
            return target, scan

    def clear_facebook_pages(self, profile_id: str) -> Optional[Profile]:
        """Xóa cache fanpage theo yêu cầu rõ ràng; không dùng khi refresh."""
        self._validate_profile_id(profile_id)
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            for p in profiles:
                if p.id != profile_id:
                    continue
                if p.facebook_pages:
                    p.facebook_pages = []
                    p.facebook_pages_synced_at = int(time.time())
                    p.revision = (getattr(p, "revision", 1) or 1) + 1
                    p.updated_at = int(time.time())
                    self.save_profiles(profiles)
                meta = self._read_scan_meta()
                entry = (meta or {}).get("profiles", {}).get(profile_id)
                last_scan = entry.get("last_scan") if isinstance(entry, dict) else None
                if (
                    isinstance(last_scan, dict)
                    and last_scan.get("retained_count") != 0
                ):
                    last_scan["retained_count"] = 0
                    entry["updated_at"] = to_utc_iso(utc_now())
                    try:
                        self._write_scan_meta(meta)
                    except OSError as e:
                        _safe_log(
                            f"[Storage] Không cập nhật được scan meta sau khi "
                            f"xóa cache: {type(e).__name__}"
                        )
                return p
            return None

    def update_profiles_batch(self, updated_list: List[Profile]) -> List[str]:
        """Cập nhật nhiều profile trong MỘT lần đọc/ghi (thao tác hàng loạt).

        Chỉ dành cho các cập nhật không đổi proxy (vd. switch-location-all):
        secret password handling của update_profile không áp dụng ở đây.
        Trả về danh sách id thực sự được ghi.
        """
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            by_id = {p.id: p for p in profiles}
            updated_ids: List[str] = []
            for updated in updated_list:
                current = by_id.get(updated.id)
                if current is None:
                    continue
                updated.revision = (getattr(current, "revision", 1) or 1) + 1
                updated.updated_at = int(time.time())
                by_id[updated.id] = updated
                updated_ids.append(updated.id)
            if updated_ids:
                self.save_profiles(list(by_id.values()))
            return updated_ids

    def _write_fingerprint_updates(
        self,
        updates: Dict[str, FingerprintConfig],
        expected_revisions: Optional[Dict[str, int]] = None,
        *,
        patches: Optional[Dict[str, Dict[str, Any]]] = None,
        expected_proxies: Optional[Dict[str, Tuple[bool, str]]] = None,
        skip_conflicts: bool = False,
    ) -> List[str]:
        """Ghi riêng fingerprint, không đi qua save_profiles.

        save_profiles() gọi migrate_plaintext_passwords(), có thể ghi lại
        secrets.dpapi từ password đang nằm trong bộ nhớ. Các bản vá fingerprint
        (audit) không được có tác dụng phụ đó, nên đọc/ghi raw JSON dưới cùng
        IPC lock và giữ nguyên mọi field proxy/secret khác.
        """
        target_ids = set(updates) | set(patches or {})
        if not target_ids:
            return []
        for profile_id in target_ids:
            self._validate_profile_id(profile_id)
        with self._lock, self._ipc_lock:
            self._assert_no_pending_deletions(target_ids)
            self._cache = None
            if self._corrupted:
                raise RuntimeError(
                    "Không thể sửa fingerprint: profiles.json đang bị hỏng. "
                    "Khôi phục file backup thủ công trước."
                )
            if not os.path.exists(PROFILES_JSON):
                return []
            try:
                with open(PROFILES_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, UnicodeError) as e:
                self._corrupted = True
                raise RuntimeError(
                    "profiles.json bị hỏng; không ghi đè để tránh mất dữ liệu."
                ) from e
            if not isinstance(data, list):
                raise RuntimeError("profiles.json sai cấu trúc (root phải là list).")

            by_id = {
                item.get("id"): item
                for item in data
                if isinstance(item, dict) and item.get("id")
            }
            conflicts = []
            for profile_id, item in by_id.items():
                if profile_id not in target_ids:
                    continue
                current_rev = int(item.get("revision") or 1)
                expected = (expected_revisions or {}).get(profile_id)
                expected_proxy = (expected_proxies or {}).get(profile_id)
                if expected_proxy is not None:
                    proxy = ProxyConfig.model_validate(item.get("proxy") or {})
                    if (proxy.enabled, proxy.identity_key()) != expected_proxy:
                        if not skip_conflicts:
                            raise ProfileProxyChanged(profile_id, current_rev, expected)
                        conflicts.append((profile_id, current_rev, expected))
                        continue
                if expected is not None and current_rev != expected:
                    conflicts.append((profile_id, current_rev, expected))
            if conflicts and not skip_conflicts:
                raise ProfileRevisionConflict(*conflicts[0])
            conflicted_ids = {conflict[0] for conflict in conflicts}

            now = int(time.time())
            changed: List[str] = []
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                profile_id = item.get("id")
                if profile_id not in target_ids or profile_id in conflicted_ids:
                    continue
                new_fp = updates.get(profile_id)
                if patches is not None and profile_id in patches:
                    fp_data = dict(item.get("fingerprint") or {})
                    patch = patches[profile_id]
                    if set(patch) - set(FingerprintConfig.model_fields):
                        raise ValueError("Trường fingerprint không hợp lệ")
                    fp_data.update(patch)
                    new_fp = FingerprintConfig.model_validate(fp_data)
                    if not patch:
                        continue
                current_rev = int(item.get("revision") or 1)
                merged = dict(item)
                merged["fingerprint"] = new_fp.model_dump()
                merged["revision"] = current_rev + 1
                merged["updated_at"] = now
                # Validate toàn bộ record trước khi ghi để không đẩy file vào
                # trạng thái mà list_profiles() sẽ coi là hỏng.
                Profile.model_validate(merged)
                data[index] = merged
                changed.append(profile_id)

            if not changed:
                return []
            self._create_timestamped_backup()
            temp_file = f"{PROFILES_JSON}.tmp.{uuid.uuid4().hex}"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(temp_file, PROFILES_JSON)
            self._corrupted = False
            self._cache = None
            self._cache_mtime = 0.0
            return changed

    def update_fingerprint_only(
        self,
        profile_id: str,
        fingerprint: FingerprintConfig,
        expected_revision: Optional[int] = None,
    ) -> Optional[Profile]:
        expected = (
            {profile_id: expected_revision}
            if expected_revision is not None
            else None
        )
        changed = self._write_fingerprint_updates(
            {profile_id: fingerprint}, expected_revisions=expected
        )
        if not changed:
            return None
        return self._profile_metadata_only(profile_id)

    def _profile_metadata_only(self, profile_id: str) -> Optional[Profile]:
        """Read a patch result without hydrating or repairing the secret store."""
        with self._lock, self._ipc_lock:
            with open(PROFILES_JSON, "r", encoding="utf-8") as stream:
                records = json.load(stream)
            return next((Profile.model_validate(item) for item in records
                         if item.get("id") == profile_id), None)

    def patch_fingerprint(
        self, profile_id: str, patch: Dict[str, Any], *, expected_revision: int,
        expected_proxy: Optional[Tuple[bool, str]] = None,
    ) -> Optional[Profile]:
        self._write_fingerprint_updates(
            {}, {profile_id: expected_revision}, patches={profile_id: patch},
            expected_proxies={profile_id: expected_proxy} if expected_proxy is not None else None,
        )
        return self._profile_metadata_only(profile_id)

    def patch_fingerprints_batch(
        self, patches: Dict[str, Dict[str, Any]], expected_revisions: Dict[str, int],
    ) -> List[str]:
        """Commit compatible patches once; conflicts/missing profiles stay untouched."""
        return self._write_fingerprint_updates(
            {}, expected_revisions, patches=patches, skip_conflicts=True,
        )

    def update_fingerprints_batch(
        self,
        updates: Dict[str, FingerprintConfig],
        expected_revisions: Optional[Dict[str, int]] = None,
    ) -> List[str]:
        return self._write_fingerprint_updates(
            updates, expected_revisions=expected_revisions
        )

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a profile while holding the shared lifecycle lock."""
        self._validate_profile_id(profile_id)
        try:
            with self._profile_locks.acquire(profile_id, timeout=2.0):
                return self._delete_profile_locked(profile_id)
        except ProfileLockTimeout as e:
            raise ProfileBusyError(
                "Profile đang được thao tác bởi tiến trình khác. Hãy thử lại sau."
            ) from e

    @contextmanager
    def _deletion_metadata_write(self):
        depth = getattr(self._delete_write_local, "depth", 0)
        self._delete_write_local.depth = depth + 1
        try:
            yield
        finally:
            self._delete_write_local.depth = depth

    def _assert_no_pending_deletions(self, incoming_ids=None):
        if getattr(self._delete_write_local, "depth", 0):
            return
        for profile_id in profile_delete.pending_profile_ids(DATA_DIR):
            journal = profile_delete.read_journal(DATA_DIR, PROFILES_DATA_DIR, profile_id)
            if journal is None:
                continue
            if journal["phase"] not in ("committed", "rolled_back") or profile_id in (incoming_ids or set()):
                raise ProfileDeletionError("Unfinished deletion must be recovered before changing profile metadata.")

    def _delete_purge(self, journal):
        """Purge only a committed tombstone, outside the global metadata lock."""
        try:
            with self._lock, self._ipc_lock:
                if any(p.id == journal["profile_id"] for p in self.list_profiles()):
                    raise ProfileDeletionError("Replacement metadata blocks quarantine purge.")
                profile_delete.require_entry_absent(journal["profile_id"])
            profile_delete.finish(DATA_DIR, PROFILES_DATA_DIR, journal, purge=True)
        except Exception as exc:
            # Deletion is committed. Keep the journal/quarantine for an
            # idempotent cleanup retry; never resurrect partially purged data.
            _safe_log(f"[Storage] deletion cleanup_pending {journal['profile_id']}: {type(exc).__name__}")
            return False
        return True

    def _recover_delete_operation_locked(self, profile_id):
        """Caller holds lifecycle lock; recover one operation without clobbering others."""
        with self._lock, self._ipc_lock:
            journal = profile_delete.read_journal(DATA_DIR, PROFILES_DATA_DIR, profile_id)
            if journal is None:
                return None
            if journal["phase"] == "rolled_back":
                profile_delete.finish(DATA_DIR, PROFILES_DATA_DIR, journal, purge=False)
                return "rolled_back"
            if journal["phase"] != "committed":
                capsule = profile_delete.read_capsule(DATA_DIR, PROFILES_DATA_DIR, journal)
                snapshot = capsule["profile"]
                restored = Profile.model_validate(snapshot)
                profiles = self.list_profiles()
                existing = next((p for p in profiles if p.id == profile_id), None)
                if existing is not None and self._profile_snapshot(existing) != self._profile_snapshot(restored):
                    raise ProfileDeletionError("Newer profile metadata conflicts with deletion recovery.")
                # Secret conflict checks run before exposing a restored directory.
                profile_delete.restore_entry(profile_id, capsule)
                profile_delete.restore_directory(DATA_DIR, PROFILES_DATA_DIR, journal)
                if existing is None:
                    # restore_entry already published a deterministic DPAPI
                    # value. Do not remigrate this target during save_profiles:
                    # a crash before rolled_back must accept its own prior write.
                    if restored.proxy:
                        restored.proxy.password = None
                    with self._deletion_metadata_write():
                        self.save_profiles(profiles + [restored])
                journal = profile_delete.write_phase(DATA_DIR, journal, "rolled_back")
                profile_delete.finish(DATA_DIR, PROFILES_DATA_DIR, journal, purge=False)
                return "rolled_back"
        self._delete_purge(journal)
        return "committed"

    def recover_delete_operations(self, lock_timeout: float = 0.2) -> int:
        handled = 0
        for profile_id in profile_delete.pending_profile_ids(DATA_DIR):
            try:
                with self._profile_locks.acquire(profile_id, timeout=lock_timeout):
                    self._recover_delete_operation_locked(profile_id)
                    handled += 1
            except Exception as exc:
                _safe_log(f"[Storage] deletion recovery_pending {profile_id}: {type(exc).__name__}")
        return handled

    def _delete_profile_locked(self, profile_id: str) -> bool:
        """Rename to quarantine, commit metadata/secrets, then purge after commit."""
        try:
            if self._recover_delete_operation_locked(profile_id) == "committed":
                return True
            # Slow tree validation happens without the global metadata lock.
            profile_delete.preflight_tree(os.path.join(PROFILES_DATA_DIR, profile_id))
            with self._lock, self._ipc_lock:
                profiles = self.list_profiles()
                profile = next((p for p in profiles if p.id == profile_id), None)
                if profile is None:
                    return False
                journal = profile_delete.prepare(
                    DATA_DIR, PROFILES_DATA_DIR, profile_id,
                    profile.model_dump(context={"include_secrets": True}))
                try:
                    capsule = profile_delete.read_capsule(DATA_DIR, PROFILES_DATA_DIR, journal)
                    profile_delete.quarantine(DATA_DIR, PROFILES_DATA_DIR, journal)
                    journal = profile_delete.write_phase(DATA_DIR, journal, "quarantined")
                    with self._deletion_metadata_write():
                        self.save_profiles([p for p in profiles if p.id != profile_id])
                    journal = profile_delete.write_phase(DATA_DIR, journal, "metadata_removed")
                    profile_delete.remove_entry(profile_id, capsule)
                    journal = profile_delete.write_phase(DATA_DIR, journal, "secret_removed")
                    journal = profile_delete.write_phase(DATA_DIR, journal, "committed")
                except Exception as exc:
                    # Inspect disk rather than our local phase: an atomic write
                    # may have committed before a post-write error was raised.
                    observed = profile_delete.read_journal(DATA_DIR, PROFILES_DATA_DIR, profile_id)
                    if observed and observed["phase"] == "committed":
                        journal = observed
                    else:
                        try:
                            self._recover_delete_operation_locked(profile_id)
                        except Exception as recovery_exc:
                            raise ProfileDeletionError(
                                "Deletion interrupted; encrypted recovery capsule and quarantine retained."
                            ) from recovery_exc
                        raise ProfileDeletionError(
                            "Deletion failed before commit; profile data, metadata and secret restored."
                        ) from exc
                # Scan cache is auxiliary; keep it intact until the core commit.
                meta = self._read_scan_meta()
                if meta and profile_id in (meta.get("profiles") or {}):
                    meta["profiles"].pop(profile_id, None)
                    try:
                        self._write_scan_meta(meta)
                    except OSError as exc:
                        _safe_log(f"[Storage] deletion scan cache cleanup retained: {type(exc).__name__}")
            self._delete_purge(journal)
            return True
        except ProfileDeletionError:
            raise
        except Exception as exc:
            raise ProfileDeletionError(
                "Deletion could not be completed safely; recovery data was retained."
            ) from exc

    def commit_staged_profile(self, profile: Profile) -> Profile:
        """Publish an already-staged profile without creating directories.

        The caller wrote the target user_data and DPAPI secret before this
        point. Re-reading under the metadata lock keeps concurrent edits to
        other profiles intact.
        """
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            if any(p.id == profile.id for p in profiles):
                raise RuntimeError(
                    f"Profile đích {profile.id} đã tồn tại trong metadata."
                )
            profiles.append(profile)
            self.save_profiles(profiles)
        return profile

    @staticmethod
    def _operation_lock_id(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:32]
        return f"copy-{digest}"

    @staticmethod
    def _profile_snapshot(profile: Profile) -> Dict[str, Any]:
        data = profile.model_dump(context={"include_secrets": False})
        proxy = data.get("proxy")
        if isinstance(proxy, dict):
            proxy.pop("password", None)
        return data

    def _read_operation_journal(self, operation_id: str) -> Optional[Dict[str, Any]]:
        path = journal_path(DATA_DIR, operation_id)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise CopyJournalError(
                f"Journal không đọc được: {type(e).__name__}"
            ) from e
        if not isinstance(data, dict):
            raise CopyJournalError("Journal phải là một object JSON")
        return data

    def _check_journal_source(self, journal: Dict[str, Any], source_id: str) -> None:
        if str(journal.get("source_id") or "") != source_id:
            raise ProfileCopyError(
                "Mã thao tác đã được dùng cho một profile nguồn khác.",
                code="COPY_OPERATION_CONFLICT",
            )
        operation_paths(DATA_DIR, PROFILES_DATA_DIR, journal)

    def _result_profile(
        self, result: Dict[str, Any], source_id: str
    ) -> Optional[Profile]:
        if str(result.get("source_id") or "") != source_id:
            raise ProfileCopyError(
                "Mã thao tác đã được dùng cho một profile nguồn khác.",
                code="COPY_OPERATION_CONFLICT",
            )
        target_id = validate_profile_id(str(result.get("target_id") or ""))
        return self.get_profile(target_id)

    def _finalize_committed_operation(
        self, journal: Dict[str, Any]
    ) -> Optional[Profile]:
        operation_id = str(journal.get("operation_id") or "")
        target_id = validate_profile_id(str(journal.get("target_id") or ""))
        target = self.get_profile(target_id)
        if target is None:
            return None
        snapshot = journal.get("target_profile")
        if not isinstance(snapshot, dict):
            snapshot = self._profile_snapshot(target)
        result = {
            "operation_id": operation_id,
            "source_id": journal.get("source_id"),
            "target_id": target_id,
            "committed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "profile": snapshot,
        }
        try:
            write_operation_result(DATA_DIR, operation_id, result)
        except ProfileCopyError as e:
            _safe_log(f"[Storage] Không ghi được kết quả copy {operation_id}: {e}")
        if journal.get("phase") != "committed":
            try:
                writer = CopyJournal(
                    journal_path(DATA_DIR, operation_id),
                    operation_id=operation_id,
                    source_id=str(journal.get("source_id") or ""),
                    target_id=target_id,
                    owner=journal.get("owner"),
                )
                writer.update("committed", target_profile=snapshot)
            except Exception as e:
                _safe_log(
                    f"[Storage] Không cập nhật được journal {operation_id}: "
                    f"{type(e).__name__}"
                )
        return target

    @staticmethod
    def _journal_data(journal: Any) -> Dict[str, Any]:
        if isinstance(journal, dict):
            return journal
        read = getattr(journal, "read", None)
        if callable(read):
            return read()
        raise TypeError("Journal không hợp lệ")

    def _cleanup_operation_artifacts(self, journal: Any) -> List[str]:
        journal = self._journal_data(journal)
        operation_id = str(journal.get("operation_id") or "")
        target_id = str(journal.get("target_id") or "")
        operation_paths(DATA_DIR, PROFILES_DATA_DIR, journal)
        errors: List[str] = []
        try:
            cleanup_operation_root(PROFILES_DATA_DIR, operation_id)
        except Exception as e:
            errors.append(f"staging:{type(e).__name__}")
        try:
            remove_operation_target(PROFILES_DATA_DIR, target_id)
        except Exception as e:
            errors.append(f"target:{type(e).__name__}")
        try:
            delete_proxy_password(target_id)
        except Exception as e:
            errors.append(f"secret:{type(e).__name__}")
        return errors

    def _mark_cleanup_pending(
        self, journal: Any, errors: List[str], error_type: str
    ) -> None:
        journal = self._journal_data(journal)
        operation_id = str(journal.get("operation_id") or "")
        try:
            writer = CopyJournal(
                journal_path(DATA_DIR, operation_id),
                operation_id=operation_id,
                source_id=str(journal.get("source_id") or ""),
                target_id=str(journal.get("target_id") or ""),
                owner=journal.get("owner"),
            )
            writer.update(
                "cleanup_pending",
                cleanup_errors=errors[:5],
                error_type=error_type,
            )
        except Exception as e:
            _safe_log(
                f"[Storage] Không ghi được cleanup_pending cho {operation_id}: "
                f"{type(e).__name__}"
            )

    def _recover_operation_locked(
        self,
        journal: Dict[str, Any],
        *,
        raise_on_cleanup_failure: bool = False,
    ) -> Optional[Profile]:
        """Resolve a stale operation while its lifecycle locks are held."""
        operation_id = str(journal.get("operation_id") or "")
        target_id = validate_profile_id(str(journal.get("target_id") or ""))
        phase = str(journal.get("phase") or "")

        if phase in ("committed", "cleanup_pending"):
            target = self.get_profile(target_id)
            if target is not None:
                finalized = self._finalize_committed_operation(journal)
                try:
                    cleanup_operation_root(PROFILES_DATA_DIR, operation_id)
                except Exception as e:
                    # The copy is already published; keep reporting the
                    # unfinished cleanup instead of deleting user data.
                    self._mark_cleanup_pending(
                        journal, [f"staging:{type(e).__name__}"], "CLEANUP_AFTER_COMMIT"
                    )
                return finalized
            errors = self._cleanup_operation_artifacts(journal)
            if errors:
                self._mark_cleanup_pending(journal, errors, "RECOVERY_CLEANUP")
                if raise_on_cleanup_failure:
                    raise ProfileCopyError(
                        "Bản sao chưa hoàn tất và chưa dọn được dữ liệu dở dang.",
                        code="CLEANUP_PENDING",
                    )
                return None
            try:
                remove_operation_files(DATA_DIR, operation_id)
            except CopyJournalError as e:
                _safe_log(f"[Storage] Không xóa được operation {operation_id}: {e}")
            return None

        # Metadata is the publication point: a target that already exists in
        # profiles.json is finished even if the journal lagged behind.
        target = self.get_profile(target_id)
        if target is not None:
            return self._finalize_committed_operation(journal)

        errors = self._cleanup_operation_artifacts(journal)
        if errors:
            self._mark_cleanup_pending(journal, errors, "RECOVERY_CLEANUP")
            if raise_on_cleanup_failure:
                raise ProfileCopyError(
                    "Bản sao chưa hoàn tất và chưa dọn được dữ liệu dở dang.",
                    code="CLEANUP_PENDING",
                )
            return None
        try:
            remove_operation_files(DATA_DIR, operation_id)
        except CopyJournalError as e:
            _safe_log(f"[Storage] Không xóa được operation {operation_id}: {e}")
        return None

    def recover_copy_operations(self, lock_timeout: float = 1.0) -> int:
        """Finish or clean stale copy operations whose owner process is gone."""
        handled = 0
        for journal in list_operation_journals(DATA_DIR):
            if journal_owner_alive(journal):
                continue
            operation_id = str(journal.get("operation_id") or "")
            try:
                with self._profile_locks.acquire(
                    self._operation_lock_id(operation_id), timeout=lock_timeout
                ):
                    fresh = self._read_operation_journal(operation_id)
                    if fresh is None or journal_owner_alive(fresh):
                        continue
                    operation_paths(DATA_DIR, PROFILES_DATA_DIR, fresh)
                    source_id = str(fresh.get("source_id") or "")
                    target_id = str(fresh.get("target_id") or "")
                    with self._profile_locks.acquire_many(
                        [source_id, target_id], timeout=lock_timeout
                    ):
                        self._recover_operation_locked(fresh)
                    handled += 1
            except (ProfileLockTimeout, ProfileCopyError, ValueError) as e:
                _safe_log(
                    f"[Storage] Bỏ qua phục hồi operation {operation_id}: "
                    f"{type(e).__name__}: {e}"
                )
        try:
            purge_expired_operations(DATA_DIR)
        except Exception as e:
            _safe_log(f"[Storage] Không dọn được operation hết TTL: {type(e).__name__}")
        return handled

    def duplicate_profile(
        self,
        profile_id: str,
        new_name: Optional[str] = None,
        copy_data: bool = False,
        *,
        operation_id: Optional[str] = None,
        source_running: Optional[Callable[[str], bool]] = None,
        lock_timeout: float = 2.0,
    ) -> Optional[Profile]:
        """Transactional profile copy (QĐ-3).

        No target metadata/directory is published until the staged copy and
        its DPAPI secret are complete. A retry with the same operation_id
        returns the previously created target instead of copying twice.
        """
        self._validate_profile_id(profile_id)
        if operation_id is None:
            operation_id = new_operation_id()
        else:
            validate_idempotency_key(operation_id)
        try:
            purge_expired_operations(DATA_DIR)
        except Exception as e:
            _safe_log(
                f"[Storage] Không dọn được operation hết TTL: {type(e).__name__}"
            )
        try:
            with self._profile_locks.acquire(
                self._operation_lock_id(operation_id), timeout=lock_timeout
            ):
                return self._duplicate_profile_locked(
                    profile_id,
                    new_name=new_name,
                    copy_data=copy_data,
                    operation_id=operation_id,
                    source_running=source_running,
                    lock_timeout=lock_timeout,
                )
        except ProfileLockTimeout as e:
            raise ProfileCopyError(
                "Profile đang được thao tác bởi tiến trình khác. Hãy thử lại sau.",
                code="PROFILE_BUSY",
            ) from e

    def _duplicate_profile_locked(
        self,
        profile_id: str,
        *,
        new_name: Optional[str],
        copy_data: bool,
        operation_id: str,
        source_running: Optional[Callable[[str], bool]],
        lock_timeout: float,
    ) -> Optional[Profile]:
        journal = self._read_operation_journal(operation_id)
        if journal is not None:
            self._check_journal_source(journal, profile_id)
        result = read_operation_result(DATA_DIR, operation_id)
        if result is not None:
            return self._result_profile(result, profile_id)
        if journal is not None and journal.get("phase") == "committed":
            target = self._finalize_committed_operation(journal)
            if target is not None:
                return target
            return None

        target_id = (
            validate_profile_id(str(journal.get("target_id")))
            if journal is not None
            else str(uuid.uuid4())
        )
        try:
            with self._profile_locks.acquire_many(
                [profile_id, target_id], timeout=lock_timeout
            ):
                # A concurrent retry may have finished while we waited.
                result = read_operation_result(DATA_DIR, operation_id)
                if result is not None:
                    return self._result_profile(result, profile_id)
                journal = self._read_operation_journal(operation_id)
                if journal is not None:
                    self._check_journal_source(journal, profile_id)
                    if journal.get("phase") == "committed":
                        target = self._finalize_committed_operation(journal)
                        if target is not None:
                            return target
                        return None
                    recovered = self._recover_operation_locked(
                        journal, raise_on_cleanup_failure=True
                    )
                    if recovered is not None:
                        return recovered
                    target_id = validate_profile_id(
                        str(journal.get("target_id") or target_id)
                    )
                return self._run_copy_transaction(
                    profile_id,
                    target_id,
                    operation_id,
                    new_name=new_name,
                    copy_data=copy_data,
                    source_running=source_running,
                )
        except ProfileLockTimeout as e:
            raise ProfileCopyError(
                "Profile đang được thao tác bởi tiến trình khác. Hãy thử lại sau.",
                code="PROFILE_BUSY",
            ) from e

    def _run_copy_transaction(
        self,
        profile_id: str,
        target_id: str,
        operation_id: str,
        *,
        new_name: Optional[str],
        copy_data: bool,
        source_running: Optional[Callable[[str], bool]],
    ) -> Optional[Profile]:
        with self._lock, self._ipc_lock:
            self._assert_no_pending_deletions({profile_id})
            orig = self.get_profile(profile_id)
        if not orig:
            return None

        def assert_source_idle():
            if not copy_data:
                return
            try:
                if source_running is None:
                    raise RuntimeError("missing source state verifier")
                running = source_running(profile_id)
                if running is not True and running is not False:
                    raise RuntimeError("inconclusive source state")
            except Exception as e:
                raise ProfileCopyError(
                    "Không xác minh được profile nguồn đã dừng; hủy sao chép.",
                    code="SOURCE_RUNNING",
                ) from e
            if running:
                raise ProfileCopyError(
                    "Hãy đóng profile trước khi sao chép dữ liệu phiên.",
                    code="SOURCE_RUNNING",
                )
        assert_source_idle()

        new_fp = generate_fingerprint(
            target_os=orig.fingerprint.os,
            timezone=orig.fingerprint.timezone,
            locale=orig.fingerprint.locale,
            latitude=orig.fingerprint.latitude or 21.0285,
            longitude=orig.fingerprint.longitude or 105.8542,
        )
        target = Profile(
            id=target_id,
            name=new_name or f"{orig.name} (Bản sao)",
            proxy=orig.proxy.model_copy(deep=True) if orig.proxy else ProxyConfig(),
            fingerprint=new_fp,
            notes=orig.notes,
            tags=list(orig.tags),
        )
        proxy_password = target.proxy.password if target.proxy else None
        if target.proxy:
            # Secret goes to DPAPI only; never to the journal or profiles.json.
            target.proxy.password = None

        source_user_data = os.path.join(PROFILES_DATA_DIR, profile_id, "user_data")
        target_dir = os.path.join(PROFILES_DATA_DIR, target_id)
        target_user_data = os.path.join(target_dir, "user_data")
        staging_user_data = os.path.join(
            PROFILES_DATA_DIR, ".staging", operation_id, "user_data"
        )
        copy_source_exists = os.path.isdir(source_user_data)

        source_size = 0
        if copy_data and copy_source_exists:
            stats = measure_tree(source_user_data)
            source_size = stats.total_bytes
            check_disk_space(source_size, PROFILES_DATA_DIR)

        snapshot = self._profile_snapshot(target)
        journal = CopyJournal(
            journal_path(DATA_DIR, operation_id),
            operation_id=operation_id,
            source_id=profile_id,
            target_id=target_id,
            owner=f"{os.getpid()}:{threading.get_ident()}",
            source_dir=os.path.join(PROFILES_DATA_DIR, profile_id),
            staging_dir=staging_user_data,
            target_dir=target_user_data,
        )
        journal.write(
            "staging",
            copy_data=bool(copy_data),
            source_size=source_size,
            target_profile=snapshot,
        )

        try:
            if copy_data and copy_source_exists:
                prepare_staging(PROFILES_DATA_DIR, operation_id)
                copied = copy_user_data(source_user_data, staging_user_data)
                journal.update(
                    "copied",
                    copied_files=copied.total_files,
                    copied_bytes=copied.total_bytes,
                )
                assert_source_idle()
                promote_staging(staging_user_data, target_user_data)
                journal.update("promoted", promoted=True)
            else:
                journal.update(
                    "promoted",
                    promoted=False,
                    source_missing=bool(copy_data and not copy_source_exists),
                )

            if proxy_password and target.proxy:
                try:
                    set_proxy_password(
                        target_id,
                        proxy_password,
                        identity=target.proxy.identity_key(),
                    )
                except Exception as e:
                    raise ProfileCopyError(
                        "Không ghi được mật khẩu proxy cho bản sao. "
                        "Dữ liệu phiên và metadata chưa được công bố.",
                        code="COMMIT_FAILED",
                    ) from e
            journal.update("secret_written")

            try:
                self.commit_staged_profile(target)
            except Exception as e:
                raise ProfileCopyError(
                    "Không ghi được metadata cho bản sao; đã hủy công bố profile đích.",
                    code="COMMIT_FAILED",
                ) from e

            try:
                journal.update("committed", target_profile=snapshot)
            except Exception as e:
                _safe_log(
                    f"[Storage] Journal {operation_id} chưa ghi committed: "
                    f"{type(e).__name__}"
                )
            try:
                write_operation_result(
                    DATA_DIR,
                    operation_id,
                    {
                        "operation_id": operation_id,
                        "source_id": profile_id,
                        "target_id": target_id,
                        "committed_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                        "profile": snapshot,
                    },
                )
            except ProfileCopyError as e:
                # The committed journal still lets a retry return the same ID.
                _safe_log(f"[Storage] Không ghi được kết quả copy {operation_id}: {e}")
            try:
                cleanup_operation_root(PROFILES_DATA_DIR, operation_id)
            except Exception as e:
                self._mark_cleanup_pending(
                    journal, [f"staging:{type(e).__name__}"], "CLEANUP_AFTER_COMMIT"
                )
            return target
        except Exception as exc:
            errors = self._cleanup_operation_artifacts(journal)
            if errors:
                self._mark_cleanup_pending(journal, errors, type(exc).__name__)
                raise ProfileCopyError(
                    f"{exc} (dữ liệu dở dang chưa dọn được; trạng thái cleanup_pending)",
                    code="CLEANUP_PENDING",
                ) from exc
            try:
                remove_operation_files(DATA_DIR, operation_id)
            except CopyJournalError as e:
                _safe_log(
                    f"[Storage] Không xóa được operation {operation_id}: {e}"
                )
            if isinstance(exc, ProfileCopyError):
                raise
            raise ProfileCopyError(
                f"Sao chép profile thất bại: {type(exc).__name__}: {exc}",
                code="COPY_FAILED",
            ) from exc
