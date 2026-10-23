import os
import sys
import random
from typing import Dict, Any, Optional, List, Tuple
from .models import FingerprintConfig

# Các hồ sơ GPU WebGL chân thực phong phú
GPU_PROFILES = {
    "windows": [
        {
            "name": "NVIDIA GeForce RTX 4090 (Siêu Khủng)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce RTX 4080 (Cao Cấp)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce RTX 4070 (Gaming)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce RTX 3080 (Cao Cấp)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce RTX 3060 (Quốc Dân)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce RTX 3050 (Phổ Thông)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce GTX 1660 SUPER",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "NVIDIA GeForce GTX 1060 6GB",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "AMD Radeon RX 7900 XTX",
            "vendor": "Google Inc. (AMD)",
            "renderer": "ANGLE (AMD, AMD Radeon RX 7900 XTX Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "AMD Radeon RX 6700 XT",
            "vendor": "Google Inc. (AMD)",
            "renderer": "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "Intel Iris Xe Graphics (Laptop Văn Phòng)",
            "vendor": "Google Inc. (Intel)",
            "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "Intel UHD Graphics 770 (PC Core i5/i7)",
            "vendor": "Google Inc. (Intel)",
            "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        {
            "name": "Intel UHD Graphics 630 (Laptop Cơ Bản)",
            "vendor": "Google Inc. (Intel)",
            "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
    ],
    "macos": [
        {
            "name": "Apple M3 Max (Đồ Họa Nặng)",
            "vendor": "Google Inc. (Apple)",
            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Version 15.0)",
        },
        {
            "name": "Apple M3 Pro (MacBook Pro)",
            "vendor": "Google Inc. (Apple)",
            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Version 15.0)",
        },
        {
            "name": "Apple M3 (MacBook Air/Pro)",
            "vendor": "Google Inc. (Apple)",
            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Version 15.0)",
        },
        {
            "name": "Apple M2 (MacBook Air/Pro)",
            "vendor": "Google Inc. (Apple)",
            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Version 14.5)",
        },
        {
            "name": "Apple M1 (MacBook Air/Pro)",
            "vendor": "Google Inc. (Apple)",
            "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Version 14.4)",
        },
    ],
    "linux": [
        {
            "name": "NVIDIA GeForce RTX 3060 (Linux)",
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "OpenGL Engine (NVIDIA GeForce RTX 3060/PCIe/SSE2)",
        },
        {
            "name": "Mesa Intel UHD Graphics (Linux)",
            "vendor": "Mesa/X.org",
            "renderer": "Mesa Intel(R) UHD Graphics 630 (CFL GT2)",
        },
    ],
}

# Các bộ mẫu cấu hình trọn gói dựng sẵn (1-Click Presets)
HARDWARE_PRESETS = [
    {
        "id": "office_laptop",
        "name": "💻 Laptop Văn Phòng (Intel Iris Xe, 16GB RAM, 8 Cores, Full HD)",
        "os": "windows",
        "width": 1920,
        "height": 1080,
        "cores": 8,
        "ram": 16,
        "gpu_name": "Intel Iris Xe Graphics (Laptop Văn Phòng)",
        "vendor": "Google Inc. (Intel)",
        "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "gaming_rtx4070",
        "name": "🎮 PC Gaming Khủng (RTX 4070, 32GB RAM, 16 Cores, 2K)",
        "os": "windows",
        "width": 2560,
        "height": 1440,
        "cores": 16,
        "ram": 32,
        "gpu_name": "NVIDIA GeForce RTX 4070 (Gaming)",
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "gaming_rtx3060",
        "name": "⚡ PC Gaming Quốc Dân (RTX 3060, 16GB RAM, 12 Cores, Full HD)",
        "os": "windows",
        "width": 1920,
        "height": 1080,
        "cores": 12,
        "ram": 16,
        "gpu_name": "NVIDIA GeForce RTX 3060 (Quốc Dân)",
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "macbook_m3",
        "name": "🍏 MacBook Pro M3 (Apple Silicon, 16GB RAM, 8 Cores, Retina)",
        "os": "macos",
        "width": 1536,
        "height": 960,
        "cores": 8,
        "ram": 16,
        "gpu_name": "Apple M3 (MacBook Air/Pro)",
        "vendor": "Google Inc. (Apple)",
        "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Version 15.0)"
    },
    {
        "id": "macbook_m2",
        "name": "🍏 MacBook Air M2 (Apple M2, 8GB RAM, 8 Cores, 1440x900)",
        "os": "macos",
        "width": 1440,
        "height": 900,
        "cores": 8,
        "ram": 8,
        "gpu_name": "Apple M2 (MacBook Air/Pro)",
        "vendor": "Google Inc. (Apple)",
        "renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Version 14.5)"
    },
    {
        "id": "budget_laptop",
        "name": "📱 Laptop Tiết Kiệm (Intel UHD 630, 8GB RAM, 4 Cores, 1366x768)",
        "os": "windows",
        "width": 1366,
        "height": 768,
        "cores": 4,
        "ram": 8,
        "gpu_name": "Intel UHD Graphics 630 (Laptop Cơ Bản)",
        "vendor": "Google Inc. (Intel)",
        "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "amd_workstation",
        "name": "🖥️ PC Đồ Họa AMD (Radeon RX 6700 XT, 32GB RAM, 12 Cores, Full HD)",
        "os": "windows",
        "width": 1920,
        "height": 1080,
        "cores": 12,
        "ram": 32,
        "gpu_name": "AMD Radeon RX 6700 XT",
        "vendor": "Google Inc. (AMD)",
        "renderer": "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "gtx1660s_mid",
        "name": "🎮 PC Gaming Tầm Trung (GTX 1660 SUPER, 16GB RAM, 6 Cores, Full HD)",
        "os": "windows",
        "width": 1920,
        "height": 1080,
        "cores": 6,
        "ram": 16,
        "gpu_name": "NVIDIA GeForce GTX 1660 SUPER",
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "rtx4090_extreme",
        "name": "🔥 Workstation Cực Khủng (RTX 4090, 64GB RAM, 24 Cores, 4K)",
        "os": "windows",
        "width": 3840,
        "height": 2160,
        "cores": 24,
        "ram": 64,
        "gpu_name": "NVIDIA GeForce RTX 4090",
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    },
    {
        "id": "linux_rtx3060",
        "name": "🐧 Linux Desktop (RTX 3060, 16GB RAM, 8 Cores, Full HD)",
        "os": "linux",
        "width": 1920,
        "height": 1080,
        "cores": 8,
        "ram": 16,
        "gpu_name": "NVIDIA GeForce RTX 3060 (Linux)",
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "OpenGL Engine (NVIDIA GeForce RTX 3060/PCIe/SSE2)"
    },
    {
        "id": "linux_intel",
        "name": "🐧 Linux Laptop (Intel UHD 630, 8GB RAM, 4 Cores, Full HD)",
        "os": "linux",
        "width": 1920,
        "height": 1080,
        "cores": 4,
        "ram": 8,
        "gpu_name": "Mesa Intel UHD Graphics (Linux)",
        "vendor": "Mesa/X.org",
        "renderer": "Mesa Intel(R) UHD Graphics 630 (CFL GT2)"
    }
]

SCREEN_RESOLUTIONS = [
    (1920, 1080),  # Full HD (phổ biến nhất)
    (1920, 1080),
    (1920, 1080),
    (2560, 1440),  # 2K QHD
    (1536, 864),
    (1440, 900),
    (1366, 768),
]

CHROME_VERSIONS = [
    ("150.0.0.0", "150.0.7339.80"),
    ("151.0.0.0", "151.0.7482.101"),
    ("152.0.0.0", "152.0.7559.109"),
    ("153.0.0.0", "153.0.7632.75"),
    ("154.0.0.0", "154.0.7712.90"),
    ("155.0.0.0", "155.0.8059.85"),
]


def _version_dirs(parent: str) -> List[str]:
    """Return x.y.z.w version directories found directly under parent."""
    import re
    try:
        if not parent or not os.path.isdir(parent):
            return []
        return [
            item for item in os.listdir(parent)
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", item)
            and os.path.isdir(os.path.join(parent, item))
        ]
    except Exception:
        return []


def get_version_from_browser_path(browser_path: Optional[str]) -> Optional[Tuple[str, str]]:
    """Detect the real browser version from the executable's version folders.

    Chrome/Edge keep the actual build in a sibling version directory even though
    the executable itself is a small launcher (Application\\chrome.exe). Reading
    that folder avoids spawning a browser just to ask for --version.
    """
    if not browser_path:
        return None
    app_dir = os.path.dirname(os.path.abspath(browser_path))
    candidates = [app_dir, os.path.dirname(app_dir)]
    versions: List[str] = []
    for parent in candidates:
        versions.extend(_version_dirs(parent))
    if not versions:
        return None
    versions.sort(key=lambda s: [int(x) for x in s.split(".") if x.isdigit()])
    latest = versions[-1]
    return (f"{latest.split('.')[0]}.0.0.0", latest)


def get_installed_chrome_version() -> Optional[Tuple[str, str]]:
    """Tự động phát hiện phiên bản Chrome thực tế trên máy để đồng bộ với User-Agent (H7)."""
    candidates = []
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        candidates = [
            os.path.join(prog_files, "Google\\Chrome\\Application"),
            os.path.join(prog_files_x86, "Google\\Chrome\\Application"),
            os.path.join(local_app_data, "Google\\Chrome\\Application"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions"
        ]
    else:
        candidates = ["/opt/google/chrome"]

    for d in candidates:
        versions = _version_dirs(d)
        if versions:
            versions.sort(key=lambda s: [int(x) for x in s.split(".") if x.isdigit()])
            latest = versions[-1]
            major = latest.split(".")[0]
            return (f"{major}.0.0.0", latest)
    return None


def detect_browser_brand(browser_path: Optional[str]) -> str:
    """Return the Chromium brand implied by the executable name."""
    if not browser_path:
        return "chrome"
    name = os.path.basename(browser_path).lower()
    if name in ("msedge.exe", "microsoft edge"):
        return "edge"
    if name in ("chromium.exe", "chromium"):
        return "chromium"
    return "chrome"


def build_user_agent(
    target_os: str, full_version: str, browser_brand: str = "chrome"
) -> str:
    """Build the reduced UA shape used by modern Chrome/Edge (major.0.0.0)."""
    if target_os == "macos":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    elif target_os == "linux":
        platform = "X11; Linux x86_64"
    else:
        platform = "Windows NT 10.0; Win64; x64"
    major = (full_version or "0").split(".", 1)[0]
    chrome_part = (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    if browser_brand == "edge":
        return f"{chrome_part} Edg/{major}.0.0.0"
    return chrome_part


def normalize_fingerprint_for_launch(
    fp: FingerprintConfig, browser_path: Optional[str] = None
) -> FingerprintConfig:
    """Repair legacy/self-contradictory fingerprints before a launch.

    This is deliberately a copy: the caller decides whether to persist the
    repaired values. It fixes the known macOS 1366x768/DPR=1 profiles created by
    older builds, keeps UA/Client Hints tied to the installed Chrome build,
    and enforces strict OS <-> UA <-> GPU coherence (preventing Frankenstein profiles).
    """
    fp = fp.model_copy(deep=True)

    detected = get_version_from_browser_path(browser_path) if browser_path else None
    detected = detected or get_installed_chrome_version()
    brand = detect_browser_brand(browser_path) if browser_path else fp.browser_brand
    fp.browser_brand = brand
    if detected:
        _, full_version = detected
        major = full_version.split(".", 1)[0]
        fp.browser_version = full_version

        # P0-05: Luôn kiểm tra tính nhất quán giữa OS và User-Agent
        ua_lower = (fp.user_agent or "").lower()
        needs_rebuild = False
        if fp.os == "macos" and ("macintosh" not in ua_lower or "windows" in ua_lower or "linux" in ua_lower):
            needs_rebuild = True
        elif fp.os == "windows" and ("windows nt" not in ua_lower or "macintosh" in ua_lower or "linux" in ua_lower):
            needs_rebuild = True
        elif fp.os == "linux" and ("linux" not in ua_lower or "windows" in ua_lower or "macintosh" in ua_lower):
            needs_rebuild = True
        elif f"chrome/{major}.0.0.0" not in (fp.user_agent or ""):
            needs_rebuild = True
        elif brand == "edge" and f"edg/{major}.0.0.0" not in ua_lower:
            needs_rebuild = True
        elif brand != "edge" and "edg/" in ua_lower:
            needs_rebuild = True

        if needs_rebuild or not fp.user_agent:
            fp.user_agent = build_user_agent(fp.os, full_version, brand)
    elif brand == "edge" and "edg/" not in (fp.user_agent or "").lower():
        fp.user_agent = build_user_agent(
            fp.os, fp.browser_version or "155.0.0.0", brand
        )

    # P0-05: Kiểm tra và sửa tương thích chéo GPU WebGL theo hệ điều hành
    if fp.os == "macos":
        # A Retina Mac must not advertise DPR=1. 1366x768 is a Windows laptop resolution.
        if (fp.device_pixel_ratio or 1.0) < 1.5:
            fp.device_pixel_ratio = 2.0
        if (fp.screen_width, fp.screen_height) == (1366, 768):
            fp.screen_width, fp.screen_height = 1440, 900

        # macOS không thể có GPU Direct3D / NVIDIA Windows
        renderer = fp.webgl_renderer or ""
        if any(bad in renderer for bad in ["Direct3D", "D3D", "NVIDIA", "GeForce", "AMD Radeon RX"]):
            macos_gpus = GPU_PROFILES.get("macos", [])
            if macos_gpus:
                idx = (fp.canvas_noise_seed or 0) % len(macos_gpus)
                fp.webgl_vendor = macos_gpus[idx]["vendor"]
                fp.webgl_renderer = macos_gpus[idx]["renderer"]
    elif fp.os == "windows":
        # Windows không thể có GPU Apple Metal
        renderer = fp.webgl_renderer or ""
        vendor = fp.webgl_vendor or ""
        if "Metal" in renderer or "Apple" in renderer or "Apple" in vendor:
            win_gpus = GPU_PROFILES.get("windows", [])
            if win_gpus:
                idx = (fp.canvas_noise_seed or 0) % len(win_gpus)
                fp.webgl_vendor = win_gpus[idx]["vendor"]
                fp.webgl_renderer = win_gpus[idx]["renderer"]
    elif fp.os == "linux":
        renderer = fp.webgl_renderer or ""
        if "Direct3D" in renderer or "Metal" in renderer or "Apple" in renderer:
            linux_gpus = GPU_PROFILES.get("linux", [])
            if linux_gpus:
                idx = (fp.canvas_noise_seed or 0) % len(linux_gpus)
                fp.webgl_vendor = linux_gpus[idx]["vendor"]
                fp.webgl_renderer = linux_gpus[idx]["renderer"]

    # Language tags must be plain BCP-47 tags. q-values are added by Chrome's
    # network stack; sending "vi;q=0.9" here produces duplicated q-values.
    cleaned = []
    for lang in [fp.locale] + list(fp.languages or []):
        if not lang:
            continue
        tag = str(lang).split(";", 1)[0].strip()
        if tag and tag not in cleaned:
            cleaned.append(tag)
    if cleaned:
        fp.languages = cleaned
        fp.locale = cleaned[0]

    return fp


# Danh sách các vùng vị trí địa lý phổ biến (Mặc định: Hà Nội)
POPULAR_LOCATIONS = {
    "hanoi": {
        "id": "hanoi",
        "name": "📍 Hà Nội (Thủ Đô)",
        "city": "Hà Nội",
        "timezone": "Asia/Ho_Chi_Minh",
        "latitude": 21.0285,
        "longitude": 105.8542,
        "accuracy": 45,
    },
    "hcm": {
        "id": "hcm",
        "name": "📍 TP. Hồ Chí Minh",
        "city": "TP. Hồ Chí Minh",
        "timezone": "Asia/Ho_Chi_Minh",
        "latitude": 10.8231,
        "longitude": 106.6297,
        "accuracy": 45,
    },
    "danang": {
        "id": "danang",
        "name": "📍 Đà Nẵng",
        "city": "Đà Nẵng",
        "timezone": "Asia/Ho_Chi_Minh",
        "latitude": 16.0544,
        "longitude": 108.2022,
        "accuracy": 45,
    },
    "singapore": {
        "id": "singapore",
        "name": "📍 Singapore",
        "city": "Singapore",
        "timezone": "Asia/Singapore",
        "latitude": 1.3521,
        "longitude": 103.8198,
        "accuracy": 50,
    },
    "tokyo": {
        "id": "tokyo",
        "name": "📍 Tokyo, Nhật Bản",
        "city": "Tokyo",
        "timezone": "Asia/Tokyo",
        "latitude": 35.6762,
        "longitude": 139.6503,
        "accuracy": 50,
    },
    "us_ny": {
        "id": "us_ny",
        "name": "📍 New York, Mỹ",
        "city": "New York",
        "timezone": "America/New_York",
        "latitude": 40.7128,
        "longitude": -74.0060,
        "accuracy": 50,
    },
    "us_la": {
        "id": "us_la",
        "name": "📍 Los Angeles, Mỹ",
        "city": "Los Angeles",
        "timezone": "America/Los_Angeles",
        "latitude": 34.0522,
        "longitude": -118.2437,
        "accuracy": 50,
    },
}


LOCALE_BY_COUNTRY = {
    "VN": "vi-VN",
    "US": "en-US",
    "GB": "en-GB",
    "CA": "en-CA",
    "AU": "en-AU",
    "SG": "en-SG",
    "JP": "ja-JP",
    "KR": "ko-KR",
    "CN": "zh-CN",
    "TW": "zh-TW",
    "TH": "th-TH",
    "ID": "id-ID",
    "MY": "ms-MY",
    "PH": "en-PH",
    "IN": "en-IN",
    "DE": "de-DE",
    "FR": "fr-FR",
    "ES": "es-ES",
    "IT": "it-IT",
    "NL": "nl-NL",
    "BR": "pt-BR",
    "PT": "pt-PT",
    "RU": "ru-RU",
    "TR": "tr-TR",
    "AE": "ar-AE",
    "SA": "ar-SA",
}


def locale_for_country(country_code: Optional[str], default: str = "vi-VN") -> str:
    """Map an ISO-3166 alpha-2 country to a sensible BCP-47 locale."""
    if not country_code:
        return default
    return LOCALE_BY_COUNTRY.get(str(country_code).strip().upper(), default)


def get_location_coordinates(city: str = "hanoi", randomize: bool = True):
    """Lấy tọa độ theo thành phố (mặc định Hà Nội) với biến thiên vi mô thực tế tránh trùng lặp 100%."""
    loc = POPULAR_LOCATIONS.get(city.lower(), POPULAR_LOCATIONS["hanoi"])
    base_lat = loc["latitude"]
    base_lon = loc["longitude"]
    if randomize:
        # Độ lệch ngẫu nhiên nhỏ (~1-3 km) trong nội thành để mỗi profile là 1 vị trí riêng
        lat = round(base_lat + random.uniform(-0.025, 0.025), 4)
        lon = round(base_lon + random.uniform(-0.025, 0.025), 4)
    else:
        lat = base_lat
        lon = base_lon
    return lat, lon, loc["timezone"]


def generate_fingerprint(
    target_os: str = "windows",
    timezone: Optional[str] = None,
    locale: str = "vi-VN",
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    city: str = "hanoi",
    browser_brand: str = "chrome",
) -> FingerprintConfig:
    """Tạo một hồ sơ dấu vân tay ngẫu nhiên nhưng đảm bảo tính logic nhất quán."""
    target_os = target_os.lower()
    if target_os not in ["windows", "macos", "linux"]:
        target_os = "windows"

    # Mặc định tọa độ Hà Nội với biến thiên thực tế nếu chưa truyền
    if latitude is None or longitude is None:
        auto_lat, auto_lon, auto_tz = get_location_coordinates(city, randomize=True)
        latitude = latitude if latitude is not None else auto_lat
        longitude = longitude if longitude is not None else auto_lon
        timezone = timezone or auto_tz
    else:
        timezone = timezone or "Asia/Ho_Chi_Minh"

    # H7: Đồng bộ phiên bản Chrome với trình duyệt cài thực tế nếu có
    installed_ver = get_installed_chrome_version()
    if installed_ver:
        major_ver, full_ver = installed_ver
    else:
        major_ver, full_ver = random.choice(CHROME_VERSIONS)

    ua = build_user_agent(target_os, full_ver, browser_brand)

    # Sử dụng HARDWARE_PRESETS chuẩn theo hệ điều hành để loại bỏ cấu hình Frankenstein
    matching_presets = [p for p in HARDWARE_PRESETS if p.get("os") == target_os]
    if matching_presets:
        preset = random.choice(matching_presets)
        gpu_vendor = preset["vendor"]
        gpu_renderer = preset["renderer"]
        width = preset["width"]
        height = preset["height"]
        cores = preset["cores"]
        ram = preset["ram"]
    else:
        gpu_list = GPU_PROFILES.get(target_os, GPU_PROFILES["windows"])
        gpu = random.choice(gpu_list)
        gpu_vendor = gpu["vendor"]
        gpu_renderer = gpu["renderer"]
        width, height = random.choice(SCREEN_RESOLUTIONS)
        cores = random.choice([4, 8, 12])
        ram = random.choice([8, 16])

    # Giới hạn device_memory tối đa 8 GiB theo chuẩn W3C Device Memory API
    device_memory_val = min(8, ram)

    # DPR chuẩn: macOS Retina luôn là 2.0, Windows/Linux thông thường là 1.0 hoặc 1.25 với màn lớn
    dpr = 2.0 if target_os == "macos" else (1.0 if width <= 1920 else 1.25)

    lang_parts = locale.split("-") if locale else ["vi", "VN"]
    base_lang = lang_parts[0] if lang_parts and lang_parts[0] else "vi"
    languages = [locale, base_lang, "en-US", "en"]
    languages = [l for l in dict.fromkeys(languages) if l]

    generated = FingerprintConfig(
        os=target_os,
        browser_brand=browser_brand,
        user_agent=ua,
        browser_version=full_ver,
        screen_width=width,
        screen_height=height,
        screen_color_depth=24,
        device_pixel_ratio=dpr,
        canvas_noise_seed=random.randint(1000, 999999),
        audio_noise_seed=round(random.uniform(0.00005, 0.0003), 7),
        webgl_vendor=gpu_vendor,
        webgl_renderer=gpu_renderer,
        hardware_concurrency=cores,
        device_memory=device_memory_val,
        timezone=timezone,
        locale=locale,
        languages=languages,
        latitude=latitude,
        longitude=longitude,
        accuracy=random.randint(20, 100),
        webrtc_mode="disable_non_proxied",
    )
    return normalize_fingerprint_for_launch(generated)
