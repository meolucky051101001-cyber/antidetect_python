r"""Real-profile detector baseline audit.

This is not a claim that a site "passed". It launches an existing profile via
BrowserLauncher, opens the detector pages through the real CDP browser, and
saves repeatable evidence: title, final URL, visible text, screenshot, basic
fingerprint snapshot, and challenge/CAPTCHA detection.

The default targets are CreepJS, BrowserLeaks and Pixelscan. Sites change and
may rate-limit automation, so a challenge is recorded as WARN, never as PASS.

Example (temporary data directory):

    $env:ANTIDETECT_DATA_DIR = "$env:TEMP\\antidetect-detector-audit"
    python tools/detector_audit.py --profile <id-or-name> `
        --screenshots audit_reports\screenshots --json audit_reports\detector.json

To smoke-test the harness without third-party sites:

    python tools/detector_audit.py --profile <id> --url https://example.com
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:  # script mode and ``python -m tools.detector_audit``
    from . import audit_common as common
except ImportError:  # pragma: no cover - the normal CLI path
    import audit_common as common

from antidetect.launcher import BrowserLauncher  # noqa: E402
from antidetect.storage import ProfileManager  # noqa: E402

DEFAULT_TARGETS: Tuple[Tuple[str, str], ...] = (
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/"),
    ("browserleaks-canvas", "https://browserleaks.com/canvas"),
    ("browserleaks-webgl", "https://browserleaks.com/webgl"),
    ("browserleaks-client-hints", "https://browserleaks.com/client-hints"),
    ("pixelscan", "https://pixelscan.net/"),
)

SNAPSHOT_JS = r"""
() => {
  const result = {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    webdriver: navigator.webdriver,
    languages: navigator.languages || [],
    timezone: null,
    screen: {
      width: screen.width,
      height: screen.height,
      availWidth: screen.availWidth,
      availHeight: screen.availHeight,
      colorDepth: screen.colorDepth
    },
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory || null,
    webglVendor: null,
    webglRenderer: null,
    canvasHash: null
  };
  try { result.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (error) {}
  try {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    if (context) {
      context.textBaseline = "top";
      context.font = "14px Arial";
      context.fillStyle = "#f60";
      context.fillRect(125, 1, 62, 20);
      context.fillStyle = "#069";
      context.fillText("Antidetect audit 123", 2, 15);
      context.fillStyle = "rgba(102, 204, 0, 0.7)";
      context.fillText("Antidetect audit 123", 4, 17);
      const data = canvas.toDataURL();
      let hash = 2166136261;
      for (let i = 0; i < data.length; i += 1) {
        hash ^= data.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
      }
      result.canvasHash = (hash >>> 0).toString(16);
    }
  } catch (error) {}
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    if (gl) {
      result.webglVendor = gl.getParameter(gl.VENDOR);
      result.webglRenderer = gl.getParameter(gl.RENDERER);
    }
  } catch (error) {}
  return result;
}
"""

_CHALLENGE_PATTERNS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "are you a robot",
    "complete the captcha",
    "solve the captcha",
    "attention required",
    "access denied",
    "unusual traffic",
    "cf-chl",
)


def parse_target(value: str) -> Tuple[str, str]:
    """Parse ``name=url`` or a bare URL into (name, url)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("target rỗng")
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        name = parsed.netloc or "target"
        return name, value
    if "=" in value:
        name, url = value.split("=", 1)
        name = name.strip() or "target"
        url = url.strip()
    else:
        name, url = value, value
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"URL không hợp lệ: {value}")
    return name, url


def detect_challenge(text: str, title: str = "") -> Optional[str]:
    haystack = f"{title}\n{text}".lower()
    matches = [pattern for pattern in _CHALLENGE_PATTERNS if pattern in haystack]
    return ", ".join(matches) if matches else None


def extract_detector_signals(text: str, url: str = "") -> Dict[str, Any]:
    """Extract a few human-readable signals; no site score is treated as truth."""
    signals: Dict[str, Any] = {}
    for key, pattern in (
        ("trust_score", r"trust score\D{0,16}(\d{1,3}(?:\.\d+)?)\s*%?"),
        ("bot_score", r"bot score\D{0,16}(\d{1,3}(?:\.\d+)?)\s*%?"),
        ("fp_id", r"(?:fingerprint|fp)\s*(?:id|hash)\D{0,12}([0-9a-f]{8,})"),
    ):
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            signals[key] = match.group(1)
    lower = (text or "").lower()
    if "pixelscan.net" in url.lower():
        if "fingerprint is inconsistent" in lower:
            signals["pixelscan_fingerprint"] = "inconsistent"
        elif "fingerprint is consistent" in lower:
            signals["pixelscan_fingerprint"] = "consistent"
        if "masking detected" in lower:
            signals["pixelscan_masking"] = "detected"
        if "no automated behavior detected" in lower:
            signals["pixelscan_bot_check"] = "no automated behavior detected"
    if "creepjs" in url.lower():
        for key, pattern in (
            ("creepjs_like_headless", r"(\d+%)\s*like headless"),
            ("creepjs_headless_detected", r"(\d+%)\s*headless"),
            ("creepjs_stealth", r"(\d+%)\s*stealth"),
        ):
            match = re.search(pattern, text or "", re.IGNORECASE)
            if match:
                signals[key] = match.group(1)
    return signals


def build_targets(raw_targets: Optional[List[str]]) -> List[Tuple[str, str]]:
    if not raw_targets:
        return list(DEFAULT_TARGETS)
    targets: List[Tuple[str, str]] = []
    for raw in raw_targets:
        try:
            targets.append(parse_target(raw))
        except ValueError as exc:
            print(f"[!] Bỏ qua target {raw!r}: {exc}", file=sys.stderr)
    return targets


def maybe_start_detector(page: Any, url: str) -> Optional[str]:
    """Start a detector that exposes an explicit landing-page scan button."""
    if "pixelscan.net" not in url.lower():
        return None
    try:
        locator = page.locator("a, button").filter(
            has_text=re.compile(r"scan my browser", re.IGNORECASE)
        ).first
        if locator.count():
            locator.click(timeout=8_000, force=True)
            page.wait_for_timeout(10_000)
            return "clicked Scan My Browser"
    except Exception as exc:  # noqa: BLE001 - detector UI can change
        return f"scan button click failed: {type(exc).__name__}: {exc}"
    return "scan button not found"


def wait_for_detector_result(page: Any, url: str) -> None:
    """Wait for known async detector UIs without hard-coding their scores."""
    lower_url = url.lower()
    if "creepjs" in lower_url:
        try:
            page.wait_for_function(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    return text.includes('FP ID:')
                        && text.includes('Headless')
                        && !text.includes('Computing...');
                }""",
                timeout=30_000,
            )
        except Exception:
            pass
        page.wait_for_timeout(2_000)
    elif "pixelscan.net" in lower_url:
        try:
            page.wait_for_function(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    const lower = text.toLowerCase();
                    return text.includes('Your Browser Fingerprint is')
                        && !lower.includes('scanning');
                }""",
                timeout=25_000,
            )
        except Exception:
            pass
        page.wait_for_timeout(1_500)


def audit_target(
    page: Any,
    name: str,
    url: str,
    args: argparse.Namespace,
    slug: str,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "name": name,
        "url": url,
        "title": None,
        "final_url": None,
        "status": "UNKNOWN",
        "error": None,
        "challenge": None,
        "action": None,
        "text": "",
        "text_truncated": False,
        "signals": {},
        "fingerprint": None,
        "screenshot": None,
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        page.wait_for_timeout(args.wait * 1000)
        record["action"] = maybe_start_detector(page, url)
        wait_for_detector_result(page, url)
        record["title"] = page.title()
        record["final_url"] = page.url
        try:
            body = page.locator("body").inner_text(timeout=10_000)
        except Exception as exc:  # noqa: BLE001 - page can be a download/blank
            body = ""
            record["error"] = f"Không đọc được body: {type(exc).__name__}: {exc}"
        record["text"] = body[:20_000]
        record["text_truncated"] = len(body) > len(record["text"])
        record["challenge"] = detect_challenge(body, record["title"] or "")
        record["signals"] = extract_detector_signals(body, url)
        record["fingerprint"] = page.evaluate(SNAPSHOT_JS)
        if args.screenshots:
            os.makedirs(args.screenshots, exist_ok=True)
            target = os.path.join(args.screenshots, f"{slug}_{common.safe_slug(name)}.png")
            page.screenshot(path=target, full_page=True)
            record["screenshot"] = os.path.abspath(target)
        if record["challenge"]:
            record["status"] = "CHALLENGE"
        elif record["error"]:
            record["status"] = "WARN"
        else:
            record["status"] = "LOADED"
    except Exception as exc:  # noqa: BLE001 - one detector must not stop the batch
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["status"] = "ERROR"
        if args.screenshots:
            try:
                os.makedirs(args.screenshots, exist_ok=True)
                target = os.path.join(args.screenshots, f"{slug}_{common.safe_slug(name)}_error.png")
                page.screenshot(path=target, full_page=True)
                record["screenshot"] = os.path.abspath(target)
            except Exception:
                pass
    return record


def audit_profile(
    launcher: BrowserLauncher,
    profile: Any,
    playwright: Any,
    args: argparse.Namespace,
    targets: List[Tuple[str, str]],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "profile": common.profile_summary(profile),
        "started_at": common.utc_now(),
        "launch": None,
        "targets": [],
        "warnings": [],
        "failures": [],
        "cleanup": None,
        "status": "UNKNOWN",
    }
    browser = None
    was_running = False
    try:
        was_running, launch_result, port = common.start_profile(launcher, profile)
        result["launch"] = {
            key: launch_result.get(key)
            for key in (
                "success",
                "port",
                "pid",
                "cdp_ready",
                "page_ready",
                "stealth_injected",
                "emulation_verified",
                "configuration_verified",
                "verification",
                "warning",
            )
            if key in launch_result
        }
        if launch_result.get("warning"):
            result["warnings"].append(str(launch_result["warning"]))
        if launch_result.get("configuration_verified") is False:
            result["warnings"].append("Launcher không xác minh được cấu hình fingerprint")

        browser = common.connect_browser(playwright, port)
        context, page = common.get_default_page(browser)
        page.set_default_timeout(args.timeout * 1000)
        slug = common.safe_slug(profile.name)

        for name, url in targets:
            print(f"    -> {name}: {url}")
            record = audit_target(page, name, url, args, slug)
            result["targets"].append(record)
            if record["status"] == "ERROR":
                result["failures"].append(f"{name}: {record['error']}")
            elif record["status"] == "CHALLENGE":
                result["warnings"].append(
                    f"{name}: phát hiện challenge/CAPTCHA ({record['challenge']})"
                )
            elif record["status"] == "WARN":
                result["warnings"].append(f"{name}: {record['error']}")
            signals = record.get("signals") or {}
            if signals.get("pixelscan_fingerprint") == "inconsistent":
                result["warnings"].append(f"{name}: Pixelscan báo fingerprint inconsistent")
            if signals.get("pixelscan_masking") == "detected":
                result["warnings"].append(f"{name}: Pixelscan báo masking detected")
            if signals.get("creepjs_like_headless") not in (None, "0%"):
                result["warnings"].append(
                    f"{name}: CreepJS báo {signals['creepjs_like_headless']} like headless"
                )

        if result["failures"]:
            result["status"] = "FAIL"
        elif result["warnings"]:
            result["status"] = "WARN"
        else:
            result["status"] = "PASS"
    except Exception as exc:  # noqa: BLE001
        result["failures"].append(f"{type(exc).__name__}: {exc}")
        result["status"] = "FAIL"
    finally:
        result["cleanup"] = common.finish_profile(launcher, profile, was_running, browser)
        result["finished_at"] = common.utc_now()
    return result


def print_profile_result(result: Dict[str, Any]) -> None:
    profile = result["profile"]
    print("")
    print(f"=== {profile['name']} ({profile['id']}) ===")
    for target in result.get("targets", []):
        print(
            f"  {target['name']}: {target['status']} | "
            f"{target.get('title') or 'không có title'} | {target.get('final_url') or target['url']}"
        )
        if target.get("challenge"):
            print(f"    challenge: {target['challenge']}")
        if target.get("action"):
            print(f"    action: {target['action']}")
        if target.get("signals"):
            print(f"    signals: {json.dumps(target['signals'], ensure_ascii=False)}")
        if target.get("screenshot"):
            print(f"    screenshot: {target['screenshot']}")
        if target.get("error"):
            print(f"    error: {target['error']}")
    for item in result.get("failures", []):
        print(f"  FAIL: {item}")
    for item in result.get("warnings", []):
        print(f"  WARN: {item}")
    if result.get("cleanup") and not result["cleanup"].get("success"):
        print(f"  WARN cleanup: {result['cleanup'].get('error')}")
    print(f"  RESULT: {result.get('status')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit baseline detector trên profile thật qua BrowserLauncher + CDP."
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="ID hoặc tên profile; có thể lặp lại. Mặc định: tất cả profile.",
    )
    parser.add_argument(
        "--url",
        action="append",
        help="Target tùy chỉnh: URL hoặc name=URL; có thể lặp lại. Mặc định: bộ detector baseline.",
    )
    parser.add_argument("--json", help="Ghi báo cáo JSON đầy đủ vào đường dẫn này.")
    parser.add_argument(
        "--screenshots",
        help="Thư mục lưu screenshot từng trang (tùy chọn).",
    )
    parser.add_argument("--wait", type=float, default=8.0, help="Số giây chờ JS detector (mặc định: 8).")
    parser.add_argument("--timeout", type=float, default=60.0, help="Timeout điều hướng mỗi trang (mặc định: 60s).")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Trả exit code 1 nếu có WARN hoặc FAIL, không chỉ FAIL.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    common.print_data_notice()
    manager = ProfileManager()
    profiles, missing = common.resolve_profiles(manager, args.profile)
    if missing:
        print("[!] Không tìm thấy profile: " + ", ".join(missing), file=sys.stderr)
    if not profiles:
        print("[!] Không có profile nào để audit.", file=sys.stderr)
        return 2

    targets = build_targets(args.url)
    if not targets:
        print("[!] Không có target hợp lệ.", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] Thiếu Playwright. Chạy: pip install -r requirements.txt", file=sys.stderr)
        return 2

    launcher = BrowserLauncher(manager)
    results = []
    with sync_playwright() as playwright:
        for profile in profiles:
            print(f"[*] Đang audit {profile.name} ({profile.id}) ...")
            results.append(audit_profile(launcher, profile, playwright, args, targets))

    for result in results:
        print_profile_result(result)

    summary = {
        "generated_at": common.utc_now(),
        "data_dir": common.data_dir(),
        "targets": [{"name": name, "url": url} for name, url in targets],
        "profiles": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item["status"] == "PASS"),
            "warn": sum(1 for item in results if item["status"] == "WARN"),
            "fail": sum(1 for item in results if item["status"] == "FAIL"),
        },
    }
    if args.json:
        common.write_json(args.json, summary)
        print(f"\n[*] Đã ghi JSON: {os.path.abspath(args.json)}")

    print(
        "\nSUMMARY: "
        f"{summary['summary']['pass']} PASS, "
        f"{summary['summary']['warn']} WARN, "
        f"{summary['summary']['fail']} FAIL"
    )
    if summary["summary"]["fail"]:
        return 1
    if args.strict and summary["summary"]["warn"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
