"""Versioned CDP health registry.

The browser process and the CDP control channel are separate liveness
signals. This registry tracks the channel per profile and stamps every
transition with a launch id, so callbacks from an old launch can never
overwrite the state of a newer one.

All timeouts use a monotonic clock. Persisted/displayed timestamps are UTC
ISO-8601. The registry never touches profile metadata or the filesystem;
the launcher subscribes with ``on_change`` to mirror transitions into the
runtime store.
"""
from __future__ import annotations

import datetime
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_RECOVERY_REQUIRED = "recovery_required"
STATE_STOPPING = "stopping"

REASON_HEARTBEAT_TIMEOUT = "HEARTBEAT_TIMEOUT"
REASON_HEARTBEAT_STALE = "HEARTBEAT_STALE"
REASON_SOCKET_SEND_FAILED = "SOCKET_SEND_FAILED"
REASON_READER_TIMEOUT = "READER_TIMEOUT"
REASON_SOCKET_CLOSED = "SOCKET_CLOSED"
REASON_TARGET_CONFIG_FAILED = "TARGET_CONFIG_FAILED"
REASON_HANDSHAKE_FAILED = "HANDSHAKE_FAILED"
REASON_VERIFICATION_FAILED = "VERIFICATION_FAILED"
REASON_STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
REASON_RUNTIME_PERSIST_FAILED = "RUNTIME_PERSIST_FAILED"
REASON_STOP_FAILED = "STOP_FAILED"
REASON_OWNER_UNKNOWN = "OWNER_UNKNOWN"
REASON_OWNED_BY_OTHER_INSTANCE = "OWNED_BY_OTHER_INSTANCE"
REASON_SERVER_RESTART = "SERVER_RESTART"
REASON_DETACHED_OWNER_EXITED = "DETACHED_OWNER_EXITED"
REASON_LEGACY_OWNERSHIP = "LEGACY_OWNERSHIP"
REASON_USER_STOP = "USER_STOP"

HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_TIMEOUT_SECONDS = 5.0
HEARTBEAT_STALE_AFTER_SECONDS = 20.0
READER_TIMEOUT_SECONDS = 30.0
STARTING_STALE_AFTER_SECONDS = 60.0

_PUBLIC_KEYS = (
    "launch_id",
    "owner_instance",
    "pid",
    "state",
    "reason",
    "verified_at",
    "last_heartbeat_at",
    "changed_at",
    "checked_at",
    "restart_required",
)


class CdpSession:
    """Handle for one CDP websocket session owned by a launch."""

    __slots__ = (
        "launch_id", "closed", "thread", "_close_lock", "_browser_close",
        "_close_pending", "_close_requested",
    )

    def __init__(self, launch_id: str):
        self.launch_id = launch_id
        self.closed = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._close_lock = threading.Lock()
        self._browser_close: Optional[Callable[[], Optional[bool]]] = None
        self._close_pending = False
        self._close_requested = False

    def mark_closed(self) -> None:
        with self._close_lock:
            self.closed.set()
            self._browser_close = None

    def bind_browser_close(self, handler: Callable[[], Optional[bool]]) -> bool:
        """Bind this socket's nonblocking close scheduler once.

        The handler must enqueue work on the already-owned websocket's loop;
        it must not open a connection or wait for browser shutdown. None/True
        means accepted, False or an exception means unavailable. The queued
        work must recheck this session/generation before sending Browser.close.
        """
        if not callable(handler):
            raise TypeError("Browser close handler must be callable")
        with self._close_lock:
            if self.closed.is_set():
                return False
            if self._browser_close is not None:
                return self._browser_close is handler
            self._browser_close = handler
            return True

    def request_browser_close(self) -> bool:
        """Schedule one graceful close; success is not proof of process exit."""
        with self._close_lock:
            if self.closed.is_set() or self._browser_close is None:
                return False
            if self._close_requested:
                return True
            if self._close_pending:
                return False
            handler = self._browser_close
            self._close_pending = True
        # Never run a callback while holding either registry or session locks.
        # It may schedule into an event loop or inspect the registry itself.
        try:
            accepted = handler() is not False
        except Exception:
            accepted = False
        with self._close_lock:
            self._close_pending = False
            if self.closed.is_set():
                return False
            self._close_requested = accepted
            return accepted

    def is_open(self) -> bool:
        return not self.closed.is_set()


class CdpUnavailable(RuntimeError):
    """Action needs a healthy CDP channel but the profile is not healthy."""

    def __init__(self, code: str, message: str, health: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.health = dict(health or {})


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class CdpHealthRegistry:
    def __init__(
        self,
        on_change: Optional[Callable[[str, str, Dict[str, Any], str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._lock = threading.RLock()
        self._records: Dict[str, Dict[str, Any]] = {}
        self._sessions: Dict[str, CdpSession] = {}
        self._on_change = on_change
        self._clock = clock

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _record(self, profile_id: str) -> Optional[Dict[str, Any]]:
        record = self._records.get(str(profile_id))
        return dict(record) if record else None

    def _emit(self, profile_id: str, launch_id: str, event: str) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            snapshot = self.snapshot(profile_id)
            callback(profile_id, launch_id, snapshot, event)
        except Exception:
            # Health callbacks are observational; a failing listener must
            # never break the CDP thread or the state machine.
            pass

    def _new_record(
        self,
        launch_id: str,
        owner_instance: str,
        pid: Optional[int],
        state: str,
        reason: Optional[str],
        restart_required: bool,
        now: float,
        now_iso: str,
    ) -> Dict[str, Any]:
        return {
            "launch_id": launch_id,
            "owner_instance": owner_instance,
            "pid": pid,
            "state": state,
            "reason": reason,
            "verified_at": None,
            "last_heartbeat_at": None,
            "changed_at": now_iso,
            "restart_required": bool(restart_required),
            "changed_monotonic": now,
            "verified_monotonic": None,
            "heartbeat_monotonic": None,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def begin_launch(self, profile_id: str, owner_instance: str) -> str:
        """Start a new generation and invalidate any older session."""
        launch_id = uuid.uuid4().hex
        now = self._clock()
        now_iso = _utc_now_iso()
        with self._lock:
            old_session = self._sessions.pop(str(profile_id), None)
            if old_session is not None:
                old_session.mark_closed()
            self._records[str(profile_id)] = self._new_record(
                launch_id,
                owner_instance,
                None,
                STATE_STARTING,
                None,
                False,
                now,
                now_iso,
            )
        self._emit(str(profile_id), launch_id, "begin")
        return launch_id

    def set_process(self, profile_id: str, launch_id: str, pid: Optional[int]) -> bool:
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record or record.get("launch_id") != launch_id:
                return False
            record["pid"] = pid
            return True

    def bind_session(self, profile_id: str, launch_id: str) -> Optional[CdpSession]:
        """Attach the live websocket session to the current launch."""
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record or record.get("launch_id") != launch_id:
                return None
            session = CdpSession(launch_id)
            old_session = self._sessions.get(str(profile_id))
            if old_session is not None:
                old_session.mark_closed()
            self._sessions[str(profile_id)] = session
            return session

    def mark_healthy(
        self, profile_id: str, launch_id: str, *, require_session: bool = True
    ) -> bool:
        """Commit verified healthy state for the current launch only."""
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record or record.get("launch_id") != launch_id:
                return False
            if record.get("state") in (STATE_STOPPING, STATE_STOPPED):
                return False
            session = self._sessions.get(str(profile_id))
            if require_session and (session is None or not session.is_open()):
                return False
            now = self._clock()
            now_iso = _utc_now_iso()
            record["state"] = STATE_HEALTHY
            record["reason"] = None
            record["restart_required"] = False
            record["verified_at"] = now_iso
            record["last_heartbeat_at"] = now_iso
            record["changed_at"] = now_iso
            record["changed_monotonic"] = now
            record["verified_monotonic"] = now
            record["heartbeat_monotonic"] = now
        self._emit(str(profile_id), launch_id, "healthy")
        return True

    def mark_heartbeat(self, profile_id: str, launch_id: str) -> bool:
        """Record a successful browser-level heartbeat."""
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record or record.get("launch_id") != launch_id:
                return False
            if record.get("state") not in (STATE_STARTING, STATE_HEALTHY):
                return False
            now = self._clock()
            record["last_heartbeat_at"] = _utc_now_iso()
            record["heartbeat_monotonic"] = now
        self._emit(str(profile_id), launch_id, "heartbeat")
        return True

    def mark_degraded(
        self,
        profile_id: str,
        launch_id: Optional[str],
        reason: str,
        *,
        restart_required: bool = True,
    ) -> bool:
        """Downgrade the current launch unless it is stopping or superseded."""
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record:
                return False
            if launch_id is not None and record.get("launch_id") != launch_id:
                return False
            state = record.get("state")
            if state in (STATE_STOPPED, STATE_RECOVERY_REQUIRED):
                return False
            if state == STATE_STOPPING and reason != REASON_STOP_FAILED:
                return False
            if state == STATE_DEGRADED and record.get("reason") == reason:
                return False
            now = self._clock()
            now_iso = _utc_now_iso()
            record["state"] = STATE_DEGRADED
            record["reason"] = reason
            record["restart_required"] = bool(restart_required)
            record["changed_at"] = now_iso
            record["changed_monotonic"] = now
            current_launch = record["launch_id"]
        self._emit(str(profile_id), current_launch, "degraded")
        return True

    def mark_recovery_required(
        self,
        profile_id: str,
        reason: str,
        *,
        owner_instance: str,
        pid: Optional[int] = None,
        launch_id: Optional[str] = None,
        restart_required: bool = True,
        force: bool = False,
    ) -> bool:
        """Declare that this process does not own a usable CDP channel.

        Without ``force`` a live, healthy, current session is preserved.
        """
        with self._lock:
            record = self._records.get(str(profile_id))
            current_launch = (record or {}).get("launch_id") or launch_id or uuid.uuid4().hex
            if launch_id is not None and record and record.get("launch_id") != launch_id:
                return False
            if not force and record:
                session = self._sessions.get(str(profile_id))
                if (
                    record.get("state") == STATE_HEALTHY
                    and record.get("owner_instance") == owner_instance
                    and session is not None
                    and session.is_open()
                ):
                    return False
            if record and record.get("state") == STATE_RECOVERY_REQUIRED:
                if (
                    record.get("reason") == reason
                    and record.get("launch_id") == current_launch
                    and record.get("owner_instance") == owner_instance
                ):
                    return False
            now = self._clock()
            now_iso = _utc_now_iso()
            session = self._sessions.pop(str(profile_id), None)
            if session is not None:
                session.mark_closed()
            self._records[str(profile_id)] = self._new_record(
                current_launch,
                owner_instance,
                pid if pid is not None else (record or {}).get("pid"),
                STATE_RECOVERY_REQUIRED,
                reason,
                restart_required,
                now,
                now_iso,
            )
        self._emit(str(profile_id), current_launch, "recovery_required")
        return True

    def mark_stopping(self, profile_id: str) -> bool:
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record:
                return False
            if record.get("state") in (STATE_STOPPING, STATE_STOPPED):
                return True
            now = self._clock()
            now_iso = _utc_now_iso()
            record["state"] = STATE_STOPPING
            record["reason"] = REASON_USER_STOP
            record["restart_required"] = False
            record["changed_at"] = now_iso
            record["changed_monotonic"] = now
            launch_id = record["launch_id"]
        self._emit(str(profile_id), launch_id, "stopping")
        return True

    def mark_stopped(self, profile_id: str, launch_id: Optional[str] = None) -> bool:
        with self._lock:
            record = self._records.get(str(profile_id))
            if launch_id is not None and record and record.get("launch_id") != launch_id:
                return False
            session = self._sessions.pop(str(profile_id), None)
            if session is not None:
                session.mark_closed()
            current_launch = (record or {}).get("launch_id") or uuid.uuid4().hex
            now = self._clock()
            now_iso = _utc_now_iso()
            self._records[str(profile_id)] = self._new_record(
                current_launch,
                (record or {}).get("owner_instance") or "",
                (record or {}).get("pid"),
                STATE_STOPPED,
                None,
                False,
                now,
                now_iso,
            )
        self._emit(str(profile_id), current_launch, "stopped")
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def current_launch_id(self, profile_id: str) -> Optional[str]:
        with self._lock:
            record = self._records.get(str(profile_id))
            return record.get("launch_id") if record else None

    def owns(self, profile_id: str, launch_id: str) -> bool:
        with self._lock:
            record = self._records.get(str(profile_id))
            return bool(record and record.get("launch_id") == launch_id)

    def session_alive(self, profile_id: str, launch_id: str) -> bool:
        with self._lock:
            record = self._records.get(str(profile_id))
            if not record or record.get("launch_id") != launch_id:
                return False
            session = self._sessions.get(str(profile_id))
            return bool(session and session.is_open())

    def request_browser_close(self, profile_id: str, launch_id: str) -> bool:
        """Ask only the exact current socket to enqueue graceful browser close.

        STOPPING remains eligible: this request is part of shutdown, unlike
        ordinary healthy-channel actions. A captured old session is never
        replaced with a newer session or rediscovered through a debugging port.
        """
        with self._lock:
            record = self._records.get(str(profile_id))
            if (not record or record.get("launch_id") != launch_id
                    or record.get("state") in (STATE_STOPPED, STATE_RECOVERY_REQUIRED)):
                return False
            session = self._sessions.get(str(profile_id))
            if session is None or session.launch_id != launch_id or not session.is_open():
                return False
        return session.request_browser_close()

    def is_stopping(self, profile_id: str) -> bool:
        with self._lock:
            record = self._records.get(str(profile_id))
            return bool(record and record.get("state") == STATE_STOPPING)

    def forget_if_launch(self, profile_id: str, launch_id: Optional[str]) -> bool:
        """Drop only a stale local generation before observing another owner."""
        with self._lock:
            key = str(profile_id)
            record = self._records.get(key)
            if record and record.get("launch_id") != launch_id:
                return False
            session = self._sessions.pop(key, None)
            if session is not None:
                session.mark_closed()
            self._records.pop(key, None)
            return True

    def snapshot(self, profile_id: str) -> Dict[str, Any]:
        """Public snapshot with an effective state (heartbeat freshness)."""
        with self._lock:
            record = self._records.get(str(profile_id))
            checked_at = _utc_now_iso()
            if not record:
                return {
                    "launch_id": None,
                    "owner_instance": None,
                    "pid": None,
                    "state": STATE_STOPPED,
                    "reason": None,
                    "verified_at": None,
                    "last_heartbeat_at": None,
                    "changed_at": None,
                    "checked_at": checked_at,
                    "restart_required": False,
                }
            state = record.get("state")
            reason = record.get("reason")
            restart_required = bool(record.get("restart_required"))
            now = self._clock()
            if state == STATE_HEALTHY:
                heartbeat = record.get("heartbeat_monotonic")
                if heartbeat is None:
                    heartbeat = record.get("changed_monotonic")
                if heartbeat is None or now - heartbeat > HEARTBEAT_STALE_AFTER_SECONDS:
                    state = STATE_DEGRADED
                    reason = REASON_HEARTBEAT_STALE
                    restart_required = True
            elif state == STATE_STARTING:
                changed = record.get("changed_monotonic")
                if changed is None or now - changed > STARTING_STALE_AFTER_SECONDS:
                    state = STATE_DEGRADED
                    reason = REASON_STARTUP_TIMEOUT
                    restart_required = True
            snapshot = {key: record.get(key) for key in _PUBLIC_KEYS}
            snapshot["state"] = state
            snapshot["reason"] = reason
            snapshot["restart_required"] = restart_required
            snapshot["checked_at"] = checked_at
            return snapshot

    def clear(self) -> None:
        """Test/shutdown helper: detach sessions and forget all state."""
        with self._lock:
            for session in self._sessions.values():
                session.mark_closed()
            self._sessions.clear()
            self._records.clear()
