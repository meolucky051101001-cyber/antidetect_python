"""Shared helpers for real-profile CDP audits.

These helpers deliberately use the existing BrowserLauncher instead of creating a
separate Playwright browser. That means the audit sees the same executable,
command-line flags, proxy configuration and fingerprint injection as a normal
profile launch.

Audit tools must be run with ``ANTIDETECT_DATA_DIR`` pointing at a test data
directory when they are not intended to touch real profiles.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from antidetect.launcher import BrowserLauncher  # noqa: E402
from antidetect.storage import DATA_DIR, ProfileManager  # noqa: E402


class AuditLaunchError(RuntimeError):
    """Raised when a profile cannot be launched for a real-browser audit."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_slug(value: str, fallback: str = "profile") -> str:
    value = (value or "").strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value[:80] or fallback


def write_json(path: str, payload: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def resolve_profiles(
    manager: ProfileManager, selectors: Optional[Iterable[str]]
) -> Tuple[List[Any], List[str]]:
    """Resolve profile IDs/names; return (matches, missing_selectors)."""
    profiles = manager.list_profiles()
    wanted = [s for s in (selectors or []) if s]
    if not wanted:
        return profiles, []

    matched: List[Any] = []
    missing: List[str] = []
    for selector in wanted:
        found = [
            p
            for p in profiles
            if p.id == selector or p.name == selector or p.name.lower() == selector.lower()
        ]
        if found:
            for profile in found:
                if all(profile.id != existing.id for existing in matched):
                    matched.append(profile)
        else:
            missing.append(selector)
    return matched, missing


def connect_browser(playwright: Any, port: int) -> Any:
    try:
        return playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{int(port)}", timeout=30_000
        )
    except Exception as exc:  # noqa: BLE001 - include details in the audit report
        raise AuditLaunchError(f"Không kết nối được CDP port {port}: {exc}") from exc


def get_default_page(browser: Any) -> Tuple[Any, Any]:
    """Use the launcher's existing page so its init script is already active."""
    contexts = browser.contexts
    if not contexts:
        raise AuditLaunchError("CDP browser không có context mặc định")
    context = contexts[0]
    pages = context.pages
    page = pages[0] if pages else context.new_page()
    return context, page


def start_profile(launcher: BrowserLauncher, profile: Any) -> Tuple[bool, Dict[str, Any], int]:
    """Launch (or attach to) a profile and return (was_running, result, port)."""
    was_running = bool(launcher.is_running(profile.id))
    result = launcher.launch(profile.id, open_url=None)
    if not result.get("success"):
        raise AuditLaunchError(result.get("error") or "Không khởi chạy được profile")
    port = result.get("port") or launcher.active_ports.get(profile.id)
    if not port:
        raise AuditLaunchError("Launcher không trả về CDP port")
    return was_running, result, int(port)


def finish_profile(
    launcher: BrowserLauncher,
    profile: Any,
    was_running: bool,
    browser: Any = None,
) -> Optional[Dict[str, Any]]:
    """Disconnect Playwright and stop only a browser launched by this audit."""
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if was_running:
        return None
    try:
        return launcher.stop(profile.id)
    except Exception as exc:  # noqa: BLE001 - report cleanup failure, keep audit result
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def profile_summary(profile: Any) -> Dict[str, Any]:
    proxy = getattr(profile, "proxy", None)
    fp = getattr(profile, "fingerprint", None)
    return {
        "id": profile.id,
        "name": profile.name,
        "proxy_enabled": bool(proxy and proxy.enabled),
        "proxy": (
            {
                "protocol": proxy.protocol,
                "host": proxy.host,
                "port": proxy.port,
                "username_present": bool(proxy.username),
            }
            if proxy and proxy.enabled
            else None
        ),
        "target_os": getattr(fp, "os", None),
        "webrtc_mode": getattr(fp, "webrtc_mode", None),
    }


def data_dir() -> str:
    return str(DATA_DIR)


def print_data_notice() -> None:
    print(f"[*] Data dir: {data_dir()}")
    print("[*] Dùng ANTIDETECT_DATA_DIR trỏ thư mục tạm nếu không muốn chạm profile thật.")
