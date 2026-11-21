from fastapi import APIRouter
from web import app as core

router = APIRouter()


@router.get("/api/profiles/{profile_id}/facebook/pages")
async def get_facebook_pages(
    profile_id: str, refresh: bool = False, deep: bool = False, force: bool = False
):
    """Liệt kê fanpage Facebook profile này sở hữu/quản lý.

    ``refresh=1`` (hoặc khi profile đang chạy mà chưa có cache) đọc trực tiếp
    từ phiên Chrome đang chạy. Discovery trả hợp đồng F4: ``discovery_status``
    (complete/partial/failed), ``completion_reason``, ``observed_count``,
    ``retained_count`` — tổng cache không bị báo nhầm là tổng vừa đọc.

    ``deep=1`` (kèm refresh) quét thêm TỪNG page: ngày tạo (trang about),
    khoảng thời gian bài gần nhất (feed) → trạng thái active/long_absent/
    uncertain/unknown và "sẵn sàng" (tuổi ≥ 30 ngày). Mỗi page tốn ~4-8s khi
    deep — với 20 page mất ~2-3 phút.

    Response cache trả ``login_status="unknown"`` (không khẳng định phiên còn
    đăng nhập); ``logged_in`` chỉ true/false khi vừa kiểm tra live. ``stale``
    phản ánh tuổi dữ liệu/độ đầy đủ của lần quét tương ứng, không luôn false.
    Cache không bao giờ bị xóa bởi refresh thông thường; dùng DELETE để xóa.
    """
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    running = await core.asyncio.to_thread(core.launcher.is_running, profile_id)
    now = core.datetime.datetime.now(core.datetime.timezone.utc)
    display_tz = core._profile_display_timezone(p)
    stale_after_hours = core._fanpage_stale_after_hours()

    if refresh or (running and not p.facebook_pages):
        if not running:
            raise core.HTTPException(
                status_code=409,
                detail="Hãy mở profile trước khi làm mới danh sách fanpage.",
            )
        try:
            result = await core.asyncio.to_thread(
                core._run_facebook_scan,
                profile_id,
                bool(deep),
                bool(force),
                stale_after_hours,
            )
        except core.ProfileLockTimeout:
            return core.JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "Đang có lượt quét fanpage khác cho profile này. "
                        "Hãy chờ lượt đó xong rồi thử lại."
                    )
                },
            )
        except core.ScanAborted as e:
            return core.JSONResponse(
                status_code=409,
                content={"detail": str(e), "code": "SCAN_ABORTED"},
            )
        except core.CdpUnavailable as e:
            return core._cdp_conflict_response(e)
        except RuntimeError as e:
            raise core.HTTPException(status_code=503, detail=str(e)) from e
        if result is None:
            raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
        return result

    # Cache: chưa kiểm tra phiên đăng nhập nên không khẳng định logged_in.
    enriched = core._enrich_facebook_pages(
        p.facebook_pages,
        now=now,
        timezone=display_tz,
        stale_after_hours=stale_after_hours,
    )
    meta = core.profile_manager.get_facebook_scan_meta(profile_id)
    last_scan = meta.get("last_scan") if isinstance(meta.get("last_scan"), dict) else {}
    scan: core.Dict[str, core.Any] = dict(core.scan_defaults(len(enriched)))
    scan.update({k: v for k, v in last_scan.items() if v is not None})
    # Cache không xác nhận phiên đăng nhập hiện tại.
    scan["login_status"] = "unknown"
    scan["identity_status"] = (
        meta.get("identity_status") or scan.get("identity_status") or "unknown"
    )
    stale = core._fanpage_stale(
        scan, enriched, p.facebook_pages_synced_at, now, stale_after_hours
    )
    return {
        "success": True,
        "logged_in": None,
        "login_status": "unknown",
        "pages": enriched,
        "source": "cache",
        "stale": stale,
        "synced_at": p.facebook_pages_synced_at,
        **core._scan_fields(scan),
        "detail": core.scan_detail(scan, len(enriched)),
    }

@router.delete("/api/profiles/{profile_id}/facebook/pages")
async def delete_facebook_pages(profile_id: str):
    """Xóa cache fanpage theo hành động rõ ràng (không tự xóa khi refresh)."""
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")

    def _clear():
        with core.profile_manager.scan_lock(profile_id, timeout=0.0):
            return core.profile_manager.clear_facebook_pages(profile_id)

    try:
        updated = await core.asyncio.to_thread(_clear)
    except core.ProfileLockTimeout:
        return core.JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "Đang có lượt quét fanpage cho profile này. "
                    "Hãy chờ lượt đó xong rồi xóa cache."
                )
            },
        )
    if updated is None:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    return {
        "success": True,
        "pages": [],
        "synced_at": updated.facebook_pages_synced_at,
        "detail": "Đã xóa cache fanpage của profile.",
    }
