import json
import os
import sys
from .models import FingerprintConfig

STEALTH_GROUPS = (
    "navigator",
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


def _stealth_config(fp: FingerprintConfig) -> dict:
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
    return {
        "os": fp.os,
        "browserBrand": brand,
        "canvasSeed": fp.canvas_noise_seed,
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
        "devicePixelRatio": fp.device_pixel_ratio or 1.0,
        "screenColorDepth": fp.screen_color_depth if hasattr(fp, "screen_color_depth") else 24,
        "webrtcMode": getattr(fp, "webrtc_mode", "disable_non_proxied") or "disable_non_proxied",
        "uaFullVersion": ua_full,
        "uaMajor": ua_major,
        "brands": brands,
        "fullVersionList": full_version_list,
    }


def get_worker_stealth_script(fp: FingerprintConfig) -> str:
    """Stealth payload for dedicated/shared/module workers and service workers.

    Workers have no window/document, so the page payload cannot be reused. This
    script only touches WorkerNavigator, OffscreenCanvas, WebGL and
    importScripts.
    """
    cfg = _stealth_config(fp)
    platform = "MacIntel" if fp.os == "macos" else ("Linux x86_64" if fp.os == "linux" else "Win32")
    ch_platform = "macOS" if fp.os == "macos" else ("Linux" if fp.os == "linux" else "Windows")
    ch_platform_ver = "14.5.0" if fp.os == "macos" else ("6.5.0" if fp.os == "linux" else "15.0.0")
    ch_arch = "arm" if fp.os == "macos" else "x86"

    script = r"""
(function() {
    const g = self;
    if (g.__adWorkerStealth) return;
    try { Object.defineProperty(g, '__adWorkerStealth', { value: true, configurable: true }); } catch(e) {}
    const CFG = __CFG__;
    const DISABLED = new Set(__DISABLED__);
    const platformStr = __PLATFORM__;
    const chPlatform = __CH_PLATFORM__;
    const chPlatformVer = __CH_PLATFORM_VER__;
    const chArch = __CH_ARCH__;
    const NavCtor = g.WorkerNavigator || g.Navigator;

    function markNative(fn, name, length, isConstructor) {
        let target = fn;
        if (!isConstructor) {
            target = { [name](...args) { return fn.apply(this, args); } }[name];
        }
        try { Object.defineProperty(target, 'name', { value: name, configurable: true }); } catch(e) {}
        if (typeof length === 'number') {
            try { Object.defineProperty(target, 'length', { value: length, configurable: true }); } catch(e) {}
        }
        return target;
    }

    function defineOn(target, prop, getter, name) {
        if (!target) return false;
        try {
            Object.defineProperty(target, prop, {
                get: markNative(getter, name, 0),
                enumerable: true,
                configurable: true
            });
            return true;
        } catch(e) { return false; }
    }
    function defineNav(prop, value, name) {
        if (!g.navigator) return;
        const proto = NavCtor && NavCtor.prototype;
        const getter = function() {
            if (NavCtor && !(this instanceof NavCtor)) throw new TypeError('Illegal invocation');
            return value;
        };
        defineOn(proto, prop, getter, name);
        try {
            if (g.navigator[prop] !== value) defineOn(g.navigator, prop, getter, name);
        } catch(e) {
            defineOn(g.navigator, prop, getter, name);
        }
    }

    defineNav('webdriver', false, 'get webdriver');
    defineNav('hardwareConcurrency', CFG.hardwareConcurrency, 'get hardwareConcurrency');
    defineNav('deviceMemory', CFG.deviceMemory || 8, 'get deviceMemory');
    defineNav('languages', Object.freeze([...CFG.languages]), 'get languages');
    defineNav('language', CFG.locale, 'get language');
    defineNav('platform', platformStr, 'get platform');
    defineNav('userAgent', 'Mozilla/5.0 (' +
        (CFG.os === 'macos' ? 'Macintosh; Intel Mac OS X 10_15_7' :
         (CFG.os === 'linux' ? 'X11; Linux x86_64' : 'Windows NT 10.0; Win64; x64')) +
        ') AppleWebKit/537.36 (KHTML, like Gecko) Chrome/' + CFG.uaMajor + '.0.0.0 Safari/537.36' +
        (CFG.browserBrand === 'edge' ? ' Edg/' + CFG.uaMajor + '.0.0.0' : ''),
        'get userAgent');

    // Client Hints in workers
    if (g.NavigatorUAData && g.NavigatorUAData.prototype) {
        const uap = g.NavigatorUAData.prototype;
        defineOn(uap, 'brands', function() {
            return CFG.brands.map(b => ({ brand: b.brand, version: b.version }));
        }, 'get brands');
        defineOn(uap, 'platform', function() { return chPlatform; }, 'get platform');
        const origGet = uap.getHighEntropyValues;
        if (origGet) {
            uap.getHighEntropyValues = markNative(function(hints) {
                const wanted = Array.isArray(hints) ? hints : [];
                return origGet.apply(this, arguments).then(res => {
                    const out = Object.assign({}, res);
                    const include = (name) => (name in out) || wanted.includes(name);
                    if (include('brands')) out.brands = CFG.brands.map(b => Object.assign({}, b));
                    if (include('fullVersionList')) out.fullVersionList = CFG.fullVersionList.map(b => Object.assign({}, b));
                    if (include('uaFullVersion')) out.uaFullVersion = CFG.uaFullVersion;
                    if (include('platform')) out.platform = chPlatform;
                    if (include('platformVersion')) out.platformVersion = chPlatformVer;
                    if (include('architecture')) out.architecture = chArch;
                    if (include('bitness')) out.bitness = '64';
                    if (include('model')) out.model = '';
                    if (include('mobile')) out.mobile = false;
                    if (include('wow64')) out.wow64 = false;
                    if (include('formFactors')) out.formFactors = ['Desktop'];
                    return out;
                });
            }, 'getHighEntropyValues', 1);
        }
    }

    function canvasIsComplex(data) {
        // S5: CreepJS flags noise on trivial solid/simple probes. Only vary
        // canvases that already contain real visual complexity (text, images,
        // gradients). The same test runs for getImageData/toDataURL/toBlob, so
        // all three paths stay consistent.
        const pixels = data.length >> 2;
        if (pixels < 256) return false;
        const seen = new Set();
        const stride = Math.max(1, Math.floor(pixels / 4096));
        for (let p = 0; p < pixels; p += stride) {
            const i = p * 4;
            if (data[i + 3] !== 255) continue;
            const key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2];
            seen.add(key);
            if (seen.size >= 64) return true;
        }
        return false;
    }

    function canvasNoise(data, seed, startX, startY, width) {
        if (!canvasIsComplex(data)) return;
        for (let i = 0; i < data.length; i += 4) {
            const pixelIndex = i / 4;
            const absX = startX + (pixelIndex % width);
            const absY = startY + Math.floor(pixelIndex / width);
            const hash = ((absX * 374761393 + absY * 668265263 + seed) ^ (seed >>> 13)) >>> 0;
            if (data[i + 3] === 255 && (hash % 7) === 0) {
                const delta = (hash % 2 === 0) ? 1 : -1;
                const rNoise = ((hash * 1103515245 + 12345) >>> 16) % 3;
                data[i]     = Math.min(255, Math.max(0, data[i]     + (rNoise >= 1 ? delta : 0)));
                data[i + 1] = Math.min(255, Math.max(0, data[i + 1] + (rNoise <= 1 ? delta : 0)));
                data[i + 2] = Math.min(255, Math.max(0, data[i + 2] + (rNoise >= 1 ? -delta : delta)));
            }
        }
    }

    if (!DISABLED.has('canvas') && typeof OffscreenCanvas !== 'undefined') {
        let origOffGID = null;
        if (typeof OffscreenCanvasRenderingContext2D !== 'undefined') {
            const p = OffscreenCanvasRenderingContext2D.prototype;
            origOffGID = p.getImageData;
            p.getImageData = markNative(function(sx, sy, sw, sh) {
                const img = origOffGID.apply(this, arguments);
                canvasNoise(img.data, CFG.canvasSeed || 12345, sx || 0, sy || 0, sw || 1);
                return img;
            }, 'getImageData', 4);
        }
        const origBlob = OffscreenCanvas.prototype.convertToBlob;
        if (origBlob) {
            OffscreenCanvas.prototype.convertToBlob = markNative(function(options) {
                try {
                    if (this.width > 0 && this.height > 0) {
                        const ctx = this.getContext('2d');
                        if (ctx) {
                            const img = origOffGID
                                ? origOffGID.apply(ctx, [0, 0, this.width, this.height])
                                : ctx.getImageData(0, 0, this.width, this.height);
                            canvasNoise(img.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                            ctx.putImageData(img, 0, 0);
                        }
                    }
                } catch(e) {}
                return origBlob.apply(this, arguments);
            }, 'convertToBlob', 0);
        }
    }

    function hookWebGL(glProto) {
        if (!glProto) return;
        const orig = glProto.getParameter;
        const limits = CFG.webglLimits;
        glProto.getParameter = markNative(function(param) {
            if (param === 0x9245) return CFG.webglVendor;
            if (param === 0x9246) return CFG.webglRenderer;
            if (limits) {
                // Cross-OS only. Same-OS profiles use the native backend,
                // whose limits already match a real Chrome on this machine.
                if (param === 0x0D3A) return new Int32Array(limits.maxViewportDims);
                if (param === 0x0D33) return limits.maxTextureSize;
                if (param === 0x84E8) return limits.maxRenderbufferSize;
                if (param === 0x851C) return limits.maxCubeMapTextureSize;
            }
            return orig.apply(this, arguments);
        }, 'getParameter', 1);
    }
    if (!DISABLED.has('webgl')) {
        try {
            if (g.WebGLRenderingContext) hookWebGL(g.WebGLRenderingContext.prototype);
            if (g.WebGL2RenderingContext) hookWebGL(g.WebGL2RenderingContext.prototype);
        } catch(e) {}
    }

    // The classic Worker wrapper loads the original script from a blob URL.
    // Resolve relative importScripts() against the original script URL.
    if (g.__adWorkerBase && typeof g.importScripts === 'function') {
        const origImport = g.importScripts.bind(g);
        g.importScripts = markNative(function() {
            const urls = Array.from(arguments).map(function(u) {
                try { return new URL(String(u), g.__adWorkerBase).href; } catch(e) { return u; }
            });
            return origImport.apply(g, urls);
        }, 'importScripts', 1);
    }
})();
"""
    replacements = {
        "__CFG__": json.dumps(cfg),
        "__DISABLED__": json.dumps(sorted(_disabled_stealth_groups())),
        "__PLATFORM__": json.dumps(platform),
        "__CH_PLATFORM__": json.dumps(ch_platform),
        "__CH_PLATFORM_VER__": json.dumps(ch_platform_ver),
        "__CH_ARCH__": json.dumps(ch_arch),
    }
    for key, value in replacements.items():
        script = script.replace(key, value)
    return script


def get_stealth_script(fp: FingerprintConfig) -> str:
    """Tạo mã JavaScript tiêm trước khi trang load (Page.addScriptToEvaluateOnNewDocument)
    để can thiệp dấu vân tay (Canvas, WebGL, AudioContext, Navigator, Screen, Fonts, WebGPU).
    """

    fp_json = json.dumps(_stealth_config(fp))

    worker_js = get_worker_stealth_script(fp)
    script = f"""
(function() {{
    if (globalThis.__adStealth) return;
    try {{ Object.defineProperty(globalThis, '__adStealth', {{ value: true, configurable: true }}); }} catch(e) {{}}
    const CFG = {fp_json};
    const DISABLED = new Set({json.dumps(sorted(_disabled_stealth_groups()))});
    const platformStr = (CFG.os === 'macos') ? 'MacIntel' : ((CFG.os === 'linux') ? 'Linux x86_64' : 'Win32');
    const chPlatform = (CFG.os === 'macos') ? 'macOS' : ((CFG.os === 'linux') ? 'Linux' : 'Windows');
    const chPlatformVer = (CFG.os === 'macos') ? '14.5.0' : ((CFG.os === 'linux') ? '6.5.0' : '15.0.0');
    const chArch = (CFG.os === 'macos') ? 'arm' : 'x86';

    // Frozen timestamps cho chrome.csi và chrome.loadTimes (S10)
    const _startE = Date.now();
    const _nowSec = _startE / 1000;

    // ==========================================
    // Helper: maintain native-looking name/length on wrapped getters.
    // NOTE: Function.prototype.toString must stay native. A Proxy-based
    // toString hook was detectable (CreepJS hasToStringProxy = true) and was
    // the source of the remaining 20% stealth score.
    // ==========================================
    function markNative(fn, name, length, isConstructor) {{
        let target = fn;
        // Concise method syntax tạo hàm không có .prototype (chuẩn native method / getter)
        // và tự động ném TypeError khi truy cập .arguments/.caller (chuẩn strict mode V8)
        if (!isConstructor) {{
            target = {{
                [name](...args) {{
                    return fn.apply(this, args);
                }}
            }}[name];
        }}
        Object.defineProperty(target, 'name', {{ value: name, configurable: true }});
        if (typeof length === 'number') {{
            Object.defineProperty(target, 'length', {{ value: length, configurable: true }});
        }}
        return target;
    }}

    // ==========================================
    // Canvas Noise Generator (2D)
    // Only touch fully opaque pixels: adding +/-1 to a partially transparent
    // pixel changes its premultiplied round-trip and makes toDataURL() differ
    // from getImageData() after PNG decode. Opaque text/shape pixels still vary.
    // ==========================================
    function canvasIsComplex(data) {{
        // S5: skip trivial solid/simple canvases. CreepJS's rgba-noise probe
        // uses a simple image; real text/images/gradients still get noise.
        const pixels = data.length >> 2;
        if (pixels < 256) return false;
        const seen = new Set();
        const stride = Math.max(1, Math.floor(pixels / 4096));
        for (let p = 0; p < pixels; p += stride) {{
            const i = p * 4;
            if (data[i + 3] !== 255) continue;
            const key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2];
            seen.add(key);
            if (seen.size >= 64) return true;
        }}
        return false;
    }}

    function canvasNoise(data, seed, startX, startY, width) {{
        if (!canvasIsComplex(data)) return;
        for (let i = 0; i < data.length; i += 4) {{
            const pixelIndex = i / 4;
            const absX = startX + (pixelIndex % width);
            const absY = startY + Math.floor(pixelIndex / width);
            const hash = ((absX * 374761393 + absY * 668265263 + seed) ^ (seed >>> 13)) >>> 0;
            if (data[i + 3] === 255 && (hash % 7) === 0) {{
                const delta = (hash % 2 === 0) ? 1 : -1;
                const rNoise = ((hash * 1103515245 + 12345) >>> 16) % 3;
                data[i]     = Math.min(255, Math.max(0, data[i]     + (rNoise >= 1 ? delta : 0)));
                data[i + 1] = Math.min(255, Math.max(0, data[i + 1] + (rNoise <= 1 ? delta : 0)));
                data[i + 2] = Math.min(255, Math.max(0, data[i + 2] + (rNoise >= 1 ? -delta : delta)));
            }}
        }}
    }}

    // Danh sách font đặc thù từng OS để bảo vệ Font Fingerprinting (S11)
    const macFonts = ['sf pro', 'helvetica neue', 'apple-system', 'blinkmacsystemfont', 'geneva', 'menlo', 'monaco'];
    const winFonts = ['segoe ui', 'calibri', 'cambria', 'consolas', 'ms sans serif', 'ms serif'];

    // ==========================================
    // Hàm áp dụng Stealth vào Realm Window (hỗ trợ cả iframe) (S2)
    // ==========================================
    function applyStealth(w) {{
        if (!w) return;
        if (w !== window) {{
            try {{
                if (w.__adStealth) return;
                Object.defineProperty(w, '__adStealth', {{ value: true, configurable: true }});
            }} catch(e) {{}}
        }}

        // 1. Chuẩn hóa navigator.webdriver === false theo chuẩn W3C
        if (!DISABLED.has('navigator')) try {{
            if (w.Navigator && w.Navigator.prototype) {{
                const proto = w.Navigator.prototype;
                if (!('webdriver' in proto) || w.navigator.webdriver !== false) {{
                    Object.defineProperty(proto, 'webdriver', {{
                        get: markNative(function() {{
                            if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                            return false;
                        }}, 'get webdriver', 0),
                        enumerable: true,
                        configurable: true
                    }});
                }}

                // 2. Hardware Concurrency, Device Memory, Languages, Platform (S8)
                Object.defineProperty(proto, 'hardwareConcurrency', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return CFG.hardwareConcurrency;
                    }}, 'get hardwareConcurrency', 0),
                    enumerable: true, configurable: true
                }});

                Object.defineProperty(proto, 'deviceMemory', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return CFG.deviceMemory || 8;
                    }}, 'get deviceMemory', 0),
                    enumerable: true, configurable: true
                }});

                const langList = CFG.languages;
                Object.defineProperty(proto, 'languages', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return Object.freeze([...langList]);
                    }}, 'get languages', 0),
                    enumerable: true, configurable: true
                }});

                Object.defineProperty(proto, 'language', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return CFG.locale;
                    }}, 'get language', 0),
                    enumerable: true, configurable: true
                }});

                Object.defineProperty(proto, 'platform', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return platformStr;
                    }}, 'get platform', 0),
                    enumerable: true, configurable: true
                }});
            }}
        }} catch(e) {{}}

        // 3. Hook NavigatorUAData Client Hints trong JS (S9)
        if (!DISABLED.has('navigator')) try {{
            if (w.NavigatorUAData && w.NavigatorUAData.prototype) {{
                const uap = w.NavigatorUAData.prototype;
                Object.defineProperty(uap, 'brands', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.NavigatorUAData)) throw new TypeError('Illegal invocation');
                        return CFG.brands.map(b => Object.freeze({{ brand: b.brand, version: b.version }}));
                    }}, 'get brands', 0),
                    enumerable: true, configurable: true
                }});
                Object.defineProperty(uap, 'platform', {{
                    get: markNative(function() {{
                        if (!(this instanceof w.NavigatorUAData)) throw new TypeError('Illegal invocation');
                        return chPlatform;
                    }}, 'get platform', 0),
                    enumerable: true, configurable: true
                }});

                const origGetHighEntropyValues = uap.getHighEntropyValues;
                if (origGetHighEntropyValues) {{
                    uap.getHighEntropyValues = markNative(function(hints) {{
                        const wanted = Array.isArray(hints) ? hints : [];
                        return origGetHighEntropyValues.apply(this, arguments).then(res => {{
                            const out = Object.assign({{}}, res);
                            const include = (name) => (name in out) || wanted.includes(name);
                            if (include('brands')) out.brands = CFG.brands.map(b => Object.assign({{}}, b));
                            if (include('fullVersionList')) out.fullVersionList = CFG.fullVersionList.map(b => Object.assign({{}}, b));
                            if (include('uaFullVersion')) out.uaFullVersion = CFG.uaFullVersion;
                            if (include('platform')) out.platform = chPlatform;
                            if (include('platformVersion')) out.platformVersion = chPlatformVer;
                            if (include('architecture')) out.architecture = chArch;
                            if (include('bitness')) out.bitness = '64';
                            if (include('model')) out.model = '';
                            if (include('mobile')) out.mobile = false;
                            if (include('wow64')) out.wow64 = false;
                            if (include('formFactors')) out.formFactors = ['Desktop'];
                            return out;
                        }});
                    }}, 'getHighEntropyValues', 1);
                }}
            }}
        }} catch(e) {{}}

        // 4. Mock window.chrome runtime & BotGuard (S10: frozen timestamps)
        if (!DISABLED.has('chrome')) try {{
            if (!w.chrome) w.chrome = {{}};
            if (!w.chrome.app) {{
                w.chrome.app = {{
                    isInstalled: false,
                    getIsInstalled: markNative(function() {{ return false; }}, 'getIsInstalled', 0),
                    getDetails: markNative(function() {{ return null; }}, 'getDetails', 0),
                    InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
                    RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }}
                }};
            }}
            if (!w.chrome.csi) {{
                w.chrome.csi = markNative(function() {{
                    return {{
                        onloadT: _startE + 145,
                        startE: _startE,
                        pageT: performance.now(),
                        tran: 15
                    }};
                }}, 'csi', 0);
            }}
            if (!w.chrome.loadTimes) {{
                w.chrome.loadTimes = markNative(function() {{
                    return {{
                        commitLoadTime: _nowSec + 0.1,
                        connectionInfo: 'h2',
                        connectionreused: true,
                        finishDocumentLoadTime: _nowSec + 0.15,
                        finishLoadTime: _nowSec + 0.15,
                        firstPaintAfterLoadTime: 0,
                        firstPaintTime: _nowSec + 0.08,
                        navigationType: 'Other',
                        npnNegotiatedProtocol: 'h2',
                        requestTime: _nowSec - 0.16,
                        startLoadTime: _nowSec - 0.16,
                        wasAlternateProtocolAvailable: false,
                        wasFetchedViaSpdy: true,
                        wasNpnNegotiated: true
                    }};
                }}, 'loadTimes', 0);
            }}
        }} catch(e) {{}}

        // 5. Screen, DPR and CSS media queries are handled natively by
        // Emulation.setDeviceMetricsOverride in the launcher. Hooking the
        // Screen prototype or the media-query API was detectable and made JS
        // disagree with CSS. Keep this section empty on purpose.

        // 6. MediaDevices enumerateDevices
        if (!DISABLED.has('media')) try {{
            if (w.navigator && w.navigator.mediaDevices && w.navigator.mediaDevices.enumerateDevices) {{
                const origEnumerate = w.navigator.mediaDevices.enumerateDevices;
                w.navigator.mediaDevices.enumerateDevices = markNative(function() {{
                    return origEnumerate.apply(this, arguments).then(devices => {{
                        return devices.map(d => {{
                            let label = d.label;
                            if (label) {{
                                if (d.kind === 'audioinput') label = (CFG.os === 'macos') ? 'Built-in Microphone' : 'Microphone (Realtek High Definition Audio)';
                                else if (d.kind === 'audiooutput') label = (CFG.os === 'macos') ? 'Internal Speakers' : 'Speakers (Realtek High Definition Audio)';
                                else if (d.kind === 'videoinput') label = (CFG.os === 'macos') ? 'FaceTime HD Camera' : 'Integrated Camera';
                            }}
                            return {{
                                deviceId: d.deviceId,
                                kind: d.kind,
                                label: label,
                                groupId: d.groupId,
                                toJSON: markNative(function() {{ return {{ deviceId: this.deviceId, kind: this.kind, label: this.label, groupId: this.groupId }}; }}, 'toJSON', 0)
                            }};
                        }});
                    }});
                }}, 'enumerateDevices', 0);
            }}
        }} catch(e) {{}}

        // 7. WebGPU Masking (S5)
        if (!DISABLED.has('webgpu')) try {{
            if (w.navigator && w.navigator.gpu && w.navigator.gpu.requestAdapter) {{
                const origReqAdapter = w.navigator.gpu.requestAdapter;
                const renderer = CFG.webglRenderer || '';
                const rendererMatch = /Renderer\s*:\s*([^,)]+)/i.exec(renderer);
                let gpuName = rendererMatch ? rendererMatch[1].trim() : renderer;
                if (!rendererMatch) {{
                    const parts = renderer.split(',');
                    if (parts.length > 1) gpuName = parts[1];
                }}
                gpuName = gpuName.replace(/Direct3D.*$/i, '').trim();
                const vClean = (CFG.webglVendor || '').toLowerCase();
                const vShort = vClean.includes('nvidia') ? 'nvidia'
                    : (vClean.includes('amd') ? 'amd'
                    : (vClean.includes('apple') ? 'apple' : 'intel'));
                let arch = '';
                if (vShort === 'apple') arch = 'metal-3';
                else if (/RTX\s*50/i.test(gpuName)) arch = 'blackwell';
                else if (/RTX\s*40/i.test(gpuName)) arch = 'ada';
                else if (/RTX\s*30/i.test(gpuName)) arch = 'ampere';
                else if (/RTX\s*20|GTX\s*16/i.test(gpuName)) arch = 'turing';
                else if (vShort === 'amd') arch = 'rdna-3';
                else if (vShort === 'intel') arch = 'gen-12lp';
                else if (vShort === 'nvidia') arch = 'ampere';
                const fakeAdapterInfo = {{
                    vendor: vShort,
                    architecture: arch,
                    device: '',
                    description: gpuName
                }};
                w.navigator.gpu.requestAdapter = markNative(async function(options) {{
                    const adapter = await origReqAdapter.apply(this, arguments);
                    if (!adapter) return null;
                    return new Proxy(adapter, {{
                        get(target, prop) {{
                            if (prop === 'info') return fakeAdapterInfo;
                            if (prop === 'requestAdapterInfo') {{
                                // Chrome 155 removed this legacy method. Only
                                // expose it when the real adapter still has it;
                                // adding it would make `in` and `typeof` disagree.
                                if (!(prop in target)) return undefined;
                                return markNative(async function() {{
                                    return fakeAdapterInfo;
                                }}, 'requestAdapterInfo', 0);
                            }}
                            const value = Reflect.get(target, prop, target);
                            if (typeof value === 'function') {{
                                // WebIDL methods brand-check their receiver; a
                                // Proxy receiver causes "Illegal invocation".
                                return markNative(value.bind(target), prop, value.length);
                            }}
                            return value;
                        }},
                        has(target, prop) {{
                            if (prop === 'requestAdapterInfo') return prop in target;
                            return Reflect.has(target, prop);
                        }}
                    }});
                }}, 'requestAdapter', 0);
            }}
        }} catch(e) {{}}

        // 8. SpeechSynthesis Voices (S7: OS matching)
        if (!DISABLED.has('speech')) try {{
            if (w.speechSynthesis && w.speechSynthesis.getVoices) {{
                const origGetVoices = w.speechSynthesis.getVoices;
                w.speechSynthesis.getVoices = markNative(function() {{
                    const voices = origGetVoices.apply(this, arguments);
                    if (!voices || !voices.length) return voices;
                    if (CFG.os === 'macos') {{
                        return voices.filter(v => !v.name.includes('Microsoft'));
                    }} else if (CFG.os === 'windows') {{
                        return voices.filter(v => !v.name.includes('Apple') && !v.name.includes('Samantha') && !v.name.includes('Alex'));
                    }}
                    return voices;
                }}, 'getVoices', 0);
            }}
        }} catch(e) {{}}

        // 9. Canvas 2D Spoofing & Font Probing Mask (S11)
        if (!DISABLED.has('canvas') || !DISABLED.has('fonts')) try {{
            let origCanvasGID = null;
            if (w.CanvasRenderingContext2D && w.CanvasRenderingContext2D.prototype) {{
                const ctxProto = w.CanvasRenderingContext2D.prototype;
                if (!DISABLED.has('canvas')) {{
                    origCanvasGID = ctxProto.getImageData;
                    ctxProto.getImageData = markNative(function(sx, sy, sw, sh) {{
                        const imgData = origCanvasGID.apply(this, arguments);
                        canvasNoise(imgData.data, CFG.canvasSeed || 12345, sx || 0, sy || 0, sw || 1);
                        return imgData;
                    }}, 'getImageData', 4);
                }}

                // Bảo vệ Font Probing qua measureText (S11)
                if (!DISABLED.has('fonts')) {{
                    const origMeasureText = ctxProto.measureText;
                    ctxProto.measureText = markNative(function(text) {{
                        const font = (this.font || '').toLowerCase();
                        const res = origMeasureText.apply(this, arguments);
                        if (CFG.os === 'macos') {{
                            for (const wf of winFonts) {{
                                if (font.includes(wf)) {{
                                    const prevFont = this.font;
                                    this.font = font.replace(new RegExp(wf, 'g'), 'non_existent_fallback_font');
                                    const fallbackRes = origMeasureText.apply(this, arguments);
                                    this.font = prevFont;
                                    return fallbackRes;
                                }}
                            }}
                            for (const mf of macFonts) {{
                                if (font.includes(mf)) {{
                                    const width = res.width + 6.5;
                                    return new Proxy(res, {{
                                        get(t, prop) {{
                                            if (prop === 'width') return width;
                                            return Reflect.get(t, prop);
                                        }}
                                    }});
                                }}
                            }}
                        }} else if (CFG.os === 'windows') {{
                            for (const mf of macFonts) {{
                                if (font.includes(mf)) {{
                                    const prevFont = this.font;
                                    this.font = font.replace(new RegExp(mf, 'g'), 'non_existent_fallback_font');
                                    const fallbackRes = origMeasureText.apply(this, arguments);
                                    this.font = prevFont;
                                    return fallbackRes;
                                }}
                            }}
                        }}
                        return res;
                    }}, 'measureText', 1);
                }}
            }}

            if (!DISABLED.has('canvas') && w.HTMLCanvasElement && w.HTMLCanvasElement.prototype) {{
                const canvasProto = w.HTMLCanvasElement.prototype;
                const origToDataURL = canvasProto.toDataURL;
                canvasProto.toDataURL = markNative(function() {{
                    let ctx = null;
                    let backup = null;
                    try {{
                        if (this.width > 0 && this.height > 0) {{
                            // A WebGL canvas has no 2D context. Read its pixels,
                            // add deterministic noise, and encode a 2D copy so
                            // WebGL toDataURL hashes differ per profile too.
                            ctx = this.getContext('2d');
                            if (!ctx) {{
                                let gl = null;
                                try {{ gl = this.getContext('webgl2'); }} catch(e) {{}}
                                if (!gl) {{
                                    try {{ gl = this.getContext('webgl') || this.getContext('experimental-webgl'); }} catch(e) {{}}
                                }}
                                if (gl) {{
                                    const dw = gl.drawingBufferWidth || this.width;
                                    const dh = gl.drawingBufferHeight || this.height;
                                    if (dw > 0 && dh > 0) {{
                                        const pixels = new Uint8Array(dw * dh * 4);
                                        gl.readPixels(0, 0, dw, dh, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
                                        const copy = w.document.createElement('canvas');
                                        copy.width = dw;
                                        copy.height = dh;
                                        const cctx = copy.getContext('2d');
                                        if (cctx) {{
                                            const img = cctx.createImageData(dw, dh);
                                            for (let y = 0; y < dh; y++) {{
                                                const src = (dh - 1 - y) * dw * 4;
                                                const dst = y * dw * 4;
                                                img.data.set(pixels.subarray(src, src + dw * 4), dst);
                                            }}
                                            canvasNoise(img.data, CFG.canvasSeed || 12345, 0, 0, dw);
                                            cctx.putImageData(img, 0, 0);
                                            return origToDataURL.apply(copy, arguments);
                                        }}
                                    }}
                                }}
                            }}
                            // Read/noise/restore the real canvas instead of drawing
                            // it onto a helper. drawImage() can color-convert and
                            // make toDataURL() differ from getImageData() by one.
                            if (ctx && origCanvasGID) {{
                                const imgData = origCanvasGID.apply(ctx, [0, 0, this.width, this.height]);
                                backup = new Uint8ClampedArray(imgData.data);
                                canvasNoise(imgData.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                                ctx.putImageData(imgData, 0, 0);
                                return origToDataURL.apply(this, arguments);
                            }}
                        }}
                    }} catch(e) {{}} finally {{
                        if (ctx && backup) {{
                            try {{
                                const restored = ctx.createImageData(this.width, this.height);
                                restored.data.set(backup);
                                ctx.putImageData(restored, 0, 0);
                            }} catch(e) {{}}
                        }}
                    }}
                    return origToDataURL.apply(this, arguments);
                }}, 'toDataURL', 0);

                const origToBlob = canvasProto.toBlob;
                canvasProto.toBlob = markNative(function(callback) {{
                    let restore = null;
                    try {{
                        if (this.width > 0 && this.height > 0) {{
                            const ctx = this.getContext('2d');
                            if (ctx && origCanvasGID) {{
                                const imgData = origCanvasGID.apply(ctx, [0, 0, this.width, this.height]);
                                let backup = new Uint8ClampedArray(imgData.data);
                                canvasNoise(imgData.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                                ctx.putImageData(imgData, 0, 0);
                                const canvas = this;
                                restore = function() {{
                                    if (!backup) return;
                                    try {{
                                        const restored = ctx.createImageData(canvas.width, canvas.height);
                                        restored.data.set(backup);
                                        ctx.putImageData(restored, 0, 0);
                                    }} catch(e) {{}}
                                    backup = null;
                                }};
                                const args = Array.prototype.slice.call(arguments);
                                const userCb = args[0];
                                args[0] = function(blob) {{
                                    restore();
                                    if (typeof userCb === 'function') userCb(blob);
                                }};
                                return origToBlob.apply(this, args);
                            }}
                        }}
                    }} catch(e) {{
                        if (restore) restore();
                    }}
                    return origToBlob.apply(this, arguments);
                }}, 'toBlob', 1);
            }}

            // OffscreenCanvas hooks
            if (!DISABLED.has('canvas') && typeof w.OffscreenCanvas !== 'undefined') {{
                let origOffGID = null;
                if (typeof w.OffscreenCanvasRenderingContext2D !== 'undefined') {{
                    const offCtxProto = w.OffscreenCanvasRenderingContext2D.prototype;
                    origOffGID = offCtxProto.getImageData;
                    offCtxProto.getImageData = markNative(function(sx, sy, sw, sh) {{
                        const imgData = origOffGID.apply(this, arguments);
                        canvasNoise(imgData.data, CFG.canvasSeed || 12345, sx || 0, sy || 0, sw || 1);
                        return imgData;
                    }}, 'getImageData', 4);

                    const origOffMT = offCtxProto.measureText;
                    if (origOffMT) {{
                        offCtxProto.measureText = markNative(function(text) {{
                            return origOffMT.apply(this, arguments);
                        }}, 'measureText', 1);
                    }}
                }}
                const origConvertToBlob = w.OffscreenCanvas.prototype.convertToBlob;
                if (origConvertToBlob) {{
                    w.OffscreenCanvas.prototype.convertToBlob = markNative(function(options) {{
                        try {{
                            if (this.width > 0 && this.height > 0) {{
                                const ctx = this.getContext('2d');
                                if (ctx) {{
                                    const imgData = origOffGID
                                        ? origOffGID.apply(ctx, [0, 0, this.width, this.height])
                                        : ctx.getImageData(0, 0, this.width, this.height);
                                    const backup = new Uint8ClampedArray(imgData.data);
                                    canvasNoise(imgData.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                                    ctx.putImageData(imgData, 0, 0);
                                    const canvas = this;
                                    const restore = function() {{
                                        try {{
                                            const restored = ctx.createImageData(canvas.width, canvas.height);
                                            restored.data.set(backup);
                                            ctx.putImageData(restored, 0, 0);
                                        }} catch(e) {{}}
                                    }};
                                    const result = origConvertToBlob.apply(this, arguments);
                                    if (result && typeof result.finally === 'function') {{
                                        return result.finally(restore);
                                    }}
                                    restore();
                                    return result;
                                }}
                            }}
                        }} catch(e) {{}}
                        return origConvertToBlob.apply(this, arguments);
                    }}, 'convertToBlob', 0);
                }}
            }}

            // document.fonts.check Font Masking (S11)
            if (!DISABLED.has('fonts') && w.document && w.document.fonts && w.document.fonts.check) {{
                const origFontsCheck = w.document.fonts.check;
                w.document.fonts.check = markNative(function(font, text) {{
                    const f = (font || '').toLowerCase();
                    if (CFG.os === 'macos') {{
                        for (const wf of winFonts) if (f.includes(wf)) return false;
                        for (const mf of macFonts) if (f.includes(mf)) return true;
                    }} else if (CFG.os === 'windows') {{
                        for (const mf of macFonts) if (f.includes(mf)) return false;
                        for (const wf of winFonts) if (f.includes(wf)) return true;
                    }}
                    return origFontsCheck.apply(this, arguments);
                }}, 'check', 1);
            }}
        }} catch(e) {{}}

        // 10. WebGL Vendor & Renderer & Extensions & readPixels (S12) & ShaderPrecision (S6)
        // S1: only UNMASKED_VENDOR/RENDERER are spoofed for same-OS profiles.
        // Native limits are already correct for the host backend. Cross-OS
        // profiles use CFG.webglLimits, measured from real GPUs.
        function hookWebGL(glProto) {{
            if (!glProto) return;
            const UNMASKED_VENDOR_WEBGL = 0x9245;
            const UNMASKED_RENDERER_WEBGL = 0x9246;
            const MAX_VIEWPORT_DIMS = 0x0D3A;
            const MAX_TEXTURE_SIZE = 0x0D33;
            const MAX_RENDERBUFFER_SIZE = 0x84E8;
            const MAX_CUBE_MAP_TEXTURE_SIZE = 0x851C;
            const limits = CFG.webglLimits;

            const origGetParameter = glProto.getParameter;
            glProto.getParameter = markNative(function(param) {{
                if (param === UNMASKED_VENDOR_WEBGL) return CFG.webglVendor;
                if (param === UNMASKED_RENDERER_WEBGL) return CFG.webglRenderer;
                if (limits) {{
                    if (param === MAX_VIEWPORT_DIMS) return new Int32Array(limits.maxViewportDims);
                    if (param === MAX_TEXTURE_SIZE) return limits.maxTextureSize;
                    if (param === MAX_RENDERBUFFER_SIZE) return limits.maxRenderbufferSize;
                    if (param === MAX_CUBE_MAP_TEXTURE_SIZE) return limits.maxCubeMapTextureSize;
                }}
                return origGetParameter.apply(this, arguments);
            }}, 'getParameter', 1);

            const _filteredExts = new Set();
            const origGetSupportedExtensions = glProto.getSupportedExtensions;
            glProto.getSupportedExtensions = markNative(function() {{
                let exts = origGetSupportedExtensions.apply(this, arguments);
                if (!exts) return exts;
                const renderer = CFG.webglRenderer || '';
                if (renderer.includes('Apple') || renderer.includes('Metal')) {{
                    const filtered = exts.filter(e => !e.includes('ANGLE') && !e.includes('WGL') && !e.includes('WEBGL_multi_draw'));
                    exts.forEach(e => {{ if (!filtered.includes(e)) _filteredExts.add(e); }});
                    return filtered;
                }}
                return exts;
            }}, 'getSupportedExtensions', 0);

            const origGetExtension = glProto.getExtension;
            glProto.getExtension = markNative(function(name) {{
                if (_filteredExts.has(name)) return null;
                return origGetExtension.apply(this, arguments);
            }}, 'getExtension', 1);

            // C10 & S12: readPixels — đa kênh RGB và Float32Array safe
            const origReadPixels = glProto.readPixels;
            glProto.readPixels = markNative(function(x, y, w, h, format, type, pixels) {{
                origReadPixels.apply(this, arguments);
                if (pixels && pixels.length) {{
                    const seed = CFG.canvasSeed || 12345;
                    const isFloat = (pixels instanceof Float32Array || pixels instanceof Float64Array);
                    const step = 24;
                    for (let i = 0; i < pixels.length; i += step) {{
                        // Mix the seed per pixel. The old parity-only formula
                        // produced identical deltas for two even seeds, so
                        // different profiles could have the same readPixels hash.
                        let h = (seed ^ Math.imul(i + 1, 0x9E3779B1)) >>> 0;
                        h = Math.imul(h ^ (h >>> 16), 0x85EBCA6B) >>> 0;
                        const delta = ((h >>> 8) & 1) ? 1 : -1;
                        if (isFloat) {{
                            const fDelta = delta * 0.001;
                            pixels[i] = Math.max(0.0, Math.min(1.0, pixels[i] + fDelta));
                            if (i + 1 < pixels.length) pixels[i + 1] = Math.max(0.0, Math.min(1.0, pixels[i + 1] - fDelta));
                            if (i + 2 < pixels.length) pixels[i + 2] = Math.max(0.0, Math.min(1.0, pixels[i + 2] + fDelta));
                        }} else {{
                            pixels[i] = Math.min(255, Math.max(0, pixels[i] + delta));
                            if (i + 1 < pixels.length) pixels[i + 1] = Math.min(255, Math.max(0, pixels[i + 1] - delta));
                            if (i + 2 < pixels.length) pixels[i + 2] = Math.min(255, Math.max(0, pixels[i + 2] + delta));
                        }}
                    }}
                }}
            }}, 'readPixels', 7);

            // S6: WebGL Shader Precision Format
            const origGetShaderPrecisionFormat = glProto.getShaderPrecisionFormat;
            if (origGetShaderPrecisionFormat) {{
                glProto.getShaderPrecisionFormat = markNative(function(shaderType, precisionType) {{
                    return origGetShaderPrecisionFormat.apply(this, arguments);
                }}, 'getShaderPrecisionFormat', 2);
            }}
        }}

        if (!DISABLED.has('webgl')) try {{
            if (w.WebGLRenderingContext) hookWebGL(w.WebGLRenderingContext.prototype);
            if (w.WebGL2RenderingContext) hookWebGL(w.WebGL2RenderingContext.prototype);
        }} catch(e) {{}}

        // 11. AudioContext Spoofing
        if (!DISABLED.has('audio')) try {{
            const processedBuffers = new WeakMap();
            if (w.AudioBuffer) {{
                const origGetChannelData = w.AudioBuffer.prototype.getChannelData;
                w.AudioBuffer.prototype.getChannelData = markNative(function(channel) {{
                    const data = origGetChannelData.apply(this, arguments);
                    if (!processedBuffers.has(this)) processedBuffers.set(this, new Set());
                    const channels = processedBuffers.get(this);
                    if (!channels.has(channel)) {{
                        channels.add(channel);
                        const seed = CFG.audioSeed || 12345;
                        let s = seed + channel * 7919;
                        for (let i = 0; i < data.length; i += 100) {{
                            s = (s * 1103515245 + 12345) & 0x7fffffff;
                            const noise = ((s / 0x7fffffff) - 0.5) * 0.0001;
                            data[i] += noise;
                        }}
                    }}
                    return data;
                }}, 'getChannelData', 1);

                if (w.AudioBuffer.prototype.copyFromChannel) {{
                    const origCopyFromChannel = w.AudioBuffer.prototype.copyFromChannel;
                    w.AudioBuffer.prototype.copyFromChannel = markNative(function(destination, channelNumber, bufferOffset) {{
                        this.getChannelData(channelNumber);
                        return origCopyFromChannel.apply(this, arguments);
                    }}, 'copyFromChannel', 2);
                }}
            }}

            if (w.AnalyserNode) {{
                function hookAnalyser(methodName, nativeLen) {{
                    const orig = w.AnalyserNode.prototype[methodName];
                    if (!orig) return;
                    w.AnalyserNode.prototype[methodName] = markNative(function(array) {{
                        orig.apply(this, arguments);
                        const seed = CFG.audioSeed || 12345;
                        let s = seed;
                        for (let i = 0; i < array.length; i += 13) {{
                            s = (s * 1103515245 + 12345) & 0x7fffffff;
                            const noise = ((s / 0x7fffffff) - 0.5) * 0.01;
                            array[i] += noise;
                        }}
                    }}, methodName, nativeLen);
                }}
                hookAnalyser('getFloatFrequencyData', 1);
                hookAnalyser('getByteFrequencyData', 1);
                hookAnalyser('getFloatTimeDomainData', 1);
                hookAnalyser('getByteTimeDomainData', 1);
            }}
        }} catch(e) {{}}

        // 12. Web Worker Hook (S3 & S14)
        if (!DISABLED.has('worker')) try {{
            if (w.Worker) {{
                const origWorker = w.Worker;
                const workerHookCode = {json.dumps(worker_js)};
                w.Worker = markNative(function(scriptURL, options) {{
                    if (options && options.type === 'module') {{
                        // Module workers are covered by the CDP worker target hook.
                        return new origWorker(scriptURL, options);
                    }}
                    try {{
                        const absoluteURL = new w.URL(String(scriptURL), w.location.href).href;
                        const xhr = new w.XMLHttpRequest();
                        xhr.open('GET', absoluteURL, false);
                        xhr.send();
                        if (xhr.status === 200) {{
                            const baseLine = 'self.__adWorkerBase = ' + JSON.stringify(absoluteURL) + ';\\n';
                            const wrappedCode = baseLine + workerHookCode + '\\n' + xhr.responseText;
                            const blob = new w.Blob([wrappedCode], {{ type: 'application/javascript' }});
                            const blobURL = w.URL.createObjectURL(blob);
                            return new origWorker(blobURL, options);
                        }}
                    }} catch(e) {{}}
                    return new origWorker(scriptURL, options);
                }}, 'Worker', 1, true);
                w.Worker.prototype = origWorker.prototype;
                // Keep the constructor link consistent with the wrapper;
                // anti-detect scripts commonly compare Worker.prototype.constructor.
                try {{
                    Object.defineProperty(origWorker.prototype, 'constructor', {{
                        value: w.Worker, writable: true, configurable: true
                    }});
                }} catch(e) {{}}
            }}
        }} catch(e) {{}}

        // 13. WebRTC: no JS hook.
        // S8: SDP from Chrome already uses mDNS and does not contain LAN IPs.
        // A createOffer hook only adds a detectable wrapper, and throwing from
        // the constructor differs from a real browser. The launcher uses the
        // policy flag plus Chrome's native mDNS instead.
    }}

    // Áp dụng stealth cho realm chính (window)
    applyStealth(window);

    // ==========================================
    // C4+C5+S2: iframe Prototype Borrowing Protection
    // WeakSet tracker thay vì window property để tránh lộ qua Object.keys()
    // ==========================================
    const _patchedWindows = new WeakSet();
    _patchedWindows.add(window);

    if (!DISABLED.has('iframe')) try {{
        const iframeProto = HTMLIFrameElement.prototype;
        const origContentWindowDesc = Object.getOwnPropertyDescriptor(iframeProto, 'contentWindow');
        const origContentDocDesc = Object.getOwnPropertyDescriptor(iframeProto, 'contentDocument');

        if (origContentWindowDesc && origContentWindowDesc.get) {{
            const origCWGetter = origContentWindowDesc.get;
            Object.defineProperty(iframeProto, 'contentWindow', {{
                get: markNative(function() {{
                    const win = origCWGetter.call(this);
                    if (win && !_patchedWindows.has(win)) {{
                        try {{
                            _patchedWindows.add(win);
                            applyStealth(win);
                        }} catch(e) {{}}
                    }}
                    return win;
                }}, 'get contentWindow', 0),
                enumerable: true,
                configurable: true
            }});
        }}

        if (origContentDocDesc && origContentDocDesc.get) {{
            const origCDGetter = origContentDocDesc.get;
            Object.defineProperty(iframeProto, 'contentDocument', {{
                get: markNative(function() {{
                    try {{ this.contentWindow; }} catch(e) {{}}
                    return origCDGetter.call(this);
                }}, 'get contentDocument', 0),
                enumerable: true,
                configurable: true
            }});
        }}
    }} catch(e) {{}}

}})();
"""
    return script


# Alias cho tương thích ngược và theo đặc tả kế hoạch P0-05
generate_stealth_js = get_stealth_script
get_webgl_limits = _webgl_limits
