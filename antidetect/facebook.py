"""Fanpage scraping qua CDP của chính profile đang chạy.

Tách khỏi launcher.py (P1-6): JS đọc DOM, parser ngày tạo và vòng đời tab
CDP nằm chung một module để test độc lập, không cần BrowserLauncher.
"""
import asyncio
import datetime
import json
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional


# Fanpage listing: mở tab facebook.com/bookmarks/pages TRONG phiên profile
# (session đã đăng nhập), đọc danh sách page từ DOM rồi đóng tab. Facebook
# thay đổi DOM thường xuyên nên scraper chỉ dùng heuristic link + aria-label
# và trả về kèm URL để người dùng kiểm tra bằng mắt khi cần.
_FB_SCRAPE_JS = r"""
(async () => {
    const SCRAPE = () => {
        const out = {
            url: location.href,
            readyState: document.readyState,
            loggedOut: false,
            pages: []
        };
        try {
            out.loggedOut = location.pathname.startsWith('/login')
                || location.pathname === '/login.php'
                || location.search.indexOf('login') >= 0
                || !!document.querySelector('form[action*="login"]');
        } catch (e) {}
        if (out.loggedOut) return out;

        const BLOCKED = new Set([
            'feed', 'friends', 'watch', 'marketplace', 'groups', 'gaming',
            'events', 'messages', 'notifications', 'search', 'bookmarks',
            'pages', 'settings', 'help', 'privacy', 'policies', 'legal',
            'about', 'ads', 'adpreferences', 'business', 'careers',
            'developer', 'login', 'signup', 'recover', 'reg', 'save',
            'stories', 'reels', 'photo', 'video', 'videos', 'notes',
            'ondevice', 'portal', 'oculus', 'shop', 'pay', 'wallet', 'games',
            'ajax', 'api', 'graphql', 'dialog', 'share', 'sharer', 'hashtag',
            'people', 'watchparty', 'mcnut', 'metaverse', 'terms'
        ]);
        const anchors = Array.from(document.querySelectorAll('a[href]'));
        for (const a of anchors) {
            let text = (a.getAttribute('aria-label') || a.textContent || '')
                .trim().replace(/\s+/g, ' ');
            text = text.replace(/^(Ảnh đại diện của|Profile picture of)\s+/i, '').trim();
            if (!text || text.length < 2 || text.length > 100) continue;
            const href = a.href || '';
            if (!href) continue;
            let u;
            try { u = new URL(href, location.origin); } catch (e) { continue; }
            const host = (u.hostname || '').replace(/^www\./, '');
            if (host !== 'facebook.com' && host !== 'm.facebook.com'
                && host !== 'web.facebook.com') continue;
            const path = u.pathname.replace(/\/+$/, '');
            if (!path || path === '/') continue;
            let id = null;
            let score = 0;
            const mPages = path.match(/^\/pages\/[^/]+\/(\d{5,})/);
            if (mPages) { id = mPages[1]; score = 2; }
            if (path === '/profile.php') {
                const qid = u.searchParams.get('id');
                if (qid && /^\d{5,}$/.test(qid)) { id = qid; score = 2; }
                else continue;
            }
            if (!id) {
                const segs = path.split('/').filter(Boolean);
                if (segs.length === 1) {
                    const seg = segs[0].toLowerCase();
                    if (BLOCKED.has(seg)) continue;
                    if (segs[0].length < 2 || segs[0].length > 60) continue;
                    if (u.search && !u.search.startsWith('?sk=')
                        && !u.search.startsWith('?ref=')
                        && !u.search.startsWith('?id=')) continue;
                    score = 1;
                } else {
                    continue;
                }
            }
            out.pages.push({
                name: text,
                url: (path === '/profile.php' && id) ? (u.origin + '/' + id) : (u.origin + path + (id ? '' : (u.search || ''))),
                id: id,
                _score: score
            });
        }
        const seen = new Map();
        for (const p of out.pages) {
            const key = p.id || p.url;
            const prev = seen.get(key);
            if (!prev || p._score > prev._score
                || (p._score === prev._score && p.name.length > prev.name.length)) {
                seen.set(key, p);
            }
        }
        out.pages = Array.from(seen.values())
            .sort((a, b) => b._score - a._score)
            .slice(0, 100)
            .map(p => ({ name: p.name, url: p.url, id: p.id }));
        return out;
    };

    const t0 = Date.now();
    while (Date.now() - t0 < 12000) {
        const r = SCRAPE();
        if (r.loggedOut || r.pages.length > 0) return r;
        await new Promise(res => setTimeout(res, 500));
    }
    return SCRAPE();
})()
"""


# Đồng bộ chi tiết từng fanpage (tuổi + hoạt động): chạy trên chính trang
# page/about, thu (1) các mốc thời gian "X ngày trước" của bài đăng để suy ra
# bài gần nhất, và (2) đoạn text quanh nhãn "Ngày tạo trang"/"Page created"
# để suy ra ngày tạo. Facebook đổi DOM thường xuyên — mọi giá trị đều giữ
# raw text để hiển thị lại khi parse thất bại.
_FB_PAGE_METRICS_JS = r"""
(async () => {
    const RXS = [
        [/(\d+)\s+giây\s+trước/i, 's'], [/(\d+)\s+phút\s+trước/i, 'm'],
        [/(\d+)\s+giờ\s+trước/i, 'h'], [/(\d+)\s+ngày\s+trước/i, 'd'],
        [/(\d+)\s+tuần\s+trước/i, 'w'], [/(\d+)\s+tháng\s+trước/i, 'mo'],
        [/(\d+)\s+năm\s+trước/i, 'y'],
        [/(\d+)\s+seconds?\s+ago/i, 's'], [/(\d+)\s+minutes?\s+ago/i, 'm'],
        [/(\d+)\s+hours?\s+ago/i, 'h'], [/(\d+)\s+days?\s+ago/i, 'd'],
        [/(\d+)\s+weeks?\s+ago/i, 'w'], [/(\d+)\s+months?\s+ago/i, 'mo'],
        [/(\d+)\s+years?\s+ago/i, 'y'],
    ];
    const CREATED_LABEL = /(ngày\s+tạo(?:\s+trang)?|page\s+created|created\s+on|date\s+created|được\s+tạo|phát\s+hành)/i;

    const scrape = () => {
        const out = {
            url: location.href,
            readyState: document.readyState,
            title: (document.title || '').slice(0, 120),
            times: [],
            createdRaw: null
        };
        const push = (n, u) => {
            if (out.times.length < 50) out.times.push({ n: parseInt(n, 10), u: u });
        };
        const seen = new Set();
        try {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node, count = 0;
            while ((node = walker.nextNode()) && count < 4000) {
                const t = (node.textContent || '').trim();
                if (!t || t.length > 48 || seen.has(t)) continue;
                count += 1; seen.add(t);
                let matched = false;
                for (const [rx, u] of RXS) {
                    const m = t.match(rx);
                    if (m) { push(m[1], u); matched = true; break; }
                }
                if (!matched) {
                    const ms = t.match(/^(\d+)([smhdw])$/i);
                    if (ms) push(ms[1], ms[2].toLowerCase());
                }
            }
        } catch (e) {}
        try {
            const txt = document.body ? (document.body.innerText || '') : '';
            const m = txt.match(new RegExp(CREATED_LABEL.source + '[^\\n]{0,100}', 'i'));
            if (m) out.createdRaw = m[0].slice(0, 140);
            if (!out.createdRaw) {
                const html = document.documentElement ? (document.documentElement.outerHTML || '') : '';
                const idx = html.indexOf('"field_type":"creation_date"');
                if (idx !== -1) {
                    const snippet = html.slice(Math.max(0, idx - 220), idx + 120);
                    const matches = Array.from(snippet.matchAll(/"text":"([^"]+)"/g));
                    if (matches.length > 0) out.createdRaw = matches[matches.length - 1][1];
                }
            }
        } catch (e) {}
        return out;
    };

    const t0 = Date.now();
    while (Date.now() - t0 < 6000) {
        const r = scrape();
        if ((r.times.length > 0 && r.readyState === 'complete') || r.createdRaw) {
            return r;
        }
        await new Promise(res => setTimeout(res, 500));
    }
    return scrape();
})()
"""


_FB_UNIT_DAYS = {"s": 0, "m": 0, "h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}

_FB_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def fb_unit_to_days(num: int, unit: str) -> int:
    return int(num) * _FB_UNIT_DAYS.get(str(unit).lower(), 0)


def parse_fb_created_date(raw: Optional[str]) -> Optional[datetime.date]:
    """Best-effort parse ngày tạo trang từ đoạn text quanh nhãn của Facebook.

    Hỗ trợ các format thường gặp: dd/mm/yyyy (và mm/dd/yyyy khi rõ ràng),
    "ngày D tháng M, YYYY", "March 5, 2020", ISO YYYY-MM-DD. Trả None khi
    không khớp — caller hiển thị raw text thay vì đoán.
    """
    if not raw:
        return None
    text = raw.lower()
    try:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if m:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})\s+tháng\s+(\d{1,2}),?\s*(\d{4})", text)
        if m:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = re.search(
            r"(" + "|".join(_FB_MONTHS_EN) + r")\s+(\d{1,2}),?\s+(\d{4})", text
        )
        if m:
            return datetime.date(int(m.group(3)), _FB_MONTHS_EN[m.group(1)], int(m.group(2)))
        m = re.search(
            r"(\d{1,2})\s+(" + "|".join(_FB_MONTHS_EN) + r")(?:,?\s+(\d{4}))?", text
        )
        if m:
            yr = int(m.group(3)) if m.group(3) else datetime.date.today().year
            return datetime.date(yr, _FB_MONTHS_EN[m.group(2)], int(m.group(1)))
        m = re.search(r"(\d{1,2})\s+tháng\s+(\d{1,2})$", text.strip())
        if m:
            return datetime.date(datetime.date.today().year, int(m.group(2)), int(m.group(1)))
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", text)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12 >= b:
                return datetime.date(y, b, a)  # chắc chắn dd/mm
            if b > 12 >= a:
                return datetime.date(y, a, b)  # chắc chắn mm/dd
            # Ambiguous: locale VN ưu tiên dd/mm.
            return datetime.date(y, b, a)
    except ValueError:
        return None
    return None


def classify_facebook_page(entry: Dict[str, Any], today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """Quy tắc quản lý farm fanpage.

    - Trạng thái đăng bài: bài gần nhất trong 7 ngày → "active" (đang đăng),
      lâu hơn hoặc không có bài → "stopped"; chưa từng quét chi tiết → None.
    - "Sẵn sàng": page đã ngâm đủ 30 ngày tuổi (tính từ ngày tạo).
    """
    today = today or datetime.date.today()
    age_days = None
    created = entry.get("created_date")
    if created:
        try:
            age_days = (today - datetime.date.fromisoformat(str(created))).days
        except ValueError:
            age_days = None

    last_days = entry.get("last_post_days")
    if last_days is None:
        status = "unknown" if entry.get("detail_synced") else None
    else:
        status = "active" if last_days <= 7 else "stopped"

    return {
        "age_days": age_days,
        "status": status,
        "ready": age_days is not None and age_days >= 30,
    }


async def fetch_pages_cdp(port: int, timeout: float = 40.0) -> Dict[str, Any]:
    import websockets

    def _http_json(path, method="GET"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {}

    from urllib.parse import quote

    new_url = f"/json/new?{quote('https://www.facebook.com/bookmarks/pages', safe='')}"
    try:
        try:
            info = await asyncio.to_thread(_http_json, new_url, "PUT")
        except Exception:
            info = await asyncio.to_thread(_http_json, new_url, "GET")
        target_id = (info or {}).get("id")
    except Exception as e:
        raise RuntimeError(
            f"Không mở được tab Facebook trong profile: {type(e).__name__}: {e}"
        ) from e
    if not target_id:
        raise RuntimeError("Chrome không trả về tab mới.")

    try:
        # Đợi tab navigate tới facebook.com (auto-attach sẽ configure
        # stealth rồi resume tab như mọi tab user mở).
        ws_url = None
        deadline = time.monotonic() + min(timeout, 15.0)
        while time.monotonic() < deadline and not ws_url:
            try:
                targets = await asyncio.to_thread(_http_json, "/json/list")
                for t in targets or []:
                    if t.get("id") != target_id:
                        continue
                    url = (t.get("url") or "").lower()
                    if "facebook.com" in url and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
            except Exception:
                pass
            if not ws_url:
                await asyncio.sleep(0.4)
        if not ws_url:
            raise RuntimeError(
                "Tab Facebook không tải được trong thời gian chờ."
            )

        async with websockets.connect(
            ws_url, max_size=10 * 1024 * 1024, open_timeout=3
        ) as ws:
            # Tab vừa mở: execution context có thể chưa sẵn sàng khi
            # evaluate đầu tiên gửi đi (CDP trả protocol error). Retry
            # cho tới khi trang trả được dict dữ liệu.
            eval_deadline = time.monotonic() + min(timeout, 30.0)
            attempt = 0
            while time.monotonic() < eval_deadline and attempt < 5:
                attempt += 1
                await ws.send(json.dumps({
                    "id": 60001,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": _FB_SCRAPE_JS,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }))
                resp_deadline = min(
                    eval_deadline, time.monotonic() + 12.0
                )
                got_value = None
                context_not_ready = False
                while time.monotonic() < resp_deadline:
                    msg = json.loads(await asyncio.wait_for(
                        ws.recv(),
                        timeout=max(0.2, resp_deadline - time.monotonic()),
                    ))
                    if msg.get("id") != 60001:
                        continue
                    err = msg.get("error")
                    if err:
                        text = str(err.get("message") or "")
                        if "context" in text.lower() or "Navigation" in text:
                            context_not_ready = True
                        else:
                            raise RuntimeError(
                                f"CDP từ chối đọc tab Facebook: {text}"
                            )
                        break
                    if (msg.get("result") or {}).get("exceptionDetails"):
                        raise RuntimeError(
                            "Lỗi đọc trang Facebook trong tab profile."
                        )
                    got_value = (
                        (msg.get("result") or {}).get("result") or {}
                    ).get("value")
                    break
                if isinstance(got_value, dict):
                    got_value["logged_in"] = not got_value.get("loggedOut")
                    return got_value
                if not context_not_ready:
                    await asyncio.sleep(0.6)
                else:
                    await asyncio.sleep(0.8)
            raise RuntimeError("Đọc trang Facebook quá thời gian chờ.")
    finally:
        # Tab này do tool mở — luôn đóng lại, kể cả khi scrape lỗi.
        try:
            await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "PUT")
        except Exception:
            try:
                await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "GET")
            except Exception:
                pass


async def fetch_pages_deep_cdp(
    port: int, pages: List[Dict[str, Any]], timeout: float = 120.0
) -> Dict[str, Dict[str, Any]]:
    """Quét chi tiết từng page: bài gần nhất (feed) + ngày tạo (about).

    Dùng MỘT tab duyệt tuần tự các page thay vì mở N tab — mỗi page tốn
    ~4-8s (2 lần navigate). Trả {page_url: {"times", "created_raw",
    "last_post_raw", "title"}}. Trang nào lỗi vẫn có entry rỗng để UI
    hiển thị "không rõ" thay vì treo.
    """
    import websockets

    def _http_json(path, method="GET"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {}

    from urllib.parse import quote

    try:
        try:
            info = await asyncio.to_thread(_http_json, "/json/new?about%3Ablank", "PUT")
        except Exception:
            info = await asyncio.to_thread(_http_json, "/json/new?about%3Ablank", "GET")
        target_id = (info or {}).get("id")
    except Exception as e:
        raise RuntimeError(
            f"Không mở được tab quét chi tiết: {type(e).__name__}: {e}"
        ) from e
    if not target_id:
        raise RuntimeError("Chrome không trả về tab mới.")

    results: Dict[str, Dict[str, Any]] = {}
    try:
        ws_url = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not ws_url:
            try:
                targets = await asyncio.to_thread(_http_json, "/json/list")
                for t in targets or []:
                    if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
            except Exception:
                pass
            if not ws_url:
                await asyncio.sleep(0.3)
        if not ws_url:
            raise RuntimeError("Tab quét chi tiết không kết nối được.")

        async with websockets.connect(ws_url, open_timeout=3) as ws:
            msg_id = 61000

            async def _evaluate(expression: str, deadline_s: float = 9.0):
                """Navigate-ready evaluate với retry khi context chưa sẵn."""
                nonlocal msg_id
                end = time.monotonic() + deadline_s
                attempt = 0
                while time.monotonic() < end and attempt < 6:
                    attempt += 1
                    msg_id += 1
                    my_id = msg_id
                    await ws.send(json.dumps({
                        "id": my_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expression,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    }))
                    resp_deadline = min(end, time.monotonic() + 7.0)
                    got = None
                    not_ready = False
                    while time.monotonic() < resp_deadline:
                        msg = json.loads(await asyncio.wait_for(
                            ws.recv(),
                            timeout=max(0.2, resp_deadline - time.monotonic()),
                        ))
                        if msg.get("id") != my_id:
                            continue
                        err = msg.get("error")
                        if err:
                            text = str(err.get("message") or "")
                            if "context" in text.lower() or "Navigation" in text:
                                not_ready = True
                            break
                        value = ((msg.get("result") or {}).get("result") or {}).get("value")
                        got = value if isinstance(value, dict) else None
                        break
                    if got is not None:
                        return got
                    await asyncio.sleep(0.7 if not_ready else 0.5)
                return None

            for page in pages:
                page_url = (page.get("url") or "").strip()
                entry: Dict[str, Any] = {
                    "times": [], "created_raw": None, "last_post_raw": None,
                }
                if page_url:
                    # 1) Trang chính page: mốc thời gian bài đăng (feed).
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id,
                        "method": "Page.navigate",
                        "params": {"url": page_url},
                    }))
                    main_metrics = await _evaluate(_FB_PAGE_METRICS_JS)
                    if main_metrics:
                        entry["times"] = main_metrics.get("times") or []
                        entry["title"] = main_metrics.get("title")
                    # 2) Trang about_profile_transparency: nhãn ngày tạo trang.
                    about_url = page_url.rstrip("/") + "/about_profile_transparency"
                    msg_id += 1
                    await ws.send(json.dumps({
                        "id": msg_id,
                        "method": "Page.navigate",
                        "params": {"url": about_url},
                    }))
                    about_metrics = await _evaluate(_FB_PAGE_METRICS_JS)
                    if about_metrics and about_metrics.get("createdRaw"):
                        entry["created_raw"] = about_metrics["createdRaw"]
                    elif main_metrics and main_metrics.get("createdRaw"):
                        entry["created_raw"] = main_metrics["createdRaw"]
                results[page_url] = entry
        return results
    finally:
        try:
            await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "PUT")
        except Exception:
            try:
                await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "GET")
            except Exception:
                pass


def merge_page_details(
    data: Dict[str, Any], details: Dict[str, Dict[str, Any]]
) -> None:
    """Gắn ngày tạo + hoạt động gần nhất vào từng page trong ``data``."""
    now_iso = datetime.date.today().isoformat()
    for page in data.get("pages") or []:
        info = details.get(page.get("url")) or {}
        page["created_raw"] = info.get("created_raw")
        page["last_post_raw"] = info.get("last_post_raw")
        # Entry rỗng vẫn có key "times"/"created_raw" nên bool(info) luôn true;
        # chỉ đánh dấu đã quét khi thực sự đọc được dữ liệu.
        page["detail_synced"] = bool(
            info.get("times") or info.get("created_raw")
        )
        created = parse_fb_created_date(page.get("created_raw"))
        if created:
            page["created_date"] = created.isoformat()
        times = info.get("times") or []
        if times:
            best = min(times, key=lambda t: fb_unit_to_days(t["n"], t["u"]))
            page["last_post_days"] = fb_unit_to_days(best["n"], best["u"])
            if not page.get("last_post_raw"):
                page["last_post_raw"] = f"{best['n']} {best['u']}"
        page.update(classify_facebook_page(page))
        page["detail_synced_at"] = now_iso
