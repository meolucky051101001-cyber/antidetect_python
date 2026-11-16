"""P2-13: controlled, signed upgrade and rollback.

Release layout (a zip)::

    release.json        manifest: version + SHA-256/size của từng file
    release.json.sig    chữ ký Ed25519 (base64) trên đúng bytes release.json
    payload/<path>      file nguồn, mirror cấu trúc thư mục cài đặt

Không có auto-update ngầm. Operator chạy ``python tools/update.py``, máy chủ
phải đã tắt (hoặc chỉ định ``--stop-server``), mọi file được kiểm hash trước
khi ghi, file cũ được backup và có thể rollback. ``data/``, ``browsers/`` và
các thư mục vận hành khác không bao giờ nằm trong payload.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .applog import get_logger

log = get_logger("updater")

APP_NAME = "antidetect-python"
SCHEMA_VERSION = 1
RELEASE_MANIFEST = "release.json"
RELEASE_SIGNATURE = "release.json.sig"
PAYLOAD_PREFIX = "payload/"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_FILES = 20000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
BACKUP_KEEP = 5

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROTECTED_ROOTS = {
    ".git", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "backups", "browsers", "data", "dist", "exports", "htmlcov",
    "node_modules", "temp_dir", "test_out_dir", "venv", "workspace",
    "workspace_backup_safety",
}
_PROTECTED_SUFFIXES = (
    ".dpapi", ".key", ".lock", ".log", ".p12", ".pem", ".pfx", ".pid",
    ".pyc", ".pyo", ".zip",
)
_PROTECTED_NAMES = {".env", ".coverage", "coverage.xml", "thumbs.db", ".ds_store"}
_PE_SUFFIXES = (".exe", ".dll", ".pyd", ".sys")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BAD_PATH_CHARS = set('<>:"|?*')


class UpdateError(RuntimeError):
    pass


class ManifestError(UpdateError):
    pass


class SignatureError(UpdateError):
    pass


class ZipSafetyError(UpdateError):
    pass


class ServerRunningError(UpdateError):
    pass


class DowngradeError(UpdateError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_no_reparse(path: str) -> str:
    """Reject links in every existing component, including the selected root.

    Rechecked at mutation boundaries. This is not an OS isolation boundary:
    hostile same-user code can still race path-based operations after a check.
    """
    absolute = os.path.abspath(path)
    ancestors = []
    current = absolute
    while True:
        ancestors.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for component in reversed(ancestors):
        try:
            value = os.lstat(component)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or (
            getattr(value, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ZipSafetyError("Reparse point/symlink không được phép trong đường cập nhật")
    return absolute


def _mkdir_checked(path: str) -> None:
    _assert_no_reparse(path)
    os.makedirs(path, exist_ok=True)
    _assert_no_reparse(path)


def _remove_checked(path: str) -> None:
    _assert_no_reparse(path)
    os.remove(path)


def _cleanup_temp(path: str) -> None:
    try:
        _remove_checked(path)
    except (OSError, UpdateError):
        # Never follow a substituted parent just to clean a temporary file.
        pass


def _remove_tree_checked(path: str) -> None:
    _assert_no_reparse(path)
    if not os.path.exists(path):
        return
    for directory, dirs, files in os.walk(path, followlinks=False):
        _assert_no_reparse(directory)
        for name in dirs + files:
            _assert_no_reparse(os.path.join(directory, name))
    _assert_no_reparse(path)
    shutil.rmtree(path)


def _preflight_targets(root: str, paths: Iterable[str]) -> None:
    for rel in paths:
        target = _safe_install_path(root, rel)
        if os.path.isdir(target):
            raise UpdateError("File cập nhật trỏ vào thư mục")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    _assert_no_reparse(path)
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_rel_path(raw: Any) -> str:
    """Chuẩn hoá path tương đối trong payload; từ chối mọi thứ nguy hiểm."""
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError("Path rỗng hoặc không phải chuỗi.")
    if "\x00" in raw:
        raise ManifestError(f"Path chứa NUL: {raw!r}")
    path = raw.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ManifestError(f"Path tuyệt đối không được phép: {raw!r}")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ManifestError(f"Path không hợp lệ (thành phần {part!r}): {raw!r}")
        if part != part.rstrip(" ."):
            raise ManifestError(f"Path có dấu chấm/khoảng trắng cuối: {raw!r}")
        if any(ch in _BAD_PATH_CHARS or ord(ch) < 32 for ch in part):
            raise ManifestError(f"Path chứa ký tự cấm: {raw!r}")
        if part.split(".")[0].upper() in _RESERVED_WINDOWS_NAMES:
            raise ManifestError(f"Path dùng tên dành riêng của Windows: {raw!r}")
    return "/".join(parts)


def is_protected_rel_path(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if parts[0].lower() in PROTECTED_ROOTS:
        return True
    if any(part.lower() == "__pycache__" for part in parts):
        return True
    low = rel_path.lower()
    if low in (RELEASE_MANIFEST, RELEASE_SIGNATURE):
        return True
    if low.endswith(_PROTECTED_SUFFIXES):
        return True
    if parts[-1].lower() in _PROTECTED_NAMES:
        return True
    return False


def _safe_install_path(root: str, rel_path: str) -> str:
    rel = normalize_rel_path(rel_path)
    if is_protected_rel_path(rel):
        raise ManifestError(f"Path bảo vệ không được ghi: {rel}")
    root_abs = _assert_no_reparse(root)
    target = os.path.abspath(os.path.join(root_abs, *rel.split("/")))
    if os.path.commonpath([root_abs, target]) != root_abs:
        raise ZipSafetyError(f"Path thoát khỏi thư mục cài đặt: {rel}")
    _assert_no_reparse(target)
    return target


def version_tuple(version: str) -> Tuple[int, ...]:
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise UpdateError(f"Version không hợp lệ: {version!r}")
    parts = []
    for segment in version.split("."):
        match = re.match(r"^(\d+)", segment)
        if not match:
            raise UpdateError(f"Version không hợp lệ: {version!r}")
        parts.append(int(match.group(1)))
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def read_installed_version(install_root: str) -> str:
    path = _safe_install_path(install_root, "antidetect/__init__.py")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise UpdateError(
            f"Không đọc được version đang cài: {path}"
        ) from exc
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not match:
        raise UpdateError(f"Không tìm thấy __version__ trong {path}")
    return match.group(1)


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    _assert_no_reparse(path)
    _mkdir_checked(os.path.dirname(os.path.abspath(path)))
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _assert_no_reparse(path)
        _assert_no_reparse(tmp)
        os.replace(tmp, path)
    finally:
        _cleanup_temp(tmp)


def read_update_state(state_path: str) -> Dict[str, Any]:
    _assert_no_reparse(state_path)
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"update_state.json bị hỏng: {state_path}") from exc
    if not isinstance(data, dict):
        raise UpdateError(f"update_state.json phải là object: {state_path}")
    return data


def _data_root(install_root: str) -> str:
    return os.environ.get("ANTIDETECT_DATA_DIR") or os.path.join(
        install_root, "data"
    )


def _resolve_paths(
    install_root: str,
    state_path: Optional[str],
    updates_dir: Optional[str],
) -> Tuple[str, str]:
    root = os.path.abspath(install_root)
    updates = updates_dir or os.path.join(_data_root(root), "updates")
    state = state_path or os.path.join(updates, "update_state.json")
    return os.path.abspath(updates), os.path.abspath(state)


def resolve_update_paths(
    install_root: Optional[str] = None,
    state_path: Optional[str] = None,
    updates_dir: Optional[str] = None,
) -> Tuple[str, str]:
    return _resolve_paths(
        install_root or _BASE_DIR, state_path, updates_dir
    )


# ---------------------------------------------------------------------------
# Keys and signatures
# ---------------------------------------------------------------------------

_BASE64_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _read_key_source(source: Any) -> str:
    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    if isinstance(source, bytes):
        return source.decode("utf-8")
    if not isinstance(source, str) or not source.strip():
        raise SignatureError("Public/private key rỗng.")
    if "BEGIN" in source and "END" in source:
        return source
    if len(source) < 260 and not set(source.strip()) <= _BASE64_CHARS:
        if not os.path.exists(source):
            raise SignatureError(f"Không tìm thấy file key: {source}")
        with open(source, "r", encoding="utf-8") as f:
            return f.read()
    return source


def load_public_key(source: Any):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    text = _read_key_source(source)
    if "BEGIN PUBLIC KEY" in text:
        try:
            key = serialization.load_pem_public_key(text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - báo lỗi cấu hình key
            raise SignatureError(f"Public key PEM không hợp lệ: {exc}") from exc
    else:
        try:
            raw = base64.b64decode("".join(text.split()), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SignatureError("Public key không phải PEM hoặc base64 thô.") from exc
        if len(raw) != 32:
            raise SignatureError("Ed25519 public key thô phải dài 32 bytes.")
        key = Ed25519PublicKey.from_public_bytes(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("Public key không phải Ed25519.")
    return key


def load_private_key(source: Any, password: Optional[str] = None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    text = _read_key_source(source)
    if "BEGIN" not in text or "PRIVATE KEY" not in text:
        try:
            raw = base64.b64decode("".join(text.split()), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SignatureError(
                "Private key không phải PEM hoặc base64 thô."
            ) from exc
        if len(raw) != 32:
            raise SignatureError("Ed25519 private key thô phải dài 32 bytes.")
        key = Ed25519PrivateKey.from_private_bytes(raw)
    else:
        try:
            key = serialization.load_pem_private_key(
                text.encode("utf-8"),
                password=password.encode("utf-8") if password else None,
            )
        except TypeError as exc:
            raise SignatureError(
                "Private key đang mã hoá; cần --password-env hoặc bỏ mật khẩu."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SignatureError(f"Private key không hợp lệ: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("Private key không phải Ed25519.")
    return key


def generate_keypair_pem(password: Optional[str] = None) -> Tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    if password:
        encryption = serialization.BestAvailableEncryption(password.encode("utf-8"))
    else:
        encryption = serialization.NoEncryption()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption,
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def sign_bytes(private_key, data: bytes) -> str:
    return base64.b64encode(private_key.sign(data)).decode("ascii")


def verify_signature(public_key, data: bytes, signature_b64: str) -> None:
    from cryptography.exceptions import InvalidSignature

    try:
        raw = base64.b64decode("".join(str(signature_b64).split()), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("Chữ ký release không phải base64 hợp lệ.") from exc
    try:
        public_key.verify(raw, data)
    except InvalidSignature as exc:
        raise SignatureError("Chữ ký release không hợp lệ.") from exc


# ---------------------------------------------------------------------------
# Manifest and archive verification
# ---------------------------------------------------------------------------

def _validate_manifest(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError("release.json phải là JSON object.")
    if data.get("schema") != SCHEMA_VERSION:
        raise ManifestError(
            f"Manifest schema không hỗ trợ: {data.get('schema')!r}"
        )
    if data.get("name") != APP_NAME:
        raise ManifestError(f"Manifest dành cho ứng dụng khác: {data.get('name')!r}")
    version = data.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestError(f"Version trong manifest không hợp lệ: {version!r}")

    files = data.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ManifestError("Manifest phải có danh sách files (1..%d)." % MAX_FILES)
    seen = set()
    total = 0
    clean_files = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ManifestError("Mỗi entry files phải là object.")
        rel = normalize_rel_path(entry.get("path"))
        if rel != entry.get("path"):
            raise ManifestError(f"Path phải ở dạng chuẩn (forward slash): {rel}")
        if is_protected_rel_path(rel):
            raise ManifestError(f"Payload chứa path bảo vệ: {rel}")
        key = rel.casefold()
        if key in seen:
            raise ManifestError(f"Path trùng trong manifest: {rel}")
        seen.add(key)
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise ManifestError(f"SHA-256 không hợp lệ cho {rel}")
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError(f"Size không hợp lệ cho {rel}")
        if size > MAX_FILE_BYTES:
            raise ManifestError(f"File quá lớn trong manifest: {rel}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ManifestError("Tổng dung lượng payload vượt giới hạn.")
        clean_files.append({"path": rel, "sha256": digest, "size": size})

    delete = data.get("delete") or []
    if not isinstance(delete, list):
        raise ManifestError("'delete' phải là list.")
    clean_delete = []
    seen_delete = set()
    for raw in delete:
        rel = normalize_rel_path(raw)
        if rel != raw:
            raise ManifestError(f"Path delete phải ở dạng chuẩn: {rel}")
        if is_protected_rel_path(rel):
            raise ManifestError(f"Delete chứa path bảo vệ: {rel}")
        if rel.casefold() in seen:
            raise ManifestError(f"Delete trùng với file payload: {rel}")
        if rel.casefold() in seen_delete:
            raise ManifestError(f"Path delete trùng: {rel}")
        seen_delete.add(rel.casefold())
        clean_delete.append(rel)

    manifest = {
        "schema": SCHEMA_VERSION,
        "name": APP_NAME,
        "version": version,
        "created_at": data.get("created_at"),
        "files": clean_files,
        "delete": sorted(clean_delete),
    }
    return manifest


def _read_manifest_from_zip(zf: zipfile.ZipFile) -> Tuple[Dict[str, Any], bytes]:
    try:
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"Không đọc được mục lục zip: {exc}") from exc
    for special in (RELEASE_MANIFEST, RELEASE_SIGNATURE):
        if sum(1 for i in infos if i.filename == special and not i.is_dir()) > 1:
            raise ZipSafetyError(f"Zip có nhiều entry {special}.")
    for info in infos:
        if info.filename == RELEASE_MANIFEST and info.is_dir():
            raise ManifestError("release.json là thư mục, không hợp lệ.")
    try:
        info = zf.getinfo(RELEASE_MANIFEST)
    except KeyError as exc:
        raise ManifestError("Zip thiếu release.json.") from exc
    if info.is_dir() or info.file_size > MAX_MANIFEST_BYTES:
        raise ManifestError("release.json có kích thước không hợp lệ.")
    try:
        raw = zf.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ManifestError(f"Không đọc được release.json: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("release.json không phải JSON UTF-8 hợp lệ.") from exc
    return _validate_manifest(data), raw


def _collect_payload_infos(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    infos = []
    seen = set()
    for info in zf.infolist():
        name = info.filename
        if name in (RELEASE_MANIFEST, RELEASE_SIGNATURE) and not info.is_dir():
            continue
        if info.is_dir():
            if name.strip("/"):
                normalize_rel_path(name.rstrip("/") if name != "/" else "")
            continue
        if info.flag_bits & 0x1:
            raise ZipSafetyError(f"Zip chứa entry mã hoá: {name}")
        normalize_rel_path(name)
        if not name.startswith(PAYLOAD_PREFIX):
            raise ZipSafetyError(f"Entry ngoài payload/: {name}")
        rel = normalize_rel_path(name[len(PAYLOAD_PREFIX):])
        if is_protected_rel_path(rel):
            raise ZipSafetyError(f"Zip chứa path bảo vệ: {rel}")
        key = name.casefold()
        if key in seen:
            raise ZipSafetyError(f"Zip có entry trùng tên: {name}")
        seen.add(key)
        if info.file_size > MAX_FILE_BYTES:
            raise ZipSafetyError(f"Entry quá lớn: {name}")
        infos.append(info)
    if len(infos) > MAX_FILES:
        raise ZipSafetyError("Zip có quá nhiều file.")
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise ZipSafetyError("Tổng dung lượng zip vượt giới hạn.")
    return infos


def _hash_zip_member(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, expected_size: int
) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with zf.open(info) as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise ManifestError(
                        f"Entry lớn hơn size khai báo: {info.filename}"
                    )
                digest.update(chunk)
    except ManifestError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ZipSafetyError(f"Không đọc được {info.filename}: {exc}") from exc
    if total != expected_size:
        raise ManifestError(
            f"Size thực tế khác manifest cho {info.filename} "
            f"({total} != {expected_size})."
        )
    return digest.hexdigest()


def verify_release(
    zip_path: str,
    public_key: Any = None,
    allow_unsigned: bool = False,
) -> Dict[str, Any]:
    """Kiểm tra chữ ký + hash toàn bộ payload mà không ghi gì xuống đĩa."""
    manifest, raw_manifest, signature_state = _verify_archive(
        zip_path, public_key, allow_unsigned
    )
    return {
        "zip": os.path.abspath(zip_path),
        "manifest": manifest,
        "manifest_sha256": sha256_bytes(raw_manifest),
        "signature": signature_state,
    }


def _verify_archive(
    zip_path: str,
    public_key: Any,
    allow_unsigned: bool,
) -> Tuple[Dict[str, Any], bytes, str]:
    if not os.path.isfile(zip_path):
        raise UpdateError(f"Không tìm thấy file release: {zip_path}")
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"Release không phải zip hợp lệ: {exc}") from exc
    with zf:
        manifest, raw_manifest = _read_manifest_from_zip(zf)
        try:
            sig_info = zf.getinfo(RELEASE_SIGNATURE)
        except KeyError:
            sig_info = None

        if sig_info is not None:
            if not public_key:
                raise SignatureError(
                    "Release có chữ ký nhưng chưa cấu hình public key "
                    "(--public-key hoặc ANTIDETECT_UPDATE_PUBLIC_KEY)."
                )
            if sig_info.file_size > 64 * 1024:
                raise SignatureError("File chữ ký quá lớn.")
            try:
                signature = zf.read(sig_info).decode("ascii")
            except (UnicodeDecodeError, zipfile.BadZipFile, RuntimeError) as exc:
                raise SignatureError("Không đọc được release.json.sig.") from exc
            verify_signature(load_public_key(public_key), raw_manifest, signature)
            signature_state = "verified"
        elif allow_unsigned:
            signature_state = "unsigned"
        else:
            raise SignatureError(
                "Release không có chữ ký. Chỉ dùng --allow-unsigned cho bản "
                "dev đã kiểm tra thủ công."
            )

        payload_infos = _collect_payload_infos(zf)
        expected = {
            PAYLOAD_PREFIX + entry["path"]: entry for entry in manifest["files"]
        }
        actual = {info.filename: info for info in payload_infos}
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            raise ManifestError(f"Zip thiếu file payload: {missing[:5]}")
        if extra:
            raise ZipSafetyError(f"Zip có file không khai báo: {extra[:5]}")
        for arcname, entry in expected.items():
            info = actual[arcname]
            digest = _hash_zip_member(zf, info, entry["size"])
            if digest != entry["sha256"]:
                raise ManifestError(
                    f"SHA-256 không khớp cho {entry['path']}."
                )
    return manifest, raw_manifest, signature_state


# ---------------------------------------------------------------------------
# Authenticode (optional, chỉ áp dụng cho file PE trong payload)
# ---------------------------------------------------------------------------

def authenticode_status(path: str) -> str:
    if sys.platform != "win32":
        return "Unsupported"
    literal = path.replace("'", "''")
    script = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; "
        "if ($s) { $s.Status.ToString() } else { 'Unknown' }"
    )
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (result.stdout or "").strip() or "Unknown"
    except Exception as exc:  # noqa: BLE001 - báo lỗi để operator quyết định
        return f"Error: {type(exc).__name__}"


def _check_authenticode(
    files: Iterable[str], required: bool
) -> Dict[str, str]:
    statuses = {}
    for path in files:
        if os.path.splitext(path)[1].lower() not in _PE_SUFFIXES:
            continue
        status = authenticode_status(path)
        statuses[os.path.basename(path)] = status
        if required and status != "Valid":
            raise UpdateError(
                f"File PE chưa có chữ ký Authenticode hợp lệ: "
                f"{os.path.basename(path)} ({status})"
            )
    return statuses


# ---------------------------------------------------------------------------
# Server guard
# ---------------------------------------------------------------------------

def _server_pid_liveness(pid: Any) -> str:
    """A failed health probe is never evidence that a recorded PID exited."""
    try:
        if isinstance(pid, bool) or not 0 < int(pid) <= 0xFFFFFFFF:
            return "unknown"
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return "alive"
        except ProcessLookupError:
            return "absent"
        except OSError:
            return "unknown"
    try:
        import ctypes
        from ctypes import wintypes
        from .process_identity import _kernel32

        api = _kernel32()
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return "absent" if ctypes.get_last_error() == 87 else "unknown"
        try:
            status = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(status)):
                return "unknown"
            return "alive" if status.value == 259 else "absent"
        finally:
            api.CloseHandle(handle)
    except Exception:
        return "unknown"


def detect_running_server() -> Optional[Dict[str, Any]]:
    from . import server_control

    info = server_control.read_server_info() or {}
    ports = []
    if info.get("port"):
        try:
            ports.append(int(info["port"]))
        except (TypeError, ValueError):
            pass
    if 8000 not in ports:
        ports.append(8000)
    for port in ports:
        health = server_control.check_server_health(port)
        if health and health.get("application") == APP_NAME:
            return {
                "port": port,
                "pid": info.get("pid"),
                "instance_id": health.get("instance_id"),
            }
    if info or os.path.lexists(server_control.PID_FILE):
        liveness = _server_pid_liveness(info.get("pid")) if info else "unknown"
        if liveness != "absent":
            # Do not kill here. The existing stop command must independently
            # prove identity; an unknown/denied query blocks install writes.
            return {
                "port": info.get("port"), "pid": info.get("pid"),
                "instance_id": info.get("instance_id"),
                "state": liveness, "health_unavailable": True,
            }
    return None


def _ensure_server_stopped(
    server_check: Callable[[], Optional[Dict[str, Any]]],
    stop_server: bool,
    stop_server_fn: Callable[[], Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    running = server_check()
    if not running:
        return None
    if not stop_server:
        raise ServerRunningError(
            f"Máy chủ đang chạy (port {running.get('port')}). Dừng server "
            "trước khi cập nhật, hoặc thêm --stop-server."
        )
    result = stop_server_fn()
    if not result.get("success"):
        raise UpdateError(
            f"Không dừng được máy chủ: {result.get('message') or result}"
        )
    if server_check():
        raise ServerRunningError(
            "Máy chủ vẫn phản hồi sau khi yêu cầu dừng; hủy cập nhật."
        )
    return result


# ---------------------------------------------------------------------------
# Copy helpers
# ---------------------------------------------------------------------------

def _copy_verified(source: str, dest: str, expected_sha: str) -> None:
    _assert_no_reparse(source)
    _assert_no_reparse(dest)
    _mkdir_checked(os.path.dirname(dest))
    tmp = f"{dest}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        # Exclusive create refuses an already-present temporary link or file.
        with open(source, "rb") as src, open(tmp, "xb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if sha256_file(tmp) != expected_sha:
            raise UpdateError(f"Hash không khớp sau khi copy: {dest}")
        _assert_no_reparse(source)
        _assert_no_reparse(dest)
        _assert_no_reparse(tmp)
        os.replace(tmp, dest)
    finally:
        _cleanup_temp(tmp)


def _extract_payload(
    zip_path: str, manifest: Dict[str, Any], dest_root: str
) -> List[str]:
    extracted = []
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"Không mở lại được release: {exc}") from exc
    with zf:
        for entry in manifest["files"]:
            arcname = PAYLOAD_PREFIX + entry["path"]
            target = _safe_install_path(dest_root, entry["path"])
            try:
                info = zf.getinfo(arcname)
            except KeyError as exc:
                raise ManifestError(f"Zip thiếu {arcname}") from exc
            digest = hashlib.sha256()
            total = 0
            _mkdir_checked(os.path.dirname(target))
            tmp = f"{target}.tmp.{uuid.uuid4().hex[:8]}"
            try:
                with zf.open(info) as src, open(tmp, "xb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > entry["size"]:
                            raise ManifestError(
                                f"Entry lớn hơn khai báo: {entry['path']}"
                            )
                        digest.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if total != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise ManifestError(f"Hash không khớp: {entry['path']}")
                _assert_no_reparse(tmp)
                _safe_install_path(dest_root, entry["path"])
                os.replace(tmp, target)
            finally:
                _cleanup_temp(tmp)
            extracted.append(target)
    return extracted


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def collect_release_files(source_root: str) -> List[str]:
    root = os.path.abspath(source_root)
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d.lower() not in PROTECTED_ROOTS and not d.startswith(".")
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if is_protected_rel_path(rel):
                continue
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            result.append(rel)
    return sorted(result)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def build_release(
    source_root: str,
    output_path: str,
    version: str,
    private_key: Any = None,
    password: Optional[str] = None,
    delete: Optional[Iterable[str]] = None,
    allow_unsigned: bool = False,
    require_package_version: bool = True,
    created_at: Optional[int] = None,
) -> Dict[str, Any]:
    """Đóng gói source thành zip có manifest + chữ ký; không sửa source."""
    source_root = os.path.abspath(source_root)
    if require_package_version:
        installed = read_installed_version(source_root)
        if version_tuple(installed) != version_tuple(version):
            raise UpdateError(
                f"--version {version} khác __version__ {installed} trong source."
            )
    rel_files = collect_release_files(source_root)
    if not rel_files:
        raise UpdateError("Không tìm thấy file nào để đóng gói.")

    files = []
    payload = {}
    total = 0
    for rel in rel_files:
        full = os.path.join(source_root, *rel.split("/"))
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise UpdateError(f"Không đọc được {rel}: {exc}") from exc
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise UpdateError(f"Release vượt giới hạn dung lượng tại {rel}.")
        payload[rel] = data
        files.append(
            {"path": rel, "sha256": sha256_bytes(data), "size": len(data)}
        )

    delete_paths = []
    for raw in delete or []:
        rel = normalize_rel_path(raw)
        if is_protected_rel_path(rel):
            raise UpdateError(f"Không thể xoá path bảo vệ: {rel}")
        if rel in payload:
            raise UpdateError(f"Path vừa có trong payload vừa delete: {rel}")
        delete_paths.append(rel)
    delete_paths = sorted(set(delete_paths))

    manifest = {
        "schema": SCHEMA_VERSION,
        "name": APP_NAME,
        "version": version,
        "created_at": int(created_at or time.time()),
        "files": files,
        "delete": delete_paths,
    }
    raw_manifest = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    if private_key is not None:
        key = (
            private_key
            if hasattr(private_key, "sign")
            else load_private_key(private_key, password)
        )
        signature = sign_bytes(key, raw_manifest)
    elif allow_unsigned:
        signature = None
    else:
        raise SignatureError(
            "Cần private key để ký release, hoặc --allow-unsigned cho bản dev."
        )

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp_zip = f"{output_path}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with zipfile.ZipFile(
            tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zf:
            zf.writestr(_zip_info(RELEASE_MANIFEST), raw_manifest)
            if signature is not None:
                zf.writestr(
                    _zip_info(RELEASE_SIGNATURE), signature.encode("ascii") + b"\n"
                )
            for rel in sorted(payload):
                zf.writestr(_zip_info(PAYLOAD_PREFIX + rel), payload[rel])
        os.replace(tmp_zip, output_path)
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass

    return {
        "zip": output_path,
        "manifest": manifest,
        "manifest_sha256": sha256_bytes(raw_manifest),
        "signature": "signed" if signature is not None else "unsigned",
    }


def read_release_manifest(zip_path: str) -> Dict[str, Any]:
    """Đọc manifest chưa verify — dùng cho tooling (derive delete list)."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            manifest, _ = _read_manifest_from_zip(zf)
        return manifest
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(f"Không đọc được release cũ: {exc}") from exc


# ---------------------------------------------------------------------------
# Apply / rollback
# ---------------------------------------------------------------------------

def _backup_file(
    root: str, rel: str, backup_files_dir: str
) -> Optional[Dict[str, Any]]:
    source = _safe_install_path(root, rel)
    if not os.path.isfile(source):
        return None
    digest = sha256_file(source)
    size = os.path.getsize(source)
    dest = _safe_install_path(backup_files_dir, rel)
    _copy_verified(source, dest, digest)
    return {"path": rel, "sha256": digest, "size": size, "existed": True}


def _read_backup_manifest(backup_dir: str, root: Optional[str] = None) -> Dict[str, Any]:
    manifest_path = _assert_no_reparse(os.path.join(backup_dir, "backup.json"))
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            backup = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateError(f"Không đọc được backup.json: {exc}") from exc
    if not isinstance(backup, dict) or type(backup.get("schema")) is not int or backup["schema"] != 1:
        raise UpdateError("Backup schema is unsupported")
    for key in ("from_version", "to_version"):
        version_tuple(backup.get(key))
    release_hash = backup.get("release_manifest_sha256")
    if not isinstance(release_hash, str) or not _SHA256_RE.fullmatch(release_hash):
        raise UpdateError("Backup release hash is invalid")
    recorded_root = backup.get("install_root")
    if recorded_root is not None:
        if not isinstance(recorded_root, str) or not os.path.isabs(recorded_root):
            raise UpdateError("Backup install root is invalid")
        if root is not None and os.path.normcase(os.path.abspath(recorded_root)) != os.path.normcase(os.path.abspath(root)):
            raise UpdateError("Backup belongs to another install root")
    # Old schema-1 backups omit install_root; they still receive full entry,
    # content-hash and target-path checks before any restore writes.
    seen = set()
    total = 0
    for group in ("files", "delete", "added"):
        entries = backup.get(group)
        if not isinstance(entries, list) or len(entries) > MAX_FILES:
            raise UpdateError("Backup file lists are missing or invalid")
        for entry in entries:
            if not isinstance(entry, dict):
                raise UpdateError("Backup entry must be an object")
            rel = normalize_rel_path(entry.get("path"))
            if rel != entry.get("path") or is_protected_rel_path(rel) or rel.casefold() in seen:
                raise UpdateError("Backup path is unsafe or duplicated")
            seen.add(rel.casefold())
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise UpdateError("Backup entry hash is invalid")
            if group != "added":
                size = entry.get("size")
                if entry.get("existed") is not True or type(size) is not int or size < 0:
                    raise UpdateError("Backup restore entry is incomplete")
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise UpdateError("Backup content exceeds configured limits")
    return backup


def _pending_path(updates_path: str, state_file: str) -> str:
    path = _assert_no_reparse(os.path.join(updates_path, "pending_update.json"))
    if os.path.normcase(path) == os.path.normcase(os.path.abspath(state_file)):
        raise UpdateError("State path trùng journal cập nhật")
    return path


def _begin_pending(
    root: str, updates_path: str, state_file: str, backup_dir: str,
    action: str, operation_id: str, state_after: Optional[Dict[str, Any]] = None,
) -> None:
    path = _pending_path(updates_path, state_file)
    if os.path.exists(path):
        raise UpdateError("Còn cập nhật chưa phục hồi; không ghi đè journal")
    _write_json_atomic(path, {
        "schema": 1, "operation_id": operation_id, "action": action,
        "install_root": os.path.abspath(root), "state_file": os.path.abspath(state_file),
        "backup_dir": os.path.abspath(backup_dir),
        "backup_manifest_sha256": sha256_file(os.path.join(backup_dir, "backup.json")),
        "state_after": state_after,
    })


def _read_pending(root: str, updates_path: str, state_file: str) -> Optional[Dict[str, Any]]:
    path = _pending_path(updates_path, state_file)
    if not os.path.exists(path):
        return None
    pending = read_update_state(path)
    operation_id = pending.get("operation_id")
    if (pending.get("schema") != 1 or pending.get("action") not in ("apply", "rollback")
            or not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id)):
        raise UpdateError("Journal cập nhật hỏng; giữ nguyên để phục hồi thủ công")
    for key, expected in (("install_root", root), ("state_file", state_file)):
        recorded = pending.get(key)
        if (not isinstance(recorded, str)
                or os.path.normcase(os.path.abspath(recorded)) != os.path.normcase(os.path.abspath(expected))):
            raise UpdateError("Journal không thuộc thư mục cài đặt/state hiện tại")
    backup_dir = pending.get("backup_dir")
    if not isinstance(backup_dir, str) or not backup_dir:
        raise UpdateError("Journal thiếu backup")
    digest = pending.get("backup_manifest_sha256")
    if (not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
            or sha256_file(os.path.join(backup_dir, "backup.json")) != digest):
        raise UpdateError("Backup manifest không khớp journal")
    if pending["action"] == "rollback":
        state_after = pending.get("state_after")
        if (not isinstance(state_after, dict) or state_after.get("schema") != 1
                or state_after.get("last_operation_id") != operation_id
                or not isinstance(state_after.get("current_version"), str)):
            raise UpdateError("Journal rollback thiếu state hoàn tất")
        backup = _read_backup_manifest(backup_dir, root)
        if state_after["current_version"] != backup.get("from_version"):
            raise UpdateError("Journal rollback không khớp version backup")
    return pending


def _clear_pending(updates_path: str, state_file: str, operation_id: str) -> None:
    path = _pending_path(updates_path, state_file)
    if not os.path.exists(path):
        return
    if read_update_state(path).get("operation_id") != operation_id:
        raise UpdateError("Journal đã thay đổi; từ chối xóa")
    _remove_checked(path)


def _recover_interrupted_update(
    root: str, updates_path: str, state_file: str,
    server_check: Callable[[], Optional[Dict[str, Any]]],
    stop_server: bool, stop_server_fn: Callable[[], Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Resume only under update.lock; keep the journal until durable completion."""
    pending = _read_pending(root, updates_path, state_file)
    if pending is None:
        return None
    operation_id = pending["operation_id"]
    state = read_update_state(state_file)
    result: Dict[str, Any] = {}
    stopped = None
    if state.get("last_operation_id") == operation_id:
        action = "finish_committed_update"
    else:
        stopped = _ensure_server_stopped(server_check, stop_server, stop_server_fn)
        result = _apply_from_backup(pending["backup_dir"], root)
        if pending["action"] == "rollback":
            _write_json_atomic(state_file, pending["state_after"])
            action = "finish_interrupted_rollback"
        else:
            # Apply writes state only at the commit point, so the old state
            # still describes the restored files. Recovery is idempotent.
            action = "rollback_interrupted_apply"
    _clear_pending(updates_path, state_file, operation_id)
    return {
        "action": action, "operation": pending["action"], "operation_id": operation_id,
        "backup_dir": pending["backup_dir"], "server_stopped": stopped,
        **result,
    }


def _apply_from_backup(backup_dir: str, root: str) -> Dict[str, Any]:
    """Khôi phục theo backup manifest; verify toàn bộ trước khi ghi."""
    backup = _read_backup_manifest(backup_dir, root)
    files_dir = os.path.join(backup_dir, "files")

    restore_entries = []
    for entry in backup.get("files") or []:
        if not isinstance(entry, dict) or not entry.get("existed"):
            continue
        rel = normalize_rel_path(entry.get("path"))
        stored = _safe_install_path(files_dir, rel)
        if not os.path.isfile(stored):
            raise UpdateError(f"Backup thiếu file: {rel}")
        if os.path.getsize(stored) != entry["size"] or sha256_file(stored) != entry["sha256"]:
            raise UpdateError(f"Backup bị sửa (hash sai): {rel}")
        restore_entries.append((rel, stored, entry.get("sha256")))

    deleted_entries = []
    for entry in backup.get("delete") or []:
        if not isinstance(entry, dict) or not entry.get("existed"):
            continue
        rel = normalize_rel_path(entry.get("path"))
        stored = _safe_install_path(files_dir, rel)
        if (not os.path.isfile(stored) or os.path.getsize(stored) != entry["size"]
                or sha256_file(stored) != entry["sha256"]):
            raise UpdateError(f"Backup bị sửa hoặc thiếu file đã xoá: {rel}")
        deleted_entries.append((rel, stored, entry.get("sha256")))

    version_in_backup = any(
        rel.casefold() == "antidetect/__init__.py"
        for rel, _, _ in restore_entries + deleted_entries
    )
    version_source = files_dir if version_in_backup else root
    if read_installed_version(version_source) != backup["from_version"]:
        raise UpdateError("Backup cannot restore the declared previous version")

    # Validate every destination, including deletions, before restoring any file.
    added_entries = [
        entry for entry in (backup.get("added") or []) if isinstance(entry, dict)
    ]
    _preflight_targets(
        root,
        [rel for rel, _, _ in restore_entries + deleted_entries]
        + [normalize_rel_path(entry.get("path")) for entry in added_entries],
    )
    for rel, stored, digest in restore_entries + deleted_entries:
        _copy_verified(stored, _safe_install_path(root, rel), digest)

    kept = []
    removed_added = []
    for entry in backup.get("added") or []:
        if not isinstance(entry, dict):
            continue
        rel = normalize_rel_path(entry.get("path"))
        target = _safe_install_path(root, rel)
        if not os.path.isfile(target):
            continue
        if sha256_file(target) == entry.get("sha256"):
            _remove_checked(target)
            removed_added.append(rel)
        else:
            # File mới đã bị sửa sau update: giữ lại thay vì xoá mất dữ liệu.
            kept.append(rel)
    if read_installed_version(root) != backup["from_version"]:
        raise UpdateError("Restored install version does not match backup")
    return {
        "restored": [rel for rel, _, _ in restore_entries + deleted_entries],
        "removed_added": removed_added,
        "kept_modified_added": kept,
    }


def _prune_backups(backups_root: str, keep: int = BACKUP_KEEP) -> None:
    _assert_no_reparse(backups_root)
    if keep <= 0 or not os.path.isdir(backups_root):
        return
    entries = sorted(
        (
            os.path.join(backups_root, name)
            for name in os.listdir(backups_root)
            if os.path.isdir(os.path.join(backups_root, name))
        ),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    for old in entries[keep:]:
        if not os.path.isfile(os.path.join(old, "backup.json")):
            continue
        try:
            _remove_tree_checked(old)
        except (OSError, UpdateError) as exc:
            log.warning("Không xoá được backup cũ %s: %s", old, exc)


def apply_update(
    zip_path: str,
    install_root: Optional[str] = None,
    public_key: Any = None,
    allow_unsigned: bool = False,
    allow_downgrade: bool = False,
    require_authenticode: bool = False,
    state_path: Optional[str] = None,
    updates_dir: Optional[str] = None,
    server_check: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    stop_server: bool = False,
    stop_server_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    dry_run: bool = False,
    backup_keep: int = BACKUP_KEEP,
) -> Dict[str, Any]:
    from .storage import InterProcessLock

    root = _assert_no_reparse(install_root or _BASE_DIR)
    if not os.path.isdir(os.path.join(root, "antidetect")):
        raise UpdateError(f"Không phải thư mục cài đặt Antidetect: {root}")
    updates_path, state_file = _resolve_paths(root, state_path, updates_dir)
    _assert_no_reparse(updates_path)
    _assert_no_reparse(state_file)
    server_check = server_check or detect_running_server
    stop_server_fn = stop_server_fn or _default_stop_server

    def _verified(
        current_version: str,
    ) -> Tuple[Dict[str, Any], bytes, str, str]:
        manifest, raw_manifest, signature_state = _verify_archive(
            zip_path, public_key, allow_unsigned
        )
        target = manifest["version"]
        if (
            version_tuple(target) < version_tuple(current_version)
            and not allow_downgrade
        ):
            raise DowngradeError(
                f"Release {target} cũ hơn bản đang cài {current_version}. "
                "Thêm --allow-downgrade nếu thực sự muốn hạ cấp."
            )
        _preflight_targets(
            root, [entry["path"] for entry in manifest["files"]] + manifest["delete"]
        )
        return manifest, raw_manifest, signature_state, target

    if dry_run:
        if _read_pending(root, updates_path, state_file) is not None:
            raise UpdateError("Còn cập nhật bị gián đoạn; dry-run không thực hiện phục hồi")
        current_version = read_installed_version(root)
        manifest, raw_manifest, signature_state, target_version = _verified(
            current_version
        )
        return {
            "dry_run": True,
            "from_version": current_version,
            "to_version": target_version,
            "signature": signature_state,
            "files": [entry["path"] for entry in manifest["files"]],
            "delete": list(manifest["delete"]),
            "updates_dir": updates_path,
            "state_path": state_file,
            "server": server_check(),
        }

    _mkdir_checked(updates_path)
    _assert_no_reparse(os.path.join(updates_path, "update.lock"))
    lock = InterProcessLock(os.path.join(updates_path, "update.lock"), timeout=5.0)
    try:
        with lock:
            recovery = _recover_interrupted_update(
                root, updates_path, state_file, server_check, stop_server, stop_server_fn
            )
            # Đọc trong lock: một updater khác có thể vừa apply xong.
            current_version = read_installed_version(root)
            manifest, raw_manifest, signature_state, target_version = _verified(
                current_version
            )
            _assert_no_reparse(os.path.join(updates_path, "update.lock"))
            # State hỏng phải chặn trước khi ghi bất kỳ file nào.
            prior_state = read_update_state(state_file)
            stopped = _ensure_server_stopped(
                server_check, stop_server, stop_server_fn
            )
            staging = os.path.join(
                updates_path, "staging", f"{target_version}-{uuid.uuid4().hex[:8]}"
            )
            _mkdir_checked(staging)
            try:
                staged_files = _extract_payload(zip_path, manifest, staging)
                authenticode = _check_authenticode(
                    staged_files, require_authenticode
                )
                # Server có thể đã khởi động trong lúc giải nén.
                _ensure_server_stopped(server_check, False, stop_server_fn)

                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
                backup_dir = os.path.join(
                    updates_path,
                    "backups",
                    f"{stamp}-{current_version}-to-{target_version}-"
                    f"{uuid.uuid4().hex[:6]}",
                )
                backup_files_dir = os.path.join(backup_dir, "files")
                _mkdir_checked(backup_files_dir)

                file_entries = []
                for entry in manifest["files"]:
                    saved = _backup_file(root, entry["path"], backup_files_dir)
                    if saved is not None:
                        file_entries.append(saved)
                delete_entries = []
                for rel in manifest["delete"]:
                    saved = _backup_file(root, rel, backup_files_dir)
                    if saved is not None:
                        delete_entries.append(saved)
                added = [
                    {"path": entry["path"], "sha256": entry["sha256"]}
                    for entry in manifest["files"]
                    if not os.path.exists(
                        _safe_install_path(root, entry["path"])
                    )
                ]
                backup_manifest = {
                    "schema": 1,
                    "install_root": root,
                    "from_version": current_version,
                    "to_version": target_version,
                    "created_at": int(time.time()),
                    "release_manifest_sha256": sha256_bytes(raw_manifest),
                    "files": file_entries,
                    "delete": delete_entries,
                    "added": added,
                }
                _write_json_atomic(
                    os.path.join(backup_dir, "backup.json"), backup_manifest
                )

                operation_id = uuid.uuid4().hex
                _begin_pending(root, updates_path, state_file, backup_dir, "apply", operation_id)
                written = []
                try:
                    for entry in manifest["files"]:
                        source = _safe_install_path(staging, entry["path"])
                        target = _safe_install_path(root, entry["path"])
                        _copy_verified(source, target, entry["sha256"])
                        written.append(entry["path"])
                    for rel in manifest["delete"]:
                        target = _safe_install_path(root, rel)
                        if os.path.isdir(target):
                            raise UpdateError(
                                f"Delete trỏ vào thư mục, từ chối: {rel}"
                            )
                        if os.path.exists(target):
                            _remove_checked(target)

                    now = int(time.time())
                    history = list(prior_state.get("history") or [])[-19:]
                    history.append(
                        {
                            "action": "apply",
                            "from": current_version,
                            "to": target_version,
                            "at": now,
                        }
                    )
                    new_state = {
                        "schema": 1,
                        "last_operation_id": operation_id,
                        "current_version": target_version,
                        "previous_version": current_version,
                        "updated_at": now,
                        "release": {
                            "version": target_version,
                            "manifest_sha256": sha256_bytes(raw_manifest),
                            "zip": os.path.basename(zip_path),
                        },
                        "rollback": {
                            "backup_dir": backup_dir,
                            "backup_manifest_sha256": sha256_file(os.path.join(backup_dir, "backup.json")),
                            "from_version": current_version,
                            "to_version": target_version,
                            "created_at": now,
                        },
                        "history": history,
                    }
                    _write_json_atomic(state_file, new_state)
                except Exception as exc:
                    if read_update_state(state_file).get("last_operation_id") == operation_id:
                        raise UpdateError("Cập nhật đã commit; journal được giữ để hoàn tất cleanup") from exc
                    auto = {}
                    try:
                        auto = _apply_from_backup(backup_dir, root)
                        _clear_pending(updates_path, state_file, operation_id)
                    except Exception as rollback_exc:  # noqa: BLE001
                        raise UpdateError(
                            f"Cập nhật lỗi ({exc}) và tự rollback cũng lỗi: "
                            f"{rollback_exc}. Backup: {backup_dir}"
                        ) from exc
                    raise UpdateError(
                        f"Cập nhật lỗi ({exc}); đã tự rollback "
                        f"({len(auto.get('restored', []))} file)."
                    ) from exc
                _clear_pending(updates_path, state_file, operation_id)
            finally:
                _remove_tree_checked(staging)

            _prune_backups(os.path.join(updates_path, "backups"), backup_keep)
            return {
                "success": True,
                "recovery": recovery,
                "from_version": current_version,
                "to_version": target_version,
                "signature": signature_state,
                "authenticode": authenticode,
                "files_written": written,
                "files_deleted": [
                    rel
                    for rel in manifest["delete"]
                    if not os.path.exists(_safe_install_path(root, rel))
                ],
                "backup_dir": backup_dir,
                "state_path": state_file,
                "server_stopped": stopped,
                "restart_required": True,
            }
    except UpdateError:
        raise
    except TimeoutError as exc:
        raise UpdateError(
            "Không lấy được khóa cập nhật; thử lại sau."
        ) from exc
    except OSError as exc:
        raise UpdateError(f"Lỗi hệ thống khi cập nhật: {exc}") from exc


def _default_stop_server() -> Dict[str, Any]:
    from . import server_control

    return server_control.stop_server()


def rollback_update(
    install_root: Optional[str] = None,
    backup_dir: Optional[str] = None,
    state_path: Optional[str] = None,
    updates_dir: Optional[str] = None,
    server_check: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    stop_server: bool = False,
    stop_server_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    from .storage import InterProcessLock

    root = _assert_no_reparse(install_root or _BASE_DIR)
    updates_path, state_file = _resolve_paths(root, state_path, updates_dir)
    _assert_no_reparse(updates_path)
    _assert_no_reparse(state_file)
    server_check = server_check or detect_running_server
    stop_server_fn = stop_server_fn or _default_stop_server
    _mkdir_checked(updates_path)
    _assert_no_reparse(os.path.join(updates_path, "update.lock"))
    lock = InterProcessLock(os.path.join(updates_path, "update.lock"), timeout=5.0)
    try:
        with lock:
            recovery = _recover_interrupted_update(
                root, updates_path, state_file, server_check, stop_server, stop_server_fn
            )
            state = read_update_state(state_file)
            same_recovered_backup = bool(
                recovery and (
                    backup_dir is None
                    or os.path.normcase(os.path.abspath(backup_dir))
                    == os.path.normcase(os.path.abspath(recovery["backup_dir"]))
                )
            )
            if recovery and recovery["operation"] == "rollback" and same_recovered_backup:
                return {
                    "success": True, "from_version": state.get("previous_version"),
                    "to_version": state.get("current_version"),
                    "backup_dir": recovery["backup_dir"], "state_path": state_file,
                    "restored": recovery.get("restored", []),
                    "removed_added": recovery.get("removed_added", []),
                    "kept_modified_added": recovery.get("kept_modified_added", []),
                    "server_stopped": recovery["server_stopped"],
                    "restart_required": True, "recovery": recovery,
                }
            chosen = backup_dir
            if not chosen:
                rollback = state.get("rollback") or {}
                chosen = rollback.get("backup_dir")
            if not chosen:
                raise UpdateError(
                    "Không có bản backup rollback trong update_state.json. "
                    "Dùng --backup <thư mục> nếu biết đường dẫn."
                )
            chosen = _assert_no_reparse(chosen)
            if not os.path.isfile(os.path.join(chosen, "backup.json")):
                raise UpdateError(f"Thư mục backup không hợp lệ: {chosen}")
            backup = _read_backup_manifest(chosen, root)
            prior_rollback = state.get("rollback") or {}
            recorded_backup = prior_rollback.get("backup_dir")
            if (isinstance(recorded_backup, str)
                    and os.path.normcase(os.path.abspath(recorded_backup)) == os.path.normcase(chosen)
                    and prior_rollback.get("backup_manifest_sha256") is not None):
                if sha256_file(os.path.join(chosen, "backup.json")) != prior_rollback["backup_manifest_sha256"]:
                    raise UpdateError("Backup manifest no longer matches the committed update")

            current_version = read_installed_version(root)
            expected_current = backup.get("to_version")
            if not force and expected_current and (
                version_tuple(current_version) != version_tuple(expected_current)
            ):
                raise UpdateError(
                    f"Bản đang cài là {current_version}, backup dành cho "
                    f"{expected_current}. Rollback có thể ghi đè thay đổi mới; "
                    "thêm --force nếu chắc chắn."
                )

            stopped = _ensure_server_stopped(
                server_check, stop_server, stop_server_fn
            )
            from_version = backup.get("from_version") or "unknown"
            now = int(time.time())
            history = list(state.get("history") or [])[-19:]
            history.append(
                {
                    "action": "rollback",
                    "from": current_version,
                    "to": from_version,
                    "at": now,
                }
            )
            operation_id = uuid.uuid4().hex
            new_state = {
                "schema": 1,
                "last_operation_id": operation_id,
                "current_version": from_version,
                "previous_version": current_version,
                "updated_at": now,
                "release": state.get("release"),
                "rollback": None,
                "history": history,
            }
            _begin_pending(
                root, updates_path, state_file, chosen, "rollback", operation_id,
                state_after=new_state,
            )
            result = _apply_from_backup(chosen, root)
            _write_json_atomic(state_file, new_state)
            _clear_pending(updates_path, state_file, operation_id)
            return {
                "success": True,
                "recovery": recovery,
                "from_version": current_version,
                "to_version": from_version,
                "backup_dir": chosen,
                "restored": result["restored"],
                "removed_added": result["removed_added"],
                "kept_modified_added": result["kept_modified_added"],
                "state_path": state_file,
                "server_stopped": stopped,
                "restart_required": True,
            }
    except UpdateError:
        raise
    except TimeoutError as exc:
        raise UpdateError(
            "Không lấy được khóa cập nhật; thử lại sau."
        ) from exc
    except OSError as exc:
        raise UpdateError(f"Lỗi hệ thống khi rollback: {exc}") from exc
