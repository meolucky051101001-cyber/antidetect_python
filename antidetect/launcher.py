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
from .stealth import get_stealth_script, get_worker_stealth_script, get_webgl_limits, get_stealth_capabilities
from .process_identity import (
    browser_pids_using_dir, read_browser_identity, invalidate_identity,
    terminate_verified_browser, browser_process_birth,
)
from .loopback import open_loopback, connect_loopback
from .launch_verification import LaunchVerification, verification_origin
from .profile_locks import ProfileLockTimeout, ProfileLocks
from .proxy_auth import ProxyAuthAttempts
from .browser_trust import BrowserTrustError, hold_trusted_browser, verify_browser_executable
from .custom_cores import build_cpp_engine_flags, custom_cores_manifest_path
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
    DEEP_NAVIGATION_TIMEOUT_S,
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
        self._health_persist_write_lock = threading.Lock()
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
        self._shutdown_requested = False
        # H5: Event để CDP thread báo hiệu đã inject stealth thành công
        self._cdp_ready_events: Dict[str, threading.Event] = {}
        # R4: page configuration is split into two phases. The CDP thread
        # pauses on about:blank until the main thread verifies core values,
        # then navigates and reports when the new document has loaded.
        self._core_verified_events: Dict[str, LaunchVerification] = {}
        self._navigation_events: Dict[str, threading.Event] = {}
        self._page_load_events: Dict[str, Any] = {}
        self._configured_targets: Dict[str, Set[str]] = collections.defaultdict(set)
        self._configured_target_cond = threading.Condition(threading.Lock())
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
        try:
            self.stop_owned()
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
        # A dequeued callback can be older than a synchronous detach/stop.
        # Serialize commits and read the current same-generation state inside
        # that lock so neither queued nor already-in-flight writes can undo it.
        with self._health_persist_write_lock:
            current = self.cdp_health.snapshot(profile_id)
            if current.get("launch_id") != launch_id:
                return False
            return self._persist_current_health_now(profile_id, launch_id, current, extra)

    def _persist_current_health_now(
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
            updated = self._runtime_store.upsert_if_owner(
                str(profile_id),
                launch_id=launch_id,
                pid=snapshot.get("pid"),
                owner_instance=self.instance_id,
                **fields,
            )
            return updated is not None
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
                "launch_id": (guard["health"] or {}).get("launch_id"),
                "owner_instance": (guard["health"] or {}).get("owner_instance"),
                "cdp_health": guard["health"],
                "launched_now": False,
            }
        return {
            "success": True,
            "message": "Đang chạy",
            "port": port,
            "launch_id": (guard["health"] or {}).get("launch_id"),
            "owner_instance": (guard["health"] or {}).get("owner_instance"),
            "cdp_health": guard["health"],
            "launched_now": False,
        }

    @staticmethod
    def _custom_cores_manifest_path() -> Optional[str]:
        return custom_cores_manifest_path()

    def _configured_state(self):
        cond = getattr(self, "_configured_target_cond", None)
        store = getattr(self, "_configured_targets", None)
        if cond is None or store is None:
            cond = threading.Condition(threading.Lock())
            store = collections.defaultdict(set)
            self._configured_target_cond = cond
            self._configured_targets = store
        return cond, store

    def _mark_target_configured(
        self, profile_id: str, launch_id: Optional[str], target_id: Optional[str]
    ) -> None:
        if not target_id:
            return
        if launch_id and self.cdp_health.current_launch_id(profile_id) != launch_id:
            return
        cond, store = self._configured_state()
        with cond:
            store[str(profile_id)].add(str(target_id))
            cond.notify_all()

    def _discard_configured_target(
        self, profile_id: str, target_id: Optional[str] = None
    ) -> None:
        cond = getattr(self, "_configured_target_cond", None)
        store = getattr(self, "_configured_targets", None)
        if cond is None or store is None:
            return
        with cond:
            if target_id is None:
                store.pop(str(profile_id), None)
            else:
                bucket = store.get(str(profile_id))
                if bucket is not None:
                    bucket.discard(str(target_id))
            cond.notify_all()

    def wait_for_target_configured(
        self, profile_id: str, target_id: str, timeout: float = 6.0
    ) -> bool:
        """Block until configure_page finishes injecting stealth & UA overrides on target_id."""
        if not target_id:
            return False
        cond, store = self._configured_state()
        pid = str(profile_id)
        tid = str(target_id)
        deadline = time.monotonic() + max(0.05, float(timeout))
        with cond:
            while tid not in store.get(pid, ()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if not self.cdp_guard(pid)["ok"]:
                    return False
                cond.wait(timeout=min(0.25, remaining))
            return True

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
        invalidate_identity(pid)
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

    def _identity_for_owner(self, pid, user_data_dir, timeout: float = 5.0):
        """Retry unknown identity for up to ``timeout`` seconds."""
        deadline = time.monotonic() + max(0.0, timeout)
        identity = self._identity_for(pid, user_data_dir)
        while identity["state"] == "unknown" and time.monotonic() < deadline:
            time.sleep(0.25)
            identity = self._identity_for(pid, user_data_dir)
        return identity

    def _wait_identity_gone(self, pid, user_data_dir, timeout: float) -> str:
        """Poll until the PID is absent; unknown is retried, never success."""
        deadline = time.monotonic() + max(0.0, timeout)
        state = self._identity_for(pid, user_data_dir)["state"]
        while state not in ("absent", "mismatch") and time.monotonic() < deadline:
            time.sleep(0.25)
            state = self._identity_for(pid, user_data_dir)["state"]
        return state

    def _runtime_matches_local(self, profile_id, record):
        if not record:
            return True
        proc = self.active_processes.get(profile_id)
        local_launch = self.cdp_health.current_launch_id(profile_id)
        if proc and record.get("pid") and proc.pid != record.get("pid"):
            return False
        if local_launch and record.get("launch_id") and local_launch != record["launch_id"]:
            return False
        return True

    def _forget_stale_local_process(self, profile_id):
        """Forget local handles without touching a replacement's persistent record."""
        with self._launch_locks[profile_id]:
            self._forget_stale_local_process_locked(profile_id)

    def _forget_stale_local_process_locked(self, profile_id):
        old_launch = self.cdp_health.current_launch_id(profile_id)
        if not self.cdp_health.forget_if_launch(profile_id, old_launch):
            return
        self.active_processes.pop(profile_id, None)
        release_port(self._requested_ports.pop(profile_id, None))
        release_port(self.active_ports.pop(profile_id, None))
        self._recovery_required.discard(profile_id)
        self._proxy_credentials.pop(profile_id, None)
        self._cdp_ready_events.pop(profile_id, None)
        self._core_verified_events.pop(profile_id, None)
        self._navigation_events.pop(profile_id, None)
        self._discard_configured_target(profile_id)

    def _clear_process(self, profile_id, *, expected_record=None):
        with self._launch_locks[profile_id]:
            return self._clear_process_locked(profile_id, expected_record=expected_record)

    @staticmethod
    def _terminate_owned_children(user_data_dir, *, exclude_pid=None):
        """Terminate handle-verified Chromium helper processes after parent kill."""
        if sys.platform != "win32" or not user_data_dir:
            return
        excluded = {int(exclude_pid)} if exclude_pid is not None else set()
        found = set(browser_pids_using_dir(user_data_dir) or ()) - excluded
        for child_pid in sorted(found):
            identity = read_browser_identity(child_pid, user_data_dir, fresh=True)
            if identity.get("state") == "owned":
                terminate_verified_browser(child_pid, user_data_dir, identity)

    @staticmethod
    def _wait_directory_idle(user_data_dir, timeout=30.0):
        """A parent exit alone does not prove Chromium children have stopped."""
        if not user_data_dir:
            return True
        started = time.monotonic()
        deadline = started + timeout
        while True:
            pids = browser_pids_using_dir(user_data_dir)
            if pids is not None and not pids:
                return True
            if time.monotonic() >= deadline:
                log.warning(
                    "[Launcher] Directory idle check failed: %s "
                    "(directory_state=%s, elapsed_ms=%.3f)",
                    "incomplete process scan" if pids is None else "browser processes remain",
                    "unknown" if pids is None else "known_processes_present",
                    (time.monotonic() - started) * 1000,
                )
                return False
            time.sleep(0.1)

    def _clear_process_locked(self, profile_id, *, expected_record=None):
        record = expected_record if expected_record is not None else (self._runtime_store.get(profile_id) or {})
        if expected_record is None and not self._runtime_matches_local(profile_id, record):
            return False
        if not self._wait_directory_idle(record.get("user_data_dir")):
            return False
        try:
            if not self._runtime_store.remove_if_matches(profile_id, record):
                return False
        except Exception:
            # Losing recovery metadata is not a successful stop/cleanup.
            return False
        requested = self._requested_ports.pop(profile_id, None) or record.get("requested_port")
        active = self.active_ports.pop(profile_id, None) or record.get("port")
        self.active_processes.pop(profile_id, None)
        self._recovery_required.discard(profile_id)
        self.cdp_health.mark_stopped(profile_id)
        self._proxy_credentials.pop(profile_id, None)
        self._cdp_ready_events.pop(profile_id, None)
        self._core_verified_events.pop(profile_id, None)
        self._navigation_events.pop(profile_id, None)
        self._discard_configured_target(profile_id)
        release_port(requested)
        if active and active != requested:
            release_port(active)
        # P0-02: Dọn dẹp thư mục tiện ích mở rộng proxy auth tạm thời nếu có
        try:
            profile_dir = self.manager.get_profile_dir(profile_id)
            ext_dir = os.path.join(profile_dir, "proxy_auth_extension")
            if os.path.exists(ext_dir):
                shutil.rmtree(ext_dir, ignore_errors=True)
        except Exception:
            pass
        return True

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
        invalidate_identity(record.get("pid"))
        identity = read_browser_identity(
            record.get("pid"), record.get("user_data_dir") or ""
        )
        if (identity.get("state") == "owned" and record.get("created")
                and identity.get("created") != record["created"]):
            # Never relabel a reused PID with a new creation identity.
            identity = {"state": "mismatch", "reason": "process_birth_changed"}
        if (identity.get("state") == "owned" and record.get("birth_ticks") is not None
                and identity.get("birth_ticks") != record["birth_ticks"]):
            identity = {"state": "mismatch", "reason": "process_birth_changed"}
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
            if self._clear_process(profile_id, expected_record=record):
                return None
            # Another instance replaced the runtime while process identity was
            # being queried. Never turn that new record into a stopped result.
            return self._runtime_store.get(profile_id) or record
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
        # Observe and discard a process under the same lock used by launch.
        # Otherwise a status reader can poll old A, then clear newly launched B.
        with self._launch_locks[profile_id]:
            return self._is_running_locked(profile_id)

    def _is_running_locked(self, profile_id: str) -> bool:
        proc = self.active_processes.get(profile_id)
        if proc:
            if proc.poll() is None:
                return True
            if self._clear_process(profile_id):
                return False
            self._forget_stale_local_process(profile_id)
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
            # A runtime may have appeared while checking legacy metadata.
            return not self._clear_process(profile_id, expected_record={})
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
        pids = browser_pids_using_dir(user_data_dir)
        if pids is None:
            raise RuntimeError("PROCESS_STATE_UNVERIFIED: không xác minh được thư mục profile đã rảnh")
        return bool(pids)

    def _cleanup_failed_launch(self, profile_id, proc, launch_id, *, diagnostics=None):
        """Run the normal stop protocol only for this exact local launch."""
        started = time.monotonic()
        result = {"success": False, "code": "SESSION_CHANGED"}

        def still_owned(record, health):
            return (
                self.active_processes.get(profile_id) is proc
                and health.get("launch_id") == launch_id
                and health.get("owner_instance") == self.instance_id
                and health.get("pid") in (None, proc.pid)
                and (not record or (
                    record.get("launch_id") == launch_id
                    and record.get("pid") == proc.pid
                    and record.get("owner_instance") == self.instance_id
                ))
            )

        try:
            with self._lifecycle_locks.acquire(profile_id, timeout=4.0):
                with self._launch_locks[profile_id]:
                    record = self._runtime_store.get(profile_id) or {}
                    if not still_owned(record, self.cdp_health.snapshot(profile_id)):
                        return False
                    # Includes STOPPING before Browser.close, the normal native
                    # fallback, and a directory-idle check even without runtime.
                    result = self._stop_impl(profile_id)
                    if result.get("success"):
                        return True
                    current = self._runtime_store.get(profile_id) or {}
                    if still_owned(current, self.cdp_health.snapshot(profile_id)):
                        self.cdp_health.mark_degraded(
                            profile_id, launch_id, REASON_STOP_FAILED,
                            restart_required=True,
                        )
                        try:
                            self._runtime_store.upsert_if_owner(
                                profile_id, launch_id=launch_id, pid=proc.pid,
                                owner_instance=self.instance_id, cleanup_failed=True,
                                cdp_health=self.cdp_health.snapshot(profile_id),
                            )
                        except Exception:
                            pass  # Keep the exact local handle for recovery.
                    return False
        except ProfileLockTimeout:
            result = {"success": False, "code": "PROFILE_BUSY"}
            return False
        except Exception:
            # Cleanup can already be running from launch's exception handler.
            # Metadata failures must preserve its boolean contract and handle.
            result = {"success": False, "code": "CLEANUP_EXCEPTION"}
            try:
                with self._launch_locks[profile_id]:
                    health = self.cdp_health.snapshot(profile_id)
                    if (self.active_processes.get(profile_id) is proc
                            and health.get("launch_id") == launch_id
                            and health.get("owner_instance") == self.instance_id):
                        self.cdp_health.mark_degraded(
                            profile_id, launch_id, REASON_STOP_FAILED,
                            restart_required=True,
                        )
                        self._runtime_store.upsert_if_owner(
                            profile_id, launch_id=launch_id, pid=proc.pid,
                            owner_instance=self.instance_id, cleanup_failed=True,
                            cdp_health=self.cdp_health.snapshot(profile_id),
                        )
            except Exception:
                pass
            return False
        finally:
            if diagnostics is not None:
                diagnostics.update(
                    protocol="ordinary_stop", code=result.get("code"),
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                )

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
                if "/json/" in lower or "/devtools/" in lower or "/__antidetect_verify__/" in lower:
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
            with open_loopback(
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
                    with open_loopback(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                        return json.loads(r.read())
                targets = await asyncio.to_thread(pages)
                target = next(t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl"))
                async with connect_loopback(target["webSocketDebuggerUrl"], open_timeout=1, close_timeout=1) as ws:
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
        probe_started = time.monotonic()
        diagnostic = {"status": "failed", "stage": "resolve_browser", "error": "NO_BROWSER"}
        self._native_brand_probe_diagnostic = diagnostic
        browser_path = browser_path or self.browser_path
        if not browser_path:
            return None
        import tempfile
        import websockets

        user_data = tempfile.mkdtemp(prefix="antidetect-brand-probe-")
        port = get_free_port()
        proc = None
        try:
            diagnostic.update(stage="verify_and_spawn", error=None)
            with hold_trusted_browser(
                browser_path, custom_manifest_path=self._custom_cores_manifest_path()
            ):
                proc = subprocess.Popen(
                    [
                    browser_path,
                    f"--user-data-dir={user_data}",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-domain-reliability",
                    "--disable-sync",
                    "about:blank",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            page_ws = None
            diagnostic["stage"] = "page_endpoint"
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    def list_pages():
                        with open_loopback(
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
                diagnostic["error"] = "PAGE_ENDPOINT_TIMEOUT"
                return None

            async def _probe():
                diagnostic["stage"] = "websocket_connect"
                async with connect_loopback(page_ws, open_timeout=2) as ws:
                    try:
                        diagnostic["stage"] = "navigate"
                        await ws.send(json.dumps({
                            "id": 70001,
                            "method": "Page.navigate",
                            "params": {"url": f"http://127.0.0.1:{port}/json/version"},
                        }))
                        # 127.0.0.1 là secure context: userAgentData xuất hiện ở đó
                        # (about:blank thì không).
                        probe_deadline = time.monotonic() + 6.0
                        diagnostic["read_budget_seconds"] = 6.0
                        command_id = 70002
                        while time.monotonic() < probe_deadline:
                            diagnostic["stage"] = "native_read"
                            await ws.send(json.dumps({
                                "id": command_id,
                                "method": "Runtime.evaluate",
                                "params": {
                                    "expression": _NATIVE_BRANDS_JS,
                                    "awaitPromise": True,
                                    "returnByValue": True,
                                },
                            }))
                            # The async native read may await browser services
                            # during first startup. Use the remaining declared
                            # budget; a shorter per-reply timeout prematurely
                            # aborted this entire probe after only two seconds.
                            end = probe_deadline
                            while time.monotonic() < end:
                                msg = json.loads(
                                    await asyncio.wait_for(
                                        ws.recv(), timeout=max(0.05, end - time.monotonic())
                                    )
                                )
                                if msg.get("id") != command_id:
                                    continue
                                value = (
                                    (msg.get("result") or {}).get("result") or {}
                                ).get("value")
                                if (
                                    isinstance(value, dict)
                                    and isinstance(value.get("brands"), list)
                                    and value["brands"]
                                ):
                                    diagnostic.update(status="passed", error=None)
                                    return value
                                diagnostic["error"] = "EVALUATION_ERROR" if msg.get("error") else "NATIVE_BRANDS_UNAVAILABLE"
                                break
                            command_id += 1
                            await asyncio.sleep(0.15)
                        diagnostic["error"] = diagnostic["error"] or "NATIVE_READ_TIMEOUT"
                        return None
                    finally:
                        # Use the already connected, owned probe socket. Never
                        # reconnect by a port that may belong to a new process.
                        try:
                            await ws.send(json.dumps({"id": 79999, "method": "Browser.close"}))
                        except Exception:
                            pass

            return _safe_run_async(_probe())
        except Exception as error:
            diagnostic["error"] = ("NATIVE_READ_TIMEOUT" if isinstance(error, (asyncio.TimeoutError, TimeoutError))
                                   else "BROWSER_UNTRUSTED" if isinstance(error, BrowserTrustError)
                                   else "PROBE_TRANSPORT_OR_RUNTIME_ERROR")
            return None
        finally:
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._terminate_owned_children(user_data, exclude_pid=proc.pid)
                        proc.kill()
                        try:
                            proc.wait(timeout=25)
                        except subprocess.TimeoutExpired:
                            pass
                    self._terminate_owned_children(user_data, exclude_pid=proc.pid)
            release_port(port)
            if not self._wait_directory_idle(user_data):
                diagnostic.update(status="failed", stage="cleanup", error="CLEANUP_UNCONFIRMED")
                raise RuntimeError("Native browser probe cleanup unconfirmed; synthetic directory retained.")
            shutil.rmtree(user_data)
            diagnostic["elapsed_ms"] = round((time.monotonic() - probe_started) * 1000, 1)
            if diagnostic["status"] != "passed":
                log.warning("Native browser probe failed: stage=%s error=%s elapsed_ms=%s",
                            diagnostic["stage"], diagnostic["error"], diagnostic["elapsed_ms"])

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
        webgl_spoofed = (
            getattr(fp, "fingerprint_algorithm_version", 1) == 1
            and "webgl" not in (getattr(fp, "stealth_disable", None) or [])
        )
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
                    with open_loopback(
                        f"http://127.0.0.1:{port}/json/list", timeout=1
                    ) as r:
                        return json.loads(r.read())

                targets = await asyncio.to_thread(pages)
                target = next(
                    t for t in targets
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                )
                async with connect_loopback(
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

    async def _verify_on_local_origin(self, port, fp):
        """Check secure-context values on a data-free localhost page before targets."""
        with verification_origin() as url:
            with open_loopback(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                pages = json.loads(response.read())
            target = next(item for item in pages if item.get("type") == "page"
                          and item.get("webSocketDebuggerUrl"))
            async with connect_loopback(target["webSocketDebuggerUrl"],
                                        open_timeout=2, close_timeout=1) as ws:
                await ws.send(json.dumps({"id": 88001, "method": "Page.navigate",
                                          "params": {"url": url}}))
                while True:
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    if reply.get("id") == 88001:
                        if reply.get("error") or reply.get("result", {}).get("errorText"):
                            return {"ok": False, "checks": {}, "error": "local_navigation_failed"}
                        break
            return await self._verify_emulation(
                port, fp, require_client_hints=True, deadline_seconds=5)

    async def _setup_cdp_stealth(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
        fp_override: Optional[Any] = None,
        launch_id: Optional[str] = None,
        cpp_engine: Optional[str] = None,
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
        launch_gate = self._core_verified_events.get(profile.id)
        ready_event = self._cdp_ready_events.get(profile.id)
        navigation_event = self._navigation_events.get(profile.id)

        # Use the launch-time normalized copy, not the stored profile. The
        # stored model may still contain legacy RAM/screen values; the injected
        # JS and the verification probe must see the same values.
        fp = fp_override or profile.fingerprint
        native_brands = self._native_brands_cache.get(fp.browser_version)
        if cpp_engine:
            stealth_js = get_stealth_script(
                fp, native_brands=native_brands, cpp_engine=cpp_engine
            )
            worker_js = get_worker_stealth_script(
                fp, native_brands=native_brands, cpp_engine=cpp_engine
            )
        else:
            stealth_js = get_stealth_script(fp, native_brands=native_brands)
            worker_js = get_worker_stealth_script(fp, native_brands=native_brands)

        # Wait for Chrome's DevTools endpoint. DevToolsActivePort is more
        # reliable than the requested port because Chrome may choose another.
        ws_url = None
        for _ in range(25):
            if launch_id and (
                self.cdp_health.current_launch_id(profile.id) != launch_id
                or not self._cdp_process_alive(profile.id)
            ):
                if session is not None:
                    session.mark_closed()
                return
            if user_data_dir:
                active_port_file = os.path.join(user_data_dir, "DevToolsActivePort")
                if os.path.exists(active_port_file):
                    try:
                        with open(active_port_file, "r", encoding="utf-8") as _pf:
                            first_line = _pf.readline().strip()
                            if first_line.isdigit():
                                port = int(first_line)
                                try:
                                    if launch_id:
                                        self._runtime_store.upsert_if_owner(
                                            profile.id, launch_id=launch_id,
                                            owner_instance=self.instance_id,
                                            port=port, cdp_port_source="DevToolsActivePort",
                                        )
                                except Exception:
                                    pass
                    except Exception:
                        pass
            try:
                url = f"http://127.0.0.1:{port}/json/version"
                req = urllib.request.Request(url)
                with open_loopback(req, timeout=1) as resp:
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
            async with connect_loopback(
                ws_url, max_size=20 * 1024 * 1024
            ) as ws:
                msg_id = 0
                attached_sessions: Set[str] = set()
                target_owners = {}
                worker_pauses = {}
                proxy_auth_attempts = ProxyAuthAttempts()

                async def send(method, params=None, session_id=None, sent_event=None):
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
                    if sent_event is not None:
                        sent_event.set()
                    try:
                        res = await asyncio.wait_for(
                            fut, timeout=HEARTBEAT_TIMEOUT_SECONDS
                        )
                        if "error" in res:
                            raise RuntimeError(f"CDP {method} error: {res['error']}")
                        return res.get("result", {})
                    except asyncio.TimeoutError as exc:
                        raise asyncio.TimeoutError(f"CDP {method} response timed out") from exc
                    finally:
                        pending_responses.pop(current_id, None)

                expected_close_markers = (
                    "Cannot find default execution context",
                    "Session closed",
                    "No target with given id",
                    "Target closed",
                    "Inspected target navigated or closed",
                    "Session with given id not found",
                )

                def spawn_protocol_reply(coro, cleanup_session=None, cleanup_target=None):
                    """Run a one-shot protocol reply without leaking unretrieved close/timeout exceptions."""
                    async def guarded_reply():
                        try:
                            await coro
                        except Exception:
                            if cleanup_session:
                                attached_sessions.discard(cleanup_session)
                            if cleanup_target and target_owners.get(cleanup_target) == cleanup_session:
                                target_owners.pop(cleanup_target, None)

                    task = asyncio.create_task(guarded_reply())
                    target_tasks.add(task)
                    task.add_done_callback(target_tasks.discard)
                    return task

                def spawn_target_task(coro, session_id, target_id):
                    """Run target setup without leaking expected close races as task tracebacks."""
                    quarantined_state = {"isolated": False}

                    async def guarded():
                        try:
                            await coro
                        except Exception as setup_exc:
                            # Strict launches must not run a target whose setup
                            # failed. Close only that target; if closing fails,
                            # retain its pause and report degraded health.
                            stopping_now = bool(
                                launch_id and self.cdp_health.is_stopping(profile.id)
                            )
                            try:
                                if fp.verification_mode == "strict":
                                    if target_id:
                                        try:
                                            close_res = await send(
                                                "Target.closeTarget", {"targetId": target_id}
                                            )
                                            if close_res.get("success") is True:
                                                quarantined_state["isolated"] = True
                                        except Exception as close_exc:
                                            if any(
                                                marker in str(close_exc)
                                                for marker in expected_close_markers
                                            ):
                                                quarantined_state["isolated"] = True
                                            else:
                                                raise
                                    if (
                                        not quarantined_state["isolated"]
                                        and target_id
                                        and self._cdp_process_alive(profile.id)
                                        and not stopping_now
                                    ):
                                        targets_res = await send("Target.getTargets")
                                        live_ids = {
                                            str(info.get("targetId") or "")
                                            for info in (targets_res or {}).get("targetInfos") or []
                                            if isinstance(info, dict)
                                        }
                                        if str(target_id) not in live_ids:
                                            quarantined_state["isolated"] = True
                                else:
                                    if session_id in worker_pauses:
                                        await send("Debugger.disable", session_id=session_id)
                                    await send(
                                        "Runtime.runIfWaitingForDebugger",
                                        session_id=session_id,
                                    )
                            except Exception as cleanup_exc:
                                if any(
                                    marker in str(cleanup_exc)
                                    for marker in expected_close_markers
                                ):
                                    quarantined_state["isolated"] = True
                                elif (
                                    fp.verification_mode != "strict"
                                    and isinstance(setup_exc, asyncio.TimeoutError)
                                    and target_id
                                    and self._cdp_process_alive(profile.id)
                                    and not stopping_now
                                ):
                                    try:
                                        targets_res = await send("Target.getTargets")
                                        live_ids = {
                                            str(info.get("targetId") or "")
                                            for info in (targets_res or {}).get("targetInfos") or []
                                            if isinstance(info, dict)
                                        }
                                        if str(target_id) not in live_ids:
                                            quarantined_state["isolated"] = True
                                    except Exception:
                                        pass
                            finally:
                                worker_pauses.pop(session_id, None)
                                if quarantined_state["isolated"]:
                                    attached_sessions.discard(session_id)
                                    if target_id and target_owners.get(target_id) == session_id:
                                        target_owners.pop(target_id, None)
                                    self._discard_configured_target(profile.id, target_id)
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
                        stopping = launch_id and self.cdp_health.is_stopping(profile.id)
                        if any(marker in text for marker in expected_close_markers):
                            return
                        if quarantined_state["isolated"]:
                            log.info(
                                "[Launcher] Đã cô lập target lỗi cấu hình CDP (%s): %s: %s",
                                target_id,
                                type(exc).__name__,
                                text,
                            )
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
                    # Shared/service workers belong only to the browser session
                    # so two debugger sessions cannot race their startup pause.
                    await send(
                        "Target.setAutoAttach",
                        {
                            "autoAttach": True,
                            "waitForDebuggerOnStart": True,
                            "flatten": True,
                            "filter": [{"type": "service_worker", "exclude": True},
                                       {"type": "shared_worker", "exclude": True}, {}],
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth_js, "runImmediately": True},
                        session_id=session_id,
                    )
                    # Chrome installs once in each existing/future document.
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
                    self._mark_target_configured(
                        profile.id, launch_id, target_info.get("targetId")
                    )
                    # R4: signal readiness only after the document is unpaused.
                    # The main thread can now verify core values on the initial
                    # about:blank page. Navigation waits for that handshake so
                    # a slow page cannot invalidate verification halfway through.
                    if ready_event is not None and (
                        not launch_id or self.cdp_health.current_launch_id(profile.id) == launch_id
                    ):
                        ready_event.set()
                    if first_tab:
                        try:
                            core_event = launch_gate
                            allowed = core_event is not None and await asyncio.to_thread(core_event.wait, 20.0)
                            if (not allowed or (launch_id and
                                    self.cdp_health.current_launch_id(profile.id) != launch_id)):
                                if navigation_event is not None:
                                    navigation_event.set()
                                return
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
                                elif fp.verification_mode == "strict":
                                    # The transient verification server has stopped.
                                    # Do not leave its dead URL as the user's tab.
                                    await send("Page.navigate", {"url": "about:blank"},
                                               session_id=session_id)
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
                            "filter": [{"type": "service_worker", "exclude": True},
                                       {"type": "shared_worker", "exclude": True}, {}],
                        },
                        session_id=session_id,
                    )
                    await send(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": stealth_js, "runImmediately": True},
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
                    # A service worker may wait before creating its execution
                    # context. Release that startup wait only after arming a
                    # before-script breakpoint, then inject at the first script.
                    service_pause = None
                    if target_info.get("type") == "service_worker":
                        service_pause = asyncio.Event()
                        worker_pauses[session_id] = service_pause
                        # Chrome queues worker-domain commands until startup is
                        # released. Put both debugger commands on the wire in
                        # order before release; awaiting their responses first
                        # deadlocks because the context does not yet exist.
                        startup_tasks = []
                        try:
                            for method, params in (
                                ("Debugger.enable", {}),
                                ("Debugger.setInstrumentationBreakpoint",
                                 {"instrumentation": "beforeScriptExecution"}),
                            ):
                                queued = asyncio.Event()
                                startup_tasks.append(asyncio.create_task(send(
                                    method, params, session_id=session_id,
                                    sent_event=queued,
                                )))
                                await asyncio.wait_for(queued.wait(), timeout=HEARTBEAT_TIMEOUT_SECONDS)
                            await send("Runtime.runIfWaitingForDebugger", session_id=session_id)
                            _, breakpoint = await asyncio.gather(*startup_tasks)
                        finally:
                            for startup_task in startup_tasks:
                                if not startup_task.done():
                                    startup_task.cancel()
                            await asyncio.gather(*startup_tasks, return_exceptions=True)
                        try:
                            await asyncio.wait_for(service_pause.wait(), timeout=HEARTBEAT_TIMEOUT_SECONDS)
                        except asyncio.TimeoutError as exc:
                            raise asyncio.TimeoutError("Service worker first-script pause timed out") from exc
                        await send("Debugger.removeBreakpoint",
                                   {"breakpointId": breakpoint["breakpointId"]},
                                   session_id=session_id)
                    await send("Runtime.enable", session_id=session_id)
                    # Auto-attachment is not recursive: dedicated workers can
                    # create further workers before the page session sees them.
                    await send(
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": True,
                         "flatten": True,
                         "filter": [{"type": "service_worker", "exclude": True},
                                    {"type": "shared_worker", "exclude": True}, {}]},
                        session_id=session_id,
                    )
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
                    evaluated = await send(
                        "Runtime.evaluate",
                        {
                            "expression": worker_js,
                            "silent": True,
                            "returnByValue": False,
                            "awaitPromise": False,
                        },
                        session_id=session_id,
                    )
                    if evaluated.get("exceptionDetails"):
                        raise RuntimeError("Worker configuration evaluation failed")
                    if service_pause is not None:
                        await send("Debugger.resume", session_id=session_id)
                        await send("Debugger.disable", session_id=session_id)
                        worker_pauses.pop(session_id, None)
                    else:
                        await send(
                            "Runtime.runIfWaitingForDebugger", session_id=session_id
                        )

                async def detach_duplicate(session_id):
                    try:
                        await send("Target.detachFromTarget", {"sessionId": session_id})
                    except Exception:
                        # The duplicate may already have disappeared. Never
                        # resume or reinject through it on this cleanup path.
                        pass

                async def dispatch_event(event):
                    method = event.get("method", "")
                    params = event.get("params", {})
                    session_id = event.get("sessionId")

                    if method == "Debugger.paused" and session_id in worker_pauses:
                        worker_pauses[session_id].set()
                        return

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
                        detached = params.get("sessionId")
                        attached_sessions.discard(detached)
                        worker_pauses.pop(detached, None)
                        proxy_auth_attempts.discard_session(detached)
                        for target_id, owner in list(target_owners.items()):
                            if owner == detached:
                                target_owners.pop(target_id, None)
                                self._discard_configured_target(profile.id, target_id)
                        asyncio.create_task(sync_session_tabs(0.35))
                        return

                    if method == "Target.attachedToTarget":
                        target_info = params.get("targetInfo", {})
                        target_type = target_info.get("type", "")
                        attached_id = params.get("sessionId")
                        if not attached_id or attached_id in attached_sessions:
                            return
                        target_id = target_info.get("targetId")
                        if target_id and target_id in target_owners:
                            # Browser and page auto-attach can report the same
                            # worker through different sessions. Only its first
                            # session may install or release the startup pause.
                            # Detaching the duplicate does not run user code.
                            task = asyncio.create_task(detach_duplicate(attached_id))
                            target_tasks.add(task)
                            task.add_done_callback(target_tasks.discard)
                            return
                        if target_id:
                            target_owners[target_id] = attached_id
                        attached_sessions.add(attached_id)
                        if target_type == "page":
                            spawn_target_task(
                                configure_page(attached_id, target_info), attached_id,
                                target_info.get("targetId"),
                            )
                        elif target_type == "iframe":
                            spawn_target_task(
                                configure_iframe(attached_id, target_info), attached_id,
                                target_info.get("targetId"),
                            )
                        elif target_type in (
                            "worker",
                            "service_worker",
                            "shared_worker",
                        ):
                            spawn_target_task(
                                configure_worker(attached_id, target_info), attached_id,
                                target_info.get("targetId"),
                            )
                        else:
                            spawn_protocol_reply(
                                send(
                                    "Runtime.runIfWaitingForDebugger",
                                    session_id=attached_id,
                                ),
                                cleanup_session=attached_id,
                                cleanup_target=target_id,
                            )

                    elif method == "Fetch.authRequired":
                        request_id = params.get("requestId")
                        challenge = params.get("authChallenge", {})
                        if request_id:
                            spawn_protocol_reply(
                                send(
                                    "Fetch.continueWithAuth",
                                    {
                                        "requestId": request_id,
                                        "authChallengeResponse": proxy_auth_attempts.response(
                                            session_id, request_id, challenge, proxy_auth
                                        ),
                                    },
                                    session_id=session_id,
                                )
                            )

                    elif method == "Fetch.requestPaused":
                        request_id = params.get("requestId")
                        if request_id:
                            spawn_protocol_reply(
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
                if session is not None:
                    event_loop = asyncio.get_running_loop()

                    def request_browser_close():
                        # Use this existing socket, which belongs to the launch;
                        # opening a new connection by port could hit a new owner.
                        async def close_current():
                            if (session.is_open() and
                                    self.cdp_health.current_launch_id(profile.id) == launch_id):
                                await send("Browser.close")
                        coroutine = close_current()
                        try:
                            future = asyncio.run_coroutine_threadsafe(coroutine, event_loop)
                        except RuntimeError:
                            coroutine.close()
                            return False
                        def consume_close(completed):
                            if not completed.cancelled():
                                completed.exception()  # socket closure may precede the reply
                        future.add_done_callback(consume_close)
                        return True

                    session.bind_browser_close(request_browser_close)
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
            if not launch_id or self.cdp_health.current_launch_id(profile.id) == launch_id:
                self._discard_configured_target(profile.id)
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
        cpp_engine: Optional[str] = None,
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
                    cpp_engine=cpp_engine,
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

        def safe_candidate(path: str) -> Optional[str]:
            # Selection is a local trust decision; imports cannot set this field.
            # Do not follow network paths, aliases or reparse points at execution.
            import stat
            path = os.path.abspath(path)
            if path.startswith(("\\\\", "//")):
                return None
            cursor = path
            try:
                while True:
                    entry = os.lstat(cursor)
                    if stat.S_ISLNK(entry.st_mode) or (
                        getattr(entry, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    ):
                        return None
                    parent = os.path.dirname(cursor)
                    if parent == cursor:
                        break
                    cursor = parent
                if (os.path.basename(path).lower() not in
                        {"chrome.exe", "msedge.exe", "chromium.exe"} or not os.path.isfile(path)):
                    return None
                return path
            except OSError:
                return None

        def resolve_pinned(name: str) -> Optional[str]:
            def find_exe(directory: str) -> Optional[str]:
                direct = safe_candidate(os.path.join(directory, "chrome-win64", "chrome.exe"))
                if direct:
                    return direct
                for root, dirs, files in os.walk(directory, followlinks=False):
                    # Each candidate validates all ancestors, including junctions.
                    for filename in ("chrome.exe", "msedge.exe", "chromium.exe"):
                        if filename in files:
                            candidate = safe_candidate(os.path.join(root, filename))
                            if candidate:
                                return candidate
                    # Avoid traversing junctions even when they have no candidate.
                    dirs[:] = [entry for entry in dirs
                               if not os.path.islink(os.path.join(root, entry))
                               and not (getattr(os.lstat(os.path.join(root, entry)), "st_file_attributes", 0) & 0x400)]
                return None

            if os.path.isabs(name):
                if os.path.isfile(name):
                    return safe_candidate(name)
                if os.path.isdir(name):
                    return find_exe(name)
                return None
            # A managed pin is one directory name, never a path.
            if name in {".", ".."} or any(char in name for char in "/\\:"):
                return None
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
                return safe_candidate(os.path.join(
                    BROWSERS_DIR, versions[-1], "chrome-win64", "chrome.exe"
                ))
        return safe_candidate(self.browser_path) if self.browser_path else None

    def begin_shutdown(self) -> None:
        """Refuse new launches; in-flight launches clean their exact generation."""
        self._shutdown_requested = True

    def launch(
        self, profile_id: str, open_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Instrumented entrypoint; see ``_launch_impl`` for the real flow."""
        metrics.inc("launch_attempts")
        started = time.monotonic()
        try:
            with self._lifecycle_locks.acquire(profile_id, timeout=2.0):
                if getattr(self, "_shutdown_requested", False):
                    result = {"success": False, "code": "SERVER_SHUTTING_DOWN", "error": "Server đang tắt; không mở thêm profile."}
                else:
                    result = self._launch_impl(profile_id, open_url=open_url)
                    if getattr(self, "_shutdown_requested", False) and result.get("launched_now"):
                        launch_id = result.get("launch_id") or (result.get("cdp_health") or {}).get("launch_id")
                        cleanup = self.stop(profile_id, expected_launch_id=launch_id, expected_owner_instance=self.instance_id) if launch_id else {"success": False, "code": "SESSION_ID_UNKNOWN"}
                        result = {"success": False, "code": "SERVER_SHUTTING_DOWN", "error": "Server đã bắt đầu tắt trong lúc mở profile.", "cleanup": cleanup}
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
            if (profile.fingerprint.verification_mode == "strict"
                    and profile.fingerprint.fingerprint_algorithm_version < 2):
                return {"success": False, "code": "FINGERPRINT_MIGRATION_REQUIRED",
                        "error": "Strict verification cần fingerprint v2; hãy nâng phiên bản thuật toán một cách chủ động.",
                        "navigation_allowed": False, "browser_started": False}

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

            # This is a configuration invariant, independent of the optional
            # fingerprint gate/force policy in API and background callers.
            proxy_error = profile.proxy.browser_configuration_error() if profile.proxy else None
            if proxy_error:
                return {"success": False, "code": "INVALID_PROXY_CONFIG", "error": proxy_error}

            try:
                attestation = verify_browser_executable(
                    browser_path, custom_manifest_path=self._custom_cores_manifest_path()
                )
            except BrowserTrustError as exc:
                return {"success": False, "code": exc.code, "error": str(exc),
                        "browser_started": False, "navigation_allowed": False}
            cpp_engine = (
                attestation.engine
                if getattr(attestation, "engine", "chromium-branded") != "chromium-branded"
                else None
            )

            profile_dir = self.manager.get_profile_dir(profile_id)
            user_data_dir = os.path.join(profile_dir, "user_data")
            if os.path.isdir(user_data_dir):
                other_processes = browser_pids_using_dir(user_data_dir)
                if other_processes is None:
                    return {"success": False, "code": "PROCESS_STATE_UNVERIFIED",
                            "error": "Chưa xác minh được thư mục profile đã rảnh; chưa khởi chạy."}
                if other_processes:
                    return {"success": False, "code": "PROFILE_BUSY",
                            "error": "Thư mục profile đang được trình duyệt khác sử dụng."}
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
            if cpp_engine:
                args.extend(build_cpp_engine_flags(cpp_engine, fp))
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

            # Mở trang trắng theo mặc định; chỉ điều hướng ra ngoài khi người dùng chỉ định URL.
            target_url = open_url or "about:blank"
            args.append("about:blank")

            # Mỗi lần launch là một generation mới; callback của session cũ
            # không được ghi đè trạng thái của lần mở này.
            launch_id = self.cdp_health.begin_launch(profile_id, self.instance_id)

            # Khởi chạy tiến trình Browser
            proc = None
            try:
                with hold_trusted_browser(
                    browser_path, custom_manifest_path=self._custom_cores_manifest_path()
                ) as launch_attestation:
                    if getattr(launch_attestation, "engine", "chromium-branded") != getattr(
                        attestation, "engine", "chromium-branded"
                    ):
                        raise BrowserTrustError("Loại engine của browser đã đổi trước khi khởi chạy.")
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
                        birth_ticks=browser_process_birth(proc.pid),
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
                core_verified = LaunchVerification(launch_id)
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
                if cpp_engine:
                    self._start_cdp_thread(
                        port,
                        profile,
                        target_url,
                        user_data_dir,
                        proxy_auth,
                        fp,
                        launch_id=launch_id,
                        cpp_engine=cpp_engine,
                    )
                else:
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

                # Read the generation-bound port under the launch lock. A late
                # CDP thread must not recreate local state after cleanup.
                current_runtime = self._runtime_store.get(profile_id) or {}
                actual_port = (current_runtime.get("port")
                               if current_runtime.get("launch_id") == launch_id else None) or port
                self.active_ports[profile_id] = actual_port
                page_ready = _safe_run_async(self._probe_page_ready(actual_port))
                verification = {"ok": False, "checks": {}, "actual": None}
                core_verification = verification
                if page_ready and stealth_ok:
                    # Phase 1: verify the initial about:blank document before
                    # the CDP thread is allowed to navigate.
                    core_verification = _safe_run_async(
                        self._verify_on_local_origin(actual_port, fp)
                        if fp.verification_mode == "strict"
                        else self._verify_emulation(actual_port, fp)
                    )
                    verification = core_verification
                core_verified.complete(
                    launch_id, bool(core_verification.get("ok")) and runtime_persisted)
                navigation_allowed = core_verified.state == "verified"
                if fp.verification_mode == "strict" and not navigation_allowed:
                    cleanup_diagnostics = {}
                    cleanup_ok = self._cleanup_failed_launch(
                        profile_id, proc, launch_id, diagnostics=cleanup_diagnostics,
                    )
                    return {
                        "success": False, "code": "CONFIGURATION_UNVERIFIED",
                        "error": "Không xác minh được cấu hình; chưa mở trang đích.",
                        "browser_started": True, "configuration_verified": False,
                        "navigation_allowed": False, "cleanup_failed": not cleanup_ok,
                        "cleanup_diagnostics": cleanup_diagnostics,
                        "verification": verification, "launched_now": True,
                        "fingerprint_capabilities": (
                            get_stealth_capabilities(fp, cpp_engine=cpp_engine)
                            if cpp_engine
                            else get_stealth_capabilities(fp)
                        ),
                        "cdp_health": self.cdp_health.snapshot(profile_id),
                    }

                # Phase 2: after navigation, verify Client Hints on HTTPS
                # documents. about:blank has no secure-context Client Hints,
                # so that check belongs only to this second pass.
                if navigation_allowed and stealth_ok and page_ready and target_url and target_url != "about:blank":
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
                capabilities = (
                    get_stealth_capabilities(fp, cpp_engine=cpp_engine)
                    if cpp_engine
                    else get_stealth_capabilities(fp)
                )
                if cpp_engine == "fingerprint-chromium":
                    launch_warnings.append(
                        "C++ core (fingerprint-chromium): phân tách phần cứng ở tầng C++ được kích hoạt theo seed của profile."
                    )
                elif fp.fingerprint_algorithm_version == 2:
                    launch_warnings.append(
                        "Fingerprint v2: chỉ Canvas 2D có phân tách; WebGL/audio/fonts/media/WebGPU dùng giá trị native chung của máy."
                    )
                else:
                    launch_warnings.append(
                        "Fingerprint legacy v1 còn giới hạn nhất quán; chưa nâng thuật toán tự động."
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
                    "launch_id": launch_id,
                    "owner_instance": self.instance_id,
                    "ws_endpoint": f"http://127.0.0.1:{actual_port}",
                    "cdp_ready": cdp_ready_final,
                    "page_ready": page_ready,
                    "stealth_injected": stealth_ok,
                    "emulation_verified": configuration_verified,
                    "configuration_verified": configuration_verified,
                    "browser_started": True,
                    "navigation_allowed": navigation_allowed,
                    "fingerprint_capabilities": capabilities,
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
                cleanup_diagnostics = {}
                cleanup_ok = (
                    self._cleanup_failed_launch(
                        profile_id, proc, launch_id, diagnostics=cleanup_diagnostics,
                    )
                    if proc is not None else self._clear_process(profile_id)
                )
                self._cdp_ready_events.pop(profile_id, None)
                self._core_verified_events.pop(profile_id, None)
                self._navigation_events.pop(profile_id, None)
                log.error(f"[Launcher] Lỗi khởi chạy: {type(e).__name__}: {e}")
                return {
                    "cleanup_failed": not cleanup_ok,
                    "cleanup_diagnostics": cleanup_diagnostics,
                    "success": False,
                    "code": e.code if isinstance(e, BrowserTrustError) else "LAUNCH_FAILED",
                    "browser_started": proc is not None,
                    "navigation_allowed": False,
                    "error": str(e) if isinstance(e, BrowserTrustError) else "Lỗi khởi chạy trình duyệt. Vui lòng thử lại.",
                }

    def stop(self, profile_id: str, *, expected_launch_id: Optional[str] = None, expected_owner_instance: Optional[str] = None) -> Dict[str, Any]:
        """Instrumented entrypoint; see ``_stop_impl`` for the real flow."""
        metrics.inc("stop_attempts")
        try:
            with self._lifecycle_locks.acquire(profile_id, timeout=4.0):
                with self._launch_locks[profile_id]:
                    record = self._runtime_store.get(profile_id) or {}
                    current = record.get("launch_id") or self.cdp_health.current_launch_id(profile_id)
                    owner = record.get("owner_instance") or self.cdp_health.snapshot(profile_id).get("owner_instance")
                    if (expected_launch_id is not None and current != expected_launch_id) or (expected_owner_instance is not None and owner != expected_owner_instance):
                        result = {"success": False, "code": "SESSION_CHANGED", "error": "Phiên profile đã thay đổi."}
                    else:
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
            record = self._runtime_store.get(profile_id) or {}
            user_data_dir = record.get("user_data_dir") or os.path.join(
                PROFILES_DATA_DIR, profile_id, "user_data"
            )
            launch_id = record.get("launch_id") or self.cdp_health.current_launch_id(profile_id)
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

            if proc and not self._runtime_matches_local(profile_id, record):
                if proc.poll() is not None:
                    self._forget_stale_local_process(profile_id)
                return {"success": False, "code": "SESSION_CHANGED", "error": "Phiên profile đã thay đổi; hãy kiểm tra lại trước khi dừng."}
            if proc and proc.poll() is not None:
                if not self._wait_directory_idle(user_data_dir):
                    return _stop_failed("Chưa xác nhận các tiến trình con đã dừng; giữ trạng thái để kiểm tra lại.")
                if not self._clear_process(profile_id, expected_record=record):
                    return {"success": False, "code": "SESSION_CHANGED", "error": "Runtime profile đã thay đổi; dữ liệu được giữ nguyên."}
                return {"success": True, "message": "Trình duyệt đã đóng"}

            pid = proc.pid if proc else (
                record.get("pid") or (profile.pid if profile else None)
            )
            if not pid:
                if not self._wait_directory_idle(user_data_dir):
                    return _stop_failed("Thư mục profile còn được sử dụng hoặc chưa xác minh được.")
                if not self._clear_process(profile_id, expected_record=record):
                    return {"success": False, "code": "SESSION_CHANGED", "error": "Runtime profile đã thay đổi; dữ liệu được giữ nguyên."}
                return {"success": True, "message": "Profile đã dừng"}
            if proc is not None and proc.poll() is None:
                live_port = self.active_ports.get(profile_id)
                if live_port:
                    self._snapshot_tabs_via_http(live_port, user_data_dir)
            if self.cdp_health.mark_stopping(profile_id):
                stopping_snapshot = self.cdp_health.snapshot(profile_id)
                if launch_id:
                    self._persist_health_now(
                        profile_id, launch_id, stopping_snapshot
                    )

            identity = None
            if not proc:
                identity = self._identity_for_stop(pid, user_data_dir)
                if identity["state"] in ("absent", "mismatch"):
                    if not self._wait_directory_idle(user_data_dir):
                        return _stop_failed("Thư mục profile còn được sử dụng hoặc chưa xác minh được.")
                    if not self._clear_process(profile_id, expected_record=record):
                        return {"success": False, "code": "SESSION_CHANGED", "error": "Runtime profile đã thay đổi; dữ liệu được giữ nguyên."}
                    return {"success": True, "message": "Phiên cũ đã kết thúc"}
                if identity["state"] != "owned":
                    return _stop_failed("Chưa xác minh được tiến trình của profile; không gửi lệnh tắt.")
            try:
                exited = False
                if sys.platform == "win32":
                    if proc:
                        # Popen owns the native handle, so PID reuse cannot
                        # redirect termination to a different process.
                        if self.cdp_health.request_browser_close(profile_id, launch_id):
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                pass
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                self._terminate_owned_children(user_data_dir, exclude_pid=proc.pid)
                                proc.kill()
                                proc.wait(timeout=25)
                            self._terminate_owned_children(user_data_dir, exclude_pid=proc.pid)
                        exited = proc.poll() is not None
                    else:
                        if record.get("created") and record["created"] != identity.get("created"):
                            return _stop_failed("Tiến trình đã thay đổi; không gửi lệnh tắt.")
                        if (record.get("birth_ticks") is not None
                                and record["birth_ticks"] != identity.get("birth_ticks")):
                            return _stop_failed("Tiến trình đã thay đổi; không gửi lệnh tắt.")
                        outcome = terminate_verified_browser(pid, user_data_dir, identity)
                        exited = outcome.get("state") == "absent"
                        if not exited:
                            return _stop_failed("Chưa xác nhận được tiến trình đã dừng; dữ liệu trạng thái được giữ nguyên.")
                        self._terminate_owned_children(user_data_dir, exclude_pid=pid)
                elif proc:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=25)
                    exited = proc.poll() is not None
                else:
                    return _stop_failed("Không xác minh được quyền sở hữu tiến trình.")
                if not exited:
                    return _stop_failed("Trình duyệt vẫn đang chạy; chưa thể báo đã dừng.")
                if not self._wait_directory_idle(user_data_dir):
                    return _stop_failed("Chưa xác nhận các tiến trình con đã dừng; giữ trạng thái để kiểm tra lại.")
            except (OSError, subprocess.SubprocessError):
                return _stop_failed("Không thể dừng trình duyệt; trạng thái được giữ nguyên.")
            if not self._clear_process(profile_id, expected_record=record):
                return {"success": False, "code": "SESSION_CHANGED", "error": "Runtime profile đã thay đổi; dữ liệu được giữ nguyên."}
            return {"success": True, "message": "Đã xác nhận trình duyệt dừng"}

    def stop_owned(self):
        """Automatic cleanup only closes generations owned by this instance."""
        candidates = {}
        for profile_id, record in self._runtime_store.all().items():
            if record.get("owner_instance") == self.instance_id and record.get("launch_id"):
                candidates[profile_id] = record["launch_id"]
        for profile_id in list(self.active_processes):
            snapshot = self.cdp_health.snapshot(profile_id)
            if snapshot.get("owner_instance") == self.instance_id and snapshot.get("launch_id"):
                candidates.setdefault(profile_id, snapshot["launch_id"])
        results = {
            profile_id: self.stop(profile_id, expected_launch_id=launch_id, expected_owner_instance=self.instance_id)
            for profile_id, launch_id in candidates.items()
        }
        failed = [pid for pid, result in results.items() if not result.get("success") and result.get("code") != "SESSION_CHANGED"]
        return {"success": not failed, "failed_profile_ids": failed, "results": results}

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
            with open_loopback(req, timeout=3) as resp:
                return resp.status in (200, 201)
        except Exception:
            try:
                from urllib.parse import quote
                with open_loopback(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=#%')}", timeout=3) as resp:
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

    def close_transient_tab(self, profile_id: str, target_id: str) -> bool:
        """Đóng tab tạm do automation mở và gỡ khỏi TRANSIENT_TARGET_IDS."""
        tid = str(target_id or "").strip()
        if not tid:
            return False
        TRANSIENT_TARGET_IDS.discard(tid)
        self._discard_configured_target(profile_id, tid)
        port = self.active_ports.get(profile_id)
        if not port:
            record = self._runtime_store.get(profile_id) or {}
            port = record.get("port")
        if not port:
            return False
        for method in ("PUT", "GET"):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{int(port)}/json/close/{tid}", method=method
                )
                with open_loopback(req, timeout=3) as resp:
                    if resp.status in (200, 201, 204):
                        return True
            except Exception:
                continue
        return False

    def open_transient_tab(
        self, profile_id: str, *, timeout: float = 8.0
    ) -> Dict[str, Any]:
        """Mở tab tạm about:blank và đợi stealth/UA/timezone hoàn tất trước khi trả ws_url.

        Đảm bảo công cụ bên ngoài (ví dụ tool bình luận) không bao giờ gọi
        Page.navigate trước khi configure_page đặt xong Network.setUserAgentOverride
        và Page.addScriptToEvaluateOnNewDocument, cũng không bao giờ mở tab trên
        profile đang degraded hoặc detached.
        """
        health = self.require_cdp(profile_id)
        port = self.active_ports.get(profile_id)
        if not port:
            record = self._runtime_store.get(profile_id) or {}
            port = record.get("port")
            if port:
                self.active_ports[profile_id] = int(port)
        if not port:
            raise CdpUnavailable("NOT_RUNNING", "Hãy mở profile trước khi tạo tab tạm.", health)

        deadline = time.monotonic() + max(1.0, float(timeout))
        info: Dict[str, Any] = {}
        for method in ("PUT", "GET"):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{int(port)}/json/new?about%3Ablank", method=method
                )
                with open_loopback(req, timeout=3) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    info = json.loads(raw) if raw.strip() else {}
                if info.get("id"):
                    break
            except Exception:
                continue

        target_id = str(info.get("id") or "").strip()
        if not target_id:
            raise CdpUnavailable(
                "CDP_UNHEALTHY",
                "Chrome CDP không trả về target_id khi mở tab tạm.",
                self.cdp_health_snapshot(profile_id),
            )
        TRANSIENT_TARGET_IDS.add(target_id)
        try:
            ws_url = info.get("webSocketDebuggerUrl")
            while time.monotonic() < deadline and not ws_url:
                try:
                    with open_loopback(
                        f"http://127.0.0.1:{int(port)}/json/list", timeout=2
                    ) as resp:
                        targets = json.loads(resp.read().decode("utf-8", "replace"))
                    for item in targets or []:
                        if isinstance(item, dict) and item.get("id") == target_id and item.get("webSocketDebuggerUrl"):
                            ws_url = item["webSocketDebuggerUrl"]
                            break
                except Exception:
                    pass
                if not ws_url:
                    time.sleep(0.1)

            remaining = max(0.2, deadline - time.monotonic())
            configured = bool(ws_url) and self.wait_for_target_configured(
                profile_id, target_id, timeout=remaining
            )
            guard = self.cdp_guard(profile_id)
            if not ws_url or not configured or not guard["ok"]:
                raise CdpUnavailable(
                    guard["code"] or "CDP_UNHEALTHY",
                    guard["message"] or "Tab tạm chưa xác nhận hoàn tất cấu hình stealth; đã đóng tab.",
                    guard["health"],
                )
            return {
                "success": True,
                "profile_id": profile_id,
                "port": int(port),
                "target_id": target_id,
                "ws_url": str(ws_url),
                "launch_id": (guard["health"] or {}).get("launch_id"),
                "owner_instance": (guard["health"] or {}).get("owner_instance"),
                "cdp_health": guard["health"],
            }
        except BaseException:
            self.close_transient_tab(profile_id, target_id)
            raise

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
            per_page = 2 * DEEP_NAVIGATION_TIMEOUT_S
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
