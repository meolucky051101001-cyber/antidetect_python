from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from web import app as core
from web.schemas import CheckProxyRequest, LaunchGateSettingsRequest

router = APIRouter()


@router.get('/health')
async def health():
    return {'application': 'antidetect-python', 'instance_id': core.app.state.instance_id}

@router.get('/api/system/metrics')
async def system_metrics():
    """Counters/timings của tiến trình hiện tại + lịch sử lịch gần nhất."""
    data = core.metrics.snapshot()
    data["scheduler"] = {
        "last_tick": (
            core._scheduler.last_run_info.get("last_tick") if core._scheduler else None
        ),
        "recent_runs": await core.asyncio.to_thread(core._schedule_history_with_jobs, 10),
    }
    return data

@router.get('/unlock', response_class=HTMLResponse)
async def unlock(request: Request):
    return core.templates.TemplateResponse(request=request, name='unlock.html', context={})

@router.post('/auth/login')
async def login(request: Request):
    auth_header = request.headers.get('authorization', '')
    token = auth_header[7:].strip() if auth_header.startswith('Bearer ') else auth_header.strip()

    is_valid = False
    if token:
        if token.startswith("otc_"):
            # P0-06: Xác thực mã dùng một lần và tiêu hủy ngay
            is_valid = core.consume_unlock_code(token)
        elif core.secrets.compare_digest(
            token.encode('utf-8', 'replace'),
            core.app.state.access_key.encode('utf-8', 'replace'),
        ):
            # Cho phép access_key cố định qua API trực tiếp hoặc test suite.
            # So sánh trên bytes để input non-ASCII trả 401 thay vì TypeError 500.
            is_valid = True

    if not is_valid:
        raise core.HTTPException(status_code=401, detail='Khóa mở ứng dụng không hợp lệ hoặc đã hết hạn')

    response = core.JSONResponse({'success': True})
    response.set_cookie('antidetect_session', core.app.state.session_key, httponly=True, samesite='strict', path='/')
    return response

@router.post('/auth/create-unlock-code')
async def create_unlock_code_endpoint(request: Request):
    if not core.authorized(request):
        raise core.HTTPException(status_code=401, detail='Chưa xác thực')
    code = core.create_unlock_code(ttl_seconds=60)
    return {"unlock_code": code, "expires_in": 60}

@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    if not core.authorized(request):
        return core.templates.TemplateResponse(
            request=request, name="unlock.html", context={}
        )
    profiles = await core.asyncio.to_thread(core._profiles_with_status)
    return core.templates.TemplateResponse(
        request=request, name="index.html", context={"profiles": profiles}
    )

@router.get("/api/settings/launch-gate")
async def get_launch_gate_settings():
    """Static pre-launch gate configuration (no profile is launched)."""
    return core._launch_gate_settings()

@router.put("/api/settings/launch-gate")
async def update_launch_gate_settings(req: LaunchGateSettingsRequest):
    try:
        await core.asyncio.to_thread(core._set_launch_gate_mode, req.mode)
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc
    snapshot = core._launch_gate_settings()
    snapshot["message"] = (
        f"Đã đặt cổng fingerprint: {snapshot['labels'].get(snapshot['mode'], snapshot['mode'])}."
    )
    if snapshot.get("env_override"):
        snapshot["message"] += (
            " Biến môi trường ANTIDETECT_LAUNCH_GATE đang ghi đè cấu hình này."
        )
    return snapshot

@router.get("/api/fingerprint/options")
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

@router.get("/api/system/logs")
async def get_system_logs(lines: int = 200):
    """Đuôi app.log để xem nhật ký hệ thống ngay trên dashboard (read-only)."""
    lines = max(10, min(1000, int(lines or 200)))

    def read_tail():
        log_file = core.os.path.join(core.DATA_DIR, "app.log")
        if not core.os.path.exists(log_file):
            return []
        size = core.os.path.getsize(log_file)
        with open(log_file, "rb") as f:
            f.seek(max(0, size - 256 * 1024))  # đọc tối đa 256KB cuối
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-lines:]

    try:
        tail = await core.asyncio.to_thread(read_tail)
    except Exception as e:
        raise core.HTTPException(status_code=500, detail=f"Không đọc được app.log: {e}") from e
    return {"success": True, "lines": tail, "count": len(tail)}

@router.post("/api/auth/token/rotate")
async def rotate_api_token(request: Request):
    """Create a persistent API token, returning plaintext only after persistence."""
    if not core.authorized(request):
        raise core.HTTPException(status_code=401, detail="Chưa xác thực")
    return await core.asyncio.to_thread(core._mutate_api_token, request, revoke=False)

@router.delete("/api/auth/token")
async def revoke_api_token(request: Request):
    """Revoke the persistent API token; report storage failures accurately."""
    if not core.authorized(request):
        raise core.HTTPException(status_code=401, detail="Chưa xác thực")
    return await core.asyncio.to_thread(core._mutate_api_token, request, revoke=True)

@router.get("/api/system/current-ip")
async def get_current_network_ip():
    """Return the host's public IP/ISP using HTTPS-only GeoIP services."""
    from antidetect.proxy import get_public_ip_info

    try:
        data = await core.asyncio.to_thread(get_public_ip_info, None, 6)
        if data.get("success"):
            return data
        return {"success": False, "error": data.get("error", "GeoIP lookup failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.post("/api/system/shutdown")
async def shutdown_system():
    server = getattr(core.app.state, 'server', None)
    if server is None:
        raise core.HTTPException(status_code=409, detail='Hãy dừng máy chủ qua terminal đã khởi chạy nó.')
    core.launcher.begin_shutdown()
    if core._scheduler is not None:
        core._scheduler.stop()
    with core._start_all_lock:
        if core._start_all_cancel_event is not None:
            core._start_all_cancel_event.set()
    with core._fanpage_service_lock:
        services = list(core._fanpage_service_cache.values())
    for service in services:
        try:
            await core.asyncio.to_thread(service.jobs.shutdown, 30.0)
        except Exception as exc:
            core.log.warning("Fanpage cleanup failed: %s", type(exc).__name__)
    result = await core.asyncio.to_thread(core.launcher.stop_owned)
    if not result['success']:
        raise core.HTTPException(status_code=409, detail='Còn profile chưa đóng. Máy chủ được giữ lại để bạn thử lại.')
    core.asyncio.get_running_loop().call_later(0.5, setattr, server, 'should_exit', True)
    return {"success": True, "message": "Hệ thống đang tắt. Bạn có thể đóng tab này an toàn."}

@router.post("/api/proxy/check")
async def check_proxy_endpoint(req: CheckProxyRequest):
    if req.profile_id and not req.password:
        saved = core.profile_manager.get_profile(req.profile_id)
        if saved and saved.proxy and saved.proxy.password:
            same_endpoint = bool(
                saved.proxy.identity_key()
                == core.ProxyConfig(
                    enabled=True,
                    protocol=req.protocol,
                    host=req.host,
                    port=req.port,
                    username=req.username,
                ).identity_key()
            )
            if same_endpoint:
                req.password = saved.proxy.password
    cfg = core.ProxyConfig(
        enabled=True,
        protocol=req.protocol,
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password
    )
    return await core.asyncio.to_thread(core.check_proxy, cfg)

@router.get("/api/fingerprint/random")
async def random_fingerprint_endpoint(target_os: str = "windows"):
    return core.generate_fingerprint(target_os=target_os)
