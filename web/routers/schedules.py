from fastapi import APIRouter
from web import app as core

router = APIRouter()

ScheduleCreateRequest = core.ScheduleCreateRequest
ScheduleUpdateRequest = core.ScheduleUpdateRequest


@router.get("/api/schedules")
async def list_schedules():
    if core._schedule_store is None:
        raise core.HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rules = core._schedule_store.list()
    except core.ScheduleStoreCorruption as e:
        raise core.HTTPException(status_code=503, detail=core.SCHEDULES_CORRUPT_DETAIL) from e
    recent_runs = await core.asyncio.to_thread(core._schedule_history_with_jobs, 20)
    return {"success": True, "rules": rules,
            "last_tick": (core._scheduler.last_run_info.get("last_tick") if core._scheduler else None),
            "recent_runs": recent_runs}

@router.post("/api/schedules")
async def create_schedule(req: ScheduleCreateRequest):
    if core._schedule_store is None:
        raise core.HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rule = core._schedule_store.add(req.name, req.time, req.action,
                                   req.profile_ids, req.enabled)
    except core.ScheduleStoreCorruption as e:
        raise core.HTTPException(status_code=503, detail=core.SCHEDULES_CORRUPT_DETAIL) from e
    except ValueError as e:
        raise core.HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "rule": rule}

@router.put("/api/schedules/{rule_id}")
async def update_schedule(rule_id: str, req: ScheduleUpdateRequest):
    if core._schedule_store is None:
        raise core.HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        rule = core._schedule_store.update(rule_id, **req.model_dump(exclude_unset=True))
    except core.ScheduleStoreCorruption as e:
        raise core.HTTPException(status_code=503, detail=core.SCHEDULES_CORRUPT_DETAIL) from e
    except ValueError as e:
        raise core.HTTPException(status_code=400, detail=str(e)) from e
    if rule is None:
        raise core.HTTPException(status_code=404, detail="Không tìm thấy lịch")
    return {"success": True, "rule": rule}

@router.delete("/api/schedules/{rule_id}")
async def delete_schedule(rule_id: str):
    if core._schedule_store is None:
        raise core.HTTPException(status_code=503, detail="Scheduler chưa khởi động")
    try:
        deleted = core._schedule_store.delete(rule_id)
    except core.ScheduleStoreCorruption as e:
        raise core.HTTPException(status_code=503, detail=core.SCHEDULES_CORRUPT_DETAIL) from e
    if not deleted:
        raise core.HTTPException(status_code=404, detail="Không tìm thấy lịch")
    return {"success": True}
