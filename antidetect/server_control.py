import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ANTIDETECT_DATA_DIR") or os.path.join(BASE_DIR, "data")
PID_FILE = os.path.join(DATA_DIR, "server.pid")

_DETAILS_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}


def _safe_log(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            print(message.encode("ascii", "backslashreplace").decode("ascii"))
        except Exception:
            pass


def get_process_image_name(pid: int) -> Optional[str]:
    """Return the executable path for a PID, or None if it does not exist."""
    if not pid:
        return None
    if sys.platform != "win32":
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except Exception:
            return None

    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(2048)
            size = wintypes.DWORD(2048)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return buf.value
            return None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return None


def get_process_command_line(pid: int) -> Optional[str]:
    """Return a process command line without requiring admin rights."""
    if not pid:
        return None
    if sys.platform != "win32":
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except Exception:
            return None

    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" "
        "-ErrorAction SilentlyContinue; "
        "if ($p) { $p | Select-Object CommandLine,CreationDate | "
        "ConvertTo-Json -Compress } else { 'null' }" % int(pid)
    )
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (result.stdout or "").strip()
        if not text or text == "null":
            return None
        data = json.loads(text)
        if isinstance(data, list):
            data = data[0] if data else {}
        return data.get("CommandLine") or None
    except Exception:
        return None


def get_process_details(pid: int) -> Optional[Dict[str, Any]]:
    """Return exe/cmdline/creation time with a short cache for repeated checks."""
    if not pid:
        return None
    now = time.monotonic()
    cached = _DETAILS_CACHE.get(int(pid))
    if cached and now - cached[0] < 2.0:
        return dict(cached[1])

    exe = get_process_image_name(pid)
    if not exe:
        return None
    command_line = get_process_command_line(pid)
    if command_line is None:
        return {"exe": exe, "cmdline": "", "created": ""}

    created = ""
    if sys.platform == "win32":
        # The command-line helper already queried CIM; query once more only for
        # CreationDate. This keeps the fast path compatible with tests that
        # monkeypatch get_process_command_line().
        script = (
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" "
            "-ErrorAction SilentlyContinue; "
            "if ($p) { [string]$p.CreationDate } else { '' }" % int(pid)
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            created = (result.stdout or "").strip()
        except Exception:
            created = ""

    details = {"exe": exe, "cmdline": command_line, "created": created}
    _DETAILS_CACHE[int(pid)] = (now, details)
    return dict(details)


def _cmdline_digest(command_line: str) -> str:
    normalized = " ".join((command_line or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def read_server_info() -> Optional[Dict[str, Any]]:
    """Read data/server.pid (JSON or legacy plain PID)."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
            if isinstance(data, int) and not isinstance(data, bool) and data > 0:
                return {"pid": data, "port": 8000, "legacy": True}
        except json.JSONDecodeError:
            if content.isdigit():
                return {"pid": int(content), "port": 8000, "legacy": True}
    except Exception as e:
        _safe_log(f"[ServerControl] Lỗi đọc server.pid: {e}")
    return None


def write_server_info(pid: int, port: int, instance_id: str):
    """Persist PID plus a command-line digest and process start time."""
    details = get_process_details(pid) or {}
    command_line = details.get("cmdline") or ""
    info = {
        "pid": int(pid),
        "port": int(port),
        "instance_id": instance_id,
        "created_at": int(time.time()),
        "process_created": details.get("created") or "",
        "cmdline_digest": _cmdline_digest(command_line) if command_line else "",
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = f"{PID_FILE}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, PID_FILE)


def remove_server_info():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass


def verify_server_identity(info: Dict[str, Any]) -> bool:
    """Fail-closed identity check before any kill.

    Requires: live process, a Python executable, a command line containing
    run.py, a matching command-line digest (when recorded), and a matching
    process creation time (when recorded). This protects against PID reuse.
    """
    pid = info.get("pid")
    if not pid:
        return False

    details = get_process_details(int(pid))
    if not details:
        return False

    exe_name = os.path.basename(details.get("exe") or "").lower()
    if not (exe_name.startswith("python") and exe_name.endswith(".exe")):
        if sys.platform != "win32":
            if "python" not in exe_name:
                return False
        else:
            return False

    command_line = details.get("cmdline") or ""
    command_lower = command_line.lower()
    if "run.py" not in command_lower:
        return False

    stored_digest = info.get("cmdline_digest")
    if stored_digest:
        if _cmdline_digest(command_line) != stored_digest:
            return False
    else:
        # Legacy PID files cannot prove the full command line; run.py is the
        # minimum acceptable evidence. Prefer refusing if it is absent.
        if not command_lower.strip():
            return False

    stored_created = info.get("process_created")
    if stored_created and details.get("created"):
        if str(stored_created) != str(details.get("created")):
            return False

    return True


def check_server_health(port: int = 8000) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"User-Agent": "AntidetectHealthCheck"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not get_process_image_name(pid):
            return True
        time.sleep(0.3)
    return not bool(get_process_image_name(pid))


def stop_server(timeout: float = 8.0) -> Dict[str, Any]:
    """Stop the app server without ever orphaning browsers.

    A 409 from /api/system/shutdown means profiles are still running: the
    server must stay alive and the caller must stop profiles first. Only a
    verified server process is ever force-killed, and server.pid is removed
    only after the process is confirmed gone.
    """
    info = read_server_info()
    if not info:
        return {"success": True, "message": "Không tìm thấy server.pid, máy chủ không chạy."}

    pid = int(info.get("pid") or 0)
    port = info.get("port", 8000)
    expected_instance_id = info.get("instance_id")

    if not verify_server_identity(info):
        # Never kill an unverified process. If it is still alive, keep the PID
        # file so the operator can inspect/retry; removing it here would hide a
        # live server when PowerShell/CIM is temporarily unavailable.
        if get_process_details(pid) is not None:
            _safe_log(
                f"[ServerControl] PID {pid} còn sống nhưng không xác minh được là "
                "Antidetect run.py. Giữ server.pid, không can thiệp tiến trình."
            )
            return {
                "success": False,
                "message": (
                    f"PID {pid} còn sống nhưng không xác minh được là server "
                    "Antidetect; giữ server.pid để kiểm tra thủ công."
                ),
            }
        # PID is absent: this is a stale record, safe to clean up.
        _safe_log(
            f"[ServerControl] PID {pid} không còn tồn tại. Đã dọn server.pid cũ."
        )
        remove_server_info()
        return {
            "success": True,
            "message": "Đã dọn server.pid cũ; tiến trình không còn tồn tại.",
        }

    health = check_server_health(port)
    if health and health.get("application") == "antidetect-python":
        if expected_instance_id and health.get("instance_id") != expected_instance_id:
            return {
                "success": False,
                "message": (
                    f"Cổng {port} đang thuộc instance khác "
                    f"({health.get('instance_id')}); không kill PID {pid}."
                ),
            }

        try:
            from .runtime_auth import read_runtime
            auth_data = read_runtime() or {}
            key = auth_data.get("access_key", "")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/system/shutdown",
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status in (200, 202):
                    _safe_log(f"[ServerControl] Đã gửi lệnh tắt máy chủ qua API cổng {port}.")
                    if _wait_for_process_exit(pid, timeout):
                        remove_server_info()
                        return {"success": True, "message": "Máy chủ đã tắt an toàn qua API."}
                    # Shutdown was accepted but process is hung. Verified kill
                    # is allowed below.
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return {
                    "success": False,
                    "message": (
                        "Máy chủ từ chối tắt vì vẫn còn profile đang chạy. "
                        "Không force-kill để tránh bỏ lại Chrome mồ côi. "
                        "Hãy dừng tất cả profile trước."
                    ),
                }
            _safe_log(
                f"[ServerControl] API shutdown trả HTTP {e.code}; không force-kill "
                "khi server vẫn phản hồi để tránh mất dữ liệu profile."
            )
            return {
                "success": False,
                "message": f"Máy chủ phản hồi HTTP {e.code}; không force-kill.",
            }
        except Exception as e:
            # Network failure with no health response: fallback is allowed
            # after re-verifying identity, because the HTTP control path is dead.
            _safe_log(
                f"[ServerControl] Gọi API shutdown thất bại "
                f"({type(e).__name__}); kiểm tra lại identity trước khi fallback..."
            )

    # Fallback: only for a verified server process whose HTTP control path is
    # unreachable (or which accepted shutdown but stayed hung).
    _DETAILS_CACHE.pop(pid, None)
    if verify_server_identity(info):
        _safe_log(f"[ServerControl] Đang dừng tiến trình Python hợp lệ (PID: {pid})...")
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            else:
                os.kill(pid, 15)
        except Exception as e:
            _safe_log(f"[ServerControl] Lỗi khi dừng PID {pid}: {type(e).__name__}: {e}")

    if _wait_for_process_exit(pid, timeout):
        remove_server_info()
        return {"success": True, "message": f"Đã dừng máy chủ PID {pid} thành công."}

    return {
        "success": False,
        "message": (
            f"Không xác nhận được PID {pid} đã dừng; giữ server.pid để kiểm tra lại."
        ),
    }


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "stop":
        res = stop_server()
        print(res.get("message", "Hoàn tất."))
        sys.exit(0 if res.get("success") else 1)
    elif action == "status":
        info = read_server_info()
        if info and verify_server_identity(info):
            health = check_server_health(info.get("port", 8000))
            print(
                f"Máy chủ đang chạy (PID: {info.get('pid')}, "
                f"Port: {info.get('port')}, Health: {bool(health)})"
            )
        else:
            print("Máy chủ không chạy.")
    else:
        print("Sử dụng: python -m antidetect.server_control [status|stop]")
