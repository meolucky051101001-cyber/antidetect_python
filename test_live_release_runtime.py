"""Opt-in real Windows/Chrome ownership, restart and CDP fault tests.

PowerShell example (browser path optional if installed Chrome is available)::

    $env:ANTIDETECT_RUN_LIVE_RELEASE = '1'
    $env:ANTIDETECT_LIVE_BROWSER = 'C:\\path\\chrome.exe'
    $env:ANTIDETECT_LIVE_RUNTIME_REPORT = 'C:\\path\\live-runtime.json'
    python -m pytest test_live_release_runtime.py -q

Three server processes run sequentially/overlapping on independent loopback
ports, sharing only a freshly allocated TEMP data directory. At most one test
profile is open. Native measurement runs before that profile opens. Production
data, cookies and browser sessions are never read. Authentication files remain
DPAPI protected and tokens are deliberately absent from diagnostic messages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from antidetect.loopback import open_loopback
from tools.live_test_diagnostics import server_failure_diagnostics

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("ANTIDETECT_RUN_LIVE_RELEASE") != "1",
    reason="Explicit Windows live release opt-in required.",
)

ROOT = Path(__file__).resolve().parent
IDENTITY_FIELDS = ("launch_id", "owner_instance", "pid", "user_data_dir")
CLEANUP_COUNT_SCOPE = (
    "Chrome/Edge processes whose --user-data-dir is inside the fixture TEMP root; "
    "excludes unrelated Chrome sessions and standalone native-brand-probe directories."
)


def _wait_until(check, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for fixture condition.")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(base, method, path, token=None, payload=None, timeout=60):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(base + path, data=body, headers=headers, method=method)
    try:
        # A fixture bearer must never be sent through env or Windows proxies.
        with open_loopback(request, timeout=timeout) as response:
            data = response.read()
            return response.status, json.loads(data) if data else {}
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _runtime(data_dir, profile_id):
    path = data_dir / "browser_runtime.json"
    return json.loads(path.read_text("utf-8")).get(profile_id) if path.exists() else None


def _same_generation(first, second):
    return bool(first and second) and all(first.get(key) == second.get(key) for key in IDENTITY_FIELDS)


def _fixture_browser_processes(data_dir):
    import psutil

    expected_root = os.path.normcase(str(data_dir.resolve())) + os.sep
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() not in {"chrome.exe", "msedge.exe", "chromium.exe"}:
                continue
            for arg in proc.info["cmdline"] or []:
                if arg.startswith("--user-data-dir="):
                    actual = os.path.normcase(os.path.realpath(arg.split("=", 1)[1]))
                    if actual.startswith(expected_root):
                        found.append(proc)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


class LiveServer:
    def __init__(self, data_dir, label, *, warm_native=False):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ready = data_dir / f"{label}.ready.dpapi"
        self.log_path = data_dir / f"{label}.log"
        self.app_log_path = data_dir / "app.log"
        self.log = open(self.log_path, "wb")
        self.token = None
        self.identity = {}
        self.failure_diagnostics = []
        env = dict(os.environ, ANTIDETECT_DATA_DIR=str(data_dir), ANTIDETECT_NO_BROWSER="1",
                   PYTHONIOENCODING="utf-8")
        browser = os.environ.get("ANTIDETECT_LIVE_BROWSER")
        if browser:
            assert Path(browser).is_file(), "Configured live browser is missing."
            env["ANTIDETECT_BROWSER_PATH"] = browser
        command = [sys.executable, str(ROOT / "tools" / "live_release_runtime_server.py"),
                   "--port", str(self.port), "--data-dir", str(data_dir),
                   "--ready-file", str(self.ready)]
        if warm_native:
            command.append("--warm-native")
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)

    def start(self):
        from antidetect.runtime_auth import _crypt

        def ready():
            if self.proc.poll() is not None:
                self.fail("Fixture server exited during startup.")
            if not self.ready.exists():
                return False
            try:
                status, body = _http(self.base, "GET", "/health", timeout=2)
            except (OSError, URLError):
                return False
            if status != 200:
                return False
            identity = json.loads(_crypt(self.ready.read_bytes(), False))
            assert identity["instance_id"] == body["instance_id"]
            self.token = identity.pop("access_key")
            self.identity = identity
            return True

        try:
            _wait_until(ready, timeout=90)
        except AssertionError:
            self.fail("Fixture startup readiness condition failed.")
        return self

    def fail(self, message):
        diagnostic = server_failure_diagnostics(
            self.proc, (("child stdout/stderr", self.log_path), ("application log", self.app_log_path)),
            secrets=(self.token,),
        )
        self.failure_diagnostics.append(diagnostic)
        pytest.fail(message + "\n" + diagnostic, pytrace=False)

    def request(self, method, path, payload=None, timeout=90):
        try:
            return _http(self.base, method, path, self.token, payload, timeout)
        except (OSError, URLError, TimeoutError):
            self.fail(f"Fixture HTTP transport failed for {method} {path.split('?', 1)[0]}.")

    def shutdown(self):
        status, result = self.request("POST", "/api/system/shutdown", {})
        assert status == 200 and result.get("success"), "Fixture shutdown refused."
        self.proc.wait(timeout=45)
        assert self.proc.returncode == 0

    def cleanup(self):
        if self.proc.poll() is None:
            try:
                self.shutdown()
            except Exception:
                self.proc.kill()  # Only this exact fixture server handle, never /T.
                self.proc.wait(timeout=15)
        self.log.close()


@pytest.fixture
def live_environment():
    # Windows CI may expose TEMP through an 8.3 alias (RUNNER~1). Compare
    # canonical paths on both sides while retaining the direct-TEMP guard.
    data_dir = Path(tempfile.mkdtemp(prefix="antidetect-live-runtime-")).resolve()
    assert data_dir.parent == Path(tempfile.gettempdir()).resolve()
    servers = []
    application_commit = subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
        cwd=ROOT, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
    ).strip()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "application_commit": application_commit,
              "scope": "real uvicorn lifespan and Chrome; fresh temporary data only",
              "checks": {}, "limitations": ["No production profiles or Facebook sessions used."]}

    def server(label, **kwargs):
        instance = LiveServer(data_dir, label, **kwargs)
        servers.append(instance)
        return instance.start()

    try:
        yield data_dir, server, report
    finally:
        cleanup_errors = []
        for instance in reversed(servers):
            try:
                instance.cleanup()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        report["server_failure_diagnostics"] = [
            diagnostic for instance in servers for diagnostic in instance.failure_diagnostics
        ]
        # Emergency cleanup is constrained by exact TEMP user-data-dir identity.
        for proc in _fixture_browser_processes(data_dir):
            try:
                birth = proc.create_time()
                if proc.create_time() == birth and proc in _fixture_browser_processes(data_dir):
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=20,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        try:
            _wait_until(lambda: not _fixture_browser_processes(data_dir), timeout=20)
            shutil.rmtree(data_dir)
        except Exception as exc:
            cleanup_errors.append(type(exc).__name__)
        report["cleanup"] = {"servers_exited": all(item.proc.poll() is not None for item in servers),
                             "fixture_browsers_remaining": len(_fixture_browser_processes(data_dir)),
                             "count_scope": CLEANUP_COUNT_SCOPE,
                             "temporary_data_removed": not data_dir.exists(),
                             "errors": cleanup_errors}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if report["status"] == "running":
            report["status"] = "failed"
        if cleanup_errors:
            report["status"] = "failed"
        output = os.environ.get("ANTIDETECT_LIVE_RUNTIME_REPORT")
        if output:
            path = Path(output).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        assert not cleanup_errors, "Live fixture cleanup failed."


def test_real_ownership_replacement_disconnect_and_restart(live_environment):
    data_dir, server, report = live_environment
    started = time.monotonic()
    a = server("a", warm_native=True)
    report["browser_version"] = a.identity["browser_version"]
    assert a.identity["native_warmed"]
    status, profile = a.request("POST", "/api/profiles", {"name": "Isolated release runtime"})
    assert status == 200
    profile_id = profile["id"]

    def launch():
        status, result = a.request("POST", f"/api/profiles/{profile_id}/start?force=true", {})
        assert status == 200 and result.get("success"), "Real Chrome launch failed."
        health = result.get("cdp_health") or {}
        report["last_launch"] = {key: health.get(key) for key in ("state", "reason", "restart_required")}
        assert result.get("cdp_ready") and health.get("state") == "healthy", "Real CDP did not become healthy."
        record = _runtime(data_dir, profile_id)
        assert record and record["owner_instance"] == a.identity["launcher_instance"]
        # Chromium renderers also carry --user-data-dir; count browser roots,
        # while cleanup continues to inspect all processes of the fixture.
        roots = [proc for proc in _fixture_browser_processes(data_dir)
                 if not any(arg.startswith("--type=") for arg in proc.cmdline())]
        assert len(roots) == 1
        return record

    first = launch()
    report["checks"]["initial_healthy_launch"] = True
    b = server("b")
    status, rows = b.request("GET", "/api/profiles")
    assert status == 200
    observed = next(row for row in rows if row["id"] == profile_id)
    assert observed["status"] == "running"
    assert observed["cdp_health"]["state"] == "recovery_required"
    assert _same_generation(first, _runtime(data_dir, profile_id))
    b.shutdown()
    assert _same_generation(first, _runtime(data_dir, profile_id))
    assert any(proc.pid == first["pid"] for proc in _fixture_browser_processes(data_dir))
    status, rows = a.request("GET", "/api/profiles")
    assert status == 200 and rows[0]["cdp_health"]["state"] == "healthy"
    report["checks"]["other_server_shutdown_preserves_owner_browser_and_runtime"] = True

    status, stopped = a.request("POST", f"/api/profiles/{profile_id}/stop", {})
    assert status == 200 and stopped["success"]
    _wait_until(lambda: not _fixture_browser_processes(data_dir))
    second = launch()
    assert first["launch_id"] != second["launch_id"]
    query = urlencode({"expected_launch_id": first["launch_id"]})
    status, _ = a.request("POST", f"/api/profiles/{profile_id}/stop/owned?{query}", {})
    assert status == 409
    assert _same_generation(second, _runtime(data_dir, profile_id))
    assert any(proc.pid == second["pid"] for proc in _fixture_browser_processes(data_dir))
    report["checks"]["stale_conditional_stop_preserves_replacement"] = True

    query = urlencode({"expected_launch_id": second["launch_id"]})
    status, disconnected = a.request("POST", f"/api/__live_release/{profile_id}/disconnect-cdp?{query}", {})
    assert status == 200 and disconnected["aborted_connections"] == 1

    def disconnected_state():
        status, rows = a.request("GET", "/api/profiles")
        assert status == 200
        row = next(row for row in rows if row["id"] == profile_id)
        return row if row["cdp_health"]["state"] == "degraded" else None

    row = _wait_until(disconnected_state)
    assert row["status"] == "running" and row["cdp_health"]["reason"] == "SOCKET_CLOSED"
    assert row["cdp_health"]["restart_required"]
    status, guard = a.request("POST", f"/api/profiles/{profile_id}/start", {})
    assert status == 409 and guard["code"] == "CDP_UNHEALTHY"
    report["checks"]["real_cdp_socket_abort_classified_separately_from_process_liveness"] = True
    status, stopped = a.request("POST", f"/api/profiles/{profile_id}/stop/owned?{query}", {})
    assert status == 200 and stopped["success"]
    _wait_until(lambda: not _fixture_browser_processes(data_dir))
    report["checks"]["matching_conditional_stop_succeeds"] = True

    third = launch()
    a.proc.kill()  # Crash the fixture server only, intentionally preserving Chrome.
    a.proc.wait(timeout=15)
    assert any(proc.pid == third["pid"] for proc in _fixture_browser_processes(data_dir))
    assert _same_generation(third, _runtime(data_dir, profile_id))
    c = server("recovered")
    status, rows = c.request("GET", "/api/profiles")
    assert status == 200
    recovered = next(row for row in rows if row["id"] == profile_id)
    assert recovered["status"] == "running"
    assert recovered["cdp_health"]["state"] == "recovery_required"
    assert recovered["cdp_health"]["restart_required"]
    assert _same_generation(third, _runtime(data_dir, profile_id))
    status, guard = c.request("POST", f"/api/profiles/{profile_id}/start", {})
    assert status == 409 and guard["code"] == "RECOVERY_REQUIRED"
    report["checks"]["forced_server_exit_and_restart_preserve_browser_with_recovery_required"] = True
    status, stopped = c.request("POST", f"/api/profiles/{profile_id}/stop", {})
    assert status == 200 and stopped["success"], "Explicit stop of recovered Chrome failed."
    _wait_until(lambda: not _fixture_browser_processes(data_dir))
    assert _runtime(data_dir, profile_id) is None
    report["checks"]["explicit_recovered_browser_stop_clears_runtime"] = True
    c.shutdown()
    report["checks"]["final_shutdown_removes_own_server_and_auth_records"] = (
        not (data_dir / "server.pid").exists() and not (data_dir / "runtime.dpapi").exists())
    assert report["checks"]["final_shutdown_removes_own_server_and_auth_records"]
    report["duration_seconds"] = round(time.monotonic() - started, 2)
    report["status"] = "passed"
