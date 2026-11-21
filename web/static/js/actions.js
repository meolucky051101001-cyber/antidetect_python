// Event delegation thay cho inline handler (onclick=...), để CSP không cần
// script-src-attr 'unsafe-inline'. Phần tử khai báo:
//   data-on-click="tenHam" data-on-click-args='["chuoi", 1, "$value"]'
// Token đặc biệt: "$value" (el.value), "$checked" (el.checked), "$el", "$event".
(function () {
    'use strict';

    const EVENTS = ['click', 'change', 'input'];
    const BUILTINS = {
        navigateTo(url) {
            if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//')) {
                window.location.href = url;
            }
        },
    };

    function escapeAttr(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function resolveArg(arg, el, event) {
        if (arg === '$value') return el.value;
        if (arg === '$checked') return el.checked;
        if (arg === '$el') return el;
        if (arg === '$event') return event;
        return arg;
    }

    function invoke(el, type, event) {
        const name = el.getAttribute('data-on-' + type);
        const fn = Object.prototype.hasOwnProperty.call(BUILTINS, name) ? BUILTINS[name] : window[name];
        if (typeof fn !== 'function') {
            console.warn('[actions] Không tìm thấy handler:', name);
            return;
        }
        let args = [];
        const raw = el.getAttribute('data-on-' + type + '-args');
        if (raw) {
            try {
                args = JSON.parse(raw);
            } catch (err) {
                console.warn('[actions] data-on-' + type + '-args không hợp lệ:', raw);
                return;
            }
            if (!Array.isArray(args)) args = [args];
        }
        fn.apply(el, args.map((arg) => resolveArg(arg, el, event)));
    }

    function dispatch(event) {
        const attr = 'data-on-' + event.type;
        let el = event.target instanceof Element ? event.target : event.target && event.target.parentElement;
        while (el && el !== document.documentElement) {
            if (el.hasAttribute(attr) && !el.disabled) {
                invoke(el, event.type, event);
                if (event.cancelBubble) break;
            }
            el = el.parentElement;
        }
    }

    EVENTS.forEach((type) => document.addEventListener(type, dispatch));

    // Sinh thuộc tính cho HTML render bằng JS:
    //   `<button ${actionAttrs('click', 'startProfile', id)}>`
    window.actionAttrs = function actionAttrs(type, name, ...args) {
        let out = `data-on-${type}="${escapeAttr(name)}"`;
        if (args.length) out += ` data-on-${type}-args="${escapeAttr(JSON.stringify(args))}"`;
        return out;
    };
})();
