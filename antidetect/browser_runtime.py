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
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                if not os.path.exists(self.path):
                    self._states = {}
                    return
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
        finally:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._states.items()}

    def get(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
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

    def remove(self, profile_id: str) -> None:
        with self._lock, self._ipc_lock:
            self._load()  # Không xóa record process khác đã ghi thêm.
            if str(profile_id) in self._states:
                self._states.pop(str(profile_id), None)
                self._write()
