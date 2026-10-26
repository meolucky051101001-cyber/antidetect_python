"""Phát hiện trình duyệt trên host, cấp cổng CDP và cờ WebRTC."""
import os
import socket
import sys
import threading
from typing import List, Optional

from .models import Profile

# Browser pin (Giai đoạn 2): chrome-for-testing tải về nằm ở đây, ngoài data/.
BROWSERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browsers"
)


def find_browser_executable() -> str:
    """Tự động tìm kiếm đường dẫn Google Chrome hoặc Microsoft Edge trên máy."""
    candidates = []

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get(
            "ProgramFiles(x86)", "C:\\Program Files (x86)"
        )

        candidates = [
            os.path.join(prog_files, "Google\\Chrome\\Application\\chrome.exe"),
            os.path.join(
                prog_files_x86, "Google\\Chrome\\Application\\chrome.exe"
            ),
            os.path.join(
                local_app_data, "Google\\Chrome\\Application\\chrome.exe"
            ),
            os.path.join(
                prog_files_x86, "Microsoft\\Edge\\Application\\msedge.exe"
            ),
            os.path.join(prog_files, "Microsoft\\Edge\\Application\\msedge.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

    for path in candidates:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        "Không tìm thấy Google Chrome hoặc Microsoft Edge trên máy!"
    )


_PORT_LOCK = threading.Lock()
_RESERVED_PORTS: set = set()


def get_free_port(exclude_ports: Optional[set] = None) -> int:
    """Find and reserve a loopback port for a debugging endpoint.

    Binding and closing a socket is inherently racy, so keep the chosen port
    reserved in-process until the browser is stopped. This prevents two
    concurrent launches from receiving the same port.
    """
    exclude = set(exclude_ports or set())
    with _PORT_LOCK:
        for _ in range(50):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                candidate = s.getsockname()[1]
            if candidate not in exclude and candidate not in _RESERVED_PORTS and candidate > 1024:
                _RESERVED_PORTS.add(candidate)
                return candidate
        raise RuntimeError("Không tìm được cổng remote debugging còn trống.")


def release_port(port: Optional[int]) -> None:
    if not port:
        return
    with _PORT_LOCK:
        _RESERVED_PORTS.discard(int(port))


def build_webrtc_flags(profile: Profile, fp) -> List[str]:
    """Return WebRTC flags appropriate for the profile mode and proxy state."""
    if not (profile.proxy and profile.proxy.enabled and profile.proxy.host):
        return []
    mode = getattr(fp, "webrtc_mode", "disable_non_proxied")
    if mode in ("disable_non_proxied", "block"):
        # Chrome 155 no longer honours the force switch on its own (the
        # preference is parsed, but STUN still leaves through the host's UDP
        # path).  The pref-style switch below is what actually blocks
        # non-proxied UDP; keep both for older Chromium builds.
        return [
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--enforce-webrtc-ip-permission-check",
        ]
    return []
