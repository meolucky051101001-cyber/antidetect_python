import os
import re
import sys
import json
import time
import uuid
import datetime
import socket
import shutil
import asyncio
import threading
import subprocess
import urllib.request
import collections
from typing import Optional, Dict, Any, List, Set

from .models import Profile
from .storage import ProfileManager, DATA_DIR, PROFILES_DATA_DIR
from .browser_runtime import BrowserRuntimeStore
from .cdp_health import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    REASON_DETACHED_OWNER_EXITED,
    REASON_HANDSHAKE_FAILED,
    REASON_HEARTBEAT_TIMEOUT,
    REASON_LEGACY_OWNERSHIP,
    REASON_OWNER_UNKNOWN,
    REASON_OWNED_BY_OTHER_INSTANCE,
    REASON_READER_TIMEOUT,
    REASON_RUNTIME_PERSIST_FAILED,
    REASON_SERVER_RESTART,
    REASON_SOCKET_CLOSED,
    REASON_SOCKET_SEND_FAILED,
    REASON_STOP_FAILED,
    REASON_TARGET_CONFIG_FAILED,
    REASON_USER_STOP,
    REASON_VERIFICATION_FAILED,
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_RECOVERY_REQUIRED,
    STATE_STARTING,
    STATE_STOPPED,
    STATE_STOPPING,
    CdpHealthRegistry,
    CdpUnavailable,
)
from .stealth import get_stealth_script, get_worker_stealth_script, get_webgl_limits
from .process_identity import browser_pids_using_dir, read_browser_identity
from .profile_locks import ProfileLockTimeout, ProfileLocks
from .applog import get_logger
from . import metrics
from .fingerprint import (
    normalize_fingerprint_for_launch,
    get_host_screen_info,
    host_os,
)
from .browser_env import (
    BROWSERS_DIR,
    build_webrtc_flags,
    find_browser_executable,
    get_free_port,
    release_port,
)
from .cookies import fetch_cookies_cdp, normalize_cookies, set_cookies_cdp
from .facebook import (
    TRANSIENT_TARGET_IDS,
    ScanAborted,
    classify_facebook_page,
    fb_unit_to_days as _fb_unit_to_days,
    fetch_pages_cdp,
    fetch_pages_deep_cdp,
    merge_page_details,
    parse_fb_created_date as _parse_fb_created_date,
)

log = get_logger("launcher")

SESSION_TABS_FILENAME = "antidetect_session_tabs.json"
MAX_RESTORED_TABS = 15


def _safe_run_async(coro):
    """Chạy coroutine an toàn, kể cả khi đang ở trong luồng có event loop đang chạy."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# P2 (review 2026-10-24): đo brands/fullVersionList thật của Chrome host ngay
# trước khi áp bất kỳ override nào. Chromium xoay grease brand theo major
# version ("Not(A:Brand" trên Chrome 155, khác bảng hardcode cũ) và sắp brand
# chính trước "Chromium", nên bảng tĩnh làm sai header sec-ch-ua.
# Đồng thời đo battery/connection THẬT của host: giả pin (vd 72%) trên desktop
# thật (100% + đang sạc) từng làm Pixelscan chấm "masking detected".
_NATIVE_BRANDS_JS = r"""
(async () => {
    try {
        if (!navigator.userAgentData) return null;
        const out = {
            brands: Array.from(navigator.userAgentData.brands || []).map(
                b => ({ brand: b.brand, version: b.version })
            )
        };
        try {
            const hev = await navigator.userAgentData.getHighEntropyValues(['fullVersionList']);
            if (hev && Array.isArray(hev.fullVersionList)) {
                out.fullVersionList = hev.fullVersionList.map(
                    b => ({ brand: b.brand, version: b.version })
                );
            }
        } catch (e) {}
        try {
            if (navigator.getBattery) {
                const b = await navigator.getBattery();
                // returnByValue serialize kiểu JSON: Infinity phải thành chuỗi.
                out.battery = {
                    level: b.level,
                    charging: b.charging,
                    chargingTime: b.chargingTime === Infinity ? 'Infinity' : b.chargingTime,
                    dischargingTime: b.dischargingTime === Infinity ? 'Infinity' : b.dischargingTime
                };
            }
        } catch (e) {}
        try {
            if (navigator.connection) {
                const c = navigator.connection;
                out.connection = {
                    effectiveType: c.effectiveType,
                    type: c.type || '',
                    rtt: c.rtt,
                    downlink: c.downlink,
                    saveData: !!c.saveData
                };
            }
        } catch (e) {}
        return out;
    } catch (e) { return null; }
})()
"""


class BrowserLauncher:

    def __init__(self, profile_manager: ProfileManager):
        self.manager = profile_manager
        self.active_processes: Dict[str, subprocess.Popen] = {}
        self.active_ports: Dict[str, int] = {}
        self._requested_ports: Dict[str, int] = {}
        self._proxy_credentials: Dict[str, tuple] = {}
        self._recovery_required: Set[str] = set()
        # Each launcher instance is a distinct CDP owner. A record written
        # by another instance cannot be trusted as a controllable channel.
        self.instance_id = f"launcher:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._runtime_store = BrowserRuntimeStore(
            os.path.join(DATA_DIR, "browser_runtime.json")
        )
        self.cdp_health = CdpHealthRegistry(on_change=self._queue_health_change)
        self._health_persist_cond = threading.Condition(threading.Lock())
        self._health_persist_pending: Dict[str, tuple] = {}
        self._health_persist_thread: Optional[threading.Thread] = None
        self._health_persist_stop = False
        self._health_persist_last: Dict[str, float] = {}
        self._health_persist_warned: Set[str] = set()
        try:
            self.browser_path = find_browser_executable()
        except FileNotFoundError:
            # Keep the dashboard/API available so the user can see a useful
            # launch error and install a supported browser without a crash loop.
            self.browser_path = None
        # C12: Lock per-profile để tránh 2 luồng launch/stop cùng profile
        self._launch_locks = collections.defaultdict(threading.RLock)
        # QĐ-6: lifecycle lock dùng chung với copy/delete ở process khác.
        self._lifecycle_locks = ProfileLocks(DATA_DIR)
        # H5: Event để CDP thread báo hiệu đã inject stealth thành công
        self._cdp_ready_events: Dict[str, threading.Event] = {}
        # R4: page configuration is split into two phases. The CDP thread
        # pauses on about:blank until the main thread verifies core values,
        # then navigates and reports when the new document has loaded.
        self._core_verified_events: Dict[str, threading.Event] = {}
        self._navigation_events: Dict[str, threading.Event] = {}
        self._page_load_events: Dict[str, Any] = {}
        # P2: brands/fullVersionList đo được từ Chrome host, cache theo
        # browser_version để không đo lại mỗi launch.
        self._native_brands_cache: Dict[str, dict] = {}
        # Batch start-all: nhiều launch đồng thời cùng thấy cache miss — khóa
        # để chỉ MỘT Chrome probe chạy, phần còn lại dùng kết quả sau.
        self._native_brands_probe_lock = threading.Lock()
        # Đăng ký dọn dẹp khi Python thoát để tránh tiến trình Chrome mồ côi
        self._cleanup_registered = True
        import atexit
        atexit.register(self._cleanup_on_exit)

    def unregister_cleanup(self):
        """Hủy đăng ký tự động dọn dẹp khi muốn chạy profile ở chế độ nền (detached)."""
        import atexit
        try:
            atexit.unregister(self._cleanup_on_exit)
        except Exception:
            pass
        self._cleanup_registered = False
        self.active_processes.clear()
        self.active_ports.clear()

    def _cleanup_on_exit(self):
        """Dừng các tiến trình Chrome do chính process này khởi chạy khi thoát.

        H1: KHÔNG quét runtime store — các browser do process khác (server
        hoặc phiên trước đó) mở phải sống sót; chúng được xác minh ownership
        qua _recover_runtime ở lần stop kế tiếp. Quét runtime store ở đây
        từng khiến `cli.py list` đóng toàn bộ browser của server.
        """
        for profile_id in list(self.active_processes):
            try:
                self.stop(profile_id)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # CDP health plumbing
    # ------------------------------------------------------------------
    def _queue_health_change(self, profile_id, launch_id, snapshot, event):
        """Coalesce health writes off the CDP thread.

        State transitions are persisted promptly; heartbeats are throttled to
        one write per 30s per profile so a 10s heartbeat cannot fsync forever.
        """
        if not launch_id:
            return
        with self._health_persist_cond:
            if event == "heartbeat":
                last = self._health_persist_last.get(str(profile_id), 0.0)
                if time.monotonic() - last < 30.0:
                    return
            self._health_persist_pending[str(profile_id)] = (
                launch_id,
                snapshot,
                event,
            )
            if (
                self._health_persist_thread is None
                or not self._health_persist_thread.is_alive()
            ):
                self._health_persist_thread = threading.Thread(
                    target=self._health_persist_loop,
                    name="cdp-health-persist",
                    daemon=True,
                )
                self._health_persist_thread.start()
            self._health_persist_cond.notify_all()

    def _health_persist_loop(self):
        while True:
            with self._health_persist_cond:
                while not self._health_persist_pending and not self._health_persist_stop:
                    self._health_persist_cond.wait(timeout=5.0)
                if self._health_persist_stop:
                    return
                items = list(self._health_persist_pending.items())
                self._health_persist_pending.clear()
            for profile_id, (launch_id, snapshot, _event) in items:
                self._persist_health_now(profile_id, launch_id, snapshot)
                with self._health_persist_cond:
                    self._health_persist_last[profile_id] = time.monotonic()

    def _persist_health_now(
        self,
        profile_id: str,
        launch_id: Optional[str],
        snapshot: Dict[str, Any],
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not launch_id or not snapshot:
            return False
        if snapshot.get("state") == STATE_STOPPED:
            return False
        owner = snapshot.get("owner_instance")
        if owner and owner != self.instance_id:
            # Never write into another launcher instance's record.
            return False
        fields = {
            "cdp_health": {
                key: snapshot.get(key)
                for key in (
                    "launch_id",
                    "owner_instance",
                    "pid",
                    "state",
                    "reason",
                    "verified_at",
                    "last_heartbeat_at",
                    "changed_at",
                    "checked_at",
                    "restart_required",
                )
            }
        }
        if extra:
            fields.update(extra)
        try:
            self._runtime_store.upsert_if_owner(
                str(profile_id),
                launch_id=launch_id,
                pid=snapshot.get("pid"),
                owner_instance=self.instance_id,
                **fields,
            )
            return True
        except Exception as exc:
            key = f"{profile_id}:{type(exc).__name__}"
            if key not in self._health_persist_warned:
                self._health_persist_warned.add(key)
                log.warning(
                    "[Launcher] Không ghi được cdp_health vào runtime: "
                    f"{type(exc).__name__}: {exc}"
                )
            return False

    def _cdp_process_alive(self, profile_id: str) -> bool:
        """Liveness check safe to call from the CDP thread (no metadata I/O)."""
        proc = self.active_processes.get(str(profile_id))
        return bool(proc and proc.poll() is None)

    def cdp_health_snapshot(
        self, profile_id: str, running: Optional[bool] = None
    ) -> Dict[str, Any]:
        if running is None:
            running = self.is_running(profile_id)
        if not running:
            snapshot = self.cdp_health.snapshot(profile_id)
            if snapshot.get("state") in (
                STATE_STARTING,
                STATE_HEALTHY,
                STATE_DEGRADED,
                STATE_RECOVERY_REQUIRED,
                STATE_STOPPING,
            ):
                self.cdp_health.mark_stopped(profile_id)
                snapshot = self.cdp_health.snapshot(profile_id)
            return snapshot
        snapshot = self.cdp_health.snapshot(profile_id)
        if snapshot.get("state") == STATE_STOPPED:
            record = self._runtime_store.get(profile_id) or {}
            launch_id = record.get("launch_id")
            reason = (
                REASON_OWNER_UNKNOWN if launch_id else REASON_LEGACY_OWNERSHIP
            )
            self._recovery_required.add(str(profile_id))
            self.cdp_health.mark_recovery_required(
                str(profile_id),
                reason,
                owner_instance=self.instance_id,
                pid=record.get("pid"),
                launch_id=launch_id,
            )
            snapshot = self.cdp_health.snapshot(profile_id)
        return snapshot

    def cdp_guard(
        self, profile_id: str, *, running: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Single guard for every CDP-dependent operation."""
        if running is None:
            running = self.is_running(profile_id)
        if not running:
            return {
                "ok": False,
                "code": "NOT_RUNNING",
                "message": "Profile chưa chạy.",
                "health": self.cdp_health_snapshot(profile_id, running=False),
            }
        snapshot = self.cdp_health_snapshot(profile_id, running=True)
        state = snapshot.get("state")
        if state == STATE_HEALTHY and snapshot.get("owner_instance") == self.instance_id:
            self._recovery_required.discard(str(profile_id))
            return {"ok": True, "code": None, "message": "", "health": snapshot}
        if state == STATE_RECOVERY_REQUIRED or profile_id in self._recovery_required:
            if snapshot.get("reason") == REASON_DETACHED_OWNER_EXITED:
                code = "DETACHED_NO_CDP"
                message = (
                    "Chrome đang mở nhưng chạy detached nên không có CDP. "
                    "Hãy dừng profile rồi mở lại từ server để dùng thao tác này."
                )
            else:
                code = "RECOVERY_REQUIRED"
                message = (
                    "CDP của phiên này chưa được xác minh quyền điều khiển. "
                    "Hãy dừng profile rồi khởi chạy lại."
                )
            return {"ok": False, "code": code, "message": message, "health": snapshot}
        if state == STATE_STARTING:
            return {
                "ok": False,
                "code": "CDP_STARTING",
                "message": "CDP đang khởi tạo; hãy thử lại sau vài giây.",
                "health": snapshot,
            }
        if state == STATE_HEALTHY:
            self._recovery_required.add(str(profile_id))
            return {
                "ok": False,
                "code": "RECOVERY_REQUIRED",
                "message": (
                    "Chrome đang được điều khiển bởi một tiến trình khác. "
                    "Hãy dừng profile rồi mở lại từ tiến trình hiện tại."
                ),
                "health": snapshot,
            }
        detail = snapshot.get("reason") or state
        return {
            "ok": False,
            "code": "CDP_UNHEALTHY",
            "message": (
                f"Chrome đang mở nhưng CDP không khỏe ({detail}). "
                "Hãy dừng rồi khởi chạy lại profile."
            ),
            "health": snapshot,
        }

    def require_cdp(self, profile_id: str) -> Dict[str, Any]:
        guard = self.cdp_guard(profile_id)
        if not guard["ok"]:
            raise CdpUnavailable(guard["code"], guard["message"], guard["health"])
        return guard["health"]

    def _running_launch_result(
        self, profile_id: str, open_url: Optional[str] = None
    ) -> Dict[str, Any]:
        guard = self.cdp_guard(profile_id, running=True)
        if not guard["ok"]:
            result = {
                "success": False,
                "code": guard["code"],
                "error": guard["message"],
                "cdp_health": guard["health"],
                "launched_now": False,
            }
            if guard["code"] in ("RECOVERY_REQUIRED", "DETACHED_NO_CDP"):
                result["recovery_required"] = True
            return result
        port = self.active_ports.get(profile_id)
        if open_url:
            if not self._open_tab_impl(profile_id, open_url):
                return {
                    "success": False,
                    "code": "CDP_UNHEALTHY",
                    "error": "Chrome đang chạy nhưng không thể mở tab mới.",
                    "cdp_health": guard["health"],
                    "launched_now": False,
                }
            return {
                "success": True,
                "message": "Profile đang chạy, đã mở trong tab mới",
                "port": port,
                "cdp_health": guard["health"],
                "launched_now": False,
            }
        return {
            "success": True,
            "message": "Đang chạy",
            "port": port,
            "cdp_health": guard["health"],
            "launched_now": False,
        }

    def mark_detached(self, profile_id: str) -> Dict[str, Any]:
        """Persist the detached (no-CDP-owner) state before the CLI exits."""
        record = self._runtime_store.get(profile_id) or {}
        launch_id = self.cdp_health.current_launch_id(profile_id) or record.get(
            "launch_id"
        )
        pid = record.get("pid")
        self._recovery_required.add(str(profile_id))
        self.cdp_health.mark_recovery_required(
            str(profile_id),
            REASON_DETACHED_OWNER_EXITED,
            owner_instance=self.instance_id,
            pid=pid,
            launch_id=launch_id,
            force=True,
        )
        snapshot = self.cdp_health.snapshot(profile_id)
        self._persist_health_now(
            str(profile_id), launch_id, snapshot, extra={"detached": True}
        )
        return snapshot

    def _identity_for(self, pid, user_data_dir):
        """Verify ownership for either a live Popen PID or a recovered record."""
        if not pid:
            return {"state": "absent", "port": None, "created": None}
        return read_browser_identity(pid, user_data_dir or "")

    def _identity_for_stop(self, pid, user_data_dir, attempts: int = 3):
        """Retry transient unknown identity before refusing to send a kill.

        CIM/PowerShell can time out while many browsers are starting; treating
        one failed query as a permanent conflict made stop/stop_all return 409
        for a perfectly healthy process. Ownership is still required — this
        only retries the query, it never kills blind.
        """
        identity = {"state": "unknown", "port": None, "created": None}
        for attempt in range(max(1, attempts)):
            identity = self._identity_for(pid, user_data_dir)
            if identity["state"] != "unknown":
                return identity
            if attempt + 1 < attempts:
                time.sleep(0.2)
        return identity

    def _clear_process(self, profile_id):
        record = self._runtime_store.get(profile_id) or {}
        requested = self._requested_ports.pop(profile_id, None) or record.get("requested_port")
        active = self.active_ports.pop(profile_id, None) or record.get("port")
        self.active_processes.pop(profile_id, None)
        self._recovery_required.discard(profile_id)
        self.cdp_health.mark_stopped(profile_id)
        self._proxy_credentials.pop(profile_id, None)
        self._cdp_ready_events.pop(profile_id, None)
        self._core_verified_events.pop(profile_id, None)
        self._navigation_events.pop(profile_id, None)
        release_port(requested)
        if active and active != requested:
            release_port(active)
        try:
            self._runtime_store.remove(profile_id)
        except Exception:
            pass
        # P0-02: Dọn dẹp thư mục tiện ích mở rộng proxy auth tạm thời nếu có
        try:
            profile_dir = self.manager.get_profile_dir(profile_id)
            ext_dir = os.path.join(profile_dir, "proxy_auth_extension")
            if os.path.exists(ext_dir):
                shutil.rmtree(ext_dir, ignore_errors=True)
        except Exception:
            pass

    def _identity(self, profile):
        from .storage import PROFILES_DATA_DIR
        return read_browser_identity(
            profile.pid, os.path.join(PROFILES_DATA_DIR, profile.id, "user_data")
        )

    def _recover_runtime(self, profile_id: str):
        """Return a persisted launch record after verifying process ownership.

        A record from another process (or from a previous server life) proves
        the browser exists, never that this process owns a usable CDP channel.
        Returns None when the record is stale or was removed. On query failure
        the record is preserved, matching stop()'s refusal to kill blind.
        """
        record = self._runtime_store.get(profile_id)
        if not record:
            return None
        identity = read_browser_identity(
            record.get("pid"), record.get("user_data_dir") or ""
        )
        if identity.get("state") == "owned":
            actual_port = identity.get("port") or record.get("port")
            if actual_port:
                self.active_ports[profile_id] = int(actual_port)
            if identity.get("created"):
                try:
                    if record.get("launch_id"):
                        self._runtime_store.upsert_if_owner(
                            profile_id,
                            launch_id=record.get("launch_id"),
                            pid=record.get("pid"),
                            created=identity.get("created"),
                        )
                    else:
                        self._runtime_store.upsert(
                            profile_id, created=identity.get("created")
                        )
                except Exception:
                    pass
            self._recovery_required.add(profile_id)
            persisted = record.get("cdp_health") or {}
            persisted_reason = (
                persisted.get("reason")
                if persisted.get("state") == STATE_RECOVERY_REQUIRED
                else None
            )
            owner = record.get("owner_instance")
            if record.get("detached") or persisted_reason == REASON_DETACHED_OWNER_EXITED:
                reason = REASON_DETACHED_OWNER_EXITED
            elif persisted_reason:
                reason = persisted_reason
            elif not owner:
                reason = REASON_LEGACY_OWNERSHIP
            elif owner != self.instance_id:
                reason = REASON_OWNED_BY_OTHER_INSTANCE
            else:
                reason = REASON_SERVER_RESTART
            self.cdp_health.mark_recovery_required(
                profile_id,
                reason,
                owner_instance=owner or self.instance_id,
                pid=record.get("pid"),
                launch_id=record.get("launch_id"),
            )
            return record
        if identity.get("state") in ("absent", "mismatch"):
            self._clear_process(profile_id)
            return None
        # Preserve persisted state on query failure; stop() will refuse to kill.
        self._recovery_required.add(profile_id)
        self.cdp_health.mark_recovery_required(
            profile_id,
            REASON_OWNER_UNKNOWN,
            owner_instance=record.get("owner_instance") or self.instance_id,
            pid=record.get("pid"),
            launch_id=record.get("launch_id"),
        )
        return record

    def is_running(self, profile_id: str) -> bool:
        """Process liveness only. Never clears or sets CDP health."""
        proc = self.active_processes.get(profile_id)
        if proc:
            if proc.poll() is None:
                return True
            self._clear_process(profile_id)
            return False
        record = self._recover_runtime(profile_id)
        if record is not None:
            return True
        profile = self.manager.get_profile(profile_id)
        if not profile or not profile.pid:
            return False
        # Legacy fallback for launches created before runtime persistence.
        identity = self._identity(profile)
        if identity["state"] == "owned":
            self._recovery_required.add(profile_id)
            self.cdp_health.mark_recovery_required(
                profile_id,
                REASON_LEGACY_OWNERSHIP,
                owner_instance=self.instance_id,
                pid=profile.pid,
            )
            if identity.get("port"):
                self.active_ports[profile_id] = identity["port"]
            return True
        if identity["state"] in ("absent", "mismatch"):
            self._clear_process(profile_id)
            return False
        # Preserve persisted state on query failure; stop() will refuse to kill.
        self._recovery_required.add(profile_id)
        self.cdp_health.mark_recovery_required(
            profile_id,
            REASON_OWNER_UNKNOWN,
            owner_instance=self.instance_id,
            pid=profile.pid,
        )
        return True

    def profile_data_in_use(self, profile_id: str) -> bool:
        """True when this tool or an external Chromium owns the user_data dir."""
        if self.is_running(profile_id):
            return True
        user_data_dir = os.path.join(PROFILES_DATA_DIR, profile_id, "user_data")
        if not os.path.isdir(user_data_dir):
            return False
        exclude = {
            proc.pid
            for proc in self.active_processes.values()
            if proc is not None and proc.poll() is None
        }
        pids = browser_pids_using_dir(user_data_dir, exclude_pids=exclude)
        # None means the process table was unavailable; do not block copies on
        # an unanswerable query (the source is already not tracked as running).
        return bool(pids)

    # ------------------------------------------------------------------
    # Lưu & khôi phục các tab đang mở của từng profile qua các lần tắt/bật
    # ------------------------------------------------------------------
    @staticmethod
    def _session_tabs_path(user_data_dir: str) -> str:
        return os.path.join(user_data_dir, SESSION_TABS_FILENAME)

    @classmethod
    def _filter_restorable_urls(cls, urls: Any) -> List[str]:
        """Chỉ giữ các URL web thực sự (http/https), loại bỏ about:blank và trang nội bộ."""
        if not urls:
            return []
        cleaned: List[str] = []
        for raw in urls:
            if not isinstance(raw, str):
                continue
            url = raw.strip()
            if not url:
                continue
            lower = url.lower()
            if not (lower.startswith("http://") or lower.startswith("https://")):
                continue
            if lower.startswith(("http://127.0.0.1:", "http://localhost:")):
                if "/json/" in lower or "/devtools/" in lower:
                    continue
            if cleaned and cleaned[-1] == url:
                continue
            cleaned.append(url)
            if len(cleaned) >= MAX_RESTORED_TABS:
                break
        return cleaned

    def _save_session_tabs(self, user_data_dir: Optional[str], urls: Any) -> bool:
        """Ghi nguyên tử danh sách tab đang mở vào user_data_dir của profile.

        Chỉ cập nhật khi có ít nhất 1 URL http(s) hợp lệ để thao tác đóng trình
        duyệt (lúc các tab lần lượt bị detach) không xóa mất danh sách đã lưu.
        """
        if not user_data_dir or not os.path.isdir(user_data_dir):
            return False
        filtered = self._filter_restorable_urls(urls)
        if not filtered:
            return False
        path = self._session_tabs_path(user_data_dir)
        tmp_path = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
        payload = {
            "urls": filtered,
            "updated_at": int(time.time()),
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return True
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

    def _load_session_tabs(self, user_data_dir: Optional[str]) -> List[str]:
        """Đọc danh sách tab đã lưu từ lần chạy trước của profile."""
        if not user_data_dir:
            return []
        path = self._session_tabs_path(user_data_dir)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return self._filter_restorable_urls(data.get("urls") or [])
            if isinstance(data, list):
                return self._filter_restorable_urls(data)
        except Exception:
            pass
        return []

    def _snapshot_tabs_via_http(
        self, port: Optional[int], user_data_dir: Optional[str]
    ) -> List[str]:
        """Chụp nhanh danh sách tab qua /json/list ngay trước khi dừng profile."""
        if not port or not user_data_dir:
            return []
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/json/list", timeout=1.0
            ) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(targets, list):
                return []
            urls = [
                t.get("url")
                for t in targets
                if isinstance(t, dict)
                and t.get("type") == "page"
                and str(t.get("id") or "") not in TRANSIENT_TARGET_IDS
            ]
            # /json/list thường trả tab mới mở trước; đảo lại để giữ thứ tự tab.
            urls.reverse()
            filtered = self._filter_restorable_urls(urls)
            if filtered:
                self._save_session_tabs(user_data_dir, filtered)
            return filtered
        except Exception:
            return []

    @staticmethod
    def _prepare_clean_chrome_preferences(user_data_dir: Optional[str]) -> None:
        """Đánh dấu trạng thái thoát sạch trong Preferences để tránh hộp thoại báo crash."""
        if not user_data_dir or not os.path.isdir(user_data_dir):
            return
        prefs_path = os.path.join(user_data_dir, "Default", "Preferences")
        if not os.path.isfile(prefs_path):
            return
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            if not isinstance(prefs, dict):
                return
            prof = prefs.get("profile")
            if not isinstance(prof, dict):
                prof = {}
                prefs["profile"] = prof
            if prof.get("exit_type") == "Normal" and prof.get("exited_cleanly") is True:
                return
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            tmp_path = f"{prefs_path}.{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False)
            os.replace(tmp_path, prefs_path)
        except Exception:
            pass

    async def _probe_page_ready(self, port):
        """Confirm a page-domain reply without treating it as emulation validation."""
        import websockets
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                def pages():
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                        return json.loads(r.read())
                targets = await asyncio.to_thread(pages)
                target = next(t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl"))
                async with websockets.connect(target["webSocketDebuggerUrl"], open_timeout=1, close_timeout=1) as ws:
                    await ws.send(json.dumps({"id": 91001, "method": "Page.getFrameTree"}))
                    end = min(deadline, time.monotonic() + 2)
                    while time.monotonic() < end:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), max(.01, end-time.monotonic())))
                        if reply.get("id") == 91001:
                            return "error" not in reply and bool(reply.get("result", {}).get("frameTree"))
            except Exception:
                await asyncio.sleep(.15)
        return False

    def _measure_native_brands_standalone(self, browser_path: Optional[str] = None) -> Optional[dict]:
        """Launch một Chrome phụ (user-data-dir tạm) và đọc UA-CH brands thật.

        P2: `Network.setUserAgentOverride(userAgentMetadata)` điều khiển cả JS
        API `navigator.userAgentData`, nên không thể đo trên phiên đã override.
        Chromium xoay grease brand theo major version (Chrome 155 thật dùng
        "Not(A:Brand";v="24" và đặt brand chính trước "Chromium") — bảng
        hardcode lệch khác đó và làm sai header sec-ch-ua. Chrome phụ dùng
        user-data-dir temp riêng (không chạm data/ của tool), không headless,
        và bị đóng ngay sau khi đo. Trả None nếu bất kỳ bước nào lỗi.
        """
        browser_path = browser_path or self.browser_path
        if not browser_path:
            return None
        import tempfile
        import websockets

        user_data = tempfile.mkdtemp(prefix="antidetect-brand-probe-")
        port = get_free_port()
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    browser_path,
                    f"--user-data-dir={user_data}",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            page_ws = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    def list_pages():
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/json/list", timeout=1
                        ) as r:
                            return json.loads(r.read())

                    targets = list_pages()
                    page = next(
                        (
                            t for t in targets
                            if t.get("type") == "page"
                            and t.get("webSocketDebuggerUrl")
                        ),
                        None,
                    )
                    if page:
                        page_ws = page["webSocketDebuggerUrl"]
                        break
                except Exception:
                    time.sleep(0.2)
            if not page_ws:
                return None

            async def _probe():
                async with websockets.connect(page_ws, open_timeout=2) as ws:
                    await ws.send(json.dumps({
                        "id": 70001,
                        "method": "Page.navigate",
                        "params": {"url": f"http://127.0.0.1:{port}/json/version"},
                    }))
                    # 127.0.0.1 là secure context: userAgentData xuất hiện ở đó
                    # (about:blank thì không).
                    probe_deadline = time.monotonic() + 6.0
                    while time.monotonic() < probe_deadline:
                        await ws.send(json.dumps({
                            "id": 70002,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": _NATIVE_BRANDS_JS,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }))
                        end = min(probe_deadline, time.monotonic() + 2.0)
                        while time.monotonic() < end:
                            msg = json.loads(
                                await asyncio.wait_for(
                                    ws.recv(), timeout=max(0.05, end - time.monotonic())
                                )
                            )
                            if msg.get("id") != 70002:
                                continue
                            value = (
                                (msg.get("result") or {}).get("result") or {}
                            ).get("value")
                            if (
                                isinstance(value, dict)
                                and isinstance(value.get("brands"), list)
                                and value["brands"]
                            ):
                                return value
                            break
                        await asyncio.sleep(0.15)
                    return None

            return _safe_run_async(_probe())
        except Exception:
            return None
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            release_port(port)
            shutil.rmtree(user_data, ignore_errors=True)

    async def _verify_emulation(
        self, port, fp, require_client_hints=False, deadline_seconds=7.0
    ):
        """Check the values a page actually sees after CDP injection.

        Returns a dict with per-check details. The first call runs against the
        initial about:blank document and checks everything that does not require
        a secure context. A second call after navigation can require Client
        Hints, which are absent on about:blank.
        """
        import websockets
        deadline = time.monotonic() + max(0.5, float(deadline_seconds))
        expr = r"""
(async () => {
    const out = {
        ua: navigator.userAgent,
        dpr: devicePixelRatio,
        sw: screen.width,
        sh: screen.height,
        cd: screen.colorDepth,
        hc: navigator.hardwareConcurrency,
        dm: navigator.deviceMemory,
        lang: navigator.language,
        langs: (navigator.languages || []).join(','),
        wd: navigator.webdriver,
        platform: navigator.platform,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        cssDeviceW: matchMedia('(device-width: ' + screen.width + 'px)').matches,
        cssDeviceH: matchMedia('(device-height: ' + screen.height + 'px)').matches,
        cssDpr: matchMedia('(resolution: ' + devicePixelRatio + 'dppx)').matches
    };
    try {
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
        out.glVendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null;
        out.glRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
        out.glMaxTexture = gl ? gl.getParameter(gl.MAX_TEXTURE_SIZE) : null;
        out.glMaxViewport = gl ? Array.from(gl.getParameter(gl.MAX_VIEWPORT_DIMS)) : null;
    } catch (e) { out.glError = String(e); }
    try {
        if (navigator.userAgentData) {
            out.chBrands = (navigator.userAgentData.brands || []).map(b => b.brand).join('|');
            out.chPlatform = navigator.userAgentData.platform;
        }
    } catch (e) { out.chError = String(e); }
    return out;
})()
"""
        expected_platform = (
            "MacIntel" if fp.os == "macos"
            else ("Linux x86_64" if fp.os == "linux" else "Win32")
        )
        expected_ch_platform = (
            "macOS" if fp.os == "macos"
            else ("Linux" if fp.os == "linux" else "Windows")
        )
        expected_brand = (
            "Microsoft Edge" if getattr(fp, "browser_brand", "chrome") == "edge"
            else ("Chromium" if getattr(fp, "browser_brand", "chrome") == "chromium"
                  else "Google Chrome")
        )
        # Authentic mode disables the WebGL hook, so the page reports the
        # host GPU. Comparing it to the stored fingerprint would fail on every
        # launch and mark a healthy profile as degraded.
        webgl_spoofed = "webgl" not in (getattr(fp, "stealth_disable", None) or [])
        expected_texture = None
        expected_viewport = None
        limits = get_webgl_limits(fp)
        if limits:
            expected_texture = int(limits["maxTextureSize"])
            expected_viewport = list(limits["maxViewportDims"])
        elif fp.os == "windows":
            expected_texture = 16384
            expected_viewport = [32767, 32767]
        elif fp.os == "macos":
            expected_texture = 16384
            expected_viewport = [16384, 16384]
        while time.monotonic() < deadline:
            try:
                def pages():
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=1
                    ) as r:
                        return json.loads(r.read())

                targets = await asyncio.to_thread(pages)
                target = next(
                    t for t in targets
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                )
                async with websockets.connect(
                    target["webSocketDebuggerUrl"],
                    open_timeout=1,
                    close_timeout=1,
                ) as ws:
                    await ws.send(json.dumps({
                        "id": 91002,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expr,
                            "returnByValue": True,
                            "awaitPromise": True,
                        },
                    }))
                    end = min(deadline, time.monotonic() + 1.5)
                    while time.monotonic() < end:
                        reply = json.loads(
                            await asyncio.wait_for(
                                ws.recv(), max(0.01, end - time.monotonic())
                            )
                        )
                        if reply.get("id") != 91002:
                            continue
                        value = (
                            reply.get("result", {})
                            .get("result", {})
                            .get("value")
                        )
                        if not isinstance(value, dict):
                            break
                        checks = {}
                        try:
                            checks["user_agent"] = value.get("ua") == fp.user_agent
                            checks["device_pixel_ratio"] = (
                                abs(float(value.get("dpr", 0))
                                    - float(fp.device_pixel_ratio or 1.0)) < 0.01
                            )
                            checks["screen_width"] = (
                                int(value.get("sw", 0)) == int(fp.screen_width)
                            )
                            checks["screen_height"] = (
                                int(value.get("sh", 0)) == int(fp.screen_height)
                            )
                            checks["color_depth"] = (
                                int(value.get("cd", 0)) == int(fp.screen_color_depth or 24)
                            )
                            checks["hardware_concurrency"] = (
                                int(value.get("hc", 0)) == int(fp.hardware_concurrency)
                            )
                            checks["device_memory"] = (
                                int(value.get("dm", 0))
                                == int(fp.device_memory or 8)
                            )
                            checks["language"] = value.get("lang") == fp.locale
                            checks["languages"] = (
                                value.get("langs") == ",".join(fp.languages or [fp.locale])
                            )
                            checks["webdriver"] = value.get("wd") is False
                            checks["platform"] = value.get("platform") == expected_platform
                            checks["timezone"] = value.get("tz") in (
                                fp.timezone,
                                "Asia/Saigon" if fp.timezone == "Asia/Ho_Chi_Minh" else fp.timezone,
                            )
                            if webgl_spoofed:
                                checks["webgl_vendor"] = (
                                    value.get("glVendor") == fp.webgl_vendor
                                )
                                checks["webgl_renderer"] = (
                                    value.get("glRenderer") == fp.webgl_renderer
                                )
                            checks["css_device_width"] = value.get("cssDeviceW") is True
                            checks["css_device_height"] = value.get("cssDeviceH") is True
                            checks["css_device_pixel_ratio"] = value.get("cssDpr") is True
                            if webgl_spoofed and expected_texture is not None:
                                checks["webgl_max_texture"] = (
                                    int(value.get("glMaxTexture") or 0) == expected_texture
                                )
                            if webgl_spoofed and expected_viewport is not None:
                                checks["webgl_max_viewport"] = (
                                    list(value.get("glMaxViewport") or [])
                                    == expected_viewport
                                )
                            # navigator.userAgentData is undefined on some
                            # non-secure contexts (notably about:blank). The
                            # first pass therefore treats it as optional; the
                            # post-navigation pass requires it for HTTPS pages.
                            if value.get("chBrands") is not None:
                                checks["client_hints_brand"] = (
                                    expected_brand in (value.get("chBrands") or "")
                                )
                                checks["client_hints_platform"] = (
                                    value.get("chPlatform") == expected_ch_platform
                                )
                            elif require_client_hints:
                                checks["client_hints_available"] = False
                        except (TypeError, ValueError):
                            checks["parse"] = False
                        return {
                            "ok": bool(checks) and all(checks.values()),
                            "checks": checks,
                            "actual": value,
                        }
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return {"ok": False, "checks": {}, "actual": None, "error": "timeout"}

    async def _setup_cdp_stealth(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
        fp_override: Optional[Any] = None,
        launch_id: Optional[str] = None,
    ):
        """Inject stealth into every page/worker/OOPIF target via CDP."""
        import websockets

        session = None
        if launch_id:
            session = self.cdp_health.bind_session(profile.id, launch_id)
            if session is None:
                # A newer launch owns this profile; let the old coroutine die
                # without touching the new generation's health.
                return
        handshake_established = False

        # Use the launch-time normalized copy, not the stored profile. The
        # stored model may still contain legacy RAM/screen values; the injected
        # JS and the verification probe must see the same values.
        fp = fp_override or profile.fingerprint
        native_brands = self._native_brands_cache.get(fp.browser_version)
        stealth_js = get_stealth_script(fp, native_brands=native_brands)
        worker_js = get_worker_stealth_script(fp, native_brands=native_brands)

        # Wait for Chrome's DevTools endpoint. DevToolsActivePort is more
        # reliable than the requested port because Chrome may choose another.
        ws_url = None
        for _ in range(25):
            if user_data_dir:
                active_port_file = os.path.join(user_data_dir, "DevToolsActivePort")
                if os.path.exists(active_port_file):
                    try:
                        with open(active_port_file, "r", encoding="utf-8") as _pf:
                            first_line = _pf.readline().strip()
                            if first_line.isdigit():
                                port = int(first_line)
                                self.active_ports[profile.id] = port
                                try:
                                    self._runtime_store.upsert(
                                        profile.id, port=port, cdp_port_source="DevToolsActivePort"
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
            try:
                url = f"http://127.0.0.1:{port}/json/version"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=1) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        break
            except Exception:
                await asyncio.sleep(0.3)

        if not ws_url:
            if session is not None:
                session.mark_closed()
            if (
                launch_id
                and self._cdp_process_alive(profile.id)
                and not self.cdp_health.is_stopping(profile.id)
            ):
                self.cdp_health.mark_degraded(
                    profile.id,
                    launch_id,
                    REASON_HANDSHAKE_FAILED,
                    restart_required=True,
                )
            log.warning(f"[Launcher] Cảnh báo: Không thể lấy CDP URL trên port {port}")
            return

        platform_str = "Win32"
        if fp.os == "macos":
            platform_str = "MacIntel"
        elif fp.os == "linux":
            platform_str = "Linux x86_64"

        ch_full = fp.browser_version or "155.0.8059.85"
        ch_major = ch_full.split(".", 1)[0]
        ch_platform = "Windows"
        ch_platform_ver = "15.0.0"
        ch_arch = "x86"
        if fp.os == "macos":
            ch_platform = "macOS"
            ch_platform_ver = "14.5.0"
            ch_arch = "arm"
        elif fp.os == "linux":
            ch_platform = "Linux"
            ch_platform_ver = "6.5.0"

        ua_metadata = {
            "brands": [
                {"brand": "Chromium", "version": ch_major},
                {"brand": "Google Chrome", "version": ch_major},
                {"brand": "Not_A Brand", "version": "24"},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": ch_full},
                {"brand": "Google Chrome", "version": ch_full},
                {"brand": "Not_A Brand", "version": "24.0.0.0"},
            ],
            "fullVersion": ch_full,
            "platform": ch_platform,
            "platformVersion": ch_platform_ver,
            "architecture": ch_arch,
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        }
        # Chrome adds q-values itself. Supplying them here produces
        # "vi;q=0.9;q=0.9" on the wire.
        accept_lang_str = ",".join(fp.languages or [fp.locale])

        if native_brands:
            ua_metadata["brands"] = native_brands["brands"]
            if native_brands.get("fullVersionList"):
                ua_metadata["fullVersionList"] = native_brands["fullVersionList"]

        pending_responses: Dict[int, asyncio.Future] = {}
        target_tasks: Set[asyncio.Task] = set()
        try:
            async with websockets.connect(
                ws_url, max_size=20 * 1024 * 1024
            ) as ws:
                msg_id = 0
                attached_sessions: Set[str] = set()

                async def send(method, params=None, session_id=None):
                    nonlocal msg_id
                    msg_id += 1
                    current_id = msg_id
                    payload = {"id": current_id, "method": method}
                    if params is not None:
                        payload["params"] = params
                    if session_id:
                        payload["sessionId"] = session_id
                    fut = asyncio.get_running_loop().create_future()
                    pending_responses[current_id] = fut
                    # Bounded send + bounded response: a half-open socket must
                    # not hold a configuration task forever.
                    await asyncio.wait_for(
                        ws.send(json.dumps(payload)),
                        timeout=HEARTBEAT_TIMEOUT_SECONDS,
                    )
                    try:
                        res = await asyncio.wait_for(
                            fut, timeout=HEARTBEAT_TIMEOUT_SECONDS
                        )
                        if "error" in res:
                            raise RuntimeError(f"CDP {method} error: {res['error']}")
                        return res.get("result", {})
                    finally:
                        pending_responses.pop(current_id, None)

                def spawn_target_task(coro, session_id):
                    """Run target setup without leaking expected close races as task tracebacks."""
                    async def guarded():
                        try:
                            await coro
                        except Exception:
                            # If setup failed before resuming a paused target, make a
                            # best effort to unpause it so Chrome does not keep a
                            # dead/worker target frozen.
                            try:
                                await send(
                                    "Runtime.runIfWaitingForDebugger",
                                    session_id=session_id,
                                )
                            except Exception:
                                pass
                            raise

                    task = asyncio.create_task(guarded())
                    target_tasks.add(task)
                    task.add_done_callback(target_tasks.discard)

                    def consume(task_obj):
                        if task_obj.cancelled():
                            return
                        try:
                            exc = task_obj.exception()
                        except Exception:
                            return
                        if exc is None:
                            return
                        text = str(exc)
                        expected = (
                            "Cannot find default execution context",
                            "Session closed",
                            "No target with given id",
                            "Target closed",
                            "Inspected target navigated or closed",
                            "Session with given id not found",
                        )
                        stopping = launch_id and self.cdp_health.is_stopping(profile.id)
                        if any(marker in text for marker in expected):
                            return
                        if isinstance(exc, asyncio.TimeoutError):
                            # A timeout while the target is gone is a normal
                            # close race. A timeout on a still-attached target
                            # is a configuration failure.
                            if (
                                session_id not in attached_sessions
                                or not self._cdp_process_alive(profile.id)
                                or stopping
                            ):
                                return
                        log.error(
                            "[Launcher] CDP background task error: "
                            f"{type(exc).__name__}: {text}"
                        )
                        if launch_id and not stopping and self._cdp_process_alive(
                            profile.id
                        ):
                            self.cdp_health.mark_degraded(
                                profile.id,
                                launch_id,
                                REASON_TARGET_CONFIG_FAILED,
                                restart_required=True,
                            )

                    task.add_done_callback(consume)
                    return task

                is_first_tab = True
                session_restore_done = False
                sync_tabs_lock = asyncio.Lock()

                async def sync_session_tabs(delay: float = 0.0):
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if not session_restore_done:
                        return
                    if not self._cdp_process_alive(profile.id):
                        return
                    if launch_id and self.cdp_health.is_stopping(profile.id):
                        return
                    if sync_tabs_lock.locked():
                        return
                    async with sync_tabs_lock:
                        if not self._cdp_process_alive(profile.id):
                            return
                        if launch_id and self.cdp_health.is_stopping(profile.id):
                            return
                        try:
                            res = await send("Target.getTargets")
                            infos = (res or {}).get("targetInfos") or []
                            urls = [
                                t.get("url")
                                for t in infos
                                if isinstance(t, dict)
                                and t.get("type") == "page"
                                and str(t.get("targetId") or "") not in TRANSIENT_TARGET_IDS
                            ]
                            if urls:
                                await asyncio.to_thread(
                                    self._save_session_tabs, user_data_dir, urls
                                )
                        except Exception:
                            pass

                async def apply_screen_emulation(session_id):
                    """S3: let Chrome's native layer set screen.* and CSS media.

                    width/height=0 leaves the real viewport alone while
                    screenWidth/screenHeight/deviceScaleFactor drive both JS and
                    @media (device-width/height/resolution). This removes the
                    need for Screen.prototype and matchMedia hooks.
                    """
                    try:
                        await send(
                            "Emulation.setDeviceMetricsOverride",
                            {
                                "width": 0,
                                "height": 0,
                                "deviceScaleFactor": float(fp.device_pixel_ratio or 1.0),
                                "mobile": False,
                                "screenWidth": int(fp.screen_width),
                                "screenHeight": int(fp.screen_height),
                            },
                            session_id=session_id,
                        )
                    except Exception as exc:
                        log.warning(
                            "[Launcher] Không đặt được screen emulation: "
                            f"{type(exc).__name__}: {exc}"
                        )

                async def configure_page(session_id, target_info):
                    nonlocal is_first_tab, session_restore_done
                    first_tab = is_first_tab
                    if first_tab:
                        is_first_tab = False
                    await send("Page.enable", session_id=session_id)
                    await apply_screen_emulation(session_id)
                    # Browser-level auto-attach discovers pages, but workers and
                    # OOPIFs are subtargets of a page. Enable auto-attach on the
                    # page session as well; otherwise module workers and
                    # cross-origin frames run without the stealth payload.
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth_js},
                        session_id=session_id,
                    )
                    # Page.addScript only covers future documents. The initial
                    # about:blank document already exists, so apply the same
                    # payload now before the R4 core verification pass.
                    try:
                        await send(
                            "Runtime.evaluate",
                            {"expression": stealth_js, "silent": True},
                            session_id=session_id,
                        )
                    except Exception:
                        pass
                    await send(
                        "Emulation.setTimezoneOverride",
                        {"timezoneId": fp.timezone},
                        session_id=session_id,
                    )
                    if fp.latitude is not None and fp.longitude is not None:
                        await send(
                            "Emulation.setGeolocationOverride",
                            {
                                "latitude": fp.latitude,
                                "longitude": fp.longitude,
                                "accuracy": fp.accuracy,
                            },
                            session_id=session_id,
                        )
                    await send(
                        "Network.setUserAgentOverride",
                        {
                            "userAgent": fp.user_agent,
                            "acceptLanguage": accept_lang_str,
                            "platform": platform_str,
                            "userAgentMetadata": ua_metadata,
                        },
                        session_id=session_id,
                    )
                    if proxy_auth:
                        await send(
                            "Fetch.enable",
                            {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]},
                            session_id=session_id,
                        )
                    await send(
                        "Runtime.runIfWaitingForDebugger", session_id=session_id
                    )
                    # R4: signal readiness only after the document is unpaused.
                    # The main thread can now verify core values on the initial
                    # about:blank page. Navigation waits for that handshake so
                    # a slow page cannot invalidate verification halfway through.
                    if profile.id in self._cdp_ready_events:
                        self._cdp_ready_events[profile.id].set()
                    if first_tab:
                        try:
                            core_event = self._core_verified_events.get(profile.id)
                            if core_event is not None:
                                await asyncio.to_thread(core_event.wait, 8.0)
                            if target_url == "about:blank":
                                nav_event = self._navigation_events.get(profile.id)
                                if nav_event is not None:
                                    nav_event.set()
                                restored_urls = await asyncio.to_thread(
                                    self._load_session_tabs, user_data_dir
                                )
                                if restored_urls:
                                    try:
                                        await send(
                                            "Page.navigate",
                                            {"url": restored_urls[0]},
                                            session_id=session_id,
                                        )
                                        for extra_url in restored_urls[1:]:
                                            try:
                                                await send(
                                                    "Target.createTarget",
                                                    {"url": extra_url},
                                                )
                                            except Exception:
                                                pass
                                        await asyncio.sleep(0.5)
                                    except Exception:
                                        pass
                            elif target_url:
                                load_event = asyncio.Event()
                                self._page_load_events[session_id] = load_event
                                try:
                                    await send(
                                        "Page.navigate",
                                        {"url": target_url},
                                        session_id=session_id,
                                    )
                                    try:
                                        await asyncio.wait_for(load_event.wait(), timeout=10.0)
                                    except asyncio.TimeoutError:
                                        pass
                                finally:
                                    self._page_load_events.pop(session_id, None)
                                    nav_event = self._navigation_events.get(profile.id)
                                    if nav_event is not None:
                                        nav_event.set()
                        finally:
                            session_restore_done = True
                        asyncio.create_task(sync_session_tabs(0.5))

                async def configure_iframe(session_id, target_info):
                    # OOPIF: Page.addScriptToEvaluateOnNewDocument handles the
                    # current document when it is paused before scripts run.
                    await send("Page.enable", session_id=session_id)
                    # Emulation.setDeviceMetricsOverride is top-level-only.
                    # OOPIFs inherit the top-level display metrics.
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth_js},
                        session_id=session_id,
                    )
                    # P0-04: Báo hiệu stealth đã được nạp thành công vào document
                    if profile.id in self._cdp_ready_events:
                        self._cdp_ready_events[profile.id].set()
                    await send(
                        "Runtime.evaluate",
                        {"expression": stealth_js, "silent": True},
                        session_id=session_id,
                    )
                    await send(
                        "Network.setUserAgentOverride",
                        {
                            "userAgent": fp.user_agent,
                            "acceptLanguage": accept_lang_str,
                            "platform": platform_str,
                            "userAgentMetadata": ua_metadata,
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Emulation.setTimezoneOverride",
                        {"timezoneId": fp.timezone},
                        session_id=session_id,
                    )
                    if proxy_auth:
                        await send(
                            "Fetch.enable",
                            {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]},
                            session_id=session_id,
                        )
                    await send(
                        "Runtime.runIfWaitingForDebugger", session_id=session_id
                    )

                async def configure_worker(session_id, target_info):
                    url = (target_info.get("url") or "").lower()
                    if url.startswith(("chrome-extension://", "devtools://")):
                        await send(
                            "Runtime.runIfWaitingForDebugger",
                            session_id=session_id,
                        )
                        return
                    await send("Runtime.enable", session_id=session_id)
                    # S6: workers are separate targets. Apply the same
                    # timezone/locale/UA at the CDP layer where supported, then
                    # fall back to the worker payload for navigator/Intl values.
                    for method, params in (
                        ("Network.enable", {}),
                        ("Emulation.setTimezoneOverride", {"timezoneId": fp.timezone}),
                        ("Emulation.setLocaleOverride", {"locale": fp.locale}),
                        (
                            "Network.setUserAgentOverride",
                            {
                                "userAgent": fp.user_agent,
                                "acceptLanguage": accept_lang_str,
                                "platform": platform_str,
                                "userAgentMetadata": ua_metadata,
                            },
                        ),
                    ):
                        try:
                            await send(method, params, session_id=session_id)
                        except Exception:
                            # Worker targets reject some page-only domains; the
                            # worker JS still covers navigator values.
                            pass
                    # Workers do not support Page.addScriptToEvaluateOnNewDocument.
                    # At waitForDebuggerOnStart they have a context but have not
                    # executed user code yet, so Runtime.evaluate is safe.
                    await send(
                        "Runtime.evaluate",
                        {
                            "expression": worker_js,
                            "silent": True,
                            "returnByValue": False,
                            "awaitPromise": False,
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Runtime.runIfWaitingForDebugger", session_id=session_id
                    )

                async def dispatch_event(event):
                    method = event.get("method", "")
                    params = event.get("params", {})
                    session_id = event.get("sessionId")

                    if method == "Page.loadEventFired":
                        load_event = self._page_load_events.get(session_id)
                        if load_event is not None:
                            load_event.set()
                        asyncio.create_task(sync_session_tabs(0.2))
                        return

                    if method == "Page.navigatedWithinDocument":
                        asyncio.create_task(sync_session_tabs(0.2))
                        return

                    if method == "Target.detachedFromTarget":
                        attached_sessions.discard(params.get("sessionId"))
                        asyncio.create_task(sync_session_tabs(0.35))
                        return

                    if method == "Target.attachedToTarget":
                        target_info = params.get("targetInfo", {})
                        target_type = target_info.get("type", "")
                        attached_id = params.get("sessionId")
                        if not attached_id:
                            return
                        attached_sessions.add(attached_id)
                        if target_type == "page":
                            spawn_target_task(
                                configure_page(attached_id, target_info), attached_id
                            )
                        elif target_type == "iframe":
                            spawn_target_task(
                                configure_iframe(attached_id, target_info), attached_id
                            )
                        elif target_type in (
                            "worker",
                            "service_worker",
                            "shared_worker",
                        ):
                            spawn_target_task(
                                configure_worker(attached_id, target_info), attached_id
                            )
                        else:
                            asyncio.create_task(
                                send(
                                    "Runtime.runIfWaitingForDebugger",
                                    session_id=attached_id,
                                )
                            )

                    elif method == "Fetch.authRequired":
                        request_id = params.get("requestId")
                        challenge = params.get("authChallenge", {})
                        if (
                            request_id
                            and proxy_auth
                            and challenge.get("source") == "Proxy"
                        ):
                            asyncio.create_task(
                                send(
                                    "Fetch.continueWithAuth",
                                    {
                                        "requestId": request_id,
                                        "authChallengeResponse": {
                                            "response": "ProvideCredentials",
                                            "username": proxy_auth[0],
                                            "password": proxy_auth[1],
                                        },
                                    },
                                    session_id=session_id,
                                )
                            )
                        elif request_id:
                            asyncio.create_task(
                                send(
                                    "Fetch.continueWithAuth",
                                    {
                                        "requestId": request_id,
                                        "authChallengeResponse": {"response": "CancelAuth"},
                                    },
                                    session_id=session_id,
                                )
                            )

                    elif method == "Fetch.requestPaused":
                        request_id = params.get("requestId")
                        if request_id:
                            asyncio.create_task(
                                send(
                                    "Fetch.continueRequest",
                                    {"requestId": request_id},
                                    session_id=session_id,
                                )
                            )

                # P0: reader_loop must never await send(). send() blocks on a
                # future in pending_responses that only reader_loop resolves,
                # so a send() from inside the reader deadlocks it, the 5s
                # response timeout breaks the loop, and closing the socket
                # makes Chrome reset deviceMetrics/timezone/UA overrides.
                async def reader_loop():
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            if not self._cdp_process_alive(profile.id):
                                break
                            if launch_id and not self.cdp_health.is_stopping(profile.id):
                                self.cdp_health.mark_degraded(
                                    profile.id,
                                    launch_id,
                                    REASON_READER_TIMEOUT,
                                    restart_required=True,
                                )
                            continue
                        except Exception:
                            if (
                                launch_id
                                and self._cdp_process_alive(profile.id)
                                and not self.cdp_health.is_stopping(profile.id)
                            ):
                                self.cdp_health.mark_degraded(
                                    profile.id,
                                    launch_id,
                                    REASON_SOCKET_CLOSED,
                                    restart_required=True,
                                )
                            break

                        try:
                            event = json.loads(msg)
                        except Exception:
                            continue

                        resp_id = event.get("id")
                        if resp_id in pending_responses:
                            fut = pending_responses[resp_id]
                            if not fut.done():
                                fut.set_result(event)
                            continue

                        await dispatch_event(event)

                async def keepalive_loop():
                    failures = 0
                    while True:
                        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                        if not self._cdp_process_alive(profile.id):
                            return
                        if launch_id and self.cdp_health.is_stopping(profile.id):
                            return
                        try:
                            await send("Browser.getVersion")
                        except Exception as exc:
                            failures += 1
                            if failures >= 2:
                                if launch_id:
                                    reason = (
                                        REASON_HEARTBEAT_TIMEOUT
                                        if isinstance(exc, asyncio.TimeoutError)
                                        else REASON_SOCKET_SEND_FAILED
                                    )
                                    self.cdp_health.mark_degraded(
                                        profile.id,
                                        launch_id,
                                        reason,
                                        restart_required=True,
                                    )
                                return
                            continue
                        failures = 0
                        if launch_id:
                            self.cdp_health.mark_heartbeat(profile.id, launch_id)
                        await sync_session_tabs()

                reader_task = asyncio.create_task(reader_loop())
                keepalive_task = asyncio.create_task(keepalive_loop())
                try:
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                        },
                    )
                    handshake_established = True
                    await reader_task
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            if (
                launch_id
                and self._cdp_process_alive(profile.id)
                and not self.cdp_health.is_stopping(profile.id)
            ):
                self.cdp_health.mark_degraded(
                    profile.id,
                    launch_id,
                    REASON_HANDSHAKE_FAILED
                    if not handshake_established
                    else REASON_SOCKET_CLOSED,
                    restart_required=True,
                )
            log.error(f"[Launcher] Lỗi kết nối CDP: {e}")
        finally:
            for fut in list(pending_responses.values()):
                if not fut.done():
                    fut.cancel()
            for task in list(target_tasks):
                task.cancel()
            if target_tasks:
                await asyncio.gather(*target_tasks, return_exceptions=True)
            if session is not None:
                session.mark_closed()
            if (
                launch_id
                and self._cdp_process_alive(profile.id)
                and not self.cdp_health.is_stopping(profile.id)
            ):
                self.cdp_health.mark_degraded(
                    profile.id,
                    launch_id,
                    REASON_HANDSHAKE_FAILED
                    if not handshake_established
                    else REASON_SOCKET_CLOSED,
                    restart_required=True,
                )

    def _start_cdp_thread(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
        fp_override: Optional[Any] = None,
        launch_id: Optional[str] = None,
    ):
        def run_loop():
            asyncio.run(
                self._setup_cdp_stealth(
                    port,
                    profile,
                    target_url,
                    user_data_dir,
                    proxy_auth,
                    fp_override,
                    launch_id=launch_id,
                )
            )

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()

    def _resolve_browser_path(self, profile: Optional[Profile]) -> Optional[str]:
        """Chọn browser binary cho lần launch này (Giai đoạn 2).

        Thứ tự: browser per-profile (tên trong browsers/ hoặc đường dẫn tuyệt
        đối) → env ANTIDETECT_BROWSER_PATH → browser pin mới nhất trong
        browsers/ → Chrome/Edge đã cài (self.browser_path). Per-profile và env
        là STRICT: chỉ định mà không resolve được sẽ trả None (launch báo lỗi
        rõ) — không bao giờ âm thầm launch nhầm browser khác. Cho phép đa dạng
        phiên bản Chrome giữa các profile và fingerprint ổn định khi Chrome
        máy tự cập nhật.
        """
        pinned_name = (getattr(profile, "browser_path", None) or "").strip() if profile else ""
        env_path = (os.environ.get("ANTIDETECT_BROWSER_PATH") or "").strip()

        def resolve_pinned(name: str) -> Optional[str]:
            def find_exe(directory: str) -> Optional[str]:
                exe = os.path.join(directory, "chrome-win64", "chrome.exe")
                if os.path.isfile(exe):
                    return exe
                for root, _dirs, files in os.walk(directory):
                    if "chrome.exe" in files:
                        return os.path.join(root, "chrome.exe")
                return None

            if os.path.isabs(name):
                # Đường dẫn tuyệt đối: file exe trực tiếp, hoặc thư mục
                # browser (env có thể trỏ vào chrome-<version>/).
                if os.path.isfile(name):
                    return name
                if os.path.isdir(name):
                    return find_exe(name)
                return None
            # Tên trong browsers/: chrome-<version> → chrome-win64/chrome.exe
            return find_exe(os.path.join(BROWSERS_DIR, name))

        if pinned_name:
            # STRICT: user chỉ định rõ cho profile này.
            return resolve_pinned(pinned_name)
        if env_path:
            # STRICT: admin chọn browser cho toàn tool.
            return resolve_pinned(env_path)

        # Browser pin mới nhất trong browsers/ (mặc định, lenient).
        if os.path.isdir(BROWSERS_DIR):
            versions = sorted(
                (
                    d for d in os.listdir(BROWSERS_DIR)
                    if d.startswith("chrome-")
                    and os.path.isfile(
                        os.path.join(BROWSERS_DIR, d, "chrome-win64", "chrome.exe")
                    )
                ),
                key=lambda d: [
                    int(x) if x.isdigit() else 0 for x in d[len("chrome-"):].split(".")
                ],
            )
            if versions:
                return os.path.join(
                    BROWSERS_DIR, versions[-1], "chrome-win64", "chrome.exe"
                )
        return self.browser_path

    def launch(
        self, profile_id: str, open_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Instrumented entrypoint; see ``_launch_impl`` for the real flow."""
        metrics.inc("launch_attempts")
        started = time.monotonic()
        try:
            with self._lifecycle_locks.acquire(profile_id, timeout=2.0):
                result = self._launch_impl(profile_id, open_url=open_url)
        except ProfileLockTimeout:
            result = {
                "success": False,
                "code": "PROFILE_BUSY",
                "error": (
                    "Profile đang được sao chép hoặc thao tác bởi tiến trình khác. "
                    "Hãy thử lại sau."
                ),
            }
        except Exception:
            metrics.inc("launch_failures")
            metrics.observe_seconds("launch", time.monotonic() - started)
            raise
        metrics.observe_seconds("launch", time.monotonic() - started)
        if result.get("success"):
            metrics.inc("launch_success")
            if result.get("degraded"):
                metrics.inc("launch_degraded")
        else:
            metrics.inc("launch_failures")
        return result

    def _launch_impl(
        self, profile_id: str, open_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Khởi chạy Chromium/Chrome cho một profile cụ thể với toàn bộ cấu hình cô lập."""
        # C12: Lock per-profile để tránh 2 luồng launch cùng profile
        with self._launch_locks[profile_id]:
            profile = self.manager.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile không tồn tại"}

            # Giai đoạn 2: browser per-profile / pin — resolve TRƯỚC mọi use
            # (UA/Client Hints + brands probe phải theo đúng binary này).
            browser_path = self._resolve_browser_path(profile)
            if not browser_path or not os.path.isfile(browser_path):
                pinned = (getattr(profile, "browser_path", None) or "").strip()
                if pinned:
                    error = (
                        f"Browser pin '{pinned}' không tìm thấy trên máy. "
                        "Hãy chạy: python tools\\install_browser.py --version <ver>, "
                        "hoặc bỏ pin browser trong Edit profile."
                    )
                else:
                    error = "Không tìm thấy Google Chrome hoặc Microsoft Edge. Hãy cài một trình duyệt Chromium được hỗ trợ rồi thử lại."
                return {"success": False, "error": error}

            if self.is_running(profile_id):
                return self._running_launch_result(profile_id, open_url)

            profile_dir = self.manager.get_profile_dir(profile_id)
            user_data_dir = os.path.join(profile_dir, "user_data")
            os.makedirs(user_data_dir, exist_ok=True)
            self._prepare_clean_chrome_preferences(user_data_dir)

            # Xóa DevToolsActivePort cũ nếu tồn tại (tránh đọc nhầm port từ phiên crash trước)
            active_port_file = os.path.join(user_data_dir, "DevToolsActivePort")
            try:
                if os.path.exists(active_port_file):
                    os.remove(active_port_file)
            except Exception:
                pass

            port = get_free_port(exclude_ports=set(self.active_ports.values()))
            self._requested_ports[profile_id] = port

            # Đồng bộ UA/Client Hints với Chrome thực tế và sửa các fingerprint
            # legacy (macOS DPR=1, screen 1366x768, language tags có q-value).
            # P0-01: Chỉ chuẩn hóa fp tạm thời trong bộ nhớ RAM cho phiên chạy Chrome này.
            # TUYỆT ĐỐI KHÔNG tự ý gọi update_profile(profile) đè vào profiles.json.
            original_fp = profile.fingerprint
            fp = normalize_fingerprint_for_launch(profile.fingerprint, browser_path)
            launch_warnings: List[str] = []
            if int(original_fp.device_memory or 0) != int(fp.device_memory or 0):
                launch_warnings.append(
                    "deviceMemory normalized to the host value "
                    f"({fp.device_memory} GB) so JS matches Sec-CH-Device-Memory."
                )
            if (original_fp.screen_width, original_fp.screen_height) != (
                fp.screen_width, fp.screen_height
            ):
                launch_warnings.append(
                    "Screen preset clamped to fit the real display: "
                    f"{original_fp.screen_width}x{original_fp.screen_height} -> "
                    f"{fp.screen_width}x{fp.screen_height}."
                )

            # S7/C2: never spawn a window larger than the physical display.
            # Same-OS presets were already clamped above; cross-OS presets keep
            # their declared screen but use a safe window size.
            window_width, window_height = fp.screen_width, fp.screen_height
            if fp.os != host_os():
                host_width, host_height, _ = get_host_screen_info()
                if window_width > host_width or window_height > host_height:
                    window_width = min(window_width, host_width)
                    window_height = min(window_height, host_height)
                    launch_warnings.append(
                        "Cross-OS profile: window clamped to the physical display "
                        f"({window_width}x{window_height}); fingerprint consistency "
                        "across OS is not guaranteed."
                    )

            # Cấu hình danh sách flags dòng lệnh
            args = [
                browser_path,
                f"--user-data-dir={user_data_dir}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--window-size={window_width},{window_height}",
                f"--lang={fp.locale}",
                f"--accept-lang={','.join(fp.languages or [fp.locale])}",
                # H3: Đồng bộ DPR giữa JS và CSS media queries
                f"--force-device-scale-factor={fp.device_pixel_ratio or 1.0}",
                "--high-dpi-support=1",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-crash-restore-bubble",
                # L3: Giới hạn dung lượng bộ nhớ đệm chống phình ổ đĩa
                "--disk-cache-size=104857600",
                "--media-cache-size=52428800",
                # C11: KHÔNG tắt IsolateOrigins (gây infobar + lệch viewport + bất thường)
                # Tắt màn hình chọn search engine trên Chrome 124+ (EU)
                "--disable-search-engine-choice-screen",
                # Tắt các kết nối mạng ngầm của Chrome (chống rò rỉ & tăng tốc)
                "--disable-blink-features=AutomationControlled",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-sync",
            ]

            # WebRTC flags must follow the profile mode and only matter when a
            # proxy is actually configured. The old unconditional flags changed
            # behaviour even for direct connections and ignored webrtc_mode.
            args.extend(build_webrtc_flags(profile, fp))
            # Do not disable WebGPU for every spoofed macOS/Linux profile. Real
            # modern Chrome exposes WebGPU on all desktop platforms; the JS
            # adapter hook is responsible for masking host-specific details.

            # Xử lý cấu hình Proxy
            proxy_auth = None
            if profile.proxy and profile.proxy.enabled and profile.proxy.host:
                proxy_arg = profile.proxy.to_server_arg()
                if proxy_arg:
                    args.append(f"--proxy-server={proxy_arg}")

                if profile.proxy.username is not None or profile.proxy.password:
                    proxy_auth = (
                        profile.proxy.username or "",
                        profile.proxy.password or "",
                    )

                    # Xác thực proxy qua CDP (Fetch.authRequired) cho mọi bản
                    # Chrome. Đường extension MV3 cũ ghi mật khẩu dạng plaintext
                    # vào thư mục profile nên đã bị loại bỏ; Chrome 137+ cũng
                    # không còn nhận --load-extension trên bản branded.
                    log.info(
                        "[Launcher] Xác thực proxy qua CDP "
                        f"({profile.proxy.host}:{profile.proxy.port}, {profile.proxy.protocol})."
                    )
                    if profile.proxy.protocol in ("socks5", "socks4"):
                        log.warning(
                            "[Launcher] Lưu ý: Chrome có thể không hỗ trợ xác thực "
                            "SOCKS5; nên dùng HTTP/HTTPS proxy nếu proxy yêu cầu auth."
                        )

            # Mở trang trắng theo mặc định; chỉ điều hướng ra ngoài khi người dùng chỉ định URL.
            target_url = open_url or "about:blank"
            args.append("about:blank")

            # Mỗi lần launch là một generation mới; callback của session cũ
            # không được ghi đè trạng thái của lần mở này.
            launch_id = self.cdp_health.begin_launch(profile_id, self.instance_id)

            # Khởi chạy tiến trình Browser
            proc = None
            try:
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.active_processes[profile_id] = proc
                self.active_ports[profile_id] = port
                self.cdp_health.set_process(profile_id, launch_id, proc.pid)
                runtime_persisted = False
                self._recovery_required.discard(profile_id)
                if proxy_auth:
                    self._proxy_credentials[profile_id] = proxy_auth

                # Persist ownership outside profiles.json so a server restart
                # or `cli.py stop-all` can still find and stop this browser.
                try:
                    starting_health = self.cdp_health.snapshot(profile_id)
                    self._runtime_store.upsert(
                        profile_id,
                        pid=proc.pid,
                        port=port,
                        requested_port=port,
                        user_data_dir=user_data_dir,
                        exe=browser_path,
                        started_at=int(time.time()),
                        launch_id=launch_id,
                        owner_instance=self.instance_id,
                        cdp_health={
                            key: starting_health.get(key)
                            for key in (
                                "launch_id",
                                "owner_instance",
                                "pid",
                                "state",
                                "reason",
                                "verified_at",
                                "last_heartbeat_at",
                                "changed_at",
                                "checked_at",
                                "restart_required",
                            )
                        },
                    )
                    runtime_persisted = True
                except Exception as e:
                    log.warning(f"[Launcher] Không ghi được runtime state: {type(e).__name__}: {e}")

                # Cập nhật trạng thái profile trong bộ nhớ (không ghi đè profiles.json khi launch)
                profile.status = "running"
                profile.port = port
                profile.pid = proc.pid

                # P0-04: Tạo event để CDP thread báo hiệu stealth script đã được nạp.
                # R4: add the core-verification and navigation handshake events.
                cdp_ready = threading.Event()
                core_verified = threading.Event()
                navigation_done = threading.Event()
                self._cdp_ready_events[profile_id] = cdp_ready
                self._core_verified_events[profile_id] = core_verified
                self._navigation_events[profile_id] = navigation_done

                # P2: đo brands/fullVersionList thật của Chrome host MỘT lần
                # mỗi browser_version (cache theo phiên launcher) trước khi
                # CDP thread build payload — sec-ch-ua khớp Chrome thật.
                if not self._native_brands_cache.get(fp.browser_version):
                    with self._native_brands_probe_lock:
                        if not self._native_brands_cache.get(fp.browser_version):
                            measured = self._measure_native_brands_standalone(browser_path)
                            if measured:
                                self._native_brands_cache[fp.browser_version] = measured

                # Khởi chạy luồng gắn kết CDP để tiêm script và điều hướng
                self._start_cdp_thread(
                    port,
                    profile,
                    target_url,
                    user_data_dir,
                    proxy_auth,
                    fp,
                    launch_id=launch_id,
                )

                # Chờ CDP tiêm stealth thành công vào trang (tối đa 6.0 giây)
                stealth_ok = cdp_ready.wait(timeout=6.0)

                # Chrome may choose a different port than requested; the CDP
                # thread updates active_ports from DevToolsActivePort.
                actual_port = self.active_ports.get(profile_id, port)
                page_ready = _safe_run_async(self._probe_page_ready(actual_port))
                verification = {"ok": False, "checks": {}, "actual": None}
                core_verification = verification
                if page_ready and stealth_ok:
                    # Phase 1: verify the initial about:blank document before
                    # the CDP thread is allowed to navigate.
                    core_verification = _safe_run_async(
                        self._verify_emulation(actual_port, fp)
                    )
                    verification = core_verification
                core_verified.set()

                # Phase 2: after navigation, verify Client Hints on HTTPS
                # documents. about:blank has no secure-context Client Hints,
                # so that check belongs only to this second pass.
                if stealth_ok and page_ready and target_url and target_url != "about:blank":
                    navigation_done.wait(timeout=12.0)
                    if target_url.startswith("https://"):
                        post_verification = _safe_run_async(
                            self._verify_emulation(
                                actual_port,
                                fp,
                                require_client_hints=True,
                                deadline_seconds=5.0,
                            )
                        )
                        if post_verification.get("ok"):
                            verification = post_verification
                        else:
                            # Keep the core result visible for diagnostics but
                            # do not claim the post-navigation check passed.
                            post_verification["core"] = core_verification
                            verification = post_verification
                configuration_verified = bool(verification.get("ok"))

                self._cdp_ready_events.pop(profile_id, None)
                self._core_verified_events.pop(profile_id, None)
                self._navigation_events.pop(profile_id, None)

                if not stealth_ok:
                    log.error(f"[Launcher] CDP stealth injection failed or timed out for {profile_id}")

                if proc.poll() is not None:
                    self.is_running(profile_id)
                    return {
                        "success": False,
                        "error": "Chrome đã thoát trong lúc khởi chạy profile.",
                        "cdp_ready": False,
                        "stealth_injected": False,
                        "cdp_health": self.cdp_health.snapshot(profile_id),
                        "launched_now": True,
                    }

                health_ok = False
                if stealth_ok and configuration_verified and runtime_persisted:
                    health_ok = self.cdp_health.mark_healthy(profile_id, launch_id)
                if not health_ok:
                    current_state = self.cdp_health.snapshot(profile_id).get("state")
                    if current_state not in (STATE_DEGRADED, STATE_RECOVERY_REQUIRED):
                        if not runtime_persisted:
                            reason = REASON_RUNTIME_PERSIST_FAILED
                        elif not stealth_ok:
                            reason = REASON_HANDSHAKE_FAILED
                        elif not configuration_verified:
                            reason = REASON_VERIFICATION_FAILED
                        else:
                            # The session can close between mark_healthy and
                            # this commit; report that race as degraded.
                            reason = REASON_SOCKET_CLOSED
                        self.cdp_health.mark_degraded(profile_id, launch_id, reason)
                final_health = self.cdp_health.snapshot(profile_id)
                if final_health.get("state") != STATE_STOPPED:
                    self._persist_health_now(profile_id, launch_id, final_health)
                cdp_ready_final = bool(
                    stealth_ok
                    and configuration_verified
                    and runtime_persisted
                    and final_health.get("state") == STATE_HEALTHY
                )
                warning_parts = list(launch_warnings)
                if not runtime_persisted:
                    warning_parts.insert(
                        0,
                        "Runtime ownership was not persisted; the browser cannot be recovered across a restart.",
                    )
                if not cdp_ready_final:
                    warning_parts.insert(
                        0,
                        "Chrome is running but stealth injection or emulation verification was not confirmed.",
                    )
                return {
                    "success": True,
                    "profile_id": profile_id,
                    "port": actual_port,
                    "pid": proc.pid,
                    "ws_endpoint": f"http://127.0.0.1:{actual_port}",
                    "cdp_ready": cdp_ready_final,
                    "page_ready": page_ready,
                    "stealth_injected": stealth_ok,
                    "emulation_verified": configuration_verified,
                    "configuration_verified": configuration_verified,
                    # MED-3: client automation phải check trường này, không chỉ
                    # "success" — browser đã mở nhưng fingerprint có thể chưa
                    # được che khi stealth/verify thất bại.
                    "degraded": not cdp_ready_final,
                    "code": None if cdp_ready_final else "CDP_UNHEALTHY",
                    "cdp_health": final_health,
                    "launched_now": True,
                    "verification": verification,
                    "warnings": launch_warnings,
                    "warning": "; ".join(warning_parts) if warning_parts else None,
                }
            except Exception as e:
                # H15: Nếu Popen thành công nhưng các bước sau lỗi, kill proc
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._clear_process(profile_id)
                self._cdp_ready_events.pop(profile_id, None)
                self._core_verified_events.pop(profile_id, None)
                self._navigation_events.pop(profile_id, None)
                log.error(f"[Launcher] Lỗi khởi chạy: {type(e).__name__}: {e}")
                return {
                    "success": False,
                    "error": "Lỗi khởi chạy trình duyệt. Vui lòng thử lại.",
                }

    def stop(self, profile_id: str) -> Dict[str, Any]:
        """Instrumented entrypoint; see ``_stop_impl`` for the real flow."""
        metrics.inc("stop_attempts")
        try:
            with self._lifecycle_locks.acquire(profile_id, timeout=4.0):
                result = self._stop_impl(profile_id)
        except ProfileLockTimeout:
            result = {
                "success": False,
                "code": "PROFILE_BUSY",
                "error": (
                    "Profile đang được sao chép hoặc thao tác bởi tiến trình khác. "
                    "Hãy thử lại sau."
                ),
            }
        metrics.inc("stop_success" if result.get("success") else "stop_failures")
        return result

    def _stop_impl(self, profile_id: str) -> Dict[str, Any]:
        """Stop only an owned browser; never clear state until exit is confirmed."""
        with self._launch_locks[profile_id]:
            profile = self.manager.get_profile(profile_id)
            if not profile:
                # Metadata đã bị xóa nhưng runtime record có thể còn giữ một
                # browser mồ côi. Trả lỗi ở đây từng chặn stop_all/shutdown
                # vĩnh viễn trên data dir có record stale của profile đã xóa.
                stale_record = self._runtime_store.get(profile_id) or {}
                if not stale_record.get("pid"):
                    return {"success": False, "error": "Profile không tồn tại"}
            proc = self.active_processes.get(profile_id)
            if proc and proc.poll() is not None:
                self._clear_process(profile_id)
                return {"success": True, "message": "Trình duyệt đã đóng"}

            record = self._runtime_store.get(profile_id) or {}
            pid = proc.pid if proc else (
                record.get("pid") or (profile.pid if profile else None)
            )
            if not pid:
                self._clear_process(profile_id)
                return {"success": True, "message": "Profile đã dừng"}
            user_data_dir = record.get("user_data_dir") or os.path.join(
                PROFILES_DATA_DIR, profile_id, "user_data"
            )
            if proc is not None and proc.poll() is None:
                live_port = self.active_ports.get(profile_id)
                if live_port:
                    self._snapshot_tabs_via_http(live_port, user_data_dir)
            launch_id = self.cdp_health.current_launch_id(profile_id) or record.get(
                "launch_id"
            )
            if self.cdp_health.mark_stopping(profile_id):
                stopping_snapshot = self.cdp_health.snapshot(profile_id)
                if launch_id:
                    self._persist_health_now(
                        profile_id, launch_id, stopping_snapshot
                    )

            def _stop_failed(message: str) -> Dict[str, Any]:
                self.cdp_health.mark_degraded(
                    profile_id,
                    launch_id,
                    REASON_STOP_FAILED,
                    restart_required=True,
                )
                snapshot = self.cdp_health.snapshot(profile_id)
                if launch_id:
                    self._persist_health_now(profile_id, launch_id, snapshot)
                return {
                    "success": False,
                    "code": "STOP_FAILED",
                    "error": message,
                    "cdp_health": snapshot,
                }

            identity = None
            if not proc:
                identity = self._identity_for_stop(pid, user_data_dir)
                if identity["state"] in ("absent", "mismatch"):
                    self._clear_process(profile_id)
                    return {"success": True, "message": "Phiên cũ đã kết thúc"}
                if identity["state"] != "owned":
                    return _stop_failed("Chưa xác minh được tiến trình của profile; không gửi lệnh tắt.")
            try:
                if sys.platform == "win32":
                    # First request normal closure; always continue to /F when
                    # graceful taskkill fails or the process does not exit. The
                    # previous early-return left Chrome and the profile lock alive.
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    last_error = ""
                    for force in (False, True):
                        if not proc:
                            current = self._identity_for_stop(pid, user_data_dir)
                            if current["state"] in ("absent", "mismatch"):
                                break
                            if current["state"] == "unknown":
                                return _stop_failed("Chưa xác minh lại được tiến trình; giữ nguyên trạng thái để kiểm tra.")
                            if current["state"] != "owned" or current.get("created") != identity.get("created"):
                                return _stop_failed("Tiến trình đã thay đổi; giữ nguyên trạng thái để kiểm tra.")
                        args = ["taskkill", "/PID", str(pid), "/T"]
                        if force:
                            args.append("/F")
                        try:
                            result = subprocess.run(
                                args,
                                capture_output=True,
                                timeout=8,
                                creationflags=flags,
                            )
                            if result.returncode != 0:
                                stderr = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
                                last_error = stderr or f"taskkill exit {result.returncode}"
                        except (OSError, subprocess.SubprocessError) as e:
                            last_error = str(e)

                        if proc:
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                pass
                            if proc.poll() is not None:
                                break
                        else:
                            for _ in range(10):
                                if self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch"):
                                    break
                                time.sleep(0.2)
                            if self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch"):
                                break
                    # Cho thêm grace period để Windows hoàn tất reap tiến trình sau lệnh taskkill /F
                    if proc and proc.poll() is None:
                        for _ in range(8):
                            try:
                                proc.wait(timeout=0.5)
                            except subprocess.TimeoutExpired:
                                pass
                            if proc.poll() is not None:
                                break
                    elif not proc and self._identity_for(pid, user_data_dir)["state"] not in ("absent", "mismatch"):
                        for _ in range(10):
                            if self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch"):
                                break
                            time.sleep(0.2)
                    if not (proc.poll() is not None if proc else self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch")):
                        detail = f" ({last_error})" if last_error else ""
                        return _stop_failed(f"Windows chưa dừng được trình duyệt{detail}. Dữ liệu trạng thái được giữ nguyên.")
                elif proc:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                else:
                    return _stop_failed("Không xác minh được quyền sở hữu tiến trình.")
                exited = proc.poll() is not None if proc else self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch")
                if not exited:
                    return _stop_failed("Trình duyệt vẫn đang chạy; chưa thể báo đã dừng.")
            except (OSError, subprocess.SubprocessError):
                return _stop_failed("Không thể dừng trình duyệt; trạng thái được giữ nguyên.")
            self._clear_process(profile_id)
            return {"success": True, "message": "Đã xác nhận trình duyệt dừng"}

    def stop_all(self, profile_ids: Optional[List[str]] = None):
        """Dừng profile đang chạy. ``None`` = toàn bộ; ``[]`` = không dừng ai.

        Phân biệt danh sách rỗng với None để API consumer gửi nhầm selection
        rỗng không vô tình đóng cả farm.
        """
        ids = set(self.active_processes)
        ids.update(self._runtime_store.all().keys())
        ids.update(p.id for p in self.manager.list_profiles() if p.pid or p.status == "running")
        if profile_ids is not None:
            wanted = set(profile_ids)
            ids &= wanted
        results = {pid: self.stop(pid) for pid in ids}
        failed = [pid for pid, result in results.items() if not result.get("success")]
        return {"success": not failed, "failed_profile_ids": failed, "results": results,
                "message": "Đã dừng toàn bộ profile" if not failed else "Một số profile chưa dừng được."}

    def _open_tab_impl(self, profile_id: str, url: str) -> bool:
        port = self.active_ports.get(profile_id)
        if not port:
            record = self._runtime_store.get(profile_id) or {}
            port = record.get("port")
            if port:
                self.active_ports[profile_id] = int(port)
        if not port:
            return False
        try:
            from urllib.parse import quote
            target_url = f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=#%')}"
            req = urllib.request.Request(target_url, method="PUT")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status in (200, 201)
        except Exception:
            try:
                from urllib.parse import quote
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=#%')}", timeout=3) as resp:
                    return True
            except Exception as e:
                log.warning(f"[Launcher] Lỗi mở tab mới: {e}")
                return False

    def open_tab(self, profile_id: str, url: str) -> bool:
        """Mở tab trên profile đang chạy; từ chối nếu CDP không khỏe (L2)."""
        guard = self.cdp_guard(profile_id)
        if not guard["ok"]:
            log.info(
                "[Launcher] Từ chối mở tab %s: %s (%s)",
                profile_id,
                guard["code"],
                (guard["health"] or {}).get("reason"),
            )
            return False
        return self._open_tab_impl(profile_id, url)

    def export_cookies(self, profile_id: str) -> List[Dict[str, Any]]:
        """Đọc cookies trực tiếp từ profile đang chạy, không tạo bản sao JSON trên đĩa."""
        self.require_cdp(profile_id)
        port = self.active_ports.get(profile_id)
        if not port:
            raise CdpUnavailable(
                "NOT_RUNNING", "Hãy mở profile trước khi đọc cookies."
            )
        try:
            return _safe_run_async(fetch_cookies_cdp(port))
        except Exception as e:
            log.warning(f"[Launcher] Không thể đọc cookies qua CDP: {type(e).__name__}")
            raise RuntimeError("Không thể đọc cookies từ profile đang chạy.") from e

    def import_cookies(self, profile_id: str, cookies: List[Dict[str, Any]]) -> bool:
        """Nạp cookies trực tiếp vào profile đang chạy qua CDP Storage.setCookies."""
        self.require_cdp(profile_id)
        port = self.active_ports.get(profile_id)
        if not port:
            raise CdpUnavailable(
                "NOT_RUNNING", "Hãy mở profile trước khi nạp cookies."
            )
        try:
            normalized_cookies = normalize_cookies(cookies)
            if not normalized_cookies:
                return False
            return _safe_run_async(set_cookies_cdp(port, normalized_cookies))
        except Exception as e:
            log.warning(f"[Launcher] Không thể nạp cookies qua CDP: {type(e).__name__} {e}")
            return False

    def fetch_facebook_pages(
        self, profile_id: str, timeout: float = 90.0, deep: bool = False
    ) -> Dict[str, Any]:
        """Liệt kê fanpage profile này sở hữu/quản lý, đọc trực tiếp từ phiên đang chạy.

        Mở tab facebook.com/bookmarks/pages bằng session đã đăng nhập của
        profile (tab có stealth như mọi tab khác), đọc DOM, rồi đóng tab.
        ``deep=True``: sau danh sách, quét thêm TỪNG page (mở tuần tự trên
        cùng một tab) để lấy ngày tạo + thời gian bài đăng gần nhất và gắn
        trạng thái theo ``classify_facebook_page``.

        Trả về contract F4: {"logged_in", "pages", "scan_id",
        "discovery_status", "completion_reason", "stability",
        "observed_count", ...}. Profile dừng giữa chừng → ``ScanAborted``.
        """
        self.require_cdp(profile_id)
        port = self.active_ports.get(profile_id)
        if not port:
            raise CdpUnavailable(
                "NOT_RUNNING", "Hãy mở profile trước khi đọc fanpage."
            )
        expected_account_id = None
        getter = getattr(self.manager, "get_facebook_scan_meta", None)
        if callable(getter):
            try:
                meta = getter(profile_id) or {}
                expected_account_id = meta.get("account_id_observed")
            except Exception as e:
                log.warning(
                    f"[Launcher] Không đọc được scan meta: {type(e).__name__}"
                )

        def should_abort() -> bool:
            return not self._facebook_scan_alive(profile_id)

        data = _safe_run_async(
            self._facebook_pages_cdp(
                port,
                timeout,
                expected_account_id=expected_account_id,
                should_abort=should_abort,
            )
        )
        status = data.get("discovery_status")
        if (
            deep
            and data.get("logged_in")
            and data.get("pages")
            and status in (None, "complete", "partial")
        ):
            per_page = 8.0
            deep_timeout = min(300.0, 20.0 + per_page * len(data["pages"]))
            try:
                details = _safe_run_async(
                    self._facebook_pages_deep_cdp(
                        port,
                        data["pages"],
                        timeout=deep_timeout,
                        should_abort=should_abort,
                    )
                )
            except ScanAborted:
                raise
            except Exception:
                details = {}
            profile = self.manager.get_profile(profile_id)
            tz_name = profile.fingerprint.timezone if profile else None
            merge_page_details(data, details, display_tz=tz_name)
        return data

    def _facebook_scan_alive(self, profile_id: str) -> bool:
        """Liveness chỉ đọc cho scan; không gọi ``_clear_process``/health lock.

        QĐ-6: CDP thread không được mutate runtime/health trong lúc quét.
        """
        proc = self.active_processes.get(profile_id)
        if proc is not None:
            return proc.poll() is None
        record = self._runtime_store.get(profile_id)
        if record:
            identity = read_browser_identity(
                record.get("pid"), record.get("user_data_dir") or ""
            )
            return identity.get("state") not in ("absent", "mismatch")
        profile = self.manager.get_profile(profile_id)
        if not profile or not profile.pid:
            return False
        identity = self._identity(profile)
        return identity.get("state") not in ("absent", "mismatch")

    async def _facebook_pages_cdp(
        self,
        port: int,
        timeout: float = 90.0,
        *,
        expected_account_id: Optional[str] = None,
        should_abort: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return await fetch_pages_cdp(
            port,
            timeout,
            expected_account_id=expected_account_id,
            should_abort=should_abort,
        )

    async def _facebook_pages_deep_cdp(
        self,
        port: int,
        pages: List[Dict[str, Any]],
        timeout: float = 120.0,
        *,
        should_abort: Optional[Any] = None,
    ) -> Dict[str, Dict[str, Any]]:
        return await fetch_pages_deep_cdp(
            port, pages, timeout, should_abort=should_abort
        )

    def launch_all(
        self,
        delay: float = 0.8,
        open_url: Optional[str] = None,
        max_concurrent: int = 8,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Any] = None,
        profile_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Start every stopped profile, running launches in parallel batches.

        ``max_concurrent`` giới hạn số launch thực hiện ĐỒNG THỜI — browser
        mở xong vẫn được giữ mở. Phiên bản cũ chờ "slot trống" theo số browser
        đang sống nên bị kẹt vô hạn khi số profile lớn hơn limit (start-all
        không đóng browser nào).

        ``profile_ids`` giới hạn phạm vi launch (multi-select trên dashboard,
        lịch tự động). ``None`` = mọi profile; danh sách rỗng = không launch
        profile nào (khác hành vi cũ để tránh selection rỗng mở cả farm).
        """
        import concurrent.futures

        profiles = self.manager.list_profiles()
        if profile_ids is not None:
            wanted = set(profile_ids)
            profiles = [p for p in profiles if p.id in wanted]
        if max_concurrent and int(max_concurrent) > 0:
            limit = max(1, int(max_concurrent))
        else:
            limit = max(1, len(profiles))

        def _launch_one(p):
            if cancel_event and cancel_event.is_set():
                res = {"success": False, "error": "Đã hủy tác vụ bởi người dùng"}
            else:
                res = self.launch(p.id, open_url=open_url)
            if progress_callback:
                try:
                    progress_callback(p.id, res)
                except Exception:
                    pass
            return {"profile_id": p.id, "name": p.name, "result": res}

        results: List[Dict[str, Any]] = []
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as pool:
            for p in profiles:
                if cancel_event and cancel_event.is_set():
                    results.append({
                        "profile_id": p.id,
                        "name": p.name,
                        "result": {"success": False, "error": "Đã hủy tác vụ bởi người dùng"},
                    })
                    continue
                if self.is_running(p.id):
                    item = {
                        "profile_id": p.id,
                        "name": p.name,
                        "result": self._running_launch_result(p.id),
                    }
                    results.append(item)
                    if progress_callback:
                        try:
                            progress_callback(p.id, item["result"])
                        except Exception:
                            pass
                    continue
                futures.append(pool.submit(_launch_one, p))
                if delay > 0:
                    time.sleep(delay)
            for fut in futures:
                results.append(fut.result())

        # Giữ thứ tự theo danh sách profile cho UI ổn định.
        order = {p.id: i for i, p in enumerate(profiles)}
        results.sort(key=lambda r: order.get(r.get("profile_id"), len(order)))
        return results
