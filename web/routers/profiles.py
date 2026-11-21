from fastapi import APIRouter
from web import app as core

router = APIRouter()

AuditFixRequest = core.AuditFixRequest
BulkCreateRequest = core.BulkCreateRequest
CreateProfileRequest = core.CreateProfileRequest
ImportCookiesRequest = core.ImportCookiesRequest
ImportProfilesRequest = core.ImportProfilesRequest
List = core.List
Optional = core.Optional
Profile = core.Profile
ProfileRuntime = core.ProfileRuntime
Request = core.Request
Response = core.Response
SwitchLocationRequest = core.SwitchLocationRequest
UpdateProfileRequest = core.UpdateProfileRequest


@router.get("/api/profiles", response_model=List[ProfileRuntime])
async def list_profiles():
    return await core.asyncio.to_thread(core._profiles_with_status)

@router.post("/api/profiles", response_model=Profile)
async def create_profile(req: CreateProfileRequest):
    # Nếu chưa truyền fingerprint, tự sinh theo OS, vị trí và proxy
    fp = req.fingerprint
    proxy = req.proxy or core.ProxyConfig()

    if fp is None:
        from antidetect.fingerprint import get_location_coordinates
        auto_lat, auto_lon, auto_tz = get_location_coordinates(req.city or "hanoi", randomize=True)
        timezone = auto_tz
        locale = "vi-VN"
        lat, lon = auto_lat, auto_lon

        # Nếu có proxy và bật, thử lấy geoip để đồng bộ múi giờ, locale & tọa độ theo proxy
        if proxy.enabled and proxy.host and proxy.port:
            geo = await core.asyncio.to_thread(core.check_proxy, proxy, 5)
            if geo.get("success"):
                timezone = geo.get("timezone", timezone)
                locale = core.locale_for_country(geo.get("country_code"), default=locale)
                lat = geo.get("lat", lat)
                lon = geo.get("lon", lon)

        fp = core.generate_fingerprint(
            target_os=req.os,
            timezone=timezone,
            locale=locale,
            latitude=lat,
            longitude=lon,
            city=req.city or "hanoi",
            browser_brand=core.detect_browser_brand(getattr(core.launcher, "browser_path", None)),
        )

    profile = core.profile_manager.create_profile(
        name=req.name,
        target_os=req.os,
        proxy=proxy,
        fingerprint=fp,
        notes=req.notes or "",
        tags=req.tags or []
    )
    return profile

@router.get("/api/profiles/export")
async def export_profiles(request: Request):
    """Xuất metadata toàn bộ profile ra JSON (KHÔNG kèm secret).

    Mật khẩu proxy không bao giờ xuất (DPAPI gắn máy — import sang máy khác
    cũng không dùng được). Yêu cầu header x-confirm-export: yes như cookies.
    """
    if request.headers.get("x-confirm-export") != "yes":
        raise core.HTTPException(status_code=403, detail="Cần xác nhận x-confirm-export: yes trước khi export.")
    profiles = core.profile_manager.list_profiles()
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
    return core.JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

@router.get("/api/profiles/audit")
async def audit_profiles_endpoint():
    """Fingerprint Health Center: static audit, no browser launched."""
    profiles = await core.asyncio.to_thread(core.profile_manager.list_profiles)
    browser_paths = await core.asyncio.to_thread(core._resolve_browser_paths, profiles)
    return await core.asyncio.to_thread(core.audit_profiles, profiles, browser_paths)

@router.post("/api/profiles/audit/fix")
async def fix_profiles_endpoint(req: AuditFixRequest):
    """Apply safe fingerprint repairs (dry_run by default)."""
    profiles = await core.asyncio.to_thread(core.profile_manager.list_profiles)
    selected_ids: core.Optional[core.Set[str]] = None
    if req.profile_ids is not None:
        if not req.profile_ids:
            raise core.HTTPException(
                status_code=400,
                detail="Danh sách profile được chọn trống. Bỏ profile_ids để kiểm tra toàn bộ.",
            )
        wanted = set(req.profile_ids)
        existing = {p.id for p in profiles}
        missing = sorted(wanted - existing)
        if missing:
            raise core.HTTPException(
                status_code=404,
                detail=f"Không tìm thấy profile: {', '.join(missing[:5])}",
            )
        selected_ids = wanted

    browser_paths = await core.asyncio.to_thread(core._resolve_browser_paths, profiles)
    # Cross-profile rules need the full set; the selection only scopes the
    # updates and decides whose seeds are reassigned.
    plan = await core.asyncio.to_thread(
        core.build_fix_plan, profiles, browser_paths, selected_ids
    )
    scoped = (
        [p for p in profiles if p.id in selected_ids]
        if selected_ids is not None
        else profiles
    )
    if selected_ids is not None:
        plan = [item for item in plan if item["profile_id"] in selected_ids]
    summary = {
        "profiles": len(scoped),
        "fixable_profiles": len(plan),
        "field_changes": sum(len(item["changes"]) for item in plan),
    }
    if req.dry_run:
        return {
            "dry_run": True,
            "changed": [],
            "changes": plan,
            "summary": summary,
            "message": (
                f"{len(plan)} profile có thể sửa an toàn; chưa ghi thay đổi nào."
                if plan
                else "Không có thay đổi an toàn nào cần áp dụng."
            ),
        }

    updates = {
        item["profile_id"]: core.FingerprintConfig.model_validate(item["fingerprint"])
        for item in plan
    }
    if not updates:
        report = await core.asyncio.to_thread(core.audit_profiles, profiles, browser_paths)
        return {
            "dry_run": False,
            "changed": [],
            "changes": [],
            "summary": summary,
            "report": report,
            "message": "Không có thay đổi an toàn nào cần áp dụng.",
        }

    expected = req.expected_revisions or {
        p.id: int(p.revision or 1) for p in profiles
    }
    try:
        changed = await core.asyncio.to_thread(
            core.profile_manager.update_fingerprints_batch, updates, expected
        )
    except RuntimeError as exc:
        raise core.HTTPException(status_code=409, detail=str(exc)) from exc

    fresh_profiles = await core.asyncio.to_thread(core.profile_manager.list_profiles)
    fresh_browser_paths = await core.asyncio.to_thread(
        core._resolve_browser_paths, fresh_profiles
    )
    report = await core.asyncio.to_thread(
        core.audit_profiles, fresh_profiles, fresh_browser_paths
    )
    return {
        "dry_run": False,
        "changed": changed,
        "changes": plan,
        "summary": summary,
        "report": report,
        "message": f"Đã sửa an toàn fingerprint cho {len(changed)} profile.",
    }

@router.get("/api/profiles/{profile_id}", response_model=Profile)
async def get_profile(profile_id: str):
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    if core.launcher.is_running(p.id):
        p.status = "running"
        p.port = core.launcher.active_ports.get(p.id)
    else:
        p.status = "stopped"
    return p

@router.put("/api/profiles/{profile_id}", response_model=Profile)
async def update_profile_endpoint(profile_id: str, req: UpdateProfileRequest):
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")

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
            and req.proxy.password in (None, "", core.REDACTED_SECRET)
        ):
            req.proxy.password = p.proxy.password
        p.proxy = req.proxy
    if req.fingerprint is not None:
        p.fingerprint = req.fingerprint
    if req.browser_path is not None:
        # "" = reset về browser mặc định của tool.
        p.browser_path = req.browser_path.strip() or None

    try:
        updated = core.profile_manager.update_profile(
            p, expected_revision=req.expected_revision if req.expected_revision is not None else p.revision
        )
    except RuntimeError as e:
        # expected_revision không khớp: profile đã bị client khác sửa.
        raise core.HTTPException(status_code=409, detail=str(e)) from e
    if not updated:
        # Profile biến mất giữa get và update (bị xóa bởi phiên khác).
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated

@router.post("/api/profiles/{profile_id}/reset-fingerprint", response_model=Profile)
async def reset_fingerprint_endpoint(profile_id: str, target_os: Optional[str] = None):
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")

    os_to_use = target_os or p.fingerprint.os
    timezone = p.fingerprint.timezone
    locale = p.fingerprint.locale
    lat = p.fingerprint.latitude
    lon = p.fingerprint.longitude

    # Nếu profile có proxy, kiểm tra xem có cần update geo/locale không
    if p.proxy and p.proxy.enabled and p.proxy.host and p.proxy.port:
        geo = await core.asyncio.to_thread(core.check_proxy, p.proxy, 4)
        if geo.get("success"):
            timezone = geo.get("timezone", timezone)
            locale = core.locale_for_country(geo.get("country_code"), default=locale)
            lat = geo.get("lat", lat)
            lon = geo.get("lon", lon)

    from antidetect.fingerprint import get_location_coordinates
    auto_lat, auto_lon, _ = get_location_coordinates("hanoi", randomize=True)
    new_fp = core.generate_fingerprint(
        target_os=os_to_use,
        timezone=timezone,
        locale=locale,
        latitude=lat if lat is not None else auto_lat,
        longitude=lon if lon is not None else auto_lon,
        browser_brand=core.detect_browser_brand(getattr(core.launcher, "browser_path", None)),
    )
    # Reset rotates identity values, not the user's algorithm/security policy.
    # Migration is an explicit edit and still uses the existing revision CAS.
    new_fp.fingerprint_algorithm_version = p.fingerprint.fingerprint_algorithm_version
    new_fp.verification_mode = p.fingerprint.verification_mode
    new_fp.stealth_disable = list(p.fingerprint.stealth_disable)
    new_fp.webrtc_mode = p.fingerprint.webrtc_mode
    p.fingerprint = new_fp
    try:
        updated = core.profile_manager.update_fingerprint_only(p.id, p.fingerprint, p.revision)
    except core.ProfileRevisionConflict as exc:
        raise core.HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if not updated:
        # Profile bị xóa bởi phiên khác giữa get và update.
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated

@router.post("/api/profiles/{profile_id}/set-location", response_model=Profile)
async def set_profile_location(profile_id: str, req: SwitchLocationRequest):
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    from antidetect.fingerprint import get_location_coordinates
    if req.latitude is not None and req.longitude is not None:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise core.HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")
        lat = req.latitude
        lon = req.longitude
        tz = req.timezone or p.fingerprint.timezone
    else:
        lat, lon, tz = get_location_coordinates(req.city, randomize=True)
        if req.timezone:
            tz = req.timezone

    if not core.validate_iana_timezone(tz):
        raise core.HTTPException(status_code=400, detail=f"Múi giờ IANA '{tz}' không hợp lệ")

    p.fingerprint.latitude = lat
    p.fingerprint.longitude = lon
    p.fingerprint.timezone = tz
    try:
        updated = core.profile_manager.patch_fingerprint(
            p.id, {"latitude": lat, "longitude": lon, "timezone": tz},
            expected_revision=p.revision,
        )
    except core.ProfileRevisionConflict as exc:
        raise core.HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if not updated:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    return updated

@router.post("/api/profiles/{profile_id}/sync-location-proxy")
async def sync_location_from_proxy(profile_id: str):
    """Giai đoạn 1 anti-detection: đồng bộ timezone/geo/locale theo IP proxy.

    Đổi proxy sau khi tạo profile là lỗi nhất quán phổ biến nhất (IP Mỹ +
    timezone Hà Nội → detector flag ngay). Endpoint này hỏi GeoIP QUA proxy
    của profile và cập nhật fingerprint cho khớp; trả về từng thay đổi để
    UI/automation hiển thị.
    """
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    if not (p.proxy and p.proxy.enabled and p.proxy.host):
        raise core.HTTPException(status_code=400, detail="Profile chưa bật proxy — không có IP để đồng bộ.")

    geo = await core.asyncio.to_thread(core.check_proxy, p.proxy, 12)
    if not geo.get("success"):
        raise core.HTTPException(status_code=502, detail=f"Không truy vấn được GeoIP qua proxy: {geo.get('error') or 'lỗi không xác định'}")

    try:
        changes = await core.asyncio.to_thread(core._commit_geo_result, p, geo)
    except core.ProfileRevisionConflict as exc:
        raise core.HTTPException(status_code=409, detail={
            "code": exc.code, "message": str(exc), "current_revision": exc.current_revision,
        }) from exc
    if not core.profile_manager.get_profile(profile_id):
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    return {
        "success": True,
        "geo": {"ip": geo.get("ip"), "country": geo.get("country"),
                "city": geo.get("city"), "isp": geo.get("isp")},
        "changes": changes,
        "message": "Fingerprint đã khớp IP proxy." if changes
                   else "Fingerprint đã khớp sẵn với IP proxy — không cần thay đổi.",
    }

@router.post("/api/profiles/switch-location-all")
async def switch_all_profiles_location(req: SwitchLocationRequest):
    """P1-03: Chuyển đổi vị trí toàn bộ profiles, kiểm tra 0 profiles, validate ZoneInfo."""
    from antidetect.fingerprint import get_location_coordinates

    custom_coords = req.latitude is not None and req.longitude is not None
    if custom_coords:
        if not (-90.0 <= req.latitude <= 90.0 and -180.0 <= req.longitude <= 180.0):
            raise core.HTTPException(status_code=400, detail="Tọa độ không hợp lệ: -90 <= lat <= 90, -180 <= lon <= 180")

    if req.timezone and not core.validate_iana_timezone(req.timezone):
        raise core.HTTPException(status_code=400, detail=f"Múi giờ IANA '{req.timezone}' không hợp lệ")

    def run_switch():
        # Chạy trong thread: list + update từng profile + is_running đều là
        # I/O/PowerShell đồng bộ. Batch ghi profiles.json đúng MỘT lần thay vì
        # N lần read+backup+write như từng update_profile riêng lẻ.
        profiles = core.profile_manager.list_profiles()
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
            if not core.validate_iana_timezone(tz):
                raise ValueError(f"Múi giờ IANA '{tz}' không hợp lệ")
            p.fingerprint.latitude = lat
            p.fingerprint.longitude = lon
            p.fingerprint.timezone = tz
            updates.append((p, lat, lon, tz))

        updated_ids = set(
            core.profile_manager.patch_fingerprints_batch(
                {p.id: {"latitude": lat, "longitude": lon, "timezone": tz}
                 for p, lat, lon, tz in updates},
                {p.id: p.revision for p, _, _, _ in updates},
            )
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
                "running": core.launcher.is_running(p.id),
                "updated": p.id in updated_ids,
            })
        return results, [p.id for p, _, _, _ in updates if p.id not in updated_ids]

    try:
        results, failed_ids = await core.asyncio.to_thread(run_switch)
    except ValueError as e:
        raise core.HTTPException(status_code=400, detail=str(e)) from e
    if results is None:
        raise core.HTTPException(status_code=404, detail="Không có profile nào để chuyển đổi vị trí")

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

@router.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    from antidetect.profile_lifecycle import stop_and_delete

    try:
        result = await core.asyncio.to_thread(stop_and_delete, core.profile_manager, core.launcher, profile_id)
    except ValueError as e:
        raise core.HTTPException(status_code=400, detail=str(e)) from e
    if not result.get("success"):
        raise core.HTTPException(
            status_code=404 if result.get("code") == "PROFILE_NOT_FOUND" else 409,
            detail=result.get("error") or result.get("message"),
        )
    return {"success": True, "message": "Đã xóa profile thành công"}

@router.post("/api/profiles/{profile_id}/duplicate", response_model=Profile)
async def duplicate_profile(
    profile_id: str,
    request: Request,
    response: Response,
    copy_data: bool = False,
):
    header_key = request.headers.get("idempotency-key")
    operation_id = header_key or core.new_operation_id()
    if header_key:
        try:
            core.validate_idempotency_key(header_key)
        except core.ProfileCopyError as e:
            return core.JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "code": e.code,
                    "detail": str(e),
                },
            )
    response.headers["X-Operation-Id"] = operation_id

    def _duplicate():
        return core.profile_manager.duplicate_profile(
            profile_id,
            copy_data=copy_data,
            operation_id=operation_id,
            source_running=core.launcher.profile_data_in_use,
        )

    try:
        result = await core.asyncio.to_thread(_duplicate)
    except core.ProfileCopyError as e:
        if e.code in ("SOURCE_RUNNING", "PROFILE_BUSY", "COPY_OPERATION_CONFLICT"):
            status_code = 409
        elif e.code == "COPY_NO_SPACE":
            status_code = 507
        elif e.code == "INVALID_IDEMPOTENCY_KEY":
            status_code = 400
        else:
            status_code = 500
        return core.JSONResponse(
            status_code=status_code,
            content={
                "success": False,
                "code": e.code,
                "detail": str(e),
            },
            headers={"X-Operation-Id": operation_id},
        )
    except ValueError as e:
        raise core.HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        return core.JSONResponse(
            status_code=500,
            content={
                "success": False,
                "code": "COPY_FAILED",
                "detail": str(e),
            },
            headers={"X-Operation-Id": operation_id},
        )
    if not result:
        return core.JSONResponse(
            status_code=404,
            content={
                "success": False,
                "code": "NOT_FOUND",
                "detail": "Profile không tồn tại",
            },
            headers={"X-Operation-Id": operation_id},
        )
    return result

@router.get("/api/profiles/{profile_id}/cookies/export")
async def export_profile_cookies(profile_id: str, request: Request):
    """Đọc cookies của profile đang chạy mà không ghi bản sao vào đĩa/cache."""
    if request.headers.get('x-confirm-cookie-access') != 'yes':
        raise core.HTTPException(status_code=403, detail='Cần xác nhận trước khi đọc cookie phiên đăng nhập.')
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    if not core.launcher.is_running(profile_id):
        raise core.HTTPException(status_code=409, detail="Hãy mở profile trước khi đọc cookies.")
    try:
        cookies = await core.asyncio.to_thread(core.launcher.export_cookies, profile_id)
    except core.CdpUnavailable as e:
        return core._cdp_conflict_response(e)
    except RuntimeError as e:
        raise core.HTTPException(status_code=503, detail=str(e)) from e
    return core.JSONResponse(
        content={"success": True, "profile_id": profile_id, "cookies": cookies, "count": len(cookies)},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )

@router.post("/api/profiles/{profile_id}/cookies/import")
async def import_profile_cookies(profile_id: str, req: ImportCookiesRequest):
    """Nạp cookies vào profile đang chạy, không lưu bản sao JSON trên đĩa."""
    p = core.profile_manager.get_profile(profile_id)
    if not p:
        raise core.HTTPException(status_code=404, detail="Profile không tồn tại")
    if not isinstance(req.cookies, list):
        raise core.HTTPException(status_code=400, detail="Định dạng cookies không hợp lệ, yêu cầu một mảng JSON các cookie.")
    if not core.launcher.is_running(profile_id):
        raise core.HTTPException(status_code=409, detail="Hãy mở profile trước khi nạp cookies.")

    def _import():
        # Đổi danh tính Facebook không được chạy chồng với lượt quét fanpage.
        with core.profile_manager.scan_lock(profile_id, timeout=0.0):
            return core.launcher.import_cookies(profile_id, req.cookies)

    try:
        success = await core.asyncio.to_thread(_import)
    except core.ProfileLockTimeout as e:
        raise core.HTTPException(
            status_code=409,
            detail="Đang có lượt quét fanpage cho profile này. Hãy chờ lượt đó xong.",
        ) from e
    except core.CdpUnavailable as e:
        return core._cdp_conflict_response(e)
    if not success:
        raise core.HTTPException(status_code=503, detail="Không thể nạp cookies vào profile đang chạy.")
    return {
        "success": True,
        "profile_id": profile_id,
        "count": len(req.cookies),
        "message": f"Đã nạp {len(req.cookies)} cookies thành công!"
    }

@router.post("/api/profiles/import")
async def import_profiles(req: ImportProfilesRequest):
    """Import metadata only; executable selection and proxy secrets stay local."""
    entries = req.profiles or []
    if not entries:
        raise core.HTTPException(status_code=400, detail="Không có profile nào trong payload (profiles: []).")
    if len(entries) > 200:
        raise core.HTTPException(status_code=400, detail="Tối đa 200 profile mỗi lần import.")

    imported, failed = [], []
    for i, entry in enumerate(entries):
        raw_name = entry.get("name") if isinstance(entry, dict) else None
        name = raw_name if isinstance(raw_name, str) else f"Imported {i + 1}"
        try:
            candidate = core.prepare_import_profile(entry, i, core._sanitize_facebook_pages)
            new_p = await core.asyncio.to_thread(
                core.profile_manager.commit_staged_profile, candidate,
            )
            imported.append({"id": new_p.id, "name": new_p.name})
        except ValueError as exc:
            failed.append({"index": i, "name": name, "code": "INVALID_PROFILE_IMPORT", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - per-item import
            failed.append({"index": i, "name": name, "code": "PROFILE_IMPORT_FAILED",
                           "reason": f"Không lưu được profile ({type(exc).__name__})."})
    return {
        "success": len(failed) == 0,
        "imported_count": len(imported),
        "failed_count": len(failed),
        "imported": imported,
        "failed": failed,
    }

@router.post("/api/profiles/bulk-create")
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
    if req.stealth_mode == "authentic" and req.os != host_os():
        raise core.HTTPException(
            status_code=400,
            detail=(
                "Stealth authentic chỉ hỗ trợ profile cùng hệ điều hành với "
                f"máy host ({host_os()}). Dùng unique cho profile {req.os}."
            ),
        )
    prefix = (req.name_prefix or "Profile").strip() or "Profile"

    created, warnings = [], []
    for i in range(req.count):
        name = f"{prefix} {i + 1:02d}"
        try:
            lat, lon, tz = get_location_coordinates(req.city, randomize=True)
            fp = core.generate_fingerprint(
                target_os=req.os, timezone=tz, locale="vi-VN",
                latitude=lat, longitude=lon, city=req.city,
                browser_brand=core.detect_browser_brand(getattr(core.launcher, "browser_path", None)),
            )
            if req.cpu:
                fp.hardware_concurrency = max(1, min(128, int(req.cpu)))
            if req.stealth_mode == "authentic":
                fp.stealth_disable = ["canvas", "webgl", "audio", "media", "worker"]
                if req.os == host_os():
                    vendor, renderer = core._host_gpu_strings()
                    fp.webgl_vendor, fp.webgl_renderer = vendor, renderer
            proxy = None
            if i < len(req.proxies):
                try:
                    proxy = parse_proxy_line(req.proxies[i])
                except ValueError:
                    warnings.append(f"{name}: proxy dòng {i + 1} không hợp lệ; không tạo profile")
                    continue
            else:
                warnings.append(f"{name}: không có proxy (direct)")
            p = core.profile_manager.create_profile(
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
