"""Static fingerprint gate evaluated immediately before a profile is spawned.

The gate reuses the Health Center audit: no browser is launched and no network
request is made. Its job is to stop the launches that are already known to be
incoherent — a UA from another OS, a duplicate canvas/audio seed, a GPU that
cannot exist on the declared OS — not to judge the quality of a persona.

Every evaluation ends in one of three honest states:

* **healthy** — the audit ran and found nothing to report;
* **warning** — the audit ran and found issues, but none that justify
  blocking (or the caller chose warning-only enforcement);
* **blocked** — the audit ran and found at least one critical issue.

A fourth state, **unverified**, covers an audit failure. It never blocks: an
infrastructure error must not keep a user out of their own browser, but it also
must not be reported as a pass.

Nothing here rewrites the profile. A blocked launch can be retried with
``force=True`` for that one launch; the caller is responsible for making that
an explicit user action. Enforcement mode is stored locally in
``data/launch_gate.json`` and can be overridden by
``ANTIDETECT_LAUNCH_GATE=off|warn|block`` for operations.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .applog import get_logger
from .models import Profile
from .profile_audit import audit_profiles
from .storage import DATA_DIR, _replace_with_retry

log = get_logger("launch_gate")

MODES = ("off", "warn", "block")
DEFAULT_MODE = "block"
MODE_LABELS = {
    "off": "Tắt cổng",
    "warn": "Chỉ cảnh báo",
    "block": "Chặn lỗi nghiêm trọng",
}
MODE_DESCRIPTIONS = {
    "off": "Không kiểm tra fingerprint trước khi mở profile.",
    "warn": "Kiểm tra và trả cảnh báo, nhưng vẫn mở profile.",
    "block": "Chặn profile có lỗi nghiêm trọng; cảnh báo không chặn.",
}

SETTINGS_PATH = os.path.join(DATA_DIR, "launch_gate.json")
_ENV_NAME = "ANTIDETECT_LAUNCH_GATE"
_SETTINGS_LOCK = threading.Lock()
_REPORTED_SEVERITIES = ("critical", "warning")


@dataclass
class GateResult:
    profile_id: str
    profile_name: str
    mode: str
    status: str
    allowed: bool
    checked: bool
    critical_count: int = 0
    warning_count: int = 0
    issues: List[Dict[str, Any]] = field(default_factory=list)
    message: str = ""
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "mode": self.mode,
            "status": self.status,
            "allowed": self.allowed,
            "checked": self.checked,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "issues": list(self.issues),
            "message": self.message,
            "error": self.error,
        }


def _normalize_mode(mode: Optional[str]) -> str:
    value = (mode or "").strip().lower()
    if value not in MODES:
        raise ValueError(
            f"Chế độ cổng fingerprint không hợp lệ: {mode!r}. "
            f"Chỉ chấp nhận: {', '.join(MODES)}."
        )
    return value


def _env_mode() -> Optional[str]:
    value = (os.environ.get(_ENV_NAME) or "").strip().lower()
    return value if value in MODES else None


def _saved_mode() -> Optional[str]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("Không đọc được cấu hình cổng fingerprint: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    value = str(data.get("mode") or "").strip().lower()
    return value if value in MODES else None


def get_mode() -> str:
    """Effective mode: environment override, then saved setting, then block."""
    return _env_mode() or _saved_mode() or DEFAULT_MODE


def set_mode(mode: str) -> str:
    value = _normalize_mode(mode)
    with _SETTINGS_LOCK:
        directory = os.path.dirname(os.path.abspath(SETTINGS_PATH))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{SETTINGS_PATH}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"mode": value}, f)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, SETTINGS_PATH)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return value


def settings_snapshot() -> Dict[str, Any]:
    env_mode = _env_mode()
    return {
        "mode": env_mode or _saved_mode() or DEFAULT_MODE,
        "modes": list(MODES),
        "labels": dict(MODE_LABELS),
        "descriptions": dict(MODE_DESCRIPTIONS),
        "env_override": env_mode,
    }


def _off_result(profile: Profile, mode: str) -> GateResult:
    return GateResult(
        profile_id=profile.id,
        profile_name=profile.name,
        mode=mode,
        status="off",
        allowed=True,
        checked=False,
        message="Cổng fingerprint đang tắt.",
    )


def _unverified_result(profile: Profile, mode: str, exc: Exception) -> GateResult:
    error = str(exc)[:500]
    return GateResult(
        profile_id=profile.id,
        profile_name=profile.name,
        mode=mode,
        status="unverified",
        allowed=True,
        checked=False,
        warning_count=1,
        issues=[
            {
                "code": "gate_unavailable",
                "severity": "warning",
                "category": "fingerprint",
                "category_label": "Dấu vân tay",
                "title": "Không kiểm tra được fingerprint",
                "detail": (
                    "Cổng fingerprint gặp lỗi nên chưa thể xác minh profile. "
                    "Profile vẫn được mở, nhưng lần mở này chưa được kiểm tra."
                ),
                "fixable": False,
            }
        ],
        message="Cổng fingerprint không chạy được; profile vẫn mở nhưng chưa được xác minh.",
        error=error,
    )


def _result_from_entry(
    profile: Profile,
    entry: Dict[str, Any],
    mode: str,
    force: bool,
) -> GateResult:
    counts = entry.get("issue_counts") or {}
    critical = int(counts.get("critical", 0) or 0)
    warnings = int(counts.get("warning", 0) or 0)
    issues = [
        issue
        for issue in (entry.get("issues") or [])
        if issue.get("severity") in _REPORTED_SEVERITIES
    ]

    if critical:
        if mode == "warn":
            status, allowed = "warning", True
            message = (
                f"Fingerprint có {critical} lỗi nghiêm trọng. "
                "Chế độ chỉ cảnh báo nên profile vẫn được mở."
            )
        elif force:
            status, allowed = "overridden", True
            message = (
                f"Đã bỏ qua cổng fingerprint cho lần mở này "
                f"({critical} lỗi nghiêm trọng chưa xử lý)."
            )
        else:
            status, allowed = "blocked", False
            message = (
                f"Cổng fingerprint chặn khởi chạy: {critical} lỗi nghiêm trọng. "
                "Sửa trong Health Center hoặc chọn mở cho riêng lần này."
            )
    elif warnings:
        status, allowed = "warning", True
        message = (
            f"Fingerprint có {warnings} cảnh báo; không có lỗi nghiêm trọng. "
            "Profile vẫn được mở."
        )
    else:
        status, allowed = "healthy", True
        message = "Fingerprint không có lỗi nghiêm trọng."

    return GateResult(
        profile_id=profile.id,
        profile_name=profile.name,
        mode=mode,
        status=status,
        allowed=allowed,
        checked=True,
        critical_count=critical,
        warning_count=warnings,
        issues=issues,
        message=message,
    )


def _audit(profiles: Sequence[Profile], browser_paths: Optional[Dict[str, Optional[str]]]):
    return audit_profiles(profiles, browser_paths, include_fix_plan=False)


def evaluate_profile(
    profile: Profile,
    profiles: Optional[Sequence[Profile]] = None,
    browser_paths: Optional[Dict[str, Optional[str]]] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> GateResult:
    """Evaluate one profile against the full set (cross-profile rules need it)."""
    effective = _normalize_mode(mode if mode is not None else get_mode())
    if effective == "off":
        return _off_result(profile, effective)

    all_profiles = list(profiles) if profiles is not None else [profile]
    try:
        report = _audit(all_profiles, browser_paths)
        entry = next(
            (
                item
                for item in report.get("profiles", [])
                if item.get("profile_id") == profile.id
            ),
            None,
        )
        if entry is None:
            raise RuntimeError("Profile không có trong kết quả audit")
        return _result_from_entry(profile, entry, effective, force)
    except Exception as exc:
        log.warning("Cổng fingerprint không đánh giá được %s: %s", profile.id, exc)
        return _unverified_result(profile, effective, exc)


def evaluate_profiles(
    profiles: Sequence[Profile],
    browser_paths: Optional[Dict[str, Optional[str]]] = None,
    mode: Optional[str] = None,
) -> Dict[str, GateResult]:
    """Evaluate a batch with a single audit pass, for start-all/scheduler."""
    all_profiles = list(profiles)
    effective = _normalize_mode(mode if mode is not None else get_mode())
    if effective == "off":
        return {p.id: _off_result(p, effective) for p in all_profiles}
    if not all_profiles:
        return {}

    try:
        report = _audit(all_profiles, browser_paths)
    except Exception as exc:
        log.warning("Cổng fingerprint không đánh giá được batch: %s", exc)
        return {
            p.id: _unverified_result(p, effective, exc) for p in all_profiles
        }

    entries = {
        item.get("profile_id"): item for item in report.get("profiles", [])
    }
    results: Dict[str, GateResult] = {}
    for profile in all_profiles:
        entry = entries.get(profile.id)
        if entry is None:
            results[profile.id] = _unverified_result(
                profile, effective, RuntimeError("Profile không có trong kết quả audit")
            )
        else:
            results[profile.id] = _result_from_entry(
                profile, entry, effective, force=False
            )
    return results
