"""Per-profile lifecycle locks dùng chung giữa các process (QĐ-6).

File lock nằm tại data/locks/<profile_id>.lock; reentrant theo thread trong
cùng process. Module không import storage để tránh vòng import.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Iterator, List, Optional, Sequence

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VALID_PROFILE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")
_POLL_INTERVAL = 0.02
log = logging.getLogger(__name__)


class ProfileLockError(RuntimeError):
    """Lỗi lifecycle lock cơ bản."""


class ProfileLockTimeout(ProfileLockError, TimeoutError):
    """Không lấy được lifecycle lock trong thời gian cho phép."""

    def __init__(self, profile_id: str, timeout: Optional[float]):
        self.profile_id = profile_id
        self.timeout = timeout
        if timeout is None:
            message = f"Không lấy được lifecycle lock cho profile {profile_id}"
        else:
            message = (
                f"Không lấy được lifecycle lock cho profile {profile_id} "
                f"sau {timeout:.2f}s"
            )
        super().__init__(message)


class _ProfileLockState:
    __slots__ = ("thread_lock", "fd", "owner", "depth")

    def __init__(self):
        self.thread_lock = threading.RLock()
        self.fd: Optional[int] = None
        self.owner: Optional[int] = None
        self.depth = 0


_REGISTRY_GUARD = threading.Lock()
_REGISTRY: dict = {}


def _state_for(lock_path: str) -> _ProfileLockState:
    key = os.path.normcase(os.path.realpath(lock_path))
    with _REGISTRY_GUARD:
        state = _REGISTRY.get(key)
        if state is None:
            state = _ProfileLockState()
            _REGISTRY[key] = state
        return state


def _try_lock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        log.debug("Failed to release profile file lock", exc_info=True)


class _HeldProfileLock:
    __slots__ = ("profile_id", "_state", "_released")

    def __init__(self, profile_id: str, state: _ProfileLockState):
        self.profile_id = profile_id
        self._state = state
        self._released = False

    def __enter__(self) -> "_HeldProfileLock":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.release()
        return False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        state = self._state
        if state.depth <= 0:
            return
        state.depth -= 1
        if state.depth == 0:
            fd = state.fd
            state.fd = None
            state.owner = None
            if fd is not None:
                _unlock_fd(fd)
                try:
                    os.close(fd)
                except OSError:
                    log.debug("Failed to close profile lock file", exc_info=True)
        state.thread_lock.release()


class ProfileLocks:
    def __init__(
        self,
        data_dir: Optional[str] = None,
        default_timeout: float = 2.0,
        *,
        suffix: str = "",
    ):
        resolved = (
            data_dir
            or os.environ.get("ANTIDETECT_DATA_DIR")
            or os.path.join(BASE_DIR, "data")
        )
        self.data_dir = os.path.abspath(resolved)
        self.lock_dir = os.path.join(self.data_dir, "locks")
        self.default_timeout = default_timeout
        # QĐ-6: lifecycle mặc định dùng "<id>.lock"; scan dùng suffix riêng
        # (".scan") để không chặn stop/delete trong lúc quét.
        self.suffix = suffix

    @staticmethod
    def _validate_profile_id(profile_id: str) -> str:
        if not isinstance(profile_id, str) or not _VALID_PROFILE_ID.match(profile_id):
            raise ValueError(f"Profile ID không hợp lệ: {profile_id!r}")
        return profile_id

    def path_for(self, profile_id: str) -> str:
        self._validate_profile_id(profile_id)
        return os.path.join(self.lock_dir, f"{profile_id}{self.suffix}.lock")

    def acquire(
        self, profile_id: str, timeout: Optional[float] = None
    ) -> _HeldProfileLock:
        wait = self.default_timeout if timeout is None else timeout
        if wait < 0:
            raise ValueError("timeout không được âm")
        return self._acquire(profile_id, wait)

    def _acquire(self, profile_id: str, timeout: float) -> _HeldProfileLock:
        lock_path = self.path_for(profile_id)
        os.makedirs(self.lock_dir, exist_ok=True)
        state = _state_for(lock_path)
        deadline = time.monotonic() + timeout

        remaining = max(0.0, deadline - time.monotonic())
        if not state.thread_lock.acquire(timeout=remaining):
            raise ProfileLockTimeout(profile_id, timeout)

        try:
            if state.depth > 0:
                state.depth += 1
                return _HeldProfileLock(profile_id, state)

            last_error: Optional[OSError] = None
            while True:
                fd: Optional[int] = None
                try:
                    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                    _try_lock_fd(fd)
                    state.fd = fd
                    state.owner = threading.get_ident()
                    state.depth = 1
                    return _HeldProfileLock(profile_id, state)
                except OSError as exc:
                    last_error = exc
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            log.debug("Failed to close profile lock after acquisition failure", exc_info=True)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProfileLockTimeout(profile_id, timeout) from last_error
                    time.sleep(min(_POLL_INTERVAL, remaining))
        except BaseException:
            state.thread_lock.release()
            raise

    @contextmanager
    def acquire_many(
        self, profile_ids: Sequence[str], timeout: Optional[float] = None
    ) -> Iterator[List[str]]:
        wait = self.default_timeout if timeout is None else timeout
        if wait < 0:
            raise ValueError("timeout không được âm")
        ordered = sorted({self._validate_profile_id(pid) for pid in profile_ids})
        deadline = time.monotonic() + wait
        with ExitStack() as stack:
            for profile_id in ordered:
                remaining = max(0.0, deadline - time.monotonic())
                stack.enter_context(self._acquire(profile_id, remaining))
            yield ordered
