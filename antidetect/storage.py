import os
import sys
import json
import shutil
import time
import threading
import re
import uuid
from typing import Any, Dict, List, Optional
from .models import Profile, FingerprintConfig, ProxyConfig
from .fingerprint import generate_fingerprint
from .applog import get_logger
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
        self._corrupted = False  # Cờ bảo vệ chống ghi đè khi file hỏng
        self._cache: Optional[List[Profile]] = None
        self._cache_mtime: float = 0.0
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(PROFILES_DATA_DIR, exist_ok=True)
        self._ensure_profiles_file()

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
        # B1: read-modify-write trong file lock; list_profiles() bên trong đọc
        # lại từ đĩa (mtime đổi khi process khác vừa ghi).
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            found = False
            for i, p in enumerate(profiles):
                if p.id == updated.id:
                    # P0-01: Kiểm tra xung đột phiên bản nếu caller yêu cầu
                    current_rev = getattr(p, "revision", 1) or 1
                    if expected_revision is not None and current_rev != expected_revision:
                        raise RuntimeError(
                            f"Conflict: Profile đã bị sửa đổi bởi phiên khác (revision hiện tại {current_rev} != {expected_revision})"
                        )
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

    def update_facebook_pages(
        self,
        profile_id: str,
        pages: List[Dict[str, Any]],
        synced_at: int,
    ) -> Optional[Profile]:
        """Merge only fanpage fields into the current record.

        A deep Facebook scrape can run for minutes; replacing the whole profile
        with the snapshot taken before it started would revert proxy edits made
        in the meantime. Re-read under the lock and touch only these fields.
        """
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            for p in profiles:
                if p.id != profile_id:
                    continue
                p.facebook_pages = pages
                p.facebook_pages_synced_at = synced_at
                p.revision = (getattr(p, "revision", 1) or 1) + 1
                p.updated_at = int(time.time())
                self.save_profiles(profiles)
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
    ) -> List[str]:
        """Ghi riêng fingerprint, không đi qua save_profiles.

        save_profiles() gọi migrate_plaintext_passwords(), có thể ghi lại
        secrets.dpapi từ password đang nằm trong bộ nhớ. Các bản vá fingerprint
        (audit) không được có tác dụng phụ đó, nên đọc/ghi raw JSON dưới cùng
        IPC lock và giữ nguyên mọi field proxy/secret khác.
        """
        if not updates:
            return []
        for profile_id in updates:
            self._validate_profile_id(profile_id)
        with self._lock, self._ipc_lock:
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
                if profile_id not in updates:
                    continue
                current_rev = int(item.get("revision") or 1)
                expected = (expected_revisions or {}).get(profile_id)
                if expected is not None and current_rev != expected:
                    conflicts.append((profile_id, current_rev, expected))
            if conflicts:
                detail = ", ".join(
                    f"{pid[:8]} (hiện tại {current}, yêu cầu {expected})"
                    for pid, current, expected in conflicts
                )
                raise RuntimeError(
                    f"Conflict: Profile đã bị sửa đổi bởi phiên khác: {detail}"
                )

            now = int(time.time())
            changed: List[str] = []
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                profile_id = item.get("id")
                new_fp = updates.get(profile_id)
                if new_fp is None:
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
        return self.get_profile(profile_id)

    def update_fingerprints_batch(
        self,
        updates: Dict[str, FingerprintConfig],
        expected_revisions: Optional[Dict[str, int]] = None,
    ) -> List[str]:
        return self._write_fingerprint_updates(
            updates, expected_revisions=expected_revisions
        )

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a profile with retryable, secret-safe ordering.

        Order: profile directory -> metadata -> secret. If the directory cannot
        be removed, metadata and the DPAPI secret are left untouched so the
        user can retry after closing the browser. Only a successful metadata
        save allows the secret to be removed.
        """
        self._validate_profile_id(profile_id)
        # B1: rmtree user_data có thể chậm (thư mục GB, retry khi file bị
        # khóa) — làm nó NGOÀI file lock để không chặn create/update của
        # process khác tới timeout 8s.
        if not any(p.id == profile_id for p in self.list_profiles()):
            return False

        p_dir = os.path.join(PROFILES_DATA_DIR, profile_id)
        if os.path.exists(p_dir):
            last_error = None
            for attempt in range(3):
                try:
                    shutil.rmtree(p_dir)
                    last_error = None
                    break
                except PermissionError as e:
                    last_error = e
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                except Exception as e:
                    last_error = e
                    break

            if os.path.exists(p_dir):
                _safe_log(
                    f"[Storage] Không thể xóa thư mục profile {profile_id} "
                    f"(file đang bị khóa). Hãy đóng trình duyệt trước khi xóa. "
                    f"Chi tiết: {type(last_error).__name__ if last_error else 'unknown'}"
                )
                raise ProfileDeletionError(
                    "Không thể xóa thư mục profile (file đang bị khóa). "
                    "Metadata và mật khẩu proxy được giữ nguyên; hãy đóng trình duyệt rồi thử lại."
                )

        # Directory is gone. Persist the metadata removal before deleting
        # the DPAPI entry: if this write fails, the secret is still needed
        # for a retry.
        with self._lock, self._ipc_lock:
            profiles = self.list_profiles()
            new_list = [p for p in profiles if p.id != profile_id]
            if len(new_list) == len(profiles):
                # Process khác đã xóa metadata sau khi ta đọc ở trên.
                return False
            self.save_profiles(new_list)

        try:
            delete_proxy_password(profile_id)
        except SecretStoreCorruption as e:
            # The profile is already gone; do not resurrect metadata just
            # because the encrypted store is unreadable. Leave an audit log.
            _safe_log(
                f"[Storage] Profile {profile_id} đã xóa nhưng không dọn được secret: {e}"
            )
        except Exception as e:
            _safe_log(
                f"[Storage] Profile {profile_id} đã xóa nhưng dọn secret thất bại: "
                f"{type(e).__name__}: {e}"
            )
        return True

    def duplicate_profile(
        self, profile_id: str, new_name: Optional[str] = None, copy_data: bool = False
    ) -> Optional[Profile]:
        with self._lock:
            orig = self.get_profile(profile_id)
            if not orig:
                return None

            name = new_name or f"{orig.name} (Bản sao)"
            new_fp = generate_fingerprint(
                target_os=orig.fingerprint.os,
                timezone=orig.fingerprint.timezone,
                locale=orig.fingerprint.locale,
                latitude=orig.fingerprint.latitude or 21.0285,
                longitude=orig.fingerprint.longitude or 105.8542,
            )

            new_profile = self.create_profile(
                name=name,
                target_os=orig.fingerprint.os,
                proxy=orig.proxy.model_copy(),
                fingerprint=new_fp,
                notes=orig.notes,
                tags=orig.tags.copy(),
            )

            # M16: Tùy chọn sao chép dữ liệu phiên (cookies, session) nếu copy_data=True
            if copy_data and new_profile:
                orig_dir = os.path.join(PROFILES_DATA_DIR, profile_id)
                new_dir = os.path.join(PROFILES_DATA_DIR, new_profile.id)
                # Không nhân bản file cookies.json rời; phiên trình duyệt chỉ nằm trong user_data.
                orig_user_data = os.path.join(orig_dir, "user_data")
                new_user_data = os.path.join(new_dir, "user_data")
                if os.path.exists(orig_user_data):
                    # MED-4: nuốt lỗi copytree ở đây từng khiến bản sao nửa
                    # chừng/corrupt được báo thành công. Raise để API trả lỗi
                    # rõ ràng; profile mới đã tạo nhưng user_data trống.
                    try:
                        def ignore_locks(d, files):
                            return [f for f in files if f in ("lockfile", "DevToolsActivePort", "SingletonLock", "SingletonCookie", "SingletonSocket")]
                        shutil.copytree(orig_user_data, new_user_data, dirs_exist_ok=True, ignore=ignore_locks)
                    except Exception as e:
                        raise RuntimeError(
                            "Sao chép dữ liệu phiên (user_data) thất bại: "
                            f"{type(e).__name__}: {e}. Profile mới đã được tạo "
                            "nhưng KHÔNG có dữ liệu phiên."
                        ) from e

            return new_profile
