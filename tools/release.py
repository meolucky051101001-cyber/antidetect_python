"""Tạo release zip có chữ ký Ed25519 cho updater.

Ví dụ:
  python tools/release.py keygen --out keys/release_private.pem
  python tools/release.py build --key keys/release_private.pem \
      --previous dist/antidetect-python-1.0.0.zip
  python tools/release.py build --version 1.1.0 --allow-unsigned   # dev only

Private key phải được giữ offline/ngoài repo. Public key được phát hành cho
máy cần cập nhật và truyền qua ``--public-key`` hoặc biến môi trường
``ANTIDETECT_UPDATE_PUBLIC_KEY``.
"""
import argparse
import getpass
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect import __version__, updater  # noqa: E402


def _password(args) -> str:
    if getattr(args, "password_env", None):
        password = os.environ.get(args.password_env)
        if not password:
            raise updater.UpdateError(
                f"Biến môi trường {args.password_env} rỗng hoặc chưa đặt."
            )
        return password
    if getattr(args, "ask_password", False):
        password = getpass.getpass("Mật khẩu private key: ")
        if not password:
            raise updater.UpdateError("Mật khẩu rỗng.")
        return password
    return ""


def cmd_keygen(args) -> int:
    password = _password(args) or None
    private_pem, public_pem = updater.generate_keypair_pem(password)
    out = os.path.abspath(args.out)
    public_out = args.public_out or (
        os.path.splitext(out)[0] + ".pub.pem"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out) and not args.force:
        raise updater.UpdateError(
            f"{out} đã tồn tại; thêm --force nếu muốn ghi đè."
        )
    with open(out, "wb") as f:
        f.write(private_pem)
        f.flush()
        os.fsync(f.fileno())
    with open(public_out, "wb") as f:
        f.write(public_pem)
        f.flush()
        os.fsync(f.fileno())
    print(f"Đã tạo private key: {out}")
    print(f"Public key:        {public_out}")
    print(
        "Giữ private key ngoài repo/offline. Phân phối public key tới máy "
        "cập nhật (--public-key hoặc ANTIDETECT_UPDATE_PUBLIC_KEY)."
    )
    return 0


def cmd_build(args) -> int:
    version = args.version or __version__
    delete = list(args.delete or [])
    if args.previous:
        old = updater.read_release_manifest(os.path.abspath(args.previous))
        current_files = {
            entry["path"] for entry in old["files"]
        }
        new_files = set(updater.collect_release_files(BASE_DIR))
        derived = sorted(current_files - new_files)
        if derived:
            print(f"File cần xoá khi nâng cấp (từ release cũ): {len(derived)}")
            for rel in derived:
                print(f"  - {rel}")
        delete.extend(derived)

    output = args.out or os.path.join(
        BASE_DIR, "dist", f"antidetect-python-{version}.zip"
    )
    password = _password(args) or None
    result = updater.build_release(
        BASE_DIR,
        output,
        version,
        private_key=args.key,
        password=password,
        delete=delete,
        allow_unsigned=args.allow_unsigned,
        require_package_version=not args.allow_version_mismatch,
    )
    manifest = result["manifest"]
    print(f"Release:   {result['zip']}")
    print(f"Version:   {manifest['version']}")
    print(f"Files:     {len(manifest['files'])}")
    print(f"Delete:    {len(manifest['delete'])}")
    print(f"Signature: {result['signature']}")
    print(f"Manifest SHA-256: {result['manifest_sha256']}")
    if result["signature"] == "unsigned":
        print("CẢNH BÁO: release chưa ký, chỉ dùng cho dev/kiểm thử nội bộ.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Đóng gói release Antidetect có chữ ký."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="Tạo cặp khóa Ed25519")
    keygen.add_argument("--out", required=True, help="Đường dẫn private key PEM")
    keygen.add_argument("--public-out", help="Đường dẫn public key PEM")
    keygen.add_argument("--password-env", help="Tên biến môi trường chứa mật khẩu")
    keygen.add_argument(
        "--ask-password", action="store_true", help="Hỏi mật khẩu tương tác"
    )
    keygen.add_argument("--force", action="store_true", help="Ghi đè key đã có")
    keygen.set_defaults(func=cmd_keygen)

    build = sub.add_parser("build", help="Đóng gói source hiện tại")
    build.add_argument(
        "--version", help="Version release (mặc định: antidetect.__version__)"
    )
    build.add_argument("--key", help="Private key PEM dùng để ký")
    build.add_argument(
        "--password-env", help="Tên biến môi trường chứa mật khẩu private key"
    )
    build.add_argument(
        "--ask-password", action="store_true", help="Hỏi mật khẩu tương tác"
    )
    build.add_argument(
        "--allow-unsigned", action="store_true", help="Không ký (dev only)"
    )
    build.add_argument(
        "--previous", help="Release zip cũ để tự suy ra danh sách file cần xoá"
    )
    build.add_argument(
        "--delete", action="append", metavar="PATH",
        help="File cần xoá khi apply (lặp lại được)",
    )
    build.add_argument("--out", "-o", help="Đường dẫn zip đầu ra")
    build.add_argument(
        "--allow-version-mismatch", action="store_true",
        help="Cho phép --version khác __version__ trong source",
    )
    build.set_defaults(func=cmd_build)
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
