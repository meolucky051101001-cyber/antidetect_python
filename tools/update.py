"""Cập nhật/rollback có kiểm soát cho bản cài Antidetect.

Không có auto-update ngầm: operator chạy CLI này, release phải được ký, hash
từng file được kiểm tra, server phải tắt (hoặc dùng --stop-server), và bản cũ
được backup trước khi ghi.

Ví dụ:
  python tools/update.py status
  python tools/update.py verify --zip dist/antidetect-python-1.1.0.zip
  python tools/update.py apply  --zip dist/antidetect-python-1.1.0.zip --yes
  python tools/update.py rollback --yes
"""
import argparse
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect import updater  # noqa: E402


def _default_public_key() -> str:
    return os.path.join(BASE_DIR, "update_public_key.pem")


def _public_key(args) -> str:
    if args.public_key:
        return args.public_key
    from_env = os.environ.get("ANTIDETECT_UPDATE_PUBLIC_KEY")
    if from_env:
        return from_env
    default = _default_public_key()
    if os.path.isfile(default):
        return default
    return ""


def _print_server(server) -> None:
    if server:
        print(
            f"Máy chủ:   ĐANG CHẠY (port {server.get('port')}, "
            f"pid {server.get('pid')})"
        )
    else:
        print("Máy chủ:   không chạy")


def cmd_status(_args) -> int:
    updates_dir, state_path = updater.resolve_update_paths(BASE_DIR)
    installed = updater.read_installed_version(BASE_DIR)
    print(f"Thư mục cài:   {BASE_DIR}")
    print(f"Version:       {installed}")
    print(f"Updates dir:   {updates_dir}")
    try:
        state = updater.read_update_state(state_path)
    except updater.UpdateError as exc:
        print(f"State:         HỎNG ({exc})")
        return 1
    if state:
        print(f"State version: {state.get('current_version')}")
        updated = state.get("updated_at")
        if updated:
            try:
                updated = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(int(updated))
                )
            except (TypeError, ValueError, OSError):
                pass
        print(f"Cập nhật lúc:  {updated}")
        rollback = state.get("rollback") or {}
        if rollback.get("backup_dir"):
            print(f"Rollback sẵn:  {rollback['backup_dir']}")
    else:
        print("State:         chưa có (chưa từng update qua CLI)")
    _print_server(updater.detect_running_server())

    backups_root = os.path.join(updates_dir, "backups")
    if os.path.isdir(backups_root):
        entries = [
            name for name in sorted(os.listdir(backups_root), reverse=True)
            if os.path.isfile(
                os.path.join(backups_root, name, "backup.json")
            )
        ]
        print(f"Backups:       {len(entries)}")
        for name in entries[:5]:
            print(f"  - {name}")
    return 0


def cmd_verify(args) -> int:
    key = _public_key(args)
    if not key and not args.allow_unsigned:
        raise updater.UpdateError(
            "Chưa có public key. Dùng --public-key, đặt "
            "ANTIDETECT_UPDATE_PUBLIC_KEY, hoặc thêm update_public_key.pem "
            "cạnh run.py."
        )
    result = updater.verify_release(
        args.zip, public_key=key or None, allow_unsigned=args.allow_unsigned
    )
    manifest = result["manifest"]
    installed = updater.read_installed_version(BASE_DIR)
    target = manifest["version"]
    if updater.version_tuple(target) > updater.version_tuple(installed):
        direction = "nâng cấp"
    elif updater.version_tuple(target) < updater.version_tuple(installed):
        direction = "HẠ CẤP (cần --allow-downgrade khi apply)"
    else:
        direction = "cài đè cùng version"
    print(f"Release:   {result['zip']}")
    print(f"Version:   {installed} -> {target} ({direction})")
    print(f"Chữ ký:    {result['signature']}")
    print(f"Manifest:  {result['manifest_sha256']}")
    print(f"Files:     {len(manifest['files'])}")
    print(f"Delete:    {len(manifest['delete'])}")
    if manifest["delete"]:
        print("  " + ", ".join(manifest["delete"][:10]))
    _print_server(updater.detect_running_server())
    print("Kết quả:   HỢP LỆ (chưa ghi gì).")
    return 0


def _print_recovery(bootstrap) -> None:
    print("Phục hồi độc lập nếu cập nhật gián đoạn: " + bootstrap["instructions_path"], flush=True)
    print(bootstrap["command_powershell"], flush=True)


def cmd_apply(args) -> int:
    if not args.dry_run and not args.yes:
        raise updater.UpdateError(
            "Chưa xác nhận. Chạy lại với --yes để thực sự cập nhật "
            "(hoặc --dry-run để xem trước)."
        )
    key = _public_key(args)
    if not key and not args.allow_unsigned:
        raise updater.UpdateError(
            "Chưa có public key. Dùng --public-key, đặt "
            "ANTIDETECT_UPDATE_PUBLIC_KEY, hoặc thêm update_public_key.pem "
            "cạnh run.py."
        )
    result = updater.apply_update(
        args.zip,
        BASE_DIR,
        public_key=key or None,
        allow_unsigned=args.allow_unsigned,
        allow_downgrade=args.allow_downgrade,
        require_authenticode=args.require_authenticode,
        stop_server=args.stop_server,
        dry_run=args.dry_run,
        recovery_notify=_print_recovery,
    )
    if result.get("dry_run"):
        print(f"Dry-run:  {result['from_version']} -> {result['to_version']}")
        print(f"Chữ ký:   {result['signature']}")
        print(f"Files:    {len(result['files'])}")
        print(f"Delete:   {len(result['delete'])}")
        _print_server(result.get("server"))
        print("Chưa ghi gì. Bỏ --dry-run và thêm --yes để áp dụng.")
        return 0
    print(
        f"Đã cập nhật {result['from_version']} -> {result['to_version']} "
        f"({len(result['files_written'])} file)."
    )
    print(f"Chữ ký:       {result['signature']}")
    print(f"Backup:       {result['backup_dir']}")
    print(f"State:        {result['state_path']}")
    if result.get("server_stopped"):
        print("Máy chủ:      đã dừng để cập nhật.")
    print("Khởi động lại: python run.py")
    return 0


def cmd_rollback(args) -> int:
    if not args.yes:
        raise updater.UpdateError("Chưa xác nhận. Chạy lại với --yes để rollback.")
    result = updater.rollback_update(
        BASE_DIR,
        backup_dir=args.backup,
        stop_server=args.stop_server,
        force=args.force,
        recovery_notify=_print_recovery,
    )
    print(
        f"Đã rollback {result['from_version']} -> {result['to_version']}."
    )
    print(f"Khôi phục:    {len(result['restored'])} file")
    print(f"Xoá file mới: {len(result['removed_added'])}")
    if result["kept_modified_added"]:
        print(
            "Giữ lại file mới đã bị sửa (không xoá mất dữ liệu): "
            + ", ".join(result["kept_modified_added"])
        )
    print(f"Backup:       {result['backup_dir']}")
    print("Khởi động lại: python run.py")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cập nhật có kiểm soát (signed upgrade/rollback)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Xem version, state, backup, server")
    status.set_defaults(func=cmd_status)

    verify = sub.add_parser(
        "verify", help="Kiểm tra chữ ký + hash release, không ghi gì"
    )
    verify.add_argument("--zip", required=True, help="Release zip cần kiểm tra")
    verify.add_argument("--public-key", help="Public key PEM hoặc raw base64")
    verify.add_argument(
        "--allow-unsigned", action="store_true", help="Chấp nhận release không ký"
    )
    verify.set_defaults(func=cmd_verify)

    apply_cmd = sub.add_parser("apply", help="Áp dụng release đã ký")
    apply_cmd.add_argument("--zip", required=True, help="Release zip")
    apply_cmd.add_argument("--public-key", help="Public key PEM hoặc raw base64")
    apply_cmd.add_argument(
        "--allow-unsigned", action="store_true", help="Chấp nhận release không ký"
    )
    apply_cmd.add_argument(
        "--allow-downgrade", action="store_true", help="Cho phép hạ version"
    )
    apply_cmd.add_argument(
        "--require-authenticode", action="store_true",
        help="Bắt buộc file PE trong payload có Authenticode hợp lệ",
    )
    apply_cmd.add_argument(
        "--stop-server", action="store_true",
        help="Tự dừng server đang chạy trước khi ghi",
    )
    apply_cmd.add_argument(
        "--dry-run", action="store_true", help="Chỉ xem kế hoạch, không ghi"
    )
    apply_cmd.add_argument(
        "--yes", action="store_true", help="Xác nhận thực sự cập nhật"
    )
    apply_cmd.set_defaults(func=cmd_apply)

    rollback = sub.add_parser("rollback", help="Khôi phục bản trước từ backup")
    rollback.add_argument(
        "--backup", help="Thư mục backup cụ thể (mặc định theo update_state.json)"
    )
    rollback.add_argument(
        "--stop-server", action="store_true",
        help="Tự dừng server đang chạy trước khi ghi",
    )
    rollback.add_argument(
        "--force", action="store_true",
        help="Cho phép rollback dù version hiện tại khác bản đã cập nhật",
    )
    rollback.add_argument("--yes", action="store_true", help="Xác nhận rollback")
    rollback.set_defaults(func=cmd_rollback)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except updater.UpdateError as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nĐã hủy.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
