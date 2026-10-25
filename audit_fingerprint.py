"""Evidence-based, read-only fingerprint audit for every stored profile.

This script does not launch real profile directories and does not write to
data/profiles.json. It injects the same page script used by the launcher into
Playwright pages, then compares what the page sees with the normalized
fingerprint that the launcher would use.
"""
import argparse
import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from antidetect.storage import ProfileManager
from antidetect.fingerprint import normalize_fingerprint_for_launch
from antidetect.launcher import find_browser_executable
from antidetect.stealth import get_stealth_script, _screen_work_area


PAGE = b"<!doctype html><html><head><meta charset=utf-8><title>audit</title></head><body>audit</body></html>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


COLLECT_JS = r"""async () => {
    const out = {};
    const withTimeout = (promise, ms, label) => Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(() => reject(new Error(label + ' timeout')), ms))
    ]);
    out.ua = navigator.userAgent;
    out.appVersion = navigator.appVersion;
    out.platform = navigator.platform;
    out.hc = navigator.hardwareConcurrency;
    out.dm = navigator.deviceMemory;
    out.langs = navigator.languages;
    out.lang = navigator.language;
    out.dpr = devicePixelRatio;
    out.screen = {
        width: screen.width, height: screen.height,
        availWidth: screen.availWidth, availHeight: screen.availHeight,
        colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth
    };
    out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    out.webdriver = navigator.webdriver;
    out.own = {
        webdriver: navigator.hasOwnProperty('webdriver'),
        hc: navigator.hasOwnProperty('hardwareConcurrency'),
        screenWidth: screen.hasOwnProperty('width'),
        dpr: Object.prototype.hasOwnProperty.call(window, 'devicePixelRatio')
    };
    out.native = {
        functionToString: Function.prototype.toString.toString().includes('[native code]'),
        // R1: wrappers must report a native source string through the shared
        // registry. These are regression checks, not just diagnostics.
        toDataURL: HTMLCanvasElement.prototype.toDataURL.toString().includes('[native code]'),
        getImageData: CanvasRenderingContext2D.prototype.getImageData.toString().includes('[native code]'),
        getParameter: WebGLRenderingContext.prototype.getParameter.toString().includes('[native code]'),
        getHighEntropyValues: navigator.userAgentData
            ? navigator.userAgentData.getHighEntropyValues.toString().includes('[native code]')
            : null
    };
    out.match = {
        exact: matchMedia(`(resolution: ${devicePixelRatio}dppx)`).matches,
        min: matchMedia(`(min-resolution: ${devicePixelRatio}dppx)`).matches,
        max: matchMedia(`(max-resolution: ${devicePixelRatio}dppx)`).matches,
        wrong: matchMedia(`(resolution: ${Math.abs(devicePixelRatio - 1) < 0.01 ? 2 : 1}dppx)`).matches,
        combo: matchMedia(`(resolution: ${devicePixelRatio}dppx) and (min-width: 1px)`).matches,
        deviceWidth: matchMedia(`(device-width: ${screen.width}px)`).matches,
        deviceHeight: matchMedia(`(device-height: ${screen.height}px)`).matches
    };
    try {
        const c = document.createElement('canvas');
        c.width = 200; c.height = 60;
        const x = c.getContext('2d');
        // S5 gate needs at least 64 distinct opaque colours. Fill the canvas
        // with an 8x8 colour grid, then draw the usual text/overlay.
        for (let gy = 0; gy < 8; gy += 1) {
            for (let gx = 0; gx < 8; gx += 1) {
                x.fillStyle = `rgb(${gx * 30}, ${gy * 30}, ${(gx * 7 + gy * 11) % 256})`;
                x.fillRect(gx * 25, gy * 7, 25, 7);
            }
        }
        x.textBaseline = 'top';
        x.font = '14px Arial';
        x.fillStyle = '#f60'; x.fillRect(125, 1, 62, 20);
        x.fillStyle = '#069'; x.fillText('Antidetect Audit 2026', 2, 15);
        x.fillStyle = 'rgba(102, 204, 0, 0.7)';
        x.fillText('Fingerprint Validation', 4, 35);
        out.canvasUrl = c.toDataURL();
        out.canvasUrl2 = c.toDataURL();
        out.canvasPixels = Array.from(x.getImageData(0, 0, 200, 60).data);
    } catch (e) { out.canvasError = String(e); }
    out.canvasHash = out.canvasUrl
        ? Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
            new TextEncoder().encode(out.canvasUrl))))
            .map(b => b.toString(16).padStart(2, '0')).join('')
        : null;
    try {
        const glCanvas = document.createElement('canvas');
        glCanvas.width = 64; glCanvas.height = 64;
        const gl = glCanvas.getContext('webgl');
        const ext = gl.getExtension('WEBGL_debug_renderer_info');
        out.glVendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null;
        out.glRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
        out.glViewport = gl ? Array.from(gl.getParameter(gl.MAX_VIEWPORT_DIMS)) : null;
        // Draw real pixels first: hashing a blank canvas produced the same
        // meaningless SHA-256 for every profile in the previous audit.
        gl.clearColor(0.23, 0.41, 0.67, 1.0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        const pixels = new Uint8Array(64 * 64 * 4);
        gl.readPixels(0, 0, 64, 64, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        out.webglPixelHash = Array.from(new Uint8Array(await crypto.subtle.digest(
            'SHA-256', pixels)))
            .map(b => b.toString(16).padStart(2, '0')).join('');
        out.webglUrl = glCanvas.toDataURL();
        out.webglUrlHash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
            new TextEncoder().encode(out.webglUrl))))
            .map(b => b.toString(16).padStart(2, '0')).join('');
        out.webglHash = out.webglPixelHash;
    } catch (e) { out.glError = String(e); }
    try {
        if (navigator.userAgentData) {
            out.brands = navigator.userAgentData.brands;
            out.hi = await withTimeout(navigator.userAgentData.getHighEntropyValues([
                'uaFullVersion', 'fullVersionList', 'platform', 'platformVersion',
                'architecture', 'bitness', 'model', 'mobile', 'wow64'
            ]), 5000, 'CH');
        }
    } catch (e) { out.chError = String(e); }
    return out;
}"""

CLEAN_DRAW_JS = r"""() => {
    const c = document.createElement('canvas');
    c.width = 200; c.height = 60;
    const x = c.getContext('2d');
    for (let gy = 0; gy < 8; gy += 1) {
        for (let gx = 0; gx < 8; gx += 1) {
            x.fillStyle = `rgb(${gx * 30}, ${gy * 30}, ${(gx * 7 + gy * 11) % 256})`;
            x.fillRect(gx * 25, gy * 7, 25, 7);
        }
    }
    x.textBaseline = 'top';
    x.font = '14px Arial';
    x.fillStyle = '#f60'; x.fillRect(125, 1, 62, 20);
    x.fillStyle = '#069'; x.fillText('Antidetect Audit 2026', 2, 15);
    x.fillStyle = 'rgba(102, 204, 0, 0.7)';
    x.fillText('Fingerprint Validation', 4, 35);
    return {url: c.toDataURL(), pixels: Array.from(x.getImageData(0, 0, 200, 60).data)};
}"""

DECODE_JS = r"""async ({url}) => {
    const img = new Image();
    img.src = url;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = 200; c.height = 60;
    const x = c.getContext('2d');
    x.drawImage(img, 0, 0);
    return Array.from(x.getImageData(0, 0, 200, 60).data);
}"""


def platform_expectations(fp):
    if fp.os == "macos":
        return ("macOS", "14.5.0", "arm")
    if fp.os == "linux":
        return ("Linux", "6.5.0", "x86")
    return ("Windows", "15.0.0", "x86")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=None,
                        help="profile id or name (repeatable; default: all)")
    parser.add_argument("--headed", action="store_true",
                        help="run a visible browser instead of headless")
    parser.add_argument("--json", nargs="?", const="-", default=None,
                        help="export results as JSON (pass file path or '-' for stdout)")
    parser.add_argument("--strict", action="store_true",
                        help="fail with non-zero exit code on any warning (e.g. fingerprint collision)")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print("Playwright is required: " + str(e))
        return 2

    manager = ProfileManager()
    profiles = manager.list_profiles()
    if args.profile:
        wanted = set(args.profile)
        profiles = [p for p in profiles if p.id in wanted or p.name in wanted]
    if not profiles:
        print("No matching profiles.")
        return 2

    try:
        browser_path = find_browser_executable()
    except Exception as e:
        print("Browser not found: " + str(e))
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    results = []
    failures = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=browser_path, headless=not args.headed
            )
            clean_context = browser.new_context(viewport={"width": 800, "height": 600})
            clean_page = clean_context.new_page()
            clean_page.goto(base + "/", wait_until="domcontentloaded")
            clean_native = clean_page.evaluate(CLEAN_DRAW_JS)

            for profile in profiles:
                fp = normalize_fingerprint_for_launch(profile.fingerprint, browser_path)
                context = browser.new_context(
                    viewport={"width": 800, "height": 600},
                    device_scale_factor=float(fp.device_pixel_ratio or 1.0),
                    user_agent=fp.user_agent,
                    locale=fp.locale,
                    timezone_id=fp.timezone,
                )
                page = context.new_page()
                # S3: screen.* and CSS media queries must come from Chrome's
                # native device emulation, not from JS hooks. width/height=0
                # keeps the ordinary viewport while screenWidth/screenHeight
                # drive both JS and @media.
                cdp = context.new_cdp_session(page)
                cdp.send("Emulation.setDeviceMetricsOverride", {
                    "width": 0,
                    "height": 0,
                    "deviceScaleFactor": float(fp.device_pixel_ratio or 1.0),
                    "mobile": False,
                    "screenWidth": int(fp.screen_width),
                    "screenHeight": int(fp.screen_height),
                })
                page.add_init_script(get_stealth_script(fp))
                page.goto(base + "/", wait_until="domcontentloaded")
                page.wait_for_timeout(250)
                data = page.evaluate(COLLECT_JS)
                context.close()

                decoded = None
                if data.get("canvasUrl"):
                    decoded = clean_page.evaluate(DECODE_JS, {"url": data["canvasUrl"]})
                native_pixels = clean_native["pixels"]
                canvas_pixels = data.get("canvasPixels") or []

                checks = []
                def check(name, actual, expected=None, ok=None):
                    if ok is None:
                        ok = actual == expected
                    checks.append({"name": name, "ok": bool(ok), "actual": actual, "expected": expected})
                    if not ok:
                        failures.append(f"{profile.name}: {name}: got {actual!r}, expected {expected!r}")

                check("userAgent", data.get("ua"), fp.user_agent)
                check("platform", data.get("platform"), "MacIntel" if fp.os == "macos" else (
                    "Linux x86_64" if fp.os == "linux" else "Win32"))
                check("hardwareConcurrency", data.get("hc"), fp.hardware_concurrency)
                check("deviceMemory", data.get("dm"), int(fp.device_memory or 8))
                check("language", data.get("lang"), fp.locale)
                check("languages", data.get("langs"), fp.languages)
                expected_dpr = float(fp.device_pixel_ratio or 1.0)
                actual_dpr = float(data.get("dpr") or 0)
                check("devicePixelRatio", actual_dpr, expected_dpr,
                      ok=abs(actual_dpr - expected_dpr) < 0.01)
                screen = data.get("screen") or {}
                # R2: the work-area getters are hooked to model the OS
                # taskbar; when there is nothing to hook, CDP's device
                # metrics report the full screen as available.
                work_area = _screen_work_area(fp)
                check("screen.width", screen.get("width"), fp.screen_width)
                check("screen.height", screen.get("height"), fp.screen_height)
                check("screen.availWidth", screen.get("availWidth"),
                      work_area.get("availWidth", fp.screen_width))
                check("screen.availHeight", screen.get("availHeight"),
                      work_area.get("availHeight", fp.screen_height))
                # ICU canonicalizes some IANA aliases (for example
                # Asia/Ho_Chi_Minh -> Asia/Saigon). Both are the same zone.
                tz_aliases = {
                    "Asia/Ho_Chi_Minh": "Asia/Saigon",
                    "Asia/Saigon": "Asia/Ho_Chi_Minh",
                }
                tz_expected = fp.timezone
                check(
                    "timezone",
                    data.get("tz"),
                    tz_expected,
                    ok=(data.get("tz") == tz_expected
                        or tz_aliases.get(data.get("tz")) == tz_expected),
                )
                check("webdriver", data.get("webdriver"), False)
                own = data.get("own") or {}
                check("webdriver prototype-owned", own.get("webdriver"), False)
                check("hardwareConcurrency prototype-owned", own.get("hc"), False)
                check("screen.width prototype-owned", own.get("screenWidth"), False)
                match = data.get("match") or {}
                check("matchMedia exact DPR", match.get("exact"), True)
                check("matchMedia min DPR", match.get("min"), True)
                check("matchMedia max DPR", match.get("max"), True)
                check("matchMedia wrong DPR", match.get("wrong"), False)
                check("matchMedia combined", match.get("combo"), True)
                check("matchMedia device-width", match.get("deviceWidth"), True)
                check("matchMedia device-height", match.get("deviceHeight"), True)
                check("canvas toDataURL stable", data.get("canvasUrl"), data.get("canvasUrl2"))
                diff_bytes = sum(1 for a, b in zip(decoded or [], canvas_pixels) if a != b)
                check("canvas toDataURL/getImageData bytes", diff_bytes, 0)
                noise_pixels = sum(
                    1 for i in range(0, min(len(native_pixels), len(canvas_pixels)), 4)
                    if native_pixels[i:i + 4] != canvas_pixels[i:i + 4]
                )
                # Profile "authentic" (stealth_disable chứa 'canvas') tắt noise
                # chủ đích: canvas phải GIỮ NGUYÊN pixel native của host.
                if "canvas" in (getattr(fp, "stealth_disable", None) or []):
                    check("canvas noise disabled (native)", noise_pixels, 0)
                else:
                    check("canvas noise applied", noise_pixels > 0, True)
                check("WebGL vendor", data.get("glVendor"), fp.webgl_vendor)
                check("WebGL renderer", data.get("glRenderer"), fp.webgl_renderer)
                viewport = data.get("glViewport")
                viewport_ok = (
                    isinstance(viewport, list) and len(viewport) == 2
                    and all(isinstance(v, int) and v >= 16384 for v in viewport)
                )
                check("WebGL viewport", viewport, "two dimensions >= 16384",
                      ok=viewport_ok)
                native = data.get("native") or {}
                # R1: Function.prototype.toString itself must still look
                # native, and every hooked function must report [native code].
                check("Function.prototype.toString native",
                      native.get("functionToString"), True)
                check("toDataURL native source", native.get("toDataURL"), True)
                check("getImageData native source", native.get("getImageData"), True)
                check("getParameter native source", native.get("getParameter"), True)
                if native.get("getHighEntropyValues") is not None:
                    check("getHighEntropyValues native source",
                          native.get("getHighEntropyValues"), True)

                exp_platform, exp_platform_ver, exp_arch = platform_expectations(fp)
                hi = data.get("hi") or {}
                check("CH platform", hi.get("platform"), exp_platform)
                check("CH platformVersion", hi.get("platformVersion"), exp_platform_ver)
                check("CH architecture", hi.get("architecture"), exp_arch)
                check("CH bitness", hi.get("bitness"), "64")
                check("CH model", hi.get("model"), "")
                check("CH mobile", hi.get("mobile"), False)
                check("CH wow64", hi.get("wow64"), False)
                check("CH uaFullVersion", hi.get("uaFullVersion"), fp.browser_version)
                full = hi.get("fullVersionList") or []
                full_map = {item.get("brand"): item.get("version") for item in full}
                check("CH fullVersionList Chrome", full_map.get("Google Chrome"), fp.browser_version)
                check("CH fullVersionList Chromium", full_map.get("Chromium"), fp.browser_version)

                results.append({
                    "id": profile.id,
                    "name": profile.name,
                    "os": fp.os,
                    "canvasHash": data.get("canvasHash"),
                    "webglHash": data.get("webglHash"),
                    "checks": checks,
                    # Profile "authentic" cố ý tắt noise → hash trùng host.
                    "canvas_noise_disabled": "canvas" in (
                        getattr(fp, "stealth_disable", None) or []
                    ),
                    "webgl_noise_disabled": "webgl" in (
                        getattr(fp, "stealth_disable", None) or []
                    ),
                })
            browser.close()
    finally:
        server.shutdown()

    warnings = []
    # P1-04: Kiểm tra va chạm (collision) hash giữa các profiles khác nhau.
    # Hai profile authentic (tắt noise) trùng hash là đúng thiết kế, không
    # phải collision cần cảnh báo.
    seen_canvas = {}
    seen_webgl = {}
    for result in results:
        ch = result.get("canvasHash")
        if ch:
            if ch in seen_canvas:
                if not result.get("canvas_noise_disabled"):
                    warnings.append(f"Canvas hash collision between profile '{result['name']}' and '{seen_canvas[ch]}'")
            else:
                seen_canvas[ch] = result["name"]

        wh = result.get("webglHash")
        if wh:
            if wh in seen_webgl:
                if not result.get("webgl_noise_disabled"):
                    warnings.append(
                        f"WebGL pixel hash collision between profile "
                        f"'{result['name']}' and '{seen_webgl[wh]}'"
                    )
            else:
                seen_webgl[wh] = result["name"]

    print("=" * 78)
    print("READ-ONLY ANTIDETECT FINGERPRINT AUDIT")
    print("=" * 78)
    for result in results:
        failed = [c for c in result["checks"] if not c["ok"]]
        print(f"\n[{result['name']}]  id={result['id']}  os={result['os']}")
        print(f"  checks: {len(result['checks']) - len(failed)}/{len(result['checks'])} passed")
        print(f"  canvas sha256: {result['canvasHash']}")
        print(f"  webgl  sha256: {result['webglHash']}")
        for item in failed:
            print(f"  FAIL {item['name']}: actual={item['actual']!r} expected={item['expected']!r}")

    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print(f"  [!] {w}")

    audit_summary = {
        "status": "FAIL" if failures else ("WARN" if warnings else "PASS"),
        "total_profiles": len(results),
        "failures_count": len(failures),
        "warnings_count": len(warnings),
        "failures": failures,
        "warnings": warnings,
        "results": results,
    }

    if args.json:
        json_output = json.dumps(audit_summary, indent=2, ensure_ascii=False)
        if args.json == "-":
            print("\nJSON REPORT:")
            print(json_output)
        else:
            try:
                with open(args.json, "w", encoding="utf-8") as f:
                    f.write(json_output)
                print(f"\n[AUDIT] Đã lưu báo cáo JSON tại: {args.json}")
            except Exception as e:
                print(f"\n[!] Không thể ghi file JSON {args.json}: {e}")

    print("\n" + "=" * 78)
    if failures:
        print(f"RESULT: FAIL ({len(failures)} mismatches)")
        for failure in failures:
            print(" - " + failure)
        return 1
    if warnings and args.strict:
        print(f"RESULT: FAIL STRICT MODE ({len(warnings)} warnings)")
        return 1
    if warnings:
        print(f"RESULT: WARN ({len(results)} profiles, {len(warnings)} warnings)")
        return 0
    print(f"RESULT: PASS ({len(results)} profiles, all checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
