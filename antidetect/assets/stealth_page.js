
(function() {
    if (globalThis.__adStealth) return;
    try { Object.defineProperty(globalThis, '__adStealth', { value: true, configurable: true }); } catch(e) {}
    const CFG = __CFG__;
    const DISABLED = new Set(__DISABLED__);
    const platformStr = (CFG.os === 'macos') ? 'MacIntel' : ((CFG.os === 'linux') ? 'Linux x86_64' : 'Win32');
    const chPlatform = (CFG.os === 'macos') ? 'macOS' : ((CFG.os === 'linux') ? 'Linux' : 'Windows');
    const chPlatformVer = (CFG.os === 'macos') ? '14.5.0' : ((CFG.os === 'linux') ? '6.5.0' : '15.0.0');
    const chArch = (CFG.os === 'macos') ? 'arm' : 'x86';

    // Frozen timestamps cho chrome.csi và chrome.loadTimes (S10)
    const _startE = Date.now();
    const _nowSec = _startE / 1000;

    // ==========================================
    // Helper: maintain native-looking name/length on wrapped getters.
    // R1: wrappers are registered in a WeakMap and Function.prototype.toString
    // is replaced with a concise method (not a Proxy) that returns the native
    // source string for them. A Proxy-based toString hook was detectable
    // (CreepJS hasToStringProxy = true) and was the source of the remaining
    // 20% stealth score.
    // ==========================================
    const nativeSources = new WeakMap();
    const nativeToString = Function.prototype.toString;

    function registerNativeSource(fn, name) {
        try {
            nativeSources.set(fn, 'function ' + name + '() { [native code] }');
        } catch(e) {}
        return fn;
    }

    function makeNativeToString(sources, originalToString) {
        const resolving = new WeakSet();
        const toStringFn = {
            toString() {
                'use strict';
                const source = sources.get(this);
                if (source !== undefined) return source;
                if (typeof this !== 'function') return originalToString.call(this);
                // Wrapped functions from a same-origin sibling realm are
                // registered in that realm's WeakMap. Follow the function's
                // own constructor to reach that realm's resolver.
                if (this !== null && (typeof this === 'object' || typeof this === 'function')) {
                    if (!resolving.has(this)) {
                        resolving.add(this);
                        try {
                            const ctor = this.constructor;
                            const ctorToString = ctor && ctor.prototype && ctor.prototype.toString;
                            if (typeof ctorToString === 'function' &&
                                ctorToString !== toStringFn &&
                                ctorToString !== originalToString) {
                                const delegated = ctorToString.call(this);
                                if (typeof delegated === 'string') return delegated;
                            }
                        } catch(e) {
                        } finally {
                            resolving.delete(this);
                        }
                    }
                }
                return originalToString.call(this);
            }
        }.toString;
        sources.set(toStringFn, 'function toString() { [native code] }');
        return toStringFn;
    }

    const adToString = makeNativeToString(nativeSources, nativeToString);

    function installNativeToString(w) {
        try {
            if (!w.Function || !w.Function.prototype) return;
            if (w.Function.prototype.toString === adToString) return;
            Object.defineProperty(w.Function.prototype, 'toString', {
                value: adToString,
                writable: true,
                enumerable: false,
                configurable: true
            });
        } catch(e) {}
    }

    try {
        installNativeToString(window);
    } catch(e) {}

    function markNative(fn, name, length, isConstructor) {
        let target = fn;
        // Concise method syntax tạo hàm không có .prototype (chuẩn native method / getter)
        // và tự động ném TypeError khi truy cập .arguments/.caller (chuẩn strict mode V8)
        if (!isConstructor) {
            target = {
                [name](...args) {
                    return fn.apply(this, args);
                }
            }[name];
        }
        registerNativeSource(target, name);
        Object.defineProperty(target, 'name', { value: name, configurable: true });
        if (typeof length === 'number') {
            Object.defineProperty(target, 'length', { value: length, configurable: true });
        }
        return target;
    }

    function installScreenWorkArea(w) {
        if (!CFG.screenWorkArea || !w.Screen || !w.Screen.prototype || !w.screen) return;
        const proto = w.Screen.prototype;
        const desired = CFG.screenWorkArea;
        for (const prop of ['availWidth', 'availHeight', 'availLeft', 'availTop']) {
            if (!Object.prototype.hasOwnProperty.call(desired, prop)) continue;
            try {
                if (w.screen[prop] === desired[prop]) continue;
            } catch(e) {
                continue;
            }
            const desc = Object.getOwnPropertyDescriptor(proto, prop);
            if (!desc || typeof desc.get !== 'function') continue;
            Object.defineProperty(proto, prop, {
                get: markNative(function() {
                    if (!(this instanceof w.Screen)) throw new TypeError('Illegal invocation');
                    return desired[prop];
                }, 'get ' + prop, 0),
                enumerable: desc.enumerable,
                configurable: true
            });
        }
    }

    // ==========================================
    // Canvas Noise Generator (2D)
    // Only touch fully opaque pixels: adding +/-1 to a partially transparent
    // pixel changes its premultiplied round-trip and makes toDataURL() differ
    // from getImageData() after PNG decode. Opaque text/shape pixels still vary.
    // ==========================================
    function canvasIsComplex(data) {
        // S5: skip trivial solid/simple canvases. CreepJS's rgba-noise probe
        // uses a simple image; real text/images/gradients still get noise.
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

    // Danh sách font đặc thù từng OS để bảo vệ Font Fingerprinting (S11)
    const macFonts = ['sf pro', 'helvetica neue', 'apple-system', 'blinkmacsystemfont', 'geneva', 'menlo', 'monaco'];
    const winFonts = ['segoe ui', 'calibri', 'cambria', 'consolas', 'ms sans serif', 'ms serif'];

    // ==========================================
    // Hàm áp dụng Stealth vào Realm Window (hỗ trợ cả iframe) (S2)
    // ==========================================
    function applyStealth(w) {
        if (!w) return;
        if (w !== window) {
            try {
                if (w.__adStealth) return;
                installNativeToString(w);
                Object.defineProperty(w, '__adStealth', { value: true, configurable: true });
            } catch(e) {}
        }

        // 1. Chuẩn hóa navigator.webdriver === false theo chuẩn W3C
        if (!DISABLED.has('navigator')) try {
            if (w.Navigator && w.Navigator.prototype) {
                const proto = w.Navigator.prototype;
                if (!('webdriver' in proto) || w.navigator.webdriver !== false) {
                    Object.defineProperty(proto, 'webdriver', {
                        get: markNative(function() {
                            if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                            return false;
                        }, 'get webdriver', 0),
                        enumerable: true,
                        configurable: true
                    });
                }

                // 2. Hardware Concurrency, Device Memory, Languages, Platform (S8)
                Object.defineProperty(proto, 'hardwareConcurrency', {
                    get: markNative(function() {
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return CFG.hardwareConcurrency;
                    }, 'get hardwareConcurrency', 0),
                    enumerable: true, configurable: true
                });

                Object.defineProperty(proto, 'deviceMemory', {
                    get: markNative(function() {
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return CFG.deviceMemory || 8;
                    }, 'get deviceMemory', 0),
                    enumerable: true, configurable: true
                });

                const langList = CFG.languages;
                Object.defineProperty(proto, 'languages', {
                    get: markNative(function() {
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return Object.freeze([...langList]);
                    }, 'get languages', 0),
                    enumerable: true, configurable: true
                });

                Object.defineProperty(proto, 'language', {
                    get: markNative(function() {
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return CFG.locale;
                    }, 'get language', 0),
                    enumerable: true, configurable: true
                });

                Object.defineProperty(proto, 'platform', {
                    get: markNative(function() {
                        if (!(this instanceof w.Navigator)) throw new TypeError('Illegal invocation');
                        return platformStr;
                    }, 'get platform', 0),
                    enumerable: true, configurable: true
                });
            }
        } catch(e) {}

        // 3. Hook NavigatorUAData Client Hints trong JS (S9)
        if (!DISABLED.has('navigator')) try {
            if (w.NavigatorUAData && w.NavigatorUAData.prototype) {
                const uap = w.NavigatorUAData.prototype;
                Object.defineProperty(uap, 'brands', {
                    get: markNative(function() {
                        if (!(this instanceof w.NavigatorUAData)) throw new TypeError('Illegal invocation');
                        return CFG.brands.map(b => Object.freeze({ brand: b.brand, version: b.version }));
                    }, 'get brands', 0),
                    enumerable: true, configurable: true
                });
                Object.defineProperty(uap, 'platform', {
                    get: markNative(function() {
                        if (!(this instanceof w.NavigatorUAData)) throw new TypeError('Illegal invocation');
                        return chPlatform;
                    }, 'get platform', 0),
                    enumerable: true, configurable: true
                });

                const origGetHighEntropyValues = uap.getHighEntropyValues;
                if (origGetHighEntropyValues) {
                    uap.getHighEntropyValues = markNative(function(hints) {
                        const wanted = Array.isArray(hints) ? hints : [];
                        return origGetHighEntropyValues.apply(this, arguments).then(res => {
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
        } catch(e) {}

        // 4. Mock window.chrome runtime & BotGuard (S10: frozen timestamps)
        if (!DISABLED.has('chrome')) try {
            if (!w.chrome) w.chrome = {};
            if (!w.chrome.app) {
                w.chrome.app = {
                    isInstalled: false,
                    getIsInstalled: markNative(function() { return false; }, 'getIsInstalled', 0),
                    getDetails: markNative(function() { return null; }, 'getDetails', 0),
                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
                };
            }
            if (!w.chrome.csi) {
                w.chrome.csi = markNative(function() {
                    return {
                        onloadT: _startE + 145,
                        startE: _startE,
                        pageT: performance.now(),
                        tran: 15
                    };
                }, 'csi', 0);
            }
            if (!w.chrome.loadTimes) {
                w.chrome.loadTimes = markNative(function() {
                    return {
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
                    };
                }, 'loadTimes', 0);
            }
        } catch(e) {}

        // 5. Screen, DPR and CSS media queries are handled natively by
        // Emulation.setDeviceMetricsOverride in the launcher. R2 adds only the
        // work-area getters that differ from CDP's full-screen emulation;
        // matchMedia and devicePixelRatio remain native.
        if (!DISABLED.has('screen')) try {
            installScreenWorkArea(w);
        } catch(e) {}

        // 5b. DOMRect/ClientRects/SVGRect per-profile offsets (nhóm 'canvas' —
        // cùng họ noise nên Authentic tự tắt). Offset NHẤT QUÁN trên mọi
        // element: hash khác nhau giữa các profile, layout tương đối nguyên.
        if (!DISABLED.has('canvas')) try {
            const DR = CFG.domRect;
            const adjust = (x, y, wd, ht) => [
                x + DR.dx, y + DR.dy, wd + DR.dw, ht + DR.dh
            ];
            const adjDomRect = (r) => {
                const v = adjust(r.x, r.y, r.width, r.height);
                return new w.DOMRect(v[0], v[1], v[2], v[3]);
            };
            const hookRectHost = (proto, name) => {
                if (!proto || !proto[name]) return;
                const orig = proto[name];
                if (name === 'getBoundingClientRect') {
                    proto[name] = markNative(function() {
                        return adjDomRect(orig.apply(this, arguments));
                    }, name, 0);
                } else {
                    // getClientRects: trả array DOMRect với tag DOMRectList.
                    proto[name] = markNative(function() {
                        const list = orig.apply(this, arguments);
                        const out = [];
                        for (let i = 0; i < list.length; i++) out.push(adjDomRect(list[i]));
                        out.item = markNative(function(i) {
                            return out[i] || null;
                        }, 'item', 1);
                        try {
                            Object.defineProperty(out, Symbol.toStringTag, {
                                value: 'DOMRectList', configurable: true
                            });
                        } catch(e) {}
                        return out;
                    }, name, 0);
                }
            };
            hookRectHost(w.Element && w.Element.prototype, 'getBoundingClientRect');
            hookRectHost(w.Element && w.Element.prototype, 'getClientRects');
            hookRectHost(w.Range && w.Range.prototype, 'getBoundingClientRect');
            hookRectHost(w.Range && w.Range.prototype, 'getClientRects');
            if (w.SVGGraphicsElement && w.SVGGraphicsElement.prototype
                && w.SVGGraphicsElement.prototype.getBBox) {
                const origBB = w.SVGGraphicsElement.prototype.getBBox;
                w.SVGGraphicsElement.prototype.getBBox = markNative(function() {
                    const b = origBB.apply(this, arguments);
                    // Ưu tiên SVGRect thật của document (createSVGRect chạy
                    // detached, không phải append DOM — tránh leak DOM mutation).
                    let rect = null;
                    try {
                        const svgEl = (this.ownerSVGElement && this.ownerSVGElement.createSVGRect)
                            ? this.ownerSVGElement
                            : w.document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                        rect = svgEl.createSVGRect();
                    } catch(e) {}
                    const target = rect || b;
                    const v = adjust(b.x, b.y, b.width, b.height);
                    try {
                        target.x = v[0]; target.y = v[1];
                        target.width = v[2]; target.height = v[3];
                    } catch(e) { return b; }
                    return target;
                }, 'getBBox', 0);
            }
        } catch(e) {}

        // 6. MediaDevices enumerateDevices — bộ device per-profile (Giai đoạn
        // 1 anti-detection). Chrome thật không trả permission thì deviceId/
        // label/groupId rỗng nhưng COUNT vẫn hiện — nên trả cùng count profile
        // và chỉ ẩn giá trị khi trang chưa có quyền.
        if (!DISABLED.has('media')) try {
            if (w.navigator && w.navigator.mediaDevices && w.navigator.mediaDevices.enumerateDevices) {
                const origEnumerate = w.navigator.mediaDevices.enumerateDevices;
                w.navigator.mediaDevices.enumerateDevices = markNative(function() {
                    return origEnumerate.apply(this, arguments).then(devices => {
                        const permitted = (devices || []).some(d => d.label);
                        return (CFG.mediaDevices || []).map(spec => ({
                            deviceId: permitted ? spec.deviceId : '',
                            kind: spec.kind,
                            label: permitted ? spec.label : '',
                            groupId: permitted ? spec.groupId : '',
                            toJSON: markNative(function() { return { deviceId: this.deviceId, kind: this.kind, label: this.label, groupId: this.groupId }; }, 'toJSON', 0)
                        }));
                    });
                }, 'enumerateDevices', 0);
            }
        } catch(e) {}

        // 7. WebGPU Masking (S5)
        if (!DISABLED.has('webgpu')) try {
            if (w.navigator && w.navigator.gpu && w.navigator.gpu.requestAdapter) {
                const origReqAdapter = w.navigator.gpu.requestAdapter;
                const renderer = CFG.webglRenderer || '';
                const rendererMatch = /Renderer\s*:\s*([^,)]+)/i.exec(renderer);
                let gpuName = rendererMatch ? rendererMatch[1].trim() : renderer;
                if (!rendererMatch) {
                    const parts = renderer.split(',');
                    if (parts.length > 1) gpuName = parts[1];
                }
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
                const fakeAdapterInfo = {
                    vendor: vShort,
                    architecture: arch,
                    device: '',
                    description: gpuName
                };
                w.navigator.gpu.requestAdapter = markNative(async function(options) {
                    const adapter = await origReqAdapter.apply(this, arguments);
                    if (!adapter) return null;
                    return new Proxy(adapter, {
                        get(target, prop) {
                            if (prop === 'info') return fakeAdapterInfo;
                            if (prop === 'requestAdapterInfo') {
                                // Chrome 155 removed this legacy method. Only
                                // expose it when the real adapter still has it;
                                // adding it would make `in` and `typeof` disagree.
                                if (!(prop in target)) return undefined;
                                return markNative(async function() {
                                    return fakeAdapterInfo;
                                }, 'requestAdapterInfo', 0);
                            }
                            const value = Reflect.get(target, prop, target);
                            if (typeof value === 'function') {
                                // WebIDL methods brand-check their receiver; a
                                // Proxy receiver causes "Illegal invocation".
                                return markNative(value.bind(target), prop, value.length);
                            }
                            return value;
                        },
                        has(target, prop) {
                            if (prop === 'requestAdapterInfo') return prop in target;
                            return Reflect.has(target, prop);
                        }
                    });
                }, 'requestAdapter', 0);
            }
        } catch(e) {}

        // 8. SpeechSynthesis Voices (S7: OS matching + cross-OS chuẩn)
        if (!DISABLED.has('speech')) try {
            if (w.speechSynthesis && w.speechSynthesis.getVoices) {
                const origGetVoices = w.speechSynthesis.getVoices;
                const VoiceProto = w.SpeechSynthesisVoice
                    ? w.SpeechSynthesisVoice.prototype : null;
                const mkVoice = (spec) => {
                    const v = VoiceProto ? Object.create(VoiceProto) : {};
                    const mk = (prop, val) => {
                        try {
                            Object.defineProperty(v, prop, {
                                get: markNative(function() { return val; }, 'get ' + prop, 0),
                                enumerable: true, configurable: true
                            });
                        } catch(e) {}
                    };
                    mk('name', spec.name);
                    mk('lang', spec.lang);
                    mk('localService', true);
                    mk('voiceURI', spec.name);
                    mk('default', !!spec.default);
                    return v;
                };
                let crossOsVoices = null;
                if (CFG.crossOs && Array.isArray(CFG.crossOs.voices)) {
                    crossOsVoices = CFG.crossOs.voices.map(mkVoice);
                }
                w.speechSynthesis.getVoices = markNative(function() {
                    // Cross-OS: host không có voices của OS đích — trả danh
                    // sách chuẩn (macOS) hoặc rỗng (Linux thật thường không
                    // có voice nào). Same-OS: giữ voices thật, chỉ lọc.
                    if (crossOsVoices) return crossOsVoices;
                    const voices = origGetVoices.apply(this, arguments);
                    if (!voices || !voices.length) return voices;
                    if (CFG.os === 'macos') {
                        return voices.filter(v => !v.name.includes('Microsoft'));
                    } else if (CFG.os === 'windows') {
                        return voices.filter(v => !v.name.includes('Apple') && !v.name.includes('Samantha') && !v.name.includes('Alex'));
                    }
                    return voices;
                }, 'getVoices', 0);
            }
        } catch(e) {}

        // 8b. Battery API per-profile (nhóm 'battery')
        if (!DISABLED.has('battery')) try {
            if (w.navigator && typeof w.navigator.getBattery === 'function') {
                const B = CFG.battery;
                const BatProto = w.BatteryManager ? w.BatteryManager.prototype : null;
                w.navigator.getBattery = markNative(function() {
                    const mgr = BatProto ? Object.create(BatProto) : {};
                    const mk = (prop, val) => {
                        try {
                            Object.defineProperty(mgr, prop, {
                                get: markNative(function() { return val; }, 'get ' + prop, 0),
                                enumerable: true, configurable: true
                            });
                        } catch(e) {}
                    };
                    mk('charging', B.charging);
                    mk('level', B.level);
                    // spec: Infinity khi đang sạc (chargingTime) / đang pin (dischargingTime)
                    mk('chargingTime', B.charging ? B.chargingTime : Infinity);
                    mk('dischargingTime', B.charging ? Infinity : B.dischargingTime);
                    // Object.create không có EventTarget internal slots: các
                    // handler và listener phải là no-op native-looking thay vì
                    // throw "Illegal invocation" như EventTarget thật.
                    for (const p of ['onchargingchange', 'onlevelchange',
                                     'onchargingtimechange', 'ondischargingtimechange']) {
                        try {
                            Object.defineProperty(mgr, p, {
                                get: markNative(function() { return null; }, 'get ' + p, 0),
                                set: markNative(function() {}, 'set ' + p, 1),
                                configurable: true
                            });
                        } catch(e) {}
                    }
                    mgr.addEventListener = markNative(function() {}, 'addEventListener', 0);
                    mgr.removeEventListener = markNative(function() {}, 'removeEventListener', 0);
                    mgr.dispatchEvent = markNative(function() { return true; }, 'dispatchEvent', 0);
                    return w.Promise.resolve(mgr);
                }, 'getBattery', 0);
            }
        } catch(e) {}

        // 8c. NetworkInformation per-profile (nhóm 'network')
        if (!DISABLED.has('network')) try {
            if (w.navigator) {
                const C = CFG.connection;
                const NIProto = w.NetworkInformation ? w.NetworkInformation.prototype : null;
                const conn = NIProto ? Object.create(NIProto) : {};
                const mk = (obj, prop, val) => {
                    try {
                        Object.defineProperty(obj, prop, {
                            get: markNative(function() { return val; }, 'get ' + prop, 0),
                            enumerable: true, configurable: true
                        });
                    } catch(e) {}
                };
                mk(conn, 'effectiveType', C.effectiveType);
                mk(conn, 'type', C.type);
                mk(conn, 'rtt', C.rtt);
                mk(conn, 'downlink', C.downlink);
                mk(conn, 'saveData', C.saveData);
                try {
                    Object.defineProperty(conn, 'onchange', {
                        get: markNative(function() { return null; }, 'get onchange', 0),
                        set: markNative(function() {}, 'set onchange', 1),
                        configurable: true
                    });
                } catch(e) {}
                conn.addEventListener = markNative(function() {}, 'addEventListener', 0);
                conn.removeEventListener = markNative(function() {}, 'removeEventListener', 0);
                conn.dispatchEvent = markNative(function() { return true; }, 'dispatchEvent', 0);
                const desc = Object.getOwnPropertyDescriptor(w.Navigator.prototype, 'connection');
                if (desc && desc.get) {
                    Object.defineProperty(w.Navigator.prototype, 'connection', {
                        get: markNative(function() { return conn; }, 'get connection', 0),
                        enumerable: true, configurable: true
                    });
                } else {
                    try {
                        Object.defineProperty(w.navigator, 'connection', {
                            value: conn, enumerable: true, configurable: true
                        });
                    } catch(e) {}
                }
            }
        } catch(e) {}

        // 9. Canvas 2D Spoofing & Font Probing Mask (S11)
        if (!DISABLED.has('canvas') || !DISABLED.has('fonts')) try {
            let origCanvasGID = null;
            if (w.CanvasRenderingContext2D && w.CanvasRenderingContext2D.prototype) {
                const ctxProto = w.CanvasRenderingContext2D.prototype;
                if (!DISABLED.has('canvas')) {
                    origCanvasGID = ctxProto.getImageData;
                    ctxProto.getImageData = markNative(function(sx, sy, sw, sh) {
                        const imgData = origCanvasGID.apply(this, arguments);
                        canvasNoise(imgData.data, CFG.canvasSeed || 12345, sx || 0, sy || 0, sw || 1);
                        return imgData;
                    }, 'getImageData', 4);
                }

                // Bảo vệ Font Probing qua measureText (S11 + cross-OS)
                if (!DISABLED.has('fonts')) {
                    const origMeasureText = ctxProto.measureText;
                    ctxProto.measureText = markNative(function(text) {
                        const font = (this.font || '').toLowerCase();
                        const res = origMeasureText.apply(this, arguments);
                        if (CFG.crossOs) {
                            // Cross-OS: font của host thật không được tồn tại
                            // trên OS đích — đo bằng font fallback như khi máy
                            // thật thiếu font đó.
                            for (const hf of CFG.crossOs.hostFonts) {
                                const hfs = String(hf).toLowerCase();
                                if (hfs && font.includes(hfs)) {
                                    const prevFont = this.font;
                                    this.font = font.split(hfs).join('non_existent_fallback_font');
                                    const fallbackRes = origMeasureText.apply(this, arguments);
                                    this.font = prevFont;
                                    return fallbackRes;
                                }
                            }
                            return res;
                        }
                        if (CFG.os === 'macos') {
                            for (const wf of winFonts) {
                                if (font.includes(wf)) {
                                    const prevFont = this.font;
                                    this.font = font.replace(new RegExp(wf, 'g'), 'non_existent_fallback_font');
                                    const fallbackRes = origMeasureText.apply(this, arguments);
                                    this.font = prevFont;
                                    return fallbackRes;
                                }
                            }
                            for (const mf of macFonts) {
                                if (font.includes(mf)) {
                                    const width = res.width + 6.5;
                                    return new Proxy(res, {
                                        get(t, prop) {
                                            if (prop === 'width') return width;
                                            return Reflect.get(t, prop);
                                        }
                                    });
                                }
                            }
                        } else if (CFG.os === 'windows') {
                            for (const mf of macFonts) {
                                if (font.includes(mf)) {
                                    const prevFont = this.font;
                                    this.font = font.replace(new RegExp(mf, 'g'), 'non_existent_fallback_font');
                                    const fallbackRes = origMeasureText.apply(this, arguments);
                                    this.font = prevFont;
                                    return fallbackRes;
                                }
                            }
                        }
                        return res;
                    }, 'measureText', 1);
                }
            }

            if (!DISABLED.has('canvas') && w.HTMLCanvasElement && w.HTMLCanvasElement.prototype) {
                const canvasProto = w.HTMLCanvasElement.prototype;
                const origToDataURL = canvasProto.toDataURL;
                canvasProto.toDataURL = markNative(function() {
                    let ctx = null;
                    let backup = null;
                    try {
                        if (this.width > 0 && this.height > 0) {
                            // A WebGL canvas has no 2D context. Read its pixels,
                            // add deterministic noise, and encode a 2D copy so
                            // WebGL toDataURL hashes differ per profile too.
                            ctx = this.getContext('2d');
                            if (!ctx) {
                                let gl = null;
                                try { gl = this.getContext('webgl2'); } catch(e) {}
                                if (!gl) {
                                    try { gl = this.getContext('webgl') || this.getContext('experimental-webgl'); } catch(e) {}
                                }
                                if (gl) {
                                    const dw = gl.drawingBufferWidth || this.width;
                                    const dh = gl.drawingBufferHeight || this.height;
                                    if (dw > 0 && dh > 0) {
                                        const pixels = new Uint8Array(dw * dh * 4);
                                        gl.readPixels(0, 0, dw, dh, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
                                        const copy = w.document.createElement('canvas');
                                        copy.width = dw;
                                        copy.height = dh;
                                        const cctx = copy.getContext('2d');
                                        if (cctx) {
                                            const img = cctx.createImageData(dw, dh);
                                            for (let y = 0; y < dh; y++) {
                                                const src = (dh - 1 - y) * dw * 4;
                                                const dst = y * dw * 4;
                                                img.data.set(pixels.subarray(src, src + dw * 4), dst);
                                            }
                                            canvasNoise(img.data, CFG.canvasSeed || 12345, 0, 0, dw);
                                            cctx.putImageData(img, 0, 0);
                                            return origToDataURL.apply(copy, arguments);
                                        }
                                    }
                                }
                            }
                            // Read/noise/restore the real canvas instead of drawing
                            // it onto a helper. drawImage() can color-convert and
                            // make toDataURL() differ from getImageData() by one.
                            if (ctx && origCanvasGID) {
                                const imgData = origCanvasGID.apply(ctx, [0, 0, this.width, this.height]);
                                backup = new Uint8ClampedArray(imgData.data);
                                canvasNoise(imgData.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                                ctx.putImageData(imgData, 0, 0);
                                return origToDataURL.apply(this, arguments);
                            }
                        }
                    } catch(e) {} finally {
                        if (ctx && backup) {
                            try {
                                const restored = ctx.createImageData(this.width, this.height);
                                restored.data.set(backup);
                                ctx.putImageData(restored, 0, 0);
                            } catch(e) {}
                        }
                    }
                    return origToDataURL.apply(this, arguments);
                }, 'toDataURL', 0);

                const origToBlob = canvasProto.toBlob;
                canvasProto.toBlob = markNative(function(callback) {
                    let restore = null;
                    try {
                        if (this.width > 0 && this.height > 0) {
                            const ctx = this.getContext('2d');
                            if (ctx && origCanvasGID) {
                                const imgData = origCanvasGID.apply(ctx, [0, 0, this.width, this.height]);
                                let backup = new Uint8ClampedArray(imgData.data);
                                canvasNoise(imgData.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                                ctx.putImageData(imgData, 0, 0);
                                const canvas = this;
                                restore = function() {
                                    if (!backup) return;
                                    try {
                                        const restored = ctx.createImageData(canvas.width, canvas.height);
                                        restored.data.set(backup);
                                        ctx.putImageData(restored, 0, 0);
                                    } catch(e) {}
                                    backup = null;
                                };
                                const args = Array.prototype.slice.call(arguments);
                                const userCb = args[0];
                                args[0] = function(blob) {
                                    restore();
                                    if (typeof userCb === 'function') userCb(blob);
                                };
                                return origToBlob.apply(this, args);
                            }
                        }
                    } catch(e) {
                        if (restore) restore();
                    }
                    return origToBlob.apply(this, arguments);
                }, 'toBlob', 1);
            }

            // OffscreenCanvas hooks
            if (!DISABLED.has('canvas') && typeof w.OffscreenCanvas !== 'undefined') {
                let origOffGID = null;
                if (typeof w.OffscreenCanvasRenderingContext2D !== 'undefined') {
                    const offCtxProto = w.OffscreenCanvasRenderingContext2D.prototype;
                    origOffGID = offCtxProto.getImageData;
                    offCtxProto.getImageData = markNative(function(sx, sy, sw, sh) {
                        const imgData = origOffGID.apply(this, arguments);
                        canvasNoise(imgData.data, CFG.canvasSeed || 12345, sx || 0, sy || 0, sw || 1);
                        return imgData;
                    }, 'getImageData', 4);

                    const origOffMT = offCtxProto.measureText;
                    if (origOffMT) {
                        offCtxProto.measureText = markNative(function(text) {
                            return origOffMT.apply(this, arguments);
                        }, 'measureText', 1);
                    }
                }
                const origConvertToBlob = w.OffscreenCanvas.prototype.convertToBlob;
                if (origConvertToBlob) {
                    w.OffscreenCanvas.prototype.convertToBlob = markNative(function(options) {
                        try {
                            if (this.width > 0 && this.height > 0) {
                                const ctx = this.getContext('2d');
                                if (ctx) {
                                    const imgData = origOffGID
                                        ? origOffGID.apply(ctx, [0, 0, this.width, this.height])
                                        : ctx.getImageData(0, 0, this.width, this.height);
                                    const backup = new Uint8ClampedArray(imgData.data);
                                    canvasNoise(imgData.data, CFG.canvasSeed || 12345, 0, 0, this.width);
                                    ctx.putImageData(imgData, 0, 0);
                                    const canvas = this;
                                    const restore = function() {
                                        try {
                                            const restored = ctx.createImageData(canvas.width, canvas.height);
                                            restored.data.set(backup);
                                            ctx.putImageData(restored, 0, 0);
                                        } catch(e) {}
                                    };
                                    const result = origConvertToBlob.apply(this, arguments);
                                    if (result && typeof result.finally === 'function') {
                                        return result.finally(restore);
                                    }
                                    restore();
                                    return result;
                                }
                            }
                        } catch(e) {}
                        return origConvertToBlob.apply(this, arguments);
                    }, 'convertToBlob', 0);
                }
            }

            // document.fonts.check Font Masking (S11 + cross-OS đầy đủ)
            if (!DISABLED.has('fonts') && w.document && w.document.fonts && w.document.fonts.check) {
                const origFontsCheck = w.document.fonts.check;
                w.document.fonts.check = markNative(function(font, text) {
                    const f = (font || '').toLowerCase();
                    if (CFG.crossOs) {
                        // Cross-OS: chỉ font trong danh sách chuẩn của OS đích
                        // là "có mặt"; mọi font khác (kể cả font host thật)
                        // phải trả false, nếu không detector thấy font của
                        // Windows lẫn macOS trên cùng máy.
                        for (const tf of CFG.crossOs.targetFonts) {
                            if (f.includes(String(tf).toLowerCase())) return true;
                        }
                        return false;
                    }
                    // Same-OS: font host đã đúng — native check chính xác nhất.
                    return origFontsCheck.apply(this, arguments);
                }, 'check', 1);
            }
        } catch(e) {}

        // 10. WebGL Vendor & Renderer & Extensions & readPixels (S12) & ShaderPrecision (S6)
        // S1: only UNMASKED_VENDOR/RENDERER are spoofed for same-OS profiles.
        // Native limits are already correct for the host backend. Cross-OS
        // profiles use CFG.webglLimits, measured from real GPUs.
        function hookWebGL(glProto) {
            if (!glProto) return;
            const UNMASKED_VENDOR_WEBGL = 0x9245;
            const UNMASKED_RENDERER_WEBGL = 0x9246;
            const MAX_VIEWPORT_DIMS = 0x0D3A;
            const MAX_TEXTURE_SIZE = 0x0D33;
            const MAX_RENDERBUFFER_SIZE = 0x84E8;
            const MAX_CUBE_MAP_TEXTURE_SIZE = 0x851C;
            const limits = CFG.webglLimits;

            const origGetParameter = glProto.getParameter;
            glProto.getParameter = markNative(function(param) {
                if (param === UNMASKED_VENDOR_WEBGL) return CFG.webglVendor;
                if (param === UNMASKED_RENDERER_WEBGL) return CFG.webglRenderer;
                if (limits) {
                    if (param === MAX_VIEWPORT_DIMS) return new Int32Array(limits.maxViewportDims);
                    if (param === MAX_TEXTURE_SIZE) return limits.maxTextureSize;
                    if (param === MAX_RENDERBUFFER_SIZE) return limits.maxRenderbufferSize;
                    if (param === MAX_CUBE_MAP_TEXTURE_SIZE) return limits.maxCubeMapTextureSize;
                }
                return origGetParameter.apply(this, arguments);
            }, 'getParameter', 1);

            const _filteredExts = new Set();
            const origGetSupportedExtensions = glProto.getSupportedExtensions;
            glProto.getSupportedExtensions = markNative(function() {
                let exts = origGetSupportedExtensions.apply(this, arguments);
                if (!exts) return exts;
                const renderer = CFG.webglRenderer || '';
                if (renderer.includes('Apple') || renderer.includes('Metal')) {
                    const filtered = exts.filter(e => !e.includes('ANGLE') && !e.includes('WGL') && !e.includes('WEBGL_multi_draw'));
                    exts.forEach(e => { if (!filtered.includes(e)) _filteredExts.add(e); });
                    return filtered;
                }
                return exts;
            }, 'getSupportedExtensions', 0);

            const origGetExtension = glProto.getExtension;
            glProto.getExtension = markNative(function(name) {
                if (_filteredExts.has(name)) return null;
                return origGetExtension.apply(this, arguments);
            }, 'getExtension', 1);

            // C10 & S12: readPixels — đa kênh RGB và Float32Array safe
            const origReadPixels = glProto.readPixels;
            glProto.readPixels = markNative(function(x, y, w, h, format, type, pixels) {
                origReadPixels.apply(this, arguments);
                if (pixels && pixels.length) {
                    const seed = CFG.canvasSeed || 12345;
                    const isFloat = (pixels instanceof Float32Array || pixels instanceof Float64Array);
                    const step = 24;
                    for (let i = 0; i < pixels.length; i += step) {
                        // Mix the seed per pixel. The old parity-only formula
                        // produced identical deltas for two even seeds, so
                        // different profiles could have the same readPixels hash.
                        let h = (seed ^ Math.imul(i + 1, 0x9E3779B1)) >>> 0;
                        h = Math.imul(h ^ (h >>> 16), 0x85EBCA6B) >>> 0;
                        const delta = ((h >>> 8) & 1) ? 1 : -1;
                        if (isFloat) {
                            const fDelta = delta * 0.001;
                            pixels[i] = Math.max(0.0, Math.min(1.0, pixels[i] + fDelta));
                            if (i + 1 < pixels.length) pixels[i + 1] = Math.max(0.0, Math.min(1.0, pixels[i + 1] - fDelta));
                            if (i + 2 < pixels.length) pixels[i + 2] = Math.max(0.0, Math.min(1.0, pixels[i + 2] + fDelta));
                        } else {
                            pixels[i] = Math.min(255, Math.max(0, pixels[i] + delta));
                            if (i + 1 < pixels.length) pixels[i + 1] = Math.min(255, Math.max(0, pixels[i + 1] - delta));
                            if (i + 2 < pixels.length) pixels[i + 2] = Math.min(255, Math.max(0, pixels[i + 2] + delta));
                        }
                    }
                }
            }, 'readPixels', 7);

            // S6: WebGL Shader Precision Format. Cross-OS: shadow own-property
            // trên object NATIVE (instanceof giữ nguyên) với bảng chuẩn ANGLE
            // desktop; same-OS giữ pass-through.
            const origGetShaderPrecisionFormat = glProto.getShaderPrecisionFormat;
            if (origGetShaderPrecisionFormat) {
                if (CFG.shaderPrecision) {
                    glProto.getShaderPrecisionFormat = markNative(function(shaderType, precisionType) {
                        const fmt = origGetShaderPrecisionFormat.apply(this, arguments);
                        try {
                            const p = glProto;
                            const isFloat = (precisionType === p.LOW_FLOAT
                                || precisionType === p.MEDIUM_FLOAT
                                || precisionType === p.HIGH_FLOAT);
                            const t = isFloat
                                ? CFG.shaderPrecision.float
                                : CFG.shaderPrecision.int;
                            Object.defineProperty(fmt, 'rangeMin', { value: t[0], configurable: true });
                            Object.defineProperty(fmt, 'rangeMax', { value: t[1], configurable: true });
                            Object.defineProperty(fmt, 'precision', { value: t[2], configurable: true });
                        } catch(e) {}
                        return fmt;
                    }, 'getShaderPrecisionFormat', 2);
                } else {
                    glProto.getShaderPrecisionFormat = markNative(function(shaderType, precisionType) {
                        return origGetShaderPrecisionFormat.apply(this, arguments);
                    }, 'getShaderPrecisionFormat', 2);
                }
            }
        }

        if (!DISABLED.has('webgl')) try {
            if (w.WebGLRenderingContext) hookWebGL(w.WebGLRenderingContext.prototype);
            if (w.WebGL2RenderingContext) hookWebGL(w.WebGL2RenderingContext.prototype);
        } catch(e) {}

        // 11. AudioContext Spoofing — GĐ3: noise per-index hash thay cho chuỗi
        // tuần hoàn. Pattern chu kỳ (mỗi 100/13 mẫu) tạo artifact phổ định kỳ
        // mà phân tích thống kê phát hiện được; hash theo (seed, channel,
        // index) cho phổ phẳng như nhiễu analog thật.
        if (!DISABLED.has('audio')) try {
            const audioNoiseAt = (seed, channel, index) => {
                let h = (seed ^ (channel * 0x9E3779B1) ^ (index * 0x85EBCA6B)) >>> 0;
                h = Math.imul(h ^ (h >>> 13), 0xC2B2AE35) >>> 0;
                h ^= h >>> 16;
                return ((h & 0xFFFF) / 0xFFFF - 0.5);  // [-0.5, 0.5)
            };
            const processedBuffers = new WeakMap();
            if (w.AudioBuffer) {
                const origGetChannelData = w.AudioBuffer.prototype.getChannelData;
                w.AudioBuffer.prototype.getChannelData = markNative(function(channel) {
                    const data = origGetChannelData.apply(this, arguments);
                    if (!processedBuffers.has(this)) processedBuffers.set(this, new Set());
                    const channels = processedBuffers.get(this);
                    if (!channels.has(channel)) {
                        channels.add(channel);
                        const seed = (Number(CFG.audioSeed) >>> 0) || 12345;
                        // Noise MỖI sample (không phải mỗi 100th) với biên độ
                        // ±5e-5 — đủ đổi hash, không phá audio thật.
                        for (let i = 0; i < data.length; i++) {
                            data[i] += audioNoiseAt(seed, channel, i) * 1e-4;
                        }
                    }
                    return data;
                }, 'getChannelData', 1);

                if (w.AudioBuffer.prototype.copyFromChannel) {
                    const origCopyFromChannel = w.AudioBuffer.prototype.copyFromChannel;
                    w.AudioBuffer.prototype.copyFromChannel = markNative(function(destination, channelNumber, bufferOffset) {
                        this.getChannelData(channelNumber);
                        return origCopyFromChannel.apply(this, arguments);
                    }, 'copyFromChannel', 2);
                }
            }

            if (w.AnalyserNode) {
                function hookAnalyser(methodName, nativeLen) {
                    const orig = w.AnalyserNode.prototype[methodName];
                    if (!orig) return;
                    w.AnalyserNode.prototype[methodName] = markNative(function(array) {
                        orig.apply(this, arguments);
                        const seed = (Number(CFG.audioSeed) >>> 0) || 12345;
                        const isByte = (methodName === 'getByteFrequencyData'
                            || methodName === 'getByteTimeDomainData');
                        // Byte data lượng tử hóa 1/255 — noise nhỏ hơn bị
                        // truncate; chỉ các mẫu rơi gần ngưỡng đổi giá trị.
                        const amp = isByte ? 0.6 : 0.01;
                        for (let i = 0; i < array.length; i++) {
                            array[i] += audioNoiseAt(seed, 0x414E41 ^ i, i) * amp;
                        }
                    }, methodName, nativeLen);
                }
                hookAnalyser('getFloatFrequencyData', 1);
                hookAnalyser('getByteFrequencyData', 1);
                hookAnalyser('getFloatTimeDomainData', 1);
                hookAnalyser('getByteTimeDomainData', 1);
            }
        } catch(e) {}

        // 12. Web Worker Hook (S3 & S14)
        if (!DISABLED.has('worker')) try {
            if (w.Worker) {
                const origWorker = w.Worker;
                const workerHookCode = __WORKER_JS__;
                w.Worker = markNative(function(scriptURL, options) {
                    if (options && options.type === 'module') {
                        // Module workers are covered by the CDP worker target hook.
                        return new origWorker(scriptURL, options);
                    }
                    try {
                        const absoluteURL = new w.URL(String(scriptURL), w.location.href).href;
                        const xhr = new w.XMLHttpRequest();
                        xhr.open('GET', absoluteURL, false);
                        xhr.send();
                        if (xhr.status === 200) {
                            const baseLine = 'self.__adWorkerBase = ' + JSON.stringify(absoluteURL) + ';\n';
                            const wrappedCode = baseLine + workerHookCode + '\n' + xhr.responseText;
                            const blob = new w.Blob([wrappedCode], { type: 'application/javascript' });
                            const blobURL = w.URL.createObjectURL(blob);
                            return new origWorker(blobURL, options);
                        }
                    } catch(e) {}
                    return new origWorker(scriptURL, options);
                }, 'Worker', 1, true);
                w.Worker.prototype = origWorker.prototype;
                // Keep the constructor link consistent with the wrapper;
                // anti-detect scripts commonly compare Worker.prototype.constructor.
                try {
                    Object.defineProperty(origWorker.prototype, 'constructor', {
                        value: w.Worker, writable: true, configurable: true
                    });
                } catch(e) {}
            }
        } catch(e) {}

        // 13. WebRTC: no JS hook.
        // S8: SDP from Chrome already uses mDNS and does not contain LAN IPs.
        // A createOffer hook only adds a detectable wrapper, and throwing from
        // the constructor differs from a real browser. The launcher uses the
        // policy flag plus Chrome's native mDNS instead.
    }

    // Áp dụng stealth cho realm chính (window)
    applyStealth(window);

    // ==========================================
    // C4+C5+S2: iframe Prototype Borrowing Protection
    // WeakSet tracker thay vì window property để tránh lộ qua Object.keys()
    // ==========================================
    const _patchedWindows = new WeakSet();
    _patchedWindows.add(window);

    if (!DISABLED.has('iframe')) try {
        const iframeProto = HTMLIFrameElement.prototype;
        const origContentWindowDesc = Object.getOwnPropertyDescriptor(iframeProto, 'contentWindow');
        const origContentDocDesc = Object.getOwnPropertyDescriptor(iframeProto, 'contentDocument');

        if (origContentWindowDesc && origContentWindowDesc.get) {
            const origCWGetter = origContentWindowDesc.get;
            Object.defineProperty(iframeProto, 'contentWindow', {
                get: markNative(function() {
                    const win = origCWGetter.call(this);
                    if (win && !_patchedWindows.has(win)) {
                        try {
                            _patchedWindows.add(win);
                            applyStealth(win);
                        } catch(e) {}
                    }
                    return win;
                }, 'get contentWindow', 0),
                enumerable: true,
                configurable: true
            });
        }

        if (origContentDocDesc && origContentDocDesc.get) {
            const origCDGetter = origContentDocDesc.get;
            Object.defineProperty(iframeProto, 'contentDocument', {
                get: markNative(function() {
                    try { this.contentWindow; } catch(e) {}
                    return origCDGetter.call(this);
                }, 'get contentDocument', 0),
                enumerable: true,
                configurable: true
            });
        }
    } catch(e) {}

})();
