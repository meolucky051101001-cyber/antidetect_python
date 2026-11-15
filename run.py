import os
import sys
import time
import json
import urllib.request
import webbrowser
import threading
import subprocess
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HAS_CONSOLE = sys.stdout is not None and sys.stderr is not None

# Add current directory to sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Fix pythonw.exe crash: redirect stdout and stderr to data/app.log if None
DATA_DIR = os.environ.get("ANTIDETECT_DATA_DIR") or os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "app.log")

if sys.stdout is None:
    try:
        sys.stdout = open(LOG_FILE, "a", encoding="utf-8")
    except Exception:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

if sys.stderr is None:
    try:
        sys.stderr = open(LOG_FILE, "a", encoding="utf-8")
    except Exception:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

from antidetect.launcher import find_browser_executable
from antidetect.server_control import write_server_info, remove_server_info
from antidetect.applog import setup_logging

setup_logging(LOG_FILE, console=_HAS_CONSOLE)


def get_app_port() -> int:
    """Lấy cổng chạy ứng dụng từ đối số CLI (--port) hoặc biến môi trường PORT, mặc định 8000."""
    for i, arg in enumerate(sys.argv):
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg == "--port" and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    try:
        return int(os.environ.get("PORT", "8000"))
    except ValueError:
        return 8000


def is_server_running(port: int = 8000) -> bool:
    """Probe sức khỏe máy chủ HTTP loopback và kiểm tra chữ ký ứng dụng."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"User-Agent": "AntidetectHealthCheck"},
        )
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("application") == "antidetect-python"
            return False
    except Exception:
        return False


def read_access_key():
    try:
        from antidetect.runtime_auth import read_runtime
        record = read_runtime() or {}
        return record.get("access_key")
    except Exception:
        return None


def request_unlock_code_from_server(port: int = 8000) -> Optional[str]:
    """Ask the already-running server for a one-time code.

    This is the only path a second instance may use: importing web.app in the
    second process would create a code in the wrong process and return 401.
    """
    access_key = read_access_key()
    if not access_key:
        return None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/auth/create-unlock-code",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                code = data.get("unlock_code")
                return code if isinstance(code, str) and code else None
    except Exception:
        return None
    return None


def get_unlock_code_for_ui(
    port: int = 8000, allow_local: bool = False
) -> Optional[str]:
    """Get an OTC from the running server, optionally falling back locally.

    ``allow_local`` is only true inside the server process (after startup).
    A second instance must never mint a local code: the server would reject it.
    """
    code = request_unlock_code_from_server(port)
    if code:
        return code
    if allow_local:
        try:
            from web.app import create_unlock_code
            return create_unlock_code(ttl_seconds=60)
        except Exception:
            pass
    return None


def open_ui_now(port: int = 8000, allow_local: bool = False):
    """Mở giao diện Antidetect với mã mở khóa 1 lần (One-Time Code)."""
    if os.environ.get("ANTIDETECT_NO_BROWSER", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        return

    base_url = f"http://127.0.0.1:{port}"
    # P0-06: Khóa dài hạn KHÔNG BAO GIỜ truyền qua dòng lệnh!
    # Chỉ truyền mã mở khóa tạm thời có hiệu lực 60s và tự hủy ngay sau khi dùng.
    code = get_unlock_code_for_ui(port, allow_local=allow_local)
    url = f"{base_url}/unlock#{code}" if code else f"{base_url}/unlock"

    opened = False
    try:
        browser_path = find_browser_executable()
        subprocess.Popen(
            [browser_path, f"--app={url}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        opened = True
    except Exception:
        pass

    if not opened:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def wait_and_open_ui(port: int = 8000):
    for _ in range(30):
        time.sleep(0.5)
        if is_server_running(port):
            break
    # We are the server process here, so a local fallback is safe and useful
    # if the loopback HTTP request races with startup.
    open_ui_now(port, allow_local=True)


if __name__ == "__main__":
    wrote_pid = False
    try:
        port = get_app_port()
        # Single-instance: Nếu server đã chạy từ trước, chỉ cần bật lại cửa sổ giao diện ngay lập tức
        if is_server_running(port):
            print(f"[Run] Máy chủ Antidetect đã đang chạy trên cổng {port}. Đang mở lại cửa sổ giao diện...")
            open_ui_now(port)
            sys.exit(0)

        from web.app import app
        import uvicorn

        # Ghi structured JSON server.pid
        write_server_info(os.getpid(), port, app.state.instance_id)
        wrote_pid = True

        # Khởi động mở giao diện ứng dụng song song khi server sẵn sàng
        threading.Thread(target=wait_and_open_ui, args=(port,), daemon=True).start()

        # Gán app.state.server để POST /api/system/shutdown tắt sạch sẽ
        config = uvicorn.Config(app, host="127.0.0.1", port=port, reload=False, log_level="info")
        server = uvicorn.Server(config)
        app.state.server = server
        server.run()
    except Exception:
        import traceback
        with open(os.path.join(DATA_DIR, "error_trace.log"), "w", encoding="utf-8") as _f:
            traceback.print_exc(file=_f)
        sys.exit(1)
    finally:
        if wrote_pid:
            try:
                remove_server_info(expected_instance_id=app.state.instance_id)
            except Exception:
                pass
