"""Asynchronous Fanpage Sync Queue & Worker (Stage 2).

Manages background ``discovery`` and ``basic`` (deep) sync jobs across multiple
profiles with bounded concurrency, per-profile scan locks, browser ownership
tracking (never closes a browser opened by the user), transient retry with
backoff, safe cancellation, and retry of failed/skipped items.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from antidetect.cdp_health import CdpUnavailable
from antidetect.facebook import ScanAborted
from antidetect.fanpage_scan import (
    DISCOVERY_ENDPOINT_TIMEOUT_S,
    REASON_LOGIN_IDENTITY_MISMATCH,
    REASON_LOGIN_LOST,
    REASON_UI_UNRECOGNIZED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    ensure_scan_contract,
    scan_detail,
)
from antidetect.profile_locks import ProfileLockTimeout

from .repository import FanpageRepository, JobLeaseLost

NO_RETRY_REASONS = frozenset(
    {
        REASON_LOGIN_LOST,
        REASON_LOGIN_IDENTITY_MISMATCH,
        REASON_UI_UNRECOGNIZED,
        "CHECKPOINT",
        "LOGIN_REQUIRED",
        "PROFILE_CLOSED",
        "PROFILE_NOT_FOUND",
        "LAUNCH_GATE_BLOCKED",
    }
)


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class FanpageSyncJobManager:
    """Background queue and worker pool for Fanpage Manager sync jobs."""

    def __init__(
        self,
        *,
        repo: FanpageRepository,
        profile_manager: Any,
        launcher: Any,
        on_profile_synced: Optional[Callable[[], Any]] = None,
        launch_gate_check: Optional[Callable[[Any], Tuple[bool, Optional[str]]]] = None,
        max_concurrent_profiles: int = 2,
        max_retries: int = 2,
        retry_delay_s: float = 0.5,
        owner_instance: Optional[str] = None,
        heartbeat_interval: float = 5.0,
        lease_seconds: float = 90.0,
    ) -> None:
        self.repo = repo
        self.profile_manager = profile_manager
        self.launcher = launcher
        self.on_profile_synced = on_profile_synced
        self.launch_gate_check = launch_gate_check
        self.max_concurrent_profiles = max(1, min(4, int(max_concurrent_profiles or 2)))
        self.max_retries = max(0, min(5, int(max_retries if max_retries is not None else 2)))
        self.retry_delay_s = max(0.01, float(retry_delay_s if retry_delay_s is not None else 0.5))
        self.owner_instance = owner_instance or uuid.uuid4().hex[:12]
        self.heartbeat_interval = max(0.01, float(heartbeat_interval))
        self.lease_seconds = max(self.heartbeat_interval * 3, float(lease_seconds))
        self._tokens: Dict[str, str] = {}
        self._closing = False

        self._lock = threading.RLock()
        self._cancel_events: Dict[str, threading.Event] = {}
        self._job_threads: Dict[str, threading.Thread] = {}
        self.repo.recover_interrupted_jobs(current_owner_instance=self.owner_instance)

    def resolve_target_profiles(
        self,
        *,
        profile_ids: Optional[List[str]] = None,
        page_ids: Optional[List[str]] = None,
        only_running: bool = False,
    ) -> List[Dict[str, Any]]:
        """Determine the ordered list of profiles to include in a sync job."""
        all_profiles = self.profile_manager.list_profiles()
        by_id = {p.id: p for p in all_profiles}

        selected_ids: List[str] = []
        if profile_ids is not None:
            for pid in profile_ids:
                clean = str(pid or "").strip()
                if clean and clean in by_id and clean not in selected_ids:
                    selected_ids.append(clean)
        elif page_ids:
            for pg_id in page_ids:
                detail = self.repo.get_page_detail(str(pg_id or "").strip())
                if not detail:
                    continue
                pref = detail.get("preferred_profile_id")
                links = detail.get("profile_links") or []
                chosen = None
                if pref and pref in by_id:
                    chosen = pref
                else:
                    for lk in links:
                        if lk.get("link_status") != "profile_deleted" and lk.get("profile_id") in by_id:
                            chosen = lk["profile_id"]
                            break
                if chosen and chosen not in selected_ids:
                    selected_ids.append(chosen)
        else:
            for p in all_profiles:
                if only_running:
                    try:
                        if not self.launcher.is_running(p.id):
                            continue
                    except Exception:
                        continue
                selected_ids.append(p.id)

        return [
            {"profile_id": pid, "profile_name": by_id[pid].name}
            for pid in selected_ids
            if pid in by_id
        ]

    def enqueue_job(
        self,
        *,
        mode: str = "discovery",
        profile_ids: Optional[List[str]] = None,
        page_ids: Optional[List[str]] = None,
        auto_start: bool = False,
        only_running: bool = False,
        force_new: bool = False,
        triggered_by: str = "user",
        run_async: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """Create and start a sync job, or return an existing active job if deduplicated.

        Returns ``(job_dict, deduplicated_bool)``.
        """
        clean_mode = (mode or "discovery").strip().lower()
        if clean_mode not in ("discovery", "basic"):
            raise ValueError("Chế độ đồng bộ không hợp lệ (chỉ hỗ trợ 'discovery' hoặc 'basic').")

        with self._lock:
            if self._closing:
                raise RuntimeError("Fanpage manager đang dừng; không nhận job mới.")

            # A persisted occurrence remains the same request even if its
            # original profiles have since been removed or renamed.
            if idempotency_key:
                existing = self.repo.get_sync_job_by_idempotency_key(idempotency_key)
                if existing:
                    return existing, True

            target_items = self.resolve_target_profiles(
                profile_ids=profile_ids,
                page_ids=page_ids,
                only_running=only_running,
            )
            if not target_items:
                raise ValueError(
                    "Không có profile phù hợp để đồng bộ. Hãy kiểm tra bộ lọc hoặc mở profile trước."
                )

            job_id = f"fjob_{uuid.uuid4().hex[:12]}"
            options = {
                "mode": clean_mode,
                "auto_start": bool(auto_start),
                "only_running": bool(only_running),
                "profile_ids": [it["profile_id"] for it in target_items],
                "page_ids": list(page_ids or []),
                "triggered_by": triggered_by,
                "max_concurrent_profiles": self.max_concurrent_profiles,
            }
            execution_token = uuid.uuid4().hex
            job = self.repo.create_sync_job(
                job_id=job_id,
                mode=clean_mode,
                profile_items=target_items,
                options=options,
                owner_instance=self.owner_instance,
                execution_token=execution_token,
                lease_seconds=self.lease_seconds,
                force_new=force_new,
                idempotency_key=idempotency_key,
            )
            if job["id"] != job_id:
                if idempotency_key and job.get("idempotency_key") == idempotency_key:
                    return job, True
                if idempotency_key:
                    raise RuntimeError("FANPAGE_JOB_BUSY: một job khác đang chạy.")
                old_opts = job.get("options") or {}
                same_request = all(old_opts.get(k) == options.get(k) for k in ("mode", "auto_start", "only_running", "profile_ids", "page_ids"))
                if not same_request:
                    raise RuntimeError("FANPAGE_JOB_BUSY: một job khác đang chạy.")
                return job, True
            self._tokens[job_id] = execution_token
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event

            if run_async:
                t = threading.Thread(
                    target=self._run_job_worker,
                    args=(job_id, cancel_event),
                    name=f"fanpage-sync-{job_id}",
                    daemon=True,
                )
                self._job_threads[job_id] = t
                t.start()
            else:
                self._run_job_worker(job_id, cancel_event)
                job = self.repo.get_sync_job(job_id) or job

            return job, False

    def cancel_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Persist cancellation so the owner observes requests from any instance."""
        job = self.repo.request_job_cancel(job_id)
        with self._lock:
            ev = self._cancel_events.get(job_id)
            if ev is not None:
                ev.set()
        return job

    def shutdown(self, timeout: float = 30.0) -> None:
        with self._lock:
            self._closing = True
            active = list(self._job_threads.items())
        for job_id, _thread in active:
            self.cancel_job(job_id)
        deadline = time.monotonic() + max(0.0, timeout)
        for _job_id, thread in active:
            thread.join(max(0.0, deadline - time.monotonic()))
        for job_id, thread in active:
            if thread.is_alive():
                token = self._tokens.get(job_id)
                if token:
                    self.repo.revoke_job(job_id, token)

    def _update_item(self, item_id: int, *, job_id: str, **kwargs):
        return self.repo.update_sync_job_item(item_id, job_id=job_id, execution_token=self._tokens.get(job_id), **kwargs)

    def _update_status(self, job_id: str, status: str, **kwargs):
        return self.repo.update_sync_job_status(job_id, status, execution_token=self._tokens.get(job_id), **kwargs)

    def retry_job(
        self,
        job_id: str,
        *,
        auto_start: Optional[bool] = None,
        run_async: bool = True,
    ) -> Tuple[Dict[str, Any], bool]:
        """Create a new sync job for failed, partial, skipped, or cancelled profiles of a job."""
        old_job = self.repo.get_sync_job(job_id)
        if not old_job:
            raise KeyError("Job không tồn tại.")

        retry_profile_ids = [
            it["profile_id"]
            for it in (old_job.get("items") or [])
            if it.get("status") in ("failed", "partial", "skipped", "cancelled")
        ]
        if not retry_profile_ids:
            raise ValueError("Job này không có mục lỗi hoặc bị bỏ qua nào cần thử lại.")

        old_opts = old_job.get("options") or {}
        use_auto_start = (
            bool(auto_start)
            if auto_start is not None
            else bool(old_opts.get("auto_start", False))
        )
        return self.enqueue_job(
            mode=str(old_job.get("mode") or "discovery"),
            profile_ids=retry_profile_ids,
            auto_start=use_auto_start,
            force_new=True,
            triggered_by="retry",
            run_async=run_async,
        )

    def _run_job_worker(self, job_id: str, cancel_event: threading.Event) -> None:
        heartbeat_stop = threading.Event()
        token = self._tokens[job_id]
        def beat():
            while not heartbeat_stop.wait(self.heartbeat_interval):
                try:
                    current = self.repo.renew_job_lease(job_id, token, self.lease_seconds)
                    if current.get("cancel_requested_at"):
                        cancel_event.set()
                except Exception:
                    cancel_event.set()
                    return
        heartbeat = threading.Thread(target=beat, daemon=True, name=f"fanpage-lease-{job_id}")
        heartbeat.start()
        try:
            started_iso = _utc_now_iso()
            job = self._update_status(
                job_id, "running", started_at=started_iso
            )
            if not job:
                return

            options = job.get("options") or {}
            mode = str(job.get("mode") or "discovery")
            deep = mode == "basic"
            auto_start = bool(options.get("auto_start", False))
            items = list(job.get("items") or [])

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_concurrent_profiles,
                thread_name_prefix=f"fp-item-{job_id[:8]}",
            ) as pool:
                futures = {
                    pool.submit(
                        self._execute_profile_item,
                        job_id=job_id,
                        item=item,
                        deep=deep,
                        auto_start=auto_start,
                        cancel_event=cancel_event,
                    ): item
                    for item in items
                }
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        fut.result()
                    except JobLeaseLost:
                        raise
                    except Exception as exc:
                        self._update_item(
                            int(futures[fut]["id"]), job_id=job_id, status="failed",
                            error_code="WORKER_ERROR", error=f"{type(exc).__name__}: {exc}",
                            finished_at=_utc_now_iso(),
                        )

            # Determine final job status
            refreshed = self.repo.get_sync_job(job_id) or {}
            if refreshed.get("cancel_requested_at"):
                cancel_event.set()
            ref_items = refreshed.get("items") or []
            for unfinished in ref_items:
                if unfinished.get("status") in ("pending", "running"):
                    unfinished["status"] = "cancelled" if cancel_event.is_set() else "failed"
                    self._update_item(
                        int(unfinished["id"]), job_id=job_id, status=unfinished["status"],
                        error_code="CANCELLED" if cancel_event.is_set() else "WORKER_INCOMPLETE",
                        error="Worker kết thúc khi mục chưa có kết quả cuối.",
                        finished_at=_utc_now_iso(),
                    )
            statuses = [it.get("status") for it in ref_items]
            cleanup_failed = any(
                (it.get("detail") or {}).get("cleanup", {}).get("status") == "failed"
                for it in ref_items
            )
            if cancel_event.is_set() or (statuses and all(s == "cancelled" for s in statuses)):
                final_status = "cancelled"
            elif any(s == "failed" for s in statuses):
                if any(s in ("completed", "partial") for s in statuses):
                    final_status = "completed_with_errors"
                else:
                    final_status = "failed"
            elif cleanup_failed or any(s in ("partial", "skipped") for s in statuses):
                final_status = "completed_with_warnings"
            else:
                final_status = "completed"

            self._update_status(
                job_id,
                final_status,
                finished_at=_utc_now_iso(),
            )
        except JobLeaseLost:
            cancel_event.set()
        except Exception as exc:
            try:
                self._update_status(
                    job_id, "failed", error=f"{type(exc).__name__}: {exc}",
                    finished_at=_utc_now_iso(),
                )
            except JobLeaseLost:
                cancel_event.set()
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2.0)
            with self._lock:
                self._cancel_events.pop(job_id, None)
                self._job_threads.pop(job_id, None)
                self._tokens.pop(job_id, None)

    def _execute_profile_item(self, *, job_id, item, deep, auto_start, cancel_event):
        # Keep launch/scan/cleanup exclusive to this profile, including jobs
        # created with force_new. Never close a session a second job borrowed.
        try:
            with self.profile_manager.scan_lock(str(item["profile_id"]), timeout=3.0):
                self._execute_profile_item_locked(job_id=job_id, item=item, deep=deep, auto_start=auto_start, cancel_event=cancel_event)
        except JobLeaseLost:
            cancel_event.set()
        except ProfileLockTimeout:
            self._update_item(int(item["id"]), job_id=job_id, status="skipped", error_code="PROFILE_BUSY", error="Profile đang được quét bởi job khác.", finished_at=_utc_now_iso())

    def _execute_profile_item_locked(
        self,
        *,
        job_id: str,
        item: Dict[str, Any],
        deep: bool,
        auto_start: bool,
        cancel_event: threading.Event,
    ) -> None:
        item_id = int(item["id"])
        profile_id = str(item["profile_id"])

        if cancel_event.is_set():
            self._update_item(
                item_id,
                job_id=job_id,
                status="cancelled",
                error_code="CANCELLED",
                error="Đã hủy trước khi chạy.",
                finished_at=_utc_now_iso(),
            )
            return

        started_iso = _utc_now_iso()
        self._update_item(
            item_id,
            job_id=job_id,
            status="running",
            attempts=1,
            started_at=started_iso,
        )

        profile = self.profile_manager.get_profile(profile_id)
        if profile is None:
            self._update_item(
                item_id,
                job_id=job_id,
                status="skipped",
                attempts=1,
                error_code="PROFILE_NOT_FOUND",
                error="Profile không còn tồn tại.",
                finished_at=_utc_now_iso(),
            )
            return

        opened_by_job = False
        owned_launch_id = None
        try:
            try:
                was_running = bool(self.launcher.is_running(profile_id))
            except Exception:
                was_running = False

            if not was_running:
                if not auto_start:
                    self._update_item(
                        item_id,
                        job_id=job_id,
                        status="skipped",
                        attempts=1,
                        error_code="PROFILE_CLOSED",
                        error="Profile đang đóng. Hãy mở profile trước hoặc bật tùy chọn Tự mở profile.",
                        detail={"opened_by_job": False},
                        finished_at=_utc_now_iso(),
                    )
                    return

                if self.launch_gate_check is not None:
                    allowed, gate_msg = self.launch_gate_check(profile)
                    if not allowed:
                        self._update_item(
                            item_id,
                            job_id=job_id,
                            status="failed",
                            attempts=1,
                            error_code="LAUNCH_GATE_BLOCKED",
                            error=gate_msg or "Cổng fingerprint chặn khởi chạy profile này.",
                            detail={"opened_by_job": False},
                            finished_at=_utc_now_iso(),
                        )
                        return

                launch_res = self.launcher.launch(profile_id)
                if not isinstance(launch_res, dict) or not launch_res.get("success"):
                    err_text = (
                        (launch_res or {}).get("error")
                        if isinstance(launch_res, dict)
                        else "Không mở được profile."
                    )
                    self._update_item(
                        item_id,
                        job_id=job_id,
                        status="failed",
                        attempts=1,
                        error_code="LAUNCH_FAILED",
                        error=str(err_text or "Không mở được profile."),
                        detail={"opened_by_job": False},
                        finished_at=_utc_now_iso(),
                    )
                    return
                opened_by_job = launch_res.get("launched_now") is True
                health = launch_res.get("cdp_health") or {}
                owned_launch_id = launch_res.get("launch_id") or health.get("launch_id")

            max_attempts = 1 + self.max_retries
            last_error_code = "SCAN_FAILED"
            last_error_msg = "Lỗi không xác định khi quét fanpage."
            last_detail: Dict[str, Any] = {"opened_by_job": opened_by_job}

            for attempt in range(1, max_attempts + 1):
                if cancel_event.is_set():
                    self._update_item(
                        item_id,
                        job_id=job_id,
                        status="cancelled",
                        attempts=attempt,
                        error_code="CANCELLED",
                        error="Đã hủy trong khi chạy.",
                        detail=last_detail,
                        finished_at=_utc_now_iso(),
                    )
                    return

                self._update_item(
                    item_id,
                    job_id=job_id,
                    status="running",
                    attempts=attempt,
                    detail=last_detail,
                )

                try:
                    with self.profile_manager.scan_lock(profile_id, timeout=3.0):
                        scan_meta = self.profile_manager.get_facebook_scan_meta(profile_id) or {}
                        raw_data = self.launcher.fetch_facebook_pages(
                            profile_id,
                            DISCOVERY_ENDPOINT_TIMEOUT_S,
                            deep,
                        )
                        scan = ensure_scan_contract(
                            raw_data,
                            expected_account_id=scan_meta.get("account_id_observed"),
                        )
                        with self.repo.job_guard(job_id, self._tokens.get(job_id)):
                            self.repo.renew_job_lease(job_id, self._tokens[job_id], self.lease_seconds)
                            applied = self.profile_manager.apply_facebook_scan(profile_id, scan)
                            if applied is not None:
                                updated_prof, scan = applied
                            else:
                                updated_prof = self.profile_manager.get_profile(profile_id)

                            if self.on_profile_synced:
                                self.on_profile_synced()
                            self.repo.renew_job_lease(job_id, self._tokens[job_id], self.lease_seconds)

                        disc_status = str(scan.get("discovery_status") or STATUS_FAILED)
                        reason = str(scan.get("completion_reason") or "")
                        total_cached = len((updated_prof.facebook_pages if updated_prof else []) or [])
                        summary_msg = scan_detail(scan, total_cached)
                        last_detail = {
                            "opened_by_job": opened_by_job,
                            "scan_id": scan.get("scan_id"),
                            "discovery_status": disc_status,
                            "completion_reason": reason,
                            "observed_count": scan.get("observed_count"),
                            "retained_count": scan.get("retained_count"),
                            "total_cached": total_cached,
                            "deep": deep,
                            "summary": summary_msg,
                        }

                        if reason in NO_RETRY_REASONS or scan.get("login_status") is False:
                            self._update_item(
                                item_id,
                                job_id=job_id,
                                status="failed",
                                attempts=attempt,
                                error_code=reason or "LOGIN_REQUIRED",
                                error=summary_msg,
                                detail=last_detail,
                                finished_at=_utc_now_iso(),
                            )
                            return

                        if disc_status == STATUS_COMPLETE:
                            self._update_item(
                                item_id,
                                job_id=job_id,
                                status="completed",
                                attempts=attempt,
                                error_code=None,
                                error=None,
                                detail=last_detail,
                                finished_at=_utc_now_iso(),
                            )
                            return

                        if disc_status == STATUS_PARTIAL:
                            self._update_item(
                                item_id,
                                job_id=job_id,
                                status="partial",
                                attempts=attempt,
                                error_code=reason or "PARTIAL_DISCOVERY",
                                error=summary_msg,
                                detail=last_detail,
                                finished_at=_utc_now_iso(),
                            )
                            return

                        # STATUS_FAILED with potentially transient reason
                        last_error_code = reason or "SCAN_FAILED"
                        last_error_msg = summary_msg

                except ProfileLockTimeout:
                    last_error_code = "PROFILE_SCAN_BUSY"
                    last_error_msg = "Profile đang bận bởi một lượt quét fanpage khác."
                except ScanAborted as exc:
                    self._update_item(
                        item_id,
                        job_id=job_id,
                        status="failed",
                        attempts=attempt,
                        error_code="SCAN_ABORTED",
                        error=str(exc),
                        detail=last_detail,
                        finished_at=_utc_now_iso(),
                    )
                    return
                except JobLeaseLost:
                    cancel_event.set()
                    try:
                        self._update_item(item_id, job_id=job_id, status="cancelled", error_code="CANCELLED", error="Job đã bị hủy hoặc mất quyền ghi.", finished_at=_utc_now_iso())
                    except JobLeaseLost:
                        pass
                    return
                except CdpUnavailable as exc:
                    last_error_code = exc.code or "CDP_UNAVAILABLE"
                    last_error_msg = str(exc)
                except Exception as exc:
                    last_error_code = "TRANSIENT_ERROR"
                    last_error_msg = f"{type(exc).__name__}: {exc}"

                if attempt < max_attempts and not cancel_event.is_set():
                    time.sleep(self.retry_delay_s * attempt)

            self._update_item(
                item_id,
                job_id=job_id,
                status="failed",
                attempts=max_attempts,
                error_code=last_error_code,
                error=last_error_msg,
                detail=last_detail,
                finished_at=_utc_now_iso(),
            )
        finally:
            # QUY TẮC CỐT LÕI: Chỉ đóng trình duyệt nếu chính job này đã mở nó!
            # Trình duyệt do người dùng mở từ trước tuyệt đối không bị đóng.
            if opened_by_job and owned_launch_id:
                try:
                    stopped = self.launcher.stop(profile_id, expected_launch_id=owned_launch_id)
                    if isinstance(stopped, dict) and stopped.get("code") == "SESSION_CHANGED":
                        cleanup = {"status": "session_changed", "launch_id": owned_launch_id}
                    elif isinstance(stopped, dict) and stopped.get("success") is True:
                        cleanup = {"status": "completed", "launch_id": owned_launch_id}
                    else:
                        cleanup = {"status": "failed", "launch_id": owned_launch_id,
                                   "code": stopped.get("code") if isinstance(stopped, dict) else "STOP_UNCONFIRMED",
                                   "error": str(stopped.get("error") or "Không xác minh được browser đã dừng.") if isinstance(stopped, dict) else "Không xác minh được browser đã dừng."}
                except Exception as exc:
                    cleanup = {"status": "failed", "launch_id": owned_launch_id,
                               "code": "STOP_ERROR", "error": f"{type(exc).__name__}: {exc}"}
                try:
                    self.repo.merge_sync_job_item_detail(
                        item_id, job_id=job_id, detail={"cleanup": cleanup},
                        execution_token=self._tokens.get(job_id),
                    )
                except JobLeaseLost:
                    # The browser cleanup still runs after revocation, but
                    # an old owner must never modify the new owner's result.
                    pass
