"""Static fingerprint health audit and safe repair plan.

Inspired by the fingerprint-check panels of Dolphin Anty / AdsPower, but
fully local: no browser is launched and no network request is made, so the
whole set of profiles can be checked on demand from the dashboard.

The audit is intentionally conservative. It reports incoherent metadata and
duplicate identities, then offers only repairs that cannot change the visible
persona on purpose: version/UA synchronization with the browser that will
actually run, OS/GPU/DPR/screen/RAM coherence, language tag cleanup, WebRTC
hardening while a proxy is enabled and unique canvas/audio seeds.
Timezone, geolocation, proxy configuration and browser pinning are never
rewritten automatically.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .fingerprint import (
    GPU_PROFILES,
    build_user_agent,
    clamp_screen_to_host,
    detect_browser_brand,
    get_host_device_memory,
    get_host_screen_info,
    get_installed_chrome_version,
    get_version_from_browser_path,
    host_os,
    normalize_fingerprint_for_launch,
)
from .models import FingerprintConfig, Profile

SEVERITY_WEIGHT = {"critical": 30, "warning": 10, "info": 2}
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_CATEGORY_LABELS = {
    "fingerprint": "Dấu vân tay",
    "location": "Vị trí",
    "network": "Mạng",
}

_FIELD_LABELS = {
    "os": "Hệ điều hành",
    "browser_brand": "Thương hiệu trình duyệt",
    "user_agent": "User-Agent",
    "browser_version": "Phiên bản trình duyệt",
    "screen_width": "Chiều rộng màn hình",
    "screen_height": "Chiều cao màn hình",
    "screen_color_depth": "Độ sâu màu",
    "device_pixel_ratio": "Tỷ lệ điểm ảnh (DPR)",
    "canvas_noise_seed": "Canvas seed",
    "audio_noise_seed": "Audio seed",
    "webgl_vendor": "WebGL vendor",
    "webgl_renderer": "WebGL renderer",
    "hardware_concurrency": "Số luồng CPU",
    "device_memory": "RAM (GB)",
    "timezone": "Múi giờ",
    "locale": "Locale",
    "languages": "Ngôn ngữ",
    "latitude": "Vĩ độ",
    "longitude": "Kinh độ",
    "accuracy": "Độ chính xác GPS",
    "webrtc_mode": "Chế độ WebRTC",
    "stealth_disable": "Stealth tắt",
}

_FIELD_ORDER = list(_FIELD_LABELS)

_LANG_TAG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_UA_CHROME_RE = re.compile(r"chrome/(\d+)", re.IGNORECASE)
_APPLE_BASE_CHIP_RE = re.compile(r"Apple (M\d)\b(?!\s*(?:Pro|Max|Ultra))")
_APPLE_BASE_CORES = {"M1": 8, "M2": 8, "M3": 8}


@dataclass
class AuditIssue:
    code: str
    severity: str
    title: str
    detail: str
    fixable: bool = False
    category: str = "fingerprint"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "category_label": _CATEGORY_LABELS.get(self.category, self.category),
            "title": self.title,
            "detail": self.detail,
            "fixable": self.fixable,
        }


def _ua_os(user_agent: str) -> Optional[str]:
    low = (user_agent or "").lower()
    if "windows nt" in low:
        return "windows"
    if "macintosh" in low or "mac os x" in low:
        return "macos"
    if "android" in low:
        return "android"
    if "iphone" in low or "ipad" in low:
        return "ios"
    if "linux" in low:
        return "linux"
    return None


def _ua_chrome_major(user_agent: str) -> Optional[int]:
    match = _UA_CHROME_RE.search(user_agent or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _gpu_os_mismatch(os_name: str, vendor: str, renderer: str) -> bool:
    blob = f"{vendor or ''} {renderer or ''}"
    if os_name == "macos":
        return any(
            token in blob
            for token in ("Direct3D", "D3D11", "NVIDIA", "GeForce", "AMD Radeon RX")
        )
    if os_name == "windows":
        return "Metal" in blob or "Apple" in blob
    if os_name == "linux":
        return "Direct3D" in blob or "Metal" in blob or "Apple" in blob
    return False


def _timezone_offset_hours(timezone: str) -> Optional[float]:
    try:
        from zoneinfo import ZoneInfo

        offset = datetime.now(ZoneInfo(timezone.strip())).utcoffset()
        return offset.total_seconds() / 3600 if offset is not None else None
    except Exception:
        return None


def _proxy_endpoint(profile: Profile) -> Optional[str]:
    proxy = profile.proxy
    if not proxy or not proxy.enabled:
        return None
    host = (proxy.host or "").strip().lower()
    try:
        port = int(proxy.port or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or port <= 0:
        return None
    return f"{(proxy.protocol or 'http').lower()}://{host}:{port}"


def _display_names(profiles: Iterable[Profile]) -> str:
    names = [p.name for p in profiles]
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + f" và {len(names) - 3} profile khác"


def _host_snapshot() -> Dict[str, Any]:
    try:
        width, height, scale = get_host_screen_info()
    except Exception:
        width, height, scale = 1920, 1080, 1.0
    try:
        memory = get_host_device_memory()
    except Exception:
        memory = 8
    try:
        installed = get_installed_chrome_version()
    except Exception:
        installed = None
    return {
        "os": host_os(),
        "screen_width": int(width),
        "screen_height": int(height),
        "scale": float(scale),
        "memory": int(memory),
        "installed_version": installed,
    }


def _resolve_entry(
    profile: Profile, browser_paths: Optional[Dict[str, Optional[str]]]
) -> Tuple[Optional[str], bool]:
    """Return (effective browser path, pinned-path-unresolved flag)."""
    if browser_paths is not None and profile.id in browser_paths:
        resolved = browser_paths.get(profile.id)
        return resolved, bool((profile.browser_path or "").strip() and not resolved)
    return (profile.browser_path or "").strip() or None, False


def _expected_version(
    browser_path: Optional[str], installed: Optional[Tuple[str, str]]
) -> Optional[Tuple[str, str]]:
    detected = None
    if browser_path:
        try:
            detected = get_version_from_browser_path(browser_path)
        except Exception:
            detected = None
    return detected or installed


def _clean_language_tag(tag: Any) -> str:
    return str(tag or "").split(";", 1)[0].strip()


def _repair_languages(fp: FingerprintConfig) -> None:
    tags: List[str] = []
    seen: Set[str] = set()
    for raw in [fp.locale] + list(fp.languages or []):
        tag = _clean_language_tag(raw)
        if not tag or not _LANG_TAG_RE.match(tag):
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    if not tags:
        tags = ["en-US", "en"]
    locale = _clean_language_tag(fp.locale)
    if not locale or not _LANG_TAG_RE.match(locale):
        locale = tags[0]
    if locale not in tags:
        tags.insert(0, locale)
    fp.locale = locale
    fp.languages = tags


def _pick_gpu(os_name: str, seed: int) -> Dict[str, str]:
    gpus = GPU_PROFILES.get(os_name) or GPU_PROFILES["windows"]
    return gpus[abs(int(seed or 0)) % len(gpus)]


def _apply_static_repairs(profile: Profile, fp: FingerprintConfig) -> FingerprintConfig:
    """Apply repairs that normalize_fingerprint_for_launch does not cover."""
    fp = fp.model_copy(deep=True)

    renderer = fp.webgl_renderer or ""
    match = _APPLE_BASE_CHIP_RE.search(renderer)
    if fp.os == "macos" and match:
        expected_cores = _APPLE_BASE_CORES[match.group(1)]
        if fp.hardware_concurrency != expected_cores:
            fp.hardware_concurrency = expected_cores

    if fp.screen_color_depth != 24:
        fp.screen_color_depth = 24

    if fp.os == "macos":
        dpr = float(fp.device_pixel_ratio or 1.0)
        if abs(dpr - 1.0) > 0.01 and abs(dpr - 2.0) > 0.01:
            fp.device_pixel_ratio = 2.0

    if not fp.webgl_renderer or not fp.webgl_vendor:
        gpu = _pick_gpu(fp.os, fp.canvas_noise_seed)
        if not fp.webgl_renderer:
            fp.webgl_renderer = gpu["renderer"]
        if not fp.webgl_vendor:
            fp.webgl_vendor = gpu["vendor"]

    ua = fp.user_agent or ""
    ua_os = _ua_os(ua)
    brand = fp.browser_brand or "chrome"
    if (
        not ua
        or "headlesschrome" in ua.lower()
        or (ua_os is not None and ua_os != fp.os)
        or (brand == "edge" and "edg/" not in ua.lower())
        or (brand != "edge" and "edg/" in ua.lower())
    ):
        fp.user_agent = build_user_agent(
            fp.os, fp.browser_version or "155.0.0.0", brand
        )

    _repair_languages(fp)

    try:
        accuracy = int(fp.accuracy or 0)
    except (TypeError, ValueError):
        accuracy = 0
    if accuracy <= 0:
        fp.accuracy = 50

    if (
        profile.proxy
        and profile.proxy.enabled
        and profile.proxy.host
        and int(profile.proxy.port or 0) > 0
        and fp.webrtc_mode == "auto"
    ):
        fp.webrtc_mode = "disable_non_proxied"

    return fp


def _allocate_seed(used: Set[int], audio: bool) -> int:
    upper = 2_147_483_647 if audio else 0xFFFFFFFF
    for _ in range(1000):
        candidate = 1000 + secrets.randbelow(max(1, upper - 1000))
        if candidate not in used:
            return candidate
    candidate = 1001
    while candidate in used:
        candidate += 1
        if candidate > upper:
            candidate = 1001
    return candidate


def _dedupe_seed(
    profiles: Sequence[Profile],
    fixed: Dict[str, FingerprintConfig],
    attr: str,
    group: str,
    audio: bool,
    prioritize_ids: Optional[Set[str]] = None,
) -> None:
    active = [
        p for p in profiles
        if group not in (p.fingerprint.stealth_disable or [])
    ]
    # When the user fixes a subset, move the selected profiles last so the
    # untouched ones keep their seeds and the selection receives new ones.
    if prioritize_ids:
        active = [p for p in active if p.id not in prioritize_ids] + [
            p for p in active if p.id in prioritize_ids
        ]
    counts: Dict[int, int] = {}
    for p in active:
        seed = int(getattr(fixed[p.id], attr) or 0)
        counts[seed] = counts.get(seed, 0) + 1
    used = {int(getattr(fp, attr) or 0) for fp in fixed.values()}
    kept: Set[int] = set()
    for p in active:
        fp = fixed[p.id]
        seed = int(getattr(fp, attr) or 0)
        if counts.get(seed, 0) <= 1:
            continue
        if seed not in kept:
            kept.add(seed)
            continue
        new_seed = _allocate_seed(used, audio=audio)
        used.add(new_seed)
        setattr(fp, attr, new_seed)


def _diff_fingerprints(
    before: FingerprintConfig, after: FingerprintConfig
) -> List[Dict[str, Any]]:
    before_data = before.model_dump()
    after_data = after.model_dump()
    keys = _FIELD_ORDER + [k for k in after_data if k not in _FIELD_ORDER]
    changes: List[Dict[str, Any]] = []
    for key in keys:
        if before_data.get(key) == after_data.get(key):
            continue
        changes.append(
            {
                "field": key,
                "label": _FIELD_LABELS.get(key, key),
                "before": before_data.get(key),
                "after": after_data.get(key),
            }
        )
    return changes


def build_fix_plan(
    profiles: Sequence[Profile],
    browser_paths: Optional[Dict[str, Optional[str]]] = None,
    prioritize_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Return safe fingerprint repairs without touching proxy/geo/timezone.

    ``profiles`` must contain every profile so cross-profile rules (seed
    collisions) stay correct; ``prioritize_ids`` marks the subset the user is
    actually fixing, so untouched profiles keep their current seeds.
    """
    profiles = list(profiles)
    fixed: Dict[str, FingerprintConfig] = {}
    for profile in profiles:
        browser_path, unresolved = _resolve_entry(profile, browser_paths)
        fp = normalize_fingerprint_for_launch(profile.fingerprint, browser_path)
        if unresolved:
            # The pin cannot be resolved; keep the declared browser version so
            # the plan never silently switches the profile to another build.
            fp.user_agent = profile.fingerprint.user_agent
            fp.browser_version = profile.fingerprint.browser_version
            fp.browser_brand = profile.fingerprint.browser_brand
        fixed[profile.id] = _apply_static_repairs(profile, fp)

    _dedupe_seed(
        profiles, fixed, "canvas_noise_seed", "canvas", audio=False,
        prioritize_ids=prioritize_ids,
    )
    _dedupe_seed(
        profiles, fixed, "audio_noise_seed", "audio", audio=True,
        prioritize_ids=prioritize_ids,
    )

    plan: List[Dict[str, Any]] = []
    for profile in profiles:
        changes = _diff_fingerprints(profile.fingerprint, fixed[profile.id])
        if not changes:
            continue
        plan.append(
            {
                "profile_id": profile.id,
                "name": profile.name,
                "revision": int(getattr(profile, "revision", 1) or 1),
                "changes": changes,
                "fingerprint": fixed[profile.id].model_dump(),
            }
        )
    return plan


def _signature(profile: Profile) -> Tuple[Any, ...]:
    fp = profile.fingerprint
    return (
        fp.os,
        fp.browser_brand,
        fp.user_agent,
        fp.browser_version,
        fp.screen_width,
        fp.screen_height,
        fp.screen_color_depth,
        round(float(fp.device_pixel_ratio or 1.0), 3),
        fp.webgl_vendor,
        fp.webgl_renderer,
        fp.hardware_concurrency,
        fp.device_memory,
        fp.timezone,
        fp.locale,
        tuple(fp.languages or []),
        fp.webrtc_mode,
    )


def audit_profiles(
    profiles: Sequence[Profile],
    browser_paths: Optional[Dict[str, Optional[str]]] = None,
    include_fix_plan: bool = True,
) -> Dict[str, Any]:
    """Audit every profile and return a dashboard-ready report.

    ``include_fix_plan=False`` skips the repair plan for callers that only need
    the findings (the pre-launch gate), which avoids normalising every
    fingerprint on a path that runs right before a browser is spawned.
    """
    profiles = list(profiles)
    host = _host_snapshot()
    issues: Dict[str, List[AuditIssue]] = {p.id: [] for p in profiles}
    seen: Dict[str, Set[str]] = {p.id: set() for p in profiles}

    def add(
        profile: Profile,
        code: str,
        severity: str,
        title: str,
        detail: str,
        fixable: bool = False,
        category: str = "fingerprint",
    ) -> None:
        if code in seen[profile.id]:
            return
        seen[profile.id].add(code)
        issues[profile.id].append(
            AuditIssue(
                code=code,
                severity=severity,
                title=title,
                detail=detail,
                fixable=fixable,
                category=category,
            )
        )

    for profile in profiles:
        fp = profile.fingerprint
        browser_path, unresolved = _resolve_entry(profile, browser_paths)
        expected = (
            None if unresolved else _expected_version(browser_path, host["installed_version"])
        )
        expected_full = expected[1] if expected else None
        expected_major = int(expected_full.split(".", 1)[0]) if expected_full else None
        expected_brand = (
            detect_browser_brand(browser_path)
            if browser_path
            else (fp.browser_brand or "chrome")
        )
        ua = fp.user_agent or ""
        ua_os = _ua_os(ua)
        ua_major = _ua_chrome_major(ua)

        if unresolved:
            add(
                profile,
                "browser_path_unresolved",
                "warning",
                "Không tìm thấy browser pin",
                f"Đường dẫn '{profile.browser_path}' không resolve được. "
                "Profile sẽ không mở được cho tới khi sửa browser pin.",
            )

        # --- Browser identity (UA / version / brand) ---
        if not ua:
            add(
                profile,
                "ua_missing",
                "critical",
                "Thiếu User-Agent",
                "Profile không có User-Agent; Chrome sẽ dùng UA mặc định lệch với fingerprint.",
                fixable=True,
            )
        else:
            if "headlesschrome" in ua.lower():
                add(
                    profile,
                    "headless_ua",
                    "critical",
                    "User-Agent headless",
                    "UA chứa HeadlessChrome — dấu hiệu automation rất rõ.",
                    fixable=True,
                )
            if ua_os is not None and ua_os != fp.os:
                add(
                    profile,
                    "ua_os_mismatch",
                    "critical",
                    "User-Agent lệch hệ điều hành",
                    f"UA khai báo {ua_os} nhưng profile giả lập {fp.os}. "
                    "Đây là tín hiệu chống gian lận rõ ràng.",
                    fixable=True,
                )
            if expected_major is not None and ua_major is not None and ua_major != expected_major:
                add(
                    profile,
                    "ua_version_mismatch",
                    "warning",
                    "User-Agent lệch phiên bản Chrome",
                    f"UA dùng Chrome/{ua_major} nhưng trình duyệt thật là "
                    f"Chrome/{expected_major} ({expected_full}).",
                    fixable=True,
                )
            brand_ok = (
                expected_brand != "edge" and "edg/" not in ua.lower()
            ) or (
                expected_brand == "edge" and "edg/" in ua.lower()
            )
            if not brand_ok:
                add(
                    profile,
                    "ua_brand_mismatch",
                    "warning",
                    "Thương hiệu UA không khớp",
                    f"UA không khớp thương hiệu '{expected_brand}' của trình duyệt sẽ chạy.",
                    fixable=True,
                )

        if (
            expected_full
            and (fp.browser_version or "") != expected_full
        ):
            add(
                profile,
                "browser_version_mismatch",
                "warning",
                "Fingerprint lệch phiên bản trình duyệt",
                f"Metadata ghi {fp.browser_version or 'không rõ'} nhưng trình duyệt "
                f"sẽ chạy {expected_full}.",
                fixable=True,
            )

        # --- WebGL / hardware ---
        if not fp.webgl_renderer or not fp.webgl_vendor:
            add(
                profile,
                "webgl_missing",
                "warning",
                "Thiếu thông tin WebGL",
                "WebGL vendor/renderer đang trống; detector sẽ thấy thông số khác thường.",
                fixable=True,
            )
        elif _gpu_os_mismatch(fp.os, fp.webgl_vendor, fp.webgl_renderer):
            add(
                profile,
                "gpu_os_mismatch",
                "critical",
                "GPU không tương thích hệ điều hành",
                f"'{fp.webgl_renderer}' không thể xuất hiện trên {fp.os}.",
                fixable=True,
            )

        if fp.device_memory != host["memory"]:
            add(
                profile,
                "ram_host_mismatch",
                "warning",
                "RAM lệch máy thật",
                f"navigator.deviceMemory khai báo {fp.device_memory} nhưng header "
                f"Sec-CH-Device-Memory của Chrome luôn gửi {host['memory']}.",
                fixable=True,
            )

        apple_match = _APPLE_BASE_CHIP_RE.search(fp.webgl_renderer or "")
        if fp.os == "macos" and apple_match:
            expected_cores = _APPLE_BASE_CORES[apple_match.group(1)]
            if fp.hardware_concurrency != expected_cores:
                add(
                    profile,
                    "apple_base_cores",
                    "warning",
                    "Số core lệch chip Apple",
                    f"{apple_match.group(0)} bản base có {expected_cores} core "
                    f"nhưng profile khai báo {fp.hardware_concurrency}.",
                    fixable=True,
                )

        if fp.hardware_concurrency < 2:
            add(
                profile,
                "low_core_count",
                "info",
                "Số core rất thấp",
                f"hardwareConcurrency={fp.hardware_concurrency} là cấu hình hiếm gặp "
                "trên máy desktop hiện đại.",
            )

        # --- Screen / DPR ---
        if fp.screen_color_depth != 24:
            add(
                profile,
                "color_depth_invalid",
                "warning",
                "Độ sâu màu bất thường",
                f"screen.colorDepth={fp.screen_color_depth}; Chrome desktop thực tế dùng 24.",
                fixable=True,
            )
        if fp.os == "macos":
            dpr = float(fp.device_pixel_ratio or 1.0)
            if dpr < 1.5:
                add(
                    profile,
                    "macos_dpr_invalid",
                    "critical",
                    "macOS dùng DPR không hợp lệ",
                    f"Mac Retina phải có DPR ≥ 1.5; profile đang khai báo {dpr}.",
                    fixable=True,
                )
            elif abs(dpr - 1.0) > 0.01 and abs(dpr - 2.0) > 0.01:
                add(
                    profile,
                    "macos_dpr_implausible",
                    "warning",
                    "DPR macOS bất thường",
                    f"macOS chỉ dùng DPR 1.0 hoặc 2.0; profile đang khai báo {dpr}.",
                    fixable=True,
                )
            if (fp.screen_width, fp.screen_height) == (1366, 768):
                add(
                    profile,
                    "macos_windows_resolution",
                    "warning",
                    "Độ phân giải Windows trên macOS",
                    "1366×768 là độ phân giải laptop Windows; Mac dùng 1440×900 trở lên.",
                    fixable=True,
                )
        clamped_width, clamped_height, clamped = clamp_screen_to_host(
            fp.screen_width, fp.screen_height, fp.device_pixel_ratio or 1.0, fp.os
        )
        if clamped:
            add(
                profile,
                "screen_exceeds_host",
                "warning",
                "Màn hình lớn hơn màn hình thật",
                f"{fp.screen_width}×{fp.screen_height} vượt màn hình host "
                f"{host['screen_width']}×{host['screen_height']} ở DPR "
                f"{fp.device_pixel_ratio}; an toàn là {clamped_width}×{clamped_height}.",
                fixable=True,
            )

        # --- Locale / languages ---
        if not (fp.locale or "").strip():
            add(
                profile,
                "locale_missing",
                "warning",
                "Thiếu locale",
                "Profile không khai báo locale; navigator.language sẽ lệch với ngôn ngữ UA.",
                fixable=True,
            )
        raw_languages = [str(v or "") for v in (fp.languages or [])]
        if any(";" in value for value in raw_languages):
            add(
                profile,
                "language_qvalue",
                "warning",
                "Ngôn ngữ chứa q-value",
                "Khai báo dạng 'vi;q=0.9' khiến Chrome thêm q-value một lần nữa "
                "và tạo header trùng lặp.",
                fixable=True,
            )
        if any(value and not _LANG_TAG_RE.match(_clean_language_tag(value)) for value in raw_languages):
            add(
                profile,
                "language_invalid",
                "warning",
                "Thẻ ngôn ngữ không hợp lệ",
                "Có thẻ ngôn ngữ không đúng chuẩn BCP-47 (vd. khoảng trắng/ký tự lạ).",
                fixable=True,
            )
        if not raw_languages or not any(value.strip() for value in raw_languages):
            add(
                profile,
                "languages_missing",
                "warning",
                "Thiếu danh sách ngôn ngữ",
                "navigator.languages trống là bất thường trên Chrome desktop.",
                fixable=True,
            )
        elif (
            (fp.locale or "").strip()
            and _clean_language_tag(fp.locale) not in raw_languages
        ):
            add(
                profile,
                "locale_not_first",
                "warning",
                "Locale không nằm trong danh sách ngôn ngữ",
                f"locale '{fp.locale}' không có trong navigator.languages, "
                "tạo ra tín hiệu không nhất quán.",
                fixable=True,
            )

        # --- Timezone / geolocation ---
        tz = (fp.timezone or "").strip()
        tz_offset = _timezone_offset_hours(tz) if tz else None
        if not tz or tz_offset is None:
            add(
                profile,
                "timezone_invalid",
                "warning",
                "Múi giờ không hợp lệ",
                f"'{fp.timezone or ''}' không phải múi giờ IANA hợp lệ. "
                "Dùng 'Chuyển Vị Trí' để đặt lại múi giờ.",
                category="location",
            )
        if fp.latitude is None and fp.longitude is None:
            add(
                profile,
                "geolocation_missing",
                "info",
                "Chưa có toạ độ GPS",
                "Profile không có latitude/longitude; trang hỏi vị trí sẽ nhận "
                "giá trị thiếu hoặc mặc định.",
                category="location",
            )
        elif fp.latitude is None or fp.longitude is None:
            add(
                profile,
                "geolocation_partial",
                "warning",
                "Toạ độ GPS thiếu một trục",
                "Chỉ có một trong hai giá trị latitude/longitude; geolocation "
                "không thể hoạt động nhất quán.",
                category="location",
            )
        if int(fp.accuracy or 0) <= 0:
            add(
                profile,
                "accuracy_invalid",
                "warning",
                "Độ chính xác GPS không hợp lệ",
                f"accuracy={fp.accuracy}; giá trị ≤ 0 khiến Geolocation API trả kết quả lạ.",
                fixable=True,
                category="location",
            )
        if fp.latitude is not None and fp.longitude is not None and tz_offset is not None:
            approximate = float(fp.longitude) / 15.0
            delta = abs(tz_offset - approximate)
            delta = min(delta, abs(24.0 - delta))
            if delta > 3.5:
                add(
                    profile,
                    "geo_timezone_mismatch",
                    "warning",
                    "Toạ độ lệch múi giờ",
                    f"Kinh độ {fp.longitude:.2f} tương ứng UTC{approximate:+.1f} "
                    f"nhưng timezone {tz} là UTC{tz_offset:+.1f}. "
                    "Hãy đặt vị trí cùng thành phố với proxy.",
                    category="location",
                )

        # --- Proxy / WebRTC ---
        proxy = profile.proxy
        proxy_error = proxy.configuration_error() if proxy else None
        proxy_ready = bool(proxy and proxy.enabled and not proxy_error)
        if proxy and proxy.enabled and not proxy_ready:
            add(
                profile,
                "proxy_incomplete",
                "critical",
                "Cấu hình proxy không hợp lệ",
                proxy_error or "Proxy đã bật cần host và port hợp lệ.",
                category="network",
            )
        elif not proxy or not proxy.enabled:
            add(
                profile,
                "no_proxy",
                "info",
                "Chưa gắn proxy (dùng IP thật)",
                "Profile đang dùng IP của máy. Hãy gắn proxy trước khi đăng nhập "
                "tài khoản quan trọng.",
                category="network",
            )
        if proxy_ready and fp.webrtc_mode == "auto":
            add(
                profile,
                "webrtc_leak_risk",
                "warning",
                "WebRTC có thể lộ IP thật",
                "Profile có proxy nhưng WebRTC ở chế độ auto. Đặt "
                "disable_non_proxied để chặn lộ IP.",
                fixable=True,
                category="network",
            )

        if fp.os != host["os"]:
            add(
                profile,
                "cross_os_profile",
                "info",
                "Profile khác hệ điều hành với máy chủ",
                f"Profile giả lập {fp.os} trên máy {host['os']}. Client Hints được "
                "override nhưng một số tín hiệu cấp thấp vẫn có thể lệch.",
            )

    # --- Cross-profile collisions ---
    def group_by(key_fn):
        groups: Dict[Any, List[Profile]] = {}
        for profile in profiles:
            key = key_fn(profile)
            if key is None:
                continue
            groups.setdefault(key, []).append(profile)
        return groups

    for seed, group in group_by(
        lambda p: p.fingerprint.canvas_noise_seed
        if "canvas" not in (p.fingerprint.stealth_disable or [])
        else None
    ).items():
        if len(group) > 1:
            for profile in group:
                others = [p for p in group if p.id != profile.id]
                add(
                    profile,
                    "duplicate_canvas_seed",
                    "critical",
                    "Canvas seed trùng profile khác",
                    f"Trùng seed #{seed} với: {_display_names(others)}. "
                    "Canvas hash sẽ giống nhau.",
                    fixable=True,
                )

    for seed, group in group_by(
        lambda p: p.fingerprint.audio_noise_seed
        if "audio" not in (p.fingerprint.stealth_disable or [])
        else None
    ).items():
        if len(group) > 1:
            for profile in group:
                others = [p for p in group if p.id != profile.id]
                add(
                    profile,
                    "duplicate_audio_seed",
                    "critical",
                    "Audio seed trùng profile khác",
                    f"Trùng seed #{seed} với: {_display_names(others)}. "
                    "Audio fingerprint sẽ giống nhau.",
                    fixable=True,
                )

    for _sig, group in group_by(_signature).items():
        if len(group) > 1:
            for profile in group:
                others = [p for p in group if p.id != profile.id]
                add(
                    profile,
                    "duplicate_environment",
                    "warning",
                    "Cấu hình phần cứng trùng profile khác",
                    f"Giống {_display_names(others)} về UA, màn hình, GPU và "
                    "phần cứng. Nên đổi preset/GPU hoặc reset fingerprint.",
                )

    for endpoint, group in group_by(_proxy_endpoint).items():
        if len(group) > 1:
            for profile in group:
                others = [p for p in group if p.id != profile.id]
                add(
                    profile,
                    "shared_proxy_endpoint",
                    "warning",
                    "Nhiều profile dùng chung proxy",
                    f"Cùng endpoint {endpoint} với {_display_names(others)}. "
                    "Các tài khoản này sẽ chung IP.",
                    category="network",
                )

    plan = build_fix_plan(profiles, browser_paths) if include_fix_plan else []
    plan_by_id = {item["profile_id"]: item for item in plan}

    results: List[Dict[str, Any]] = []
    for profile in profiles:
        profile_issues = sorted(
            issues[profile.id],
            key=lambda item: (SEVERITY_RANK.get(item.severity, 9), item.code),
        )
        counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in profile_issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        score = max(
            0,
            100
            - sum(SEVERITY_WEIGHT.get(issue.severity, 0) for issue in profile_issues),
        )
        if counts["critical"]:
            status = "critical"
        elif counts["warning"]:
            status = "warning"
        else:
            status = "healthy"
        fix_item = plan_by_id.get(profile.id)
        fp = profile.fingerprint
        proxy = profile.proxy
        proxy_label = "Direct (IP thật)"
        if proxy and proxy.enabled and proxy.host:
            proxy_label = f"{proxy.protocol or 'http'}://{proxy.host}:{proxy.port}"
        results.append(
            {
                "profile_id": profile.id,
                "name": profile.name,
                "os": fp.os,
                "browser_brand": fp.browser_brand,
                "revision": int(getattr(profile, "revision", 1) or 1),
                "status": status,
                "score": score,
                "issue_counts": counts,
                "issues": [issue.as_dict() for issue in profile_issues],
                "can_fix": bool(fix_item),
                "fix_preview": fix_item["changes"] if fix_item else [],
                "fingerprint_summary": {
                    "browser": f"{fp.browser_brand or 'chrome'} {fp.browser_version or '?'}",
                    "screen": (
                        f"{fp.screen_width}×{fp.screen_height} @"
                        f"{round(float(fp.device_pixel_ratio or 1.0), 2)}"
                    ),
                    "gpu": fp.webgl_renderer or "—",
                    "hardware": (
                        f"{fp.hardware_concurrency} core / {fp.device_memory} GB"
                    ),
                    "timezone": fp.timezone or "—",
                    "locale": fp.locale or "—",
                    "proxy": proxy_label,
                },
            }
        )

    results.sort(
        key=lambda item: (
            SEVERITY_RANK.get(
                "critical" if item["status"] == "critical"
                else "warning" if item["status"] == "warning"
                else "info",
                3,
            ),
            item["score"],
            item["name"].lower(),
        )
    )

    total_issues = {"critical": 0, "warning": 0, "info": 0}
    for item in results:
        for severity, count in item["issue_counts"].items():
            total_issues[severity] = total_issues.get(severity, 0) + count
    average = round(sum(item["score"] for item in results) / len(results), 1) if results else 100.0

    return {
        "generated_at": int(time.time()),
        "summary": {
            "total": len(results),
            "healthy": sum(1 for item in results if item["status"] == "healthy"),
            "warning": sum(1 for item in results if item["status"] == "warning"),
            "critical": sum(1 for item in results if item["status"] == "critical"),
            "average_score": average,
            "issues": total_issues,
            "fixable_profiles": sum(1 for item in results if item["can_fix"]),
            "without_proxy": sum(
                1
                for p in profiles
                if not (p.proxy and p.proxy.enabled and p.proxy.host and p.proxy.port)
            ),
        },
        "profiles": results,
    }
