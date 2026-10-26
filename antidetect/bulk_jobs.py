"""Cross-process state for long-running bulk jobs (Start-All).

Only one bulk job may be active for a shared data directory. State lives in
``data/bulk_jobs.json`` behind an interprocess lock, and the owner renews a
heartbeat lease while it runs. A new server instance may take over only after
the lease goes stale, so a crashed owner cannot block the farm forever.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .applog import get_logger
from .storage import InterProcessLock, _replace_with_retry
from . import metrics

log = get_logger("bulk_jobs")

ACTIVE_STATUSES = ("running", "cancelling")
DEFAULT_STALE_SECONDS = 90.0
MAX_RESULTS = 500


class BulkJobStoreCorruption(RuntimeError):
    pass


def _empty_state() -> Dict[str, Any]:
    return {"schema": 1, "current": None}


class BulkJobStore:
    def __init__(self, path: str, stale_seconds: float = DEFAULT_STALE_SECONDS):
        self.path = path
        self.stale_seconds = max(1.0, float(stale_seconds))
        self._lock = threading.RLock()
        self._ipc_lock = InterProcessLock(f"{path}.lock", timeout=8.0)

    def _read_locked(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return _empty_state()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BulkJobStoreCorruption(
                f"Không đọc được bulk job state: {self.path}"
            ) from exc
        if not isinstance(data, dict):
            raise BulkJobStoreCorruption(
                f"Bulk job state phải là JSON object: {self.path}"
            )
        current = data.get("current")
        if current is not None and not isinstance(current, dict):
            raise BulkJobStoreCorruption(
                f"Bulk job 'current' phải là object: {self.path}"
            )
        return {
            "schema": data.get("schema", 1),
            "current": copy.deepcopy(current) if isinstance(current, dict) else None,
        }

    def _write_locked(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, self.path)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _is_active(self, job: Optional[Dict[str, Any]], now: float) -> bool:
        if not job or job.get("status") not in ACTIVE_STATUSES:
            return False
        try:
            heartbeat = float(job.get("heartbeat_at") or job.get("start_time") or 0)
        except (TypeError, ValueError):
            heartbeat = 0.0
        return (now - heartbeat) <= self.stale_seconds

    def is_active(self, job: Optional[Dict[str, Any]]) -> bool:
        return self._is_active(job, time.time())

    def read(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read_locked()["current"]

    def try_claim(
        self,
        job_id: str,
        owner_instance: str,
        kind: str,
        total: int,
        starting_count: int = 0,
        max_concurrent: int = 0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Claim the single bulk-job slot.

        A live job owned by another instance blocks the claim. A live job owned
        by this same instance is replaceable: the in-memory job is authoritative
        and a missing local record means the previous claim cannot still be
        executing (for example after a test reset).
        """
        now = time.time()
        with self._lock, self._ipc_lock:
            state = self._read_locked()
            current = state.get("current")
            if current:
                same_owner = str(current.get("owner_instance") or "") == str(owner_instance)
                live = self._is_active(current, now)
                if live and not same_owner:
                    metrics.inc("bulk_job_conflicts")
                    return False, copy.deepcopy(current)
                if current.get("status") in ACTIVE_STATUSES and not live:
                    metrics.inc("bulk_job_stale")
                    log.warning(
                        "Bulk job cũ %s đã hết lease (owner=%s); cho phép claim mới.",
                        current.get("job_id"), current.get("owner_instance"),
                    )
            job: Dict[str, Any] = {
                "job_id": job_id,
                "kind": kind,
                "status": "running",
                "owner_instance": owner_instance,
                "owner_pid": os.getpid(),
                "total": int(total),
                "starting_count": int(starting_count or 0),
                "completed_count": 0,
                "failed_count": 0,
                "max_concurrent": int(max_concurrent or 0),
                "results": [],
                "start_time": now,
                "heartbeat_at": now,
                "end_time": None,
                "error": None,
                "cancel_requested": False,
            }
            if extra:
                job.update(extra)
            job["job_id"] = job_id
            job["owner_instance"] = owner_instance
            job["status"] = "running"
            state["current"] = job
            self._write_locked(state)
            return True, copy.deepcopy(job)

    def update(self, job_id: str, owner_instance: str, **fields: Any) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock, self._ipc_lock:
            state = self._read_locked()
            current = state.get("current")
            if (
                not current
                or current.get("job_id") != job_id
                or current.get("owner_instance") != owner_instance
                or current.get("status") not in ACTIVE_STATUSES
            ):
                return None
            current.update(fields)
            current["job_id"] = job_id
            current["owner_instance"] = owner_instance
            current["heartbeat_at"] = now
            results = current.get("results")
            if isinstance(results, list) and len(results) > MAX_RESULTS:
                current["results"] = results[-MAX_RESULTS:]
            self._write_locked(state)
            return copy.deepcopy(current)

    def heartbeat(self, job_id: str, owner_instance: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock, self._ipc_lock:
            state = self._read_locked()
            current = state.get("current")
            if (
                not current
                or current.get("job_id") != job_id
                or current.get("owner_instance") != owner_instance
                or current.get("status") not in ACTIVE_STATUSES
            ):
                return None
            current["heartbeat_at"] = now
            self._write_locked(state)
            return copy.deepcopy(current)

    def finish(
        self,
        job_id: str,
        owner_instance: str,
        status: str,
        error: Optional[str] = None,
        results: Optional[list] = None,
    ) -> bool:
        with self._lock, self._ipc_lock:
            state = self._read_locked()
            current = state.get("current")
            if (
                not current
                or current.get("job_id") != job_id
                or current.get("owner_instance") != owner_instance
                or current.get("status") not in ACTIVE_STATUSES
            ):
                return False
            current["status"] = status
            current["end_time"] = time.time()
            current["heartbeat_at"] = current["end_time"]
            current["cancel_requested"] = False
            if error:
                current["error"] = str(error)[:500]
            if results is not None:
                current["results"] = list(results)[-MAX_RESULTS:]
            self._write_locked(state)
            return True

    def request_cancel(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock, self._ipc_lock:
            state = self._read_locked()
            current = state.get("current")
            if not current or current.get("status") not in ACTIVE_STATUSES:
                return None
            if job_id and current.get("job_id") != job_id:
                return None
            current["cancel_requested"] = True
            current["status"] = "cancelling"
            self._write_locked(state)
            return copy.deepcopy(current)

    def clear(self) -> None:
        with self._lock, self._ipc_lock:
            self._write_locked(_empty_state())


class BulkJobHeartbeat:
    """Renew a bulk-job lease and surface remote cancel requests."""

    def __init__(
        self,
        store: BulkJobStore,
        job_id: str,
        owner_instance: str,
        cancel_event: threading.Event,
        interval: float = 2.0,
    ):
        self.store = store
        self.job_id = job_id
        self.owner_instance = owner_instance
        self.cancel_event = cancel_event
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"bulk-heartbeat-{job_id[:8]}"
        )
        self._last_error_log = 0.0

    def start(self) -> "BulkJobHeartbeat":
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.heartbeat(self.job_id, self.owner_instance)
            except Exception as exc:  # noqa: BLE001 - heartbeat không được giết launch
                now = time.time()
                if now - self._last_error_log >= 30.0:
                    self._last_error_log = now
                    log.warning("Bulk heartbeat lỗi: %s: %s", type(exc).__name__, exc)
            else:
                if job is None:
                    return
                if job.get("cancel_requested"):
                    self.cancel_event.set()
            if self._stop.wait(self.interval):
                return
