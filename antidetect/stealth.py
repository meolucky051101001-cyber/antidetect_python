import json
from .models import FingerprintConfig


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
        "deviceMemory": min(8, fp.device_memory) if fp.device_memory else 8,
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
    script only touches WorkerNavigator, Function.prototype.toString,
    OffscreenCanvas, WebGL and importScripts.
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
    const platformStr = __PLATFORM__;
    const chPlatform = __CH_PLATFORM__;
    const chPlatformVer = __CH_PLATFORM_VER__;
    const chArch = __CH_ARCH__;
    const NavCtor = g.WorkerNavigator || g.Navigator;

    const nativeToString = Function.prototype.toString;
    const hookedFunctions = new WeakSet();
    function markNative(fn, name, length, isConstructor) {
        let target = fn;
        if (!isConstructor) {
            target = { [name](...args) { return fn.apply(this, args); } }[name];
        }
        hookedFunctions.add(target);
        try { Object.defineProperty(target, 'name', { value: name, configurable: true }); } catch(e) {}
        if (typeof length === 'number') {
            try { Object.defineProperty(target, 'length', { value: length, configurable: true }); } catch(e) {}
        }
        return target;
    }
    const proxyToString = new Proxy(nativeToString, {
        apply(target, thisArg, argArray) {
            if (hookedFunctions.has(thisArg)) {
                return `function ${thisArg.name || ''}() { [native code] }`;
            }
            return Reflect.apply(target, thisArg, argArray);
        }
    });
    Function.prototype.toString = proxyToString;
    hookedFunctions.add(proxyToString);

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
    defineNav('deviceMemory', Math.min(8, CFG.deviceMemory || 8), 'get deviceMemory');
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

    function canvasNoise(data, seed, startX, startY, width) {
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

    if (typeof OffscreenCanvas !== 'undefined') {
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
        glProto.getParameter = markNative(function(param) {
            if (param === 0x9245) return CFG.webglVendor;
            if (param === 0x9246) return CFG.webglRenderer;
            if (param === 0x0D3A) return new Int32Array([32768, 32768]);
            return orig.apply(this, arguments);
        }, 'getParameter', 1);
    }
    try {
        if (g.WebGLRenderingContext) hookWebGL(g.WebGLRenderingContext.prototype);
        if (g.WebGL2RenderingContext) hookWebGL(g.WebGL2RenderingContext.prototype);
    } catch(e) {}

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
    const platformStr = (CFG.os === 'macos') ? 'MacIntel' : ((CFG.os === 'linux') ? 'Linux x86_64' : 'Win32');
    const chPlatform = (CFG.os === 'macos') ? 'macOS' : ((CFG.os === 'linux') ? 'Linux' : 'Windows');
    const chPlatformVer = (CFG.os === 'macos') ? '14.5.0' : ((CFG.os === 'linux') ? '6.5.0' : '15.0.0');
    const chArch = (CFG.os === 'macos') ? 'arm' : 'x86';

    // Frozen timestamps cho chrome.csi và chrome.loadTimes (S10)
    const _startE = Date.now();
    const _nowSec = _startE / 1000;

    // ==========================================
    // Helper: Giả lập hàm native để che giấu hook (S1)
    // ==========================================
    const nativeToString = Function.prototype.toString;
    const hookedFunctions = new WeakSet();

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
        hookedFunctions.add(target);
        Object.defineProperty(target, 'name', {{ value: name, configurable: true }});
        if (typeof length === 'number') {{
            Object.defineProperty(target, 'length', {{ value: length, configurable: true }});
        }}
        return target;
    }}

    // Proxy-based Function.prototype.toString
    const proxyToString = new Proxy(nativeToString, {{
        apply(target, thisArg, argArray) {{
            if (hookedFunctions.has(thisArg)) {{
                return `function ${{thisArg.name || ''}}() {{ [native code] }}`;
            }}
            return Reflect.apply(target, thisArg, argArray);
        }}
    }});
    Function.prototype.toString = proxyToString;
    hookedFunctions.add(proxyToString);

    // ==========================================
    // Canvas Noise Generator (2D)
    // Only touch fully opaque pixels: adding +/-1 to a partially transparent
    // pixel changes its premultiplied round-trip and makes toDataURL() differ
    // from getImageData() after PNG decode. Opaque text/shape pixels still vary.
    // ==========================================
    function canvasNoise(data, seed, startX, startY, width) {{
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

        // 0. Patch Function.prototype.toString nếu là window realm con (iframe)
        if (w !== window && w.Function && w.Function.prototype) {{
            try {{
                const iNativeToStr = w.Function.prototype.toString;
                w.Function.prototype.toString = new w.Proxy(iNativeToStr, {{
                    apply(target, thisArg, argArray) {{
                        if (hookedFunctions.has(thisArg)) {{
                            return `function ${{thisArg.name || ''}}() {{ [native code] }}`;
                        }}
                        return Reflect.apply(target, thisArg, argArray);
                    }}
                }});
                hookedFunctions.add(w.Function.prototype.toString);
            }} catch(e) {{}}
        }}

        // 1. Chuẩn hóa navigator.webdriver === false theo chuẩn W3C
        try {{
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
                        return Math.min(8, CFG.deviceMemory || 8);
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
        try {{
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
        try {{
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

        // 5. Screen & Window Dimensions
        try {{
            const taskbarH = (CFG.os === 'macos') ? 25 : ((CFG.os === 'linux') ? 32 : 40);
            const screenTargets = [];
            if (w.Screen && w.Screen.prototype) screenTargets.push(w.Screen.prototype);
            for (const target of screenTargets) {{
                const def = (prop, value, name) => {{
                    try {{
                        Object.defineProperty(target, prop, {{
                            get: markNative(function() {{
                                if (w.Screen && !(this instanceof w.Screen)) throw new TypeError('Illegal invocation');
                                return value;
                            }}, name, 0),
                            enumerable: true, configurable: true
                        }});
                    }} catch(e) {{}}
                }};
                def('width', CFG.screenWidth, 'get width');
                def('height', CFG.screenHeight, 'get height');
                def('availWidth', CFG.screenWidth, 'get availWidth');
                def('availHeight', CFG.screenHeight - taskbarH, 'get availHeight');
                def('availLeft', 0, 'get availLeft');
                def('availTop', 0, 'get availTop');
                def('colorDepth', CFG.screenColorDepth || 24, 'get colorDepth');
                def('pixelDepth', CFG.screenColorDepth || 24, 'get pixelDepth');
            }}

            // Chrome 155 defines devicePixelRatio as an own accessor on window;
            // defining only Window.prototype is silently shadowed.
            const dprGetter = function() {{ return CFG.devicePixelRatio; }};
            if (w.Window && w.Window.prototype) {{
                try {{
                    Object.defineProperty(w.Window.prototype, 'devicePixelRatio', {{
                        get: markNative(dprGetter, 'get devicePixelRatio', 0),
                        enumerable: true, configurable: true
                    }});
                }} catch(e) {{}}
            }}
            try {{
                Object.defineProperty(w, 'devicePixelRatio', {{
                    get: markNative(dprGetter, 'get devicePixelRatio', 0),
                    enumerable: true, configurable: true
                }});
            }} catch(e) {{}}

            // matchMedia does not read the JS devicePixelRatio value; rewrite
            // resolution queries so CSS and JS agree on the same DPR.
            if (w.matchMedia) {{
                const origMatchMedia = w.matchMedia;
                w.matchMedia = markNative(function(query) {{
                    const original = String(query);
                    const dpr = Number(CFG.devicePixelRatio) || 1;
                    let rewritten = original;
                    try {{
                        rewritten = rewritten.replace(
                            /\(\s*resolution\s*:\s*([0-9.]+)dppx\s*\)/gi,
                            `(resolution: ${{dpr}}dppx)`
                        );
                        rewritten = rewritten.replace(
                            /\(\s*-webkit-device-pixel-ratio\s*:\s*([0-9.]+)\s*\)/gi,
                            `(-webkit-device-pixel-ratio: ${{dpr}})`
                        );
                        rewritten = rewritten.replace(
                            /\(\s*min-resolution\s*:\s*([0-9.]+)dppx\s*\)/gi,
                            (m, x) => `(min-resolution: ${{dpr >= Number(x) ? 0 : 999}}dppx)`
                        );
                        rewritten = rewritten.replace(
                            /\(\s*max-resolution\s*:\s*([0-9.]+)dppx\s*\)/gi,
                            (m, x) => `(max-resolution: ${{dpr <= Number(x) ? 999 : 0}}dppx)`
                        );
                    }} catch(e) {{}}
                    const mql = origMatchMedia.call(this, rewritten);
                    try {{
                        Object.defineProperty(mql, 'media', {{ get: () => original, configurable: true }});
                    }} catch(e) {{}}
                    // The native CSS engine can still report its real DPR (for
                    // example when Playwright launches without --force-device-
                    // scale-factor). Override the simple exact-resolution result
                    // from CFG as well; min/max are rewritten above.
                    try {{
                        const exact = /^\(\s*(?:resolution\s*:\s*([0-9.]+)dppx|-webkit-device-pixel-ratio\s*:\s*([0-9.]+))\s*\)$/i.exec(original);
                        if (exact) {{
                            const want = Number(exact[1] || exact[2]);
                            Object.defineProperty(mql, 'matches', {{
                                get: () => dpr === want,
                                configurable: true
                            }});
                        }}
                    }} catch(e) {{}}
                    return mql;
                }}, 'matchMedia', 1);
            }}
        }} catch(e) {{}}

        // 6. MediaDevices enumerateDevices
        try {{
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
        try {{
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
        try {{
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
        try {{
            let origCanvasGID = null;
            if (w.CanvasRenderingContext2D && w.CanvasRenderingContext2D.prototype) {{
                const ctxProto = w.CanvasRenderingContext2D.prototype;
                origCanvasGID = ctxProto.getImageData;
                ctxProto.getImageData = markNative(function(sx, sy, sw, sh) {{
                    const imgData = origCanvasGID.apply(this, arguments);
                    canvasNoise(imgData.data, CFG.canvasSeed || 12345, sx || 0, sy || 0, sw || 1);
                    return imgData;
                }}, 'getImageData', 4);

                // Bảo vệ Font Probing qua measureText (S11)
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

            if (w.HTMLCanvasElement && w.HTMLCanvasElement.prototype) {{
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
            if (typeof w.OffscreenCanvas !== 'undefined') {{
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
            if (w.document && w.document.fonts && w.document.fonts.check) {{
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
        function hookWebGL(glProto) {{
            if (!glProto) return;
            const UNMASKED_VENDOR_WEBGL = 0x9245;
            const UNMASKED_RENDERER_WEBGL = 0x9246;
            const MAX_VIEWPORT_DIMS = 0x0D3A;
            const MAX_TEXTURE_SIZE = 0x0D33;
            const MAX_RENDERBUFFER_SIZE = 0x84E8;
            const MAX_CUBE_MAP_TEXTURE_SIZE = 0x851C;
            const MAX_VERTEX_ATTRIBS = 0x8869;
            const MAX_TEXTURE_IMAGE_UNITS = 0x8872;
            const MAX_VERTEX_TEXTURE_IMAGE_UNITS = 0x8B4C;
            const MAX_COMBINED_TEXTURE_IMAGE_UNITS = 0x8B4D;
            const MAX_VARYING_VECTORS = 0x8DFC;
            const MAX_FRAGMENT_UNIFORM_VECTORS = 0x8DFD;
            const MAX_VERTEX_UNIFORM_VECTORS = 0x8DFB;
            const ALIASED_LINE_WIDTH_RANGE = 0x846E;
            const ALIASED_POINT_SIZE_RANGE = 0x846D;

            const rendererLower = (CFG.webglRenderer || '').toLowerCase();
            const isApple = rendererLower.includes('apple') || rendererLower.includes('metal');
            const isIntel = rendererLower.includes('intel');
            const maxTexture = isApple ? 16384 : (isIntel ? 16384 : 32768);
            const maxViewport = isApple ? 16384 : (isIntel ? 16384 : 32768);

            const origGetParameter = glProto.getParameter;
            glProto.getParameter = markNative(function(param) {{
                if (param === UNMASKED_VENDOR_WEBGL) return CFG.webglVendor;
                if (param === UNMASKED_RENDERER_WEBGL) return CFG.webglRenderer;
                if (param === MAX_VIEWPORT_DIMS) return new Int32Array([maxViewport, maxViewport]);
                if (param === MAX_TEXTURE_SIZE) return maxTexture;
                if (param === MAX_RENDERBUFFER_SIZE) return maxTexture;
                if (param === MAX_CUBE_MAP_TEXTURE_SIZE) return maxTexture;
                if (param === MAX_VERTEX_ATTRIBS) return 16;
                if (param === MAX_TEXTURE_IMAGE_UNITS) return 16;
                if (param === MAX_VERTEX_TEXTURE_IMAGE_UNITS) return 16;
                if (param === MAX_COMBINED_TEXTURE_IMAGE_UNITS) return 32;
                if (param === MAX_VARYING_VECTORS) return 30;
                if (param === MAX_FRAGMENT_UNIFORM_VECTORS) return 1024;
                if (param === MAX_VERTEX_UNIFORM_VECTORS) return 4096;
                if (param === ALIASED_LINE_WIDTH_RANGE) return new Float32Array([1, 1]);
                if (param === ALIASED_POINT_SIZE_RANGE) return new Float32Array([1, 1024]);
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

        try {{
            if (w.WebGLRenderingContext) hookWebGL(w.WebGLRenderingContext.prototype);
            if (w.WebGL2RenderingContext) hookWebGL(w.WebGL2RenderingContext.prototype);
        }} catch(e) {{}}

        // 11. AudioContext Spoofing
        try {{
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
        try {{
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

        // 13. WebRTC Leak Protection (P2-03)
        try {{
            if (w.RTCPeerConnection) {{
                const origRTC = w.RTCPeerConnection;
                const mode = CFG.webrtcMode || 'disable_non_proxied';

                if (mode === 'block') {{
                    w.RTCPeerConnection = markNative(function() {{
                        throw new DOMException("Failed to construct 'RTCPeerConnection': WebRTC is disabled by policy.", "NotSupportedError");
                    }}, 'RTCPeerConnection', 0, true);
                    w.RTCPeerConnection.prototype = origRTC.prototype;
                    try {{
                        Object.defineProperty(origRTC.prototype, 'constructor', {{
                            value: w.RTCPeerConnection, writable: true, configurable: true
                        }});
                    }} catch(e) {{}}
                }} else if (mode === 'disable_non_proxied') {{
                    const origCreateOffer = origRTC.prototype.createOffer;
                    if (origCreateOffer) {{
                        origRTC.prototype.createOffer = markNative(function() {{
                            return origCreateOffer.apply(this, arguments).then(offer => {{
                                if (offer && offer.sdp) {{
                                    const cleanSdp = offer.sdp.replace(/(?:c=IN\\s+IP4\\s+)(?:10\\.\\d+\\.\\d+\\.\\d+|192\\.168\\.\\d+\\.\\d+|172\\.(?:1[6-9]|2\\d|3[0-1])\\.\\d+\\.\\d+)/g, 'c=IN IP4 0.0.0.0');
                                    return new RTCSessionDescription({{ type: offer.type, sdp: cleanSdp }});
                                }}
                                return offer;
                            }});
                        }}, 'createOffer', 0);
                    }}
                }}
            }}
        }} catch(e) {{}}
    }}

    // Áp dụng stealth cho realm chính (window)
    applyStealth(window);

    // ==========================================
    // C4+C5+S2: iframe Prototype Borrowing Protection
    // WeakSet tracker thay vì window property để tránh lộ qua Object.keys()
    // ==========================================
    const _patchedWindows = new WeakSet();
    _patchedWindows.add(window);

    try {{
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
