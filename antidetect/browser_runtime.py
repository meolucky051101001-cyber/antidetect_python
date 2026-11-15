"""Atomic persistence for browser launch ownership across server restarts.

Profiles.json deliberately does not store transient runtime fields (status,
pid, port). This store is the source of truth for reconnecting to or stopping
browsers after the FastAPI process restarts.
"""
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .storage import InterProcessLock


class BrowserRuntimeStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        # M1: server và cli cùng giữ một BrowserRuntimeStore; upsert/remove
        # từng process ghi nguyên snapshot in-memory của mình sẽ xóa record
        # process kia vừa ghi. File lock riêng + re-load trước khi mutate.
        self._ipc_lock = InterProcessLock(path + ".lock", timeout=5.0)
        self._states: Dict[str, Dict[str, Any]] = {}
        self._disk_signature: Optional[tuple] = None
        self._load()

    def _stat_signature(self) -> Optional[tuple]:
        try:
            stat = os.stat(self.path)
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        with self._lock:
            self._disk_signature = self._stat_signature()
            if self._disk_signature is None:
                self._states = {}
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("runtime root must be an object")
                cleaned = {}
                for profile_id, record in data.items():
                    if not isinstance(profile_id, str) or not isinstance(record, dict):
                        continue
                    try:
                        pid = int(record.get("pid") or 0)
                    except (TypeError, ValueError):
                        pid = 0
                    if pid <= 0:
                        continue
                    cleaned[profile_id] = dict(record)
                self._states = cleaned
            except Exception:
                # A corrupt runtime file must never prevent the app from
                # starting. It only loses recovery metadata, not profile data.
                self._states = {}

    def _refresh(self) -> None:
        """Pick up records written by another process (server <-> CLI).

        Long-lived processes previously kept the snapshot from startup, so a
        browser launched by the CLI stayed invisible to the dashboard and
        stop_all skipped it.
        """
        with self._lock:
            if self._stat_signature() != self._disk_signature:
                self._load()

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temp = f"{self.path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self._states, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
            self._disk_signature = self._stat_signature()
        finally:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            self._refresh()
            return {k: dict(v) for k, v in self._states.items()}

    def get(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._refresh()
            record = self._states.get(str(profile_id))
            return dict(record) if record else None

    def upsert(self, profile_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock, self._ipc_lock:
            self._load()  # Merge với record process khác vừa ghi xuống đĩa.
            record = dict(self._states.get(str(profile_id)) or {})
            record.update(fields)
            record["profile_id"] = str(profile_id)
            record["updated_at"] = int(time.time())
            self._states[str(profile_id)] = record
            self._write()
            return dict(record)

    def upsert_if_owner(
        self,
        profile_id: str,
        *,
        launch_id: Optional[str] = None,
        pid: Optional[int] = None,
        owner_instance: Optional[str] = None,
        **fields: Any,
    ) -> Optional[Dict[str, Any]]:
        """Update a record only if it still belongs to the expected launch.

        A late callback from an old CDP session must not overwrite the record
        of a newer launch. Returns None when the ownership precondition fails
        or the record no longer exists.
        """
        if launch_id is None and pid is None:
            raise ValueError("upsert_if_owner requires launch_id and/or pid")
        with self._lock, self._ipc_lock:
            self._load()
            key = str(profile_id)
            record = self._states.get(key)
            if not record:
                return None
            if launch_id is not None and str(record.get("launch_id") or "") != str(
                launch_id
            ):
                return None
            if pid is not None:
                try:
                    actual_pid = int(record.get("pid") or 0)
                except (TypeError, ValueError):
                    actual_pid = 0
                if actual_pid != int(pid):
                    return None
            if owner_instance is not None and str(
                record.get("owner_instance") or ""
            ) != str(owner_instance):
                return None
            merged = dict(record)
            merged.update(fields)
            merged["profile_id"] = key
            merged["updated_at"] = int(time.time())
            self._states[key] = merged
            self._write()
            return dict(merged)

    def remove(self, profile_id: str) -> None:
        with self._lock, self._ipc_lock:
            self._load()  # Không xóa record process khác đã ghi thêm.
            if str(profile_id) in self._states:
                self._states.pop(str(profile_id), None)
                self._write()

    def remove_if_matches(self, profile_id: str, expected: Dict[str, Any]) -> bool:
        """Remove only the runtime identity whose process was checked by caller."""
        with self._lock, self._ipc_lock:
            self._load()
            key = str(profile_id)
            current = self._states.get(key)
            if not current:
                return True
            identity_fields = ("launch_id", "owner_instance", "pid", "created", "user_data_dir")
            if not expected or any(current.get(field) != expected.get(field) for field in identity_fields):
                return False
            self._states.pop(key)
            self._write()
            return True
