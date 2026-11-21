"""Process identity queries and termination bound to a Windows process handle."""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any, Dict, Tuple

import psutil

_identity_cache: Dict[Tuple[int, str], Tuple[float, Dict[str, Any]]] = {}
_BROWSER_NAMES = {"chrome.exe", "msedge.exe", "chromium.exe"}
_QUERY = 0x1000
_TERMINATE = 0x0001
_STILL_ACTIVE = 259


def windows_argv(command_line):
    if sys.platform != "win32":
        raise RuntimeError("Windows process identity is required")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not argv:
        raise OSError("Cannot inspect browser command line")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


def _kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "QueryFullProcessImageNameW": (
            [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
            wintypes.BOOL,
        ),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes = args
        getattr(api, name).restype = result
    return api


def invalidate_identity(pid=None):
    if pid is not None:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return
    for key in list(_identity_cache):
        if pid is None or key[0] == pid:
            _identity_cache.pop(key, None)


def _options(args):
    result = {}
    for index, arg in enumerate(args):
        if not arg.startswith("--"):
            continue
        if "=" in arg:
            key, value = arg[2:].split("=", 1)
            result[key] = value
        elif index + 1 < len(args) and not args[index + 1].startswith("--"):
            result[arg[2:]] = args[index + 1]
    return result


def _handle_snapshot(api, handle):
    status = wintypes.DWORD()
    if not api.GetExitCodeProcess(handle, ctypes.byref(status)):
        return {"state": "unknown", "reason": "exit_query_failed"}
    if status.value != _STILL_ACTIVE:
        return {"state": "absent", "reason": "process_exited"}
    buf = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buf))
    if not api.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return {"state": "unknown", "reason": "image_query_failed"}
    if os.path.basename(buf.value).lower() not in _BROWSER_NAMES:
        return {"state": "mismatch", "reason": "executable_mismatch"}
    times = [wintypes.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        return {"state": "unknown", "reason": "birth_query_failed"}
    birth = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return {"state": "candidate", "birth_ticks": birth}


def _query_record(pid):
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction Stop; "
        "if($p){$p | Select-Object Name,CommandLine,CreationDate | ConvertTo-Json -Compress}else{'null'}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise OSError("process query failed")
    return json.loads(result.stdout)


def _cim_creation_date(ticks):
    return f"/Date({(ticks - 116444736000000000) // 10000})/"


def _native_record(pid, birth_ticks):
    try:
        process = psutil.Process(pid)
        return {
            "Name": process.name(),
            "Argv": process.cmdline(),
            "CreationDate": _cim_creation_date(birth_ticks),
        }
    except (psutil.Error, OSError):
        return None


def _identity_on_handle(api, handle, pid, expected_dir):
    before = _handle_snapshot(api, handle)
    if before["state"] != "candidate":
        return before
    record = _native_record(pid, before["birth_ticks"])
    if record is None:
        record = _query_record(pid)
    # Keep the original handle open throughout the command-line query. A
    # process that exits during CIM cannot lend its identity to a reused PID.
    after = _handle_snapshot(api, handle)
    if after["state"] != "candidate":
        return after
    if before["birth_ticks"] != after["birth_ticks"]:
        return {"state": "unknown", "reason": "process_changed"}
    if not isinstance(record, dict) or not record.get("CreationDate"):
        return {"state": "unknown", "reason": "incomplete_process_query"}
    argv = record.get("Argv")
    command_line = record.get("CommandLine")
    if not (isinstance(argv, list) and argv) and not command_line:
        return {"state": "unknown", "reason": "incomplete_process_query"}
    if record.get("Name", "").lower() not in _BROWSER_NAMES:
        return {"state": "unknown", "reason": "inconsistent_process_query"}
    options = _options(argv if isinstance(argv, list) and argv else windows_argv(command_line))
    actual = options.get("user-data-dir")
    if not actual or os.path.normcase(os.path.realpath(actual)) != expected_dir:
        return {"state": "mismatch", "reason": "directory_mismatch"}
    try:
        port = int(options.get("remote-debugging-port", "0"))
    except (TypeError, ValueError):
        port = 0
    return {
        "state": "owned", "port": port if 0 < port < 65536 else None,
        "created": str(record["CreationDate"]), "birth_ticks": before["birth_ticks"],
    }


def read_browser_identity(pid, user_data_dir, *, fresh=False):
    """Display queries may cache; destructive callers must invalidate/use fresh."""
    if sys.platform != "win32":
        return {"state": "unknown", "reason": "unsupported_platform"}
    try:
        pid = int(pid)
        if pid <= 0:
            return {"state": "absent", "reason": "invalid_pid"}
        if not user_data_dir:
            return {"state": "unknown", "reason": "missing_directory"}
        expected = os.path.normcase(os.path.realpath(user_data_dir))
        key = (pid, expected)
        now = time.monotonic()
        cached = _identity_cache.get(key)
        if not fresh and cached and now - cached[0] < 1.5:
            return dict(cached[1])
        api = _kernel32()
        handle = api.OpenProcess(_QUERY, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            result = {"state": "absent" if error == 87 else "unknown",
                      "reason": "invalid_pid" if error == 87 else "process_open_failed"}
        else:
            try:
                result = _identity_on_handle(api, handle, pid, expected)
            finally:
                api.CloseHandle(handle)
        if result["state"] == "unknown":
            invalidate_identity(pid)
        else:
            _identity_cache[key] = (now, dict(result))
        return result
    except Exception:
        if isinstance(pid, int):
            invalidate_identity(pid)
        return {"state": "unknown", "reason": "process_query_failed"}


def browser_process_birth(pid):
    """Capture native birth without a slow command-line query after our spawn."""
    if sys.platform != "win32":
        return None
    try:
        api = _kernel32()
        handle = api.OpenProcess(_QUERY, False, int(pid))
        if not handle:
            return None
        try:
            return _handle_snapshot(api, handle).get("birth_ticks")
        finally:
            api.CloseHandle(handle)
    except Exception:
        return None


def terminate_verified_browser(pid, user_data_dir, expected_identity, timeout=8.0):
    """Terminate the same native handle whose directory/birth was verified."""
    handle = None
    try:
        pid = int(pid)
        if sys.platform != "win32" or pid <= 0 or not user_data_dir:
            return {"state": "unknown", "reason": "invalid_termination_request"}
        api = _kernel32()
        handle = api.OpenProcess(_QUERY | _TERMINATE, False, pid)
        if not handle:
            return {"state": "unknown", "reason": "process_open_failed"}
        actual = _identity_on_handle(api, handle, pid, os.path.normcase(os.path.realpath(user_data_dir)))
        if actual["state"] != "owned":
            return actual
        if not expected_identity.get("created") or actual["created"] != expected_identity["created"]:
            return {"state": "unknown", "reason": "process_birth_changed"}
        expected_birth = expected_identity.get("birth_ticks")
        if expected_birth is not None and actual["birth_ticks"] != expected_birth:
            return {"state": "unknown", "reason": "process_birth_changed"}
        if not api.TerminateProcess(handle, 1):
            return {"state": "unknown", "reason": "terminate_failed"}
        deadline = time.monotonic() + timeout
        while True:
            result = _handle_snapshot(api, handle)
            if result["state"] == "absent":
                return result
            if result["state"] == "unknown" or time.monotonic() >= deadline:
                return {"state": "unknown", "reason": "exit_unconfirmed"}
            time.sleep(0.1)
    except Exception:
        return {"state": "unknown", "reason": "termination_query_failed"}
    finally:
        if handle:
            api.CloseHandle(handle)
        invalidate_identity(pid)


def _browser_pids_using_dir_cim(user_data_dir, exclude_pids=None):
    if sys.platform != "win32" or not user_data_dir:
        return None
    try:
        command = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR "
            "Name='msedge.exe' OR Name='chromium.exe'\" -ErrorAction Stop | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return None
        payload = json.loads(result.stdout) if result.stdout.strip() else []
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return None
        expected = os.path.normcase(os.path.realpath(user_data_dir))
        excluded = {int(pid) for pid in (exclude_pids or [])}
        pids = set()
        for record in payload:
            if not isinstance(record, dict):
                return None
            pid = int(record.get("ProcessId"))
            if pid <= 0:
                return None
            if pid in excluded:
                continue
            command_line = record.get("CommandLine")
            if not command_line:
                # CIM can retain an exiting process in this snapshot after its
                # command line has disappeared. Skip it only with fresh native
                # proof of exit; a live/denied/unknown entry still blocks idle.
                if read_browser_identity(pid, user_data_dir, fresh=True).get("state") == "absent":
                    continue
                return None
            options = _options(windows_argv(command_line))
            actual = options.get("user-data-dir")
            if actual and os.path.normcase(os.path.realpath(actual)) == expected:
                pids.add(pid)
        return pids
    except Exception:
        return None


def browser_pids_using_dir(user_data_dir, exclude_pids=None):
    """Return matching PIDs, or None if the scan cannot prove completeness."""
    if sys.platform != "win32" or not user_data_dir:
        return None
    try:
        expected = os.path.normcase(os.path.realpath(user_data_dir))
        excluded = {int(pid) for pid in (exclude_pids or [])}
        pids = set()
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                record = process.info
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                return _browser_pids_using_dir_cim(user_data_dir, exclude_pids)
            name = (record.get("name") or "").lower()
            if name not in _BROWSER_NAMES:
                continue
            pid = int(record.get("pid"))
            if pid <= 0:
                return _browser_pids_using_dir_cim(user_data_dir, exclude_pids)
            if pid in excluded:
                continue
            argv = record.get("cmdline")
            if not argv:
                return _browser_pids_using_dir_cim(user_data_dir, exclude_pids)
            actual = _options(argv).get("user-data-dir")
            if actual and os.path.normcase(os.path.realpath(actual)) == expected:
                pids.add(pid)
        return pids
    except Exception:
        return _browser_pids_using_dir_cim(user_data_dir, exclude_pids)
