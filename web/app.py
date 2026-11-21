import os
import sys
import time
import uuid
import asyncio
import datetime
import threading
import secrets
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Set  # noqa: F401 - re-exported for web.app compatibility
from fastapi import FastAPI, Request, HTTPException, Response  # noqa: F401 - re-exported for web.app compatibility
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: F401 - re-exported for web.app compatibility
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add root folder to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.models import Profile, FingerprintConfig, ProxyConfig, REDACTED_SECRET  # noqa: F401 - re-exported for web.app compatibility
from antidetect.storage import (
    ProfileManager,
    ProfileRevisionConflict,
    DATA_DIR,
    PROFILES_DATA_DIR,
)
from antidetect.profile_copy import (
    ProfileCopyError,  # noqa: F401 - re-exported for web.app compatibility
    new_operation_id,  # noqa: F401 - re-exported for web.app compatibility
    validate_idempotency_key,  # noqa: F401 - re-exported for web.app compatibility
)
from antidetect.profile_import import prepare_import_profile  # noqa: F401 - re-exported for web.app compatibility
from antidetect.fingerprint import (
    generate_fingerprint,  # noqa: F401 - re-exported for web.app compatibility
    locale_for_country,  # noqa: F401 - re-exported for web.app compatibility
    detect_browser_brand,  # noqa: F401 - re-exported for web.app compatibility
)
from antidetect.profile_audit import audit_profiles, build_fix_plan  # noqa: F401 - re-exported for web.app compatibility
from antidetect.launch_gate import (
    evaluate_profile as _evaluate_launch_gate,
    evaluate_profiles as _evaluate_launch_gates,
    get_mode as _launch_gate_mode,
    set_mode as _set_launch_gate_mode,  # noqa: F401 - re-exported for web.app compatibility
    settings_snapshot as _launch_gate_settings,  # noqa: F401 - re-exported for web.app compatibility
)
from antidetect.launcher import BrowserLauncher
from antidetect.cdp_health import CdpUnavailable
from antidetect.fanpage_scan import (
    DISCOVERY_ENDPOINT_TIMEOUT_S,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_UNKNOWN,
    ensure_scan_contract,
    scan_defaults,  # noqa: F401 - re-exported for web.app compatibility
    scan_detail,
)
from antidetect.facebook import ScanAborted  # noqa: F401 - re-exported for web.app compatibility
from antidetect.profile_locks import ProfileLockTimeout  # noqa: F401 - re-exported for web.app compatibility
from antidetect.proxy import check_proxy
from antidetect.scheduler import ScheduleStoreCorruption  # noqa: F401 - re-exported for web.app compatibility
from antidetect.bulk_jobs import (
    BulkJobHeartbeat,
    BulkJobStore,
    BulkJobStoreCorruption,
)
from antidetect.applog import get_logger, setup_logging
from antidetect import metrics
from antidetect.fanpages import FanpageRevisionConflictError, FanpageService  # noqa: F401 - re-exported for web.app compatibility

from .schemas import (
    AuditFixRequest,  # noqa: F401 - re-exported for web.app compatibility
    BulkCreateRequest,  # noqa: F401 - re-exported for web.app compatibility
    CdpHealth,
    CheckProxyRequest,  # noqa: F401 - re-exported for web.app compatibility
    CreateProfileRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageAlertUpdateRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageBulkUpdateRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageMergeRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageOpenRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpagePruneRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageSyncJobCreateRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageSyncJobRetryRequest,  # noqa: F401 - re-exported for web.app compatibility
    FanpageUpdateRequest,  # noqa: F401 - re-exported for web.app compatibility
    ImportCookiesRequest,  # noqa: F401 - re-exported for web.app compatibility
    ImportProfilesRequest,  # noqa: F401 - re-exported for web.app compatibility
    LaunchGateSettingsRequest,  # noqa: F401 - re-exported for web.app compatibility
    ProfileRuntime,
    ScheduleCreateRequest,  # noqa: F401 - re-exported for web.app compatibility
    ScheduleUpdateRequest,  # noqa: F401 - re-exported for web.app compatibility
    StartAllBody,  # noqa: F401 - re-exported for web.app compatibility
    StopAllBody,  # noqa: F401 - re-exported for web.app compatibility
    SwitchLocationRequest,  # noqa: F401 - re-exported for web.app compatibility
    UpdateProfileRequest,  # noqa: F401 - re-exported for web.app compatibility
)
from .services import (
    apply_geo_to_profile as _apply_geo_to_profile,
    auto_max_concurrent as _auto_max_concurrent,  # noqa: F401 - re-exported for web.app compatibility
    enrich_facebook_pages as _enrich_facebook_pages,
    fanpage_stale_after_hours as _fanpage_stale_after_hours,  # noqa: F401 - re-exported for web.app compatibility
    host_gpu_strings as _host_gpu_strings,  # noqa: F401 - re-exported for web.app compatibility
    profile_display_timezone as _profile_display_timezone,
    ram_by_profile_dir as _ram_by_profile_dir,
    sanitize_facebook_pages as _sanitize_facebook_pages,  # noqa: F401 - re-exported for web.app compatibility
    validate_iana_timezone,  # noqa: F401 - re-exported for web.app compatibility
    worker_pool as _worker_pool,
)

log = get_logger("web")


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
            item.cdp_health = CdpHealth(
                **launcher.cdp_health_snapshot(p.id, running=True)
            )
        runtime_profiles.append(item)
    return runtime_profiles


_CDP_CONFLICT_CODES = {
    "CDP_UNHEALTHY",
    "CDP_STARTING",
    "RECOVERY_REQUIRED",
    "DETACHED_NO_CDP",
    "PROFILE_BUSY",
}


def _cdp_conflict_response(exc: CdpUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "code": exc.code,
            "detail": str(exc),
            "cdp_health": exc.health,
        },
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(os.path.join(DATA_DIR, "app.log"), console=False)
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
        log.warning("Không ghi được runtime key: %s", e)
    # Đợt C: lịch tự động — runner daemon dậy mỗi 30s.
    global _scheduler, _schedule_store
    try:
        from antidetect.scheduler import ScheduleStore, Scheduler

        _schedule_store = ScheduleStore(os.path.join(DATA_DIR, "schedules.json"))
        _scheduler = Scheduler(
            _schedule_store,
            callbacks={
                "start_all": _scheduler_start_all,
                "stop_all": launcher.stop_all,
                "sync_location_proxy": _scheduler_sync_all_locations,
                "sync_fanpages": _scheduler_sync_fanpages,
                "sync_fanpages_deep": _scheduler_sync_fanpages_deep,
            },
            history_path=os.path.join(DATA_DIR, "schedule_runs.json"),
            profile_ids_resolver=lambda: [p.id for p in profile_manager.list_profiles()],
        )
        _scheduler.start()
    except Exception as e:
        log.warning("Không khởi động được scheduler: %s", e)
    yield
    launcher.begin_shutdown()
    with _start_all_lock:
        if _start_all_cancel_event is not None:
            _start_all_cancel_event.set()
    try:
        if _scheduler is not None:
            _scheduler.stop()
            await asyncio.to_thread(_scheduler.join, 30.0)
    except Exception:
        pass
    with _fanpage_service_lock:
        services = list(_fanpage_service_cache.values())
    for service in services:
        try:
            await asyncio.to_thread(service.jobs.shutdown, 30.0)
        except Exception as exc:
            log.warning("Fanpage cleanup failed: %s", type(exc).__name__)
    # Cleanup is limited to generations created by this server instance.
    try:
        await asyncio.to_thread(launcher.stop_owned)
    finally:
        try:
            from antidetect.runtime_auth import delete_runtime

            delete_runtime(expected_instance_id=app.state.instance_id)
        except Exception:
            pass


_scheduler = None
_schedule_store = None

app = FastAPI(
    title="Antidetect Python API",
    description="REST API & Dashboard cho Antidetect Browser",
    version="1.1.0",
    lifespan=lifespan,
)
app.state.access_key = secrets.token_urlsafe(32)
app.state.session_key = secrets.token_urlsafe(32)
app.state.instance_id = secrets.token_hex(16)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    # Validation's input/context can embed the complete proxy credential object.
    return JSONResponse(status_code=422, content={
        "detail": [{"loc": list(error["loc"]), "type": error["type"],
                    "msg": "Dữ liệu không hợp lệ."}
                   for error in exc.errors()]
    })

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
    # Không còn inline <script> hay inline handler (onclick=): sự kiện đi qua
    # /static/js/actions.js, nên script-src-attr là 'none'. connect-src/img-src
    # chỉ 'self' để script lạ (nếu có) không gửi dữ liệu ra host ngoài.
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
            "script-src 'self'; "
            "script-src-attr 'none'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
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
_bulk_job_store = BulkJobStore(os.path.join(DATA_DIR, "bulk_jobs.json"))


def _record_bulk_progress(
    job_id: str, profile_id: str, res: Dict[str, Any]
) -> None:
    """Cập nhật job cục bộ + file trạng thái dùng chung; lỗi ghi chỉ log."""
    with _start_all_lock:
        job = _active_start_all_job
        if not job or job.get("job_id") != job_id:
            return
        health_state = (res.get("cdp_health") or {}).get("state")
        cdp_failed = res.get("success") and (
            res.get("cdp_ready") is False
            or (health_state and health_state != "healthy")
        )
        if res.get("success") and not cdp_failed:
            job["completed_count"] += 1
        else:
            job["failed_count"] += 1
        job["results"].append({"profile_id": profile_id, "result": res})
        completed = job["completed_count"]
        failed = job["failed_count"]
        results = list(job["results"])
    try:
        _bulk_job_store.update(
            job_id,
            app.state.instance_id,
            completed_count=completed,
            failed_count=failed,
            results=results,
        )
    except Exception as exc:  # noqa: BLE001 - progress chỉ là quan sát
        log.warning("Không ghi được tiến độ bulk job: %s", exc)


def _finish_bulk_job(
    job_id: str, status: str, error: Optional[str] = None
) -> None:
    with _start_all_lock:
        job = _active_start_all_job
        snapshot = None
        if job and job.get("job_id") == job_id:
            job["status"] = status
            job["end_time"] = time.time()
            if error:
                job["error"] = error
            snapshot = dict(job)
    if snapshot is None:
        return
    try:
        _bulk_job_store.finish(
            job_id,
            app.state.instance_id,
            status,
            error=error,
            results=snapshot.get("results"),
        )
    except Exception as exc:  # noqa: BLE001 - job đã chạy xong
        log.warning("Không ghi được trạng thái bulk job: %s", exc)


# --- Web Page Route ---












# --- REST API Endpoints ---














def _resolve_browser_paths(profiles: List[Profile]) -> Dict[str, Optional[str]]:
    """Resolve the exact browser binary per profile without launching it."""
    return {p.id: launcher._resolve_browser_path(p) for p in profiles}




















def _scheduler_start_all(profile_ids=None):
    """Callback scheduler: Scheduler.run gọi cb(ids) positionally.

    Không truyền thẳng launcher.launch_all vì tham số đầu của nó là ``delay``
    (list ID sẽ bị hiểu thành delay) và sẽ raise TypeError.

    Job theo lịch dùng chung slot với Start-All thủ công: nếu slot đang bận
    (cùng process hoặc instance khác) thì bỏ lượt, để không mở trùng browser
    và không giữ claim của lịch suốt thời gian chờ.
    """
    global _active_start_all_job, _start_all_cancel_event
    if not _bulk_launch_lock.acquire(blocking=False):
        metrics.inc("scheduler_start_all_skipped")
        log.warning("Bỏ qua Start-All theo lịch: một bulk job khác đang chạy.")
        return {"success": False, "skipped": True, "reason": "bulk_busy"}
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    heartbeat = None
    try:
        profiles = _profiles_with_status()
        if profile_ids is not None:
            wanted = set(profile_ids)
            profiles = [p for p in profiles if p.id in wanted]
        to_start = [p for p in profiles if p.status != "running"]
        gate_mode = _launch_gate_mode()
        blocked = []
        allowed_to_start = to_start
        launch_ids = profile_ids
        if gate_mode != "off":
            browser_paths = _resolve_browser_paths(profiles)
            gate_by_id = _evaluate_launch_gates(
                profiles, browser_paths, gate_mode
            )
            blocked = [
                gate_by_id[p.id]
                for p in to_start
                if p.id in gate_by_id and not gate_by_id[p.id].allowed
            ]
            blocked_ids = {item.profile_id for item in blocked}
            allowed_to_start = [p for p in to_start if p.id not in blocked_ids]
            if to_start and not allowed_to_start:
                metrics.inc("scheduler_start_all_gate_blocked")
                log.warning(
                    "Bỏ qua Start-All theo lịch: cổng fingerprint chặn %d profile.",
                    len(blocked),
                )
                return {
                    "success": False,
                    "skipped": True,
                    "reason": "launch_gate_blocked",
                    "blocked": [item.as_dict() for item in blocked],
                }
            if blocked_ids:
                base_ids = (
                    list(profile_ids)
                    if profile_ids is not None
                    else [p.id for p in profiles]
                )
                launch_ids = [pid for pid in base_ids if pid not in blocked_ids]
        with _start_all_lock:
            if _active_start_all_job is not None and _active_start_all_job.get(
                "status"
            ) in ("running", "cancelling"):
                metrics.inc("scheduler_start_all_skipped")
                log.warning(
                    "Bỏ qua Start-All theo lịch: job %s đang chạy.",
                    _active_start_all_job.get("job_id"),
                )
                return {"success": False, "skipped": True, "reason": "bulk_busy"}
        try:
            claimed, existing = _bulk_job_store.try_claim(
                job_id=job_id,
                owner_instance=app.state.instance_id,
                kind="scheduled_start_all",
                total=len(profiles),
                starting_count=len(allowed_to_start),
            )
        except (BulkJobStoreCorruption, TimeoutError) as exc:
            raise RuntimeError(f"bulk job state: {exc}") from exc
        if not claimed:
            metrics.inc("scheduler_start_all_skipped")
            log.warning(
                "Bỏ qua Start-All theo lịch: instance %s đang chạy job %s.",
                existing.get("owner_instance"), existing.get("job_id"),
            )
            return {
                "success": False,
                "skipped": True,
                "reason": "remote_bulk_job",
                "job_id": existing.get("job_id"),
            }
        with _start_all_lock:
            _start_all_cancel_event = cancel_event
            _active_start_all_job = {
                "job_id": job_id,
                "kind": "scheduled_start_all",
                "status": "running",
                "owner_instance": app.state.instance_id,
                "total": len(profiles),
                "starting_count": len(allowed_to_start),
                "blocked_count": len(blocked),
                "gate_mode": gate_mode,
                "completed_count": 0,
                "failed_count": 0,
                "max_concurrent": 0,
                "results": [],
                "start_time": time.time(),
                "heartbeat_at": time.time(),
                "cancel_requested": False,
            }
        heartbeat = BulkJobHeartbeat(
            _bulk_job_store, job_id, app.state.instance_id, cancel_event
        )
        status = "completed"
        error = None
        try:
            heartbeat.start()
            results = launcher.launch_all(
                delay=0.8,
                cancel_event=cancel_event,
                progress_callback=lambda p_id, res: _record_bulk_progress(
                    job_id, p_id, res
                ),
                profile_ids=launch_ids,
            )
            def _launch_failed(item):
                result = item.get("result") or {}
                if not result.get("success"):
                    return True
                if result.get("cdp_ready") is False:
                    return True
                state = (result.get("cdp_health") or {}).get("state")
                return bool(state and state != "healthy")

            failures = [
                {
                    "profile_id": item.get("profile_id"),
                    "code": (item.get("result") or {}).get("code"),
                }
                for item in results
                if _launch_failed(item)
            ]
            if cancel_event.is_set():
                status = "cancelled"
            elif failures:
                status = "partial"
            return {
                "success": not failures,
                "job_id": job_id,
                "status": status,
                "failed_count": len(failures),
                "failures": failures,
                "results": results,
                "blocked": [item.as_dict() for item in blocked],
                "gate_mode": gate_mode,
            }
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
            raise
        finally:
            heartbeat.stop()
            _finish_bulk_job(job_id, status, error)
    finally:
        _bulk_launch_lock.release()


def _commit_geo_result(profile, geo):
    """Apply only observed geo fields after rechecking the lookup snapshot."""
    candidate = profile.model_copy(deep=True)
    changes = _apply_geo_to_profile(candidate, geo, lambda _: None)
    patch = {
        field: getattr(candidate.fingerprint, field)
        for field in ("latitude", "longitude", "timezone", "locale", "languages")
        if getattr(candidate.fingerprint, field) != getattr(profile.fingerprint, field)
    }
    updated = profile_manager.patch_fingerprint(
        profile.id, patch, expected_revision=profile.revision,
        expected_proxy=(profile.proxy.enabled, profile.proxy.identity_key()),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Profile không tồn tại")
    return changes


def _scheduler_sync_all_locations(profile_ids=None):
    """Callback scheduler (sync, chạy trong thread scheduler)."""
    profiles = profile_manager.list_profiles()
    if profile_ids is not None:
        wanted = set(profile_ids)
        profiles = [p for p in profiles if p.id in wanted]
    done, skipped = 0, 0
    results = {}
    for p in profiles:
        if not (p.proxy and p.proxy.enabled and p.proxy.host):
            skipped += 1
            results[p.id] = {"status": "skipped", "code": "PROXY_DISABLED"}
            continue
        try:
            geo = check_proxy(p.proxy, 12)
            if geo.get("success"):
                _commit_geo_result(p, geo)
                done += 1
                results[p.id] = {"status": "succeeded", "success": True}
            else:
                results[p.id] = {"status": "failed", "success": False, "code": "GEO_LOOKUP_FAILED", "retryable": True}
        except ProfileRevisionConflict as exc:
            results[p.id] = {"status": "failed", "success": False, "code": exc.code, "retryable": False}
        except Exception:
            results[p.id] = {"status": "failed", "success": False, "code": "GEO_SYNC_FAILED", "retryable": False}
    failed = [pid for pid, result in results.items() if result["status"] == "failed"]
    return {"success": not failed, "synced": done, "skipped": skipped,
            "total": len(profiles), "failed_profile_ids": failed, "results": results}


def _scheduler_sync_fanpages(profile_ids=None):
    """Callback scheduler: đưa job đồng bộ danh sách fanpage (discovery) vào hàng đợi."""
    from antidetect.scheduler import current_occurrence_key

    svc = get_fanpage_service()
    job, deduplicated = svc.enqueue_sync_job(
        mode="discovery",
        profile_ids=profile_ids,
        auto_start=True,
        triggered_by="scheduler",
        run_async=True,
        idempotency_key=current_occurrence_key(),
    )
    return {"job_id": job.get("id"), "deduplicated": deduplicated}


def _scheduler_sync_fanpages_deep(profile_ids=None):
    """Callback scheduler: đưa job đồng bộ chi tiết fanpage (basic) vào hàng đợi."""
    from antidetect.scheduler import current_occurrence_key

    svc = get_fanpage_service()
    job, deduplicated = svc.enqueue_sync_job(
        mode="basic",
        profile_ids=profile_ids,
        auto_start=True,
        triggered_by="scheduler",
        run_async=True,
        idempotency_key=current_occurrence_key(),
    )
    return {"job_id": job.get("id"), "deduplicated": deduplicated}








_token_rotate_lock = threading.Lock()
_token_rotate_times: list = []


def _mutate_api_token(request: Request, *, revoke: bool):
    """Recheck authorization while holding the same lock as the mutation.

    Middleware authorization may precede another process's revoke. The second
    check prevents that stale request from recreating a token after revocation.
    """
    from antidetect.runtime_auth import (
        api_token_lock, delete_api_token_hash, write_api_token_hash,
    )

    try:
        with api_token_lock():
            if not authorized(request):
                raise HTTPException(status_code=401, detail="Chưa xác thực")
            if revoke:
                delete_api_token_hash()
                return {"success": True, "message": "Đã thu hồi API token."}
            now = time.time()
            with _token_rotate_lock:
                _token_rotate_times[:] = [t for t in _token_rotate_times if now - t < 60]
                if len(_token_rotate_times) >= 5:
                    raise HTTPException(
                        status_code=429, detail="Quá nhiều lần tạo token trong 1 phút."
                    )
                _token_rotate_times.append(now)
            import hashlib as _hashlib
            token = secrets.token_urlsafe(32)
            write_api_token_hash(_hashlib.sha256(token.encode("utf-8")).hexdigest())
            return {
                "success": True,
                "token": token,
                "note": "Token chỉ hiển thị MỘT lần này. Lưu ngay vào chỗ an toàn; làm mất chỉ có thể rotate token mới.",
            }
    except HTTPException:
        raise
    except (OSError, RuntimeError):
        # Do not expose the DPAPI path, OS error, or any capability in errors.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "TOKEN_STORAGE_UNAVAILABLE",
                "message": "Không cập nhật được API token. Hãy thử lại sau.",
            },
        ) from None






# --- Đợt C: Lịch tự động ---

SCHEDULES_CORRUPT_DETAIL = (
    "Dữ liệu lịch bị hỏng; hãy khôi phục file schedules.json hợp lệ."
)


def _schedule_history_with_jobs(limit=20):
    """Enrich accepted runs without starting a worker or modifying its database."""
    import sqlite3
    from pathlib import Path
    import antidetect.storage as storage_module

    events = _scheduler.run_history.recent(limit) if _scheduler else []
    events = [dict(event) for event in events]
    pending = [event for event in events if event.get("status") == "accepted" and event.get("job_id")]
    if not pending:
        return events
    data_dir = getattr(profile_manager, "data_dir", None) or storage_module.DATA_DIR
    db = Path(data_dir) / "fanpages.sqlite"
    if not db.is_file():
        for event in pending:
            event["job_status"] = "unavailable"
        return events
    try:
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
            for event in pending:
                row = conn.execute("SELECT status FROM sync_jobs WHERE job_id=?", (event["job_id"],)).fetchone()
                event["job_status"] = row[0] if row else "unavailable"
    except sqlite3.Error:
        for event in pending:
            event["job_status"] = "unavailable"
    return events






















def _scan_fields(scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    scan = scan or {}
    return {
        "scan_id": scan.get("scan_id"),
        "discovery_status": scan.get("discovery_status"),
        "completion_reason": scan.get("completion_reason"),
        "stability": scan.get("stability"),
        "observed_count": scan.get("observed_count"),
        "retained_count": scan.get("retained_count"),
        "total_pages_in_source": scan.get("total_pages_in_source"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "identity_status": scan.get("identity_status"),
        "collector_version": scan.get("collector_version"),
    }


def _fanpage_stale(
    scan: Optional[Dict[str, Any]],
    pages: List[Dict[str, Any]],
    synced_at: int,
    now: datetime.datetime,
    stale_after_hours: int,
) -> bool:
    if not pages:
        return False
    status = (scan or {}).get("discovery_status")
    if status in (STATUS_PARTIAL, STATUS_FAILED, STATUS_UNKNOWN, None):
        return True
    if synced_at and (now.timestamp() - synced_at) > stale_after_hours * 3600:
        return True
    return any(
        pg.get("activity_stale") or pg.get("activity_scan_complete") is False
        for pg in pages
    )


def _run_facebook_scan(
    profile_id: str, deep: bool, force: bool, stale_after_hours: int
):
    """Chạy trong thread: giữ scan lock suốt fetch + merge (QĐ-6).

    Scan lock chỉ chặn lượt quét khác, không chặn stop/delete; profile dừng
    giữa chừng làm collector ném ``ScanAborted`` và cache giữ nguyên.
    """
    with profile_manager.scan_lock(profile_id, timeout=0.0):
        profile = profile_manager.get_profile(profile_id)
        if profile is None:
            return None
        scan_meta = profile_manager.get_facebook_scan_meta(profile_id)
        data = launcher.fetch_facebook_pages(
            profile_id,
            DISCOVERY_ENDPOINT_TIMEOUT_S,
            deep,
        )
        scan = ensure_scan_contract(
            data, expected_account_id=scan_meta.get("account_id_observed")
        )
        applied = profile_manager.apply_facebook_scan(profile_id, scan)
        if applied is None:
            return None
        updated, scan = applied
        if (
            force
            and scan.get("discovery_status") == STATUS_COMPLETE
            and scan.get("observed_count") == 0
            and (
                scan.get("completion_reason") == "EMPTY_CONFIRMED"
                or (
                    scan.get("completion_reason") == "TOTAL_MATCH"
                    and scan.get("total_pages_in_source") == 0
                )
            )
        ):
            cleared = profile_manager.clear_facebook_pages(profile_id)
            if cleared is not None:
                updated = cleared
            scan["retained_count"] = 0

        if scan.get("completion_reason") == "LOGIN_LOST":
            return {
                "success": False,
                "logged_in": False,
                "login_status": False,
                "pages": [],
                "source": "live",
                "stale": False,
                **_scan_fields(scan),
                "detail": (
                    "Profile chưa đăng nhập Facebook. "
                    "Hãy mở profile, đăng nhập rồi thử lại."
                ),
            }

        now = datetime.datetime.now(datetime.timezone.utc)
        enriched = _enrich_facebook_pages(
            updated.facebook_pages or [],
            now=now,
            timezone=_profile_display_timezone(updated),
            stale_after_hours=stale_after_hours,
        )
        observed = int(scan.get("observed_count") or 0)
        login_status = scan.get("login_status")
        login_known = isinstance(login_status, bool)
        return {
            "success": True,
            "logged_in": login_status if login_known else None,
            "login_status": login_status if login_known else "unknown",
            "pages": enriched,
            "source": "live" if observed else "cache",
            "stale": _fanpage_stale(
                scan, enriched, updated.facebook_pages_synced_at,
                now, stale_after_hours,
            ),
            "synced_at": updated.facebook_pages_synced_at,
            **_scan_fields(scan),
            "detail": scan_detail(scan, len(enriched)),
        }




















# --- Fanpage Manager (Giai đoạn 1) ---

_fanpage_service_cache: Dict[str, FanpageService] = {}
_fanpage_service_lock = threading.Lock()


def _check_profile_launch_gate_for_job(profile: Profile) -> tuple[bool, Optional[str]]:
    gate_mode = _launch_gate_mode()
    if gate_mode == "off":
        return True, None
    profiles = profile_manager.list_profiles()
    browser_paths = _resolve_browser_paths(profiles)
    gate = _evaluate_launch_gate(
        profile, profiles, browser_paths, gate_mode, False
    )
    return bool(gate.allowed), gate.message


def get_fanpage_service() -> FanpageService:
    """Khởi tạo lười (lazy) FanpageService gắn với thư mục data của profile_manager hiện hành.

    Tuyệt đối không mở/ghi SQLite ở thời điểm import module để các bài test
    hoán đổi ``profile_manager`` hoặc ``ANTIDETECT_DATA_DIR`` luôn cô lập
    100% khỏi ``data/`` thật.
    """
    import antidetect.storage as _storage_mod

    data_dir = os.path.abspath(
        getattr(profile_manager, "data_dir", None)
        or getattr(_storage_mod, "DATA_DIR", None)
        or os.environ.get("ANTIDETECT_DATA_DIR")
        or DATA_DIR
    )
    with _fanpage_service_lock:
        svc = _fanpage_service_cache.get(data_dir)
        if (
            svc is None
            or svc.profile_manager is not profile_manager
            or svc.launcher is not launcher
        ):
            svc = FanpageService(
                profile_manager=profile_manager,
                launcher=launcher,
                db_path=os.path.join(data_dir, "fanpages.sqlite"),
                launch_gate_check=_check_profile_launch_gate_for_job,
                owner_instance=getattr(app.state, "instance_id", None),
            )
            _fanpage_service_cache[data_dir] = svc
        return svc




























# --- Fanpage Sync Jobs (Giai đoạn 2) ---












# --- Fanpage Alerts & Operational Health (Giai đoạn 3) ---


from web.routers import system, profile_runtime, profiles, facebook, schedules, fanpages  # noqa: E402
for _r in (system, profile_runtime, profiles, facebook, schedules, fanpages):
    app.include_router(_r.router)

from web.routers.system import health, system_metrics, unlock, login, create_unlock_code_endpoint, home_page, get_launch_gate_settings, update_launch_gate_settings, get_fingerprint_options, get_system_logs, rotate_api_token, revoke_api_token, get_current_network_ip, shutdown_system, check_proxy_endpoint, random_fingerprint_endpoint  # noqa: E402,F401 - re-exported for web.app compatibility
from web.routers.profile_runtime import start_all_profiles, get_start_all_status, cancel_start_all, stop_all_profiles, start_profile, stop_profile, stop_owned_profile  # noqa: E402,F401 - re-exported for web.app compatibility
from web.routers.profiles import list_profiles, create_profile, export_profiles, audit_profiles_endpoint, fix_profiles_endpoint, get_profile, update_profile_endpoint, reset_fingerprint_endpoint, set_profile_location, sync_location_from_proxy, switch_all_profiles_location, delete_profile, duplicate_profile, export_profile_cookies, import_profile_cookies, import_profiles, bulk_create_profiles  # noqa: E402,F401 - re-exported for web.app compatibility
from web.routers.facebook import get_facebook_pages, delete_facebook_pages  # noqa: E402,F401 - re-exported for web.app compatibility
from web.routers.schedules import list_schedules, create_schedule, update_schedule, delete_schedule  # noqa: E402,F401 - re-exported for web.app compatibility
from web.routers.fanpages import fanpages_page, get_fanpages_summary, export_fanpages_csv, bulk_update_fanpages, create_fanpages_backup_endpoint, list_fanpages_backups_endpoint, prune_fanpages_data_endpoint, list_fanpages_endpoint, get_fanpage_detail_endpoint, get_fanpage_metrics_endpoint, merge_fanpage_endpoint, update_fanpage_endpoint, open_fanpage_in_profile, create_fanpage_sync_job_endpoint, list_fanpage_sync_jobs_endpoint, get_fanpage_sync_job_endpoint, cancel_fanpage_sync_job_endpoint, retry_fanpage_sync_job_endpoint, list_fanpage_alerts_endpoint, acknowledge_all_fanpage_alerts_endpoint, update_fanpage_alert_endpoint  # noqa: E402,F401 - re-exported for web.app compatibility
