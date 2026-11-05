"""Facebook DOM & Metrics Audit Tool for Phase 0 Verification.

Tuân thủ kế hoạch FANPAGE_MANAGER_PLAN_2026-11-05 (Giai đoạn 0):
- Khảo sát dữ liệu 5-10 page mẫu thuộc 2-3 profile.
- Đối chiếu các trường dữ liệu trên màn hình Facebook thực tế.
- Kiểm tra tính khả thi của các bộ chọn DOM (VI / EN).
- Đo thời gian điều hướng, đọc DOM và rủi ro chuyển danh tính (Profile Switch).
- Hoàn toàn KHÔNG sửa đổi data/profiles.json hay dữ liệu gốc.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from antidetect.facebook import (
    classify_facebook_page,
    parse_fb_created_date,
    fb_unit_to_days,
)
from antidetect.storage import DATA_DIR, ProfileManager, PROFILES_JSON
from antidetect.launcher import BrowserLauncher


# Danh sách 8 page mẫu tuyển chọn từ 3 profile đại diện cho Phase 0
SAMPLE_PAGES_PHASE0 = [
    # Nhóm 1: Profile 1 (Tài khoản phụ lâu năm, có page cũ > 1 năm và page mới)
    {
        "profile_id": "da7555ad-33d1-49a3-b5ac-af02b39c5a67",
        "profile_name": "Profile 1 - Nguyễn Thành Công(phụ)",
        "page_id": "61576575056385",
        "page_name": "Mê Nhà Đẹp",
        "url": "https://www.facebook.com/61576575056385",
        "category": "Page lâu năm (tạo 2025-08-19, tuổi > 400d)",
        "expected_ready": True,
    },
    {
        "profile_id": "da7555ad-33d1-49a3-b5ac-af02b39c5a67",
        "profile_name": "Profile 1 - Nguyễn Thành Công(phụ)",
        "page_id": "61580355912577",
        "page_name": "Góc Decor Xinh",
        "url": "https://www.facebook.com/61580355912577",
        "category": "Page có bài đăng gần đây (active trong cache)",
        "expected_ready": True,
    },
    {
        "profile_id": "da7555ad-33d1-49a3-b5ac-af02b39c5a67",
        "profile_name": "Profile 1 - Nguyễn Thành Công(phụ)",
        "page_id": "61595653902005",
        "page_name": "Chị Đẹp Khó Tính",
        "url": "https://www.facebook.com/61595653902005",
        "category": "Page mới tạo gần đây (2026-10-26, tuổi ~10d)",
        "expected_ready": False,
    },
    # Nhóm 2: Profile 7 (Farm page chủ đề giải trí / mibobo)
    {
        "profile_id": "f6f8a933-26b6-47e8-9a81-de3ed5033b7c",
        "profile_name": "Profile 7 - Meo Meo",
        "page_id": "61594671061927",
        "page_name": "Tiện Ích Ngôi Nhà",
        "url": "https://www.facebook.com/61594671061927",
        "category": "Page ngâm > 30 ngày (tạo 2026-09-23), chưa có bài",
        "expected_ready": True,
    },
    {
        "profile_id": "f6f8a933-26b6-47e8-9a81-de3ed5033b7c",
        "profile_name": "Profile 7 - Meo Meo",
        "page_id": "61594794845359",
        "page_name": "Super mibobo đáng yêu",
        "url": "https://www.facebook.com/61594794845359",
        "category": "Page có bug regex cũ ('28 mo' = 840d thay vì 28 tháng 9)",
        "expected_ready": True,
    },
    {
        "profile_id": "f6f8a933-26b6-47e8-9a81-de3ed5033b7c",
        "profile_name": "Profile 7 - Meo Meo",
        "page_id": "61595967628603",
        "page_name": "Con bò bèo béo",
        "url": "https://www.facebook.com/61595967628603",
        "category": "Page mới (2026-10-26), đang active gần đây",
        "expected_ready": False,
    },
    # Nhóm 3: Profile 5 (Nhiều page nhất - 12 page, chủ đề unboxing / decor)
    {
        "profile_id": "caec2c73-f27a-49c8-be1f-43aa8a76ce84",
        "profile_name": "Profile 5 - Lê Thị Quyên 2",
        "page_id": "61593767531478",
        "page_name": "Tạp Hoá Unboxing",
        "url": "https://www.facebook.com/61593767531478",
        "category": "Page ngâm > 90 ngày (tạo 2026-08-01), chưa sync bài",
        "expected_ready": True,
    },
    {
        "profile_id": "caec2c73-f27a-49c8-be1f-43aa8a76ce84",
        "profile_name": "Profile 5 - Lê Thị Quyên 2",
        "page_id": "61595844003942",
        "page_name": "Page của Mibo",
        "url": "https://www.facebook.com/61595844003942",
        "category": "Page mới (2026-10-26), trạng thái active",
        "expected_ready": False,
    },
]


def audit_cache_data() -> Dict[str, Any]:
    """Phân tích 58 page hiện có trong profiles.json và phát hiện các khiếm khuyết."""
    pm = ProfileManager()
    profiles = pm.list_profiles()

    report = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total_profiles": len(profiles),
        "profiles_with_pages": 0,
        "total_page_entries": 0,
        "unique_page_ids": 0,
        "anomalies": [],
        "readiness_summary": {"ready_age_30d": 0, "not_ready": 0, "unknown_age": 0},
        "activity_summary": {"active": 0, "stopped": 0, "unknown": 0},
    }

    seen_ids = set()
    for p in profiles:
        pages = getattr(p, "facebook_pages", []) or []
        if not pages:
            continue
        report["profiles_with_pages"] += 1
        for page in pages:
            report["total_page_entries"] += 1
            pid = page.get("id")
            if pid:
                seen_ids.add(pid)

            # Phân tích tuổi & ready
            c_date = page.get("created_date")
            ready = page.get("ready")
            if c_date:
                if ready:
                    report["readiness_summary"]["ready_age_30d"] += 1
                else:
                    report["readiness_summary"]["not_ready"] += 1
            else:
                report["readiness_summary"]["unknown_age"] += 1

            # Phân tích trạng thái hoạt động
            status = page.get("status")
            if status == "active":
                report["activity_summary"]["active"] += 1
            elif status == "stopped":
                report["activity_summary"]["stopped"] += 1
            else:
                report["activity_summary"]["unknown"] += 1

            # Kiểm tra lỗi parser 'X mo' từ ngày tháng
            lp_raw = page.get("last_post_raw") or ""
            c_raw = page.get("created_raw") or ""
            if "mo" in lp_raw:
                # Phát hiện lỗi regex nhầm ngày trong tháng thành X tháng trước
                m = re.match(r"^(\d+)\s+mo$", lp_raw)
                if m and c_raw and m.group(1) in c_raw:
                    report["anomalies"].append({
                        "profile_name": p.name,
                        "page_id": pid,
                        "page_name": page.get("name"),
                        "created_raw": c_raw,
                        "last_post_raw": lp_raw,
                        "last_post_days": page.get("last_post_days"),
                        "issue": "Regex nhầm ngày tạo thành 'X tháng trước' (False stopped status)",
                    })

    report["unique_page_ids"] = len(seen_ids)
    return report


# Script trích xuất DOM chuyên sâu chạy trong tab Facebook
_FACEBOOK_PROBE_JS = r"""
(() => {
    const results = {
        url: location.href,
        title: document.title,
        lang: document.documentElement.lang || navigator.language,
        timestamp: new Date().toISOString(),
        indicators: {},
        metrics: {},
        actions: [],
        raw_text_snippets: []
    };

    // 1. Kiểm tra trạng thái đăng nhập
    const isLoginPage = location.pathname.includes('/login') || !!document.querySelector('input[type="password"]');
    results.indicators.is_logged_in = !isLoginPage;

    // 2. Kiểm tra loại giao diện
    const bodyText = document.body ? document.body.innerText : '';
    results.indicators.is_profile_switch_prompt = bodyText.includes('Chuyển trang') 
        || bodyText.includes('Switch profile')
        || bodyText.includes('Bạn cần chuyển trang để quản lý');

    // 3. Quét các action buttons (Manage, Switch, Follow, Liked...)
    const buttons = Array.from(document.querySelectorAll('div[role="button"], button, a[role="button"]'));
    for (const b of buttons) {
        const text = (b.innerText || b.getAttribute('aria-label') || '').trim();
        if (text && text.length < 40) {
            if (/quản lý|manage|chuyển ngay|switch now|xem trang tổng quan|view dashboard/i.test(text)) {
                results.actions.push({ type: 'admin_control', label: text });
            } else if (/theo dõi|follow|đã thích|liked|thích|like/i.test(text)) {
                results.actions.push({ type: 'page_action', label: text });
            }
        }
    }

    // 4. Tìm số Follower & Like
    // Phổ biến ở phần header của New Page Experience: "X người theo dõi • Y người đang theo dõi"
    // hoặc "X followers • Y following"
    const followerRegexVi = /([\d.,\s]+[KkMmBbTt]?)\s+(người\s+theo\s+dõi|lượt\s+thích|người\s+thích)/gi;
    const followerRegexEn = /([\d.,\s]+[KkMmBbTt]?)\s+(followers?|likes?)/gi;
    
    const findMatches = (regex, type) => {
        let m;
        while ((m = regex.exec(bodyText)) !== null) {
            results.metrics[type + '_' + m[2].toLowerCase()] = {
                raw_value: m[1].trim(),
                label: m[2].trim(),
                context: bodyText.substring(Math.max(0, m.index - 20), Math.min(bodyText.length, m.index + 50)).replace(/\n/g, ' ')
            };
        }
    };
    findMatches(followerRegexVi, 'vi');
    findMatches(followerRegexEn, 'en');

    // 5. Kiểm tra khu vực Feed / Bài viết gần nhất
    const feed = document.querySelector('div[role="feed"]');
    if (feed) {
        results.indicators.has_feed = true;
        const articles = feed.querySelectorAll('div[role="article"]');
        results.indicators.article_count = articles.length;
        if (articles.length > 0) {
            const firstArticle = articles[0];
            results.metrics.first_post = {
                text_preview: (firstArticle.innerText || '').slice(0, 150).replace(/\n/g, ' '),
                aria_labels: Array.from(firstArticle.querySelectorAll('[aria-label]')).map(el => el.getAttribute('aria-label')).slice(0, 5)
            };
        }
    } else {
        results.indicators.has_feed = false;
    }

    // 6. Kiểm tra Professional Dashboard nếu URL chứa
    if (location.pathname.includes('professional_dashboard') || location.pathname.includes('insights')) {
        results.indicators.is_dashboard = true;
        // Quét các thẻ chỉ số tổng quan (Reach, Engagement, Followers, Net followers)
        const statCards = [];
        const cardHeaders = Array.from(document.querySelectorAll('h3, h4, span, div'))
            .filter(el => /lượt tiếp cận|tương tác|người theo dõi|reach|engagement|content interactions/i.test(el.innerText || ''));
        for (const ch of cardHeaders.slice(0, 10)) {
            statCards.push((ch.innerText || '').slice(0, 100).replace(/\n/g, ' '));
        }
        results.metrics.dashboard_cards = statCards;
    }

    return results;
})()
"""


async def probe_page_cdp(port: int, url: str, timeout: float = 20.0) -> Dict[str, Any]:
    """Mở tab phụ qua CDP, điều hướng tới URL và chạy _FACEBOOK_PROBE_JS."""
    import websockets
    import urllib.request
    from urllib.parse import quote

    def _http(path: str, method: str = "GET") -> Dict[str, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            try:
                return json.loads(resp.read().decode("utf-8", "replace"))
            except Exception:
                return {}

    t0 = time.monotonic()
    new_url = f"/json/new?{quote(url, safe=':/?&=#%')}"
    tab_info = await asyncio.to_thread(_http, new_url, "PUT")
    target_id = (tab_info or {}).get("id")
    if not target_id:
        tab_info = await asyncio.to_thread(_http, new_url, "GET")
        target_id = (tab_info or {}).get("id")
    if not target_id:
        raise RuntimeError("Chrome CDP không tạo được tab mới.")

    try:
        # Chờ WebSocket Debugger URL
        ws_url = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not ws_url:
            targets = await asyncio.to_thread(_http, "/json/list")
            for t in targets or []:
                if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if not ws_url:
                await asyncio.sleep(0.3)

        if not ws_url:
            raise RuntimeError("Không lấy được WebSocket CDP cho tab.")

        # Kết nối WebSocket
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            # Đợi load cơ bản
            await asyncio.sleep(2.5)
            # Evaluate script
            eval_req = {
                "id": 99001,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": _FACEBOOK_PROBE_JS,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            }
            await ws.send(json.dumps(eval_req))
            resp_deadline = time.monotonic() + min(timeout, 15.0)
            probe_result = None
            while time.monotonic() < resp_deadline:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw_msg)
                if msg.get("id") == 99001:
                    probe_result = ((msg.get("result") or {}).get("result") or {}).get("value")
                    break

            load_time = round(time.monotonic() - t0, 2)
            return {
                "success": True,
                "load_time_seconds": load_time,
                "data": probe_result or {},
            }
    finally:
        # Đóng tab phụ ngay sau khi probe xong
        try:
            await asyncio.to_thread(_http, f"/json/close/{target_id}", "PUT")
        except Exception:
            try:
                await asyncio.to_thread(_http, f"/json/close/{target_id}", "GET")
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Facebook DOM Probe for Phase 0 Verification")
    parser.add_argument("--inspect-cache", action="store_true", help="Phân tích dữ liệu cache 58 fanpage hiện có")
    parser.add_argument("--list-sample", action="store_true", help="Liệt kê 8 page mẫu tuyển chọn từ 3 profile")
    parser.add_argument("--probe-profile", type=str, help="Chạy probe trên profile đang mở (truyền ID hoặc tên profile)")
    parser.add_argument("--page-id", type=str, help="ID của page cần probe cụ thể (mặc định lấy page mẫu của profile)")
    args = parser.parse_args()

    if args.list_sample or (not args.inspect_cache and not args.probe_profile):
        print("\n" + "=" * 70)
        print("DANH SÁCH 8 PAGE MẪU TUYỂN CHỌN CHO GIAI ĐOẠN 0 (3 PROFILES ĐẠI DIỆN)")
        print("=" * 70)
        for i, item in enumerate(SAMPLE_PAGES_PHASE0, 1):
            print(f"\n{i}. [{item['page_name']}] - ID: {item['page_id']}")
            print(f"   Profile: {item['profile_name']} ({item['profile_id']})")
            print(f"   URL: {item['url']}")
            print(f"   Nhóm mẫu: {item['category']}")
            print(f"   Kỳ vọng tuổi: {'≥ 30 ngày (Đủ ngưỡng nội bộ)' if item['expected_ready'] else '< 30 ngày'}")

    if args.inspect_cache:
        print("\n" + "=" * 70)
        print("KẾT QUẢ KHẢO SÁT 58 FANPAGE TRONG CACHE HIỆN TẠI (DATA AUDIT)")
        print("=" * 70)
        res = audit_cache_data()
        print(f"- Tổng số profiles: {res['total_profiles']} (Có fanpage: {res['profiles_with_pages']})")
        print(f"- Tổng số fanpage entries: {res['total_page_entries']} (Unique IDs: {res['unique_page_ids']})")
        print(f"- Trạng thái tuổi page nội bộ:")
        print(f"  + Đủ ngưỡng tuổi (>=30 ngày): {res['readiness_summary']['ready_age_30d']}")
        print(f"  + Chưa đủ ngưỡng tuổi: {res['readiness_summary']['not_ready']}")
        print(f"  + Chưa xác định ngày tạo: {res['readiness_summary']['unknown_age']}")
        print(f"- Trạng thái hoạt động (dựa trên parser cũ):")
        print(f"  + Active (bài <= 7 ngày): {res['activity_summary']['active']}")
        print(f"  + Stopped (bài > 7 ngày): {res['activity_summary']['stopped']}")
        print(f"  + Unknown (chưa đọc được bài): {res['activity_summary']['unknown']}")

        print(f"\n[!] CÁC KHIẾM KHUYẾT / ANOMALIES PHÁT HIỆN TRONG CACHE CŨ ({len(res['anomalies'])} trường hợp):")
        for ano in res["anomalies"][:8]:
            print(f"  * Page: {ano['page_name']} ({ano['page_id']}) - Profile: {ano['profile_name']}")
            print(f"    Ngày tạo raw: '{ano['created_raw']}' => Bị parser cũ đọc thành bài đăng: '{ano['last_post_raw']}' ({ano['last_post_days']} ngày)")
            print(f"    Nguyên nhân: {ano['issue']}")

    if args.probe_profile:
        pm = ProfileManager()
        launcher = BrowserLauncher(pm)
        profiles = pm.list_profiles()
        matched = [p for p in profiles if args.probe_profile in (p.id, p.name) or args.probe_profile.lower() in p.name.lower()]
        if not matched:
            print(f"[-] Không tìm thấy profile nào khớp với: {args.probe_profile}")
            return
        target_prof = matched[0]
        print(f"\n[*] Profile đã chọn: {target_prof.name} ({target_prof.id})")
        is_running = launcher.is_running(target_prof.id)
        port = launcher.active_ports.get(target_prof.id)

        if not is_running or not port:
            print(f"[-] Profile hiện ĐANG DỪNG (stopped).")
            print(f"[*] Để đảm bảo an toàn tuyệt đối, vui lòng mở profile này từ Dashboard hoặc dùng lệnh:")
            print(f"    python -m antidetect.cli start {target_prof.id}")
            print(f"    Sau đó chạy lại script probe để kiểm tra trực tiếp tab đang chạy.")
            return

        print(f"[+] Profile đang chạy trên CDP Port: {port}")
        # Tìm page để probe
        target_page_url = None
        if args.page_id:
            target_page_url = f"https://www.facebook.com/{args.page_id}"
        else:
            # Lấy 1 page từ sample list thuộc profile này
            for s in SAMPLE_PAGES_PHASE0:
                if s["profile_id"] == target_prof.id:
                    target_page_url = s["url"]
                    break
            if not target_page_url and target_prof.facebook_pages:
                target_page_url = target_prof.facebook_pages[0].get("url")

        if not target_page_url:
            print("[-] Profile không có fanpage nào để probe.")
            return

        print(f"[*] Tiến hành probe an toàn URL: {target_page_url}")
        res = asyncio.run(probe_page_cdp(port, target_page_url))
        print(f"[+] Probe hoàn tất trong {res['load_time_seconds']}s:")
        print(json.dumps(res["data"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
