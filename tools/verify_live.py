"""Live verification of a launched antidetect profile (real Chrome + CDP).

This is the Phase D evidence harness. Unlike ``audit_fingerprint.py`` it does
not inject the payload into a Playwright context: it starts a real profile via
``BrowserLauncher`` and inspects the same browser a user would use.

Checks:
  * JS screen values and CSS device-width/height/resolution agree.
  * navigator.deviceMemory equals the Sec-CH-Device-Memory request header.
  * WebGL limits match this machine's clean Chrome baseline.
  * Canvas follows its explicit revision: v1 complexity gate, v2 coordinate
    readout, or native when disabled. V2 native GPU values are observations.
  * Worker timezone/language/UA are overridden (not host values).
  * WebRTC constructors are untouched and no local IP appears in SDP.

Example:
    $env:ANTIDETECT_DATA_DIR = "$env:TEMP\\antidetect-verify"
    python tools/verify_live.py --profile <id> --json verify.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:  # script mode and ``python -m tools.verify_live``
    from . import audit_common as common
except ImportError:  # pragma: no cover - normal CLI path
    import audit_common as common

from antidetect.fingerprint import normalize_fingerprint_for_launch  # noqa: E402
from antidetect.launcher import BrowserLauncher, find_browser_executable  # noqa: E402
from antidetect.storage import DATA_DIR, ProfileManager  # noqa: E402
from antidetect.stealth import (  # noqa: E402
    _battery_profile,
    _screen_work_area,
    _webgl_limits,
    _effective_disabled_groups,
    get_stealth_capabilities,
)

PAGE = (
    b"<!doctype html><html><head><meta charset=utf-8><title>verify</title>"
    b"</head><body>verify</body></html>"
)

# Clean Chrome 155 on this machine (ANGLE D3D11, AMD RX 6700 XT).
CLEAN_WEBGL = {
    "maxTextureSize": 16384,
    "maxViewportDims": [32767, 32767],
    "maxRenderbufferSize": 16384,
}

# ICU canonicalizes a few well-known aliases; both are the same zone.
_TIMEZONE_ALIASES = {
    "asia/saigon": "asia/ho_chi_minh",
    "asia/calcutta": "asia/kolkata",
    "europe/kiev": "europe/kyiv",
    "america/buenos_aires": "america/argentina/buenos_aires",
}


def _timezones_match(actual: Any, expected: Any) -> bool:
    if not actual or not expected:
        return False
    left = _TIMEZONE_ALIASES.get(str(actual).lower(), str(actual).lower())
    right = _TIMEZONE_ALIASES.get(str(expected).lower(), str(expected).lower())
    return left == right


def _private_ip_in_sdp(sdp: str) -> bool:
    return bool(
        re.search(
            r"(?<![\d.])("
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r")(?![\d.])",
            sdp or "",
        )
    )


# --- Giai đoạn 2: drift baseline (phát hiện máy/Chrome thay đổi) ---

def _baseline_path(profile_id: str) -> str:
    return os.path.join(DATA_DIR, "fingerprint_baselines", f"{profile_id}.json")


def _load_baseline(profile_id: str) -> Optional[Dict[str, Any]]:
    path = _baseline_path(profile_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_baseline(profile_id: str, actual: Dict[str, Any]) -> Dict[str, Any]:
    """Chụp baseline fingerprint máy/profile — các lần verify sau so với nó."""
    m = re.search(r"Chrome/([\d.]+)", actual.get("ua") or "")
    snapshot = {
        "saved_at": int(time.time()),
        "browser_version": m.group(1) if m else None,
        "ua": actual.get("ua"),
        "platform": actual.get("platform"),
        "glVendor": actual.get("glVendor"),
        "glRenderer": actual.get("glRenderer"),
        "hardwareConcurrency": actual.get("hc"),
        "deviceMemory": actual.get("dm"),
        "screen": {"w": actual.get("sw"), "h": actual.get("sh"),
                   "dpr": actual.get("dpr")},
        "tz": actual.get("tz"),
    }
    path = _baseline_path(profile_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return snapshot


def _baseline_drift(baseline: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, list]:
    """So baseline vs hiện tại. Lưu ý: glRenderer/hc/dm là profile-driven ở
    chế độ Unique (đổi fingerprint = chủ ý); `ua` đổi nghĩa là Chrome/browser
    binary đã khác — tín hiệu máy thay đổi đáng tin nhất."""
    drift: Dict[str, list] = {}
    m = re.search(r"Chrome/([\d.]+)", actual.get("ua") or "")
    pairs = {
        "ua": (baseline.get("ua"), actual.get("ua")),
        "browser_version": (baseline.get("browser_version"), m.group(1) if m else None),
        "glVendor": (baseline.get("glVendor"), actual.get("glVendor")),
        "glRenderer": (baseline.get("glRenderer"), actual.get("glRenderer")),
        "hardwareConcurrency": (baseline.get("hardwareConcurrency"), actual.get("hc")),
        "deviceMemory": (baseline.get("deviceMemory"), actual.get("dm")),
        "platform": (baseline.get("platform"), actual.get("platform")),
    }
    for key, (old, new) in pairs.items():
        if old is not None and new is not None and old != new:
            drift[key] = [old, new]
    return drift

LIVE_JS = r"""async () => {
    const out = { ok: true, errors: [] };
    const safe = (name, fn, fallback) => {
        try { return fn(); } catch (e) { out.errors.push(name + ': ' + e); return fallback; }
    };

    out.ua = navigator.userAgent;
    out.platform = navigator.platform;
    out.webdriver = navigator.webdriver;
    out.languages = Array.from(navigator.languages || []);
    out.language = navigator.language;
    out.hardwareConcurrency = navigator.hardwareConcurrency;
    out.deviceMemory = navigator.deviceMemory;
    out.dpr = devicePixelRatio;
    out.screen = {
        width: screen.width,
        height: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth: screen.colorDepth
    };
    out.timezone = safe('timezone',
        () => Intl.DateTimeFormat().resolvedOptions().timeZone, null);

    // Battery + NetworkInformation per-profile (đồng bộ với stealth config).
    out.battery = null;
    try {
        if (navigator.getBattery) {
            const b = await navigator.getBattery();
            out.battery = {
                level: b.level,
                charging: b.charging,
                chargingTime: b.chargingTime,
                dischargingTime: b.dischargingTime
            };
        }
    } catch (e) { out.errors.push('battery: ' + e); }
    out.connection = safe('connection', () => navigator.connection ? {
        effectiveType: navigator.connection.effectiveType,
        type: navigator.connection.type,
        rtt: navigator.connection.rtt,
        downlink: navigator.connection.downlink,
        saveData: navigator.connection.saveData
    } : null, null);
    out.css = {
        deviceWidth: matchMedia(`(device-width: ${screen.width}px)`).matches,
        deviceHeight: matchMedia(`(device-height: ${screen.height}px)`).matches,
        resolution: matchMedia(`(resolution: ${devicePixelRatio}dppx)`).matches,
        minResolution: matchMedia(`(min-resolution: ${devicePixelRatio}dppx)`).matches
    };
    out.own = {
        webdriver: navigator.hasOwnProperty('webdriver'),
        screenWidth: screen.hasOwnProperty('width'),
        toStringNative: Function.prototype.toString.toString().includes('[native code]')
    };
    out.native = {
        functionToString: Function.prototype.toString.toString().includes('[native code]'),
        toDataURL: Function.prototype.toString.call(
            HTMLCanvasElement.prototype.toDataURL
        ).includes('[native code]'),
        getImageData: Function.prototype.toString.call(
            CanvasRenderingContext2D.prototype.getImageData
        ).includes('[native code]'),
        getParameter: Function.prototype.toString.call(
            WebGLRenderingContext.prototype.getParameter
        ).includes('[native code]'),
        hcGetter: (() => {
            const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'hardwareConcurrency');
            return d && d.get
                ? Function.prototype.toString.call(d.get).includes('[native code]')
                : null;
        })()
    };

    // WebGL limits / renderer.
    out.webgl = safe('webgl', () => {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        if (!gl) return null;
        const ext = gl.getExtension('WEBGL_debug_renderer_info');
        return {
            maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
            maxViewportDims: Array.from(gl.getParameter(gl.MAX_VIEWPORT_DIMS) || []),
            maxRenderbufferSize: gl.getParameter(gl.MAX_RENDERBUFFER_SIZE),
            vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
            renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)
        };
    }, null);

    // Canvas S5 gate: a solid canvas must be untouched; a 64-colour canvas
    // must receive deterministic +/-1 noise.
    out.canvas = safe('canvas', () => {
        const simple = document.createElement('canvas');
        simple.width = 32; simple.height = 32;
        const sx = simple.getContext('2d');
        sx.fillStyle = '#123456';
        sx.fillRect(0, 0, 32, 32);
        const sd = sx.getImageData(0, 0, 32, 32).data;
        let simpleDiff = 0;
        for (let i = 0; i < sd.length; i += 4) {
            if (sd[i] !== 0x12 || sd[i + 1] !== 0x34 || sd[i + 2] !== 0x56 || sd[i + 3] !== 255) {
                simpleDiff += 1;
            }
        }

        const complex = document.createElement('canvas');
        complex.width = 64; complex.height = 64;
        const cx = complex.getContext('2d');
        for (let y = 0; y < 8; y += 1) {
            for (let x = 0; x < 8; x += 1) {
                cx.fillStyle = `rgb(${x * 32}, ${y * 32}, ${(x * 7 + y * 11) % 256})`;
                cx.fillRect(x * 8, y * 8, 8, 8);
            }
        }
        const cd = cx.getImageData(0, 0, 64, 64).data;
        let complexDiff = 0;
        for (let y = 0; y < 64; y += 1) {
            for (let x = 0; x < 64; x += 1) {
                const bx = Math.floor(x / 8);
                const by = Math.floor(y / 8);
                const expected = [bx * 32, by * 32, (bx * 7 + by * 11) % 256];
                const i = (y * 64 + x) * 4;
                if (cd[i] !== expected[0] || cd[i + 1] !== expected[1] || cd[i + 2] !== expected[2]) {
                    complexDiff += 1;
                }
            }
        }
        return { simpleDiff, complexDiff, simpleHash: simple.toDataURL().length };
    }, null);

    // Worker overrides: timezone, locale, UA.
    try {
        const workerCode = `
            postMessage({
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                language: navigator.language,
                languages: Array.from(navigator.languages || []),
                userAgent: navigator.userAgent,
                hardwareConcurrency: navigator.hardwareConcurrency,
                deviceMemory: navigator.deviceMemory,
                platform: navigator.platform
            });
        `;
        out.worker = await new Promise((resolve) => {
            let done = false;
            const worker = new Worker(URL.createObjectURL(new Blob([workerCode], { type: 'application/javascript' })));
            const timer = setTimeout(() => {
                if (done) return;
                done = true;
                try { worker.terminate(); } catch (e) {}
                resolve({ error: 'worker timeout' });
            }, 4000);
            worker.onmessage = (event) => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                try { worker.terminate(); } catch (e) {}
                resolve(event.data);
            };
            worker.onerror = (event) => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                resolve({ error: String(event.message || 'worker error') });
            };
        });
    } catch (e) {
        out.worker = { error: String(e) };
    }

    // WebRTC: constructors must still be native; SDP must not expose LAN IPs.
    try {
        out.webrtc = {
            ctorNative: RTCPeerConnection.toString().includes('[native code]'),
            sdp: ''
        };
        const pc = new RTCPeerConnection({ iceServers: [] });
        pc.createDataChannel('verify');
        const offer = await pc.createOffer();
        out.webrtc.sdp = offer.sdp || '';
        pc.close();
    } catch (e) {
        out.webrtc = { error: String(e) };
    }

    return out;
}"""


class VerifyServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: ANN001
        # A browser closing a keep-alive connection during cleanup is normal.
        pass


class VerifyHandler(BaseHTTPRequestHandler):
    requests: List[Dict[str, str]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.requests.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # Chrome 155 sends the legacy Device-Memory header and/or the newer
        # Sec-CH-Device-Memory hint depending on which one is requested. Ask
        # for both and make it critical so the very first navigation retries.
        self.send_header("Accept-CH", "Device-Memory, Sec-CH-Device-Memory")
        self.send_header("Critical-CH", "Device-Memory")
        self.send_header("Vary", "Device-Memory, Sec-CH-Device-Memory")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


def _check(name: str, ok: bool, actual: Any, expected: Any) -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "actual": actual, "expected": expected}


def _canvas_policy_checks(fp, canvas):
    """Validate the selected canvas revision without assuming v1 for all profiles."""
    disabled = "canvas" in _effective_disabled_groups(fp)
    revision = getattr(fp, "fingerprint_algorithm_version", 1)
    checks = []
    for label, expect_noise in (
        ("simple", not disabled and revision >= 2),
        ("complex", not disabled),
    ):
        actual = canvas.get(label + "Diff")
        valid = isinstance(actual, int) and (actual > 0 if expect_noise else actual == 0)
        checks.append(_check(
            "canvas_" + label + ("_noisy" if expect_noise else "_native"),
            valid, actual, ">0" if expect_noise else 0,
        ))
    return checks


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Live verify one real profile.")
    parser.add_argument("--profile", required=True, help="Profile id or name")
    parser.add_argument("--json", help="Write the full report to this path")
    parser.add_argument(
        "--save-baseline", action="store_true",
        help="Chụp fingerprint hiện tại làm baseline (Giai đoạn 2); các lần "
             "verify sau tự so và báo drift khi máy/Chrome thay đổi.",
    )
    args = parser.parse_args(argv)

    manager = ProfileManager()
    profiles, missing = common.resolve_profiles(manager, [args.profile])
    if missing or not profiles:
        print(f"[!] Profile not found: {args.profile}", file=sys.stderr)
        return 2
    profile = profiles[0]

    try:
        browser_path = find_browser_executable()
    except Exception as exc:
        print(f"[!] Browser not found: {exc}", file=sys.stderr)
        return 2
    fp = normalize_fingerprint_for_launch(profile.fingerprint, browser_path)

    server = VerifyServer(("127.0.0.1", 0), VerifyHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] Playwright is required.", file=sys.stderr)
        return 2

    launcher = BrowserLauncher(manager)
    report: Dict[str, Any] = {
        "profile": common.profile_summary(profile),
        "expected": {
            "screen_width": fp.screen_width,
            "screen_height": fp.screen_height,
            "device_pixel_ratio": fp.device_pixel_ratio,
            "device_memory": fp.device_memory,
            "timezone": fp.timezone,
            "locale": fp.locale,
            "languages": fp.languages,
            "user_agent": fp.user_agent,
            "hardware_concurrency": fp.hardware_concurrency,
            "webgl_limits": _webgl_limits(fp),
        },
        "checks": [],
        "capabilities": get_stealth_capabilities(fp),
        "observations": {},
        "launch": None,
        "cleanup": None,
        "status": "UNKNOWN",
    }

    browser = None
    was_running = False
    try:
        was_running, launch_result, cdp_port = common.start_profile(launcher, profile)
        report["launch"] = {
            k: launch_result.get(k)
            for k in (
                "success", "port", "pid", "cdp_ready", "page_ready",
                "stealth_injected", "emulation_verified",
                "configuration_verified", "verification", "warning",
            )
            if k in launch_result
        }

        with sync_playwright() as pw:
            browser = common.connect_browser(pw, cdp_port)
            _, page = common.get_default_page(browser)
            page.set_default_timeout(20_000)
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            # Reload after Accept-CH so Sec-CH-Device-Memory is present.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            data = page.evaluate(LIVE_JS)

        headers = VerifyHandler.requests[-1] if VerifyHandler.requests else {}
        sec_ch_dm = headers.get("sec-ch-device-memory")
        legacy_dm = headers.get("device-memory")
        header_dm = sec_ch_dm if sec_ch_dm is not None else legacy_dm
        report["request_headers"] = {
            "sec-ch-device-memory": sec_ch_dm,
            "device-memory": legacy_dm,
            "user-agent": headers.get("user-agent"),
            "accept-language": headers.get("accept-language"),
        }
        report["requests_seen"] = len(VerifyHandler.requests)

        checks = report["checks"]
        checks.append(_check("webdriver_false", data.get("webdriver") is False,
                            data.get("webdriver"), False))
        checks.append(_check("ua", data.get("ua") == fp.user_agent,
                            data.get("ua"), fp.user_agent))
        checks.append(_check("platform", data.get("platform") == (
            "MacIntel" if fp.os == "macos" else
            ("Linux x86_64" if fp.os == "linux" else "Win32")),
            data.get("platform"), "platform"))
        checks.append(_check("language", data.get("language") == fp.locale,
                            data.get("language"), fp.locale))
        checks.append(_check("languages", data.get("languages") == (fp.languages or [fp.locale]),
                            data.get("languages"), fp.languages))
        checks.append(_check("timezone", _timezones_match(data.get("timezone"), fp.timezone),
                            data.get("timezone"), fp.timezone))
        checks.append(_check("hardwareConcurrency",
                            data.get("hardwareConcurrency") == fp.hardware_concurrency,
                            data.get("hardwareConcurrency"), fp.hardware_concurrency))
        checks.append(_check("deviceMemory",
                            data.get("deviceMemory") == fp.device_memory,
                            data.get("deviceMemory"), fp.device_memory))
        checks.append(_check("deviceMemory_header",
                            header_dm is not None and float(header_dm) == float(fp.device_memory),
                            header_dm, fp.device_memory))

        screen = data.get("screen") or {}
        # R2 hooks the work-area getters to model the OS taskbar. When the
        # work area is empty (no taskbar margin to model), CDP device metrics
        # report the full screen as available.
        work_area = _screen_work_area(fp)
        expected_avail_w = work_area.get("availWidth", fp.screen_width)
        expected_avail_h = work_area.get("availHeight", fp.screen_height)
        checks.append(_check("screen.width", screen.get("width") == fp.screen_width,
                            screen.get("width"), fp.screen_width))
        checks.append(_check("screen.height", screen.get("height") == fp.screen_height,
                            screen.get("height"), fp.screen_height))
        checks.append(_check("screen.availWidth", screen.get("availWidth") == expected_avail_w,
                            screen.get("availWidth"), expected_avail_w))
        checks.append(_check("screen.availHeight", screen.get("availHeight") == expected_avail_h,
                            screen.get("availHeight"), expected_avail_h))
        checks.append(_check("devicePixelRatio",
                            abs(float(data.get("dpr") or 0) - float(fp.device_pixel_ratio or 1.0)) < 0.01,
                            data.get("dpr"), fp.device_pixel_ratio))
        css = data.get("css") or {}
        checks.append(_check("css_device_width", css.get("deviceWidth") is True,
                            css.get("deviceWidth"), True))
        checks.append(_check("css_device_height", css.get("deviceHeight") is True,
                            css.get("deviceHeight"), True))
        checks.append(_check("css_resolution", css.get("resolution") is True,
                            css.get("resolution"), True))
        checks.append(_check("css_min_resolution", css.get("minResolution") is True,
                            css.get("minResolution"), True))

        own = data.get("own") or {}
        checks.append(_check("navigator_webdriver_own", own.get("webdriver") is False,
                            own.get("webdriver"), False))
        checks.append(_check("screen_width_own", own.get("screenWidth") is False,
                            own.get("screenWidth"), False))
        checks.append(_check("toString_native", own.get("toStringNative") is True,
                            own.get("toStringNative"), True))
        native = data.get("native") or {}
        checks.append(_check("toDataURL_native_source", native.get("toDataURL") is True,
                            native.get("toDataURL"), True))
        checks.append(_check("getImageData_native_source", native.get("getImageData") is True,
                            native.get("getImageData"), True))
        checks.append(_check("getParameter_native_source", native.get("getParameter") is True,
                            native.get("getParameter"), True))
        checks.append(_check("hardwareConcurrency_getter_native_source",
                            native.get("hcGetter") is True,
                            native.get("hcGetter"), True))

        webgl = data.get("webgl") or {}
        revision = getattr(fp, "fingerprint_algorithm_version", 1)
        native_webgl = revision >= 2 or "webgl" in _effective_disabled_groups(fp)
        if native_webgl:
            # Hardware observation, not a comparison against a different GPU's
            # hard-coded limits or a profile renderer no longer being spoofed.
            report["observations"]["native_webgl"] = webgl
            report["observations"]["native_webgl_validation"] = "not_compared_to_clean_baseline"
        else:
            expected_limits = _webgl_limits(fp) or CLEAN_WEBGL
            checks.append(_check("webgl_max_texture",
                                webgl.get("maxTextureSize") == expected_limits["maxTextureSize"],
                                webgl.get("maxTextureSize"), expected_limits["maxTextureSize"]))
            checks.append(_check("webgl_max_viewport",
                                webgl.get("maxViewportDims") == expected_limits["maxViewportDims"],
                                webgl.get("maxViewportDims"), expected_limits["maxViewportDims"]))
            checks.append(_check("webgl_max_renderbuffer",
                                webgl.get("maxRenderbufferSize") == expected_limits["maxRenderbufferSize"],
                                webgl.get("maxRenderbufferSize"), expected_limits["maxRenderbufferSize"]))
            checks.append(_check("webgl_vendor", webgl.get("vendor") == fp.webgl_vendor,
                                webgl.get("vendor"), fp.webgl_vendor))
            checks.append(_check("webgl_renderer", webgl.get("renderer") == fp.webgl_renderer,
                                webgl.get("renderer"), fp.webgl_renderer))

        checks.extend(_canvas_policy_checks(fp, data.get("canvas") or {}))

        # Battery + NetworkInformation: hooks là opt-in (mặc định tắt) — khi
        # tắt, trang thấy giá trị native; chỉ so sánh khi biết nguồn expected
        # (native cache đo được, hoặc hooks đang bật với fallback deterministic).
        from antidetect.stealth import _native_or_fallback
        fp_proxy = getattr(profile, "proxy", None)
        native_hw = {}
        if hasattr(launcher, "_native_brands_cache"):
            native_hw = launcher._native_brands_cache.get(fp.browser_version) or {}
        batt_exp = _native_or_fallback(native_hw, "battery", _battery_profile(fp))
        groups_disabled = _effective_disabled_groups(fp)
        battery_checkable = native_hw.get("battery") or "battery" not in groups_disabled
        connection_checkable = native_hw.get("connection") or "network" not in groups_disabled
        batt = data.get("battery")
        conn = data.get("connection")
        checks.append(_check("battery_available", batt is not None,
                            bool(batt), True))
        if batt and battery_checkable:
            checks.append(_check("battery_level", batt.get("level") == batt_exp["level"],
                                batt.get("level"), batt_exp["level"]))
            checks.append(_check("battery_charging", batt.get("charging") == batt_exp["charging"],
                                batt.get("charging"), batt_exp["charging"]))
            checks.append(_check("battery_times",
                                batt.get("chargingTime") == batt_exp["chargingTime"]
                                and batt.get("dischargingTime") == batt_exp["dischargingTime"],
                                [batt.get("chargingTime"), batt.get("dischargingTime")],
                                [batt_exp["chargingTime"], batt_exp["dischargingTime"]]))
        checks.append(_check("connection_available", conn is not None,
                            bool(conn), True))
        if conn and connection_checkable:
            proxy_on = bool(fp_proxy and fp_proxy.enabled and fp_proxy.host)
            if proxy_on:
                # Qua proxy, navigator.connection native phản ánh đường truyền
                # proxy realtime (Chrome thật cũng vậy) — chỉ kiểm cấu trúc
                # bucket theo spec, không so bằng giá trị đo direct.
                rtt = conn.get("rtt")
                downlink = conn.get("downlink")
                checks.append(_check("connection_rtt_bucket",
                                    isinstance(rtt, int) and rtt >= 0 and rtt % 25 == 0,
                                    rtt, "bội 25 (spec)"))
                checks.append(_check("connection_downlink_sane",
                                    isinstance(downlink, (int, float))
                                    and 0 < downlink <= 10,
                                    downlink, "0 < downlink <= 10"))
                checks.append(_check("connection_effectiveType",
                                    conn.get("effectiveType") in ("slow-2g", "2g", "3g", "4g"),
                                    conn.get("effectiveType"), "valid"))
            else:
                # Connection native dao động theo mạng tại thời điểm đo
                # (rtt đo lúc T0 khác lúc verify là hành vi của Chrome thật,
                # không phải leak) — chỉ kiểm cấu trúc bucket theo spec.
                rtt = conn.get("rtt")
                downlink = conn.get("downlink")
                checks.append(_check("connection_rtt_bucket",
                                    isinstance(rtt, int) and rtt >= 0 and rtt % 25 == 0,
                                    rtt, "bội 25 (spec)"))
                checks.append(_check("connection_downlink_sane",
                                    isinstance(downlink, (int, float))
                                    and 0 < downlink <= 10,
                                    downlink, "0 < downlink <= 10"))
                checks.append(_check("connection_effectiveType",
                                    conn.get("effectiveType") in ("slow-2g", "2g", "3g", "4g"),
                                    conn.get("effectiveType"), "valid"))

        worker = data.get("worker") or {}
        checks.append(_check("worker_timezone", _timezones_match(worker.get("timezone"), fp.timezone),
                            worker.get("timezone"), fp.timezone))
        checks.append(_check("worker_language", worker.get("language") == fp.locale,
                            worker.get("language"), fp.locale))
        checks.append(_check("worker_ua", worker.get("userAgent") == fp.user_agent,
                            worker.get("userAgent"), fp.user_agent))
        checks.append(_check("worker_deviceMemory",
                            worker.get("deviceMemory") == fp.device_memory,
                            worker.get("deviceMemory"), fp.device_memory))

        webrtc = data.get("webrtc") or {}
        checks.append(_check("webrtc_constructor_native", webrtc.get("ctorNative") is True,
                            webrtc.get("ctorNative"), True))
        sdp = webrtc.get("sdp") or ""
        private_ip = _private_ip_in_sdp(sdp)
        checks.append(_check("webrtc_no_private_ip", not private_ip,
                            "private IP in SDP" if private_ip else "clean",
                            "no private IP"))

        failures = [c for c in checks if not c["ok"]]
        report["failures"] = [f"{c['name']}: got {c['actual']!r}, expected {c['expected']!r}"
                              for c in failures]
        report["status"] = "PASS" if not failures else "FAIL"

        # Giai đoạn 2: drift baseline — so với lần chụp trước để phát hiện
        # Chrome update / GPU / máy thay đổi (tín hiệu cả farm drift cùng lúc).
        actual = ((report.get("launch") or {}).get("verification") or {}).get("actual") or {}
        baseline = _load_baseline(profile.id)
        if args.save_baseline and actual:
            saved = _save_baseline(profile.id, actual)
            report["baseline"] = {"action": "saved", "snapshot": saved}
        elif baseline and actual:
            drift = _baseline_drift(baseline, actual)
            report["baseline"] = {
                "action": "compared",
                "saved_at": baseline.get("saved_at"),
                "drift": drift,
                "note": (
                    "ua/browser_version đổi = Chrome hoặc browser pin đã khác "
                    "→ chạy lại detector audit cho cả farm. glRenderer/hc/dm "
                    "ở chế độ Unique do profile cấu hình — drift ở đó có thể "
                    "là chủ ý."
                ),
            }
        else:
            report["baseline"] = {
                "action": "none",
                "note": "Chưa có baseline. Chạy lại với --save-baseline sau khi verify PASS.",
            }
    except Exception as exc:  # noqa: BLE001
        report["failures"] = [f"{type(exc).__name__}: {exc}"]
        report["status"] = "FAIL"
    finally:
        report["cleanup"] = common.finish_profile(launcher, profile, was_running, browser)
        server.shutdown()

    print("=" * 78)
    print("LIVE PROFILE VERIFICATION")
    print("=" * 78)
    print(f"Profile: {profile.name} ({profile.id})")
    if report.get("launch"):
        print(f"Launch verified: {report['launch'].get('configuration_verified')}")
        print(f"Emulation verified: {report['launch'].get('emulation_verified')}")
    print(f"Headers: {json.dumps(report.get('request_headers'), ensure_ascii=False)}")
    for check in report["checks"]:
        flag = "PASS" if check["ok"] else "FAIL"
        print(f"  [{flag}] {check['name']}: {check['actual']!r} (expected {check['expected']!r})")
    for failure in report.get("failures", []):
        print(f"  FAIL: {failure}")
    baseline_info = report.get("baseline") or {}
    if baseline_info.get("action") == "compared":
        drift = baseline_info.get("drift") or {}
        if drift:
            print(f"  [DRIFT] Máy/Chrome đã thay đổi so với baseline "
                  f"({time.strftime('%Y-%m-%d', time.localtime(baseline_info.get('saved_at', 0)))}):")
            for key, (old, new) in drift.items():
                print(f"     {key}: {old!r} -> {new!r}")
            print("  → Chạy lại detector audit cho cả farm trước khi dùng thật.")
    elif baseline_info.get("action") == "saved":
        print("  [BASELINE] Đã chụp fingerprint hiện tại làm baseline.")
    print(f"RESULT: {report['status']}")

    if args.json:
        common.write_json(args.json, report)
        print(f"[*] Report written: {os.path.abspath(args.json)}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
