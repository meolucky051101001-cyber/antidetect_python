import os
import sys
import json
import time
import uuid
import asyncio
import threading
import secrets
import zoneinfo
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# Add root folder to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.models import Profile, FingerprintConfig, ProxyConfig, REDACTED_SECRET
from antidetect.storage import ProfileManager, ProfileDeletionError, DATA_DIR, PROFILES_DATA_DIR
from antidetect.fingerprint import (
    generate_fingerprint,
    get_host_device_memory,
    locale_for_country,
    detect_browser_brand,
)
from antidetect.launcher import BrowserLauncher
from antidetect.proxy import check_proxy


def _auto_max_concurrent() -> int:
    """Số Chrome mở đồng thời khi start-all, chọn theo RAM của máy.

    Mỗi Chrome profile tiêu tốn ~300-600MB; với quy mô ~20 profile, mở dần
    theo batch tránh làm máy 16GB bị quá tải trong lúc các phiên trước chưa
    giữ ổn định.
    """
    ram = get_host_device_memory() or 8
    if ram <= 8:
        return 4
    if ram <= 16:
        return 6
    return 8


# --- Đợt A: uptime + RAM cho profile đang chạy ---

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil là optional
    psutil = None

_ram_cache: Dict[str, Any] = {"at": 0.0, "data": {}}


class ProfileRuntime(Profile):
    """Profile + trạng thái runtime chỉ đọc (uptime/RAM) cho GET /api/profiles."""
    uptime_seconds: Optional[int] = None
    ram_mb: Optional[float] = None


def _ram_by_profile_dir(user_data_dirs: Dict[str, str]) -> Dict[str, float]:
    """Tổng WorkingSet (MB) của các Chrome process theo user-data-dir.

    Cache 5 giây — psutil.process_iter quét toàn bộ process table, không rẻ
    khi gọi mỗi lần poll 3s với 20 profile.
    """
    if psutil is None:
        return {}
    now = time.time()
    if now - _ram_cache["at"] < 5.0:
        return {k: v for k, v in _ram_cache["data"].items() if k in user_data_dirs}
    result: Dict[str, float] = {}
    needle_map = {ud: pid for pid, ud in user_data_dirs.items() if ud}
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("chrome.exe", "msedge.exe", "chromium.exe"):
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            mem = proc.info.get("memory_info")
            if not mem:
                continue
            for ud, pid in needle_map.items():
                if ud and ud in joined:
                    result[pid] = result.get(pid, 0.0) + mem.rss / (1024 * 1024)
        except Exception:
            continue
    _ram_cache["at"] = now
    _ram_cache["data"] = result
    return {k: v for k, v in result.items() if k in user_data_dirs}


def _profiles_with_status():
    """List profiles + sync trạng thái running; chạy trong thread.

    is_running có thể spawn PowerShell (block tới 5s/lần) — gọi trực tiếp trên
    event loop sau khi restart server với nhiều browser đang mở sẽ đóng băng
    toàn bộ API (kể cả /health, /stop).
    """
    profiles = profile_manager.list_profiles()
    runtime_profiles = []
    running_dirs: Dict[str, str] = {}
    for p in profiles:
        if launcher.is_running(p.id):
            p.status = "running"
            p.port = launcher.active_ports.get(p.id)
            running_dirs[p.id] = os.path.join(PROFILES_DATA_DIR, p.id, "user_data")
    ram_map = _ram_by_profile_dir(running_dirs) if running_dirs else {}
    for p in profiles:
        item = ProfileRuntime(**p.model_dump())
        if p.status == "running":
            record = launcher._runtime_store.get(p.id) or {}
            started = record.get("started_at")
            if started:
                item.uptime_seconds = max(0, int(time.time()) - int(started))
            item.ram_mb = round(ram_map.get(p.id, 0.0), 1) or None
        runtime_profiles.append(item)
    return runtime_profiles


def _worker_pool(max_workers: int = 64):
    """Default executor thật cho asyncio.to_thread.

    anyio.to_thread limiter không điều khiển asyncio.to_thread; muốn batch
    start-all (~20 lệnh handshake CDP 10-15s) không chiếm hết thread mặc định
    của loop thì phải set_default_executor.
    """
    import concurrent.futures

    return concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="ad-worker"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Quy mô ~20 profile: mỗi lệnh start giữ một worker thread suốt handshake
    # CDP (~10-15s). Pool mặc định của loop chỉ có min(32, cpu+4) thread, nên
    # stop/list gửi ngay sau batch start sẽ xếp hàng — nâng lên 64.
    try:
        asyncio.get_running_loop().set_default_executor(_worker_pool())
    except Exception:
        pass
    # run.py needs the per-start key to open the UI with /#key. The record is
    # protected by the current Windows user's DPAPI.
    try:
        from antidetect.runtime_auth import write_runtime

        write_runtime(
            {
                "access_key": app.state.access_key,
                "session_key": app.state.session_key,
                "instance_id": app.state.instance_id,
            }
        )
    except Exception as e:
        print(f"[Web] Không ghi được runtime key: {e}")
    # Đợt C: lịch tự động — runner daemon dậy mỗi 30s.
    global _scheduler, _schedule_store
    try:
        from antidetect.scheduler import ScheduleStore, Scheduler

        _schedule_store = ScheduleStore(os.path.join(DATA_DIR, "schedules.json"))
        _scheduler = Scheduler(_schedule_store, callbacks={
            "start_all": _scheduler_start_all,
            "stop_all": launcher.stop_all,
            "sync_location_proxy": _scheduler_sync_all_locations,
        })
        _scheduler.start()
    except Exception as e:
        print(f"[Web] Không khởi động được scheduler: {e}")
    yield
    try:
        if _scheduler is not None:
            _scheduler.stop()
    except Exception:
        pass
    # Đóng toàn bộ trình duyệt đang chạy khi tắt server.
    launcher.stop_all()
    try:
        from antidetect.runtime_auth import delete_runtime

        delete_runtime()
    except Exception:
        pass


_scheduler = None
_schedule_store = None

app = FastAPI(
    title="Antidetect Python API",
    description="REST API & Dashboard cho Antidetect Browser",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.access_key = secrets.token_urlsafe(32)
app.state.session_key = secrets.token_urlsafe(32)
app.state.instance_id = secrets.token_hex(16)

# P0-06: Kho lưu trữ mã mở khóa một lần (One-Time Unlock Codes) với TTL 60 giây
_one_time_codes: Dict[str, float] = {}
_otc_lock = threading.Lock()


def create_unlock_code(ttl_seconds: int = 60) -> str:
    """Tạo mã mở khóa dùng 1 lần (OTC) có hiệu lực ngắn (mặc định 60s)."""
    now = time.time()
    code = f"otc_{secrets.token_urlsafe(32)}"
    with _otc_lock:
        expired = [k for k, exp in _one_time_codes.items() if exp < now]
        for k in expired:
            _one_time_codes.pop(k, None)
        _one_time_codes[code] = now + ttl_seconds
    return code


def consume_unlock_code(code: str) -> bool:
    """Kiểm tra và hủy ngay lập tức mã mở khóa nếu còn hạn (chống replay attack)."""
    now = time.time()
    with _otc_lock:
        exp = _one_time_codes.pop(code, None)
        if exp is not None and exp >= now:
            return True
    return False


def authorized(request):
    bearer = request.headers.get('authorization', '') or ''
    cookie = request.cookies.get('antidetect_session', '') or ''
    # Header decode latin-1 có thể chứa ký tự non-ASCII; encode về bytes để
    # compare_digest không ném TypeError (vốn thành lỗi 500) trên input lạ.
    key = ('Bearer ' + app.state.access_key).encode('utf-8', 'replace')
    session = app.state.session_key.encode('utf-8', 'replace')
    if bearer and secrets.compare_digest(bearer.encode('utf-8', 'replace'), key):
        return True
    if cookie and secrets.compare_digest(cookie.encode('utf-8', 'replace'), session):
        return True
    # Đợt C: API token dài hạn cho automation — so SHA-256(bearer) với hash
    # lưu DPAPI. Plaintext token không bao giờ nằm trên đĩa.
    if bearer.startswith('Bearer '):
        try:
            from antidetect.runtime_auth import read_api_token_hash
            stored = read_api_token_hash()
            if stored:
                import hashlib as _hashlib
                candidate = _hashlib.sha256(
                    bearer[7:].strip().encode('utf-8')
                ).hexdigest()
                if secrets.compare_digest(candidate, stored):
                    return True
        except Exception:
            pass
    return False


# Dashboard và API dùng cùng origin; không bật CORS cho các cổng localhost khác.
@app.middleware("http")
async def local_origin_check_middleware(request: Request, call_next):
    if request.url.hostname not in ("localhost", "127.0.0.1"):
        return JSONResponse(status_code=403, content={"error": "Host không hợp lệ."})
    path = request.url.path
    if path.startswith(("/api/", "/auth/")):
        from urllib.parse import urlparse

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        sec_fetch_site = request.headers.get("sec-fetch-site")

        if sec_fetch_site == "cross-site":
            return JSONResponse(status_code=403, content={"error": "Request cross-site bị từ chối."})

        expected_origin = f"{request.url.scheme}://{request.url.netloc}".lower()
        if origin:
            parsed = urlparse(origin)
            actual_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            if actual_origin != expected_origin:
                return JSONResponse(status_code=403, content={"error": "Origin không khớp dashboard."})
        elif referer:
            parsed = urlparse(referer)
            actual_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
            if actual_origin != expected_origin:
                return JSONResponse(status_code=403, content={"error": "Referer không khớp dashboard."})
        elif sec_fetch_site in ("cross-site", "same-site"):
            return JSONResponse(status_code=403, content={"error": "Request trình duyệt không có origin hợp lệ."})
    # /docs, /redoc và /openapi.json phơi toàn bộ API surface; yêu cầu cùng
    # phiên hợp lệ như /api/ thay vì để mở trên localhost.
    needs_auth = path.startswith('/api/') or path in (
        '/docs', '/redoc', '/openapi.json'
    ) or path.startswith(('/docs/', '/redoc/'))
    if needs_auth and not authorized(request):
        return JSONResponse(status_code=401, content={"detail": "Phiên truy cập hết hạn. Hãy mở lại ứng dụng từ shortcut."})
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    # P2-04: Content Security Policy chống XSS và script injection.
    # 'unsafe-inline' còn cần vì dashboard dùng inline handler/script; các
    # directive dưới đây chặn plugin, base tag và form gửi ra ngoài.
    # Swagger UI/ReDoc mặc định tải bundle từ jsdelivr và font từ Google
    # Fonts; chỉ nới CSP trên đúng trang docs (đã yêu cầu phiên), dashboard
    # vẫn giữ CSP siết.
    if path in ('/docs', '/redoc') or path.startswith(('/docs/', '/redoc/')):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
            "https://fonts.googleapis.com; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
    else:
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
    return response

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

profile_manager = ProfileManager()
launcher = BrowserLauncher(profile_manager)

# P1-06: Khóa và theo dõi trạng thái hàng đợi Start-All
_bulk_launch_lock = threading.Lock()
_start_all_lock = threading.Lock()
_active_start_all_job: Optional[Dict[str, Any]] = None
_start_all_cancel_event: Optional[threading.Event] = None


def validate_iana_timezone(tz_name: str) -> bool:
    """P1-03: Kiểm tra múi giờ chuẩn IANA hợp lệ."""
    if not tz_name or not isinstance(tz_name, str):
        return False
    try:
        zoneinfo.ZoneInfo(tz_name.strip())
        return True
    except Exception:
        return False


# --- Data Transfer Objects ---
class CreateProfileRequest(BaseModel):
    name: str = "Profile Mới"
    os: str = "windows"  # windows, macos, linux
    proxy: Optional[ProxyConfig] = None
    notes: Optional[str] = ""
    tags: Optional[List[str]] = []
    city: Optional[str] = "hanoi"  # Mặc định Hà Nội theo vị trí người dùng
    # Tuỳ chọn tự sinh fingerprint hoặc dùng cấu hình tuỳ biến
    fingerprint: Optional[FingerprintConfig] = None


class SwitchLocationRequest(BaseModel):
    city: str = "hanoi"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    proxy: Optional[ProxyConfig] = None
    fingerprint: Optional[FingerprintConfig] = None
    # Browser pin riêng (Giai đoạn 2): tên trong browsers/ hoặc đường dẫn
    # tuyệt đối; None = giữ nguyên, "" = về mặc định của tool.
    browser_path: Optional[str] = None
    # Opt-in strict concurrency: truyền revision mình đã đọc; nếu profile bị
    # sửa bởi client khác từ đó, update bị từ chối 409 thay vì ghi đè.
    expected_revision: Optional[int] = None


class CheckProxyRequest(BaseModel):
    protocol: str = "http"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    profile_id: Optional[str] = None


class ImportCookiesRequest(BaseModel):
    cookies: List[Dict[str, Any]]


# --- Web Page Route ---
@app.get('/health')
async def health():
    return {'application': 'antidetect-python', 'instance_id': app.state.instance_id}


@app.get('/unlock', response_class=HTMLResponse)
async def unlock(request: Request):
    return templates.TemplateResponse(request=request, name='unlock.html', context={})


@app.post('/auth/login')
async def login(request: Request):
    auth_header = request.headers.get('authorization', '')
    token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else auth_header.strip()

    is_valid = False
    if token:
        if token.startswith("otc_"):
            # P0-06: Xác thực mã dùng một lần và tiêu hủy ngay
            is_valid = consume_unlock_code(token)
        elif secrets.compare_digest(
            token.encode('utf-8', 'replace'),
            app.state.access_key.encode('utf-8', 'replace'),
        ):
            # Cho phép access_key cố định qua API trực tiếp hoặc test suite.
            # So sánh trên bytes để input non-ASCII trả 401 thay vì TypeError 500.
            is_valid = True

    if not is_valid:
        raise HTTPException(status_code=401, detail='Khóa mở ứng dụng không hợp lệ hoặc đã hết hạn')

    response = JSONResponse({'success': True})
    response.set_cookie('antidetect_session', app.state.session_key, httponly=True, samesite='strict', path='/')
    return response


@app.post('/auth/create-unlock-code')
async def create_unlock_code_endpoint(request: Request):
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Chưa xác thực')
    code = create_unlock_code(ttl_seconds=60)
    return {"unlock_code": code, "expires_in": 60}


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    if not authorized(request):
        return templates.TemplateResponse(
            request=request, name="unlock.html", context={}
        )
    profiles = await asyncio.to_thread(_profiles_with_status)
    return templates.TemplateResponse(
        request=request, name="index.html", context={"profiles": profiles}
    )


# --- REST API Endpoints ---
@app.get("/api/profiles", response_model=List[ProfileRuntime])
async def list_profiles():
    return await asyncio.to_thread(_profiles_with_status)


@app.post("/api/profiles", response_model=Profile)
async def create_profile(req: CreateProfileRequest):
    # Nếu chưa truyền fingerprint, tự sinh theo OS, vị trí và proxy
    fp = req.fingerprint
    proxy = req.proxy or ProxyConfig()

    if fp is None:
        from antidetect.fingerprint import get_location_coordinates
        auto_lat, auto_lon, auto_tz = get_location_coordinates(req.city or "hanoi", randomize=True)
        timezone = auto_tz
        locale = "vi-VN"
        lat, lon = auto_lat, auto_lon

        # Nếu có proxy và bật, thử lấy geoip để đồng bộ múi giờ, locale & tọa độ theo proxy
        if proxy.enabled and proxy.host and proxy.port:
            geo = await asyncio.to_thread(check_proxy, proxy, 5)
            if geo.get("success"):
                timezone = geo.get("timezone", timezone)
                locale = locale_for_country(geo.get("country_code"), default=locale)
                lat = geo.get("lat", lat)
                lon = geo.get("lon", lon)

        fp = generate_fingerprint(
            target_os=req.os,
            timezone=timezone,
            locale=locale,
            latitude=lat,
            longitude=lon,
            city=req.city or "hanoi",
            browser_brand=detect_browser_brand(getattr(launcher, "browser_path", None)),
        )

    profile = profile_manager.create_profile(
        name=req.name,
        target_os=req.os,
        proxy=proxy,
        fingerprint=fp,
        notes=req.notes or "",
        tags=req.tags or []
    )
    return profile


class StartAllBody(BaseModel):
    profile_ids: Optional[List[str]] = None


@app.post("/api/profiles/start-all")
async def start_all_profiles(
    url: Optional[str] = None,
    max_concurrent: Optional[int] = None,
    body: Optional[StartAllBody] = None,
):
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")
    if max_concurrent is None:
        # Chọn theo RAM máy (16GB → 6 Chrome đồng thời) — an toàn cho quy mô
        # ~20 profile; client vẫn override được bằng query param.
        max_concurrent = _auto_max_concurrent()
    max_concurrent = max(0, int(max_concurrent or 0))

    global _active_start_all_job, _start_all_cancel_event
    body_ids = body.profile_ids if body is not None else None
    if body_ids is not None and not body_ids:
        raise HTTPException(
            status_code=400,
            detail="Danh sách profile được chọn trống. Bỏ profile_ids để chạy toàn bộ.",
        )
    profiles = await asyncio.to_thread(_profiles_with_status)
    selected_ids = set(body_ids) if body_ids is not None else None
    if selected_ids is not None:
        profiles = [p for p in profiles if p.id in selected_ids]
        if not profiles:
            raise HTTPException(status_code=404, detail="Không profile nào trong danh sách chọn tồn tại.")

    # Lock order: _bulk_launch_lock -> _start_all_lock. The bulk thread already
    # holds the bulk lock for its whole lifetime before touching job state, so
    # acquiring in this order cannot deadlock. Failing to acquire it means
    # another bulk launch is alive, not merely a stale job record.
    if not _bulk_launch_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Một tác vụ Start-All khác đang chạy. Hãy đợi hoặc hủy tác vụ hiện tại."
        )
    try:
        with _start_all_lock:
            if _active_start_all_job is not None and _active_start_all_job.get("status") in ("running", "cancelling"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Đang có tác vụ Start-All ({_active_start_all_job.get('job_id')}) đang xử lý. Hãy đợi hoặc hủy tác vụ hiện tại."
                )
            to_start = [p for p in profiles if p.status != "running"]
            job_id = str(uuid.uuid4())
            cancel_event = threading.Event()
            _start_all_cancel_event = cancel_event
            _active_start_all_job = {
                "job_id": job_id,
                "status": "running",
                "total": len(profiles),
                "starting_count": len(to_start),
                "completed_count": 0,
                "failed_count": 0,
                "max_concurrent": max_concurrent,
                "results": [],
                "start_time": time.time(),
            }
    except BaseException:
        _bulk_launch_lock.release()
        raise

    def run_bulk():
        status = "completed"
        error = None
        try:
            launcher.launch_all(
                delay=0.8,
                open_url=url,
                max_concurrent=max_concurrent,
                cancel_event=cancel_event,
                progress_callback=on_progress,
                profile_ids=list(selected_ids) if selected_ids is not None else None,
            )
            if cancel_event.is_set():
                status = "cancelled"
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
        finally:
            with _start_all_lock:
                if _active_start_all_job and _active_start_all_job.get("job_id") == job_id:
                    _active_start_all_job["status"] = status
                    _active_start_all_job["end_time"] = time.time()
                    if error:
                        _active_start_all_job["error"] = error
            _bulk_launch_lock.release()

    def on_progress(p_id, res):
        with _start_all_lock:
            if _active_start_all_job and _active_start_all_job.get("job_id") == job_id:
                if res.get("success"):
                    _active_start_all_job["completed_count"] += 1
                else:
                    _active_start_all_job["failed_count"] += 1
                _active_start_all_job["results"].append({"profile_id": p_id, "result": res})

    try:
        threading.Thread(target=run_bulk, daemon=True).start()
    except Exception as exc:
        with _start_all_lock:
            if _active_start_all_job and _active_start_all_job.get("job_id") == job_id:
                _active_start_all_job["status"] = "failed"
                _active_start_all_job["error"] = str(exc)[:500]
                _active_start_all_job["end_time"] = time.time()
        _bulk_launch_lock.release()
        raise HTTPException(status_code=500, detail="Không khởi động được luồng Start-All.") from exc

    limit_label = (
        "không giới hạn" if max_concurrent <= 0 else f"tối đa {max_concurrent}"
    )
    return {
        "success": True,
        "message": f"Đang khởi chạy {len(to_start)} profile ({limit_label} cùng lúc)...",
        "job_id": job_id,
        "total": len(profiles),
        "starting_count": len(to_start),
        "max_concurrent": max_concurrent,
    }


@app.get("/api/profiles/start-all/status")
async def get_start_all_status():
    """P1-06: Lấy tiến độ và trạng thái của hàng đợi Start-All."""
    with _start_all_lock:
        if not _active_start_all_job:
            return {"active": False, "job": None}
        return {
            "active": _active_start_all_job.get("status") in ("running", "cancelling"),
            "job": dict(_active_start_all_job)
        }


@app.post("/api/profiles/start-all/cancel")
async def cancel_start_all():
    """P1-06: Hủy bỏ hàng đợi Start-All đang chạy."""
    with _start_all_lock:
        if not _active_start_all_job or _active_start_all_job.get("status") != "running":
            return {"success": False, "message": "Không có tiến trình Start-All nào đang chạy."}
        if _start_all_cancel_event:
            _start_all_cancel_event.set()
        _active_start_all_job["status"] = "cancelling"
        return {"success": True, "message": "Đã gửi yêu cầu dừng hàng đợi Start-All."}


class StopAllBody(BaseModel):
    profile_ids: Optional[List[str]] = None


@app.post("/api/profiles/stop-all")
async def stop_all_profiles(body: Optional[StopAllBody] = None):
    ids = body.profile_ids if body else None
    if ids is not None and not ids:
        raise HTTPException(
            status_code=400,
            detail="Danh sách profile được chọn trống. Bỏ profile_ids để dừng toàn bộ.",
        )
    return await asyncio.to_thread(launcher.stop_all, ids)


@app.get("/api/profiles/export")
async def export_profiles(request: Request):
    """Xuất metadata toàn bộ profile ra JSON (KHÔNG kèm secret).

    Mật khẩu proxy không bao giờ xuất (DPAPI gắn máy — import sang máy khác
    cũng không dùng được). Yêu cầu header x-confirm-export: yes như cookies.
    """
    if request.headers.get("x-confirm-export") != "yes":
        raise HTTPException(status_code=403, detail="Cần xác nhận x-confirm-export: yes trước khi export.")
    profiles = profile_manager.list_profiles()
    items = []
    for p in profiles:
        d = p.model_dump()
        d.pop("id", None)
        d.pop("status", None)
        d.pop("port", None)
        d.pop("pid", None)
        d.pop("revision", None)
        if d.get("proxy"):
            d["proxy"]["password"] = None
        items.append(d)
    from datetime import datetime

    payload = {
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "profiles": items,
    }
    filename = f"antidetect-profiles-{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


class ImportProfilesRequest(BaseModel):
    profiles: List[Dict[str, Any]] = []


@app.get("/api/profiles/{profile_id}", response_model=Profile)
async def get_profile(profile_id: str):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if launcher.is_running(p.id):
        p.status = "running"
        p.port = launcher.active_ports.get(p.id)
    else:
        p.status = "stopped"
    return p


@app.put("/api/profiles/{profile_id}", response_model=Profile)
async def update_profile_endpoint(profile_id: str, req: UpdateProfileRequest):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    if req.name is not None:
        p.name = req.name
    if req.notes is not None:
        p.notes = req.notes
    if req.tags is not None:
        p.tags = req.tags
    if req.proxy is not None:
        same_endpoint = bool(
            p.proxy
            and req.proxy.identity_key() == p.proxy.identity_key()
        )
        if (
            same_endpoint
            and p.proxy
            and p.proxy.password
            and req.proxy.password in (None, "", REDACTED_SECRET)
        ):
            req.proxy.password = p.proxy.password
        p.proxy = req.proxy
    if req.fingerprint is not None:
        p.fingerprint = req.fingerprint
    if req.browser_path is not None:
        # "" = reset về browser mặc định của tool.
        p.browser_path = req.browser_path.strip() or None

    try:
        updated = profile_manager.update_profile(
            p, expected_revision=req.expected_revision
        )
    except RuntimeError as e:
        # expected_revision không khớp: profile đã bị client khác sửa.
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not updated:
        # Profile biến mất giữa get và update (bị xóa bởi phiên khác).
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated


@app.post("/api/profiles/{profile_id}/reset-fingerprint", response_model=Profile)
async def reset_fingerprint_endpoint(profile_id: str, target_os: Optional[str] = None):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")

    os_to_use = target_os or p.fingerprint.os
    timezone = p.fingerprint.timezone
    locale = p.fingerprint.locale
    lat = p.fingerprint.latitude
    lon = p.fingerprint.longitude

    # Nếu profile có proxy, kiểm tra xem có cần update geo/locale không
    if p.proxy and p.proxy.enabled and p.proxy.host and p.proxy.port:
        geo = await asyncio.to_thread(check_proxy, p.proxy, 4)
        if geo.get("success"):
            timezone = geo.get("timezone", timezone)
            locale = locale_for_country(geo.get("country_code"), default=locale)
            lat = geo.get("lat", lat)
            lon = geo.get("lon", lon)

    from antidetect.fingerprint import get_location_coordinates
    auto_lat, auto_lon, _ = get_location_coordinates("hanoi", randomize=True)
    new_fp = generate_fingerprint(
        target_os=os_to_use,
        timezone=timezone,
        locale=locale,
        latitude=lat if lat is not None else auto_lat,
        longitude=lon if lon is not None else auto_lon,
        browser_brand=detect_browser_brand(getattr(launcher, "browser_path", None)),
    )
    p.fingerprint = new_fp
    updated = profile_manager.update_profile(p)
    if not updated:
        # Profile bị xóa bởi phiên khác giữa get và update.
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated


@app.get("/api/fingerprint/options")
async def get_fingerprint_options():
    from antidetect.fingerprint import (
        GPU_PROFILES,
        SCREEN_RESOLUTIONS,
        HARDWARE_PRESETS,
        POPULAR_LOCATIONS,
        get_host_device_memory,
        get_host_screen_info,
        get_installed_chrome_version,
        host_os,
    )
    ver_info = get_installed_chrome_version()
    browser_ver = ver_info[1] if ver_info else "155.0.8059.85"
    host_width, host_height, host_scale = get_host_screen_info()
    common_timezones = [
        "Asia/Ho_Chi_Minh",
        "America/New_York",
        "America/Los_Angeles",
        "America/Chicago",
        "Europe/London",
        "Europe/Paris",
        "Europe/Berlin",
        "Asia/Tokyo",
        "Asia/Seoul",
        "Asia/Singapore",
        "Asia/Bangkok",
        "Australia/Sydney"
    ]
    return {
        "gpu_profiles": GPU_PROFILES,
        "screen_resolutions": SCREEN_RESOLUTIONS,
        "hardware_presets": HARDWARE_PRESETS,
        "timezones": common_timezones,
        "locations": POPULAR_LOCATIONS,
        "browser_version": browser_ver,
        "host": {
            "os": host_os(),
            "screen_width": host_width,
            "screen_height": host_height,
            "device_pixel_ratio": round(max(0.5, host_scale), 2),
            "device_memory": get_host_device_memory(),
        },
    }


@app.post("/api/profiles/{profile_id}/set-location", response_model=Profile)
async def set_profile_location(profile_id: str, req: SwitchLocationRequest):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    from antidetect.fingerprint import get_location_coordinates
    if req.latitude is not None and req.longitude is not None:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")
        lat = req.latitude
        lon = req.longitude
        tz = req.timezone or p.fingerprint.timezone
    else:
        lat, lon, tz = get_location_coordinates(req.city, randomize=True)
        if req.timezone:
            tz = req.timezone

    if not validate_iana_timezone(tz):
        raise HTTPException(status_code=400, detail=f"Múi giờ IANA '{tz}' không hợp lệ")

    p.fingerprint.latitude = lat
    p.fingerprint.longitude = lon
    p.fingerprint.timezone = tz
    updated = profile_manager.update_profile(p)
    if not updated:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated


def _apply_geo_to_profile(p, geo: dict) -> dict:
    """Áp GeoIP result vào fingerprint profile (sync — gọi trong thread)."""
    changes = {}
    tz = geo.get("timezone")
    if tz and validate_iana_timezone(tz) and tz != p.fingerprint.timezone:
        changes["timezone"] = [p.fingerprint.timezone, tz]
        p.fingerprint.timezone = tz
    lat, lon = geo.get("lat"), geo.get("lon")
    if lat is not None and lon is not None:
        lat, lon = float(lat), float(lon)
        if abs((p.fingerprint.latitude or 0) - lat) > 0.001 or abs((p.fingerprint.longitude or 0) - lon) > 0.001:
            changes["latitude"] = [p.fingerprint.latitude, lat]
            changes["longitude"] = [p.fingerprint.longitude, lon]
            p.fingerprint.latitude = lat
            p.fingerprint.longitude = lon
    country = geo.get("country_code")
    if country:
        new_locale = locale_for_country(country, default=p.fingerprint.locale)
        if new_locale != p.fingerprint.locale:
            changes["locale"] = [p.fingerprint.locale, new_locale]
            p.fingerprint.locale = new_locale
            base_lang = new_locale.split("-")[0]
            p.fingerprint.languages = [new_locale, base_lang, "en-US", "en"]
            seen = set()
            p.fingerprint.languages = [l for l in p.fingerprint.languages if not (l in seen or seen.add(l))]
    if changes:
        profile_manager.update_profile(p)
    return changes


def _scheduler_start_all(profile_ids=None):
    """Callback scheduler: Scheduler.run gọi cb(ids) positionally.

    Không truyền thẳng launcher.launch_all vì tham số đầu của nó là ``delay``
    (list ID sẽ bị hiểu thành delay) và sẽ raise TypeError.
    """
    return launcher.launch_all(delay=0.8, profile_ids=profile_ids)


def _scheduler_sync_all_locations(profile_ids=None):
    """Callback scheduler (sync, chạy trong thread scheduler)."""
    profiles = profile_manager.list_profiles()
    if profile_ids:
        wanted = set(profile_ids)
        profiles = [p for p in profiles if p.id in wanted]
    done, skipped = 0, 0
    for p in profiles:
        if not (p.proxy and p.proxy.enabled and p.proxy.host):
            skipped += 1
            continue
        try:
            geo = check_proxy(p.proxy, 12)
            if geo.get("success"):
                if _apply_geo_to_profile(p, geo):
                    done += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return {"synced": done, "skipped": skipped, "total": len(profiles)}


@app.post("/api/profiles/{profile_id}/sync-location-proxy")
async def sync_location_from_proxy(profile_id: str):
    """Giai đoạn 1 anti-detection: đồng bộ timezone/geo/locale theo IP proxy.

    Đổi proxy sau khi tạo profile là lỗi nhất quán phổ biến nhất (IP Mỹ +
    timezone Hà Nội → detector flag ngay). Endpoint này hỏi GeoIP QUA proxy
    của profile và cập nhật fingerprint cho khớp; trả về từng thay đổi để
    UI/automation hiển thị.
    """
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if not (p.proxy and p.proxy.enabled and p.proxy.host):
        raise HTTPException(status_code=400, detail="Profile chưa bật proxy — không có IP để đồng bộ.")

    geo = await asyncio.to_thread(check_proxy, p.proxy, 12)
    if not geo.get("success"):
        raise HTTPException(status_code=502, detail=f"Không truy vấn được GeoIP qua proxy: {geo.get('error') or 'lỗi không xác định'}")

    changes = await asyncio.to_thread(_apply_geo_to_profile, p, geo)
    if not profile_manager.get_profile(profile_id):
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return {
        "success": True,
        "geo": {"ip": geo.get("ip"), "country": geo.get("country"),
                "city": geo.get("city"), "isp": geo.get("isp")},
        "changes": changes,
        "message": "Fingerprint đã khớp IP proxy." if changes
                   else "Fingerprint đã khớp sẵn với IP proxy — không cần thay đổi.",
    }


@app.post("/api/profiles/switch-location-all")
async def switch_all_profiles_location(req: SwitchLocationRequest):
    """P1-03: Chuyển đổi vị trí toàn bộ profiles, kiểm tra 0 profiles, validate ZoneInfo."""
    from antidetect.fingerprint import get_location_coordinates

    custom_coords = req.latitude is not None and req.longitude is not None
    if custom_coords:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")

    if req.timezone and not validate_iana_timezone(req.timezone):
        raise HTTPException(status_code=400, detail=f"Múi giờ IANA '{req.timezone}' không hợp lệ")

    def run_switch():
        # Chạy trong thread: list + update từng profile + is_running đều là
        # I/O/PowerShell đồng bộ. Batch ghi profiles.json đúng MỘT lần thay vì
        # N lần read+backup+write như từng update_profile riêng lẻ.
        profiles = profile_manager.list_profiles()
        if not profiles:
            return None, []
        updates = []
        for p in profiles:
            if custom_coords:
                lat = req.latitude
                lon = req.longitude
                tz = req.timezone or p.fingerprint.timezone
            else:
                lat, lon, tz = get_location_coordinates(req.city, randomize=True)
                if req.timezone:
                    tz = req.timezone
            if not validate_iana_timezone(tz):
                raise ValueError(f"Múi giờ IANA '{tz}' không hợp lệ")
            p.fingerprint.latitude = lat
            p.fingerprint.longitude = lon
            p.fingerprint.timezone = tz
            updates.append((p, lat, lon, tz))

        updated_ids = set(
            profile_manager.update_profiles_batch([p for p, _, _, _ in updates])
        )
        results = []
        for p, lat, lon, tz in updates:
            results.append({
                "id": p.id,
                "name": p.name,
                "city": req.city,
                "latitude": lat,
                "longitude": lon,
                "timezone": tz,
                "running": launcher.is_running(p.id),
                "updated": p.id in updated_ids,
            })
        return results, [p.id for p, _, _, _ in updates if p.id not in updated_ids]

    try:
        results, failed_ids = await asyncio.to_thread(run_switch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if results is None:
        raise HTTPException(status_code=404, detail="Không có profile nào để chuyển đổi vị trí")

    ok_count = len([r for r in results if r["updated"]])
    message = (
        f"Đã chuyển vị trí {ok_count} profile sang {req.city.upper()} thành công!"
        if not failed_ids
        else f"Đã chuyển {ok_count}/{len(results)} profile; {len(failed_ids)} profile không ghi được (đã bị thay đổi bởi phiên khác)."
    )
    return {
        "success": not failed_ids,
        "message": message,
        "count": ok_count,
        "failed_profile_ids": failed_ids,
        "profiles": results
    }


@app.get("/api/system/logs")
async def get_system_logs(lines: int = 200):
    """Đuôi app.log để xem nhật ký hệ thống ngay trên dashboard (read-only)."""
    lines = max(10, min(1000, int(lines or 200)))

    def read_tail():
        log_file = os.path.join(DATA_DIR, "app.log")
        if not os.path.exists(log_file):
            return []
        size = os.path.getsize(log_file)
        with open(log_file, "rb") as f:
            f.seek(max(0, size - 256 * 1024))  # đọc tối đa 256KB cuối
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-lines:]

    try:
        tail = await asyncio.to_thread(read_tail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không đọc được app.log: {e}")
    return {"success": True, "lines": tail, "count": len(tail)}


_token_rotate_lock = threading.Lock()
_token_rotate_times: list = []


@app.post("/api/auth/token/rotate")
async def rotate_api_token(request: Request):
    """Tạo API token dài hạn mới (token cũ vô hiệu ngay). Trả plaintext 1 lần."""
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Chưa xác thực')
    now = time.time()
    with _token_rotate_lock:
        _token_rotate_times[:] = [t for t in _token_rotate_times if now - t < 60]
        if len(_token_rotate_times) >= 5:
            raise HTTPException(status_code=429, detail='Quá nhiều lần tạo token trong 1 phút.')
        _token_rotate_times.append(now)
    import hashlib as _hashlib
    token = secrets.token_urlsafe(32)
    from antidetect.runtime_auth import write_api_token_hash
    await asyncio.to_thread(
        write_api_token_hash,
        _hashlib.sha256(token.encode('utf-8')).hexdigest(),
    )
    return {"success": True, "token": token,
            "note": "Token chỉ hiển thị MỘT lần này. Lưu ngay vào chỗ an toàn; làm mất chỉ có thể rotate token mới."}


@app.delete("/api/auth/token")
async def revoke_api_token(request: Request):
    """Thu hồi API token dài hạn."""
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Chưa xác thực')
    from antidetect.runtime_auth import delete_api_token_hash
    await asyncio.to_thread(delete_api_token_hash)
    return {"success": True, "message": "Đã thu hồi API token."}


# --- Đợt C: Lịch tự động ---

class ScheduleCreateRequest(BaseModel):
    name: str = ""
    time: str  # HH:MM
    action: str  # start_all | stop_all | sync_location_proxy
    profile_ids: Optional[List[str]] = None
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = None
    time: Optional[str] = None
    action: Optional[str] = None
    profile_ids: Optional[List[str]] = None
    enabled: Optional[bool] = None


@app.get("/api/schedules")
async def list_schedules():
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    return {"success": True, "rules": _schedule_store.list(),
            "last_tick": (_scheduler.last_run_info.get("last_tick") if _scheduler else None)}


@app.post("/api/schedules")
async def create_schedule(req: ScheduleCreateRequest):
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rule = _schedule_store.add(req.name, req.time, req.action,
                                   req.profile_ids, req.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "rule": rule}


@app.put("/api/schedules/{rule_id}")
async def update_schedule(rule_id: str, req: ScheduleUpdateRequest):
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rule = _schedule_store.update(rule_id, **req.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if rule is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy lịch")
    return {"success": True, "rule": rule}


@app.delete("/api/schedules/{rule_id}")
async def delete_schedule(rule_id: str):
    if _schedule_store is None:
        raise HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    if not _schedule_store.delete(rule_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy lịch")
    return {"success": True}


@app.get("/api/system/current-ip")
async def get_current_network_ip():
    """Return the host's public IP/ISP using HTTPS-only GeoIP services."""
    from antidetect.proxy import get_public_ip_info

    try:
        data = await asyncio.to_thread(get_public_ip_info, None, 6)
        if data.get("success"):
            return data
        return {"success": False, "error": data.get("error", "GeoIP lookup failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/system/shutdown")
async def shutdown_system():
    server = getattr(app.state, 'server', None)
    if server is None:
        raise HTTPException(status_code=409, detail='Hãy dừng máy chủ qua terminal đã khởi chạy nó.')
    result = await asyncio.to_thread(launcher.stop_all)
    if not result['success']:
        raise HTTPException(status_code=409, detail='Còn profile chưa đóng. Máy chủ được giữ lại để bạn thử lại.')
    asyncio.get_running_loop().call_later(0.5, setattr, server, 'should_exit', True)
    return {"success": True, "message": "Hệ thống đang tắt. Bạn có thể đóng tab này an toàn."}


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    stopped = await asyncio.to_thread(launcher.stop, profile_id)
    if not stopped.get('success'):
        error = stopped.get('error', 'Chưa đóng được profile')
        # "Profile không tồn tại" là lỗi không tìm thấy (404), không phải xung đột.
        if 'không tồn tại' in error.lower():
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=409, detail=error)
    try:
        success = await asyncio.to_thread(profile_manager.delete_profile, profile_id)
    except ProfileDeletionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        # ID không hợp lệ (chống path traversal) phải là 400, không phải 500.
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not success:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return {"success": True, "message": "Đã xóa profile thành công"}


@app.post("/api/profiles/{profile_id}/duplicate", response_model=Profile)
async def duplicate_profile(profile_id: str, copy_data: bool = False):
    def _duplicate():
        if copy_data and launcher.is_running(profile_id):
            return "RUNNING"
        try:
            p = profile_manager.duplicate_profile(profile_id, copy_data=copy_data)
        except RuntimeError as e:
            return ("COPY_FAILED", str(e))
        if not p:
            return "NOT_FOUND"
        return ("OK", p)

    result = await asyncio.to_thread(_duplicate)
    if result == "RUNNING":
        raise HTTPException(status_code=409, detail='Hãy đóng profile trước khi sao chép dữ liệu phiên.')
    if result == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if isinstance(result, tuple) and result[0] == "COPY_FAILED":
        # Không báo thành công khi bản sao user_data nửa chừng/corrupt.
        raise HTTPException(status_code=500, detail=result[1])
    return result[1]


@app.get("/api/profiles/{profile_id}/cookies/export")
async def export_profile_cookies(profile_id: str, request: Request):
    """Đọc cookies của profile đang chạy mà không ghi bản sao vào đĩa/cache."""
    if request.headers.get('x-confirm-cookie-access') != 'yes':
        raise HTTPException(status_code=403, detail='Cần xác nhận trước khi đọc cookie phiên đăng nhập.')
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if not launcher.is_running(profile_id):
        raise HTTPException(status_code=409, detail="Hãy mở profile trước khi đọc cookies.")
    try:
        cookies = await asyncio.to_thread(launcher.export_cookies, profile_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return JSONResponse(
        content={"success": True, "profile_id": profile_id, "cookies": cookies, "count": len(cookies)},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.post("/api/profiles/{profile_id}/cookies/import")
async def import_profile_cookies(profile_id: str, req: ImportCookiesRequest):
    """Nạp cookies vào profile đang chạy, không lưu bản sao JSON trên đĩa."""
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    if not isinstance(req.cookies, list):
        raise HTTPException(status_code=400, detail="Định dạng cookies không hợp lệ, yêu cầu một mảng JSON các cookie.")
    if not launcher.is_running(profile_id):
        raise HTTPException(status_code=409, detail="Hãy mở profile trước khi nạp cookies.")
    success = await asyncio.to_thread(launcher.import_cookies, profile_id, req.cookies)
    if not success:
        raise HTTPException(status_code=503, detail="Không thể nạp cookies vào profile đang chạy.")
    return {
        "success": True,
        "profile_id": profile_id,
        "count": len(req.cookies),
        "message": f"Đã nạp {len(req.cookies)} cookies thành công!"
    }


def _sanitize_facebook_pages(raw: Any) -> List[Dict[str, Any]]:
    """Chỉ giữ fanpage có URL http(s) tuyệt đối.

    URL này được render thẳng thành href trên dashboard, nên loại bỏ
    javascript:/data:/file: từ cache hoặc file import (chống stored XSS cục bộ).
    Entry sai kiểu hoặc URL không hợp lệ bị bỏ qua thay vì làm hỏng cả danh sách.
    """
    from urllib.parse import urlparse

    pages: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return pages
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            continue
        item = dict(entry)
        item["url"] = url
        pages.append(item)
    return pages


def _enrich_facebook_pages(raw: Any) -> List[Dict[str, Any]]:
    from antidetect.launcher import classify_facebook_page

    pages = _sanitize_facebook_pages(raw)
    return [{**entry, **classify_facebook_page(entry)} for entry in pages]


@app.get("/api/profiles/{profile_id}/facebook/pages")
async def get_facebook_pages(
    profile_id: str, refresh: bool = False, deep: bool = False, force: bool = False
):
    """Liệt kê fanpage Facebook profile này sở hữu/quản lý.

    ``refresh=1`` (hoặc khi profile đang chạy mà chưa có cache) đọc trực tiếp
    từ phiên Chrome đang chạy. ``deep=1`` (kèm refresh) quét thêm TỪNG page:
    ngày tạo (trang about), bài đăng gần nhất (feed) → trạng thái
    đang đăng/dừng (ngưỡng 7 ngày) và "sẵn sàng" (tuổi ≥ 30 ngày).
    Mỗi page tốn ~4-8s khi deep — với 20 page mất ~2-3 phút.

    Kết quả live rỗng không ghi đè cache đang có (Facebook đổi DOM có thể
    trả 0 page dù tài khoản vẫn có fanpage); truyền ``force=1`` để chấp nhận
    danh sách rỗng và xóa cache.
    """
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    running = await asyncio.to_thread(launcher.is_running, profile_id)

    if refresh or (running and not p.facebook_pages):
        if not running:
            raise HTTPException(
                status_code=409,
                detail="Hãy mở profile trước khi làm mới danh sách fanpage.",
            )
        try:
            data = await asyncio.to_thread(
                launcher.fetch_facebook_pages,
                profile_id,
                40.0,
                bool(deep),
            )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        if not data.get("logged_in"):
            return {
                "success": False,
                "logged_in": False,
                "pages": [],
                "source": "live",
                "detail": "Profile chưa đăng nhập Facebook. Hãy mở profile, đăng nhập rồi thử lại.",
            }
        pages = _enrich_facebook_pages(data.get("pages") or [])
        cached = _enrich_facebook_pages(p.facebook_pages)
        if not pages and cached and not force:
            # Giữ cache cũ: 0 page từ một tab vừa mở thường là dấu hiệu
            # layout Facebook đổi/trang chưa render xong, không phải mất hết page.
            return {
                "success": True,
                "logged_in": True,
                "pages": cached,
                "source": "cache",
                "stale": True,
                "synced_at": p.facebook_pages_synced_at,
                "detail": (
                    "Facebook trả về 0 fanpage nên đã giữ dữ liệu cũ. "
                    "Nếu tài khoản thực sự không còn fanpage, gọi lại với force=1."
                ),
            }
        p.facebook_pages = pages
        p.facebook_pages_synced_at = int(time.time())
        updated = profile_manager.update_profile(p)
        if updated is None:
            raise HTTPException(status_code=404, detail="Profile không tồn tại")
        return {
            "success": True,
            "logged_in": True,
            "pages": pages,
            "source": "live",
            "stale": False,
            "synced_at": p.facebook_pages_synced_at,
        }

    # Cache: tính lại trạng thái runtime (không lưu) để phản ánh quy tắc mới.
    enriched = _enrich_facebook_pages(p.facebook_pages)
    return {
        "success": True,
        "logged_in": True,
        "pages": enriched,
        "source": "cache",
        "stale": False,
        "synced_at": p.facebook_pages_synced_at,
        "detail": None if enriched else "Chưa có dữ liệu fanpage. Mở profile và bấm Làm mới.",
    }


@app.post("/api/profiles/import")
async def import_profiles(req: ImportProfilesRequest):
    """Import profiles từ export JSON. Id mới tự sinh; password proxy phải nhập lại."""
    entries = req.profiles or []
    if not entries:
        raise HTTPException(status_code=400, detail="Không có profile nào trong payload (profiles: []).")
    if len(entries) > 200:
        raise HTTPException(status_code=400, detail="Tối đa 200 profile mỗi lần import.")

    imported, failed = [], []
    for i, entry in enumerate(entries):
        name = str(entry.get("name") or f"Imported {i + 1}") if isinstance(entry, dict) else f"#{i + 1}"
        try:
            if not isinstance(entry, dict):
                raise ValueError("entry không phải object")
            entry = dict(entry)
            for k in ("id", "status", "port", "pid", "revision"):
                entry.pop(k, None)
            proxy_data = entry.get("proxy") or {}
            if isinstance(proxy_data, dict):
                proxy_data = dict(proxy_data)
                proxy_data["password"] = None  # không bao giờ import password
            fp = FingerprintConfig.model_validate(entry.get("fingerprint") or {})
            proxy = ProxyConfig.model_validate(proxy_data)
            new_p = profile_manager.create_profile(
                name=name,
                target_os=fp.os,
                proxy=proxy,
                fingerprint=fp,
                notes=str(entry.get("notes") or ""),
                tags=list(entry.get("tags") or []),
            )
            new_p.browser_path = entry.get("browser_path") or None
            new_p.facebook_pages = _sanitize_facebook_pages(entry.get("facebook_pages"))
            new_p.facebook_pages_synced_at = int(entry.get("facebook_pages_synced_at") or 0)
            profile_manager.update_profile(new_p)
            imported.append({"id": new_p.id, "name": new_p.name})
        except Exception as exc:  # noqa: BLE001 - per-item import
            failed.append({"index": i, "name": name, "reason": f"{type(exc).__name__}: {exc}"})
    return {
        "success": len(failed) == 0,
        "imported_count": len(imported),
        "failed_count": len(failed),
        "imported": imported,
        "failed": failed,
    }


class BulkCreateRequest(BaseModel):
    count: int = Field(ge=1, le=50)
    os: str = "windows"
    city: str = "hanoi"
    stealth_mode: str = "unique"  # unique | authentic
    name_prefix: str = "Profile"
    cpu: Optional[int] = None
    proxies: List[str] = []


def _host_gpu_strings() -> tuple:
    """Vendor/renderer GPU thật của host (cho authentic bulk-create).

    Dự phòng theo bảng GPU khai báo — verify_live sẽ bắt nếu authentic lệch.
    """
    try:
        from antidetect.fingerprint import GPU_PROFILES
        for gp in GPU_PROFILES:
            r = gp if isinstance(gp, str) else (gp.get("renderer") or "")
            if "Intel" in r:
                return "Google Inc. (Intel)", r
        first = GPU_PROFILES[0]
        r = first if isinstance(first, str) else (first.get("renderer") or "")
        return "Google Inc. (Intel)", r
    except Exception:
        return "Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11)"


@app.post("/api/profiles/bulk-create")
async def bulk_create_profiles(req: BulkCreateRequest):
    """Tạo hàng loạt profile cùng cấu hình, fingerprint RIÊNG từng profile.

    Proxy: mỗi dòng 'protocol://user:pass@host:port' gán cho 1 profile theo
    thứ tự; thiếu proxy cho profile nào thì profile đó direct + warning
    (KHÔNG tuần hoàn cùng proxy cho nhiều profile — chống liên kết).
    """
    from antidetect.fingerprint import get_location_coordinates, host_os
    from antidetect.proxy import parse_proxy_line

    req.os = (req.os or "windows").lower()
    if req.os not in ("windows", "macos", "linux"):
        req.os = "windows"
    req.stealth_mode = (req.stealth_mode or "unique").lower()
    if req.stealth_mode not in ("unique", "authentic"):
        req.stealth_mode = "unique"
    prefix = (req.name_prefix or "Profile").strip() or "Profile"

    created, warnings = [], []
    for i in range(req.count):
        name = f"{prefix} {i + 1:02d}"
        try:
            lat, lon, tz = get_location_coordinates(req.city, randomize=True)
            fp = generate_fingerprint(
                target_os=req.os, timezone=tz, locale="vi-VN",
                latitude=lat, longitude=lon, city=req.city,
                browser_brand=detect_browser_brand(getattr(launcher, "browser_path", None)),
            )
            if req.cpu:
                fp.hardware_concurrency = max(1, min(128, int(req.cpu)))
            if req.stealth_mode == "authentic":
                fp.stealth_disable = ["canvas", "webgl", "audio", "media", "worker"]
                if req.os == host_os():
                    vendor, renderer = _host_gpu_strings()
                    fp.webgl_vendor, fp.webgl_renderer = vendor, renderer
            proxy = None
            if i < len(req.proxies):
                try:
                    proxy = parse_proxy_line(req.proxies[i])
                except ValueError as exc:
                    warnings.append(f"{name}: proxy dòng {i + 1} lỗi ({exc}) → tạo direct")
            else:
                warnings.append(f"{name}: không có proxy (direct)")
            p = profile_manager.create_profile(
                name=name, target_os=req.os, proxy=proxy, fingerprint=fp,
            )
            created.append({"id": p.id, "name": p.name})
        except Exception as exc:  # noqa: BLE001 - per-item create
            warnings.append(f"{name}: tạo lỗi {type(exc).__name__}: {exc}")
    return {
        "success": len(created) > 0,
        "created_count": len(created),
        "created": created,
        "warnings": warnings,
    }


@app.post("/api/profiles/{profile_id}/start")
async def start_profile(profile_id: str, url: Optional[str] = None):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    # M13: Validate URL scheme — chỉ cho phép http:// và https://
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")
    res = await asyncio.to_thread(launcher.launch, profile_id, open_url=url)
    if not res.get("success"):
        raise HTTPException(status_code=500, detail=res.get("error", "Lỗi khởi chạy"))
    return res


@app.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    res = await asyncio.to_thread(launcher.stop, profile_id)
    if not res.get("success"):
        raise HTTPException(status_code=409, detail=res.get("error", "Không thể dừng profile"))
    return res


@app.post("/api/proxy/check")
async def check_proxy_endpoint(req: CheckProxyRequest):
    if req.profile_id and not req.password:
        saved = profile_manager.get_profile(req.profile_id)
        if saved and saved.proxy and saved.proxy.password:
            same_endpoint = bool(
                saved.proxy.identity_key()
                == ProxyConfig(
                    enabled=True,
                    protocol=req.protocol,
                    host=req.host,
                    port=req.port,
                    username=req.username,
                ).identity_key()
            )
            if same_endpoint:
                req.password = saved.proxy.password
    cfg = ProxyConfig(
        enabled=True,
        protocol=req.protocol,
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password
    )
    return await asyncio.to_thread(check_proxy, cfg)


@app.get("/api/fingerprint/random")
async def random_fingerprint_endpoint(target_os: str = "windows"):
    return generate_fingerprint(target_os=target_os)
