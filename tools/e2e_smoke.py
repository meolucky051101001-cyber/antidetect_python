"""E2E smoke test: khởi động server thật và sweep toàn bộ API chính.

Dùng cho kiểm tra cuối trước khi vận hành. Yêu cầu ``ANTIDETECT_DATA_DIR`` trỏ
tới thư mục dữ liệu TẠM — script từ chối chạy nếu trúng ``data/`` thật của repo.

Các bước: health → auth (401/403/login) → CRUD profile → expected_revision →
start browser thật (check ``degraded``) → status running → mở tab → đọc
cookies → stop → switch-location-all → delete → shutdown; cuối cùng kiểm tra
server thoát sạch. Mỗi bước PASS/FAIL được in và tổng kết trả exit code.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PORT = int(os.environ.get("E2E_PORT", "8788"))
BASE = f"http://127.0.0.1:{PORT}"


def _request(method, path, token=None, body=None, headers=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                return resp.status, json.loads(resp.read().decode() or "{}")
            except json.JSONDecodeError:
                return resp.status, {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)}


def main():
    data_dir = os.environ.get("ANTIDETECT_DATA_DIR") or ""
    if not data_dir:
        print("Thiếu ANTIDETECT_DATA_DIR. Hãy trỏ tới thư mục dữ liệu TẠM.")
        return 2
    real_data = os.path.realpath(os.path.join(BASE_DIR, "data"))
    if os.path.realpath(data_dir) == real_data:
        print("TỪ CHỐI: ANTIDETECT_DATA_DIR đang trỏ vào data/ thật.")
        return 2

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f": {detail}" if detail else ""), flush=True)

    # --- Khởi động server ---
    env = dict(os.environ)
    env["ANTIDETECT_NO_BROWSER"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "run.py"), f"--port={PORT}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=BASE_DIR,
    )
    print(f"[*] Server PID {proc.pid} — đợi /health...")
    deadline = time.time() + 30
    healthy = False
    while time.time() < deadline:
        status, body = _request("GET", "/health", timeout=3)
        if status == 200 and body.get("application") == "antidetect-python":
            healthy = True
            break
        time.sleep(0.4)

    try:
        check("server /health khởi động trong 30s", healthy)

        # --- Lấy access key từ runtime DPAPI (cùng Windows user) ---
        from antidetect.runtime_auth import read_runtime
        token = (read_runtime() or {}).get("access_key")
        check("đọc access_key từ runtime.dpapi", bool(token))

        # --- Auth ---
        status, _ = _request("GET", "/api/profiles")
        check("API không auth bị từ chối 401", status == 401)
        status, _ = _request(
            "GET", "/api/profiles",
            headers={"Origin": "https://example.invalid", "Sec-Fetch-Site": "cross-site"},
        )
        check("request cross-origin bị chặn 403", status == 403)
        status, _ = _request("POST", "/auth/login", body={"token": "sai-key"})
        check("login sai key bị từ chối 401", status == 401)
        # Login đọc token từ Authorization header, không phải body.
        status, _ = _request("POST", "/auth/login", token=token)
        check("login đúng access_key", status == 200)

        # --- Profiles CRUD ---
        status, body = _request("GET", "/api/profiles", token=token)
        check("GET /api/profiles", status == 200 and isinstance(body, list))
        base_count = len(body)

        status, body = _request(
            "POST", "/api/profiles",
            token=token,
            body={"name": "E2E Smoke", "os": "windows"},
        )
        check("POST tạo profile", status == 200 and body.get("name") == "E2E Smoke")
        pid_new = body["id"]
        revision = body["revision"]

        status, body = _request(
            "GET", "/api/fingerprint/options", token=token,
        )
        check(
            "GET /api/fingerprint/options",
            status == 200 and body.get("gpu_profiles") and body.get("host"),
        )

        status, body = _request(
            "PUT", f"/api/profiles/{pid_new}",
            token=token,
            body={"name": "E2E Smoke v2", "expected_revision": revision + 999},
        )
        check("PUT revision stale bị từ chối 409", status == 409)
        status, body = _request(
            "PUT", f"/api/profiles/{pid_new}",
            token=token,
            body={"name": "E2E Smoke v2", "expected_revision": revision},
        )
        check("PUT revision đúng", status == 200 and body.get("name") == "E2E Smoke v2")

        # --- Launch browser thật ---
        t0 = time.time()
        status, body = _request(
            "POST", f"/api/profiles/{pid_new}/start", token=token, timeout=120
        )
        launch_secs = round(time.time() - t0, 1)
        check(
            "start browser thật",
            status == 200 and body.get("success") and body.get("port"),
            f"{launch_secs}s, port {body.get('port')}, pid {body.get('pid')}",
        )
        check(
            "start không degraded (fingerprint đã được che)",
            body.get("degraded") is False,
            f"cdp_ready={body.get('cdp_ready')}, verified={body.get('configuration_verified')}",
        )

        status, body = _request("GET", "/api/profiles", token=token)
        entry = next((p for p in body if p["id"] == pid_new), {})
        check("profile có status=running sau start", entry.get("status") == "running")

        status, body = _request(
            "POST", f"/api/profiles/{pid_new}/start", token=token
        )
        check(
            "start lặp lại khi đang chạy → không spawn thêm",
            status == 200 and body.get("success") and "đang chạy" in (body.get("message") or ""),
        )

        status, body = _request(
            "POST", f"/api/profiles/{pid_new}/start?url=https://example.com",
            token=token, timeout=60,
        )
        check("mở tab mới qua start?url", status == 200 and body.get("success"))

        status, body = _request(
            "GET", f"/api/profiles/{pid_new}/cookies/export",
            token=token, headers={"x-confirm-cookie-access": "yes"}, timeout=60,
        )
        check(
            "cookies export (có confirm header)",
            status == 200 and body.get("success") and isinstance(body.get("cookies"), list),
            f"count={body.get('count')}",
        )
        status, _ = _request(
            "GET", f"/api/profiles/{pid_new}/cookies/export", token=token
        )
        check("cookies export thiếu confirm → 403", status == 403)

        status, body = _request(
            "POST", f"/api/profiles/{pid_new}/stop", token=token, timeout=60
        )
        check("stop browser", status == 200 and body.get("success"))

        # --- Thao tác hàng loạt ---
        status, body = _request(
            "POST", "/api/profiles/switch-location-all",
            token=token, body={"city": "hcm"}, timeout=120,
        )
        check(
            "switch-location-all",
            status == 200 and body.get("success") and body.get("count", 0) >= 1
            and not body.get("failed_profile_ids"),
            f"count={body.get('count')}",
        )

        status, body = _request("DELETE", f"/api/profiles/{pid_new}", token=token, timeout=60)
        check("delete profile", status == 200 and body.get("success"))
        status, _ = _request("DELETE", f"/api/profiles/{pid_new}", token=token)
        check("delete profile không tồn tại → 404", status == 404)

        # --- Shutdown ---
        status, body = _request("POST", "/api/system/shutdown", token=token, timeout=120)
        check("API shutdown", status == 200 and body.get("success"))
    finally:
        try:
            proc.wait(timeout=20)
            exited_clean = True
        except subprocess.TimeoutExpired:
            proc.kill()
            exited_clean = False
    check("server thoát sạch sau shutdown", exited_clean and proc.returncode is not None)

    passed = sum(1 for _, ok in results if ok)
    print(f"\nE2E SMOKE: {passed}/{len(results)} PASS")
    for name, ok in results:
        if not ok:
            print(f"  FAILED: {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
