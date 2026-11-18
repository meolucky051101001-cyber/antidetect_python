"""Local-only SHA-256 custom C++ browser core registry and launch flags.

Supports C++-patched antidetect Chromium cores (such as Fingerprint-Chromium)
without weakening default Google/Microsoft Authenticode verification:
1. Custom binaries are trusted ONLY when explicitly pinned by full SHA-256 and
   canonical local path in ``browsers/trusted_cores.json`` (outside ``data/``).
2. Profile imports (``prepare_import_profile``) cannot set ``browser_path`` or
   modify ``trusted_cores.json``.
3. When ``engine == "fingerprint-chromium"``, native C++ seed/platform/CPU/TZ
   flags are emitted at launch so Canvas, WebGL, Audio, Fonts, and ClientRects
   are separated in compiled C++ with zero JavaScript prototype wrappers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from .browser_env import BROWSERS_DIR
from .browser_trust import (
    BrowserTrustError,
    _CUSTOM_ALLOWED_BASENAMES,
    _CUSTOM_ENGINES,
    _checked_local_path_syntax,
    _locked_file,
    _reject_reparse_ancestors,
    _validate_pe,
    verify_browser_executable,
)
from .fingerprint import get_version_from_browser_path
from .models import FingerprintConfig

DEFAULT_CUSTOM_CORES_MANIFEST = os.path.join(BROWSERS_DIR, "trusted_cores.json")


def custom_cores_manifest_path() -> str:
    env_path = (os.environ.get("ANTIDETECT_CUSTOM_CORES_MANIFEST") or "").strip()
    return os.path.abspath(env_path) if env_path else DEFAULT_CUSTOM_CORES_MANIFEST


def build_cpp_engine_flags(engine: Optional[str], fp: FingerprintConfig) -> List[str]:
    """Return CLI flags for a verified C++ antidetect Chromium engine."""
    if engine != "fingerprint-chromium":
        return []
    flags: List[str] = []
    disabled = {str(g).strip().lower() for g in (getattr(fp, "stealth_disable", None) or [])}
    if "canvas" not in disabled:
        raw_seed = int(getattr(fp, "canvas_noise_seed", 0) or 0) & 0xFFFFFFFF
        seed = raw_seed if raw_seed > 0 else 12345
        flags.append(f"--fingerprint={seed}")
    fp_os = (getattr(fp, "os", "windows") or "windows").strip().lower()
    platform_arg = "macos" if fp_os == "macos" else ("linux" if fp_os == "linux" else "windows")
    flags.append(f"--fingerprint-platform={platform_arg}")
    cores = max(1, min(128, int(getattr(fp, "hardware_concurrency", 8) or 8)))
    flags.append(f"--fingerprint-hardware-concurrency={cores}")
    tz = (getattr(fp, "timezone", "") or "").strip()
    if tz and re.fullmatch(r"[A-Za-z0-9_./+-]{1,64}", tz):
        flags.append(f"--timezone={tz}")
    return flags


def _read_manifest_or_empty(manifest_path: str) -> Dict[str, Any]:
    manifest_abs = _checked_local_path_syntax(manifest_path, "Manifest")
    exists = _reject_reparse_ancestors(manifest_abs, allow_missing_leaf=True, label="manifest")
    if not exists or not os.path.exists(manifest_abs):
        return {"schema": 1, "cores": []}
    with open(manifest_abs, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("cores"), list):
        raise BrowserTrustError("Cấu trúc manifest custom browser core không hợp lệ.")
    return data


def _write_manifest_atomic(manifest_path: str, payload: Dict[str, Any]) -> None:
    manifest_abs = _checked_local_path_syntax(manifest_path, "Manifest")
    _reject_reparse_ancestors(manifest_abs, allow_missing_leaf=True, label="manifest")
    parent = os.path.dirname(manifest_abs)
    os.makedirs(parent, exist_ok=True)
    _reject_reparse_ancestors(parent, allow_missing_leaf=False, label="manifest")
    tmp = f"{manifest_abs}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, manifest_abs)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def pin_custom_core(
    exe_path: str,
    *,
    engine: str = "fingerprint-chromium",
    version: Optional[str] = None,
    product: Optional[str] = None,
    manifest_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a local PE browser binary and pin its exact SHA-256 locally."""
    if engine not in _CUSTOM_ENGINES:
        raise ValueError(f"Engine phải thuộc {sorted(_CUSTOM_ENGINES)}.")
    target_manifest = manifest_path or custom_cores_manifest_path()
    checked = _checked_local_path_syntax(exe_path, "Custom browser")
    _reject_reparse_ancestors(checked, allow_missing_leaf=False, label="browser")
    checked = os.path.realpath(checked)
    if os.path.basename(checked).lower() not in _CUSTOM_ALLOWED_BASENAMES:
        raise BrowserTrustError("Tên file custom browser core phải là chrome.exe, chromium.exe hoặc msedge.exe.")
    with _locked_file(checked) as (stream, _handle, _identity):
        _validate_pe(stream)
        stream.seek(0)
        hasher = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
        digest = hasher.hexdigest()

    resolved_version = (version or "").strip()
    if not resolved_version:
        detected = get_version_from_browser_path(checked)
        if detected:
            resolved_version = detected[1]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", resolved_version):
        raise ValueError("Cần chỉ định --version dạng x.y.z.w (ví dụ 142.0.7444.59) khi thư mục không chứa version.")
    resolved_product = (product or ("Fingerprint Chromium" if engine == "fingerprint-chromium" else "Custom Chromium")).strip()
    if not 1 <= len(resolved_product) <= 120:
        raise ValueError("Tên product phải dài 1..120 ký tự.")

    manifest = _read_manifest_or_empty(target_manifest)
    norm_checked = os.path.normcase(checked)
    retained = [
        item for item in manifest["cores"]
        if isinstance(item, dict)
        and os.path.normcase(os.path.abspath(str(item.get("path") or ""))) != norm_checked
    ]
    entry = {
        "path": checked,
        "sha256": digest,
        "engine": engine,
        "version": resolved_version,
        "product": resolved_product,
    }
    retained.append(entry)
    _write_manifest_atomic(target_manifest, {"schema": 1, "cores": retained})
    attestation = verify_browser_executable(checked, custom_manifest_path=target_manifest)
    return {
        "path": attestation.path,
        "sha256": attestation.sha256,
        "engine": attestation.engine,
        "version": attestation.version,
        "product": attestation.product,
        "manifest_path": os.path.abspath(target_manifest),
    }


def list_custom_cores(*, manifest_path: Optional[str] = None) -> List[Dict[str, Any]]:
    target_manifest = manifest_path or custom_cores_manifest_path()
    manifest = _read_manifest_or_empty(target_manifest)
    return list(manifest["cores"])


def remove_custom_core(exe_path: str, *, manifest_path: Optional[str] = None) -> bool:
    target_manifest = manifest_path or custom_cores_manifest_path()
    manifest = _read_manifest_or_empty(target_manifest)
    norm_target = os.path.normcase(os.path.abspath(exe_path))
    before = len(manifest["cores"])
    retained = [
        item for item in manifest["cores"]
        if isinstance(item, dict)
        and os.path.normcase(os.path.abspath(str(item.get("path") or ""))) != norm_target
    ]
    if len(retained) == before:
        return False
    _write_manifest_atomic(target_manifest, {"schema": 1, "cores": retained})
    return True
