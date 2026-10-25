"""Tải và quản lý Chrome bản pin (Chrome-for-Testing) cho tool.

Vì sao: Chrome của máy tự cập nhật → UA/JA3/WebGL của cả farm đổi cùng lúc
giữa chừng (dấu hiệu bất thường với site theo dõi dài hạn). Browser pin
khiến fingerprint ổn định theo tuần/tháng, và cho phép các profile dùng
khác phiên bản Chrome để giảm liên kết theo UA.

Dùng:
  python tools/install_browser.py --list
  python tools/install_browser.py --version 155.0.8059.85
  python tools/install_browser.py --major 155        # bản known-good mới nhất của major 155
  python tools/install_browser.py --remove 155.0.8059.85

Browser tải về nằm ở <repo>/browsers/chrome-<version>/ (ngoài data/, trong
.gitignore). Launcher tự ưu tiên: browser per-profile → env
ANTIDETECT_BROWSER_PATH → browsers/ (mới nhất) → Chrome/Edge đã cài.
"""
import argparse
import io
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSERS_DIR = os.path.join(BASE_DIR, "browsers")
CFP_INDEX = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"


def fetch_index() -> list:
    req = urllib.request.Request(CFP_INDEX, headers={"User-Agent": "antidetect-python"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("versions") or []


def pick_version(versions: list, version: str = None, major: str = None) -> dict:
    for entry in reversed(versions):  # known-good list theo thứ tự tăng dần
        if version and entry.get("version") != version:
            continue
        if major and not entry.get("version", "").startswith(major + "."):
            continue
        downloads = (entry.get("downloads") or {}).get("chrome") or []
        url = next(
            (d.get("url") for d in downloads if d.get("platform") == "win64"),
            None,
        )
        if url:
            return {"version": entry["version"], "url": url}
    raise SystemExit(
        f"Không tìm thấy bản Chrome-for-Testing phù hợp "
        f"(version={version or '-'}, major={major or '-'}). Hãy xem --list."
    )


def download_and_extract(entry: dict) -> str:
    dest = os.path.join(BROWSERS_DIR, f"chrome-{entry['version']}")
    if os.path.exists(os.path.join(dest, "chrome-win64", "chrome.exe")):
        print(f"[=] Đã có sẵn: {dest}")
        return dest
    os.makedirs(BROWSERS_DIR, exist_ok=True)
    zip_path = dest + ".zip"
    print(f"[*] Tải {entry['url']}")
    t0 = time.time()
    req = urllib.request.Request(entry["url"], headers={"User-Agent": "antidetect-python"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct % 10 == 0:
                    print(f"    {pct}% ({done >> 20}MB)", flush=True)
    print(f"[*] Tải xong {done >> 20}MB trong {time.time() - t0:.0f}s — giải nén...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    os.remove(zip_path)
    exe = os.path.join(dest, "chrome-win64", "chrome.exe")
    if not os.path.exists(exe):
        shutil.rmtree(dest, ignore_errors=True)
        raise SystemExit("Zip không chứa chrome.exe như kỳ vọng — đã dọn dẹp.")
    print(f"[OK] Đã cài: {exe}")
    return dest


def list_browsers() -> None:
    if not os.path.isdir(BROWSERS_DIR):
        print("(chưa có browser pin nào — launcher sẽ dùng Chrome/Edge đã cài)")
        return
    found = []
    for name in sorted(os.listdir(BROWSERS_DIR)):
        exe = os.path.join(BROWSERS_DIR, name, "chrome-win64", "chrome.exe")
        if name.startswith("chrome-") and os.path.exists(exe):
            found.append(name)
    if not found:
        print("(chưa có browser pin nào hợp lệ)")
        return
    print("Browser pin khả dụng (mới nhất được ưu tiên khi không chỉ định):")
    for name in found:
        print(f"  {name}")


def remove_browser(version: str) -> None:
    dest = os.path.join(BROWSERS_DIR, f"chrome-{version}")
    if not os.path.isdir(dest):
        raise SystemExit(f"Không tìm thấy {dest}")
    shutil.rmtree(dest)
    print(f"[OK] Đã xóa {dest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="Liệt kê browser đã tải")
    parser.add_argument("--version", help="Cài đúng phiên bản này (vd 155.0.8059.85)")
    parser.add_argument("--major", help="Cài bản known-good mới nhất của major (vd 155)")
    parser.add_argument("--remove", help="Xóa browser pin theo version")
    args = parser.parse_args()

    if args.list:
        list_browsers()
        return
    if args.remove:
        remove_browser(args.remove)
        return
    if not args.version and not args.major:
        parser.error("Cần --version hoặc --major (hoặc --list)")

    entry = pick_version(fetch_index(), args.version, args.major)
    print(f"[*] Chọn Chrome-for-Testing {entry['version']}")
    download_and_extract(entry)


if __name__ == "__main__":
    main()
