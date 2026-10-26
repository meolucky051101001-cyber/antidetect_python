"""Helper thuần cho web API: RAM/uptime, thread pool, GeoIP, fanpage (P1-7).

Không giữ singleton và không đăng ký route — app.py truyền dependency vào,
nhờ vậy test được từng hàm mà không cần dựng FastAPI app.
"""
import os
import time
import zoneinfo
from typing import Any, Callable, Dict, List

from antidetect.facebook import classify_facebook_page
from antidetect.fingerprint import get_host_device_memory, locale_for_country

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil là optional
    psutil = None

_ram_cache: Dict[str, Any] = {"at": 0.0, "data": {}}


def auto_max_concurrent() -> int:
    """Số Chrome mở đồng thời khi start-all, chọn theo RAM của máy.

    Mỗi Chrome profile tiêu tốn ~300-600MB; với quy mô ~20 profile, mở dần
    theo batch tránh làm máy 16GB bị quá tải trong lúc các phiên trước chưa
    giữ ổn định.
    """
    ram = get_host_device_memory() or 8
    if ram <= 8:
        return 4
    if ram <= 16:
        return 6
    return 8


def ram_by_profile_dir(user_data_dirs: Dict[str, str]) -> Dict[str, float]:
    """Tổng WorkingSet (MB) của các Chrome process theo user-data-dir.

    Cache 5 giây — psutil.process_iter quét toàn bộ process table, không rẻ
    khi gọi mỗi lần poll 3s với 20 profile.
    """
    if psutil is None:
        return {}
    now = time.time()
    if now - _ram_cache["at"] < 5.0:
        return {k: v for k, v in _ram_cache["data"].items() if k in user_data_dirs}
    result: Dict[str, float] = {}
    needle_map = {ud: pid for pid, ud in user_data_dirs.items() if ud}
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("chrome.exe", "msedge.exe", "chromium.exe"):
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            mem = proc.info.get("memory_info")
            if not mem:
                continue
            for ud, pid in needle_map.items():
                if ud and ud in joined:
                    result[pid] = result.get(pid, 0.0) + mem.rss / (1024 * 1024)
        except Exception:
            continue
    _ram_cache["at"] = now
    _ram_cache["data"] = result
    return {k: v for k, v in result.items() if k in user_data_dirs}


def worker_pool(max_workers: int = 64):
    """Default executor thật cho asyncio.to_thread.

    anyio.to_thread limiter không điều khiển asyncio.to_thread; muốn batch
    start-all (~20 lệnh handshake CDP 10-15s) không chiếm hết thread mặc định
    của loop thì phải set_default_executor.
    """
    import concurrent.futures

    return concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="ad-worker"
    )


def validate_iana_timezone(tz_name: str) -> bool:
    """P1-03: Kiểm tra múi giờ chuẩn IANA hợp lệ."""
    if not tz_name or not isinstance(tz_name, str):
        return False
    try:
        zoneinfo.ZoneInfo(tz_name.strip())
        return True
    except Exception:
        return False


def apply_geo_to_profile(
    p, geo: dict, update_profile: Callable[[Any], Any]
) -> dict:
    """Áp GeoIP result vào fingerprint profile (sync — gọi trong thread)."""
    changes = {}
    tz = geo.get("timezone")
    if tz and validate_iana_timezone(tz) and tz != p.fingerprint.timezone:
        changes["timezone"] = [p.fingerprint.timezone, tz]
        p.fingerprint.timezone = tz
    lat, lon = geo.get("lat"), geo.get("lon")
    if lat is not None and lon is not None:
        lat, lon = float(lat), float(lon)
        if abs((p.fingerprint.latitude or 0) - lat) > 0.001 or abs((p.fingerprint.longitude or 0) - lon) > 0.001:
            changes["latitude"] = [p.fingerprint.latitude, lat]
            changes["longitude"] = [p.fingerprint.longitude, lon]
            p.fingerprint.latitude = lat
            p.fingerprint.longitude = lon
    country = geo.get("country_code")
    if country:
        new_locale = locale_for_country(country, default=p.fingerprint.locale)
        if new_locale != p.fingerprint.locale:
            changes["locale"] = [p.fingerprint.locale, new_locale]
            p.fingerprint.locale = new_locale
            base_lang = new_locale.split("-")[0]
            p.fingerprint.languages = [new_locale, base_lang, "en-US", "en"]
            seen = set()
            p.fingerprint.languages = [l for l in p.fingerprint.languages if not (l in seen or seen.add(l))]
    if changes:
        update_profile(p)
    return changes


def sanitize_facebook_pages(raw: Any) -> List[Dict[str, Any]]:
    """Chỉ giữ fanpage có URL http(s) tuyệt đối.

    URL này được render thẳng thành href trên dashboard, nên loại bỏ
    javascript:/data:/file: từ cache hoặc file import (chống stored XSS cục bộ).
    Entry sai kiểu hoặc URL không hợp lệ bị bỏ qua thay vì làm hỏng cả danh sách.
    """
    from urllib.parse import urlparse

    pages: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return pages
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            continue
        item = dict(entry)
        item["url"] = url
        pages.append(item)
    return pages


def enrich_facebook_pages(raw: Any) -> List[Dict[str, Any]]:
    pages = sanitize_facebook_pages(raw)
    return [{**entry, **classify_facebook_page(entry)} for entry in pages]


def host_gpu_strings() -> tuple:
    """Vendor/renderer GPU thật của host (cho authentic bulk-create).

    Dự phòng theo bảng GPU khai báo — verify_live sẽ bắt nếu authentic lệch.
    """
    try:
        from antidetect.fingerprint import GPU_PROFILES
        for gp in GPU_PROFILES:
            r = gp if isinstance(gp, str) else (gp.get("renderer") or "")
            if "Intel" in r:
                return "Google Inc. (Intel)", r
        first = GPU_PROFILES[0]
        r = first if isinstance(first, str) else (first.get("renderer") or "")
        return "Google Inc. (Intel)", r
    except Exception:
        return "Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11)"
