from fastapi import APIRouter
from web import app as core

router = APIRouter()

Optional = core.Optional
StartAllBody = core.StartAllBody
StopAllBody = core.StopAllBody


@router.post("/api/profiles/start-all")
async def start_all_profiles(
    url: Optional[str] = None,
    max_concurrent: Optional[int] = None,
    body: Optional[StartAllBody] = None,
):
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise core.HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")
    if max_concurrent is None:
        # Chọn theo RAM máy (16GB → 6 Chrome đồng thời) — an toàn cho quy mô
        # ~20 profile; client vẫn override được bằng query param.
        max_concurrent = core._auto_max_concurrent()
    max_concurrent = max(0, int(max_concurrent or 0))


    body_ids = body.profile_ids if body is not None else None
    if body_ids is not None and not body_ids:
        raise core.HTTPException(
            status_code=400,
            detail="Danh sách profile được chọn trống. Bỏ profile_ids để chạy toàn bộ.",
        )
    profiles = await core.asyncio.to_thread(core._profiles_with_status)
    selected_ids = set(body_ids) if body_ids is not None else None
    if selected_ids is not None:
        profiles = [p for p in profiles if p.id in selected_ids]
        if not profiles:
            raise core.HTTPException(status_code=404, detail="Không profile nào trong danh sách chọn tồn tại.")

    to_start = [p for p in profiles if p.status != "running"]
    gate_mode = core._launch_gate_mode()
    blocked = []
    allowed_to_start = to_start
    launch_ids: core.Optional[core.List[str]] = (
        list(selected_ids) if selected_ids is not None else None
    )
    if gate_mode != "off":
        browser_paths = await core.asyncio.to_thread(core._resolve_browser_paths, profiles)
        gate_by_id = await core.asyncio.to_thread(
            core._evaluate_launch_gates, profiles, browser_paths, gate_mode
        )
        blocked = [
            gate_by_id[p.id]
            for p in to_start
            if p.id in gate_by_id and not gate_by_id[p.id].allowed
        ]
        blocked_ids = {item.profile_id for item in blocked}
        allowed_to_start = [p for p in to_start if p.id not in blocked_ids]
        if to_start and not allowed_to_start:
            return core.JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "detail": (
                        f"Cổng fingerprint chặn toàn bộ {len(blocked)} profile "
                        "trong danh sách. Sửa trong Health Center hoặc đổi chế độ cổng."
                    ),
                    "blocked": [item.as_dict() for item in blocked],
                    "gate_mode": gate_mode,
                },
            )
        if blocked_ids:
            base_ids = (
                list(selected_ids)
                if selected_ids is not None
                else [p.id for p in profiles]
            )
            launch_ids = [pid for pid in base_ids if pid not in blocked_ids]

    # Lock order: _bulk_launch_lock -> _start_all_lock -> bulk job IPC lock.
    # The bulk thread holds the process lock for its whole lifetime, so a
    # failure to acquire it means another local bulk launch is alive. A second
    # server instance is rejected by the file-backed job store instead.
    if not core._bulk_launch_lock.acquire(blocking=False):
        raise core.HTTPException(
            status_code=409,
            detail="Một tác vụ Start-All khác đang chạy. Hãy đợi hoặc hủy tác vụ hiện tại."
        )
    try:
        with core._start_all_lock:
            if core._active_start_all_job is not None and core._active_start_all_job.get("status") in ("running", "cancelling"):
                raise core.HTTPException(
                    status_code=409,
                    detail=f"Đang có tác vụ Start-All ({core._active_start_all_job.get('job_id')}) đang xử lý. Hãy đợi hoặc hủy tác vụ hiện tại."
                )
            job_id = str(core.uuid.uuid4())
            cancel_event = core.threading.Event()
        try:
            claimed, existing = core._bulk_job_store.try_claim(
                job_id=job_id,
                owner_instance=core.app.state.instance_id,
                kind="start_all",
                total=len(profiles),
                starting_count=len(allowed_to_start),
                max_concurrent=max_concurrent,
            )
        except core.BulkJobStoreCorruption as exc:
            core.log.error("Bulk job state hỏng: %s", exc)
            raise core.HTTPException(
                status_code=503,
                detail="Trạng thái Start-All dùng chung bị hỏng. Kiểm tra data/bulk_jobs.json trước khi chạy lại.",
            ) from exc
        except TimeoutError as exc:
            raise core.HTTPException(
                status_code=503,
                detail="Không lấy được khóa trạng thái Start-All; thử lại sau.",
            ) from exc
        if not claimed:
            raise core.HTTPException(
                status_code=409,
                detail=(
                    f"Đang có tác vụ {existing.get('kind') or 'Start-All'} "
                    f"({existing.get('job_id')}) do instance "
                    f"{str(existing.get('owner_instance') or 'khác')[:8]} xử lý. "
                    "Hãy đợi hoặc hủy tác vụ hiện tại."
                ),
            )
        with core._start_all_lock:
            core._start_all_cancel_event = cancel_event
            core._active_start_all_job = {
                "job_id": job_id,
                "kind": "start_all",
                "status": "running",
                "owner_instance": core.app.state.instance_id,
                "total": len(profiles),
                "starting_count": len(allowed_to_start),
                "blocked_count": len(blocked),
                "gate_mode": gate_mode,
                "completed_count": 0,
                "failed_count": 0,
                "max_concurrent": max_concurrent,
                "results": [],
                "start_time": core.time.time(),
                "heartbeat_at": core.time.time(),
                "cancel_requested": False,
            }
    except BaseException:
        core._bulk_launch_lock.release()
        raise

    def run_bulk():
        heartbeat = core.BulkJobHeartbeat(
            core._bulk_job_store, job_id, core.app.state.instance_id, cancel_event
        )
        status = "completed"
        error = None
        try:
            heartbeat.start()
            core.launcher.launch_all(
                delay=0.8,
                open_url=url,
                max_concurrent=max_concurrent,
                cancel_event=cancel_event,
                progress_callback=on_progress,
                profile_ids=launch_ids,
            )
            if cancel_event.is_set():
                status = "cancelled"
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
        finally:
            heartbeat.stop()
            core._finish_bulk_job(job_id, status, error)
            core._bulk_launch_lock.release()

    def on_progress(p_id, res):
        core._record_bulk_progress(job_id, p_id, res)

    try:
        core.threading.Thread(target=run_bulk, daemon=True).start()
    except Exception as exc:
        core._finish_bulk_job(job_id, "failed", str(exc)[:500])
        core._bulk_launch_lock.release()
        raise core.HTTPException(status_code=500, detail="Không khởi động được luồng Start-All.") from exc

    limit_label = (
        "không giới hạn" if max_concurrent <= 0 else f"tối đa {max_concurrent}"
    )
    start_count = len(allowed_to_start) if gate_mode != "off" else len(to_start)
    message = f"Đang khởi chạy {start_count} profile ({limit_label} cùng lúc)..."
    if blocked:
        message += f" Bỏ qua {len(blocked)} profile bị cổng fingerprint chặn."
    return {
        "success": True,
        "message": message,
        "job_id": job_id,
        "total": len(profiles),
        "starting_count": start_count,
        "max_concurrent": max_concurrent,
        "blocked": [item.as_dict() for item in blocked],
        "gate_mode": gate_mode,
    }

@router.get("/api/profiles/start-all/status")
async def get_start_all_status():
    """P1-06/P2-12: tiến độ Start-All, kể cả job do instance khác sở hữu."""
    with core._start_all_lock:
        local_job = dict(core._active_start_all_job) if core._active_start_all_job else None
    try:
        remote_job = core._bulk_job_store.read()
    except core.BulkJobStoreCorruption as exc:
        core.log.error("Bulk job state hỏng: %s", exc)
        return {
            "active": False,
            "job": None,
            "error": "Trạng thái Start-All dùng chung bị hỏng; kiểm tra data/bulk_jobs.json.",
        }
    job = local_job
    if remote_job and (
        not local_job or remote_job.get("job_id") != local_job.get("job_id")
    ):
        job = remote_job
    if not job:
        return {"active": False, "job": None}
    job["remote"] = job.get("owner_instance") != core.app.state.instance_id
    if job is local_job:
        active = job.get("status") in ("running", "cancelling")
    else:
        active = core._bulk_job_store.is_active(job)
    if not active and job.get("status") in ("running", "cancelling"):
        job["stale"] = True
    return {"active": active, "job": job}

@router.post("/api/profiles/start-all/cancel")
async def cancel_start_all():
    """P1-06/P2-12: hủy job cục bộ hoặc gửi yêu cầu tới instance đang sở hữu."""
    with core._start_all_lock:
        local_job = core._active_start_all_job
        local_active = bool(
            local_job and local_job.get("status") in ("running", "cancelling")
        )
        if local_active:
            job_id = local_job.get("job_id")
            if core._start_all_cancel_event:
                core._start_all_cancel_event.set()
            local_job["status"] = "cancelling"
    if local_active:
        try:
            core._bulk_job_store.request_cancel(job_id)
        except Exception as exc:  # noqa: BLE001 - local cancel vẫn có hiệu lực
            core.log.warning("Không ghi được yêu cầu hủy bulk job: %s", exc)
        return {
            "success": True,
            "job_id": job_id,
            "remote": False,
            "message": "Đã gửi yêu cầu dừng hàng đợi Start-All.",
        }
    try:
        remote_job = core._bulk_job_store.request_cancel()
    except core.BulkJobStoreCorruption as exc:
        core.log.error("Bulk job state hỏng: %s", exc)
        raise core.HTTPException(
            status_code=503,
            detail="Trạng thái Start-All dùng chung bị hỏng; kiểm tra data/bulk_jobs.json.",
        ) from exc
    if remote_job:
        core.metrics.inc("bulk_job_remote_cancel")
        return {
            "success": True,
            "job_id": remote_job.get("job_id"),
            "remote": True,
            "message": "Đã gửi yêu cầu hủy tới instance đang chạy Start-All.",
        }
    return {"success": False, "message": "Không có tiến trình Start-All nào đang chạy."}

@router.post("/api/profiles/stop-all")
async def stop_all_profiles(body: Optional[StopAllBody] = None):
    ids = body.profile_ids if body else None
    if ids is not None and not ids:
        raise core.HTTPException(
            status_code=400,
            detail="Danh sách profile được chọn trống. Bỏ profile_ids để dừng toàn bộ.",
        )
    return await core.asyncio.to_thread(core.launcher.stop_all, ids)

@router.post("/api/profiles/{profile_id}/start")
async def start_profile(profile_id: str, url: Optional[str] = None, force: bool = False):
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    # M13: Validate URL scheme — chỉ cho phép http:// và https://
    if url:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise core.HTTPException(status_code=400, detail="URL không hợp lệ: chỉ chấp nhận http:// hoặc https://")

    gate = None
    gate_mode = core._launch_gate_mode()
    if gate_mode != "off":
        try:
            already_running = await core.asyncio.to_thread(core.launcher.is_running, profile_id)
        except Exception:
            already_running = False
        # Opening a tab in a browser that is already running cannot change the
        # fingerprint presented at spawn, so the gate only applies to a launch.
        if not already_running:
            profiles = await core.asyncio.to_thread(core.profile_manager.list_profiles)
            browser_paths = await core.asyncio.to_thread(core._resolve_browser_paths, profiles)
            gate = await core.asyncio.to_thread(
                core._evaluate_launch_gate, p, profiles, browser_paths, gate_mode, force
            )
            if not gate.allowed:
                return core.JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "detail": gate.message,
                        "gate": gate.as_dict(),
                    },
                )

    res = await core.asyncio.to_thread(core.launcher.launch, profile_id, open_url=url)
    if not res.get("success"):
        code = res.get("code")
        if code in core._CDP_CONFLICT_CODES or res.get("recovery_required"):
            return core.JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "code": code,
                    "detail": res.get("error", "Lỗi khởi chạy"),
                    "cdp_health": res.get("cdp_health"),
                },
            )
        raise core.HTTPException(status_code=500, detail=res.get("error", "Lỗi khởi chạy"))
    if gate and gate.status in ("warning", "overridden", "unverified"):
        res["gate"] = gate.as_dict()
    return res

@router.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    res = await core.asyncio.to_thread(core.launcher.stop, profile_id)
    if not res.get("success"):
        raise core.HTTPException(status_code=409, detail=res.get("error", "Không thể dừng profile"))
    return res

@router.post("/api/profiles/{profile_id}/stop/owned")
async def stop_owned_profile(profile_id: str, expected_launch_id: str):
    if not expected_launch_id.strip():
        raise core.HTTPException(status_code=400, detail="Thiếu định danh phiên profile.")
    res = await core.asyncio.to_thread(core.launcher.stop, profile_id, expected_launch_id=expected_launch_id)
    if not res.get("success"):
        raise core.HTTPException(status_code=409, detail=res.get("error", "Không thể dừng phiên profile."))
    return res
