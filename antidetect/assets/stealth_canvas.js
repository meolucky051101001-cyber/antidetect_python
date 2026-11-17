// Canvas read/export policy shared by window and worker payloads.
// All records and original methods are closure-owned; no page-global marker.
function canvasIsComplex(data) {
    const pixels = data.length >> 2;
    if (pixels < 256) return false;
    const seen = new Set();
    const stride = Math.max(1, Math.floor(pixels / 4096));
    for (let p = 0; p < pixels; p += stride) {
        const i = p * 4;
        if (data[i + 3] !== 255) continue;
        seen.add((data[i] << 16) | (data[i + 1] << 8) | data[i + 2]);
        if (seen.size >= 64) return true;
    }
    return false;
}
function canvasNoise(data, seed, startX, startY, width) {
    // Revision 1 keeps its read-region complexity policy for explicit legacy
    // profiles. Revision 2 is coordinate based for full/tile/crop consistency.
    if (!CONTRACT_V2 && !canvasIsComplex(data)) return;
    // Float16/P3 pipelines require a separate policy; retain native pixels.
    if (!data || data.BYTES_PER_ELEMENT !== 1) return;
    for (let i = 0; i < data.length; i += 4) {
        const p = i / 4;
        const x = startX + p % width;
        const y = startY + Math.floor(p / width);
        const hash = ((x * 374761393 + y * 668265263 + seed) ^ (seed >>> 13)) >>> 0;
        if (data[i + 3] === 255 && hash % 7 === 0) {
            const delta = hash % 2 === 0 ? 1 : -1;
            const rNoise = ((hash * 1103515245 + 12345) >>> 16) % 3;
            data[i] = Math.min(255, Math.max(0, data[i] + (rNoise >= 1 ? delta : 0)));
            data[i+1] = Math.min(255, Math.max(0, data[i+1] + (rNoise <= 1 ? delta : 0)));
            data[i+2] = Math.min(255, Math.max(0, data[i+2] + (rNoise >= 1 ? -delta : delta)));
        }
    }
}
function installCanvas(w) {
    if (DISABLED.has('canvas')) return;
    const contexts = new WeakMap();
    const readers = new WeakMap();
    const installed = new WeakSet();
    const numericSeed = Number(CFG.canvasSeed);
    const seed = CONTRACT_V2 && Number.isFinite(numericSeed) ? numericSeed : (numericSeed || 12345);

    function modifyRead(image, args, context) {
        // Repeating WebIDL conversion on objects could invoke user code twice.
        // Those uncommon argument forms retain native data.
        if (CONTRACT_V2 && Array.from(args).slice(0,4).some(
            v => v !== null && (typeof v === 'object' || typeof v === 'function'))) return image;
        if (CONTRACT_V2 && typeof context.getContextAttributes === 'function') {
            const attrs = context.getContextAttributes();
            if (attrs.colorSpace && attrs.colorSpace !== 'srgb') return image;
        }
        if (CONTRACT_V2 && image.colorSpace && image.colorSpace !== 'srgb') return image;
        let x = Number(args[0]) | 0, y = Number(args[1]) | 0;
        const width = Number(args[2]) | 0, height = Number(args[3]) | 0;
        if (CONTRACT_V2) {
            if (width < 0) x += width;
            if (height < 0) y += height;
        }
        canvasNoise(image.data, seed, x, y, CONTRACT_V2 ? image.width : (width || 1));
        return image;
    }
    function hookContext(proto) {
        if (!proto || installed.has(proto)) return;
        installed.add(proto);
        const read = proto.getImageData;
        if (typeof read !== 'function') return;
        readers.set(proto, read);
        proto.getImageData = markNative(function(sx,sy,sw,sh) {
            const image = read.apply(this, arguments);
            return modifyRead(image, arguments, this);
        }, 'getImageData', 4);
    }
    hookContext(w.CanvasRenderingContext2D && w.CanvasRenderingContext2D.prototype);
    hookContext(w.OffscreenCanvasRenderingContext2D && w.OffscreenCanvasRenderingContext2D.prototype);

    function nativeReader(context) {
        for (let proto = Object.getPrototypeOf(context); proto; proto = Object.getPrototypeOf(proto)) {
            if (readers.has(proto)) return readers.get(proto);
        }
        return null;
    }
    function snapshot(canvas, offscreen) {
        const context = contexts.get(canvas);
        // Do not call getContext('2d') on an uninitialized/WebGL canvas:
        // export must not lock its context type or alter a graphics pipeline.
        if (!context || !canvas.width || !canvas.height) return null;
        const read = nativeReader(context);
        if (!read) return null;
        const attrs = typeof context.getContextAttributes === 'function' ? context.getContextAttributes() : {};
        if (CONTRACT_V2 && attrs.colorSpace && attrs.colorSpace !== 'srgb') return null;
        const image = read.call(context, 0, 0, canvas.width, canvas.height);
        if (!image.data || image.data.BYTES_PER_ELEMENT !== 1) return null;
        canvasNoise(image.data, seed, 0, 0, canvas.width);
        const copy = offscreen ? new w.OffscreenCanvas(canvas.width, canvas.height) : w.document.createElement('canvas');
        if (!offscreen) { copy.width = canvas.width; copy.height = canvas.height; }
        const target = copy.getContext('2d', attrs);
        if (!target) return null;
        target.putImageData(image, 0, 0);
        return copy;
    }
    function hookCanvas(proto, offscreen) {
        if (!proto) return;
        const getContext = proto.getContext;
        if (typeof getContext === 'function') {
            proto.getContext = markNative(function() {
                const context = getContext.apply(this, arguments);
                if (context && nativeReader(context)) contexts.set(this, context);
                return context;
            }, 'getContext', 1);
        }
        const methods = offscreen ? ['convertToBlob'] : ['toDataURL', 'toBlob'];
        for (const name of methods) {
            const original = proto[name];
            if (typeof original !== 'function') continue;
            proto[name] = markNative(function() {
                let copy;
                try { copy = snapshot(this, offscreen); }
                catch(e) {
                    // Invoke native export on the original to preserve its
                    // exception vs Promise rejection (including tainted input).
                    return original.apply(this, arguments);
                }
                return original.apply(copy || this, arguments);
            }, name, name === 'toBlob' ? 1 : 0);
        }
    }
    hookCanvas(w.HTMLCanvasElement && w.HTMLCanvasElement.prototype, false);
    hookCanvas(w.OffscreenCanvas && w.OffscreenCanvas.prototype, true);
}
