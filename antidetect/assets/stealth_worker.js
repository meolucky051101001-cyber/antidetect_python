
(function() {
    const g = self;
    // The CDP controller owns one installation per worker execution context.
    const CFG = __CFG__;
    const CONTRACT_V2 = Number(CFG.algorithmVersion || 1) >= 2;
    const DISABLED = new Set(__DISABLED__);
    const platformStr = __PLATFORM__;
    const chPlatform = __CH_PLATFORM__;
    const chPlatformVer = __CH_PLATFORM_VER__;
    const chArch = __CH_ARCH__;
    const NavCtor = g.WorkerNavigator || g.Navigator;

    // R1: keep the native source string for every wrapper. Function.prototype
    // .toString is overridden with a concise method (not a Proxy) and consults
    // this WeakMap before falling back to the real native implementation.
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
    try {
        Object.defineProperty(Function.prototype, 'toString', {
            value: adToString,
            writable: true,
            enumerable: false,
            configurable: true
        });
    } catch(e) {}

    function markNative(fn, name, length, isConstructor) {
        let target = fn;
        if (!isConstructor) {
            target = { [name](...args) { return fn.apply(this, args); } }[name];
        }
        registerNativeSource(target, name);
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
        if (DISABLED.has('navigator') || !g.navigator) return;
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
    if (!DISABLED.has('navigator') && g.NavigatorUAData && g.NavigatorUAData.prototype) {
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

    __CANVAS_RUNTIME__
    installCanvas(g);

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
    if (!CONTRACT_V2 && !DISABLED.has('webgl')) {
        try {
            if (g.WebGLRenderingContext) hookWebGL(g.WebGLRenderingContext.prototype);
            if (g.WebGL2RenderingContext) hookWebGL(g.WebGL2RenderingContext.prototype);
        } catch(e) {}
    }

})();
