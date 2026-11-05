import os
import sys
import json
import argparse
from typing import Optional, Dict, Any

# Ensure root folder is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.models import Profile, ProxyConfig, FingerprintConfig, REDACTED_SECRET
from antidetect.storage import ProfileManager
from antidetect.launcher import BrowserLauncher
from antidetect.launch_gate import (
    evaluate_profile as evaluate_launch_gate,
    evaluate_profiles as evaluate_launch_gates,
    get_mode as get_launch_gate_mode,
)
from antidetect.fingerprint import (
    generate_fingerprint,
    get_location_coordinates,
    GPU_PROFILES,
    SCREEN_RESOLUTIONS,
    HARDWARE_PRESETS,
    POPULAR_LOCATIONS
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def get_manager_and_launcher():
    manager = ProfileManager()
    launcher = BrowserLauncher(manager)
    return manager, launcher


def action_list(manager: ProfileManager, launcher: BrowserLauncher) -> Dict[str, Any]:
    profiles = manager.list_profiles()
    res = []
    for p in profiles:
        is_run = launcher.is_running(p.id)
        port = launcher.active_ports.get(p.id) if is_run else None
        p_dict = p.model_dump() if hasattr(p, "model_dump") else p.dict()
        p_dict["status"] = "running" if is_run else "stopped"
        p_dict["port"] = port
        res.append(p_dict)
    return {"success": True, "profiles": res}


def _resolve_gate_browser_paths(launcher: BrowserLauncher, profiles) -> Dict[str, Optional[str]]:
    return {p.id: launcher._resolve_browser_path(p) for p in profiles}


def action_start(manager: ProfileManager, launcher: BrowserLauncher, profile_id: str, url: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    clean_url = url.strip() if url else None
    if clean_url and not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        clean_url = "https://" + clean_url

    if launcher.is_running(profile_id):
        guard = launcher.cdp_guard(profile_id, running=True)
        if not guard["ok"]:
            return {
                "success": False,
                "already_running": True,
                "code": guard["code"],
                "error": guard["message"],
                "cdp_health": guard["health"],
                "recovery_required": guard["code"]
                in ("RECOVERY_REQUIRED", "DETACHED_NO_CDP"),
            }
        if clean_url:
            success = launcher.open_tab(profile_id, clean_url)
            return {
                "success": success,
                "already_running": True,
                "tab_opened": success,
                "port": launcher.active_ports.get(profile_id),
                "message": f"Profile đang chạy, đã mở tab mới: {clean_url}" if success else "Profile đang chạy nhưng không mở được tab mới"
            }
        return {
            "success": True,
            "already_running": True,
            "port": launcher.active_ports.get(profile_id),
            "message": "Profile đã đang chạy"
        }

    profile = manager.get_profile(profile_id)
    gate = None
    if profile is not None:
        profiles = manager.list_profiles()
        gate = evaluate_launch_gate(
            profile,
            profiles,
            _resolve_gate_browser_paths(launcher, profiles),
            get_launch_gate_mode(),
            force=force,
        )
        if not gate.allowed:
            return {
                "success": False,
                "blocked": True,
                "error": gate.message,
                "gate": gate.as_dict(),
            }

    res = launcher.launch(profile_id, open_url=clean_url)
    launcher.unregister_cleanup()  # Keep browser running independently
    if gate is not None and gate.checked and gate.status != "healthy":
        res = dict(res)
        res["gate"] = gate.as_dict()
    return res


def action_stop(manager: ProfileManager, launcher: BrowserLauncher, profile_id: str) -> Dict[str, Any]:
    res = launcher.stop(profile_id)
    return res


def action_start_all(manager: ProfileManager, launcher: BrowserLauncher, url: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    clean_url = url.strip() if url else None
    profiles = manager.list_profiles()
    gate_mode = get_launch_gate_mode()
    blocked = []
    launch_ids = None
    if gate_mode != "off" and not force:
        to_start = [p for p in profiles if not launcher.is_running(p.id)]
        gates = evaluate_launch_gates(
            to_start,
            _resolve_gate_browser_paths(launcher, profiles),
            gate_mode,
        )
        blocked = [result for result in gates.values() if not result.allowed]
        if to_start and len(blocked) == len(to_start):
            return {
                "success": False,
                "blocked": [result.as_dict() for result in blocked],
                "gate_mode": gate_mode,
                "error": "Cổng fingerprint chặn toàn bộ profile cần mở.",
            }
        if blocked:
            blocked_ids = {result.profile_id for result in blocked}
            launch_ids = [p.id for p in profiles if p.id not in blocked_ids]
    res = launcher.launch_all(delay=1.0, open_url=clean_url, profile_ids=launch_ids)
    launcher.unregister_cleanup()
    return {
        "success": True,
        "results": res,
        "blocked": [result.as_dict() for result in blocked],
        "gate_mode": gate_mode,
    }


def action_stop_all(manager: ProfileManager, launcher: BrowserLauncher) -> Dict[str, Any]:
    res = launcher.stop_all()
    return res


def action_create(manager: ProfileManager, launcher: BrowserLauncher, data: Dict[str, Any]) -> Dict[str, Any]:
    name = data.get("name", "Profile Mới")
    target_os = data.get("os", "windows")
    notes = data.get("notes", "")
    tags = data.get("tags", [])
    city = data.get("city", "hanoi")

    proxy_data = data.get("proxy", {})
    proxy = ProxyConfig(
        enabled=proxy_data.get("enabled", False),
        protocol=proxy_data.get("protocol", "http"),
        host=proxy_data.get("host", ""),
        port=proxy_data.get("port", 0),
        username=proxy_data.get("username"),
        password=(
            None
            if proxy_data.get("password") in (None, "", REDACTED_SECRET)
            else proxy_data.get("password")
        ),
    )

    lat, lon, tz = get_location_coordinates(city, randomize=True)
    fp = generate_fingerprint(
        target_os=target_os,
        timezone=tz,
        locale="vi-VN",
        latitude=lat,
        longitude=lon,
        city=city
    )

    profile = manager.create_profile(
        name=name,
        target_os=target_os,
        proxy=proxy,
        fingerprint=fp,
        notes=notes,
        tags=tags
    )
    p_dict = profile.model_dump() if hasattr(profile, "model_dump") else profile.dict()
    return {"success": True, "profile": p_dict}


def action_delete(manager: ProfileManager, launcher: BrowserLauncher, profile_id: str) -> Dict[str, Any]:
    try:
        launcher.stop(profile_id)
    except Exception:
        pass
    success = manager.delete_profile(profile_id)
    return {"success": success, "message": "Đã xóa profile" if success else "Không tìm thấy profile"}


def action_reset_fingerprint(manager: ProfileManager, launcher: BrowserLauncher, profile_id: str, target_os: Optional[str] = None) -> Dict[str, Any]:
    p = manager.get_profile(profile_id)
    if not p:
        return {"success": False, "error": "Profile không tồn tại"}
    os_to_use = target_os or p.fingerprint.os
    lat, lon, tz = get_location_coordinates("hanoi", randomize=True)
    new_fp = generate_fingerprint(
        target_os=os_to_use,
        timezone=tz,
        locale="vi-VN",
        latitude=lat,
        longitude=lon
    )
    p.fingerprint = new_fp
    updated = manager.update_profile(p)
    if not updated:
        return {"success": False, "error": "Cập nhật fingerprint thất bại"}
    p_dict = updated.model_dump() if hasattr(updated, "model_dump") else updated.dict()
    return {"success": True, "profile": p_dict}


def action_update(manager: ProfileManager, launcher: BrowserLauncher, profile_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    p = manager.get_profile(profile_id)
    if not p:
        return {"success": False, "error": "Profile không tồn tại"}
    if "name" in data:
        p.name = data["name"]
    if "notes" in data:
        p.notes = data["notes"]
    if "tags" in data:
        p.tags = data["tags"]
    if "proxy" in data:
        pr = data["proxy"]
        new_proxy = ProxyConfig(
            enabled=pr.get("enabled", False),
            protocol=pr.get("protocol", "http"),
            host=pr.get("host", ""),
            port=pr.get("port", 0),
            username=pr.get("username"),
            password=pr.get("password"),
        )
        if new_proxy.password == REDACTED_SECRET:
            new_proxy.password = None
        # Cùng endpoint + password trống/che → giữ secret DPAPI hiện có.
        # Đổi endpoint thì không mang secret cũ sang (identity binding).
        if (
            p.proxy
            and new_proxy.identity_key() == p.proxy.identity_key()
            and p.proxy.password
            and new_proxy.password in (None, "")
        ):
            new_proxy.password = p.proxy.password
        p.proxy = new_proxy
    updated = manager.update_profile(p)
    p_dict = updated.model_dump() if hasattr(updated, "model_dump") else updated.dict()
    return {"success": True, "profile": p_dict}


def action_options() -> Dict[str, Any]:
    return {
        "success": True,
        "gpu_profiles": GPU_PROFILES,
        "screen_resolutions": SCREEN_RESOLUTIONS,
        "hardware_presets": HARDWARE_PRESETS,
        "locations": POPULAR_LOCATIONS
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True, help="Hành động cần thực hiện")
    parser.add_argument(
        "--payload",
        default=None,
        help="Dữ liệu JSON đầu vào (không dùng cho mật khẩu proxy)",
    )
    parser.add_argument(
        "--payload-stdin",
        action="store_true",
        help="Đọc JSON payload từ stdin để mật khẩu không lộ qua command line",
    )
    args = parser.parse_args()

    action = args.action
    try:
        if args.payload_stdin:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        elif args.payload is not None:
            payload = json.loads(args.payload)
        else:
            payload = {}
    except Exception:
        payload = {}

    manager, launcher = get_manager_and_launcher()

    try:
        if action == "list":
            result = action_list(manager, launcher)
        elif action == "start":
            result = action_start(
                manager,
                launcher,
                payload.get("id") or payload.get("profile_id"),
                payload.get("url"),
                force=bool(payload.get("force")),
            )
        elif action == "stop":
            result = action_stop(manager, launcher, payload.get("id") or payload.get("profile_id"))
        elif action == "start_all":
            result = action_start_all(
                manager,
                launcher,
                payload.get("url"),
                force=bool(payload.get("force")),
            )
        elif action == "stop_all":
            result = action_stop_all(manager, launcher)
        elif action == "create":
            result = action_create(manager, launcher, payload)
        elif action == "delete":
            result = action_delete(manager, launcher, payload.get("id") or payload.get("profile_id"))
        elif action == "reset_fingerprint":
            result = action_reset_fingerprint(manager, launcher, payload.get("id") or payload.get("profile_id"), payload.get("os"))
        elif action == "update":
            result = action_update(manager, launcher, payload.get("id") or payload.get("profile_id"), payload)
        elif action == "options":
            result = action_options()
        else:
            result = {"success": False, "error": f"Hành động không hợp lệ: {action}"}
    except Exception as e:
        import traceback
        result = {"success": False, "error": str(e), "traceback": traceback.format_exc()}

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
