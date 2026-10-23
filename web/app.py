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
from pydantic import BaseModel

# Add root folder to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.models import Profile, FingerprintConfig, ProxyConfig, REDACTED_SECRET
from antidetect.storage import ProfileManager, ProfileDeletionError
from antidetect.fingerprint import (
    generate_fingerprint,
    locale_for_country,
    detect_browser_brand,
)
from antidetect.launcher import BrowserLauncher
from antidetect.proxy import check_proxy


@asynccontextmanager
async def lifespan(_app: FastAPI):
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
    yield
    # Đóng toàn bộ trình duyệt đang chạy khi tắt server.
    launcher.stop_all()
    try:
        from antidetect.runtime_auth import delete_runtime

        delete_runtime()
    except Exception:
        pass


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
    bearer = request.headers.get('authorization', '')
    cookie = request.cookies.get('antidetect_session', '')
    return bool(
        (bearer and secrets.compare_digest(bearer, 'Bearer ' + app.state.access_key))
        or (cookie and secrets.compare_digest(cookie, app.state.session_key))
    )


# Dashboard và API dùng cùng origin; không bật CORS cho các cổng localhost khác.
@app.middleware("http")
async def local_origin_check_middleware(request: Request, call_next):
    if request.url.hostname not in ("localhost", "127.0.0.1"):
        return JSONResponse(status_code=403, content={"error": "Host không hợp lệ."})
    if request.url.path.startswith(("/api/", "/auth/")):
        from urllib.parse import urlparse

        if request.url.hostname not in ("localhost", "127.0.0.1"):
            return JSONResponse(status_code=403, content={"error": "API chỉ nhận kết nối loopback."})

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
        if request.url.path.startswith('/api/') and not authorized(request):
            return JSONResponse(status_code=401, content={"detail": "Phiên truy cập hết hạn. Hãy mở lại ứng dụng từ shortcut."})
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    # P2-04: Content Security Policy chống XSS và script injection
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https:; "
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
        elif secrets.compare_digest(token, app.state.access_key):
            # Cho phép access_key cố định qua API trực tiếp hoặc test suite
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
    profiles = profile_manager.list_profiles()
    # Cập nhật trạng thái thực tế
    for p in profiles:
        if launcher.is_running(p.id):
            p.status = "running"
            p.port = launcher.active_ports.get(p.id)
        else:
            p.status = "stopped"
            p.port = None
    return templates.TemplateResponse(
        request=request, name="index.html", context={"profiles": profiles}
    )


# --- REST API Endpoints ---
@app.get("/api/profiles", response_model=List[Profile])
async def list_profiles():
    profiles = profile_manager.list_profiles()
    for p in profiles:
        if launcher.is_running(p.id):
            p.status = "running"
            p.port = launcher.active_ports.get(p.id)
        else:
            p.status = "stopped"
            p.port = None
    return profiles


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


@app.post("/api/profiles/start-all")
async def start_all_profiles(url: Optional[str] = None, max_concurrent: int = 8):
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")
    max_concurrent = max(0, int(max_concurrent or 0))

    global _active_start_all_job, _start_all_cancel_event
    with _start_all_lock:
        if _active_start_all_job is not None and _active_start_all_job.get("status") == "running":
            raise HTTPException(
                status_code=409,
                detail=f"Đang có tác vụ Start-All ({_active_start_all_job.get('job_id')}) đang xử lý. Hãy đợi hoặc hủy tác vụ hiện tại."
            )
        profiles = profile_manager.list_profiles()
        to_start = [p for p in profiles if not launcher.is_running(p.id)]
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

    def run_bulk():
        if not _bulk_launch_lock.acquire(blocking=False):
            return  # Đã có luồng khác đang chạy, bỏ qua
        def on_progress(p_id, res):
            with _start_all_lock:
                if _active_start_all_job and _active_start_all_job.get("job_id") == job_id:
                    if res.get("success"):
                        _active_start_all_job["completed_count"] += 1
                    else:
                        _active_start_all_job["failed_count"] += 1
                    _active_start_all_job["results"].append({"profile_id": p_id, "result": res})
        try:
            launcher.launch_all(
                delay=0.8,
                open_url=url,
                max_concurrent=max_concurrent,
                cancel_event=cancel_event,
                progress_callback=on_progress,
            )
        finally:
            with _start_all_lock:
                if _active_start_all_job and _active_start_all_job.get("job_id") == job_id:
                    _active_start_all_job["status"] = "cancelled" if cancel_event.is_set() else "completed"
                    _active_start_all_job["end_time"] = time.time()
            _bulk_launch_lock.release()

    threading.Thread(target=run_bulk, daemon=True).start()

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
            "active": _active_start_all_job.get("status") == "running",
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


@app.post("/api/profiles/stop-all")
async def stop_all_profiles():
    return await asyncio.to_thread(launcher.stop_all)


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

    updated = profile_manager.update_profile(p)
    if not updated:
        raise HTTPException(status_code=500, detail="Không thể cập nhật profile")
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
    return updated


@app.get("/api/fingerprint/options")
async def get_fingerprint_options():
    from antidetect.fingerprint import (
        GPU_PROFILES,
        SCREEN_RESOLUTIONS,
        HARDWARE_PRESETS,
        POPULAR_LOCATIONS,
        get_installed_chrome_version,
    )
    ver_info = get_installed_chrome_version()
    browser_ver = ver_info[1] if ver_info else "155.0.8059.85"
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
    return updated


@app.post("/api/profiles/switch-location-all")
async def switch_all_profiles_location(req: SwitchLocationRequest):
    """P1-03: Chuyển đổi vị trí toàn bộ profiles, kiểm tra 0 profiles, validate ZoneInfo."""
    profiles = profile_manager.list_profiles()
    if not profiles:
        raise HTTPException(status_code=404, detail="Không có profile nào để chuyển đổi vị trí")
    from antidetect.fingerprint import get_location_coordinates

    custom_coords = req.latitude is not None and req.longitude is not None
    if custom_coords:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")

    if req.timezone and not validate_iana_timezone(req.timezone):
        raise HTTPException(status_code=400, detail=f"Múi giờ IANA '{req.timezone}' không hợp lệ")

    updated_profiles = []
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
            raise HTTPException(status_code=400, detail=f"Múi giờ IANA '{tz}' không hợp lệ")

        p.fingerprint.latitude = lat
        p.fingerprint.longitude = lon
        p.fingerprint.timezone = tz
        profile_manager.update_profile(p)
        updated_profiles.append({
            "id": p.id,
            "name": p.name,
            "city": req.city,
            "latitude": lat,
            "longitude": lon,
            "timezone": tz,
            "running": launcher.is_running(p.id)
        })

    return {
        "success": True,
        "message": f"Đã chuyển vị trí {len(updated_profiles)} profile sang {req.city.upper()} thành công!",
        "count": len(updated_profiles),
        "profiles": updated_profiles
    }


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
        raise HTTPException(status_code=409, detail=stopped.get('error', 'Chưa đóng được profile'))
    try:
        success = await asyncio.to_thread(profile_manager.delete_profile, profile_id)
    except ProfileDeletionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not success:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return {"success": True, "message": "Đã xóa profile thành công"}


@app.post("/api/profiles/{profile_id}/duplicate", response_model=Profile)
async def duplicate_profile(profile_id: str, copy_data: bool = False):
    if copy_data and launcher.is_running(profile_id):
        raise HTTPException(status_code=409, detail='Hãy đóng profile trước khi sao chép dữ liệu phiên.')
    p = profile_manager.duplicate_profile(profile_id, copy_data=copy_data)
    if not p:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return p


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
