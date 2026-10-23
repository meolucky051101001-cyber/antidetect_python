"""Real-profile IP / WebRTC / DNS leak audit.

The audit launches an existing Antidetect profile through BrowserLauncher,
connects to the real Chrome CDP endpoint, and measures what the page can see:

* public IPv4/IPv6 as seen by the browser (proxy exit address when enabled);
* the host's direct public addresses for comparison (server-side, no proxy);
* WebRTC ICE candidates and SDP addresses, classified as local, public or mDNS;
* an optional best-effort bash.ws DNS leak page capture for review.

It never edits profiles.json or profile fingerprint data. Use a temporary
``ANTIDETECT_DATA_DIR`` when you do not want to open a real profile, for
example:

    $env:ANTIDETECT_DATA_DIR = "$env:TEMP\\antidetect-leak-audit"
    python tools/leak_audit.py --profile <id-or-name> --json audit.json

WebRTC local/private addresses and, with a proxy enabled, browser/WebRTC
addresses matching the host's direct IPs are failures. The bash.ws DNS result
is ``CLEAN``, ``LEAK``, ``REVIEW`` or ``MANUAL``; its ASN evidence is kept for
review because the expected DNS egress depends on the proxy provider.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:  # script mode and ``python -m tools.leak_audit``
    from . import audit_common as common
except ImportError:  # pragma: no cover - the normal CLI path
    import audit_common as common

from antidetect.launcher import BrowserLauncher  # noqa: E402
from antidetect.storage import ProfileManager  # noqa: E402

IP_ENDPOINTS = (
    ("ipify", "https://api.ipify.org?format=json"),
    ("ipify64", "https://api64.ipify.org?format=json"),
    ("v6ipify", "https://v6.ipify.org?format=json"),
    ("ifconfig", "https://ifconfig.me/ip"),
)
DNS_URL = "https://bash.ws/dnsleak/test/"
WEBRTC_TIMEOUT_MS = 6000

WEBRTC_JS = r"""
async () => {
  const out = {
    supported: Boolean(window.RTCPeerConnection),
    candidates: [],
    sdp: "",
    gatheringState: "",
    error: null
  };
  if (!out.supported) return out;
  let pc = null;
  try {
    pc = new RTCPeerConnection({
      iceServers: [{urls: ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}],
      iceCandidatePoolSize: 0
    });
    pc.onicecandidate = (event) => {
      if (event.candidate && event.candidate.candidate) {
        out.candidates.push(event.candidate.candidate);
      }
    };
    try { pc.createDataChannel("audit"); } catch (e) {}
    await pc.setLocalDescription(await pc.createOffer());
    out.gatheringState = pc.iceGatheringState;
    await new Promise((resolve) => {
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        resolve();
      };
      pc.addEventListener("icegatheringstatechange", () => {
        out.gatheringState = pc.iceGatheringState;
        if (pc.iceGatheringState === "complete") finish();
      });
      setTimeout(finish, 6000);
    });
    out.sdp = (pc.localDescription && pc.localDescription.sdp) || "";
    out.gatheringState = pc.iceGatheringState;
  } catch (error) {
    out.error = String(error);
  } finally {
    try { if (pc) pc.close(); } catch (error) {}
  }
  return out;
}
"""

_CANDIDATE_RE = re.compile(
    r"candidate:\S+\s+\d+\s+\w+\s+\d+\s+([^\s]+)\s+\d+\s+typ\s+(\w+)",
    re.IGNORECASE,
)
_SDP_ADDRESS_RE = re.compile(r"^c=IN\s+IP[46]\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F]{0,4}\b")


def normalize_ip(address: str) -> Optional[str]:
    """Return a compressed canonical IP, or None for an mDNS/invalid value."""
    value = (address or "").strip().strip("[]")
    if not value or value.lower().endswith(".local") or "%" in value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def classify_ip(address: str) -> str:
    """Classify an ICE/SDP address as mdns, local, public, invalid or other."""
    value = (address or "").strip().strip("[]")
    if value.lower().endswith(".local") or value.lower().endswith(".local."):
        return "mdns"
    normalized = normalize_ip(value)
    if not normalized:
        return "invalid"
    ip = ipaddress.ip_address(normalized)
    if ip.is_unspecified:
        # Chrome puts c=IN IP4 0.0.0.0 in the SDP when a policy blocks all
        # ICE candidates.  That is a "no address" marker, not a LAN leak.
        return "unspecified"
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
    ):
        return "local"
    if ip.is_global:
        return "public"
    return "other"


def extract_webrtc_addresses(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract candidate/SDP addresses and their candidate type."""
    found: List[Dict[str, str]] = []
    seen = set()

    def add(address: str, candidate_type: str, source: str) -> None:
        address = (address or "").strip()
        if not address:
            return
        key = (address, candidate_type)
        if key in seen:
            return
        seen.add(key)
        found.append(
            {
                "address": address,
                "type": candidate_type or "unknown",
                "source": source,
                "kind": classify_ip(address),
            }
        )

    for raw in payload.get("candidates") or []:
        match = _CANDIDATE_RE.search(str(raw))
        if match:
            add(match.group(1), match.group(2), "ice-candidate")
        else:
            for address in _IPV4_RE.findall(str(raw)):
                add(address, "unknown", "ice-candidate")

    sdp = str(payload.get("sdp") or "")
    for match in _SDP_ADDRESS_RE.finditer(sdp):
        add(match.group(1), "sdp-connection", "sdp")

    return found


def summarize_webrtc(payload: Dict[str, Any]) -> Dict[str, Any]:
    addresses = extract_webrtc_addresses(payload)
    groups: Dict[str, List[str]] = {
        "local": [],
        "public": [],
        "mdns": [],
        "unspecified": [],
        "other": [],
        "invalid": [],
    }
    for item in addresses:
        groups.setdefault(item["kind"], []).append(item["address"])
    return {
        "supported": bool(payload.get("supported")),
        "gathering_state": payload.get("gatheringState"),
        "candidate_count": len(payload.get("candidates") or []),
        "sdp_length": len(str(payload.get("sdp") or "")),
        "error": payload.get("error"),
        "addresses": addresses,
        "local": sorted(set(groups.get("local", []))),
        "public": sorted(set(groups.get("public", []))),
        "mdns": sorted(set(groups.get("mdns", []))),
        "unspecified": sorted(set(groups.get("unspecified", []))),
        "other": sorted(set(groups.get("other", []))),
        "invalid": sorted(set(groups.get("invalid", []))),
    }


def parse_ip_text(text: str) -> Optional[str]:
    """Extract the first public/private IP address from an echo endpoint."""
    text = text or ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("ip", "query", "origin"):
                value = data.get(key)
                if isinstance(value, str) and normalize_ip(value.split(",")[0]):
                    return normalize_ip(value.split(",")[0])
    except (ValueError, TypeError):
        pass

    for match in _IPV4_RE.findall(text):
        normalized = normalize_ip(match)
        if normalized and classify_ip(normalized) == "public":
            return normalized
    for match in _IPV6_RE.findall(text):
        normalized = normalize_ip(match)
        if normalized and classify_ip(normalized) == "public":
            return normalized
    return None


def fetch_direct_ip() -> Dict[str, Any]:
    """Query host IP(s) with environment proxies explicitly disabled.

    IPv4 and IPv6 are collected separately because a WebRTC leak can expose
    only the host IPv6 while the usual echo endpoint returns IPv4.
    """
    session = requests.Session()
    session.trust_env = False
    errors = []
    found: List[str] = []
    sources: List[str] = []
    for name, url in IP_ENDPOINTS:
        try:
            response = session.get(
                url,
                timeout=10,
                proxies={"http": None, "https": None},
                headers={"User-Agent": "Antidetect-Leak-Audit/1.0"},
            )
            response.raise_for_status()
            address = parse_ip_text(response.text)
            if address and address not in found:
                found.append(address)
                sources.append(name)
            elif not address:
                errors.append(f"{name}: không tìm thấy IP trong phản hồi")
        except Exception as exc:  # noqa: BLE001 - network failures are audit evidence
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return {
        "ip": found[0] if found else None,
        "ips": found,
        "sources": sources,
        "source": sources[0] if sources else None,
        "error": "; ".join(errors) if errors else None,
    }


def fetch_browser_ip(context: Any) -> Dict[str, Any]:
    """Ask external echo endpoints from inside the real browser context."""
    page = context.new_page()
    errors = []
    found: List[str] = []
    sources: List[str] = []
    try:
        for name, url in IP_ENDPOINTS:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                body = page.locator("body").inner_text(timeout=5_000)
                address = parse_ip_text(body)
                if address and address not in found:
                    found.append(address)
                    sources.append(name)
                elif not address:
                    errors.append(f"{name}: không tìm thấy IP trong phản hồi")
            except Exception as exc:  # noqa: BLE001 - page/network failures
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        try:
            page.close()
        except Exception:
            pass
    return {
        "ip": found[0] if found else None,
        "ips": found,
        "sources": sources,
        "source": sources[0] if sources else None,
        "error": "; ".join(errors) if errors else None,
    }


def classify_dns_verdict(text: str, clicked: bool) -> str:
    """Classify bash.ws output without treating a landing page as evidence."""
    if not clicked:
        return "MANUAL"
    lower = (text or "").lower()
    if "completed in" not in lower:
        return "MANUAL"
    if (
        "dns may be leaking" in lower
        or "dns is leaking" in lower
        or "different isps for dns" in lower
        or "dns leak detected" in lower
    ):
        return "LEAK"
    if "[ok] dns is not leaking" in lower or "dns is not leaking" in lower:
        return "CLEAN"
    if "dns server" in lower:
        return "REVIEW"
    return "MANUAL"


def extract_asns(text: str) -> List[str]:
    """Extract deduplicated ASN labels without swallowing the explanatory sentence."""
    found: List[str] = []
    for number, name in re.findall(
        r"\bAS(\d+)\s+([A-Z][A-Za-z0-9&.,'() -]{1,80})", text or ""
    ):
        clean_name = name.strip(" .,")
        label = f"AS{number} {clean_name}".strip()
        if label and label not in found:
            found.append(label)
    return found[:20]


def run_dns_probe(context: Any, screenshot_dir: Optional[str], slug: str) -> Dict[str, Any]:
    """Best-effort bash.ws DNS test; raw evidence is kept for review."""
    result: Dict[str, Any] = {
        "url": DNS_URL,
        "clicked": False,
        "error": None,
        "verdict": "MANUAL",
        "text": "",
        "result_text": "",
        "screenshot": None,
        "ip_candidates": [],
        "asns": [],
    }
    page = context.new_page()
    try:
        page.goto(DNS_URL, wait_until="domcontentloaded", timeout=45_000)
        clicked = False
        try:
            # The page may hide the run button while its CSS/fonts settle, so a
            # JavaScript click is more reliable than an actionability click.
            clicked = bool(
                page.evaluate(
                    """() => {
                        const button = document.querySelector('button[data-action="run-test"]');
                        if (!button) return false;
                        button.click();
                        return true;
                    }"""
                )
            )
        except Exception as exc:  # noqa: BLE001 - page layout can change
            result["error"] = f"Không bấm được nút chạy DNS bằng JS: {type(exc).__name__}: {exc}"
        if not clicked:
            try:
                button = page.locator('button[data-action="run-test"]').first
                button.click(timeout=5_000, force=True)
                clicked = True
            except Exception as exc:  # noqa: BLE001 - page layout can change
                if not result["error"]:
                    result["error"] = (
                        f"Không bấm được nút chạy DNS: {type(exc).__name__}: {exc}"
                    )
        result["clicked"] = clicked

        if clicked:
            page.wait_for_timeout(15_000)
        try:
            result["result_text"] = page.locator("#cmd").inner_text(timeout=5_000)[:20_000]
        except Exception:
            result["result_text"] = ""
        body = page.locator("body").inner_text(timeout=5_000)
        result["text"] = body[:20_000]
        result["verdict"] = classify_dns_verdict(body, clicked)
        result["asns"] = extract_asns(body)
        ips = []
        for match in _IPV4_RE.findall(body):
            normalized = normalize_ip(match)
            if normalized and normalized not in ips:
                ips.append(normalized)
        result["ip_candidates"] = ips[:30]
        if screenshot_dir:
            os.makedirs(screenshot_dir, exist_ok=True)
            target = os.path.join(screenshot_dir, f"{slug}_dns.png")
            page.screenshot(path=target, full_page=True)
            result["screenshot"] = os.path.abspath(target)
    except Exception as exc:  # noqa: BLE001 - DNS page is optional evidence
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            page.close()
        except Exception:
            pass
    return result


def page_fingerprint_snapshot(page: Any) -> Dict[str, Any]:
    try:
        return page.evaluate(
            r"""
            () => {
              const result = {
                webdriver: navigator.webdriver,
                userAgent: navigator.userAgent,
                platform: navigator.platform,
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
                deviceMemory: navigator.deviceMemory || null
              };
              try {
                result.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
              } catch (error) {}
              return result;
            }
            """
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def matching_ips(left: Any, right: Any) -> List[str]:
    right_set = {normalize_ip(item) for item in (right or []) if item}
    result = []
    for item in left or []:
        normalized = normalize_ip(item)
        if normalized and normalized in right_set and normalized not in result:
            result.append(normalized)
    return result


def audit_profile(
    launcher: BrowserLauncher,
    profile: Any,
    playwright: Any,
    args: argparse.Namespace,
    direct_ip: Dict[str, Any],
) -> Dict[str, Any]:
    profile_result: Dict[str, Any] = {
        "profile": common.profile_summary(profile),
        "started_at": common.utc_now(),
        "launch": None,
        "direct_ip": direct_ip,
        "browser_ip": None,
        "fingerprint_snapshot": None,
        "webrtc": None,
        "dns": None,
        "failures": [],
        "warnings": [],
        "cleanup": None,
        "status": "UNKNOWN",
    }

    browser = None
    was_running = False
    try:
        was_running, launch_result, port = common.start_profile(launcher, profile)
        profile_result["launch"] = {
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
            profile_result["warnings"].append(str(launch_result["warning"]))
        if launch_result.get("configuration_verified") is False:
            profile_result["warnings"].append("Launcher không xác minh được cấu hình fingerprint")

        browser = common.connect_browser(playwright, port)
        context, page = common.get_default_page(browser)
        profile_result["fingerprint_snapshot"] = page_fingerprint_snapshot(page)
        snapshot = profile_result["fingerprint_snapshot"] or {}
        if snapshot.get("webdriver") is True:
            profile_result["failures"].append("navigator.webdriver=true trên page audit")

        profile_result["browser_ip"] = fetch_browser_ip(context)
        webrtc_raw = page.evaluate(WEBRTC_JS)
        profile_result["webrtc"] = summarize_webrtc(webrtc_raw if isinstance(webrtc_raw, dict) else {})
        webrtc = profile_result["webrtc"]

        proxy_enabled = bool(profile.proxy and profile.proxy.enabled)
        webrtc_mode = getattr(profile.fingerprint, "webrtc_mode", "auto")
        proxy_blocks_udp = proxy_enabled and webrtc_mode in ("block", "disable_non_proxied")
        if not webrtc.get("supported"):
            profile_result["failures"].append("WebRTC không khả dụng hoặc không thể kiểm tra")
        if webrtc.get("error"):
            profile_result["warnings"].append(f"WebRTC error: {webrtc['error']}")
        if webrtc.get("local"):
            profile_result["failures"].append(
                "WebRTC lộ địa chỉ local/private: " + ", ".join(webrtc["local"])
            )
        if webrtc.get("candidate_count") == 0 and not proxy_blocks_udp:
            profile_result["warnings"].append("WebRTC không trả về ICE candidate")

        browser_info = profile_result["browser_ip"] or {}
        browser_ips = browser_info.get("ips") or ([browser_info["ip"]] if browser_info.get("ip") else [])
        direct_ips = (direct_ip or {}).get("ips") or (
            [(direct_ip or {}).get("ip")] if (direct_ip or {}).get("ip") else []
        )
        if proxy_enabled:
            if not browser_ips:
                profile_result["warnings"].append(
                    "Không lấy được IP public của browser nên chưa thể xác nhận proxy egress"
                )
            else:
                browser_matches = matching_ips(browser_ips, direct_ips)
                if browser_matches:
                    profile_result["failures"].append(
                        "IP browser trùng IP direct của host khi proxy đang bật: "
                        + ", ".join(browser_matches)
                    )
            webrtc_matches = matching_ips(webrtc.get("public", []), direct_ips)
            if webrtc_matches:
                profile_result["failures"].append(
                    "WebRTC srflx/public candidate trùng IP direct của host khi proxy đang bật: "
                    + ", ".join(webrtc_matches)
                )
        else:
            if not browser_ips:
                profile_result["warnings"].append("Không lấy được IP public của browser")
            if not direct_ips:
                profile_result["warnings"].append("Không lấy được IP direct của host")

        if args.dns:
            profile_result["dns"] = run_dns_probe(context, args.screenshot_dir, common.safe_slug(profile.name))
            dns = profile_result["dns"]
            if dns.get("error"):
                profile_result["warnings"].append(
                    "DNS probe best-effort lỗi: " + str(dns["error"])
                )
            elif dns.get("verdict") == "LEAK":
                if proxy_enabled:
                    profile_result["failures"].append(
                        "DNS leak page báo DNS khác ISP với internet access; cần đối chiếu DNS server với proxy egress. "
                        + ("ASN: " + "; ".join(dns.get("asns", [])) if dns.get("asns") else "")
                    )
                else:
                    profile_result["warnings"].append(
                        "DNS leak page báo DNS khác ISP với internet access (profile không bật proxy); "
                        "cần review thủ công. "
                        + ("ASN: " + "; ".join(dns.get("asns", [])) if dns.get("asns") else "")
                    )
            elif dns.get("verdict") != "CLEAN":
                profile_result["warnings"].append(
                    "DNS probe chưa có kết quả đủ để kết luận; xem text/screenshot"
                )

        if profile_result["failures"]:
            profile_result["status"] = "FAIL"
        elif profile_result["warnings"]:
            profile_result["status"] = "WARN"
        else:
            profile_result["status"] = "PASS"
    except Exception as exc:  # noqa: BLE001 - keep the evidence for this profile
        profile_result["failures"].append(f"{type(exc).__name__}: {exc}")
        profile_result["status"] = "FAIL"
    finally:
        profile_result["cleanup"] = common.finish_profile(
            launcher, profile, was_running, browser
        )
        profile_result["finished_at"] = common.utc_now()
    return profile_result


def print_profile_result(result: Dict[str, Any]) -> None:
    profile = result["profile"]
    print("")
    print(f"=== {profile['name']} ({profile['id']}) ===")
    print(f"  Proxy bật: {profile['proxy_enabled']}")
    direct_info = result.get("direct_ip") or {}
    browser_info = result.get("browser_ip") or {}
    direct_ips = direct_info.get("ips") or ([direct_info["ip"]] if direct_info.get("ip") else [])
    browser_ips = browser_info.get("ips") or (
        [browser_info["ip"]] if browser_info.get("ip") else []
    )
    print(f"  Host direct IP: {', '.join(direct_ips) or 'không xác định'}")
    print(f"  Browser public IP: {', '.join(browser_ips) or 'không xác định'}")
    webrtc = result.get("webrtc") or {}
    print(f"  WebRTC local: {', '.join(webrtc.get('local', [])) or 'không có'}")
    print(f"  WebRTC public: {', '.join(webrtc.get('public', [])) or 'không có'}")
    print(f"  WebRTC mDNS: {', '.join(webrtc.get('mdns', [])) or 'không có'}")
    dns = result.get("dns")
    if dns:
        print(f"  DNS: {dns.get('verdict')} (clicked={dns.get('clicked')})")
        if dns.get("asns"):
            print("  DNS ASN: " + "; ".join(dns["asns"][:3]))
    for item in result.get("failures", []):
        print(f"  FAIL: {item}")
    for item in result.get("warnings", []):
        print(f"  WARN: {item}")
    if result.get("cleanup") and not result["cleanup"].get("success"):
        print(f"  WARN cleanup: {result['cleanup'].get('error')}")
    print(f"  RESULT: {result.get('status')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit leak IP/WebRTC/DNS trên profile thật qua BrowserLauncher + CDP."
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="ID hoặc tên profile; có thể lặp lại. Mặc định: tất cả profile.",
    )
    parser.add_argument("--json", help="Ghi báo cáo JSON đầy đủ vào đường dẫn này.")
    parser.add_argument(
        "--screenshot-dir",
        help="Thư mục lưu screenshot trang DNS (tùy chọn).",
    )
    parser.add_argument(
        "--dns",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Chạy thêm DNS leak page best-effort (mặc định: bật).",
    )
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

    launcher = BrowserLauncher(manager)
    direct_ip = fetch_direct_ip()
    direct_ips = direct_ip.get("ips") or []
    if direct_ips:
        print(f"[*] Host direct IP: {', '.join(direct_ips)}")
    else:
        print(
            f"[!] Không lấy được host direct IP: {direct_ip.get('error')}",
            file=sys.stderr,
        )

    results = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] Thiếu Playwright. Chạy: pip install -r requirements.txt", file=sys.stderr)
        return 2

    with sync_playwright() as playwright:
        for profile in profiles:
            print(f"[*] Đang audit {profile.name} ({profile.id}) ...")
            results.append(audit_profile(launcher, profile, playwright, args, direct_ip))

    for result in results:
        print_profile_result(result)

    summary = {
        "generated_at": common.utc_now(),
        "data_dir": common.data_dir(),
        "direct_ip": direct_ip,
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
