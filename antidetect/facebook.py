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

from .fanpage_scan import (
    COLLECTOR_VERSION,
    DISCOVERY_DEADLINE_S,
    MAX_PAGES,
    MAX_SCROLL_ROUNDS,
    STABLE_ROUNDS,
    UI_UNRECOGNIZED_ROUNDS,
    PageAccumulator,
    ScanAborted,
    evaluate_discovery,
)

# Các targetId của tab tạm do trình quét fanpage tự mở và tự đóng.
# Launcher dùng tập này để không ghi đè tab tạm vào danh sách tab phiên của người dùng.
TRANSIENT_TARGET_IDS: set = set()


# Fanpage listing: mở tab facebook.com/bookmarks/pages TRONG phiên profile
# (session đã đăng nhập). Mỗi vòng đọc DOM, ghi nhận bằng chứng kết thúc
# (tổng hiển thị / sentinel / empty state), rồi cuộn hoặc bấm tải thêm.
# Python giữ tập tích lũy ngoài DOM nên list ảo hóa không làm mất page.
_FB_SCRAPE_JS = r"""
(async () => {
    const PARSER = 'f4-2026-11-05.1';
    const out = {
        url: location.href,
        readyState: document.readyState,
        title: (document.title || '').slice(0, 120),
        loggedOut: false,
        accountId: null,
        pages: [],
        total: null,
        endMarker: false,
        emptyConfirmed: false,
        uiRecognized: false,
        loading: false,
        containerFound: false,
        scrolled: false,
        clickedMore: 0,
        parserVersion: PARSER
    };
    try {
        out.loggedOut = location.pathname.startsWith('/login')
            || location.pathname === '/login.php'
            || location.search.indexOf('login') >= 0
            || !!document.querySelector('form[action*="login"]');
    } catch (e) {}
    if (out.loggedOut) return out;

    // Identity: c_user đọc mỗi vòng; HTML chỉ quét một lần rồi cache attribute.
    try {
        const m = (document.cookie || '').match(/(?:^|;\s*)c_user=(\d{5,})/);
        if (m) out.accountId = m[1];
    } catch (e) {}
    if (!out.accountId) {
        try {
            out.accountId = document.documentElement
                ? document.documentElement.getAttribute('data-fb-scan-account')
                : null;
        } catch (e) {}
    }
    if (!out.accountId) {
        try {
            const html = document.documentElement
                ? (document.documentElement.outerHTML || '') : '';
            const rxs = [
                /"USER_ID":"(\d{5,})"/,
                /"userID":"(\d{5,})"/,
                /"actor_id":"(\d{5,})"/,
                /"profile_id":"(\d{5,})"/
            ];
            for (const rx of rxs) {
                const m = html.match(rx);
                if (m) { out.accountId = m[1]; break; }
            }
            if (out.accountId && document.documentElement) {
                document.documentElement.setAttribute('data-fb-scan-account', out.accountId);
            }
        } catch (e) {}
    }

    const main = document.querySelector('div[role="main"]')
        || document.querySelector('[role="main"]')
        || document.body;
    if (!main) return out;
    const mainText = (main.innerText || '').replace(/\s+/g, ' ').trim();
    const head = mainText.slice(0, 800);
    try {
        out.loading = !!main.querySelector('[role="progressbar"]')
            || !!main.querySelector('[aria-busy="true"]')
            || /(^|\s)(đang tải|loading)(\s|$)/i.test(head.slice(0, 300));
    } catch (e) {}

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
    const anchors = Array.from(main.querySelectorAll('a[href]'));
    for (const a of anchors) {
        if (out.pages.length >= 600) break;
        let text = (a.getAttribute('aria-label') || a.textContent || '')
            .trim().replace(/\s+/g, ' ');
        text = text.replace(/^(Ảnh đại diện của|Profile picture of)\s+/i, '').trim();
        if (!text || text.length < 2 || text.length > 100) continue;
        let u;
        try { u = new URL(a.href, location.origin); } catch (e) { continue; }
        const host = (u.hostname || '').replace(/^www\./, '').toLowerCase();
        if (host !== 'facebook.com' && host !== 'm.facebook.com'
            && host !== 'web.facebook.com') continue;
        const path = u.pathname.replace(/\/+$/, '');
        if (!path || path === '/') continue;
        let id = null;
        let score = 0;
        const mPages = path.match(/^\/pages\/[^/]+\/(\d{5,})/i);
        if (mPages) { id = mPages[1]; score = 2; }
        if (path.toLowerCase() === '/profile.php') {
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
                score = 1;
            } else {
                continue;
            }
        }
        out.pages.push({
            name: text,
            url: (path.toLowerCase() === '/profile.php' && id)
                ? (u.origin + '/' + id) : (u.origin + path),
            id: id,
            _score: score
        });
    }
    const seen = new Map();
    for (const p of out.pages) {
        const key = p.id ? ('id:' + p.id) : ('url:' + p.url.toLowerCase());
        const prev = seen.get(key);
        if (!prev || p._score > prev._score
            || (p._score === prev._score && p.name.length > prev.name.length)) {
            seen.set(key, p);
        }
    }
    out.pages = Array.from(seen.values()).map(p => ({
        name: p.name,
        url: p.url,
        id: p.id,
        id_status: p.id ? 'verified' : 'url_only'
    }));

    out.endMarker = /(đã xem hết|bạn đã xem hết|không còn (?:trang|kết quả)|no more (?:pages|results)|you'?ve seen all|end of (?:the )?list)/i.test(mainText);
    const emptyRx = /(chưa có (?:trang|fanpage) nào|không có (?:trang|fanpage) nào|bạn chưa tạo (?:trang|fanpage)|no pages yet|you (?:don'?t|do not) have any pages|haven'?t created any pages)/i;
    out.emptyConfirmed = !out.loading && out.pages.length === 0 && emptyRx.test(mainText);
    const listRegion = main.querySelector(
        'div[role="grid"], div[role="list"], ul[role="list"], div[role="article"]'
    );
    out.uiRecognized = out.pages.length > 0 || out.emptyConfirmed
        || out.endMarker || !!listRegion;

    const totalPatterns = [
        /\bpages?\s*[\(\:]\s*(\d+)\b/i,
        /\b(\d+)\s+(?:trang|pages?)\b/i,
        /\b(?:trang|pages?)\s*[:\-]?\s*(\d+)\b/i,
        /\b(\d+)\s+(?:kết quả|results?)\b/i
    ];
    for (const rx of totalPatterns) {
        const m = head.match(rx);
        if (m) {
            const n = parseInt(m[1], 10);
            if (!isNaN(n) && n >= 0 && n <= 100000) {
                if (n > 0 || out.emptyConfirmed) out.total = n;
                break;
            }
        }
    }

    // Cuộn container sâu nhất có nhiều link page; giữ marker để vòng sau
    // vẫn cuộn đúng phần tử dù React render lại.
    const isScrollable = (el) => {
        try {
            const st = getComputedStyle(el);
            return /(auto|scroll)/i.test(st.overflowY)
                && el.scrollHeight > el.clientHeight + 40;
        } catch (e) { return false; }
    };
    let container = null;
    try {
        container = main.querySelector('[data-fb-scan-scroll="1"]');
        if (container && !isScrollable(container)) container = null;
        if (!container) {
            const nodes = main.querySelectorAll('div,section');
            const limit = Math.min(nodes.length, 1200);
            let bestScore = -1;
            for (let i = 0; i < limit; i++) {
                const el = nodes[i];
                if (!isScrollable(el)) continue;
                const score = el.getElementsByTagName('a').length;
                if (score > bestScore) { container = el; bestScore = score; }
            }
            if (container) container.setAttribute('data-fb-scan-scroll', '1');
        }
    } catch (e) { container = null; }
    out.containerFound = !!container;
    try {
        if (container) {
            container.scrollTop = container.scrollHeight;
            out.scrolled = container.scrollHeight > container.clientHeight;
        } else {
            const before = window.scrollY || document.documentElement.scrollTop || 0;
            window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
            const after = window.scrollY || document.documentElement.scrollTop || 0;
            out.scrolled = after !== before;
        }
    } catch (e) {}

    try {
        let clicks = 0;
        const buttons = Array.from(
            main.querySelectorAll('div[role="button"], button')
        );
        for (const b of buttons) {
            const label = (b.innerText || b.getAttribute('aria-label') || '')
                .trim().replace(/\s+/g, ' ');
            if (/^(xem thêm|see more|tải thêm|load more|show more)$/i.test(label)) {
                b.click();
                clicks += 1;
                if (clicks >= 2) break;
            }
        }
        out.clickedMore = clicks;
    } catch (e) {}
    return out;
})()
"""


# Đồng bộ chi tiết từng fanpage (tuổi + hoạt động): chạy trên chính trang
# page/about, thu (1) mốc thời gian của bài do CHÍNH page đăng để suy ra bài
# gần nhất, và (2) đoạn text quanh nhãn "Ngày tạo trang"/"Page created".
#
# F3: chỉ đọc anchor thời gian nằm trong bài của page (permalink khớp page
# hiện tại), bỏ bài ghim/quảng cáo/gợi ý và bình luận. Facebook đổi DOM
# thường xuyên — mọi giá trị vẫn giữ raw để hiển thị khi parse thất bại.
_FB_PAGE_METRICS_JS = r"""
(async () => {
    const PARSER = 'f3-2026-11-05.1';
    const RXS = [
        [/(\d+)\s+giây\s+trước/i, 's'], [/(\d+)\s+phút\s+trước/i, 'm'],
        [/(\d+)\s+giờ\s+trước/i, 'h'], [/(\d+)\s+ngày\s+trước/i, 'd'],
        [/(\d+)\s+tuần\s+trước/i, 'w'], [/(\d+)\s+tháng\s+trước/i, 'mo'],
        [/(\d+)\s+năm\s+trước/i, 'y'],
        [/(\d+)\s+seconds?\s+ago/i, 's'], [/(\d+)\s+minutes?\s+ago/i, 'm'],
        [/(\d+)\s+hours?\s+ago/i, 'h'], [/(\d+)\s+days?\s+ago/i, 'd'],
        [/(\d+)\s+weeks?\s+ago/i, 'w'], [/(\d+)\s+months?\s+ago/i, 'mo'],
        [/(\d+)\s+years?\s+ago/i, 'y'],
        [/^(vừa\s+xong|just\s+now)$/i, 'now'],
    ];
    const CREATED_LABEL = /(ngày\s+tạo(?:\s+trang)?|page\s+created|created\s+on|date\s+created|được\s+tạo|phát\s+hành)/i;
    const PINNED = /(^|\n)\s*(đã\s+ghim|pinned\s+post|pinned)\b/i;
    const PROMOTED = /(được\s+tài\s+trợ|sponsored|được\s+đề\s+xuất|suggested\s+for\s+you)/i;

    const normPath = (p) => String(p || '').replace(/\/+$/, '').toLowerCase();
    const pagePath = normPath(location.pathname);
    let pageId = null;
    const pm = location.pathname.match(/^\/pages\/[^/]+\/(\d{5,})/i);
    if (pm) pageId = pm[1];
    if (!pageId) {
        const qid = new URLSearchParams(location.search).get('id');
        if (qid && /^\d{5,}$/.test(qid)) pageId = qid;
    }

    const linkInfo = (href) => {
        let u;
        try { u = new URL(href, location.origin); } catch (e) { return null; }
        if (!/^https?:$/.test(u.protocol)) return null;
        const host = (u.hostname || '').replace(/^www\./, '').toLowerCase();
        if (host !== 'facebook.com' && host !== 'm.facebook.com'
            && host !== 'web.facebook.com') return null;
        const path = normPath(u.pathname);
        const qid = u.searchParams.get('id');
        return {
            u: u, path: path,
            id: (qid && /^\d{5,}$/.test(qid)) ? qid : null,
            isComment: /\/comment\/|\/comments\/|comment_id=|\/replies\//i.test(u.pathname + u.search)
        };
    };
    const isPageLink = (li) => {
        if (!li) return false;
        if (pageId && li.id === pageId) return true;
        if (pageId && li.u.searchParams.get('id') === pageId) return true;
        if (pagePath && li.path === pagePath) return true;
        if (pagePath && li.path.indexOf(pagePath + '/') === 0) return true;
        if (pageId) {
            const segs = li.path.split('/').filter(Boolean);
            if (segs.indexOf(pageId) !== -1) return true;
            if (segs.length && segs[0].slice(-(pageId.length + 1)) === '-' + pageId) return true;
        }
        return false;
    };
    const isPermalink = (li) => {
        if (!li) return false;
        if (/\/posts\/|\/permalink\/|\/videos\/|\/photos\/|\/reel\//.test(li.path)) return true;
        return li.u.searchParams.has('story_fbid') || li.u.searchParams.has('fbid');
    };

    const scrape = () => {
        const out = {
            url: location.href,
            readyState: document.readyState,
            title: (document.title || '').slice(0, 120),
            times: [],
            createdRaw: null,
            followersRaw: null,
            likesRaw: null,
            category: null,
            observedPageId: pageId || null,
            accessState: 'ok',
            feedReady: false,
            articleCount: 0,
            parserVersion: PARSER
        };
        const seen = new Set();
        try {
            const pathLower = String(location.pathname || '').toLowerCase();
            if (pathLower.indexOf('/checkpoint') === 0) {
                out.accessState = 'checkpoint';
            } else if (pathLower.indexOf('/login') === 0 || (document.querySelector('input[name="email"]') && document.querySelector('input[name="pass"]'))) {
                out.accessState = 'login_required';
            }
        } catch (e) {}
        try {
            const articles = Array.from(document.querySelectorAll('div[role="article"]'));
            out.articleCount = articles.length;
            out.feedReady = !!document.querySelector('div[role="feed"]') || articles.length > 0;
            for (const article of articles) {
                if (out.times.length >= 20) break;
                // Bình luận cũng là role=article lồng trong bài — bỏ qua.
                if (article.parentElement
                    && article.parentElement.closest('div[role="article"]')) continue;
                const text = (article.innerText || '').trim();
                const head = text.slice(0, 240);
                if (PINNED.test(head)) continue;
                if (PROMOTED.test(head.slice(0, 320))) continue;
                const links = Array.from(article.querySelectorAll('a[href]'));
                let own = false;
                for (const a of links) {
                    const li = linkInfo(a.href);
                    if (li && !li.isComment && isPageLink(li)) { own = true; break; }
                }
                if (!own) continue;
                for (const a of links) {
                    if (out.times.length >= 20) break;
                    const li = linkInfo(a.href);
                    if (!li || li.isComment) continue;
                    const label = (a.getAttribute('aria-label') || a.textContent || '')
                        .trim().replace(/\s+/g, ' ');
                    if (!label || label.length > 48) continue;
                    let n = null, unit = null;
                    for (const [rx, u] of RXS) {
                        const m = label.match(rx);
                        if (m) {
                            n = (u === 'now') ? 0 : parseInt(m[1], 10);
                            unit = u;
                            break;
                        }
                    }
                    if (unit === null || n === null || isNaN(n)) continue;
                    if (!isPermalink(li) && !isPageLink(li)) continue;
                    const key = n + '|' + unit + '|' + label;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.times.push({
                        n: n, u: unit, raw: label,
                        url: (li.u.href || '').split('#')[0],
                        own: true
                    });
                }
            }
        } catch (e) {}
        try {
            const txt = document.body ? (document.body.innerText || '') : '';
            const m = txt.match(new RegExp(CREATED_LABEL.source + '[^\\n]{0,100}', 'i'));
            if (m) out.createdRaw = m[0].slice(0, 140);
            const html = document.documentElement ? (document.documentElement.outerHTML || '') : '';
            if (!out.createdRaw && html) {
                const idx = html.indexOf('"field_type":"creation_date"');
                if (idx !== -1) {
                    const snippet = html.slice(Math.max(0, idx - 220), idx + 120);
                    const matches = Array.from(snippet.matchAll(/"text":"([^"]+)"/g));
                    if (matches.length > 0) out.createdRaw = matches[matches.length - 1][1];
                }
            }
            if (!out.observedPageId && html) {
                const idMatch = html.match(/"pageID"\s*:\s*"(\d{5,})"/)
                    || html.match(/"delegate_page"\s*:\s*\{\s*"id"\s*:\s*"(\d{5,})"/)
                    || html.match(/fb:\/\/(?:page|profile)\/(\d{5,})/);
                if (idMatch) out.observedPageId = idMatch[1];
            }
            // Đọc follower/like/category từ vùng header/intro (ngoài bài viết role="article")
            const headerLinks = Array.from(document.querySelectorAll('a[href], span')).filter(el => !el.closest('div[role="article"]'));
            const flwRx = /\b(\d[\d.,]*\s*(?:k|m|b|nghìn|nghin|ngàn|ngan|vạn|van|tr|triệu|trieu|tỷ|ty)?)\s*(?:người\s+theo\s+dõi|nguoi\s+theo\s+doi|followers?)\b/i;
            const likeRx = /\b(\d[\d.,]*\s*(?:k|m|b|nghìn|nghin|ngàn|ngan|vạn|van|tr|triệu|trieu|tỷ|ty)?)\s*(?:lượt\s+thích|người\s+thích|likes?)\b/i;
            for (const el of headerLinks) {
                if (out.followersRaw && out.likesRaw) break;
                const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
                if (!t || t.length > 64) continue;
                if (!out.followersRaw) {
                    const mf = t.match(flwRx);
                    if (mf) out.followersRaw = mf[0].slice(0, 60);
                }
                if (!out.likesRaw) {
                    const ml = t.match(likeRx);
                    if (ml) out.likesRaw = ml[0].slice(0, 60);
                }
            }
            const catMatch = txt.match(/(?:^|\n)\s*(?:Trang|Page)\s*[·•]\s*([^\n·•]{2,60})/i);
            if (catMatch) out.category = catMatch[1].trim();
        } catch (e) {}
        return out;
    };

    const t0 = Date.now();
    while (Date.now() - t0 < 6000) {
        const r = scrape();
        if (r.createdRaw) return r;
        if (r.feedReady && (r.times.length > 0 || Date.now() - t0 > 3000)) return r;
        await new Promise(res => setTimeout(res, 400));
    }
    return scrape();
})()
"""


PARSER_VERSION = "f3-2026-11-05.1"
DEFAULT_DISPLAY_TIMEZONE = "Asia/Ho_Chi_Minh"
ACTIVITY_WINDOW_DAYS = 7
STALE_AFTER_HOURS = 48
_FUTURE_TOLERANCE = datetime.timedelta(minutes=5)
_UNIT_SECONDS = {
    "s": (1, 1),
    "m": (60, 60),
    "h": (3600, 3600),
    "d": (86400, 86400),
    "w": (7 * 86400, 7 * 86400),
    "mo": (31 * 86400, 28 * 86400),
    "y": (366 * 86400, 365 * 86400),
}
_UNIT_ALIASES = {
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "giây": "s", "giay": "s",
    "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "phút": "m", "phut": "m",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "giờ": "h", "gio": "h",
    "d": "d", "day": "d", "days": "d", "ngày": "d", "ngay": "d",
    "w": "w", "week": "w", "weeks": "w", "tuần": "w", "tuan": "w",
    "mo": "mo", "month": "mo", "months": "mo", "tháng": "mo", "thang": "mo",
    "y": "y", "year": "y", "years": "y", "năm": "y", "nam": "y",
    "now": "now", "justnow": "now",
}
_RELATIVE_PATTERNS = (
    (re.compile(r"(\d+)\s*(?:giây|giay)\s*trước", re.I), "s"),
    (re.compile(r"(\d+)\s*(?:phút|phut)\s*trước", re.I), "m"),
    (re.compile(r"(\d+)\s*(?:giờ|gio)\s*trước", re.I), "h"),
    (re.compile(r"(\d+)\s*(?:ngày|ngay)\s*trước", re.I), "d"),
    (re.compile(r"(\d+)\s*(?:tuần|tuan)\s*trước", re.I), "w"),
    (re.compile(r"(\d+)\s*(?:tháng|thang)\s*trước", re.I), "mo"),
    (re.compile(r"(\d+)\s*(?:năm|nam)\s*trước", re.I), "y"),
    (re.compile(r"(\d+)\s*seconds?\s*ago", re.I), "s"),
    (re.compile(r"(\d+)\s*minutes?\s*ago", re.I), "m"),
    (re.compile(r"(\d+)\s*hours?\s*ago", re.I), "h"),
    (re.compile(r"(\d+)\s*days?\s*ago", re.I), "d"),
    (re.compile(r"(\d+)\s*weeks?\s*ago", re.I), "w"),
    (re.compile(r"(\d+)\s*months?\s*ago", re.I), "mo"),
    (re.compile(r"(\d+)\s*years?\s*ago", re.I), "y"),
)
_JUST_NOW_RE = re.compile(r"^(?:vừa\s+xong|just\s+now)$", re.I)

_FB_UNIT_DAYS = {"s": 0, "m": 0, "h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}

_FB_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value: Any) -> Optional[datetime.datetime]:
    """Chuẩn hóa datetime/ISO string về UTC aware; None nếu không hợp lệ."""
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def to_utc_iso(value: Any) -> Optional[str]:
    dt = _as_utc(value)
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_display_timezone(value: Any = None):
    """Timezone hiển thị của profile; fallback Asia/Ho_Chi_Minh."""
    if isinstance(value, datetime.tzinfo):
        return value
    name = str(value or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    from zoneinfo import ZoneInfo

    return ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)


def fb_unit_to_days(num: int, unit: str) -> int:
    return int(num) * _FB_UNIT_DAYS.get(str(unit).lower(), 0)


def parse_relative_label(text: Optional[str]) -> Optional[dict]:
    """Đọc nhãn tương đối → {n, u, raw}; không khớp trả None."""
    if not text or not isinstance(text, str):
        return None
    raw = text.strip().replace("\xa0", " ")
    if _JUST_NOW_RE.match(raw):
        return {"n": 0, "u": "now", "raw": raw}
    for rx, unit in _RELATIVE_PATTERNS:
        m = rx.search(raw)
        if m:
            return {"n": int(m.group(1)), "u": unit, "raw": raw}
    m = re.match(r"^(\d+)\s*([smhdw])$", raw, re.I)
    if m:
        return {"n": int(m.group(1)), "u": m.group(2).lower(), "raw": raw}
    return None


def relative_label_interval(
    num: int, unit: str, observed_at: Any
) -> Optional[tuple]:
    """Khoảng [min, max] UTC cho nhãn tương đối theo QĐ-4.

    ``min`` là mốc cũ nhất có thể, ``max`` là mốc mới nhất có thể.
    """
    obs = _as_utc(observed_at)
    if obs is None:
        return None
    try:
        n = int(num)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > 10000:
        return None
    key = _UNIT_ALIASES.get(str(unit or "").strip().lower())
    if key == "now":
        return (obs - datetime.timedelta(seconds=60), obs)
    if key not in _UNIT_SECONDS:
        return None
    low, high = _UNIT_SECONDS[key]
    min_dt = obs - datetime.timedelta(seconds=(n + 1) * low)
    max_dt = obs - datetime.timedelta(seconds=n * high)
    if min_dt > max_dt:
        return None
    return (min_dt, max_dt)


def _parse_local_day(value: Any) -> Optional[datetime.date]:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def absolute_day_interval(day: Any, timezone: Any = None) -> Optional[tuple]:
    """Ngày lịch (không giờ) → cả ngày local [00:00, 24:00) quy về UTC."""
    parsed = _parse_local_day(day)
    if parsed is None:
        return None
    tz = resolve_display_timezone(timezone)
    start = datetime.datetime.combine(parsed, datetime.time.min, tzinfo=tz)
    return (start.astimezone(datetime.timezone.utc),
            (start + datetime.timedelta(days=1)).astimezone(datetime.timezone.utc))


def absolute_point_interval(value: Any) -> Optional[tuple]:
    """Timestamp tuyệt đối có giờ/timezone → một điểm UTC."""
    dt = _as_utc(value)
    if dt is None:
        return None
    return (dt, dt)


def normalize_last_post_fields(
    entry: Dict[str, Any],
    *,
    display_tz: Any = None,
    now: Any = None,
) -> Dict[str, Any]:
    """Chuẩn hóa timestamp bài đăng lúc đọc (không sửa dữ liệu gốc).

    - Có ``last_post_at_min/max`` hợp lệ → giữ nguyên, chỉ chuẩn hóa ISO.
    - Legacy ``last_post_days`` + ``detail_synced_at`` (ngày local) → khoảng
      ``[obs_min-(N+1)d, obs_max-N·d]``, precision=approximate, legacy=true.
    - Chỉ có ``facebook_pages_synced_at`` hoặc thiếu mốc → unknown, không
      gán "hôm nay". Idempotent: không đổi observed_at mỗi lần đọc.
    """
    page = dict(entry) if isinstance(entry, dict) else {}
    tz = resolve_display_timezone(display_tz)
    now_dt = _as_utc(now) or utc_now()

    existing_min = _as_utc(page.get("last_post_at_min"))
    existing_max = _as_utc(page.get("last_post_at_max"))
    if existing_min and existing_max and existing_min <= existing_max:
        page["last_post_at_min"] = to_utc_iso(existing_min)
        page["last_post_at_max"] = to_utc_iso(existing_max)
        if not page.get("last_post_precision"):
            page["last_post_precision"] = "approximate"
        page["last_post_legacy"] = bool(page.get("last_post_legacy", False))
        return page
    if page.get("last_post_at_min") or page.get("last_post_at_max"):
        # Có dữ liệu F3 nhưng hỏng/đảo ngược: giữ raw, không đoán lại.
        page["last_post_precision"] = "unknown"
        page["last_post_quality"] = "invalid"
        return page

    point = _as_utc(page.get("last_post_at"))
    if point is not None:
        page["last_post_at_min"] = to_utc_iso(point)
        page["last_post_at_max"] = to_utc_iso(point)
        page.setdefault("last_post_precision", "exact")
        page.setdefault("last_post_observed_at", to_utc_iso(point))
        page.setdefault("last_post_legacy", False)
        return page

    days_raw = page.get("last_post_days")
    try:
        days = int(days_raw) if days_raw is not None else None
    except (TypeError, ValueError):
        days = None
    synced_day = _parse_local_day(page.get("detail_synced_at"))
    has_scan_marker = bool(page.get("detail_synced")) or days is not None
    if days is None or days < 0 or days > 36500 or synced_day is None:
        if has_scan_marker:
            page.setdefault("last_post_precision", "unknown")
            page["activity_quality"] = (
                "legacy_missing_marker" if days is not None else "unknown"
            )
        return page

    obs_start = datetime.datetime.combine(synced_day, datetime.time.min, tzinfo=tz)
    obs_end = obs_start + datetime.timedelta(days=1)
    if obs_end > now_dt + datetime.timedelta(days=1):
        page["last_post_precision"] = "unknown"
        page["last_post_quality"] = "invalid"
        return page
    min_dt = obs_start - datetime.timedelta(days=days + 1)
    max_dt = obs_end - datetime.timedelta(days=days)
    page["last_post_at_min"] = to_utc_iso(min_dt)
    page["last_post_at_max"] = to_utc_iso(max_dt)
    page["last_post_observed_at"] = to_utc_iso(obs_end)
    page["last_post_precision"] = "approximate"
    page["last_post_source"] = "legacy_cache"
    page["last_post_legacy"] = True
    page["last_post_quality"] = "legacy"
    page["activity_checked_at"] = to_utc_iso(obs_end)
    page["activity_scan_complete"] = True
    page.setdefault("parser_version", "legacy")
    return page


def parse_fb_created_date(raw: Optional[str]) -> Optional[datetime.date]:
    """Best-effort parse ngày tạo trang từ đoạn text quanh nhãn của Facebook.

    Hỗ trợ dd/mm/yyyy (và mm/dd/yyyy khi rõ ràng), "ngày D tháng M, YYYY",
    "March 5, 2020", ISO YYYY-MM-DD. Không đoán năm khi nguồn thiếu năm —
    trả None để caller giữ raw thay vì tạo ngày giả.
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
            r"(\d{1,2})\s+(" + "|".join(_FB_MONTHS_EN) + r")\s+(\d{4})", text
        )
        if m:
            return datetime.date(int(m.group(3)), _FB_MONTHS_EN[m.group(2)], int(m.group(1)))
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


def _page_age_days(entry: Dict[str, Any], today: Optional[datetime.date]) -> Optional[int]:
    created = entry.get("created_date")
    if not created:
        return None
    try:
        created_date = datetime.date.fromisoformat(str(created))
    except ValueError:
        return None
    return (today - created_date).days


def classify_facebook_page(
    entry: Dict[str, Any],
    today: Optional[datetime.date] = None,
    *,
    now: Any = None,
    timezone: Any = None,
    stale_after_hours: int = STALE_AFTER_HOURS,
) -> Dict[str, Any]:
    """Trạng thái hoạt động theo khoảng timestamp + độ tươi của quan sát.

    - ``active`` khi mốc cũ nhất vẫn trong 7 ngày (chắc chắn còn gần).
    - ``long_absent`` khi mốc mới nhất đã quá 7 ngày (chắc chắn đã lâu).
    - ``uncertain`` khi khoảng ước lượng cắt qua ranh giới 7 ngày.
    - ``unknown`` khi quan sát cũ hơn ngưỡng stale, lần đọc thiếu phạm vi
      hoặc timestamp thiếu/hỏng; tuổi đã ghi nhận vẫn được trả.
    """
    page = entry if isinstance(entry, dict) else {}
    tz = resolve_display_timezone(timezone)
    now_dt = _as_utc(now) or utc_now()
    page = normalize_last_post_fields(page, display_tz=tz, now=now_dt)

    local_today = now_dt.astimezone(tz).date()
    if isinstance(today, datetime.datetime):
        today = today.date()
    if today is None:
        today = local_today
    age_days = _page_age_days(page, today)

    min_dt = _as_utc(page.get("last_post_at_min"))
    max_dt = _as_utc(page.get("last_post_at_max"))
    observed = _as_utc(page.get("last_post_observed_at"))
    legacy = bool(page.get("last_post_legacy"))
    precision = str(page.get("last_post_precision") or "unknown")
    has_scan_marker = (
        bool(page.get("detail_synced"))
        or page.get("last_post_days") is not None
        or precision != "unknown"
        or bool(page.get("activity_checked_at"))
    )

    result = {
        "age_days": age_days,
        "ready": age_days is not None and age_days >= 30,
        "last_post_precision": precision,
        "last_post_legacy": legacy,
        "activity_scan_complete": bool(page.get("activity_scan_complete", bool(min_dt and max_dt))),
        "activity_checked_at": page.get("activity_checked_at"),
        "last_post_observed_at": page.get("last_post_observed_at"),
        "last_post_source": page.get("last_post_source"),
        "last_post_url": page.get("last_post_url"),
        "parser_version": page.get("parser_version") or PARSER_VERSION,
        "last_post_age_min_days": None,
        "last_post_age_max_days": None,
        "last_post_days": None,
        "status": None,
        "activity_quality": "unknown",
        "activity_stale": False,
    }

    if min_dt is None or max_dt is None:
        result["status"] = "unknown" if has_scan_marker else None
        return result

    if page.get("last_post_at") and precision == "exact":
        result["last_post_at"] = page.get("last_post_at")

    effective_max = max_dt
    invalid = min_dt > max_dt or min_dt > now_dt + _FUTURE_TOLERANCE
    if max_dt > now_dt + _FUTURE_TOLERANCE:
        if legacy:
            # Ngày local chỉ có độ chính xác ngày: cận trên có thể vượt "now"
            # trong cùng ngày quan sát — kẹp lại thay vì coi là dữ liệu hỏng.
            effective_max = now_dt
        else:
            invalid = True
    if invalid:
        result["status"] = "unknown"
        result["activity_quality"] = "invalid"
        return result

    observed_stale = (
        observed is None
        or (now_dt - observed).total_seconds() > stale_after_hours * 3600
    )
    incomplete = result["activity_scan_complete"] is False
    stale = observed_stale or incomplete
    oldest_age_s = max(0.0, (now_dt - min_dt).total_seconds())
    youngest_age_s = max(0.0, (now_dt - effective_max).total_seconds())
    min_age_days = int(youngest_age_s // 86400)
    max_age_days = int(oldest_age_s // 86400)

    if stale:
        status = "unknown"
    elif oldest_age_s <= ACTIVITY_WINDOW_DAYS * 86400:
        status = "active"
    elif youngest_age_s > ACTIVITY_WINDOW_DAYS * 86400:
        status = "long_absent"
    else:
        status = "uncertain"

    if observed is None:
        quality = "unknown"
    elif incomplete:
        quality = "incomplete"
    elif observed_stale:
        quality = "stale"
    elif legacy:
        quality = "legacy"
    elif precision in ("exact", "day"):
        quality = "verified"
    else:
        quality = "approximate"

    result.update({
        "status": status,
        "activity_quality": quality,
        "activity_stale": stale,
        "last_post_age_min_days": min_age_days,
        "last_post_age_max_days": max_age_days,
        # Client cũ: tuổi nhỏ nhất (bài mới nhất có thể), tính tại response.
        "last_post_days": min_age_days,
    })
    return result


def _fb_listing_url_error(url: Any) -> Optional[str]:
    """None nếu là trang danh sách; "login" nếu bị đẩy về đăng nhập."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return "nav"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host.endswith("facebook.com"):
        return "nav"
    path = (parsed.path or "").lower()
    if path.startswith("/login") or path == "/login.php":
        return "login"
    if "/pages" in path or "/bookmarks" in path:
        return None
    return "nav"


async def fetch_pages_cdp(
    port: int,
    timeout: float = 90.0,
    *,
    expected_account_id: Optional[str] = None,
    should_abort: Optional[Any] = None,
) -> Dict[str, Any]:
    """Discovery tăng dần: đọc nhiều vòng, chỉ complete khi có bằng chứng.

    Trả contract F4 (``scan_id``, ``discovery_status``, ``completion_reason``,
    ``stability``, ``observed_count``, ...). Tab do collector mở luôn được
    đóng trong ``finally``; profile dừng giữa chừng → ``ScanAborted``.
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

    def _check_abort():
        if should_abort and should_abort():
            raise ScanAborted(
                "Profile đã dừng trong khi quét fanpage; giữ nguyên dữ liệu cũ."
            )

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
    TRANSIENT_TARGET_IDS.add(str(target_id))

    started_at = utc_now()
    try:
        # Đợi tab navigate tới facebook.com (auto-attach sẽ configure
        # stealth rồi resume tab như mọi tab user mở).
        ws_url = None
        connect_deadline = time.monotonic() + min(timeout, 15.0)
        while time.monotonic() < connect_deadline and not ws_url:
            _check_abort()
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
            msg_id = 62000

            async def _evaluate_round(deadline: float):
                """Một vòng đọc + cuộn; retry khi execution context chưa sẵn."""
                nonlocal msg_id
                for _ in range(2):
                    _check_abort()
                    if time.monotonic() >= deadline:
                        return None, "deadline"
                    msg_id += 1
                    my_id = msg_id
                    await ws.send(json.dumps({
                        "id": my_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": _FB_SCRAPE_JS,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    }))
                    resp_deadline = min(
                        deadline, time.monotonic() + 7.0
                    )
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
                            else:
                                raise RuntimeError(
                                    f"CDP từ chối đọc tab Facebook: {text}"
                                )
                            break
                        if (msg.get("result") or {}).get("exceptionDetails"):
                            raise RuntimeError(
                                "Lỗi đọc trang Facebook trong tab profile."
                            )
                        value = (
                            (msg.get("result") or {}).get("result") or {}
                        ).get("value")
                        return (value if isinstance(value, dict) else None), "ok"
                    if not not_ready:
                        return None, "timeout"
                    await asyncio.sleep(0.4)
                return None, "not_ready"

            budget = float(timeout or DISCOVERY_DEADLINE_S)
            scan_deadline = time.monotonic() + min(
                DISCOVERY_DEADLINE_S, max(1.0, budget)
            )
            accumulator = PageAccumulator()
            total = None
            end_marker = False
            empty_confirmed = False
            ui_recognized = False
            identity_status = "unknown"
            account_id = None
            login_status = True
            login_lost = False
            nav_error = False
            deadline_hit = False
            max_pages_hit = False
            max_scrolls_hit = False
            stable_rounds = 0
            last_added = False
            last_loading = False
            unrecognized_rounds = 0
            nav_bad_rounds = 0
            round_index = 0
            eval_failures = 0
            last_snap: Dict[str, Any] = {}

            while True:
                _check_abort()
                if time.monotonic() >= scan_deadline:
                    deadline_hit = True
                    break
                if len(accumulator) >= MAX_PAGES:
                    max_pages_hit = True
                    break
                if round_index >= MAX_SCROLL_ROUNDS:
                    max_scrolls_hit = True
                    break

                snap, state = await _evaluate_round(scan_deadline)
                if snap is None:
                    eval_failures += 1
                    if eval_failures >= 3 or state == "timeout":
                        nav_error = True
                        break
                    await asyncio.sleep(0.5)
                    continue
                eval_failures = 0
                round_index += 1
                last_snap = snap

                if snap.get("loggedOut"):
                    login_status = False
                    login_lost = True
                    break

                snap_account = str(snap.get("accountId") or "").strip()
                if re.fullmatch(r"\d{5,}", snap_account or ""):
                    account_id = snap_account
                    identity_status = "verified"
                    if (
                        expected_account_id
                        and str(expected_account_id) != account_id
                    ):
                        break

                url_error = _fb_listing_url_error(snap.get("url"))
                if url_error == "login":
                    login_status = False
                    login_lost = True
                    break
                if url_error:
                    nav_bad_rounds += 1
                    if nav_bad_rounds >= 3:
                        nav_error = True
                        break
                    await asyncio.sleep(0.5)
                    continue
                nav_bad_rounds = 0

                last_loading = bool(snap.get("loading"))
                added = accumulator.add_many(snap.get("pages") or [])
                last_added = added > 0
                ui_recognized = ui_recognized or bool(
                    snap.get("uiRecognized")
                )
                if snap.get("endMarker"):
                    end_marker = True
                if (
                    not accumulator
                    and snap.get("emptyConfirmed")
                    and not last_loading
                ):
                    empty_confirmed = True
                try:
                    snap_total = (
                        int(snap.get("total"))
                        if snap.get("total") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    snap_total = None
                if (
                    snap_total is not None
                    and not last_loading
                    and (snap_total > 0 or empty_confirmed)
                    and total is None
                ):
                    total = snap_total

                if total is not None and len(accumulator) == total:
                    break
                if end_marker:
                    break
                if empty_confirmed:
                    break

                if (
                    not accumulator
                    and not ui_recognized
                    and not last_loading
                ):
                    unrecognized_rounds += 1
                    if unrecognized_rounds >= UI_UNRECOGNIZED_ROUNDS:
                        break
                else:
                    unrecognized_rounds = 0

                if round_index >= MAX_SCROLL_ROUNDS:
                    max_scrolls_hit = True
                    break
                if len(accumulator) >= MAX_PAGES:
                    max_pages_hit = True
                    break

                if round_index == 1:
                    stable_rounds = 0
                elif added == 0 and not last_loading:
                    stable_rounds += 1
                    if stable_rounds >= STABLE_ROUNDS:
                        break
                else:
                    stable_rounds = 0
                await asyncio.sleep(0.7 if last_loading else 0.5)

            stability = (
                "growing"
                if (last_added or last_loading) and round_index > 0
                else "stable"
            )
            result = evaluate_discovery(
                pages=accumulator.pages(),
                total=total,
                end_marker=end_marker,
                empty_confirmed=empty_confirmed,
                ui_recognized=ui_recognized or round_index == 0,
                logged_in=login_status,
                login_lost=login_lost,
                identity_status=identity_status,
                account_id_observed=account_id,
                expected_account_id=expected_account_id,
                nav_error=nav_error,
                stability=stability,
                deadline_hit=deadline_hit,
                max_pages_hit=max_pages_hit,
                max_scrolls_hit=max_scrolls_hit,
                started_at=started_at,
                finished_at=utc_now(),
                collector_version=COLLECTOR_VERSION,
            )
            result["rounds"] = round_index
            result["scroll_rounds"] = round_index
            result["logged_in"] = login_status and not login_lost
            result["url"] = last_snap.get("url")
            return result
    finally:
        TRANSIENT_TARGET_IDS.discard(str(target_id))
        # Tab này do tool mở — luôn đóng lại, kể cả khi scrape lỗi.
        try:
            await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "PUT")
        except Exception:
            try:
                await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "GET")
            except Exception:
                pass


def _fb_url_identity(url: str) -> tuple:
    """(page_id|None, path chuẩn hóa) để kiểm tra navigation đúng page."""
    from urllib.parse import parse_qs, urlparse

    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return (None, "")
    path = (parsed.path or "").rstrip("/").lower()
    page_id = None
    m = re.match(r"^/pages/[^/]+/(\d{5,})", path)
    if m:
        page_id = m.group(1)
    if not page_id:
        qid = (parse_qs(parsed.query).get("id") or [None])[0]
        if qid and re.fullmatch(r"\d{5,}", str(qid)):
            page_id = str(qid)
    if not page_id:
        m = re.search(r"(?:^|/)(\d{5,})$", path) or re.search(r"-(\d{5,})$", path)
        if m:
            page_id = m.group(1)
    return (page_id, path)


def _fb_same_page(actual_url: str, expected_url: str) -> bool:
    actual_id, actual_path = _fb_url_identity(actual_url)
    expected_id, expected_path = _fb_url_identity(expected_url)
    if not actual_path or not expected_path:
        return False
    if actual_id and expected_id:
        return actual_id == expected_id
    return actual_path == expected_path


async def fetch_pages_deep_cdp(
    port: int,
    pages: List[Dict[str, Any]],
    timeout: float = 120.0,
    *,
    should_abort: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """Quét chi tiết từng page: bài gần nhất (feed) + ngày tạo (about).

    Dùng MỘT tab duyệt tuần tự các page thay vì mở N tab — mỗi page tốn
    ~4-8s (2 lần navigate). Trả {page_url: {"times", "created_raw",
    "observed_at", "completed", "parser_version", ...}}. Chỉ nhận metrics
    khi URL tab đã khớp đúng page đích (không đọc DOM của page trước).
    Trang nào lỗi vẫn có entry rỗng để UI hiển thị "không rõ" thay vì treo.
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
    TRANSIENT_TARGET_IDS.add(str(target_id))

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

            async def _navigate_and_evaluate(url: str, deadline_s: float = 8.0):
                """Navigate rồi chỉ nhận metrics khi location đã khớp page đích."""
                nonlocal msg_id
                msg_id += 1
                await ws.send(json.dumps({
                    "id": msg_id,
                    "method": "Page.navigate",
                    "params": {"url": url},
                }))
                end = time.monotonic() + deadline_s
                for _ in range(2):
                    remaining = end - time.monotonic()
                    if remaining <= 0.4:
                        break
                    metrics = await _evaluate(
                        _FB_PAGE_METRICS_JS, deadline_s=min(7.0, remaining)
                    )
                    if metrics and _fb_same_page(metrics.get("url"), url):
                        return metrics
                    await asyncio.sleep(0.3)
                return None

            for page in pages:
                if should_abort and should_abort():
                    raise ScanAborted(
                        "Profile đã dừng trong khi quét chi tiết; "
                        "giữ nguyên dữ liệu cũ."
                    )
                page_url = (page.get("url") or "").strip()
                entry: Dict[str, Any] = {
                    "times": [],
                    "created_raw": None,
                    "last_post_raw": None,
                    "followers_raw": None,
                    "likes_raw": None,
                    "category": None,
                    "observed_page_id": None,
                    "access_state": "ok",
                    "title": None,
                    "observed_at": to_utc_iso(utc_now()),
                    "completed": False,
                    "feed_ready": False,
                    "article_count": 0,
                    "parser_version": PARSER_VERSION,
                }
                if page_url:
                    # 1) Trang chính page: mốc thời gian bài đăng (feed) + follower/like header.
                    main_metrics = await _navigate_and_evaluate(page_url)
                    if main_metrics:
                        entry["times"] = main_metrics.get("times") or []
                        entry["title"] = main_metrics.get("title")
                        entry["followers_raw"] = main_metrics.get("followersRaw")
                        entry["likes_raw"] = main_metrics.get("likesRaw")
                        entry["category"] = main_metrics.get("category")
                        entry["observed_page_id"] = main_metrics.get("observedPageId")
                        entry["access_state"] = main_metrics.get("accessState") or "ok"
                        entry["feed_ready"] = bool(main_metrics.get("feedReady"))
                        entry["article_count"] = main_metrics.get("articleCount") or 0
                        entry["completed"] = bool(main_metrics.get("feedReady"))
                        entry["parser_version"] = (
                            main_metrics.get("parserVersion") or PARSER_VERSION
                        )
                    # 2) Trang about_profile_transparency: nhãn ngày tạo trang.
                    about_url = page_url.rstrip("/") + "/about_profile_transparency"
                    about_metrics = await _navigate_and_evaluate(about_url)
                    if about_metrics and about_metrics.get("createdRaw"):
                        entry["created_raw"] = about_metrics["createdRaw"]
                    elif main_metrics and main_metrics.get("createdRaw"):
                        entry["created_raw"] = main_metrics["createdRaw"]
                    if about_metrics:
                        if not entry["observed_page_id"] and about_metrics.get("observedPageId"):
                            entry["observed_page_id"] = about_metrics["observedPageId"]
                        if not entry["followers_raw"] and about_metrics.get("followersRaw"):
                            entry["followers_raw"] = about_metrics["followersRaw"]
                        if not entry["likes_raw"] and about_metrics.get("likesRaw"):
                            entry["likes_raw"] = about_metrics["likesRaw"]
                results[page_url] = entry
        return results
    finally:
        TRANSIENT_TARGET_IDS.discard(str(target_id))
        try:
            await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "PUT")
        except Exception:
            try:
                await asyncio.to_thread(_http_json, f"/json/close/{target_id}", "GET")
            except Exception:
                pass


def _candidate_from_detail(item: Any, observed_at: datetime.datetime):
    """Một mốc thời gian ứng viên từ deep scan; loại bài ghim/bình luận/gợi ý."""
    if not isinstance(item, dict):
        return None
    if item.get("pinned") or item.get("comment") or item.get("suggested") or item.get("sponsored"):
        return None
    if item.get("own") is False:
        return None
    n = item.get("n")
    unit = item.get("u") or item.get("unit")
    raw = item.get("raw") or item.get("text")
    if n is None or unit is None:
        parsed = parse_relative_label(raw)
        if parsed:
            n, unit, raw = parsed["n"], parsed["u"], parsed["raw"]
    interval = relative_label_interval(n, unit, observed_at)
    if interval is None:
        return None
    unit_key = _UNIT_ALIASES.get(str(unit or "").strip().lower(), str(unit or ""))
    if unit_key == "now":
        raw = raw or "vừa xong"
    elif not raw:
        raw = f"{n} {unit_key}"
    return {
        "min": interval[0],
        "max": interval[1],
        "raw": str(raw)[:120],
        "url": item.get("url"),
        "source": item.get("source") or "page_feed",
    }


def merge_page_details(
    data: Dict[str, Any],
    details: Dict[str, Dict[str, Any]],
    *,
    observed_at: Any = None,
    display_tz: Any = None,
    clock: Any = None,
) -> None:
    """Gắn ngày tạo + khoảng thời gian bài gần nhất vào từng page trong ``data``.

    ``details`` đến từ ``fetch_pages_deep_cdp`` (mỗi entry có observed_at
    riêng). Không tạo timestamp chính xác từ nhãn tương đối; không xóa dữ
    liệu đã ghi nhận khi lần quét mới không đọc được bài (chỉ đánh dấu
    chất lượng và để trạng thái thành unknown).
    """
    tz = resolve_display_timezone(display_tz)
    clock_value = clock() if callable(clock) else clock
    now_dt = _as_utc(clock_value) or utc_now()
    fallback_obs = _as_utc(observed_at)

    for page in data.get("pages") or []:
        if not isinstance(page, dict):
            continue
        info = details.get(page.get("url")) or {}
        if not isinstance(info, dict) or not info:
            page["created_raw"] = None
            page["last_post_raw"] = None
            page["detail_synced"] = False
            page["activity_scan_complete"] = False
            page.update(classify_facebook_page(page, now=now_dt, timezone=tz))
            continue

        page_obs = _as_utc(info.get("observed_at")) or fallback_obs or now_dt
        candidates = []
        for item in info.get("times") or []:
            cand = _candidate_from_detail(item, page_obs)
            if cand is not None:
                candidates.append(cand)
        # Nhãn không thể trỏ tới tương lai — bỏ ứng viên hỏng thay vì nhận.
        candidates = [
            c for c in candidates
            if c["max"] <= now_dt + _FUTURE_TOLERANCE
        ]

        if candidates:
            # Bài mới nhất = ứng viên có cận trên muộn nhất; không mặc định
            # bài đầu tiên trong DOM là bài mới nhất.
            best = max(candidates, key=lambda c: (c["max"], c["min"]))
            page.pop("last_post_at", None)
            page["last_post_at_min"] = to_utc_iso(best["min"])
            page["last_post_at_max"] = to_utc_iso(best["max"])
            page["last_post_observed_at"] = to_utc_iso(page_obs)
            page["last_post_precision"] = "approximate"
            page["last_post_legacy"] = False
            page["last_post_source"] = best["source"]
            page["last_post_url"] = best["url"] or page.get("url")
            page["last_post_raw"] = best["raw"]
            page["last_post_quality"] = "approximate"

        if info.get("created_raw"):
            page["created_raw"] = info.get("created_raw")
        elif "created_raw" not in page:
            page["created_raw"] = None
        created = parse_fb_created_date(page.get("created_raw"))
        if created:
            page["created_date"] = created.isoformat()
        if info.get("followers_raw") or info.get("followers"):
            flw_raw = info.get("followers_raw") or info.get("followers")
            page["followers"] = flw_raw
            page["followers_raw"] = flw_raw
        if info.get("likes_raw") or info.get("likes"):
            lk_raw = info.get("likes_raw") or info.get("likes")
            page["likes"] = lk_raw
            page["likes_raw"] = lk_raw
        if info.get("category"):
            page["category"] = str(info["category"])[:120]
        obs_page_id = str(info.get("observed_page_id") or "").strip()
        if obs_page_id and re.fullmatch(r"\d{5,}", obs_page_id) and not page.get("id"):
            page["id"] = obs_page_id
            page["id_status"] = "verified"
        if info.get("access_state"):
            page["access_state"] = info["access_state"]
        synced = bool(
            candidates
            or info.get("created_raw")
            or info.get("followers_raw")
            or info.get("likes_raw")
            or info.get("title")
            or info.get("feed_ready")
        )
        page["detail_synced"] = synced
        if synced:
            page["detail_synced_at"] = page_obs.astimezone(tz).date().isoformat()
        page["activity_checked_at"] = to_utc_iso(page_obs)
        # Chỉ đủ phạm vi khi feed đã tải VÀ tìm được ít nhất một bài của page.
        page["activity_scan_complete"] = bool(candidates) and bool(
            info.get("completed", True)
        )
        page["parser_version"] = info.get("parser_version") or PARSER_VERSION
        if not candidates and not page.get("last_post_precision"):
            page["last_post_precision"] = "unknown"
        page.update(classify_facebook_page(page, now=now_dt, timezone=tz))
