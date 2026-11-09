"""Soak test runner cho Antidetect Python.

Chỉ chạy trên dữ liệu TẠM; từ chối nếu ``--data-dir`` trỏ vào ``data/`` thật
hoặc ra ngoài thư mục temp. Runner khởi động dashboard riêng, tạo profile,
luân phiên start/stop, mở/đóng tab trên trang test cục bộ, chạy copy giao dịch
định kỳ và ghi metric RAM/CPU/CDP/orphan/độ trễ API vào JSONL.

Ví dụ:

    python tools\\soak_test.py run --hours 24 --profiles 5
    python tools\\soak_test.py status --state <results>\\soak_state.json
    python tools\\soak_test.py stop --state <results>\\soak_state.json
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil là optional
    psutil = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DATA_DIR = os.path.realpath(os.path.join(BASE_DIR, "data"))
STATE_FILENAME = "soak_state.json"
EVENTS_FILENAME = "soak_events.jsonl"
METRICS_FILENAME = "soak_metrics.jsonl"
STOP_FILENAME = "STOP"
MAX_ERRORS = 100


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path, payload):
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _append_jsonl(path, payload):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fh.flush()


def _decode_json(raw):
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _http(base, method, path, token=None, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = _decode_json(resp.read())
            return resp.status, payload, (time.perf_counter() - started) * 1000, None
    except urllib.error.HTTPError as exc:
        payload = _decode_json(exc.read())
        return exc.code, payload, (time.perf_counter() - started) * 1000, f"HTTP {exc.code}"
    except Exception as exc:
        return -1, {}, (time.perf_counter() - started) * 1000, f"{type(exc).__name__}: {exc}"


def _http_plain(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, _decode_json(resp.read()), None
    except Exception as exc:
        return -1, {}, f"{type(exc).__name__}: {exc}"


def _free_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _in_temp(path):
    try:
        root = os.path.realpath(tempfile.gettempdir())
        real = os.path.realpath(path)
        return real == root or real.startswith(root + os.sep)
    except Exception:
        return False


def _read_access_key(data_dir):
    code = (
        "import json;"
        "from antidetect.runtime_auth import read_runtime;"
        "print(json.dumps(read_runtime() or {}))"
    )
    env = dict(os.environ)
    env["ANTIDETECT_DATA_DIR"] = data_dir
    env["PYTHONPATH"] = BASE_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    output = subprocess.check_output(
        [sys.executable, "-c", code], cwd=BASE_DIR, env=env, timeout=30
    )
    record = json.loads(output.decode("utf-8", "replace").strip() or "{}")
    return record.get("access_key")


def _process_metrics(pid):
    if psutil is None or not pid:
        return None
    try:
        proc = psutil.Process(int(pid))
        with proc.oneshot():
            handles = None
            try:
                handles = proc.num_handles()
            except Exception:
                pass
            return {
                "pid": int(pid),
                "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
                "cpu_percent": round(proc.cpu_percent(interval=None), 1),
                "threads": proc.num_threads(),
                "handles": handles,
            }
    except Exception:
        return None


def _chrome_processes(dirs):
    if psutil is None:
        return {}
    result = {
        profile_id: {"pids": [], "rss_mb": 0.0, "cpu_percent": 0.0}
        for profile_id in dirs
    }
    normalized = {
        profile_id: os.path.normcase(os.path.realpath(path))
        for profile_id, path in dirs.items()
    }
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("chrome.exe", "msedge.exe", "chromium.exe"):
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = os.path.normcase(" ".join(cmdline))
            if not joined:
                continue
            for profile_id, path in normalized.items():
                if path and path in joined:
                    mem = proc.info.get("memory_info")
                    entry = result[profile_id]
                    entry["pids"].append(int(proc.info["pid"]))
                    if mem:
                        entry["rss_mb"] += mem.rss / (1024 * 1024)
                    try:
                        entry["cpu_percent"] += proc.cpu_percent(interval=None)
                    except Exception:
                        pass
                    break
        except Exception:
            continue
    for entry in result.values():
        entry["rss_mb"] = round(entry["rss_mb"], 1)
        entry["cpu_percent"] = round(entry["cpu_percent"], 1)
    return result


def _hide_pid(pid):
    if sys.platform != "win32" or not pid:
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found = {"count": 0}

        def _enum(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == int(pid) and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                found["count"] += 1
            return True

        user32.EnumWindows(WNDENUMPROC(_enum), 0)
        return found["count"] > 0
    except Exception:
        return False


def _keep_awake(enabled):
    if sys.platform != "win32":
        return
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        )
    except Exception:
        pass


def _close_soak_tabs(port, soak_base, keep=1):
    status, targets, error = _http_plain(f"http://127.0.0.1:{port}/json/list")
    if status != 200 or not isinstance(targets, list):
        return 0, 0, error or "CDP target list unavailable"
    pages = [
        target
        for target in targets
        if target.get("type") == "page"
        and str(target.get("url") or "").startswith(soak_base)
    ]
    closed = 0
    if len(pages) > keep:
        for target in pages[:-keep]:
            target_id = target.get("id")
            if not target_id:
                continue
            close_status, _, _ = _http_plain(
                f"http://127.0.0.1:{port}/json/close/{target_id}"
            )
            if close_status == 200:
                closed += 1
    return len(pages), closed, None


class _QuietSoakServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _SoakPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            b"<!doctype html><meta charset='utf-8'><title>Antidetect soak</title>"
            b"<h1>Antidetect soak test page</h1>"
            b"<script>setInterval(function(){document.title='soak-'+Date.now();},5000);</script>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class SoakFailure(RuntimeError):
    pass


class SoakRunner:
    def __init__(self, args):
        self.args = args
        self.hours = float(args.hours)
        self.target_profiles = max(1, int(args.profiles))
        self.min_active = min(
            max(0, int(getattr(args, "min_active", 0))), self.target_profiles
        )
        self.port = int(args.port or _free_port())
        self.base = f"http://127.0.0.1:{self.port}"
        self.data_dir = os.path.abspath(args.data_dir)
        self.results_dir = os.path.abspath(args.results_dir)
        self.state_path = os.path.join(self.results_dir, STATE_FILENAME)
        self.events_path = os.path.join(self.results_dir, EVENTS_FILENAME)
        self.metrics_path = os.path.join(self.results_dir, METRICS_FILENAME)
        self.stop_path = os.path.join(self.results_dir, STOP_FILENAME)
        self.log_path = os.path.join(self.results_dir, "soak.log")
        self.server = None
        self.server_log = None
        self.local_server = None
        self.local_thread = None
        self.token = None
        self.profiles = []
        self.errors = []
        self.critical = False
        self.critical_reason = None
        self.stop_requested = False
        self._sample_count = 0
        self._server_failures = 0
        self._low_memory_samples = 0
        self._tab_cycles = 0
        self._lifecycle_cycles = 0
        self._copy_cycles = 0
        self._tabs_opened = 0
        self._tabs_closed = 0
        self._shed_count = 0
        self._shed_blocked_logged = False
        self._cycle_index = 0
        self._copy_index = 0
        self._api_latencies = []
        self._chrome_peak = {"rss_mb": 0.0, "count": 0}
        self._server_peak = {"rss_mb": 0.0, "threads": 0, "handles": 0}
        self._min_available_mb = None
        self._started_ts = time.time()
        self._deadline_ts = self._started_ts + self.hours * 3600
        self._started_mono = time.monotonic()
        self.state = {
            "schema": 1,
            "status": "starting",
            "pid": os.getpid(),
            "started_at": _now_iso(),
            "deadline_at": datetime.fromtimestamp(
                self._deadline_ts, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "hours": self.hours,
            "port": self.port,
            "data_dir": self.data_dir,
            "results_dir": self.results_dir,
            "target_profiles": self.target_profiles,
            "min_active": self.min_active,
            "profiles": [],
            "counters": {
                "samples": 0,
                "lifecycle_cycles": 0,
                "tab_cycles": 0,
                "copy_cycles": 0,
                "tabs_opened": 0,
                "tabs_closed": 0,
                "memory_shed": 0,
                "errors": 0,
            },
            "last_sample": {},
            "last_event": "",
            "errors": [],
            "finished_at": None,
            "summary": {},
        }

    def _log(self, message):
        line = f"[{_now_iso()}] {message}"
        print(line, flush=True)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def _event(self, name, **fields):
        record = {"at": _now_iso(), "event": name}
        record.update(fields)
        self.state["last_event"] = name
        _append_jsonl(self.events_path, record)
        self._log(name + (f" {fields}" if fields else ""))

    def _error(self, scope, message, critical=False, code=None):
        text = str(message).replace("\n", " ")[:500]
        record = {"at": _now_iso(), "scope": scope, "message": text}
        if code:
            record["code"] = code
        self.errors.append(record)
        self.errors = self.errors[-MAX_ERRORS:]
        self.state["errors"] = self.errors
        self.state["counters"]["errors"] = len(self.errors)
        _append_jsonl(self.events_path, {**record, "event": "error", "critical": critical})
        self._log(f"ERROR [{scope}] {text}")
        if critical and not self.critical:
            self.critical = True
            self.critical_reason = f"{scope}: {text}"

    def _save(self):
        self.state["profiles"] = [
            {
                "id": item["id"],
                "name": item["name"],
                "active": bool(item["active"]),
                "pid": item.get("pid"),
                "cdp_port": item.get("port"),
                "starts": item.get("starts", 0),
                "stops": item.get("stops", 0),
            }
            for item in self.profiles
        ]
        _atomic_json(self.state_path, self.state)

    def _validate_dirs(self):
        if not _in_temp(self.data_dir):
            raise SoakFailure(
                "TỪ CHỐI: --data-dir phải nằm trong thư mục temp; "
                "không dùng data/ thật hoặc đường dẫn tùy ý."
            )
        real_data = os.path.realpath(self.data_dir)
        if real_data == REAL_DATA_DIR or real_data.startswith(REAL_DATA_DIR + os.sep):
            raise SoakFailure("TỪ CHỐI: --data-dir trỏ vào data/ thật.")
        if not _in_temp(self.results_dir):
            raise SoakFailure("TỪ CHỐI: --results-dir phải nằm trong thư mục temp.")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)
        if os.path.exists(self.stop_path):
            os.remove(self.stop_path)

    def _start_local_server(self):
        self.local_server = _QuietSoakServer(("127.0.0.1", 0), _SoakPageHandler)
        self.local_thread = threading.Thread(
            target=self.local_server.serve_forever, name="soak-page", daemon=True
        )
        self.local_thread.start()
        port = self.local_server.server_address[1]
        self.soak_base = f"http://127.0.0.1:{port}"
        self._event("local_page_ready", url=self.soak_base)

    def _start_dashboard(self):
        env = dict(os.environ)
        env["ANTIDETECT_DATA_DIR"] = self.data_dir
        env["ANTIDETECT_NO_BROWSER"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self.server_log = open(
            os.path.join(self.results_dir, "server.log"), "ab", buffering=0
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.server = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "run.py"), f"--port={self.port}"],
            cwd=BASE_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.server_log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            status, body, _latency, _error = _http(
                self.base, "GET", "/health", timeout=3
            )
            if status == 200 and body.get("application") == "antidetect-python":
                self.state["server_pid"] = self.server.pid
                self._event("server_ready", pid=self.server.pid, port=self.port)
                return
            if self.server.poll() is not None:
                break
            time.sleep(0.5)
        raise SoakFailure("Dashboard không khởi động được trong 60s; xem server.log.")

    def _read_token(self):
        self.token = _read_access_key(self.data_dir)
        if not self.token:
            raise SoakFailure("Không đọc được access_key từ runtime DPAPI tạm.")
        status, _body, _latency, error = _http(
            self.base, "GET", "/api/profiles", token=self.token, timeout=10
        )
        if status != 200:
            raise SoakFailure(f"Token soak không dùng được: {error or status}")
        self._event("auth_ready")

    def _create_profiles(self):
        status, existing, _latency, error = _http(
            self.base, "GET", "/api/profiles", token=self.token, timeout=15
        )
        if status != 200 or not isinstance(existing, list):
            raise SoakFailure(f"Không đọc được danh sách profile: {error or status}")
        by_name = {item.get("name"): item for item in existing}
        for index in range(1, self.target_profiles + 1):
            name = f"Soak {index}"
            found = by_name.get(name)
            if found:
                profile_id = found["id"]
            else:
                status, body, _latency, error = _http(
                    self.base,
                    "POST",
                    "/api/profiles",
                    token=self.token,
                    body={"name": name, "os": "windows"},
                    timeout=30,
                )
                if status != 200 or not body.get("id"):
                    self._error("create_profile", error or f"HTTP {status}", critical=True)
                    raise SoakFailure("Không tạo được profile soak.")
                profile_id = body["id"]
            self.profiles.append(
                {
                    "id": profile_id,
                    "name": name,
                    "active": False,
                    "pid": None,
                    "port": None,
                    "starts": 0,
                    "stops": 0,
                }
            )
        self._event("profiles_created", count=len(self.profiles))
        self._save()

    def _profile_dir(self, profile):
        return os.path.join(self.data_dir, "profiles", profile["id"], "user_data")

    def _chrome_map(self):
        return {item["id"]: self._profile_dir(item) for item in self.profiles}

    def _available_mb(self):
        if psutil is None:
            return None
        return psutil.virtual_memory().available / (1024 * 1024)

    def _start_profile(self, profile):
        if self._available_mb() is not None and self._available_mb() < self.args.min_free_mb:
            self._event("ramp_paused", profile=profile["name"], available_mb=round(self._available_mb(), 1))
            return False
        status, body, _latency, error = _http(
            self.base,
            "POST",
            f"/api/profiles/{profile['id']}/start",
            token=self.token,
            timeout=180,
        )
        if status != 200 or not body.get("success"):
            self._error(
                "start",
                f"{profile['name']}: {error or body.get('detail') or body.get('error')}",
                code=body.get("code"),
            )
            return False
        profile["active"] = True
        profile["pid"] = body.get("pid")
        profile["port"] = body.get("port")
        profile["starts"] = profile.get("starts", 0) + 1
        _hide_pid(profile["pid"])
        health = (body.get("cdp_health") or {}).get("state")
        if body.get("degraded") or health not in (None, "healthy"):
            self._error(
                "start_degraded",
                f"{profile['name']}: degraded=True, cdp={health}",
            )
        self._event(
            "profile_started",
            profile=profile["name"],
            pid=profile["pid"],
            cdp_port=profile["port"],
        )
        return True

    def _wait_no_chrome(self, profile, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = _chrome_processes({profile["id"]: self._profile_dir(profile)})[
                profile["id"]
            ]
            if not entry["pids"]:
                return True
            time.sleep(1)
        return False

    def _stop_profile(self, profile, reason):
        status, body, _latency, error = _http(
            self.base,
            "POST",
            f"/api/profiles/{profile['id']}/stop",
            token=self.token,
            timeout=120,
        )
        if status != 200 or not body.get("success"):
            detail = (body.get("detail") or body.get("error")) if isinstance(body, dict) else None
            msg = f"{error} ({detail})" if (error and detail) else (detail or error or status)
            self._error(
                "stop",
                f"{profile['name']}: {msg}",
                code=body.get("code") if isinstance(body, dict) else None,
            )
            # Kiểm tra xem Chrome thực tế đã dừng hay chưa để tránh lệch trạng thái
            time.sleep(1.0)
            pids = _chrome_processes(self._chrome_map()).get(profile["id"], {}).get("pids", [])
            if not pids:
                profile["active"] = False
                profile["stops"] = profile.get("stops", 0) + 1
                self._event("profile_stopped", profile=profile["name"], reason=reason)
            return False
        profile["active"] = False
        profile["stops"] = profile.get("stops", 0) + 1
        if not self._wait_no_chrome(profile):
            self._error(
                "orphan_after_stop",
                f"{profile['name']}: Chrome vẫn còn sau 30s",
                critical=True,
            )
            return False
        self._event("profile_stopped", profile=profile["name"], reason=reason)
        return True

    def _ramp_up(self):
        for profile in self.profiles:
            if self.stop_requested or self.critical:
                break
            if not profile["active"]:
                self._start_profile(profile)
                time.sleep(self.args.ramp_delay)
        self._save()

    def _sample(self):
        sample_started = time.monotonic()
        health_status, health_body, health_latency, health_error = _http(
            self.base, "GET", "/health", timeout=10
        )
        list_status, profiles_payload, list_latency, list_error = _http(
            self.base, "GET", "/api/profiles", token=self.token, timeout=20
        )
        self._api_latencies.extend([health_latency, list_latency])
        self._api_latencies = self._api_latencies[-2000:]
        if health_status != 200 or health_body.get("application") != "antidetect-python":
            self._server_failures += 1
            if self._server_failures >= 3:
                self._error(
                    "server_health",
                    f"/health lỗi 3 lần liên tiếp: {health_error or health_status}",
                    critical=True,
                )
        else:
            self._server_failures = 0
        if list_status == 401:
            self._error("auth", "Token bị từ chối 401", critical=True)
        by_id = {}
        if list_status == 200 and isinstance(profiles_payload, list):
            by_id = {item["id"]: item for item in profiles_payload}
        chrome = _chrome_processes(self._chrome_map())
        cdp_states = {}
        running = 0
        degraded = 0
        for profile in self.profiles:
            entry = by_id.get(profile["id"], {})
            status_text = entry.get("status")
            health = (entry.get("cdp_health") or {}).get("state")
            if health:
                cdp_states[health] = cdp_states.get(health, 0) + 1
            if status_text == "running":
                running += 1
            if health in ("degraded", "recovery_required"):
                degraded += 1
            pids = chrome.get(profile["id"], {}).get("pids", [])
            if profile["active"] and profile.get("pid"):
                _hide_pid(profile["pid"])
            if profile["active"] and not pids:
                self._error(
                    "missing_chrome",
                    f"{profile['name']}: active nhưng không thấy process",
                    critical=True,
                )
            if not profile["active"] and pids:
                self._error(
                    "orphan_chrome",
                    f"{profile['name']}: không active nhưng còn {len(pids)} process",
                    critical=True,
                )
            if profile["active"] and status_text not in (None, "running"):
                self._error(
                    "status_mismatch",
                    f"{profile['name']}: active nhưng API status={status_text}",
                    critical=True,
                )
        server_metrics = _process_metrics(self.server.pid if self.server else None)
        chrome_total = sum(entry["rss_mb"] for entry in chrome.values())
        chrome_count = sum(len(entry["pids"]) for entry in chrome.values())
        self._chrome_peak["rss_mb"] = max(self._chrome_peak["rss_mb"], chrome_total)
        self._chrome_peak["count"] = max(self._chrome_peak["count"], chrome_count)
        if server_metrics:
            self._server_peak["rss_mb"] = max(
                self._server_peak["rss_mb"], server_metrics["rss_mb"]
            )
            self._server_peak["threads"] = max(
                self._server_peak["threads"], server_metrics["threads"]
            )
            if server_metrics.get("handles") is not None:
                self._server_peak["handles"] = max(
                    self._server_peak["handles"], server_metrics["handles"]
                )
        available = self._available_mb()
        if available is not None:
            self._min_available_mb = (
                available
                if self._min_available_mb is None
                else min(self._min_available_mb, available)
            )
            if available < self.args.shed_free_mb:
                self._low_memory_samples += 1
            else:
                self._low_memory_samples = 0
                self._shed_blocked_logged = False
        artifacts = self._artifact_counts()
        sample = {
            "at": _now_iso(),
            "elapsed_seconds": round(time.time() - self._started_ts, 1),
            "health_status": health_status,
            "profiles_status": list_status,
            "api_latency_ms": {
                "health": round(health_latency, 1),
                "profiles": round(list_latency, 1),
            },
            "api_error": health_error or list_error,
            "server": server_metrics,
            "chrome": {
                "count": chrome_count,
                "rss_mb": round(chrome_total, 1),
                "by_profile": chrome,
            },
            "running": running,
            "active_tracked": sum(1 for item in self.profiles if item["active"]),
            "degraded": degraded,
            "cdp_states": cdp_states,
            "available_mb": round(available, 1) if available is not None else None,
            "artifacts": artifacts,
        }
        self._sample_count += 1
        self.state["counters"]["samples"] = self._sample_count
        self.state["last_sample"] = sample
        _append_jsonl(self.metrics_path, sample)
        if self._low_memory_samples >= 3:
            self._shed_one()
        self._save()
        if time.monotonic() - sample_started > 20:
            self._log(f"sample chậm: {time.monotonic() - sample_started:.1f}s")

    def _artifact_counts(self):
        profiles_root = os.path.join(self.data_dir, "profiles")
        staging_root = os.path.join(profiles_root, ".staging")
        operations_root = os.path.join(self.data_dir, "copy_operations")
        locks_root = os.path.join(self.data_dir, "locks")

        def _tree(path):
            count = 0
            size = 0
            if not os.path.isdir(path):
                return count, size
            for root, _dirs, files in os.walk(path):
                for file_name in files:
                    count += 1
                    try:
                        size += os.path.getsize(os.path.join(root, file_name))
                    except OSError:
                        pass
            return count, size

        staging_files, staging_bytes = _tree(staging_root)
        operation_files, operation_bytes = _tree(operations_root)
        lock_files = 0
        if os.path.isdir(locks_root):
            lock_files = len(
                [name for name in os.listdir(locks_root) if name.endswith(".lock")]
            )
        return {
            "staging_files": staging_files,
            "staging_bytes": staging_bytes,
            "operation_files": operation_files,
            "operation_bytes": operation_bytes,
            "lock_files": lock_files,
        }

    def _shed_one(self):
        active = [item for item in self.profiles if item["active"]]
        if not active:
            return
        if len(active) <= self.min_active:
            self._low_memory_samples = 0
            if not self._shed_blocked_logged:
                self._shed_blocked_logged = True
                self._event(
                    "shed_blocked_min_active",
                    active=len(active),
                    available_mb=round(self._available_mb() or 0, 1),
                )
            return
        chrome = _chrome_processes(self._chrome_map())
        victim = max(
            active,
            key=lambda item: chrome.get(item["id"], {}).get("rss_mb", 0.0),
        )
        self._low_memory_samples = 0
        self._shed_count += 1
        self.state["counters"]["memory_shed"] = self._shed_count
        self._event(
            "memory_shed",
            profile=victim["name"],
            available_mb=round(self._available_mb() or 0, 1),
        )
        self._stop_profile(victim, "memory_shed")

    def _tab_cycle(self):
        opened = 0
        closed = 0
        for profile in self.profiles:
            if self.stop_requested or self.critical:
                break
            if not profile["active"] or not profile.get("port"):
                continue
            url = f"{self.soak_base}/soak?profile={profile['id']}"
            status, body, _latency, error = _http(
                self.base,
                "POST",
                f"/api/profiles/{profile['id']}/start?{urllib.parse.urlencode({'url': url})}",
                token=self.token,
                timeout=60,
            )
            if status == 200 and body.get("success"):
                opened += 1
                _hide_pid(profile.get("pid"))
            else:
                self._error(
                    "open_tab",
                    f"{profile['name']}: {error or body.get('detail') or body.get('error')}",
                )
            try:
                _pages, closed_here, close_error = _close_soak_tabs(
                    profile["port"], self.soak_base
                )
                closed += closed_here
                if close_error:
                    self._error("close_tab", f"{profile['name']}: {close_error}")
            except Exception as exc:
                self._error("close_tab", f"{profile['name']}: {type(exc).__name__}")
        self._tab_cycles += 1
        self._tabs_opened += opened
        self._tabs_closed += closed
        self.state["counters"]["tab_cycles"] = self._tab_cycles
        self.state["counters"]["tabs_opened"] = self._tabs_opened
        self.state["counters"]["tabs_closed"] = self._tabs_closed
        self._event("tab_cycle", opened=opened, closed=closed)

    def _lifecycle_cycle(self):
        active = [item for item in self.profiles if item["active"]]
        if self.critical:
            return
        if not active:
            for profile in self.profiles:
                if self._start_profile(profile):
                    break
            self._save()
            return
        if len(active) < self.min_active:
            for profile in self.profiles:
                if not profile["active"] and self._start_profile(profile):
                    break
            self._save()
            return
        if len(active) <= self.min_active:
            self._event("lifecycle_skipped_min_active", active=len(active))
            return
        profile = active[self._cycle_index % len(active)]
        self._cycle_index += 1
        stopped = self._stop_profile(profile, "cycle")
        if stopped or not profile["active"]:
            self._start_profile(profile)
        self._lifecycle_cycles += 1
        self.state["counters"]["lifecycle_cycles"] = self._lifecycle_cycles
        self._event("lifecycle_cycle", profile=profile["name"])
        self._save()

    def _copy_cycle(self):
        if self.critical:
            return
        candidates = [item for item in self.profiles if item["active"]]
        if not candidates or len(candidates) <= self.min_active:
            return
        source = candidates[self._copy_index % len(candidates)]
        self._copy_index += 1
        if not self._stop_profile(source, "copy_source"):
            return
        operation_id = str(uuid.uuid4())
        status, body, _latency, error = _http(
            self.base,
            "POST",
            f"/api/profiles/{source['id']}/duplicate?copy_data=true",
            token=self.token,
            headers={"Idempotency-Key": operation_id},
            timeout=300,
        )
        target_id = body.get("id") if isinstance(body, dict) else None
        if status == 200 and target_id:
            target_dir = os.path.join(
                self.data_dir, "profiles", target_id, "user_data"
            )
            size = 0
            if os.path.isdir(target_dir):
                for root, _dirs, files in os.walk(target_dir):
                    for file_name in files:
                        try:
                            size += os.path.getsize(os.path.join(root, file_name))
                        except OSError:
                            pass
            if size <= 0:
                self._error(
                    "copy_verify",
                    f"{source['name']}: target {target_id} không có dữ liệu",
                )
            delete_status, delete_body, _latency, delete_error = _http(
                self.base,
                "DELETE",
                f"/api/profiles/{target_id}",
                token=self.token,
                timeout=120,
            )
            if delete_status != 200 or not delete_body.get("success"):
                self._error(
                    "copy_cleanup",
                    f"target {target_id}: {delete_error or delete_body.get('detail')}",
                )
            else:
                self._event("copy_verified", source=source["name"], bytes=size)
        else:
            self._error(
                "copy",
                f"{source['name']}: {error or body.get('detail') or body.get('error') or status}",
                code=body.get("code") if isinstance(body, dict) else None,
            )
            if target_id:
                _http(
                    self.base,
                    "DELETE",
                    f"/api/profiles/{target_id}",
                    token=self.token,
                    timeout=120,
                )
        self._start_profile(source)
        self._copy_cycles += 1
        self.state["counters"]["copy_cycles"] = self._copy_cycles
        self._event(
            "copy_cycle",
            source=source["name"],
            status=status,
            operation_id=operation_id,
        )
        self._save()

    def _check_stop_requested(self):
        if os.path.exists(self.stop_path):
            self.stop_requested = True
            self.state["stop_requested"] = True
            self._event("stop_requested")

    def _loop(self):
        next_sample = time.monotonic()
        next_tab = next_sample + self.args.tab_interval
        next_cycle = next_sample + self.args.cycle_interval + 25.0
        next_copy = next_sample + min(self.args.copy_interval, 3600)
        while True:
            self._check_stop_requested()
            if self.stop_requested or self.critical:
                break
            if time.time() >= self._deadline_ts:
                break
            now = time.monotonic()
            if now >= next_sample:
                self._sample()
                next_sample = max(next_sample + self.args.sample_interval, now + 1)
            if now >= next_tab:
                self._tab_cycle()
                next_tab = max(next_tab + self.args.tab_interval, now + 1)
            if now >= next_cycle:
                self._lifecycle_cycle()
                next_cycle = max(next_cycle + self.args.cycle_interval, now + 1)
            if now >= next_copy:
                self._copy_cycle()
                next_copy = max(next_copy + self.args.copy_interval, now + 1)
            time.sleep(1)

    def _finalize(self):
        completed = (
            time.time() >= self._deadline_ts
            and not self.stop_requested
            and not self.critical
        )
        self.state["status"] = "stopping"
        self._save()
        for profile in self.profiles:
            if profile["active"]:
                self._stop_profile(profile, "finalize")
        if self.server and self.server.poll() is None:
            status, _body, _latency, _error = _http(
                self.base,
                "POST",
                "/api/system/shutdown",
                token=self.token,
                timeout=120,
            )
            try:
                self.server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.server.terminate()
                try:
                    self.server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.server.kill()
        if self.local_server:
            self.local_server.shutdown()
            self.local_server.server_close()
        if self.server_log:
            try:
                self.server_log.close()
            except Exception:
                pass
        orphan_count = 0
        chrome = _chrome_processes(self._chrome_map())
        for profile in self.profiles:
            pids = chrome.get(profile["id"], {}).get("pids", [])
            if pids:
                orphan_count += len(pids)
                self._error(
                    "orphan_final",
                    f"{profile['name']}: còn {len(pids)} process sau finalize",
                    critical=True,
                )
        artifacts = self._artifact_counts()
        latencies = sorted(self._api_latencies)
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else None
        duration = time.time() - self._started_ts
        passed = (
            completed
            and not self.critical
            and orphan_count == 0
            and not self.errors
        )
        self.state["finished_at"] = _now_iso()
        self.state["status"] = "completed" if passed else "failed"
        self.state["summary"] = {
            "passed": passed,
            "completed_duration": completed,
            "duration_seconds": round(duration, 1),
            "planned_hours": self.hours,
            "min_active": self.min_active,
            "stop_requested": self.stop_requested,
            "critical_reason": self.critical_reason,
            "samples": self._sample_count,
            "lifecycle_cycles": self._lifecycle_cycles,
            "tab_cycles": self._tab_cycles,
            "copy_cycles": self._copy_cycles,
            "tabs_opened": self._tabs_opened,
            "tabs_closed": self._tabs_closed,
            "memory_shed": self._shed_count,
            "errors": len(self.errors),
            "orphan_processes": orphan_count,
            "api_latency_p95_ms": round(p95, 1) if p95 is not None else None,
            "chrome_peak": self._chrome_peak,
            "server_peak": self._server_peak,
            "min_available_mb": round(self._min_available_mb, 1)
            if self._min_available_mb is not None
            else None,
            "artifacts": artifacts,
        }
        self._save()
        _atomic_json(os.path.join(self.results_dir, "soak_summary.json"), self.state["summary"])
        self._event(
            "finished",
            status=self.state["status"],
            passed=passed,
            duration_seconds=round(duration, 1),
        )
        return 0 if passed else 1

    def run(self):
        self._validate_dirs()
        _keep_awake(True)
        self._save()
        self._log(
            f"SOAK start: {self.hours}h, {self.target_profiles} profiles, "
            f"data={self.data_dir}, results={self.results_dir}"
        )
        try:
            self._start_local_server()
            self._start_dashboard()
            self._read_token()
            self._create_profiles()
            self.state["status"] = "running"
            self._save()
            self._ramp_up()
            self._loop()
        except SoakFailure as exc:
            self._error("runner", str(exc), critical=True)
        except Exception as exc:
            self._error("runner", f"{type(exc).__name__}: {exc}", critical=True)
            import traceback

            traceback.print_exc()
        finally:
            return_code = self._finalize()
            _keep_awake(False)
        return return_code


def _load_state(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _status(path):
    state = _load_state(path)
    print(json.dumps(
        {
            "status": state.get("status"),
            "pid": state.get("pid"),
            "started_at": state.get("started_at"),
            "deadline_at": state.get("deadline_at"),
            "last_event": state.get("last_event"),
            "counters": state.get("counters"),
            "profiles": state.get("profiles"),
            "last_sample": state.get("last_sample"),
            "summary": state.get("summary"),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def _stop(path):
    state_path = os.path.abspath(path)
    results_dir = os.path.dirname(state_path)
    stop_path = os.path.join(results_dir, STOP_FILENAME)
    with open(stop_path, "w", encoding="utf-8") as fh:
        fh.write(_now_iso())
    print(f"Đã gửi yêu cầu dừng soak: {stop_path}")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Soak test Antidetect Python")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Chạy soak")
    run_parser.add_argument("--hours", type=float, default=24.0)
    run_parser.add_argument("--profiles", type=int, default=5)
    run_parser.add_argument("--min-active", type=int, default=0)
    run_parser.add_argument("--port", type=int, default=0)
    run_parser.add_argument("--data-dir", default="")
    run_parser.add_argument("--results-dir", default="")
    run_parser.add_argument("--sample-interval", type=float, default=30.0)
    run_parser.add_argument("--tab-interval", type=float, default=300.0)
    run_parser.add_argument("--cycle-interval", type=float, default=600.0)
    run_parser.add_argument("--copy-interval", type=float, default=21600.0)
    run_parser.add_argument("--ramp-delay", type=float, default=8.0)
    run_parser.add_argument("--min-free-mb", type=float, default=900.0)
    run_parser.add_argument("--shed-free-mb", type=float, default=650.0)

    status_parser = sub.add_parser("status", help="Đọc trạng thái soak")
    status_parser.add_argument("--state", required=True)
    stop_parser = sub.add_parser("stop", help="Yêu cầu dừng soak")
    stop_parser.add_argument("--state", required=True)

    args = parser.parse_args(argv)
    if args.command == "status":
        return _status(args.state)
    if args.command == "stop":
        return _stop(args.state)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if not args.data_dir:
        args.data_dir = os.path.join(
            tempfile.gettempdir(), f"antidetect-soak-{stamp}"
        )
    if not args.results_dir:
        args.results_dir = os.path.join(
            tempfile.gettempdir(), f"antidetect-soak-results-{stamp}"
        )
    return SoakRunner(args).run()


if __name__ == "__main__":
    sys.exit(main())
