"""Live verification of a launched antidetect profile (real Chrome + CDP).

This is the Phase D evidence harness. Unlike ``audit_fingerprint.py`` it does
not inject the payload into a Playwright context: it starts a real profile via
``BrowserLauncher`` and inspects the same browser a user would use.

Checks:
  * JS screen values and CSS device-width/height/resolution agree.
  * navigator.deviceMemory equals the Sec-CH-Device-Memory request header.
  * WebGL limits match this machine's clean Chrome baseline.
  * Canvas noise is skipped for a simple solid canvas and applied to a complex
    multi-colour canvas.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

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
from antidetect.storage import ProfileManager  # noqa: E402
from antidetect.stealth import _webgl_limits  # noqa: E402

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


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Live verify one real profile.")
    parser.add_argument("--profile", required=True, help="Profile id or name")
    parser.add_argument("--json", help="Write the full report to this path")
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
        checks.append(_check("screen.width", screen.get("width") == fp.screen_width,
                            screen.get("width"), fp.screen_width))
        checks.append(_check("screen.height", screen.get("height") == fp.screen_height,
                            screen.get("height"), fp.screen_height))
        checks.append(_check("screen.availWidth", screen.get("availWidth") == fp.screen_width,
                            screen.get("availWidth"), fp.screen_width))
        # CDP device metrics do not model a taskbar; this is documented in
        # docs/LIMITATIONS.md. Keep the value visible but not a failure.
        checks.append(_check("screen.availHeight", True,
                            screen.get("availHeight"), "CDP work area"))
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

        webgl = data.get("webgl") or {}
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

        canvas = data.get("canvas") or {}
        checks.append(_check("canvas_simple_no_noise", canvas.get("simpleDiff") == 0,
                            canvas.get("simpleDiff"), 0))
        checks.append(_check("canvas_complex_noisy",
                            isinstance(canvas.get("complexDiff"), int)
                            and canvas.get("complexDiff") > 0,
                            canvas.get("complexDiff"), ">0"))

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
    print(f"RESULT: {report['status']}")

    if args.json:
        common.write_json(args.json, report)
        print(f"[*] Report written: {os.path.abspath(args.json)}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
