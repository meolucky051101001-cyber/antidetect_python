import json
import os
import sys
from typing import Optional
from .models import FingerprintConfig
from .fingerprint import get_host_screen_info, get_host_work_area

from pathlib import Path

_ASSETS_DIR = Path(__file__).with_name("assets")


def _read_asset(name: str) -> str:
    return (_ASSETS_DIR / name).read_text(encoding="utf-8")


_WORKER_TEMPLATE = _read_asset("stealth_worker.js")
_PAGE_TEMPLATE = _read_asset("stealth_page.js")
_CANVAS_TEMPLATE = _read_asset("stealth_canvas.js")

STEALTH_GROUPS = (
    "navigator",
    "screen",
    "chrome",
    "media",
    "webgpu",
    "speech",
    "canvas",
    "fonts",
    "webgl",
    "audio",
    "worker",
    "webrtc",
    "iframe",
    "battery",
    "network",
)


def _disabled_stealth_groups() -> set:
    """Groups disabled via ANTIDETECT_STEALTH_DISABLE (comma-separated).

    This exists so detector bisection can turn one hook family off at a time
    without editing the source. ``all`` disables every optional hook.
    """
    raw = os.environ.get("ANTIDETECT_STEALTH_DISABLE", "")
    items = {item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()}
    if "all" in items:
        return set(STEALTH_GROUPS)
    # Group names are canonical strings; the env value is normalized to
    # lower-case, so map back to the canonical name before intersecting.
    canonical = {group.lower(): group for group in STEALTH_GROUPS}
    return {canonical[item] for item in items if item in canonical}


# Các hook mặc định TẮT: Pixelyscan phát hiện sự tồn tại của wrapper trên
# getBattery/connection (kể cả khi giá trị đồng bộ native) — same-OS thì máy
# thật đã đúng, để native hoàn toàn là an toàn nhất. Bật lại cho nhu cầu giả
# per-profile bằng env ANTIDETECT_STEALTH_ENABLE=battery,network.
_DEFAULT_OFF_GROUPS = {"battery", "network"}


def _effective_disabled_groups(fp) -> set:
    """Merged disabled set: env + default-off + per-profile, trừ explicit enable.

    Per-profile disabling (e.g. ["canvas","webgl","audio"] for the
    "authentic" mode) lets a profile match a real Chrome on this machine
    instead of adding deterministic noise. ANTIDETECT_STEALTH_ENABLE chỉ có
    tác dụng bật lại các nhóm default-off, không override profile-disable.
    """
    disabled = _disabled_stealth_groups() | set(_DEFAULT_OFF_GROUPS)
    raw = (os.environ.get("ANTIDETECT_STEALTH_ENABLE") or "").replace(";", ",")
    enabled = {g.strip().lower() for g in raw.split(",") if g.strip()}
    if enabled:
        canonical = {group.lower(): group for group in STEALTH_GROUPS}
        enabled = {canonical[g] for g in enabled if g in canonical}
        disabled -= (enabled & set(_DEFAULT_OFF_GROUPS))
    extra = getattr(fp, "stealth_disable", None) or []
    return disabled | {g for g in extra if g in STEALTH_GROUPS}


# Font enumeration protection (cross-OS). Detector quét hàng trăm font qua
# document.fonts.check/measureText; cùng-OS thì font host đã đúng, cross-OS
# (vd. profile macOS trên host Windows) phải trả theo danh sách chuẩn của OS
# đích thay vì để lộ font của host.
_WINDOWS_FONTS = [
    "Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria",
    "Cambria Math", "Candara", "Comic Sans MS", "Consolas", "Constantia",
    "Corbel", "Cordia New", "Courier New", "Ebrima", "Franklin Gothic Medium",
    "Gabriola", "Gadugi", "Georgia", "Impact", "Ink Free", "Javanese Text",
    "Leelawadee UI", "Lucida Console", "Lucida Sans Unicode", "Malgun Gothic",
    "Marlett", "Microsoft Himalaya", "Microsoft JhengHei",
    "Microsoft New Tai Lue", "Microsoft PhagsPa", "Microsoft Sans Serif",
    "Microsoft Tai Le", "Microsoft YaHei", "Microsoft Yi Baiti", "MingLiU-ExtB",
    "Mongolian Baiti", "MS Gothic", "MV Boli", "Myanmar Text", "Nirmala UI",
    "Palatino Linotype", "Segoe MDL2 Assets", "Segoe Print", "Segoe Script",
    "Segoe UI", "Segoe UI Emoji", "Segoe UI Historic", "Segoe UI Symbol",
    "SimSun", "Sitka Text", "Sylfaen", "Symbol", "Tahoma", "Times New Roman",
    "Trebuchet MS", "Verdana", "Webdings", "Wingdings", "Wingdings 2",
    "Wingdings 3", "Yu Gothic",
]
_MACOS_FONTS = [
    "Al Nile", "American Typewriter", "Apple Braille", "Apple Color Emoji",
    "Apple SD Gothic Neo", "Arial", "Arial Hebrew", "Arial Rounded MT Bold",
    "Avenir", "Avenir Next", "Avenir Next Condensed", "Baskerville",
    "Bodoni 72", "Bradley Hand", "Brush Script MT", "Chalkboard",
    "Chalkduster", "Charter", "Cochin", "Comic Sans MS", "Copperplate",
    "Courier New", "Damascus", "Didot", "Euphemia UCAS", "Futura",
    "Galvji", "Geeza Pro", "Geneva", "Georgia", "Gill Sans",
    "Grantha Sangam MN", "Helvetica", "Helvetica Neue", "Herculanum",
    "Hiragino Maru Gothic ProN", "Hiragino Mincho ProN", "Hiragino Sans",
    "Hoefler Text", "Impact", "InaiMathi", "Iowan Old Style", "Kailasa",
    "Kefa", "Khmer MN", "Kohinoor Bangla", "Kohinoor Devanagari",
    "Kohinoor Telugu", "Krungthep", "Lao MN", "Lao Sangam MN", "Lucida Grande",
    "Malayalam MN", "Malayalam Sangam MN", "Marker Felt", "Menlo", "Mishafi",
    "Mistral", "Mukta Mahee", "Nadeem", "Nanum Myeongjo", "New Peninim MT",
    "Noteworthy", "Optima", "Palatino", "Papyrus", "Party LET", "PingFang HK",
    "PingFang SC", "PingFang TC", "Rockwell", "Sathu", "Savoye LET", "Seravek",
    "SignPainter", "Silom", "Sinhala MN", "Skia", "Snell Roundhand",
    "Songti SC", "Sukhumvit Set", "Symbol", "Tahoma", "Thonburi",
    "Times New Roman", "Trattatello", "Trebuchet MS", "Verdana", "Waseem",
    "Webdings", "Wingdings", "Zapfino",
]
_LINUX_FONTS = [
    "DejaVu Sans", "DejaVu Sans Mono", "DejaVu Serif", "Liberation Mono",
    "Liberation Sans", "Liberation Serif", "Noto Sans", "Noto Sans Mono",
    "Noto Serif", "Ubuntu", "Ubuntu Mono", "Cantarell", "Droid Sans",
    "FreeSans", "FreeMono", "FreeSerif", "Roboto", "Arial", "Courier New",
    "Georgia", "Times New Roman", "Verdana",
]

# SpeechSynthesis voices chuẩn theo OS đích cho profile cross-OS (host không
# có voices của OS kia nên phải dựng; same-OS giữ voices thật đã lọc).
_MACOS_VOICES = [
    ("Samantha", "en-US", True),
    ("Alex", "en-US", False),
    ("Daniel", "en-GB", False),
    ("Fred", "en-US", False),
    ("Karen", "en-AU", False),
    ("Moira", "en-IE", False),
    ("Tessa", "en-ZA", False),
    ("Thomas", "fr-FR", False),
    ("Anna", "de-DE", False),
    ("Alice", "it-IT", False),
    ("Monica", "es-ES", False),
    ("Milena", "ru-RU", False),
    ("Ting-Ting", "zh-CN", False),
    ("Sin-ji", "zh-HK", False),
    ("Mei-Jia", "zh-TW", False),
    ("Kyoko", "ja-JP", False),
    ("Yuna", "ko-KR", False),
    ("Lekha", "hi-IN", False),
    ("Paulina", "pl-PL", False),
    ("Yelda", "tr-TR", False),
]


def _os_fonts(os_name: str) -> list:
    if os_name == "macos":
        return _MACOS_FONTS
    if os_name == "linux":
        return _LINUX_FONTS
    return _WINDOWS_FONTS


def _cross_os_data(fp: FingerprintConfig) -> Optional[dict]:
    """Font/voice sets cho profile cross-OS; None khi cùng OS với host."""
    host = _host_os()
    if fp.os == host:
        return None
    data = {
        "targetFonts": _os_fonts(fp.os),
        "hostFonts": _os_fonts(host),
    }
    # Linux thật thường không có speech voices (Chrome cần speech-dispatcher).
    if fp.os == "macos":
        data["voices"] = [
            {"name": n, "lang": l, "default": d}
            for n, l, d in _MACOS_VOICES
        ]
    else:
        data["voices"] = []
    return data


def _battery_profile(fp: FingerprintConfig) -> dict:
    """Battery status per-profile, deterministic theo canvas seed.

    AdsPower/GoLogin cho cấu hình pin từng profile; ở đây sinh từ seed để
    cùng profile luôn thấy cùng giá trị giữa các phiên (detector so sánh
    battery qua nhiều lần ghé thăm).
    """
    seed = int(fp.canvas_noise_seed or 0)
    h = (seed * 2654435761 + 40503) % 1000000
    level = (40 + (h % 61)) / 100.0
    charging = (h // 100) % 2 == 0
    # spec BatteryManager: thời gian "không áp dụng" là Infinity (đang pin thì
    # không thể ước lượng chargingTime và ngược lại). Config được nhúng trực
    # tiếp vào script JS (không qua JSON.parse) nên literal Infinity an toàn.
    if charging:
        charging_time = 600 + (h % 1800)
        discharging_time = float("inf")
    else:
        charging_time = float("inf")
        discharging_time = 600 + (h % 7200)
    return {
        "level": round(level, 2),
        "charging": bool(charging),
        "chargingTime": charging_time,
        "dischargingTime": discharging_time,
    }


def _connection_profile(fp: FingerprintConfig) -> dict:
    """NetworkInformation per-profile, deterministic theo canvas seed.

    rtt làm tròn bội 25 ms và downlink bội 0.25 Mbps đúng bucket mà spec
    NetworkInformation yêu cầu — giá trị lạ (vd rtt 57) là dấu hiệu giả.
    """
    seed = int(fp.canvas_noise_seed or 0)
    h = (seed * 40503 + 977) % 1000000
    rtt = 50 + (h % 76)
    rtt -= rtt % 25
    downlink = 2.5 + ((h // 100) % 33) * 0.25
    return {
        "effectiveType": "4g",
        "type": "wifi",
        "rtt": rtt,
        "downlink": round(downlink, 2),
        "saveData": False,
    }


def _domrect_offsets(fp: FingerprintConfig) -> dict:
    """Offset DOMRect/ClientRects per-profile, deterministic theo seed.

    Giai đoạn 1 (anti-detection): DOMRect là vector nối profile còn sót —
    mọi profile cùng máy hiện trả giá trị giống hệt nhau. Offset nhỏ
    (±0.5px) áp dụng NHẤT QUÁN trên mọi element: thống kê hash khác nhau
    giữa các profile nhưng layout tương đối nguyên vẹn.
    """
    seed = int(fp.canvas_noise_seed or 0)

    def mk(i: int) -> float:
        v = ((seed ^ (i * 0x9E3779B1)) * 2654435761) & 0xFFFFFFFF
        v = ((v >> (i + 3)) ^ v) & 0xFFFFF
        return round(((v % 1000) - 500) / 1000.0, 3)

    offsets = {"dx": mk(0), "dy": mk(1), "dw": mk(2), "dh": mk(3)}
    if all(abs(v) < 0.05 for v in offsets.values()):
        offsets["dh"] = 0.113  # đảm bảo hook không thành no-op vô nghĩa
    return offsets


def _media_devices(fp: FingerprintConfig) -> list:
    """Bộ device MediaDevices per-profile, deterministic theo seed.

    Trước đây chỉ đổi label — số lượng device + groupId vẫn của host, mọi
    profile cùng máy trùng nhau. Giờ trả bộ cố định theo profile (1-2 mic,
    1-2 loa, 1-2 cam; audio dùng chung một groupId như máy thật).
    """
    seed = int(fp.canvas_noise_seed or 0)
    h = (seed * 2654435761 + 97) & 0xFFFFFFFF
    mics = 1 + (h % 2)
    speakers = 1 + ((h >> 4) % 2)
    cams = 1 + ((h >> 8) % 2)

    if fp.os == "macos":
        mic_label = "Built-in Microphone"
        spk_label = "Internal Speakers"
        cam_label = "FaceTime HD Camera"
    else:
        mic_label = "Microphone (Realtek High Definition Audio)"
        spk_label = "Speakers (Realtek High Definition Audio)"
        cam_label = "Integrated Camera"

    def dev_id(i: int) -> str:
        x = (seed ^ (i * 0x9E3779B1)) & 0xFFFFFFFF
        x = (x * 2654435761) & 0xFFFFFFFF
        y = ((x >> 7) * 40503) & 0xFFFFFFFF
        return f"{x:08x}{y:08x}{(x ^ y):08x}"

    audio_group = dev_id(99)
    cam_group = dev_id(98)
    devices = []
    n = 0
    for _ in range(mics):
        devices.append({"kind": "audioinput", "label": mic_label,
                        "deviceId": dev_id(n), "groupId": audio_group})
        n += 1
    for _ in range(speakers):
        devices.append({"kind": "audiooutput", "label": spk_label,
                        "deviceId": dev_id(n), "groupId": audio_group})
        n += 1
    for _ in range(cams):
        devices.append({"kind": "videoinput", "label": cam_label,
                        "deviceId": dev_id(n), "groupId": cam_group})
        n += 1
    return devices


# Bảng shader precision chuẩn ANGLE desktop (float mô phỏng highp trên mọi
# mức, int 31 bit). Chỉ áp dụng cho profile cross-OS — same-OS giữ native.
_SHADER_FLOAT = [127, 127, 23]
_SHADER_INT = [31, 31, 0]


def _shader_precision(fp: FingerprintConfig) -> Optional[dict]:
    if fp.os == _host_os():
        return None
    return {"float": list(_SHADER_FLOAT), "int": list(_SHADER_INT)}


def _host_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def _webgl_limits(fp: FingerprintConfig):
    """Return REALISTIC WebGL limits for cross-OS emulation, or None.

    When the emulated OS matches the host, the native WebGL backend already
    returns the exact limits a real Chrome would report. Overriding them was
    the source of S1: the old table returned 32768 even on ANGLE D3D11, whose
    real MAX_TEXTURE_SIZE is 16384 and MAX_VIEWPORT_DIMS is [32767, 32767].
    Only cross-OS profiles need a table, because their native backend differs.
    """
    if fp.os == _host_os():
        return None

    renderer = (fp.webgl_renderer or "").lower()
    if fp.os == "macos":
        # Apple Metal on current Apple Silicon.
        return {
            "maxTextureSize": 16384,
            "maxViewportDims": [16384, 16384],
            "maxRenderbufferSize": 16384,
            "maxCubeMapTextureSize": 16384,
        }
    if fp.os == "linux":
        # NVIDIA's OpenGL driver exposes 32768; Mesa/Intel exposes 16384.
        if any(token in renderer for token in ("nvidia", "geforce", "rtx", "gtx", "quadro")):
            size = 32768
        else:
            size = 16384
        return {
            "maxTextureSize": size,
            "maxViewportDims": [size, size],
            "maxRenderbufferSize": size,
            "maxCubeMapTextureSize": size,
        }
    # Windows ANGLE D3D11 (NVIDIA/AMD/Intel all use the D3D11 limits).
    return {
        "maxTextureSize": 16384,
        "maxViewportDims": [32767, 32767],
        "maxRenderbufferSize": 16384,
        "maxCubeMapTextureSize": 16384,
    }


def _screen_work_area(fp: FingerprintConfig) -> dict:
    """Return a coherent screen work area, or an empty dict.

    CDP's device metrics do not model the OS taskbar, so ``screen.availHeight``
    equals ``screen.height`` and CreepJS flags ``noTaskbar``. For same-OS
    profiles use the host's real work area (converted to CSS pixels); for
    cross-OS profiles use a conservative platform default. Only properties
    whose value differs from Chrome's native emulated value are included, so a
    bottom taskbar normally hooks only ``availHeight``.
    """
    width = int(fp.screen_width or 0)
    height = int(fp.screen_height or 0)
    if width <= 0 or height <= 0:
        return {}

    margins = None
    if fp.os == _host_os():
        work = get_host_work_area()
        if work:
            host_width, host_height, _ = get_host_screen_info()
            left, top, right, bottom, scale = work
            scale = max(0.5, float(scale or 1.0))
            margins = (
                max(0, int(round(left / scale))),
                max(0, int(round(top / scale))),
                max(0, int(round((host_width - right) / scale))),
                max(0, int(round((host_height - bottom) / scale))),
            )

    if margins is None:
        if fp.os == "macos":
            margins = (0, 25, 0, 0)  # menu bar; Dock usually autohides
        elif fp.os == "linux":
            margins = (0, 32, 0, 0)  # common top panel
        else:
            margins = (0, 0, 0, 40)  # Windows taskbar

    left, top, right, bottom = margins
    edges = {"left": left, "top": top, "right": right, "bottom": bottom}
    edge = max(edges, key=edges.get)
    taskbar = int(edges[edge])
    if taskbar <= 0:
        return {}

    if edge in ("left", "right"):
        taskbar = min(taskbar, max(0, width - 240))
        if taskbar <= 0:
            return {}
        return {
            "availWidth": width - taskbar,
            "availHeight": height,
            "availLeft": taskbar if edge == "left" else 0,
            "availTop": 0,
        }

    taskbar = min(taskbar, max(0, height - 240))
    if taskbar <= 0:
        return {}
    return {
        "availWidth": width,
        "availHeight": height - taskbar,
        "availLeft": 0,
        "availTop": taskbar if edge == "top" else 0,
    }


def _native_or_fallback(native_brands: Optional[dict], key: str, fallback):
    """Lấy giá trị hardware native đo từ Chrome host, fallback nếu không có.

    Battery/connection giả (vd pin 72% trên desktop thật báo 100% + đang sạc)
    làm Pixelscan chấm masking — giá trị native đồng bộ với máy thật luôn an
    toàn hơn; fallback deterministic chỉ dùng khi probe fail (cross-OS profile
    hoặc lỗi đo).
    """
    value = (native_brands or {}).get(key)
    if isinstance(value, dict) and value:
        value = dict(value)
        if key == "battery":
            # Probe trả chuỗi 'Infinity' vì returnByValue serialize kiểu JSON.
            for t in ("chargingTime", "dischargingTime"):
                if value.get(t) == "Infinity":
                    value[t] = float("inf")
        return value
    return fallback


def _stealth_config(fp: FingerprintConfig, native_brands: Optional[dict] = None) -> dict:
    ua_full = fp.browser_version or "155.0.8059.85"
    ua_major = ua_full.split(".", 1)[0]
    brand = getattr(fp, "browser_brand", "chrome") or "chrome"
    if brand == "edge":
        brands = [
            {"brand": "Chromium", "version": ua_major},
            {"brand": "Microsoft Edge", "version": ua_major},
            {"brand": "Not_A Brand", "version": "24"},
        ]
        full_version_list = [
            {"brand": "Chromium", "version": ua_full},
            {"brand": "Microsoft Edge", "version": ua_full},
            {"brand": "Not_A Brand", "version": "24.0.0.0"},
        ]
    elif brand == "chromium":
        brands = [
            {"brand": "Chromium", "version": ua_major},
            {"brand": "Not_A Brand", "version": "24"},
        ]
        full_version_list = [
            {"brand": "Chromium", "version": ua_full},
            {"brand": "Not_A Brand", "version": "24.0.0.0"},
        ]
    else:
        brands = [
            {"brand": "Chromium", "version": ua_major},
            {"brand": "Google Chrome", "version": ua_major},
            {"brand": "Not_A Brand", "version": "24"},
        ]
        full_version_list = [
            {"brand": "Chromium", "version": ua_full},
            {"brand": "Google Chrome", "version": ua_full},
            {"brand": "Not_A Brand", "version": "24.0.0.0"},
        ]
    # P2 (review 2026-10-24): brands đo được từ Chrome host tại launch-time
    # thay cho bảng hardcode. Chromium xoay grease brand theo major version
    # (Chrome 155 thật gửi "Not(A:Brand";v="24" và đặt brand chính trước
    # "Chromium"), nên bảng tĩnh ở trên sai lệch header sec-ch-ua.
    if native_brands:
        measured_brands = native_brands.get("brands")
        measured_full = native_brands.get("fullVersionList")
        if isinstance(measured_brands, list) and measured_brands:
            brands = measured_brands
        if isinstance(measured_full, list) and measured_full:
            full_version_list = measured_full
    return {
        "os": fp.os,
        "browserBrand": brand,
        "canvasSeed": fp.canvas_noise_seed,
        "algorithmVersion": getattr(fp, "fingerprint_algorithm_version", 1),
        "audioSeed": fp.audio_noise_seed,
        "webglVendor": fp.webgl_vendor,
        "webglRenderer": fp.webgl_renderer,
        "hardwareConcurrency": fp.hardware_concurrency,
        "deviceMemory": fp.device_memory or 8,
        "webglLimits": _webgl_limits(fp),
        "languages": fp.languages,
        "locale": fp.locale,
        "screenWidth": fp.screen_width,
        "screenHeight": fp.screen_height,
        "screenWorkArea": _screen_work_area(fp),
        "devicePixelRatio": fp.device_pixel_ratio or 1.0,
        "screenColorDepth": fp.screen_color_depth if hasattr(fp, "screen_color_depth") else 24,
        "webrtcMode": getattr(fp, "webrtc_mode", "disable_non_proxied") or "disable_non_proxied",
        "uaFullVersion": ua_full,
        "uaMajor": ua_major,
        "brands": brands,
        "fullVersionList": full_version_list,
        # Per-profile hardware signals: ưu tiên giá trị native đo từ Chrome
        # host (đồng bộ máy thật), fallback deterministic theo seed khi probe
        # fail (chủ yếu cho cross-OS).
        "battery": _native_or_fallback(native_brands, "battery", _battery_profile(fp)),
        "connection": _native_or_fallback(
            native_brands, "connection", _connection_profile(fp)
        ),
        # Giai đoạn 1 anti-detection: DOMRect offsets + bộ MediaDevices
        # per-profile (vector nối profile); shaderPrecision chỉ cho cross-OS.
        "domRect": _domrect_offsets(fp),
        "mediaDevices": _media_devices(fp),
        "shaderPrecision": _shader_precision(fp),
        # Font/voice sets cho cross-OS; null khi cùng OS với host (dùng native).
        "crossOs": _cross_os_data(fp),
    }


def get_worker_stealth_script(
    fp: FingerprintConfig,
    native_brands: Optional[dict] = None,
    disabled: Optional[set] = None,
) -> str:
    """Stealth payload for dedicated/shared/module workers and service workers.

    Workers have no window/document, so the page payload cannot be reused. This
    script only touches WorkerNavigator, OffscreenCanvas, WebGL and
    importScripts.
    """
    cfg = _stealth_config(fp, native_brands=native_brands)
    # Workers have no Screen object; do not ship host work-area values there.
    cfg.pop("screenWorkArea", None)
    # Workers also have no DOM/MediaDevices; shaderPrecision hook chỉ cài
    # trên page realm.
    cfg.pop("domRect", None)
    cfg.pop("mediaDevices", None)
    cfg.pop("shaderPrecision", None)
    if disabled is None:
        disabled = _effective_disabled_groups(fp)
    platform = "MacIntel" if fp.os == "macos" else ("Linux x86_64" if fp.os == "linux" else "Win32")
    ch_platform = "macOS" if fp.os == "macos" else ("Linux" if fp.os == "linux" else "Windows")
    ch_platform_ver = "14.5.0" if fp.os == "macos" else ("6.5.0" if fp.os == "linux" else "15.0.0")
    ch_arch = "arm" if fp.os == "macos" else "x86"

    script = _WORKER_TEMPLATE
    replacements = {
        "__CFG__": json.dumps(cfg),
        "__CANVAS_RUNTIME__": _CANVAS_TEMPLATE,
        "__DISABLED__": json.dumps(sorted(disabled)),
        "__PLATFORM__": json.dumps(platform),
        "__CH_PLATFORM__": json.dumps(ch_platform),
        "__CH_PLATFORM_VER__": json.dumps(ch_platform_ver),
        "__CH_ARCH__": json.dumps(ch_arch),
    }
    for key, value in replacements.items():
        script = script.replace(key, value)
    return script


def get_stealth_script(
    fp: FingerprintConfig, native_brands: Optional[dict] = None
) -> str:
    """Tạo mã JavaScript tiêm trước khi trang load (Page.addScriptToEvaluateOnNewDocument)
    để can thiệp dấu vân tay (Canvas, WebGL, AudioContext, Navigator, Screen, Fonts, WebGPU).
    """

    fp_json = json.dumps(_stealth_config(fp, native_brands=native_brands))
    disabled = _effective_disabled_groups(fp)

    worker_js = get_worker_stealth_script(
        fp, native_brands=native_brands, disabled=disabled
    )
    script = _PAGE_TEMPLATE
    replacements = {
        "__CFG__": fp_json,
        "__CANVAS_RUNTIME__": _CANVAS_TEMPLATE,
        "__DISABLED__": json.dumps(sorted(disabled)),
        "__WORKER_JS__": json.dumps(worker_js),
    }
    for key, value in replacements.items():
        script = script.replace(key, value)

    return script


# Alias cho tương thích ngược và theo đặc tả kế hoạch P0-05
generate_stealth_js = get_stealth_script
get_webgl_limits = _webgl_limits


def get_stealth_capabilities(fp: FingerprintConfig) -> dict:
    """Describe enforced behavior, not a detector or privacy certification.

    Revision 2 preserves browser contracts by keeping surfaces native where a
    JavaScript substitute cannot safely reproduce WebIDL/render semantics.
    """
    revision = getattr(fp, "fingerprint_algorithm_version", 1)
    disabled = _effective_disabled_groups(fp)
    native_surfaces = [
        "webgl", "webgpu", "audio", "fonts", "client_rects", "media_devices", "speech"
    ]
    if revision >= 2:
        return {
            "algorithm_version": revision,
            "canvas_2d": (
                "native" if "canvas" in disabled else "seeded_opaque_srgb_unorm8"
            ),
            "native_surfaces": native_surfaces,
            "native_canvas_cases": [
                "webgl", "display_p3", "float16", "object_coordinate_arguments"
            ],
            "worker_installation": "cdp_execution_context",
            "cross_os_rendering": "unsupported",
            "profile_separation": (
                "none_from_render_hooks" if "canvas" in disabled else "canvas_2d_only"
            ),
            "limitations": [
                "Native GPU, audio, fonts and network may link profiles on one host.",
                "Canvas readout changes do not provide an undetectability guarantee.",
            ],
        }
    return {
        "algorithm_version": revision,
        "canvas_2d": "native" if "canvas" in disabled else "legacy_region_gate",
        "native_surfaces": [],
        "worker_installation": "cdp_execution_context",
        "cross_os_rendering": "experimental",
        "profile_separation": "legacy_unverified",
        "limitations": [
            "Legacy rendering hooks retain known crop and API consistency limitations.",
            "Canvas exports use immutable snapshots and may differ from older releases.",
            "Legacy global markers, iframe wrapping and Blob worker wrapping were removed.",
        ],
    }
