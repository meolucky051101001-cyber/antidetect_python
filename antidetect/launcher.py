import os
import re
import sys
import json
import time
import datetime
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
from .stealth import get_stealth_script, get_worker_stealth_script, get_webgl_limits
from .proxy import create_proxy_auth_extension
from .process_identity import read_browser_identity
from .fingerprint import (
    normalize_fingerprint_for_launch,
    get_version_from_browser_path,
    get_host_screen_info,
    host_os,
)

# Browser pin (Giai đoạn 2): chrome-for-testing tải về nằm ở đây, ngoài data/.
BROWSERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browsers"
)


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
        # Chrome 155 no longer honours the force switch on its own (the
        # preference is parsed, but STUN still leaves through the host's UDP
        # path).  The pref-style switch below is what actually blocks
        # non-proxied UDP; keep both for older Chromium builds.
        return [
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
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


# Fanpage listing: mở tab facebook.com/bookmarks/pages TRONG phiên profile
# (session đã đăng nhập), đọc danh sách page từ DOM rồi đóng tab. Facebook
# thay đổi DOM thường xuyên nên scraper chỉ dùng heuristic link + aria-label
# và trả về kèm URL để người dùng kiểm tra bằng mắt khi cần.
_FB_SCRAPE_JS = r"""
(async () => {
    const SCRAPE = () => {
        const out = {
            url: location.href,
            readyState: document.readyState,
            loggedOut: false,
            pages: []
        };
        try {
            out.loggedOut = location.pathname.startsWith('/login')
                || location.pathname === '/login.php'
                || location.search.indexOf('login') >= 0
                || !!document.querySelector('form[action*="login"]');
        } catch (e) {}
        if (out.loggedOut) return out;

        const BLOCKED = new Set([
            'feed', 'friends', 'watch', 'marketplace', 'groups', 'gaming',
            'events', 'messages', 'notifications', 'search', 'bookmarks',
            'pages', 'settings', 'help', 'privacy', 'policies', 'legal',
            'about', 'ads', 'adpreferences', 'business', 'careers',
            'developer', 'login', 'signup', 'recover', 'reg', 'save',
            'stories', 'reels', 'photo', 'video', 'videos', 'notes',
            'ondevice', 'portal', 'oculus', 'shop', 'pay', 'wallet', 'games',
            'ajax', 'api', 'graphql', 'dialog', 'share', 'sharer', 'hashtag',
            'people', 'watchparty', 'mcnut', 'metaverse', 'terms'
        ]);
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        for (const a of anchors) {
            let text = (a.getAttribute('aria-label') || a.textContent || '')
                .trim().replace(/\s+/g, ' ');
            if (!text || text.length < 2 || text.length > 100) continue;
            const href = a.href || '';
            if (!href) continue;
            let u;
            try { u = new URL(href, location.origin); } catch (e) { continue; }
            const host = (u.hostname || '').replace(/^www\./, '');
            if (host !== 'facebook.com' && host !== 'm.facebook.com'
                && host !== 'web.facebook.com') continue;
            const path = u.pathname.replace(/\/+$/, '');
            if (!path || path === '/') continue;
            let id = null;
            let score = 0;
            const mPages = path.match(/^\/pages\/[^/]+\/(\d{5,})/);
            if (mPages) { id = mPages[1]; score = 2; }
            if (path === '/profile.php') {
                const qid = u.searchParams.get('id');
                if (qid && /^\d{5,}$/.test(qid)) { id = qid; score = 2; }
                else continue;
            }
            if (!id) {
                const segs = path.split('/').filter(Boolean);
                if (segs.length === 1) {
                    const seg = segs[0].toLowerCase();
                    if (BLOCKED.has(seg)) continue;
                    if (segs[0].length < 2 || segs[0].length > 60) continue;
                    if (u.search && !u.search.startsWith('?sk=')
                        && !u.search.startsWith('?ref=')
                        && !u.search.startsWith('?id=')) continue;
                    score = 1;
                } else {
                    continue;
                }
            }
            out.pages.push({
                name: text,
                url: u.origin + path + (id ? '' : (u.search || '')),
                id: id,
                _score: score
            });
        }
        const seen = new Map();
        for (const p of out.pages) {
            const key = p.id || p.url;
            const prev = seen.get(key);
            if (!prev || p._score > prev._score
                || (p._score === prev._score && p.name.length > prev.name.length)) {
                seen.set(key, p);
            }
        }
        out.pages = Array.from(seen.values())
            .sort((a, b) => b._score - a._score)
            .slice(0, 100)
            .map(p => ({ name: p.name, url: p.url, id: p.id }));
        return out;
    };

    const t0 = Date.now();
    while (Date.now() - t0 < 12000) {
        const r = SCRAPE();
        if (r.loggedOut || r.pages.length > 0) return r;
        await new Promise(res => setTimeout(res, 500));
    }
    return SCRAPE();
})()
"""


# Đồng bộ chi tiết từng fanpage (tuổi + hoạt động): chạy trên chính trang
# page/about, thu (1) các mốc thời gian "X ngày trước" của bài đăng để suy ra
# bài gần nhất, và (2) đoạn text quanh nhãn "Ngày tạo trang"/"Page created"
# để suy ra ngày tạo. Facebook đổi DOM thường xuyên — mọi giá trị đều giữ
# raw text để hiển thị lại khi parse thất bại.
_FB_PAGE_METRICS_JS = r"""
(async () => {
    const RXS = [
        [/(\d+)\s+giây\s+trước/i, 's'], [/(\d+)\s+phút\s+trước/i, 'm'],
        [/(\d+)\s+giờ\s+trước/i, 'h'], [/(\d+)\s+ngày\s+trước/i, 'd'],
        [/(\d+)\s+tuần\s+trước/i, 'w'], [/(\d+)\s+tháng\s+trước/i, 'mo'],
        [/(\d+)\s+năm\s+trước/i, 'y'],
        [/(\d+)\s+seconds?\s+ago/i, 's'], [/(\d+)\s+minutes?\s+ago/i, 'm'],
        [/(\d+)\s+hours?\s+ago/i, 'h'], [/(\d+)\s+days?\s+ago/i, 'd'],
        [/(\d+)\s+weeks?\s+ago/i, 'w'], [/(\d+)\s+months?\s+ago/i, 'mo'],
        [/(\d+)\s+years?\s+ago/i, 'y'],
    ];
    const CREATED_LABEL = /(ngày\s+tạo(?:\s+trang)?|page\s+created|created\s+on|date\s+created|được\s+tạo|phát\s+hành)/i;

    const scrape = () => {
        const out = {
            url: location.href,
            readyState: document.readyState,
            title: (document.title || '').slice(0, 120),
            times: [],
            createdRaw: null
        };
        const push = (n, u) => {
            if (out.times.length < 50) out.times.push({ n: parseInt(n, 10), u: u });
        };
        const seen = new Set();
        try {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node, count = 0;
            while ((node = walker.nextNode()) && count < 4000) {
                const t = (node.textContent || '').trim();
                if (!t || t.length > 48 || seen.has(t)) continue;
                count += 1; seen.add(t);
                let matched = false;
                for (const [rx, u] of RXS) {
                    const m = t.match(rx);
                    if (m) { push(m[1], u); matched = true; break; }
                }
                if (!matched) {
                    const ms = t.match(/^(\d+)([smhdw])$/i);
                    if (ms) push(ms[1], ms[2].toLowerCase());
                }
            }
        } catch (e) {}
        try {
            const txt = document.body ? (document.body.innerText || '') : '';
            const m = txt.match(new RegExp(CREATED_LABEL.source + '[^\\n]{0,100}', 'i'));
            if (m) out.createdRaw = m[0].slice(0, 140);
        } catch (e) {}
        return out;
    };

    const t0 = Date.now();
    while (Date.now() - t0 < 6000) {
        const r = scrape();
        if ((r.times.length > 0 && r.readyState === 'complete') || r.createdRaw) {
            return r;
        }
        await new Promise(res => setTimeout(res, 500));
    }
    return scrape();
})()
"""

_FB_UNIT_DAYS = {"s": 0, "m": 0, "h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}

_FB_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def _fb_unit_to_days(num: int, unit: str) -> int:
    return int(num) * _FB_UNIT_DAYS.get(str(unit).lower(), 0)


def _parse_fb_created_date(raw: Optional[str]) -> Optional[datetime.date]:
    """Best-effort parse ngày tạo trang từ đoạn text quanh nhãn của Facebook.

    Hỗ trợ các format thường gặp: dd/mm/yyyy (và mm/dd/yyyy khi rõ ràng),
    "ngày D tháng M, YYYY", "March 5, 2020", ISO YYYY-MM-DD. Trả None khi
    không khớp — caller hiển thị raw text thay vì đoán.
    """
    if not raw:
        return None
    text = raw.lower()
    try:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if m:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})\s+tháng\s+(\d{1,2}),?\s*(\d{4})", text)
        if m:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = re.search(
            r"(" + "|".join(_FB_MONTHS_EN) + r")\s+(\d{1,2}),?\s+(\d{4})", text
        )
        if m:
            return datetime.date(int(m.group(3)), _FB_MONTHS_EN[m.group(1)], int(m.group(2)))
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", text)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12 >= b:
                return datetime.date(y, b, a)  # chắc chắn dd/mm
            if b > 12 >= a:
                return datetime.date(y, a, b)  # chắc chắn mm/dd
            # Ambiguous: locale VN ưu tiên dd/mm.
            return datetime.date(y, b, a)
    except ValueError:
        return None
    return None


def classify_facebook_page(entry: Dict[str, Any], today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Quy tắc quản lý farm fanpage.

    - Trạng thái đăng bài: bài gần nhất trong 7 ngày → "active" (đang đăng),
      lâu hơn hoặc không có bài → "stopped"; chưa từng quét chi tiết → None.
    - "Sẵn sàng": page đã ngâm đủ 30 ngày tuổi (tính từ ngày tạo).
    """
    today = today or datetime.date.today()
    age_days = None
    created = entry.get("created_date")
    if created:
        try:
            age_days = (today - datetime.date.fromisoformat(str(created))).days
        except ValueError:
            age_days = None

    last_days = entry.get("last_post_days")
    if last_days is None:
        status = "unknown" if entry.get("detail_synced") else None
    else:
        status = "active" if last_days <= 7 else "stopped"

    return {
        "age_days": age_days,
        "status": status,
        "ready": age_days is not None and age_days >= 30,
    }


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
                            checks["webgl_vendor"] = value.get("glVendor") == fp.webgl_vendor
                            checks["webgl_renderer"] = value.get("glRenderer") == fp.webgl_renderer
                            checks["css_device_width"] = value.get("cssDeviceW") is True
                            checks["css_device_height"] = value.get("cssDeviceH") is True
                            checks["css_device_pixel_ratio"] = value.get("cssDpr") is True
                            if expected_texture is not None:
                                checks["webgl_max_texture"] = (
                                    int(value.get("glMaxTexture") or 0) == expected_texture
                                )
                            if expected_viewport is not None:
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
    ):
        """Inject stealth into every page/worker/OOPIF target via CDP."""
        import websockets

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

        if native_brands:
            ua_metadata["brands"] = native_brands["brands"]
            if native_brands.get("fullVersionList"):
                ua_metadata["fullVersionList"] = native_brands["fullVersionList"]

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
                        print(
                            "[Launcher] Không đặt được screen emulation: "
                            f"{type(exc).__name__}: {exc}"
                        )

                async def configure_page(session_id, target_info):
                    nonlocal is_first_tab
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
                        core_event = self._core_verified_events.get(profile.id)
                        if core_event is not None:
                            await asyncio.to_thread(core_event.wait, 8.0)
                        if target_url == "about:blank":
                            nav_event = self._navigation_events.get(profile.id)
                            if nav_event is not None:
                                nav_event.set()
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
                        return

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
                            if not self.is_running(profile.id):
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

                async def keepalive_loop():
                    while True:
                        await asyncio.sleep(20.0)
                        if not self.is_running(profile.id):
                            return
                        try:
                            await send("Browser.getVersion")
                        except Exception:
                            return

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
                    await reader_task
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            print(f"[Launcher] Lỗi kết nối CDP: {e}")

    def _start_cdp_thread(
        self,
        port: int,
        profile: Profile,
        target_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        proxy_auth: Optional[tuple] = None,
        fp_override: Optional[Any] = None,
    ):
        def run_loop():
            asyncio.run(
                self._setup_cdp_stealth(
                    port, profile, target_url, user_data_dir, proxy_auth, fp_override
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
                        "Hãy chạy: python tools\install_browser.py --version <ver>, "
                        "hoặc bỏ pin browser trong Edit profile."
                    )
                else:
                    error = "Không tìm thấy Google Chrome hoặc Microsoft Edge. Hãy cài một trình duyệt Chromium được hỗ trợ rồi thử lại."
                return {"success": False, "error": error}

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
                    browser_ver = get_version_from_browser_path(browser_path)
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
                        exe=browser_path,
                        started_at=int(time.time()),
                    )
                except Exception as e:
                    print(f"[Launcher] Không ghi được runtime state: {type(e).__name__}: {e}")

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
                    port, profile, target_url, user_data_dir, proxy_auth, fp
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
                warning_parts = list(launch_warnings)
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
            identity = None
            if not proc:
                identity = self._identity_for_stop(pid, user_data_dir)
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
                            current = self._identity_for_stop(pid, user_data_dir)
                            if current["state"] in ("absent", "mismatch"):
                                break
                            if current["state"] == "unknown":
                                return {"success": False, "error": "Chưa xác minh lại được tiến trình; giữ nguyên trạng thái để kiểm tra."}
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

    def fetch_facebook_pages(
        self, profile_id: str, timeout: float = 40.0, deep: bool = False
    ) -> Dict[str, Any]:
        """Liệt kê fanpage profile này sở hữu/quản lý, đọc trực tiếp từ phiên đang chạy.

        Mở tab facebook.com/bookmarks/pages bằng session đã đăng nhập của
        profile (tab có stealth như mọi tab khác), đọc DOM, rồi đóng tab.
        ``deep=True``: sau danh sách, quét thêm TỪNG page (mở tuần tự trên
        cùng một tab) để lấy ngày tạo + thời gian bài đăng gần nhất và gắn
        trạng thái theo ``classify_facebook_page``.

        Trả về {"logged_in": bool, "pages": [...], "url": str}.
        """
        running = self.is_running(profile_id)
        port = self.active_ports.get(profile_id)
        if not port or not running:
            raise RuntimeError("Hãy mở profile trước khi đọc fanpage.")
        data = _safe_run_async(self._facebook_pages_cdp(port, timeout))
        if deep and data.get("logged_in") and data.get("pages"):
            per_page = 8.0
            deep_timeout = min(300.0, 20.0 + per_page * len(data["pages"]))
            try:
                details = _safe_run_async(
                    self._facebook_pages_deep_cdp(
                        port, data["pages"], timeout=deep_timeout
                    )
                )
            except Exception:
                details = {}
            now_iso = datetime.date.today().isoformat()
            for page in data["pages"]:
                info = details.get(page.get("url")) or {}
                page["created_raw"] = info.get("created_raw")
                page["last_post_raw"] = info.get("last_post_raw")
                # Entry rỗng vẫn có key "times"/"created_raw" nên bool(info)
                # luôn true; chỉ đánh dấu đã quét khi thực sự đọc được dữ liệu.
                page["detail_synced"] = bool(
                    info.get("times") or info.get("created_raw")
                )
                created = _parse_fb_created_date(page.get("created_raw"))
                if created:
                    page["created_date"] = created.isoformat()
                times = info.get("times") or []
                if times:
                    best = min(
                        times, key=lambda t: _fb_unit_to_days(t["n"], t["u"])
                    )
                    page["last_post_days"] = _fb_unit_to_days(best["n"], best["u"])
                    if not page.get("last_post_raw"):
                        page["last_post_raw"] = f"{best['n']} {best['u']}"
                page.update(classify_facebook_page(page))
                page["detail_synced_at"] = now_iso
        return data

    async def _facebook_pages_cdp(self, port: int, timeout: float = 40.0) -> Dict[str, Any]:
        import websockets

        def _http_json(path, method="GET"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", method=method
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {}

        from urllib.parse import quote

        new_url = f"/json/new?{quote('https://www.facebook.com/bookmarks/pages', safe='')}"
        try:
            try:
                info = await asyncio.to_thread(_http_json, new_url, "PUT")
            except Exception:
                info = await asyncio.to_thread(_http_json, new_url, "GET")
            target_id = (info or {}).get("id")
        except Exception as e:
            raise RuntimeError(
                f"Không mở được tab Facebook trong profile: {type(e).__name__}: {e}"
            ) from e
        if not target_id:
            raise RuntimeError("Chrome không trả về tab mới.")

        try:
            # Đợi tab navigate tới facebook.com (auto-attach sẽ configure
            # stealth rồi resume tab như mọi tab user mở).
            ws_url = None
            deadline = time.monotonic() + min(timeout, 15.0)
            while time.monotonic() < deadline and not ws_url:
                try:
                    targets = await asyncio.to_thread(_http_json, "/json/list")
                    for t in targets or []:
                        if t.get("id") != target_id:
                            continue
                        url = (t.get("url") or "").lower()
                        if "facebook.com" in url and t.get("webSocketDebuggerUrl"):
                            ws_url = t["webSocketDebuggerUrl"]
                            break
                except Exception:
                    pass
                if not ws_url:
                    await asyncio.sleep(0.4)
            if not ws_url:
                raise RuntimeError(
                    "Tab Facebook không tải được trong thời gian chờ."
                )

            async with websockets.connect(
                ws_url, max_size=10 * 1024 * 1024, open_timeout=3
            ) as ws:
                # Tab vừa mở: execution context có thể chưa sẵn sàng khi
                # evaluate đầu tiên gửi đi (CDP trả protocol error). Retry
                # cho tới khi trang trả được dict dữ liệu.
                eval_deadline = time.monotonic() + min(timeout, 30.0)
                attempt = 0
                while time.monotonic() < eval_deadline and attempt < 5:
                    attempt += 1
                    await ws.send(json.dumps({
                        "id": 60001,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": _FB_SCRAPE_JS,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    }))
                    resp_deadline = min(
                        eval_deadline, time.monotonic() + 12.0
                    )
                    got_value = None
                    context_not_ready = False
                    while time.monotonic() < resp_deadline:
                        msg = json.loads(await asyncio.wait_for(
                            ws.recv(),
                            timeout=max(0.2, resp_deadline - time.monotonic()),
                        ))
                        if msg.get("id") != 60001:
                            continue
                        err = msg.get("error")
                        if err:
                            text = str(err.get("message") or "")
                            if "context" in text.lower() or "Navigation" in text:
                                context_not_ready = True
                            else:
                                raise RuntimeError(
                                    f"CDP từ chối đọc tab Facebook: {text}"
                                )
                            break
                        if (msg.get("result") or {}).get("exceptionDetails"):
                            raise RuntimeError(
                                "Lỗi đọc trang Facebook trong tab profile."
                            )
                        got_value = (
                            (msg.get("result") or {}).get("result") or {}
                        ).get("value")
                        break
                    if isinstance(got_value, dict):
                        got_value["logged_in"] = not got_value.get("loggedOut")
                        return got_value
                    if not context_not_ready:
                        await asyncio.sleep(0.6)
                    else:
                        await asyncio.sleep(0.8)
                raise RuntimeError("Đọc trang Facebook quá thời gian chờ.")
        finally:
            # Tab này do tool mở — luôn đóng lại, kể cả khi scrape lỗi.
            try:
                await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "PUT")
            except Exception:
                try:
                    await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "GET")
                except Exception:
                    pass

    async def _facebook_pages_deep_cdp(
        self, port: int, pages: List[Dict[str, Any]], timeout: float = 120.0
    ) -> Dict[str, Dict[str, Any]]:
        """Quét chi tiết từng page: bài gần nhất (feed) + ngày tạo (about).

        Dùng MỘT tab duyệt tuần tự các page thay vì mở N tab — mỗi page tốn
        ~4-8s (2 lần navigate). Trả {page_url: {"times", "created_raw",
        "last_post_raw", "title"}}. Trang nào lỗi vẫn có entry rỗng để UI
        hiển thị "không rõ" thay vì treo.
        """
        import websockets

        def _http_json(path, method="GET"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", method=method
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {}

        from urllib.parse import quote

        try:
            try:
                info = await asyncio.to_thread(_http_json, "/json/new?about%3Ablank", "PUT")
            except Exception:
                info = await asyncio.to_thread(_http_json, "/json/new?about%3Ablank", "GET")
            target_id = (info or {}).get("id")
        except Exception as e:
            raise RuntimeError(
                f"Không mở được tab quét chi tiết: {type(e).__name__}: {e}"
            ) from e
        if not target_id:
            raise RuntimeError("Chrome không trả về tab mới.")

        results: Dict[str, Dict[str, Any]] = {}
        try:
            ws_url = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not ws_url:
                try:
                    targets = await asyncio.to_thread(_http_json, "/json/list")
                    for t in targets or []:
                        if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                            ws_url = t["webSocketDebuggerUrl"]
                            break
                except Exception:
                    pass
                if not ws_url:
                    await asyncio.sleep(0.3)
            if not ws_url:
                raise RuntimeError("Tab quét chi tiết không kết nối được.")

            async with websockets.connect(ws_url, open_timeout=3) as ws:
                msg_id = 61000

                async def _evaluate(expression: str, deadline_s: float = 9.0):
                    """Navigate-ready evaluate với retry khi context chưa sẵn."""
                    nonlocal msg_id
                    end = time.monotonic() + deadline_s
                    attempt = 0
                    while time.monotonic() < end and attempt < 6:
                        attempt += 1
                        msg_id += 1
                        my_id = msg_id
                        await ws.send(json.dumps({
                            "id": my_id,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expression,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }))
                        resp_deadline = min(end, time.monotonic() + 7.0)
                        got = None
                        not_ready = False
                        while time.monotonic() < resp_deadline:
                            msg = json.loads(await asyncio.wait_for(
                                ws.recv(),
                                timeout=max(0.2, resp_deadline - time.monotonic()),
                            ))
                            if msg.get("id") != my_id:
                                continue
                            err = msg.get("error")
                            if err:
                                text = str(err.get("message") or "")
                                if "context" in text.lower() or "Navigation" in text:
                                    not_ready = True
                                break
                            value = ((msg.get("result") or {}).get("result") or {}).get("value")
                            got = value if isinstance(value, dict) else None
                            break
                        if got is not None:
                            return got
                        await asyncio.sleep(0.7 if not_ready else 0.5)
                    return None

                for page in pages:
                    page_url = (page.get("url") or "").strip()
                    entry: Dict[str, Any] = {
                        "times": [], "created_raw": None, "last_post_raw": None,
                    }
                    if page_url:
                        # 1) Trang chính page: mốc thời gian bài đăng (feed).
                        msg_id += 1
                        await ws.send(json.dumps({
                            "id": msg_id,
                            "method": "Page.navigate",
                            "params": {"url": page_url},
                        }))
                        main_metrics = await _evaluate(_FB_PAGE_METRICS_JS)
                        if main_metrics:
                            entry["times"] = main_metrics.get("times") or []
                            entry["title"] = main_metrics.get("title")
                        # 2) Trang about: nhãn ngày tạo trang.
                        about_url = page_url.rstrip("/") + "/about"
                        msg_id += 1
                        await ws.send(json.dumps({
                            "id": msg_id,
                            "method": "Page.navigate",
                            "params": {"url": about_url},
                        }))
                        about_metrics = await _evaluate(_FB_PAGE_METRICS_JS)
                        if about_metrics and about_metrics.get("createdRaw"):
                            entry["created_raw"] = about_metrics["createdRaw"]
                        elif main_metrics and main_metrics.get("createdRaw"):
                            entry["created_raw"] = main_metrics["createdRaw"]
                    results[page_url] = entry
            return results
        finally:
            try:
                await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "PUT")
            except Exception:
                try:
                    await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "GET")
                except Exception:
                    pass

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
                        "result": {"success": True, "message": "Đang chạy"},
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
