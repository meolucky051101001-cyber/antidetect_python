import argparse
import getpass
import sys
import os
import json

# Add root folder to sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.models import ProxyConfig
from antidetect.storage import ProfileManager
from antidetect.launcher import BrowserLauncher
from antidetect.fingerprint import generate_fingerprint
from antidetect.applog import setup_logging
from antidetect.launch_gate import (
    evaluate_profile as evaluate_launch_gate,
    evaluate_profiles as evaluate_launch_gates,
    get_mode as get_launch_gate_mode,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _resolve_gate_browser_paths(launcher, profiles):
    return {p.id: launcher._resolve_browser_path(p) for p in profiles}


def _print_gate_block(gate):
    print("Cổng fingerprint chặn khởi chạy:")
    for issue in gate.issues:
        if issue.get("severity") == "critical":
            print(f" - {issue.get('title')}: {issue.get('detail')}")
    print(
        "Sửa trong Fingerprint Health Center hoặc thêm --force "
        "để mở cho riêng lần này."
    )


def _apply_secure_proxy_password(proxy_cfg):
    """Lấy mật khẩu proxy mà không bắt người dùng gõ vào argv.

    Password trong argv bị lộ qua process list và lịch sử shell, nên URL chỉ
    được chứa username. Password lấy từ ANTIDETECT_PROXY_PASSWORD hoặc prompt
    khi chạy tương tác.
    """
    if proxy_cfg.password:
        print(
            "Lỗi: không nhập mật khẩu proxy trực tiếp trong --proxy vì sẽ lộ "
            "qua process list/lịch sử shell. Dùng dạng protocol://user@host:port "
            "rồi đặt ANTIDETECT_PROXY_PASSWORD hoặc nhập khi được hỏi.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not proxy_cfg.username:
        return
    password = os.environ.get("ANTIDETECT_PROXY_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            print(
                "Lỗi: cần mật khẩu proxy nhưng không có terminal để nhập. "
                "Đặt biến môi trường ANTIDETECT_PROXY_PASSWORD.",
                file=sys.stderr,
            )
            sys.exit(1)
        password = getpass.getpass(f"Mật khẩu proxy cho {proxy_cfg.username}: ")
    proxy_cfg.password = password or None


def main():
    setup_logging(console=True)
    parser = argparse.ArgumentParser(
        description="Antidetect Python CLI — Điều khiển trình duyệt Antidetect qua dòng lệnh"
    )
    subparsers = parser.add_subparsers(dest="command", help="Lệnh cần thực thi")

    # Lệnh: list
    subparsers.add_parser("list", help="Liệt kê tất cả profile")

    # Lệnh: create
    create_parser = subparsers.add_parser("create", help="Tạo profile mới")
    create_parser.add_argument("--name", default="Profile Mới", help="Tên profile")
    create_parser.add_argument(
        "--os",
        default="windows",
        choices=["windows", "macos", "linux"],
        help="Hệ điều hành giả lập",
    )
    create_parser.add_argument(
        "--proxy",
        help="Proxy dạng protocol://user@host:port hoặc protocol://host:port "
        "(mật khẩu qua ANTIDETECT_PROXY_PASSWORD hoặc prompt, không đặt trong URL)",
    )

    # Lệnh: start
    start_parser = subparsers.add_parser("start", help="Khởi chạy profile")
    start_parser.add_argument("id", help="ID của profile cần khởi chạy")
    start_parser.add_argument("--url", help="Đường dẫn URL cần mở ngay sau khi bật")
    start_parser.add_argument("--detach", action="store_true", help="Chạy profile ở chế độ nền và thoát CLI mà không tắt trình duyệt")
    start_parser.add_argument("--force", action="store_true", help="Bỏ qua cổng fingerprint cho riêng lần mở này")

    # Lệnh: start-all
    start_all_parser = subparsers.add_parser("start-all", help="Khởi chạy toàn bộ profile cùng lúc")
    start_all_parser.add_argument("--delay", type=float, default=0.8, help="Độ trễ (giây) giữa các profile (mặc định: 0.8s)")
    start_all_parser.add_argument("--url", help="Đường dẫn URL cần mở ngay sau khi bật")
    start_all_parser.add_argument(
        "--max-concurrent",
        type=int,
        default=8,
        help="Số profile mở đồng thời tối đa; 0 = không giới hạn (mặc định: 8)",
    )
    start_all_parser.add_argument("--detach", action="store_true", help="Chạy toàn bộ profile ở chế độ nền và thoát CLI mà không tắt trình duyệt")
    start_all_parser.add_argument("--force", action="store_true", help="Bỏ qua cổng fingerprint cho toàn bộ lần mở này")

    # Lệnh: stop
    stop_parser = subparsers.add_parser("stop", help="Dừng profile đang chạy")
    stop_parser.add_argument("id", help="ID của profile cần dừng")

    # Lệnh: stop-all
    subparsers.add_parser("stop-all", help="Dừng toàn bộ các profile đang chạy")

    # Lệnh: delete
    del_parser = subparsers.add_parser("delete", help="Xóa profile")
    del_parser.add_argument("id", help="ID của profile cần xóa")

    # Lệnh: reset-fp
    rfp_parser = subparsers.add_parser("reset-fp", help="Sinh ngẫu nhiên bộ dấu vân tay mới cho profile")
    rfp_parser.add_argument("id", help="ID của profile")
    rfp_parser.add_argument("--os", choices=["windows", "macos", "linux"], help="Hệ điều hành mới (tùy chọn)")

    # Lệnh: set-proxy
    proxy_parser = subparsers.add_parser("set-proxy", help="Cài đặt hoặc cập nhật proxy cho profile")
    proxy_parser.add_argument("id", help="ID của profile")
    proxy_parser.add_argument(
        "--proxy",
        help="Proxy dạng protocol://user@host:port hoặc protocol://host:port "
        "(mật khẩu qua ANTIDETECT_PROXY_PASSWORD hoặc prompt, không đặt trong URL)",
    )
    proxy_parser.add_argument("--disable", action="store_true", help="Tắt proxy cho profile này")

    args = parser.parse_args()

    manager = ProfileManager()
    launcher = BrowserLauncher(manager)

    if args.command == "list":
        profiles = manager.list_profiles()
        print(f"\n{'ID':<38} {'Tên':<20} {'Hệ điều hành':<12} {'Trạng thái'}")
        print("-" * 80)
        health_labels = {
            "starting": "đang khởi tạo",
            "healthy": "khỏe",
            "degraded": "mất kết nối",
            "recovery_required": "cần khởi chạy lại",
            "stopping": "đang dừng",
        }
        for p in profiles:
            if launcher.is_running(p.id):
                health = launcher.cdp_health_snapshot(p.id, running=True)
                state = health.get("state") or "unknown"
                detail = health_labels.get(state, state)
                reason = health.get("reason")
                if reason and state != "healthy":
                    detail += f" · {reason}"
                status = f"Đang chạy · CDP {detail}"
            else:
                status = "Đã dừng"
            print(f"{p.id:<38} {p.name:<20} {p.fingerprint.os:<12} {status}")
        print(f"\nTổng cộng: {len(profiles)} profile.\n")

    elif args.command == "create":
        proxy_cfg = ProxyConfig()
        if args.proxy:
            # Parse proxy — tự thêm http:// nếu thiếu scheme
            try:
                from urllib.parse import urlparse

                proxy_str = args.proxy
                if "://" not in proxy_str:
                    proxy_str = f"http://{proxy_str}"
                parsed = urlparse(proxy_str)
                proxy_cfg.enabled = True
                proxy_cfg.protocol = parsed.scheme or "http"
                proxy_cfg.host = parsed.hostname or ""
                proxy_cfg.port = parsed.port or 8080
                proxy_cfg.username = parsed.username
                proxy_cfg.password = parsed.password
            except Exception as e:
                print(f"Lỗi phân tích cú pháp proxy: {e}")
                sys.exit(1)
            _apply_secure_proxy_password(proxy_cfg)

        fp = generate_fingerprint(target_os=args.os)
        p = manager.create_profile(
            name=args.name,
            target_os=args.os,
            proxy=proxy_cfg,
            fingerprint=fp,
        )
        print(f"Đã tạo profile thành công! ID: {p.id}")

    elif args.command == "start":
        clean_url = None
        if hasattr(args, "url") and args.url:
            clean_url = args.url.strip()
            u = clean_url.lower()
            if not (u.startswith("http://") or u.startswith("https://")):
                print("Lỗi: URL phải bắt đầu bằng http:// hoặc https://")
                sys.exit(1)
        profile = manager.get_profile(args.id)
        if profile is not None:
            all_profiles = manager.list_profiles()
            gate = evaluate_launch_gate(
                profile,
                all_profiles,
                _resolve_gate_browser_paths(launcher, all_profiles),
                get_launch_gate_mode(),
                force=getattr(args, "force", False),
            )
            if not gate.allowed:
                _print_gate_block(gate)
                sys.exit(2)
            if gate.checked and gate.status != "healthy":
                print(f"Cổng fingerprint: {gate.message}")
        res = launcher.launch(args.id, open_url=clean_url)
        if res.get("success"):
            print(f"Khởi chạy profile thành công!")
            print(f"Remote Debugging Port: {res.get('port')}")
            print(f"CDP Endpoint: {res.get('ws_endpoint')}")
            if getattr(args, "detach", False):
                if res.get("launched_now"):
                    launcher.mark_detached(args.id)
                    print(
                        "CẢNH BÁO: phiên detached không còn CDP — API/dashboard "
                        "sẽ yêu cầu dừng và mở lại profile trước khi thao tác."
                    )
                launcher.unregister_cleanup()
                print("Profile đang chạy ở chế độ nền (detached).")
            else:
                print(f"Nhấn Ctrl+C để dừng profile và thoát.")
                # Giữ tiến trình chạy để CDP daemon thread không bị kill
                try:
                    import time as _time
                    while launcher.is_running(args.id):
                        _time.sleep(1)
                    print(f"Trình duyệt đã đóng.")
                except KeyboardInterrupt:
                    print(f"\nĐang dừng profile...")
                    launcher.stop(args.id)
                    print(f"Đã dừng profile thành công.")
        else:
            code = res.get("code")
            suffix = f" [{code}]" if code else ""
            print(f"Lỗi khởi chạy: {res.get('error')}{suffix}")
            sys.exit(1)

    elif args.command == "start-all":
        clean_url = None
        if hasattr(args, "url") and args.url:
            clean_url = args.url.strip()
            u = clean_url.lower()
            if not (u.startswith("http://") or u.startswith("https://")):
                print("Lỗi: URL phải bắt đầu bằng http:// hoặc https://")
                sys.exit(1)
        profiles = manager.list_profiles()
        gate_mode = get_launch_gate_mode()
        blocked = []
        launch_ids = None
        if gate_mode != "off" and not getattr(args, "force", False):
            to_start = [p for p in profiles if not launcher.is_running(p.id)]
            gates = evaluate_launch_gates(
                to_start,
                _resolve_gate_browser_paths(launcher, profiles),
                gate_mode,
            )
            blocked = [result for result in gates.values() if not result.allowed]
            if to_start and len(blocked) == len(to_start):
                for gate in blocked:
                    print(
                        f" - {gate.profile_name}: "
                        f"{gate.critical_count} lỗi nghiêm trọng"
                    )
                print(
                    "Toàn bộ profile cần mở đều bị cổng fingerprint chặn. "
                    "Sửa trong Fingerprint Health Center hoặc thêm --force."
                )
                sys.exit(2)
            if blocked:
                blocked_ids = {result.profile_id for result in blocked}
                launch_ids = [p.id for p in profiles if p.id not in blocked_ids]
                for gate in blocked:
                    print(f" - Bỏ qua {gate.profile_name}: {gate.message}")
        print("Đang khởi chạy toàn bộ profile...")
        res = launcher.launch_all(
            delay=args.delay,
            open_url=clean_url,
            max_concurrent=args.max_concurrent,
            profile_ids=launch_ids,
        )
        print(f"Đã kích hoạt {len(res)} profile:")
        running_ids = []
        launched_now_ids = []
        for r in res:
            sub_res = r["result"]
            health_state = (sub_res.get("cdp_health") or {}).get("state")
            cdp_failed = (
                sub_res.get("success")
                and (sub_res.get("cdp_ready") is False
                     or (health_state and health_state != "healthy"))
            )
            if sub_res.get("success") and not cdp_failed:
                status_text = "Thành công"
            elif sub_res.get("success"):
                status_text = (
                    f"Thất bại CDP ({sub_res.get('code') or health_state})"
                )
            else:
                status_text = f"Thất bại ({sub_res.get('error')})"
            print(f" - {r['name']} ({r['profile_id'][:8]}...): {status_text}")
            if sub_res.get("success"):
                running_ids.append(r["profile_id"])
                if sub_res.get("launched_now"):
                    launched_now_ids.append(r["profile_id"])

        if getattr(args, "detach", False):
            for profile_id in launched_now_ids:
                launcher.mark_detached(profile_id)
            if launched_now_ids:
                print(
                    "CẢNH BÁO: các profile detached không còn CDP — API/dashboard "
                    "sẽ yêu cầu dừng và mở lại trước khi thao tác."
                )
            launcher.unregister_cleanup()
            print("\nToàn bộ profile đang chạy ở chế độ nền (detached).")
        elif running_ids:
            print(f"\n{len(running_ids)} profile đang chạy. Nhấn Ctrl+C để dừng toàn bộ profile và thoát.")
            try:
                import time as _time
                while any(launcher.is_running(pid) for pid in running_ids):
                    _time.sleep(1)
                print("Toàn bộ trình duyệt đã đóng.")
            except KeyboardInterrupt:
                print("\nĐang dừng toàn bộ profile...")
                launcher.stop_all()
                print("Đã dừng toàn bộ profile thành công.")

    elif args.command == "stop":
        res = launcher.stop(args.id)
        print(res.get("message", res.get("error", "Đã dừng")))
        if not res.get("success"):
            sys.exit(1)

    elif args.command == "stop-all":
        res = launcher.stop_all()
        print(res.get("message", "Đã dừng toàn bộ các profile đang chạy."))
        if not res.get("success"):
            print("Profile chưa dừng được: " + ", ".join(res.get("failed_profile_ids", [])))
            sys.exit(1)

    elif args.command == "delete":
        from antidetect.storage import ProfileDeletionError
        # Giống API web: dừng browser trước khi xóa — nếu không Chrome giữ
        # khóa user_data và delete fail với ProfileDeletionError.
        stopped = launcher.stop(args.id)
        if not stopped.get("success"):
            print(stopped.get("error", "Chưa dừng được profile."))
            sys.exit(1)
        try:
            deleted = manager.delete_profile(args.id)
        except ProfileDeletionError as e:
            print(str(e))
            sys.exit(1)
        if deleted:
            print(f"Đã xóa profile {args.id} thành công.")
        else:
            print(f"Không tìm thấy profile {args.id}.")
            sys.exit(1)

    elif args.command == "reset-fp":
        p = manager.get_profile(args.id)
        if not p:
            print(f"Không tìm thấy profile {args.id}.")
            sys.exit(1)
        os_to_use = args.os or p.fingerprint.os
        new_fp = generate_fingerprint(
            target_os=os_to_use,
            timezone=p.fingerprint.timezone,
            locale=p.fingerprint.locale,
            latitude=p.fingerprint.latitude if p.fingerprint.latitude is not None else 21.0285,
            longitude=p.fingerprint.longitude if p.fingerprint.longitude is not None else 105.8542
        )
        p.fingerprint = new_fp
        manager.update_profile(p)
        print(f"Đã đổi dấu vân tay mới cho profile '{p.name}'!")
        print(f"- OS: {new_fp.os}")
        print(f"- Màn hình: {new_fp.screen_width}x{new_fp.screen_height}")
        print(f"- Canvas Seed: #{new_fp.canvas_noise_seed}")
        print(f"- GPU: {new_fp.webgl_renderer}")

    elif args.command == "set-proxy":
        p = manager.get_profile(args.id)
        if not p:
            print(f"Không tìm thấy profile {args.id}.")
            sys.exit(1)
        if args.disable:
            p.proxy.enabled = False
            manager.update_profile(p)
            print(f"Đã tắt proxy cho profile '{p.name}'.")
        elif args.proxy:
            from urllib.parse import urlparse
            proxy_str = args.proxy
            if "://" not in proxy_str:
                proxy_str = f"http://{proxy_str}"
            parsed = urlparse(proxy_str)
            p.proxy.enabled = True
            p.proxy.protocol = parsed.scheme or "http"
            p.proxy.host = parsed.hostname or ""
            p.proxy.port = parsed.port or 8080
            p.proxy.username = parsed.username
            # Secret cũ có thể đã được get_profile nạp sẵn; xóa trước để helper
            # không nhầm nó là mật khẩu vừa nhập trong URL. Nếu URL có mật khẩu
            # thì giữ lại để helper từ chối (argv đã lộ).
            p.proxy.password = parsed.password or None
            _apply_secure_proxy_password(p.proxy)
            manager.update_profile(p)
            print(f"Đã cập nhật proxy cho profile '{p.name}': {p.proxy.to_display_url()}")
        else:
            print("Vui lòng cung cấp --proxy <url> hoặc --disable.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
