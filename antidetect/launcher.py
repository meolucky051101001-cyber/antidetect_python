import os
import sys
import json
import time
import socket
import shutil
import asyncio
import threading
import subprocess
import urllib.request
import collections
from typing import Optional, Dict, Any, List

from .models import Profile
from .storage import ProfileManager, DATA_DIR, PROFILES_DATA_DIR
from .browser_runtime import BrowserRuntimeStore
from .stealth import get_stealth_script, get_worker_stealth_script
from .proxy import create_proxy_auth_extension
from .process_identity import read_browser_identity
from .fingerprint import normalize_fingerprint_for_launch, get_version_from_browser_path


def find_browser_executable() -> str:
    """Tự động tìm kiếm đường dẫn Google Chrome hoặc Microsoft Edge trên máy."""
    candidates = []

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get(
            "ProgramFiles(x86)", "C:\\Program Files (x86)"
        )

        candidates = [
            os.path.join(prog_files, "Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(
                prog_files_x86, "Google\\Chrome\\Application\\chrome.exe"
            ),
            os.path.join(
                local_app_data, "Google\\Chrome\\Application\\chrome.exe"
            ),
            os.path.join(
                prog_files_x86, "Microsoft\\Edge\\Application\\msedge.exe"
            ),
            os.path.join(prog_files, "Microsoft\\Edge\\Application\\msedge.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

    for path in candidates:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        "Không tìm thấy Google Chrome hoặc Microsoft Edge trên máy!"
    )


_PORT_LOCK = threading.Lock()
_RESERVED_PORTS: set = set()


def get_free_port(exclude_ports: Optional[set] = None) -> int:
    """Find and reserve a loopback port for a debugging endpoint.

    Binding and closing a socket is inherently racy, so keep the chosen port
    reserved in-process until the browser is stopped. This prevents two
    concurrent launches from receiving the same port.
    """
    exclude = set(exclude_ports or set())
    with _PORT_LOCK:
        for _ in range(50):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                candidate = s.getsockname()[1]
            if candidate not in exclude and candidate not in _RESERVED_PORTS and candidate > 1024:
                _RESERVED_PORTS.add(candidate)
                return candidate
        raise RuntimeError("Không tìm được cổng remote debugging còn trống.")


def release_port(port: Optional[int]) -> None:
    if not port:
        return
    with _PORT_LOCK:
        _RESERVED_PORTS.discard(int(port))


def build_webrtc_flags(profile: Profile, fp) -> List[str]:
    """Return WebRTC flags appropriate for the profile mode and proxy state."""
    if not (profile.proxy and profile.proxy.enabled and profile.proxy.host):
        return []
    mode = getattr(fp, "webrtc_mode", "disable_non_proxied")
    if mode in ("disable_non_proxied", "block"):
        return [
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--enforce-webrtc-ip-permission-check",
        ]
    return []


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


def _normalize_cookie(c: Any) -> Optional[Dict[str, Any]]:
    """Chuẩn hóa cấu trúc cookie từ nhiều nguồn (CDP, EditThisCookie, J2TEAM, v.v.)."""
    if not isinstance(c, dict):
        return None
    nc = dict(c)
    # Hỗ trợ EditThisCookie / J2TEAM: expirationDate -> expires
    if "expirationDate" in nc and "expires" not in nc:
        nc["expires"] = nc["expirationDate"]
    # Nếu expires dạng milliseconds (13 chữ số), chuyển sang seconds
    if "expires" in nc and isinstance(nc["expires"], (int, float)):
        if nc["expires"] > 1e11:
            nc["expires"] = nc["expires"] / 1000.0
    # name và value là bắt buộc và phải là chuỗi
    if "name" not in nc or "value" not in nc:
        return None
    nc["name"] = str(nc["name"])
    nc["value"] = str(nc["value"])
    # Chuẩn hóa sameSite
    if "sameSite" in nc and isinstance(nc["sameSite"], str):
        ss = nc["sameSite"].lower()
        if ss in ("no_restriction", "none"):
            nc["sameSite"] = "None"
        elif ss == "lax":
            nc["sameSite"] = "Lax"
        elif ss == "strict":
            nc["sameSite"] = "Strict"
        elif ss == "unspecified":
            nc.pop("sameSite", None)
    return nc


class BrowserLauncher:

    def __init__(self, profile_manager: ProfileManager):
        self.manager = profile_manager
        self.active_processes: Dict[str, subprocess.Popen] = {}
        self.active_ports: Dict[str, int] = {}
        self._requested_ports: Dict[str, int] = {}
        self._proxy_credentials: Dict[str, tuple] = {}
        self._runtime_store = BrowserRuntimeStore(
            os.path.join(DATA_DIR, "browser_runtime.json")
        )
        try:
            self.browser_path = find_browser_executable()
        except FileNotFoundError:
            # Keep the dashboard/API available so the user can see a useful
            # launch error and install a supported browser without a crash loop.
            self.browser_path = None
        # C12: Lock per-profile để tránh 2 luồng launch/stop cùng profile
        self._launch_locks = collections.defaultdict(threading.RLock)
        # H5: Event để CDP thread báo hiệu đã inject stealth thành công
        self._cdp_ready_events: Dict[str, threading.Event] = {}
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
        """Dọn dẹp các tiến trình Chrome do phiên làm việc này khởi chạy khi ứng dụng thoát."""
        ids = set(self.active_processes)
        ids.update(self._runtime_store.all().keys())
        for profile_id in list(ids):
            try:
                self.stop(profile_id)
            except Exception:
                pass

    def _identity_for(self, pid, user_data_dir):
        """Verify ownership for either a live Popen PID or a recovered record."""
        if not pid:
            return {"state": "absent", "port": None, "created": None}
        return read_browser_identity(pid, user_data_dir or "")

    def _clear_process(self, profile_id):
        record = self._runtime_store.get(profile_id) or {}
        requested = self._requested_ports.pop(profile_id, None) or record.get("requested_port")
        active = self.active_ports.pop(profile_id, None) or record.get("port")
        self.active_processes.pop(profile_id, None)
        self._proxy_credentials.pop(profile_id, None)
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
        profile = self.manager.get_profile(profile_id)
        if profile:
            profile.status, profile.port, profile.pid = "stopped", None, None

    def _identity(self, profile):
        from .storage import PROFILES_DATA_DIR
        return read_browser_identity(
            profile.pid, os.path.join(PROFILES_DATA_DIR, profile.id, "user_data")
        )

    def _recover_runtime(self, profile_id: str):
        """Return a persisted launch record after verifying process ownership.

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
                    self._runtime_store.upsert(
                        profile_id, created=identity.get("created")
                    )
                except Exception:
                    pass
            return record
        if identity.get("state") in ("absent", "mismatch"):
            self._clear_process(profile_id)
            return None
        return record

    def is_running(self, profile_id: str) -> bool:
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
            if identity.get("port"):
                self.active_ports[profile_id] = identity["port"]
            return True
        if identity["state"] in ("absent", "mismatch"):
            self._clear_process(profile_id)
            return False
        # Preserve persisted state on query failure; stop() will refuse to kill.
        return True

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

    async def _verify_emulation(self, port, fp):
        """Check the values a page actually sees after CDP injection.

        Returns a dict with per-check details. Verification only passes when
        all core checks pass, including timezone, WebGL and Client Hints.
        """
        import websockets
        deadline = time.monotonic() + 7
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
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone
    };
    try {
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
        out.glVendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null;
        out.glRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
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
                                == int(min(8, fp.device_memory or 8))
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
                            checks["webgl_vendor"] = value.get("glVendor") == fp.webgl_vendor
                            checks["webgl_renderer"] = value.get("glRenderer") == fp.webgl_renderer
                            # navigator.userAgentData is undefined on some
                            # non-secure contexts (notably about:blank). Do not
                            # fail verification for an API the page cannot see;
                            # on https pages the hook is still checked.
                            if value.get("chBrands") is not None:
                                checks["client_hints_brand"] = (
                                    expected_brand in (value.get("chBrands") or "")
                                )
                                checks["client_hints_platform"] = (
                                    value.get("chPlatform") == expected_ch_platform
                                )
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
    ):
        """Inject stealth into every page/worker/OOPIF target via CDP."""
        import websockets

        fp = profile.fingerprint
        stealth_js = get_stealth_script(fp)
        worker_js = get_worker_stealth_script(fp)

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
            print(f"[Launcher] Cảnh báo: Không thể lấy CDP URL trên port {port}")
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

        try:
            async with websockets.connect(
                ws_url, max_size=20 * 1024 * 1024
            ) as ws:
                msg_id = 0
                pending_responses: Dict[int, asyncio.Future] = {}

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
                    await ws.send(json.dumps(payload))
                    try:
                        res = await asyncio.wait_for(fut, timeout=5.0)
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
                        if isinstance(exc, asyncio.TimeoutError) or any(
                            marker in text for marker in expected
                        ):
                            return
                        print(
                            "[Launcher] CDP background task error: "
                            f"{type(exc).__name__}: {text}"
                        )

                    task.add_done_callback(consume)
                    return task

                is_first_tab = True

                async def configure_page(session_id, target_info):
                    nonlocal is_first_tab
                    await send("Page.enable", session_id=session_id)
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
                    # P0-04: Báo hiệu stealth đã được nạp thành công vào document
                    if profile.id in self._cdp_ready_events:
                        self._cdp_ready_events[profile.id].set()
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
                    if is_first_tab and target_url:
                        is_first_tab = False
                        await send(
                            "Page.navigate",
                            {"url": target_url},
                            session_id=session_id,
                        )

                async def configure_iframe(session_id, target_info):
                    # OOPIF: Page.addScriptToEvaluateOnNewDocument handles the
                    # current document when it is paused before scripts run.
                    await send("Page.enable", session_id=session_id)
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

                    if method == "Target.attachedToTarget":
                        target_info = params.get("targetInfo", {})
                        target_type = target_info.get("type", "")
                        attached_id = params.get("sessionId")
                        if not attached_id:
                            return
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

                async def reader_loop():
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            if not self.is_running(profile.id):
                                break
                            try:
                                await send("Browser.getVersion")
                            except Exception:
                                break
                            continue
                        except Exception:
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

                reader_task = asyncio.create_task(reader_loop())
                await send(
                    "Target.setAutoAttach",
                    {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": True,
                        "flatten": True,
                    },
                )
                await reader_task

        except Exception as e:
            print(f"[Launcher] Lỗi kết nối CDP: {e}")

    def _start_cdp_thread(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
    ):
        def run_loop():
            asyncio.run(
                self._setup_cdp_stealth(
                    port, profile, target_url, user_data_dir, proxy_auth
                )
            )

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()

    def launch(
        self, profile_id: str, open_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Khởi chạy Chromium/Chrome cho một profile cụ thể với toàn bộ cấu hình cô lập."""
        # C12: Lock per-profile để tránh 2 luồng launch cùng profile
        with self._launch_locks[profile_id]:
            profile = self.manager.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile không tồn tại"}

            if not self.browser_path or not os.path.isfile(self.browser_path):
                return {
                    "success": False,
                    "error": "Không tìm thấy Google Chrome hoặc Microsoft Edge. Hãy cài một trình duyệt Chromium được hỗ trợ rồi thử lại.",
                }

            if self.is_running(profile_id):
                # L2: Nếu profile đang chạy và có open_url, mở URL đó trong tab mới
                if open_url:
                    if not self.open_tab(profile_id, open_url):
                        return {"success": False, "error": "Chrome đang chạy nhưng không thể mở tab mới."}
                    return {
                        "success": True,
                        "message": "Profile đang chạy, đã mở trong tab mới",
                        "port": self.active_ports.get(profile_id),
                    }
                return {
                    "success": True,
                    "message": "Profile đang chạy",
                    "port": self.active_ports.get(profile_id),
                }

            profile_dir = self.manager.get_profile_dir(profile_id)
            user_data_dir = os.path.join(profile_dir, "user_data")
            os.makedirs(user_data_dir, exist_ok=True)

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
            fp = normalize_fingerprint_for_launch(profile.fingerprint, self.browser_path)

            # Cấu hình danh sách flags dòng lệnh
            args = [
                self.browser_path,
                f"--user-data-dir={user_data_dir}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--window-size={fp.screen_width},{fp.screen_height}",
                f"--lang={fp.locale}",
                f"--accept-lang={','.join(fp.languages or [fp.locale])}",
                # H3: Đồng bộ DPR giữa JS và CSS media queries
                f"--force-device-scale-factor={fp.device_pixel_ratio or 1.0}",
                "--high-dpi-support=1",
                "--no-first-run",
                "--no-default-browser-check",
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

                    # Chrome 137+ removed --load-extension for branded builds.
                    # Use CDP Fetch.authRequired instead. Older builds keep the
                    # extension path because their Fetch domain is less reliable.
                    browser_ver = get_version_from_browser_path(self.browser_path)
                    major = 0
                    if browser_ver:
                        try:
                            major = int(browser_ver[1].split(".", 1)[0])
                        except Exception:
                            major = 0
                    if major >= 137:
                        print(
                            "[Launcher] Chrome >=137: xác thực proxy qua CDP "
                            f"({profile.proxy.host}:{profile.proxy.port}, {profile.proxy.protocol})."
                        )
                        if profile.proxy.protocol in ("socks5", "socks4"):
                            print(
                                "[Launcher] Lưu ý: Chrome có thể không hỗ trợ xác thực "
                                "SOCKS5; nên dùng HTTP/HTTPS proxy nếu proxy yêu cầu auth."
                            )
                    else:
                        ext_dir = create_proxy_auth_extension(profile.proxy, profile_dir)
                        if ext_dir:
                            args.append(f"--load-extension={ext_dir}")
                        elif profile.proxy.protocol in ("socks5", "socks4"):
                            print(
                                "[Launcher] Cảnh báo: Chrome cũ không hỗ trợ xác thực "
                                "SOCKS5 qua extension. Hãy dùng proxy HTTP/HTTPS có auth."
                            )

            # Mở trang trắng theo mặc định; chỉ điều hướng ra ngoài khi người dùng chỉ định URL.
            target_url = open_url or "about:blank"
            args.append("about:blank")

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
                if proxy_auth:
                    self._proxy_credentials[profile_id] = proxy_auth

                # Persist ownership outside profiles.json so a server restart
                # or `cli.py stop-all` can still find and stop this browser.
                try:
                    self._runtime_store.upsert(
                        profile_id,
                        pid=proc.pid,
                        port=port,
                        requested_port=port,
                        user_data_dir=user_data_dir,
                        exe=self.browser_path,
                        started_at=int(time.time()),
                    )
                except Exception as e:
                    print(f"[Launcher] Không ghi được runtime state: {type(e).__name__}: {e}")

                # Cập nhật trạng thái profile trong bộ nhớ (không ghi đè profiles.json khi launch)
                profile.status = "running"
                profile.port = port
                profile.pid = proc.pid

                # P0-04: Tạo event để CDP thread báo hiệu stealth script đã được nạp
                cdp_ready = threading.Event()
                self._cdp_ready_events[profile_id] = cdp_ready

                # Khởi chạy luồng gắn kết CDP để tiêm script và điều hướng
                self._start_cdp_thread(
                    port, profile, target_url, user_data_dir, proxy_auth
                )

                # Chờ CDP tiêm stealth thành công vào trang (tối đa 6.0 giây)
                stealth_ok = cdp_ready.wait(timeout=6.0)

                # Chrome may choose a different port than requested; the CDP
                # thread updates active_ports from DevToolsActivePort.
                actual_port = self.active_ports.get(profile_id, port)
                page_ready = _safe_run_async(self._probe_page_ready(actual_port))
                verification = {"ok": False, "checks": {}, "actual": None}
                if page_ready and stealth_ok:
                    verification = _safe_run_async(
                        self._verify_emulation(actual_port, fp)
                    )
                configuration_verified = bool(verification.get("ok"))

                self._cdp_ready_events.pop(profile_id, None)

                if not stealth_ok:
                    print(f"[Launcher] CDP stealth injection failed or timed out for {profile_id}")

                if proc.poll() is not None:
                    self.is_running(profile_id)
                    return {
                        "success": False,
                        "error": "Chrome đã thoát trong lúc khởi chạy profile.",
                        "cdp_ready": False,
                        "stealth_injected": False,
                    }

                cdp_ready_final = stealth_ok and configuration_verified
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
                    "verification": verification,
                    "warning": (
                        None
                        if cdp_ready_final
                        else "Chrome is running but stealth injection or emulation verification was not confirmed."
                    ),
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
                print(f"[Launcher] Lỗi khởi chạy: {type(e).__name__}: {e}")
                return {
                    "success": False,
                    "error": "Lỗi khởi chạy trình duyệt. Vui lòng thử lại.",
                }

    def stop(self, profile_id: str) -> Dict[str, Any]:
        """Stop only an owned browser; never clear state until exit is confirmed."""
        with self._launch_locks[profile_id]:
            profile = self.manager.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile không tồn tại"}
            proc = self.active_processes.get(profile_id)
            if proc and proc.poll() is not None:
                self._clear_process(profile_id)
                return {"success": True, "message": "Trình duyệt đã đóng"}

            record = self._runtime_store.get(profile_id) or {}
            pid = proc.pid if proc else (record.get("pid") or profile.pid)
            if not pid:
                self._clear_process(profile_id)
                return {"success": True, "message": "Profile đã dừng"}
            user_data_dir = record.get("user_data_dir") or os.path.join(
                PROFILES_DATA_DIR, profile.id, "user_data"
            )
            identity = None
            if not proc:
                identity = self._identity_for(pid, user_data_dir)
                if identity["state"] in ("absent", "mismatch"):
                    self._clear_process(profile_id)
                    return {"success": True, "message": "Phiên cũ đã kết thúc"}
                if identity["state"] != "owned":
                    return {"success": False, "error": "Chưa xác minh được tiến trình của profile; không gửi lệnh tắt."}
            try:
                if sys.platform == "win32":
                    # First request normal closure; always continue to /F when
                    # graceful taskkill fails or the process does not exit. The
                    # previous early-return left Chrome and the profile lock alive.
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    last_error = ""
                    for force in (False, True):
                        if not proc:
                            current = self._identity_for(pid, user_data_dir)
                            if current["state"] in ("absent", "mismatch"):
                                break
                            if current["state"] != "owned" or current.get("created") != identity.get("created"):
                                return {"success": False, "error": "Tiến trình đã thay đổi; giữ nguyên trạng thái để kiểm tra."}
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
                    if not (proc.poll() is not None if proc else self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch")):
                        detail = f" ({last_error})" if last_error else ""
                        return {"success": False, "error": f"Windows chưa dừng được trình duyệt{detail}. Dữ liệu trạng thái được giữ nguyên."}
                elif proc:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                else:
                    return {"success": False, "error": "Không xác minh được quyền sở hữu tiến trình."}
                exited = proc.poll() is not None if proc else self._identity_for(pid, user_data_dir)["state"] in ("absent", "mismatch")
                if not exited:
                    return {"success": False, "error": "Trình duyệt vẫn đang chạy; chưa thể báo đã dừng."}
            except (OSError, subprocess.SubprocessError):
                return {"success": False, "error": "Không thể dừng trình duyệt; trạng thái được giữ nguyên."}
            self._clear_process(profile_id)
            return {"success": True, "message": "Đã xác nhận trình duyệt dừng"}

    def stop_all(self):
        ids = set(self.active_processes)
        ids.update(self._runtime_store.all().keys())
        ids.update(p.id for p in self.manager.list_profiles() if p.pid or p.status == "running")
        results = {pid: self.stop(pid) for pid in ids}
        failed = [pid for pid, result in results.items() if not result.get("success")]
        return {"success": not failed, "failed_profile_ids": failed, "results": results,
                "message": "Đã dừng toàn bộ profile" if not failed else "Một số profile chưa dừng được."}

    def open_tab(self, profile_id: str, url: str) -> bool:
        """Mở một tab mới trên profile đang chạy thông qua CDP HTTP endpoint (L2)."""
        port = self.active_ports.get(profile_id)
        if not port:
            self._recover_runtime(profile_id)
            port = self.active_ports.get(profile_id)
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
                print(f"[Launcher] Lỗi mở tab mới: {e}")
                return False

    async def _fetch_cookies_cdp(self, port: int) -> List[Dict[str, Any]]:
        """Lấy danh sách cookies thông qua CDP Storage.getCookies."""
        import websockets
        url = f"http://127.0.0.1:{port}/json/version"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ws_url = data.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("Chrome không cung cấp endpoint CDP.")
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"id": 999, "method": "Storage.getCookies"}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if message.get("id") != 999:
                    continue
                if "error" in message:
                    raise RuntimeError("CDP từ chối yêu cầu đọc cookies.")
                return message.get("result", {}).get("cookies", [])

    def export_cookies(self, profile_id: str) -> List[Dict[str, Any]]:
        """Đọc cookies trực tiếp từ profile đang chạy, không tạo bản sao JSON trên đĩa."""
        running = self.is_running(profile_id)
        port = self.active_ports.get(profile_id)
        if not port or not running:
            raise RuntimeError("Hãy mở profile trước khi đọc cookies.")
        try:
            return _safe_run_async(self._fetch_cookies_cdp(port))
        except Exception as e:
            print(f"[Launcher] Không thể đọc cookies qua CDP: {type(e).__name__}")
            raise RuntimeError("Không thể đọc cookies từ profile đang chạy.") from e

    def import_cookies(self, profile_id: str, cookies: List[Dict[str, Any]]) -> bool:
        """Nạp cookies trực tiếp vào profile đang chạy qua CDP Storage.setCookies."""
        running = self.is_running(profile_id)
        port = self.active_ports.get(profile_id)
        if not port or not running:
            return False
        try:
            # Chuẩn hóa danh sách cookies
            normalized_cookies = []
            for c in cookies:
                nc = _normalize_cookie(c)
                if nc:
                    normalized_cookies.append(nc)

            if not normalized_cookies:
                return False

            async def _inject():
                import websockets
                url = f"http://127.0.0.1:{port}/json/version"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    ws_url = json.loads(resp.read().decode("utf-8")).get("webSocketDebuggerUrl")
                if not ws_url:
                    raise RuntimeError("Chrome không cung cấp endpoint CDP.")
                async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({
                        "id": 888,
                        "method": "Storage.setCookies",
                        "params": {"cookies": normalized_cookies}
                    }))
                    while True:
                        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                        if message.get("id") != 888:
                            continue
                        if "error" in message:
                            print(f"[Launcher] Lỗi CDP Storage.setCookies: {message['error']}")
                            return False
                        return True

            return _safe_run_async(_inject())
        except Exception as e:
            print(f"[Launcher] Không thể nạp cookies qua CDP: {type(e).__name__} {e}")
            return False

    def launch_all(
        self,
        delay: float = 0.8,
        open_url: Optional[str] = None,
        max_concurrent: int = 8,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Start every stopped profile, queueing when max_concurrent is reached."""
        profiles = self.manager.list_profiles()
        results = []
        limit = max(0, int(max_concurrent or 0))

        def live_process_count() -> int:
            count = 0
            for pid, proc in list(self.active_processes.items()):
                if proc.poll() is None:
                    count += 1
                else:
                    self._clear_process(pid)
            return count

        for p in profiles:
            if cancel_event and cancel_event.is_set():
                results.append({
                    "profile_id": p.id,
                    "name": p.name,
                    "result": {"success": False, "error": "Đã hủy tác vụ bởi người dùng"}
                })
                continue

            if self.is_running(p.id):
                item = {
                    "profile_id": p.id,
                    "name": p.name,
                    "result": {"success": True, "message": "Đang chạy"},
                }
                results.append(item)
                if progress_callback:
                    try:
                        progress_callback(p.id, item["result"])
                    except Exception:
                        pass
                continue

            if limit:
                waited = 0.0
                while live_process_count() >= limit:
                    if cancel_event and cancel_event.is_set():
                        break
                    time.sleep(0.5)
                    waited += 0.5
                    if waited % 15 == 0:
                        print(
                            f"[Launcher] Đang chờ slot trống để mở {p.name} "
                            f"(giới hạn {limit} profile đồng thời)."
                        )

            if cancel_event and cancel_event.is_set():
                results.append({
                    "profile_id": p.id,
                    "name": p.name,
                    "result": {"success": False, "error": "Đã hủy tác vụ bởi người dùng"}
                })
                break

            res = self.launch(p.id, open_url=open_url)
            item = {"profile_id": p.id, "name": p.name, "result": res}
            results.append(item)
            if progress_callback:
                try:
                    progress_callback(p.id, res)
                except Exception:
                    pass

            if delay > 0:
                time.sleep(delay)
        return results
