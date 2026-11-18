"""Private TEMP-only soak bootstrap; warm offline trust before app measurement."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import runpy
import stat
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


class SoakServerError(RuntimeError):
    pass


def _no_reparse(path):
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        try:
            entry = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
            raise SoakServerError("Fixture paths must not contain reparse points.")
    return absolute.resolve()


def _temp_child(path):
    path = _no_reparse(path)
    if Path(tempfile.gettempdir()).resolve() not in path.parents:
        raise SoakServerError("Fixture paths must be children of TEMP.")
    return path


def _browser_path(explicit):
    if explicit:
        return Path(explicit)
    for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(key):
                candidate = Path(os.environ[key]) / relative
                if candidate.is_file():
                    return candidate
    raise SoakServerError("A signed installed Chrome/Edge executable is required.")


def _current_metrics():
    if sys.platform != "win32":
        raise SoakServerError("The private soak bootstrap requires Windows.")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(count)):
        raise SoakServerError("Cannot record cold/warm process handle counts.")
    return {"pid": os.getpid(), "handles": count.value}


def _verify_browser(path):
    # Avoid importing the package/application before cold/warm trust samples.
    name = "_antidetect_soak_browser_trust"
    spec = importlib.util.spec_from_file_location(name, ROOT / "antidetect" / "browser_trust.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return asdict(module.verify_browser_executable(path))


def _write_report(path, report):
    pending = _no_reparse(path.with_name(path.name + ".tmp." + uuid.uuid4().hex))
    try:
        with pending.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, _no_reparse(path))
    finally:
        pending.unlink(missing_ok=True)


def start(data_dir, report_path, operation_id, port, browser=None, *,
          metrics=_current_metrics, verify=_verify_browser, execute=runpy.run_path):
    # All path/operation checks must precede any application/trust imports.
    data_dir = _temp_child(data_dir)
    report_path = _temp_child(report_path)
    if (report_path.name != "trust-warmup.json" or not data_dir.is_dir()
            or not report_path.parent.is_dir() or report_path.exists()
            or str(uuid.UUID(operation_id)) != operation_id
            or not 1 <= int(port) <= 65535):
        raise SoakServerError("Invalid or previously used private soak bootstrap arguments.")
    real_data = (ROOT / "data").resolve()
    if data_dir == real_data or real_data in data_dir.parents:
        raise SoakServerError("Real application data cannot be used by the soak bootstrap.")
    selected = _browser_path(browser or os.environ.get("ANTIDETECT_BROWSER_PATH"))
    cold = metrics()
    attestation = verify(selected)
    warm = metrics()
    if (cold.get("pid") != os.getpid() or warm.get("pid") != os.getpid()
            or any(type(sample.get("handles")) is not int or sample["handles"] < 0 for sample in (cold, warm))):
        raise SoakServerError("Trust metrics do not belong to the bootstrap process.")
    if not all(attestation.get(key) for key in ("path", "sha256", "publisher", "version")):
        raise SoakServerError("Browser trust did not produce complete attestation.")
    report = {
        "schema": 1, "pid": os.getpid(), "operation_id": operation_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "data_dir": str(data_dir),
        "browser": attestation, "cold_before_trust": cold, "warm_after_trust": warm,
        "trust_handles_delta": warm["handles"] - cold["handles"],
        "scope": "cold/warm samples precede application imports; no browser process is spawned during trust warmup",
    }
    _write_report(report_path, report)
    overrides = {"ANTIDETECT_DATA_DIR": str(data_dir), "ANTIDETECT_NO_BROWSER": "1",
                 "ANTIDETECT_BROWSER_PATH": attestation["path"]}
    previous = {key: os.environ.get(key) for key in overrides}
    old_arguments = sys.argv
    try:
        os.environ.update(overrides)
        sys.argv = [str(ROOT / "run.py"), "--port=" + str(port)]
        execute(str(ROOT / "run.py"), run_name="__main__")
    finally:
        sys.argv = old_arguments
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--browser")
    args = parser.parse_args(argv)
    start(args.data_dir, args.report, args.operation_id, args.port, args.browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
