"""Shared profile deletion transaction for API, CLI and bridge callers."""
from typing import Any, Dict

from .profile_locks import ProfileLockTimeout
from .storage import ProfileDeletionError


def stop_and_delete(manager: Any, launcher: Any, profile_id: str) -> Dict[str, Any]:
    """Hold the same lifecycle lock across stop and deletion in one thread."""
    manager._validate_profile_id(profile_id)
    try:
        with manager._profile_locks.acquire(profile_id, timeout=4.0):
            if manager.get_profile(profile_id) is None:
                return {"success": False, "code": "PROFILE_NOT_FOUND", "error": "Profile không tồn tại"}
            try:
                stopped = launcher.stop(profile_id)
            except Exception:
                return {"success": False, "code": "STOP_FAILED", "error": "Không xác nhận được trình duyệt đã dừng; dữ liệu được giữ nguyên."}
            if not isinstance(stopped, dict) or not stopped.get("success"):
                return {
                    "success": False,
                    "code": (stopped or {}).get("code", "STOP_FAILED") if isinstance(stopped, dict) else "STOP_FAILED",
                    "error": (stopped or {}).get("error", "Chưa dừng được profile.") if isinstance(stopped, dict) else "Chưa dừng được profile.",
                }
            # Recheck while holding the same lock: a success result must not
            # hide a replacement runtime or an inconclusive process query.
            is_running = getattr(launcher, "is_running", None)
            if callable(is_running):
                try:
                    still_running = is_running(profile_id)
                except Exception:
                    still_running = True
                if still_running:
                    return {"success": False, "code": "STOP_FAILED", "error": "Chưa xác minh được profile đã dừng; dữ liệu được giữ nguyên."}
            # Every cooperating launch uses this same lock and must reload
            # metadata after acquiring it.
            directory_in_use = getattr(launcher, "profile_data_in_use", None)
            if callable(directory_in_use):
                try:
                    busy = directory_in_use(profile_id)
                except Exception:
                    return {"success": False, "code": "PROCESS_STATE_UNVERIFIED", "error": "Chưa xác minh được thư mục profile đã rảnh; dữ liệu được giữ nguyên."}
                if busy:
                    return {"success": False, "code": "PROFILE_BUSY", "error": "Thư mục profile vẫn đang được trình duyệt sử dụng."}
            deleted = manager.delete_profile(profile_id)
            return {
                "success": bool(deleted),
                "code": None if deleted else "PROFILE_NOT_FOUND",
                "message": "Đã xóa profile" if deleted else "Profile không tồn tại",
            }
    except ProfileLockTimeout:
        return {"success": False, "code": "PROFILE_BUSY", "error": "Profile đang được thao tác; hãy thử lại."}
    except ProfileDeletionError as exc:
        return {"success": False, "code": "DELETE_FAILED", "error": str(exc)}
