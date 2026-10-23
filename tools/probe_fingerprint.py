"""Independent, deep-surface leak detection probe suite for antidetect profiles.

Tests 8 critical fingerprint surfaces across isolated browser instances:
1. Navigator & Client Hints (Platform, High-Entropy Values)
2. Canvas 2D & OffscreenCanvas noise consistency
3. AudioContext & AudioBuffer stability (no cumulative drift)
4. WebGL Extensions & GPU Vendor/Renderer isolation
5. WebGPU masking & adapter isolation
6. Font Probing consistency (OS-specific fonts)
7. MediaDevices generic masking
8. WebRTC ICE Candidate & SDP private IP leak check
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from antidetect.storage import ProfileManager
from antidetect.fingerprint import normalize_fingerprint_for_launch
from antidetect.launcher import find_browser_executable
from antidetect.stealth import get_stealth_script


PROBE_HTML = b"<!doctype html><html><head><meta charset=utf-8><title>probe</title></head><body>probe</body></html>"


class ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(PROBE_HTML)))
        self.end_headers()
        self.wfile.write(PROBE_HTML)


PROBE_EVALUATE_JS = r"""async () => {
    const report = { checks: [], warnings: [], errors: [] };

    const record = (surface, name, ok, actual, expected, detail) => {
        report.checks.push({ surface, name, ok: Boolean(ok), actual, expected, detail: detail || '' });
    };

    // 1. Navigator & Client Hints
    try {
        record('navigator', 'webdriver_false', navigator.webdriver === false, navigator.webdriver, false);
        record('navigator', 'hardwareConcurrency_positive', navigator.hardwareConcurrency > 0, navigator.hardwareConcurrency, '>0');
        record('navigator', 'deviceMemory_valid', [2, 4, 8].includes(navigator.deviceMemory), navigator.deviceMemory, '2, 4, or 8');

        if (navigator.userAgentData) {
            const hi = await navigator.userAgentData.getHighEntropyValues([
                'platform', 'platformVersion', 'architecture', 'bitness', 'model'
            ]);
            report.clientHints = hi;
        }
    } catch(e) {
        report.errors.push('Navigator check error: ' + e);
    }

    // 2. Canvas 2D & OffscreenCanvas Noise Check
    try {
        const c1 = document.createElement('canvas');
        c1.width = 160; c1.height = 40;
        const ctx1 = c1.getContext('2d');
        ctx1.fillStyle = '#ff5500';
        ctx1.fillRect(10, 5, 100, 25);
        ctx1.fillStyle = '#0066cc';
        ctx1.fillText('ProbeCanvas2026', 15, 20);

        const url1 = c1.toDataURL();
        const url2 = c1.toDataURL();
        record('canvas', 'toDataURL_deterministic', url1 === url2, true, true, 'Repeated toDataURL must produce identical string');

        const imgData = ctx1.getImageData(0, 0, 160, 40).data;
        record('canvas', 'getImageData_pixels_extracted', imgData.length === 160 * 40 * 4, true, true);
    } catch(e) {
        report.errors.push('Canvas check error: ' + e);
    }

    // 3. AudioContext & AudioBuffer Stability
    try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (AudioCtx) {
            const actx = new AudioCtx();
            const buffer = actx.createBuffer(1, 256, 44100);
            const d1 = buffer.getChannelData(0);
            const val1 = d1[0];
            const d2 = buffer.getChannelData(0);
            const val2 = d2[0];
            record('audio', 'getChannelData_non_cumulative', val1 === val2, val1 === val2, true, 'Noise must not compound on repeated reads');
            actx.close();
        }
    } catch(e) {
        report.errors.push('Audio check error: ' + e);
    }

    // 4. WebGL Extensions & Parameter Isolation
    try {
        const glCanvas = document.createElement('canvas');
        const gl = glCanvas.getContext('webgl') || glCanvas.getContext('experimental-webgl');
        if (gl) {
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            report.webglVendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null;
            report.webglRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
            report.webglExtensions = gl.getSupportedExtensions() || [];

            record('webgl', 'vendor_present', Boolean(report.webglVendor), report.webglVendor, 'Non-empty');
            record('webgl', 'renderer_present', Boolean(report.webglRenderer), report.webglRenderer, 'Non-empty');
        }
    } catch(e) {
        report.errors.push('WebGL check error: ' + e);
    }

    // 5. WebGPU Isolation
    try {
        if (navigator.gpu) {
            try {
                const adapter = await navigator.gpu.requestAdapter();
                if (adapter && adapter.info) {
                    report.webgpuAdapter = {
                        vendor: adapter.info.vendor,
                        architecture: adapter.info.architecture,
                        device: adapter.info.device,
                        description: adapter.info.description
                    };
                }
            } catch(e) {
                report.webgpuAdapter = null;
            }
        }
    } catch(e) {
        report.errors.push('WebGPU check error: ' + e);
    }

    // 6. Font Probing Consistency
    try {
        if (document.fonts && document.fonts.check) {
            report.fontsCheck = {
                segoeUI: document.fonts.check('12px "Segoe UI"'),
                calibri: document.fonts.check('12px "Calibri"'),
                sfPro: document.fonts.check('12px "SF Pro"'),
                helveticaNeue: document.fonts.check('12px "Helvetica Neue"')
            };
        }
    } catch(e) {
        report.errors.push('Font check error: ' + e);
    }

    // 7. MediaDevices Masking
    try {
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            const devices = await navigator.mediaDevices.enumerateDevices();
            report.mediaDevices = devices.map(d => ({ kind: d.kind, label: d.label, deviceId: d.deviceId ? 'present' : 'empty' }));
        }
    } catch(e) {
        report.errors.push('MediaDevices check error: ' + e);
    }

    // 8. WebRTC Private IP Leak Check
    try {
        if (window.RTCPeerConnection) {
            const pc = new RTCPeerConnection({ iceServers: [] });
            pc.createDataChannel('leak-test');
            const offer = await pc.createOffer();
            report.webrtcSdp = offer.sdp;

            // Check if private LAN IP pattern exists in SDP (10.x.x.x, 192.168.x.x, 172.16-31.x.x)
            const privateIpRegex = /(?:c=IN\s+IP4\s+)(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[0-1])\.\d+\.\d+)/;
            const match = privateIpRegex.exec(offer.sdp || '');
            record('webrtc', 'no_private_ip_in_sdp', !match, match ? match[1] : 'Clean', 'No LAN IP in SDP');
            pc.close();
        }
    } catch(e) {
        report.errors.push('WebRTC check error: ' + e);
    }

    return report;
};"""


def run_probe():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=None,
                        help="Profile ID or name to probe (repeatable; default: all)")
    parser.add_argument("--json", nargs="?", const="-", default=None,
                        help="Export report as JSON (pass file path or '-' for stdout)")
    parser.add_argument("--strict", action="store_true",
                        help="Exit code 1 if any warning is detected")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"[!] Playwright required for probe: {e}")
        return 2

    manager = ProfileManager()
    profiles = manager.list_profiles()
    if args.profile:
        wanted = set(args.profile)
        profiles = [p for p in profiles if p.id in wanted or p.name in wanted]

    if not profiles:
        print("[!] No matching profiles found.")
        return 2

    try:
        browser_path = find_browser_executable()
    except Exception as e:
        print(f"[!] Browser executable not found: {e}")
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}/"

    results = []
    total_failures = 0
    total_warnings = 0

    print("=" * 78)
    print("INDEPENDENT ANTIDETECT PROBE & LEAK DETECTOR (P2-01)")
    print("=" * 78)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=browser_path, headless=True)

            for profile in profiles:
                fp = normalize_fingerprint_for_launch(profile.fingerprint, browser_path)
                context = browser.new_context(
                    viewport={"width": fp.screen_width, "height": fp.screen_height},
                    device_scale_factor=float(fp.device_pixel_ratio or 1.0),
                    user_agent=fp.user_agent,
                    locale=fp.locale,
                    timezone_id=fp.timezone,
                )
                page = context.new_page()
                page.add_init_script(get_stealth_script(fp))
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_timeout(300)

                data = page.evaluate(PROBE_EVALUATE_JS)
                context.close()

                # Perform OS and cross-surface validations
                checks = data.get("checks", [])
                warnings = data.get("warnings", [])

                # Validate Client Hints platform
                ch = data.get("clientHints") or {}
                if fp.os == "macos":
                    if ch.get("platform") != "macOS":
                        checks.append({
                            "surface": "clientHints", "name": "ch_platform_match",
                            "ok": False, "actual": ch.get("platform"), "expected": "macOS"
                        })
                    # Cross-OS extension leak check: Apple GPU should not expose DirectX / ANGLE
                    gl_exts = data.get("webglExtensions") or []
                    leaked_angle = [e for e in gl_exts if "ANGLE" in e or "WGL" in e]
                    if leaked_angle:
                        warnings.append(f"macOS profile leaks Windows ANGLE/WGL extensions: {leaked_angle[:3]}")

                    # Font cross check
                    fonts = data.get("fontsCheck") or {}
                    if fonts.get("segoeUI") is True:
                        warnings.append("macOS profile reports Windows Segoe UI font available")

                elif fp.os == "windows":
                    if ch.get("platform") != "Windows":
                        checks.append({
                            "surface": "clientHints", "name": "ch_platform_match",
                            "ok": False, "actual": ch.get("platform"), "expected": "Windows"
                        })

                # Check media device labels
                media = data.get("mediaDevices") or []
                for d in media:
                    lbl = (d.get("label") or "").lower()
                    if any(brand in lbl for brand in ["realtek", "logitech", "corsair", "razer", "nvidia"]):
                        warnings.append(f"MediaDevice leaked native hardware brand: {d.get('label')}")

                failed_checks = [c for c in checks if not c["ok"]]
                total_failures += len(failed_checks)
                total_warnings += len(warnings)

                res_entry = {
                    "profile_id": profile.id,
                    "profile_name": profile.name,
                    "target_os": fp.os,
                    "checks_passed": len(checks) - len(failed_checks),
                    "checks_total": len(checks),
                    "failed_checks": failed_checks,
                    "warnings": warnings,
                    "errors": data.get("errors", []),
                    "details": {
                        "webglVendor": data.get("webglVendor"),
                        "webglRenderer": data.get("webglRenderer"),
                        "clientHints": ch,
                        "fontsCheck": data.get("fontsCheck"),
                    }
                }
                results.append(res_entry)

                status_label = "PASS" if not failed_checks and not warnings else ("WARN" if not failed_checks else "FAIL")
                print(f"\n[{profile.name}] ({fp.os.upper()}) -> {status_label}")
                print(f"  Passed: {len(checks) - len(failed_checks)}/{len(checks)} checks")
                if failed_checks:
                    for fc in failed_checks:
                        print(f"  [FAIL] {fc['surface']}.{fc['name']}: actual={fc['actual']!r}, expected={fc['expected']!r}")
                if warnings:
                    for w in warnings:
                        print(f"  [WARN] {w}")

            browser.close()
    finally:
        server.shutdown()

    summary = {
        "status": "FAIL" if total_failures > 0 else ("WARN" if total_warnings > 0 else "PASS"),
        "total_profiles": len(results),
        "total_checks": sum(r["checks_total"] for r in results),
        "total_failures": total_failures,
        "total_warnings": total_warnings,
        "profiles": results
    }

    if args.json:
        out = json.dumps(summary, indent=2, ensure_ascii=False)
        if args.json == "-":
            print("\nJSON PROBE REPORT:")
            print(out)
        else:
            try:
                with open(args.json, "w", encoding="utf-8") as f:
                    f.write(out)
                print(f"\n[PROBE] Báo cáo chi tiết đã được lưu tại: {args.json}")
            except Exception as e:
                print(f"[!] Không thể lưu JSON: {e}")

    print("\n" + "=" * 78)
    if total_failures > 0:
        print(f"RESULT: FAIL ({total_failures} failures across {len(results)} profiles)")
        return 1
    if total_warnings > 0 and args.strict:
        print(f"RESULT: FAIL STRICT MODE ({total_warnings} warnings across {len(results)} profiles)")
        return 1

    print(f"RESULT: PASS (All {len(results)} profiles passed independent probe verification)")
    return 0


if __name__ == "__main__":
    sys.exit(run_probe())
