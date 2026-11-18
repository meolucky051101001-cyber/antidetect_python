"""Helper thuần cho web API: RAM/uptime, thread pool, GeoIP, fanpage (P1-7).

Không giữ singleton và không đăng ký route — app.py truyền dependency vào,
nhờ vậy test được từng hàm mà không cần dựng FastAPI app.
"""
import os
import time
import threading
import zoneinfo
from typing import Any, Callable, Dict, List

from antidetect.facebook import (
    DEFAULT_DISPLAY_TIMEZONE,
    STALE_AFTER_HOURS,
    classify_facebook_page,
    normalize_last_post_fields,
)
from antidetect.fingerprint import get_host_device_memory, locale_for_country

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil là optional
    psutil = None

_ram_cache: Dict[str, Any] = {"at": 0.0, "key": None, "data": {}}
_ram_scan_lock = threading.Lock()


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


def _canonical_profile_dir(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _process_profile_dir(arguments) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.startswith("--user-data-dir="):
            value = argument.split("=", 1)[1]
        elif argument == "--user-data-dir" and index + 1 < len(arguments):
            value = arguments[index + 1]
        else:
            continue
        return _canonical_profile_dir(value) if value else None
    return None


def ram_by_profile_dir(user_data_dirs: Dict[str, str]) -> Dict[str, float]:
    """Measure matching browser trees; retain no machine-wide process handles."""
    if psutil is None or not user_data_dirs:
        return {}
    key = tuple(sorted((profile_id, _canonical_profile_dir(directory))
                       for profile_id, directory in user_data_dirs.items() if directory))
    if not key:
        return {}
    with _ram_scan_lock:
        now = time.monotonic()
        if key == _ram_cache["key"] and now - _ram_cache["at"] < 5.0:
            return dict(_ram_cache["data"])
        wanted = {directory: profile_id for profile_id, directory in key}
        result: Dict[str, float] = {}
        measured = set()
        try:
            # memory_info/cmdline on every Windows process opens handles that
            # process_iter's global cache retains. Inspect only browser names
            # first, and read RSS only for the matching profile and children.
            for process in psutil.process_iter(["pid", "name"]):
                try:
                    if (process.info.get("name") or "").lower() not in (
                            "chrome.exe", "msedge.exe", "chromium.exe"):
                        continue
                    profile_id = wanted.get(_process_profile_dir(process.cmdline()))
                    if profile_id is None:
                        continue
                    members = [process]
                    try:
                        members.extend(process.children(recursive=True))
                    except psutil.Error:
                        pass
                    for member in members:
                        if member.pid in measured:
                            continue
                        try:
                            rss = member.memory_info().rss
                        except psutil.Error:
                            continue
                        measured.add(member.pid)
                        result[profile_id] = result.get(profile_id, 0.0) + rss / (1024 * 1024)
                except psutil.Error:
                    continue
        finally:
            # No cached Process instances may survive a dashboard RAM scan.
            psutil.process_iter.cache_clear()
        _ram_cache.update(at=now, key=key, data=result)
        return dict(result)


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


def enrich_facebook_pages(
    raw: Any,
    *,
    now: Any = None,
    timezone: Any = None,
    stale_after_hours: int = STALE_AFTER_HOURS,
) -> List[Dict[str, Any]]:
    """Chuẩn hóa timestamp + trạng thái hoạt động tại thời điểm trả response.

    Legacy ``last_post_days`` được quy đổi thành khoảng UTC từ
    ``detail_synced_at`` (ngày local) thay vì dùng số đã đóng băng; nhờ vậy
    tuổi bài tự tăng theo thời gian và dữ liệu cũ không còn báo "đang đăng".
    """
    pages = sanitize_facebook_pages(raw)
    result: List[Dict[str, Any]] = []
    for entry in pages:
        normalized = normalize_last_post_fields(
            entry, display_tz=timezone, now=now
        )
        normalized.update(
            classify_facebook_page(
                normalized,
                now=now,
                timezone=timezone,
                stale_after_hours=stale_after_hours,
            )
        )
        result.append(normalized)
    return result


def profile_display_timezone(profile: Any) -> str:
    fingerprint = getattr(profile, "fingerprint", None)
    return str(getattr(fingerprint, "timezone", "") or DEFAULT_DISPLAY_TIMEZONE)


def fanpage_stale_after_hours() -> int:
    """Ngưỡng stale cấu hình được; mặc định 48 giờ theo QĐ-4."""
    try:
        value = int(os.environ.get("ANTIDETECT_FANPAGE_STALE_HOURS", "48"))
    except (TypeError, ValueError):
        return STALE_AFTER_HOURS
    return value if value > 0 else STALE_AFTER_HOURS


def host_gpu_strings() -> tuple:
    """GPU khai báo hợp lệ theo OS host cho authentic bulk-create.

    Authentic tắt WebGL spoof nên trình duyệt dùng GPU thật; giá trị này chỉ
    cần là một entry hợp lệ trong bảng GPU để audit/UI không thấy chuỗi rác.
    """
    from antidetect.fingerprint import GPU_PROFILES, host_os

    gpus = GPU_PROFILES.get(host_os()) or GPU_PROFILES.get("windows") or []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        renderer = gpu.get("renderer") or ""
        if "Intel" in renderer:
            return gpu.get("vendor") or "Google Inc. (Intel)", renderer
    if gpus and isinstance(gpus[0], dict):
        first = gpus[0]
        return first.get("vendor") or "Google Inc.", first.get("renderer") or ""
    return (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    )
