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
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import urllib.request
import zipfile
from urllib.parse import quote, urlparse
from contextlib import contextmanager
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSERS_DIR = os.path.join(BASE_DIR, "browsers")
CFP_INDEX = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
_BROWSER_OPERATION_LOCK = threading.Lock()


def _reject_reparse(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SystemExit("UNSAFE_BROWSER_PATH: từ chối symlink/junction/reparse point.")


def _checked_browser_destination(version: str) -> Path:
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit("INVALID_BROWSER_VERSION: cần version đầy đủ, ví dụ 155.0.8059.85.")
    root = Path(os.path.abspath(BROWSERS_DIR))
    # Validate the original chain before resolve, which would hide junctions.
    for ancestor in (root, *root.parents):
        _reject_reparse(ancestor)
    dest = root / f"chrome-{version}"
    _reject_reparse(dest)
    resolved_root, resolved_dest = root.resolve(), dest.resolve()
    if resolved_dest == resolved_root or resolved_dest.parent != resolved_root:
        raise SystemExit("UNSAFE_BROWSER_PATH: đường dẫn nằm ngoài thư mục browsers.")
    return dest


@contextmanager
def _browser_operation_lock(version: str):
    """Serialize this tool's installs/removes; filesystem checks remain mandatory."""
    dest = _checked_browser_destination(version)
    with _BROWSER_OPERATION_LOCK:
        dest.parent.mkdir(parents=True, exist_ok=True)
        lock_path = dest.parent / ".install.lock"
        _reject_reparse(lock_path)
        with open(lock_path, "a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 8.0
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SystemExit("BROWSER_BUSY: thao tác cài/xóa browser khác đang chạy.") from None
                    time.sleep(0.05)
            try:
                yield _checked_browser_destination(version)
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_checked_tree(version: str) -> None:
    dest = _checked_browser_destination(version)
    if not dest.is_dir():
        raise SystemExit(f"Không tìm thấy browser version {version}.")
    identity = dest.stat()
    # Do not traverse a junction even when it points back into the tree.
    for current, directories, files in os.walk(dest, followlinks=False):
        _reject_reparse(Path(current))
        for name in directories + files:
            _reject_reparse(Path(current) / name)
    checked = _checked_browser_destination(version)
    final_identity = checked.stat()
    if (identity.st_dev, identity.st_ino) != (final_identity.st_dev, final_identity.st_ino):
        raise SystemExit("UNSAFE_BROWSER_PATH: thư mục browser đã thay đổi.")
    shutil.rmtree(checked)


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


def fetch_metadata(download_url: str) -> dict:
    """Đọc metadata GCS của file zip: md5Hash (base64) + size.

    known-good-versions JSON chỉ có URL/version, không kèm hash. GCS Object
    Metadata API là nguồn duy nhất CfT công bố (MD5, không có SHA-256).
    """
    parsed = urlparse(download_url)
    if (parsed.hostname or "").lower() != "storage.googleapis.com":
        raise SystemExit(
            f"Không hỗ trợ kiểm tra checksum cho host {parsed.hostname!r}."
        )
    path = parsed.path.lstrip("/")
    bucket, _, object_name = path.partition("/")
    if not bucket or not object_name:
        raise SystemExit(f"URL tải không hợp lệ: {download_url}")
    api = (
        "https://storage.googleapis.com/storage/v1/b/"
        f"{quote(bucket, safe='')}/o/{quote(object_name, safe='')}"
    )
    req = urllib.request.Request(api, headers={"User-Agent": "antidetect-python"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - fail closed, báo rõ nguồn lỗi
        raise SystemExit(
            f"Không đọc được metadata checksum từ GCS: {type(exc).__name__}: {exc}"
        ) from exc


def download_and_extract(entry: dict) -> str:
    with _browser_operation_lock(entry.get("version")) as dest:
        return _download_and_extract_locked(entry, str(dest))


def _download_and_extract_locked(entry: dict, dest: str) -> str:
    if os.path.exists(os.path.join(dest, "chrome-win64", "chrome.exe")):
        print(f"[=] Đã có sẵn: {dest}")
        return dest
    meta = fetch_metadata(entry["url"])
    expected_md5 = str(meta.get("md5Hash") or "").strip()
    expected_size = int(meta.get("size") or 0)
    if not expected_md5 or expected_size <= 0:
        raise SystemExit(
            "GCS không trả md5Hash/size cho archive — hủy cài để tránh "
            "chạy binary chưa xác minh."
        )
    os.makedirs(BROWSERS_DIR, exist_ok=True)
    zip_path = dest + ".zip"
    print(f"[*] Tải {entry['url']}")
    t0 = time.time()
    digest = hashlib.md5()
    req = urllib.request.Request(entry["url"], headers={"User-Agent": "antidetect-python"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct % 10 == 0:
                    print(f"    {pct}% ({done >> 20}MB)", flush=True)
    actual_md5 = base64.b64encode(digest.digest()).decode("ascii")
    if done != expected_size or actual_md5 != expected_md5:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise SystemExit(
            "Checksum không khớp — đã hủy cài. "
            f"(size {done}/{expected_size}, md5 {actual_md5}/{expected_md5})"
        )
    print(f"[*] Checksum MD5 khớp metadata GCS — tải xong {done >> 20}MB trong {time.time() - t0:.0f}s — giải nén...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    os.remove(zip_path)
    exe = os.path.join(dest, "chrome-win64", "chrome.exe")
    if not os.path.exists(exe):
        if os.path.isdir(dest):
            _remove_checked_tree(entry["version"])
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
    with _browser_operation_lock(version) as dest:
        _remove_checked_tree(version)
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
