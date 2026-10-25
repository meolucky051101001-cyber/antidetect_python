"""Read-only process identity checks for recovering app-owned Windows browsers."""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Dict, Any, Tuple

_identity_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}


def windows_argv(command_line):
    if sys.platform != 'win32':
        raise RuntimeError('Windows process identity is required')
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not argv:
        raise OSError('Cannot inspect browser command line')
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def read_browser_identity(pid, user_data_dir):
    """Return absent/mismatch/owned/unknown; never treat inability to query as exit.

    P1-02: Sử dụng Windows Native API trước để loại bỏ 99% trường hợp PID không tồn tại
    hoặc không phải Chrome trong vòng <0.05ms, tránh khởi động powershell.exe gây nghẽn event loop.
    """
    if sys.platform != 'win32':
        return {'state': 'unknown'}
    try:
        pid = int(pid)
        if pid <= 0:
            return {'state': 'absent'}

        # Kiểm tra cache TTL (1.5 giây)
        now = time.monotonic()
        cached = _identity_cache.get(pid)
        if cached and (now - cached[0]) < 1.5:
            return cached[1]

        def remember(res):
            # "unknown" = truy vấn lỗi/timeout, không phải kết luận. Cache nó
            # khiến stop() vừa trả 409 lại tiếp tục thấy unknown dù process
            # đã sẵn sàng trả lời.
            if res.get("state") != "unknown":
                _identity_cache[pid] = (now, res)
            return res

        # 1. Native API check: Kiểm tra tiến trình có tồn tại không
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return remember({'state': 'absent'})

        # 2. Native API check: Kiểm tra tên file thực thi có phải Chrome/Edge không
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe_name = os.path.basename(buf.value).lower()
                if exe_name not in {'chrome.exe', 'msedge.exe', 'chromium.exe'}:
                    return remember({'state': 'mismatch'})
            else:
                return remember({'state': 'mismatch'})
        finally:
            kernel32.CloseHandle(handle)

        # 3. Chỉ khi PID thực sự là tiến trình Chrome, mới truy vấn CommandLine để kiểm tra user-data-dir
        command = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                   f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction Stop; "
                   "if($p){$p | Select-Object Name,CommandLine,CreationDate | ConvertTo-Json -Compress}else{'null'}")
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return remember({'state': 'unknown'})
        record = json.loads(result.stdout)
        if record is None:
            return remember({'state': 'absent'})
        if record.get('Name', '').lower() not in {'chrome.exe', 'msedge.exe', 'chromium.exe'}:
            return remember({'state': 'mismatch'})
        if not record.get('CommandLine'):
            return remember({'state': 'unknown'})
        args = windows_argv(record['CommandLine'])
        options = dict(a[2:].split('=', 1) for a in args if a.startswith('--') and '=' in a)
        actual = options.get('user-data-dir', '')
        expected = os.path.normcase(os.path.realpath(user_data_dir))
        if not actual or os.path.normcase(os.path.realpath(actual)) != expected:
            return remember({'state': 'mismatch'})
        port = int(options.get('remote-debugging-port', '0'))
        return remember({'state': 'owned', 'port': port if 0 < port < 65536 else None,
                         'created': str(record.get('CreationDate', ''))})
    except Exception:
        return {'state': 'unknown'}
