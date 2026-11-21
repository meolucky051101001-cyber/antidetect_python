from fastapi import APIRouter
from web import app as core

router = APIRouter()

FanpageAlertUpdateRequest = core.FanpageAlertUpdateRequest
FanpageBulkUpdateRequest = core.FanpageBulkUpdateRequest
FanpageMergeRequest = core.FanpageMergeRequest
FanpageOpenRequest = core.FanpageOpenRequest
FanpagePruneRequest = core.FanpagePruneRequest
FanpageSyncJobCreateRequest = core.FanpageSyncJobCreateRequest
FanpageSyncJobRetryRequest = core.FanpageSyncJobRetryRequest
FanpageUpdateRequest = core.FanpageUpdateRequest
HTMLResponse = core.HTMLResponse
Optional = core.Optional
Request = core.Request


@router.get("/fanpages", response_class=HTMLResponse)
async def fanpages_page(request: Request):
    if not core.authorized(request):
        return core.templates.TemplateResponse(
            request=request, name="unlock.html", context={}
        )
    return core.templates.TemplateResponse(
        request=request, name="fanpages.html", context={}
    )

@router.get("/api/fanpages/summary")
async def get_fanpages_summary(
    include_archived: bool = False,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None,
):
    svc = core.get_fanpage_service()
    summary = await core.asyncio.to_thread(
        svc.get_summary,
        include_archived=include_archived,
        profile_id=profile_id,
        tag=tag,
    )
    return {"success": True, **summary}

@router.get("/api/fanpages/export")
async def export_fanpages_csv(
    q: Optional[str] = None,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None,
    activity_status: Optional[str] = None,
    connection_status: Optional[str] = None,
    data_quality: Optional[str] = None,
    age_ready: Optional[str] = None,
    archived: str = "active",
    sort_by: str = "page_name",
    sort_dir: str = "asc",
):
    svc = core.get_fanpage_service()
    csv_data = await core.asyncio.to_thread(
        svc.export_csv,
        q=q or "",
        profile_id=profile_id or "",
        tag=tag or "",
        activity_status=activity_status or "",
        connection_status=connection_status or "",
        data_quality=data_quality or "",
        age_ready=age_ready,
        archived=archived,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    csv_bytes = (
        csv_data.encode("utf-8") if isinstance(csv_data, str) else csv_data
    )
    return core.Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="fanpages_export.csv"',
            "Cache-Control": "no-store",
        },
    )

@router.post("/api/fanpages/bulk")
async def bulk_update_fanpages(req: FanpageBulkUpdateRequest):
    if not req.page_ids:
        raise core.HTTPException(
            status_code=400, detail="Danh sách fanpage được chọn trống."
        )
    svc = core.get_fanpage_service()
    result = await core.asyncio.to_thread(
        svc.bulk_update,
        page_ids=req.page_ids,
        add_tags=req.add_tags,
        remove_tags=req.remove_tags,
        is_archived=req.is_archived,
    )
    return {"success": True, **result}

@router.post("/api/fanpages/backup")
async def create_fanpages_backup_endpoint(max_backups: int = 10):
    svc = core.get_fanpage_service()
    info = await core.asyncio.to_thread(svc.create_backup, max_backups=max_backups)
    return {"success": True, "backup": info}

@router.get("/api/fanpages/backups")
async def list_fanpages_backups_endpoint():
    svc = core.get_fanpage_service()
    backups = await core.asyncio.to_thread(svc.list_backups)
    return {"success": True, "backups": backups, "items": backups}

@router.post("/api/fanpages/prune")
async def prune_fanpages_data_endpoint(req: Optional[FanpagePruneRequest] = None):
    req = req or core.FanpagePruneRequest()
    svc = core.get_fanpage_service()
    stats = await core.asyncio.to_thread(
        svc.prune_old_data,
        metric_retention_days=req.metric_retention_days,
        job_retention_days=req.job_retention_days,
    )
    return {"success": True, **stats}

@router.get("/api/fanpages")
async def list_fanpages_endpoint(
    q: Optional[str] = None,
    profile_id: Optional[str] = None,
    tag: Optional[str] = None,
    activity_status: Optional[str] = None,
    connection_status: Optional[str] = None,
    data_quality: Optional[str] = None,
    age_ready: Optional[str] = None,
    archived: str = "active",
    sort_by: str = "page_name",
    sort_dir: str = "asc",
    page: int = 1,
    page_size: int = 50,
):
    svc = core.get_fanpage_service()
    result = await core.asyncio.to_thread(
        svc.list_pages,
        q=q or "",
        profile_id=profile_id or "",
        tag=tag or "",
        activity_status=activity_status or "",
        connection_status=connection_status or "",
        data_quality=data_quality or "",
        age_ready=age_ready,
        archived=archived,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
    return {"success": True, **result}

@router.get("/api/fanpages/{page_id}")
async def get_fanpage_detail_endpoint(page_id: str):
    svc = core.get_fanpage_service()
    detail = await core.asyncio.to_thread(svc.get_page, page_id)
    if detail is None:
        raise core.HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, "page": detail}

@router.get("/api/fanpages/{page_id}/metrics")
async def get_fanpage_metrics_endpoint(page_id: str, days: int = 28):
    svc = core.get_fanpage_service()
    series = await core.asyncio.to_thread(svc.get_page_metrics, page_id, days=days)
    if series is None:
        raise core.HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, **series}

@router.post("/api/fanpages/{page_id}/merge")
async def merge_fanpage_endpoint(page_id: str, req: FanpageMergeRequest):
    svc = core.get_fanpage_service()
    try:
        merged = await core.asyncio.to_thread(
            svc.merge_pages, page_id, req.source_page_id
        )
    except KeyError as exc:
        raise core.HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc
    if merged is None:
        raise core.HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, "page": merged}

@router.patch("/api/fanpages/{page_id}")
async def update_fanpage_endpoint(page_id: str, req: FanpageUpdateRequest):
    svc = core.get_fanpage_service()
    try:
        updated = await core.asyncio.to_thread(
            svc.update_page,
            page_id,
            tags=req.tags,
            note=req.note,
            preferred_profile_id=req.preferred_profile_id,
            is_archived=req.is_archived,
            expected_revision=req.expected_revision,
        )
    except core.FanpageRevisionConflictError as exc:
        return core.JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "REVISION_CONFLICT",
                "detail": str(exc),
                "current_revision": exc.current_revision,
            },
        )
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc

    if updated is None:
        raise core.HTTPException(status_code=404, detail="Fanpage không tồn tại")
    return {"success": True, "page": updated}

@router.post("/api/fanpages/{page_id}/open")
async def open_fanpage_in_profile(
    page_id: str, req: Optional[FanpageOpenRequest] = None
):
    req = req or core.FanpageOpenRequest()
    svc = core.get_fanpage_service()
    detail = await core.asyncio.to_thread(svc.get_page, page_id)
    if detail is None:
        raise core.HTTPException(status_code=404, detail="Fanpage không tồn tại")

    linked_profiles = [
        lk
        for lk in (detail.get("linked_profiles") or [])
        if lk.get("link_status") != "profile_deleted"
        and not lk.get("profile_missing")
    ]
    linked_ids = {lk["profile_id"] for lk in linked_profiles}

    target_profile_id = (req.profile_id or "").strip() or None
    if target_profile_id:
        if target_profile_id not in linked_ids:
            raise core.HTTPException(
                status_code=400,
                detail="Profile được chọn không liên kết với fanpage này.",
            )
    else:
        preferred = detail.get("preferred_profile_id")
        if preferred and preferred in linked_ids:
            target_profile_id = preferred
        elif linked_profiles:
            active_links = [
                lk
                for lk in linked_profiles
                if lk.get("link_status") == "active"
            ]
            target_profile_id = (
                active_links[0]["profile_id"]
                if active_links
                else linked_profiles[0]["profile_id"]
            )

    if not target_profile_id:
        raise core.HTTPException(
            status_code=409,
            detail="Fanpage không còn profile khả dụng để mở.",
        )

    p = core.profile_manager.get_profile(target_profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")

    base_url = str(detail.get("canonical_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise core.HTTPException(
            status_code=400,
            detail="Fanpage chưa có URL hợp lệ (http/https) để mở.",
        )

    target_kind = (req.target or "page").strip().lower()
    if target_kind == "transparency":
        if "profile.php?id=" in base_url:
            open_url = f"{base_url}&sk=about_profile_transparency"
        else:
            open_url = f"{base_url.rstrip('/')}/about_profile_transparency"
    else:
        open_url = base_url

    gate = None
    gate_mode = core._launch_gate_mode()
    if gate_mode != "off":
        try:
            already_running = await core.asyncio.to_thread(
                core.launcher.is_running, target_profile_id
            )
        except Exception:
            already_running = False
        if not already_running:
            profiles = await core.asyncio.to_thread(core.profile_manager.list_profiles)
            browser_paths = await core.asyncio.to_thread(
                core._resolve_browser_paths, profiles
            )
            gate = await core.asyncio.to_thread(
                core._evaluate_launch_gate,
                p,
                profiles,
                browser_paths,
                gate_mode,
                bool(req.force),
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

    res = await core.asyncio.to_thread(
        core.launcher.launch, target_profile_id, open_url=open_url
    )
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
        raise core.HTTPException(
            status_code=500, detail=res.get("error", "Lỗi khởi chạy")
        )
    if gate and gate.status in ("warning", "overridden", "unverified"):
        res["gate"] = gate.as_dict()
    return {
        **res,
        "page_id": page_id,
        "profile_id": target_profile_id,
        "profile_name": p.name,
        "opened_url": open_url,
    }

@router.post("/api/fanpage-sync/jobs", status_code=202)
async def create_fanpage_sync_job_endpoint(req: FanpageSyncJobCreateRequest):
    svc = core.get_fanpage_service()
    try:
        job, deduplicated = await core.asyncio.to_thread(
            svc.enqueue_sync_job,
            mode=req.mode,
            profile_ids=req.profile_ids,
            page_ids=req.page_ids,
            auto_start=req.auto_start,
            only_running=req.only_running,
            force_new=req.force_new,
            triggered_by="user",
            run_async=True,
        )
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if "FANPAGE_JOB_BUSY" in str(exc) or "đang dừng" in str(exc):
            raise core.HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    return core.JSONResponse(
        status_code=202,
        content={
            "success": True,
            "deduplicated": deduplicated,
            "job": job,
        },
    )

@router.get("/api/fanpage-sync/jobs")
async def list_fanpage_sync_jobs_endpoint(limit: int = 20):
    svc = core.get_fanpage_service()
    jobs = await core.asyncio.to_thread(svc.list_sync_jobs, limit=limit)
    active = await core.asyncio.to_thread(svc.repo.find_active_sync_job)
    return {
        "success": True,
        "active_job": active,
        "jobs": jobs,
    }

@router.get("/api/fanpage-sync/jobs/{job_id}")
async def get_fanpage_sync_job_endpoint(job_id: str):
    svc = core.get_fanpage_service()
    job = await core.asyncio.to_thread(svc.get_sync_job, job_id)
    if job is None:
        raise core.HTTPException(status_code=404, detail="Job đồng bộ không tồn tại.")
    return {"success": True, "job": job}

@router.post("/api/fanpage-sync/jobs/{job_id}/cancel")
async def cancel_fanpage_sync_job_endpoint(job_id: str):
    svc = core.get_fanpage_service()
    job = await core.asyncio.to_thread(svc.cancel_sync_job, job_id)
    if job is None:
        raise core.HTTPException(status_code=404, detail="Job đồng bộ không tồn tại.")
    return {"success": True, "job": job}

@router.post("/api/fanpage-sync/jobs/{job_id}/retry", status_code=202)
async def retry_fanpage_sync_job_endpoint(
    job_id: str, req: Optional[FanpageSyncJobRetryRequest] = None
):
    req = req or core.FanpageSyncJobRetryRequest()
    svc = core.get_fanpage_service()
    try:
        job, deduplicated = await core.asyncio.to_thread(
            svc.retry_sync_job,
            job_id,
            auto_start=req.auto_start,
            run_async=True,
        )
    except KeyError as exc:
        raise core.HTTPException(status_code=404, detail="Job đồng bộ không tồn tại.") from exc
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc
    return core.JSONResponse(
        status_code=202,
        content={
            "success": True,
            "deduplicated": deduplicated,
            "job": job,
        },
    )

@router.get("/api/fanpage-alerts")
async def list_fanpage_alerts_endpoint(
    status: str = "active",
    severity: str = "",
    alert_type: str = "",
    limit: int = 100,
):
    svc = core.get_fanpage_service()
    data = await core.asyncio.to_thread(
        svc.list_alerts,
        status=status,
        severity=severity,
        alert_type=alert_type,
        limit=limit,
    )
    return {"success": True, **data}

@router.post("/api/fanpage-alerts/acknowledge-all")
async def acknowledge_all_fanpage_alerts_endpoint():
    svc = core.get_fanpage_service()
    count = await core.asyncio.to_thread(svc.acknowledge_all_alerts)
    return {"success": True, "acknowledged_count": count}

@router.patch("/api/fanpage-alerts/{alert_id}")
async def update_fanpage_alert_endpoint(
    alert_id: int, req: FanpageAlertUpdateRequest
):
    svc = core.get_fanpage_service()
    try:
        alert = await core.asyncio.to_thread(
            svc.update_alert,
            alert_id,
            action=req.action,
            snooze_hours=req.snooze_hours,
        )
    except ValueError as exc:
        raise core.HTTPException(status_code=400, detail=str(exc)) from exc
    if alert is None:
        raise core.HTTPException(status_code=404, detail="Cảnh báo không tồn tại.")
    return {"success": True, "alert": alert}
