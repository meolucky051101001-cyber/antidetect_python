"""CLI ghim và xác minh C++ Antidetect Browser Core local (SHA-256 pinned)."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from antidetect.browser_trust import BrowserTrustError, verify_browser_executable  # noqa: E402
from antidetect.custom_cores import (  # noqa: E402
    custom_cores_manifest_path,
    list_custom_cores,
    pin_custom_core,
    remove_custom_core,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quản lý bản ghim SHA-256 local cho C++ Antidetect Browser Core (ví dụ Fingerprint-Chromium)."
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Đường dẫn trusted_cores.json tùy chỉnh (mặc định: browsers/trusted_cores.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pin_cmd = sub.add_parser("pin", help="Xác minh PE và ghim SHA-256 cho một custom browser core local.")
    pin_cmd.add_argument("--exe", required=True, help="Đường dẫn tuyệt đối tới chrome.exe / chromium.exe.")
    pin_cmd.add_argument(
        "--engine",
        default="fingerprint-chromium",
        choices=("fingerprint-chromium", "custom-chromium"),
        help="Loại nhân C++ (mặc định: fingerprint-chromium).",
    )
    pin_cmd.add_argument("--version", default=None, help="Phiên bản x.y.z.w (vd: 142.0.7444.59).")
    pin_cmd.add_argument("--product", default=None, help="Tên hiển thị sản phẩm.")

    sub.add_parser("list", help="Liệt kê các custom browser core đã ghim.")

    verify_cmd = sub.add_parser("verify", help="Xác minh offline một binary theo trust policy hiện hành.")
    verify_cmd.add_argument("--exe", required=True, help="Đường dẫn tới binary cần kiểm tra.")

    rm_cmd = sub.add_parser("remove", help="Gỡ bản ghim của một custom browser core.")
    rm_cmd.add_argument("--exe", required=True, help="Đường dẫn tới binary cần gỡ ghim.")

    args = parser.parse_args(argv)
    manifest = args.manifest or custom_cores_manifest_path()
    try:
        if args.command == "pin":
            result = pin_custom_core(
                args.exe,
                engine=args.engine,
                version=args.version,
                product=args.product,
                manifest_path=manifest,
            )
            print(json.dumps({"status": "pinned", **result}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            cores = list_custom_cores(manifest_path=manifest)
            print(json.dumps({"manifest_path": os.path.abspath(manifest), "cores": cores}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            att = verify_browser_executable(args.exe, custom_manifest_path=manifest)
            print(json.dumps({"status": "verified", **dataclasses.asdict(att)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "remove":
            removed = remove_custom_core(args.exe, manifest_path=manifest)
            print(json.dumps({"status": "removed" if removed else "not_found", "path": os.path.abspath(args.exe)}, ensure_ascii=False, indent=2))
            return 0 if removed else 1
    except (BrowserTrustError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
