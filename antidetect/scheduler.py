"""Lịch tự động cho farm profile (Giai đoạn C).

Rules lưu `data/schedules.json` (atomic write, IPC lock chung storage):
    {"id": str, "name": str, "time": "HH:MM", "action": "start_all"|"stop_all"|"sync_location_proxy",
     "profile_ids": [str] (rỗng = tất cả), "enabled": bool, "last_run_date": "YYYY-MM-DD"|null}

Runner là daemon thread dậy mỗi 30 giây: rule đến giờ (và chưa chạy hôm nay)
→ thực thi qua callbacks được app inject. Catch-up: khi server khởi động,
rule đúng giờ trong vòng 60 phút trước mà chưa chạy hôm nay → chạy 1 lần.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .storage import InterProcessLock

VALID_ACTIONS = ("start_all", "stop_all", "sync_location_proxy")
MAX_RULES = 20


class ScheduleStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._rules: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                rules = data.get("rules") if isinstance(data, dict) else data
                if isinstance(rules, list):
                    self._rules = [r for r in rules if isinstance(r, dict) and r.get("id")]
                    return
            except Exception:
                pass
            self._rules = []

    def _save(self) -> None:
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rules": self._rules}, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._rules]

    def add(self, name: str, rule_time: str, action: str,
            profile_ids: Optional[List[str]] = None, enabled: bool = True) -> Dict[str, Any]:
        with self._lock:
            if len(self._rules) >= MAX_RULES:
                raise ValueError(f"Tối đa {MAX_RULES} lịch.")
            if action not in VALID_ACTIONS:
                raise ValueError(f"Hành động không hợp lệ: {action}")
            if not self._valid_time(rule_time):
                raise ValueError(f"Giờ không hợp lệ (HH:MM): {rule_time!r}")
            rule = {
                "id": str(uuid.uuid4()),
                "name": (name or "").strip() or f"Lịch {rule_time}",
                "time": rule_time,
                "action": action,
                "profile_ids": list(profile_ids or []),
                "enabled": bool(enabled),
                "last_run_date": None,
            }
            self._rules.append(rule)
            self._save()
            return dict(rule)

    def update(self, rule_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            for r in self._rules:
                if r.get("id") != rule_id:
                    continue
                if "enabled" in fields:
                    r["enabled"] = bool(fields["enabled"])
                if "time" in fields:
                    if not self._valid_time(fields["time"]):
                        raise ValueError(f"Giờ không hợp lệ: {fields['time']!r}")
                    r["time"] = fields["time"]
                if "action" in fields:
                    if fields["action"] not in VALID_ACTIONS:
                        raise ValueError("action không hợp lệ")
                    r["action"] = fields["action"]
                if "profile_ids" in fields:
                    r["profile_ids"] = list(fields["profile_ids"] or [])
                if "name" in fields:
                    r["name"] = str(fields["name"] or r["name"])
                self._save()
                return dict(r)
            return None

    def delete(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.get("id") != rule_id]
            if len(self._rules) != before:
                self._save()
                return True
            return False

    @staticmethod
    def _valid_time(t: str) -> bool:
        try:
            hh, mm = str(t).split(":")
            return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
        except Exception:
            return False


class Scheduler(threading.Thread):
    """Daemon runner — callbacks do app inject để tránh import vòng."""

    def __init__(self, store: ScheduleStore, callbacks: Dict[str, Callable],
                 check_interval: float = 30.0, catchup_minutes: int = 60):
        super().__init__(daemon=True, name="antidetect-scheduler")
        self.store = store
        self.callbacks = callbacks  # action -> callable(profile_ids: Optional[list])
        self.check_interval = check_interval
        self.catchup_minutes = catchup_minutes
        self._stop_event = threading.Event()
        self.last_run_info: Dict[str, Any] = {"last_tick": None, "ran": []}

    def stop(self) -> None:
        self._stop_event.set()

    def _due(self, rule: Dict[str, Any], now: datetime) -> Optional[str]:
        """Trả lý do chạy ('due' | 'catchup') hoặc None."""
        if not rule.get("enabled"):
            return None
        today = now.strftime("%Y-%m-%d")
        if rule.get("last_run_date") == today:
            return None
        try:
            hh, mm = (int(x) for x in str(rule.get("time", "")).split(":"))
        except Exception:
            return None
        rule_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < rule_dt:
            return None
        # Ngoài cửa sổ catch-up: rule mới chờ tới ngày mai đúng giờ, rule cũ
        # lỡ hôm nay cũng bỏ (đánh dấu sẽ chạy ngày mai).
        if (now - rule_dt).total_seconds() > self.catchup_minutes * 60:
            return None
        if rule.get("last_run_date") is None:
            return "due"  # lần đầu chạy, đang trong cửa sổ
        return "catchup"

    def _mark_run(self, rule_id: str, today: str) -> None:
        with self.store._lock:
            for r in self.store._rules:
                if r.get("id") == rule_id:
                    r["last_run_date"] = today
                    self.store._save()
                    break

    def run(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            now = datetime.now()
            self.last_run_info["last_tick"] = now.isoformat(timespec="seconds")
            for rule in self.store.list():
                reason = self._due(rule, now)
                if not reason:
                    continue
                action = rule.get("action")
                cb = self.callbacks.get(action)
                if cb is None:
                    continue
                ids = rule.get("profile_ids") or None
                today = now.strftime("%Y-%m-%d")
                try:
                    cb(ids)
                    self._mark_run(rule["id"], today)
                    self.last_run_info["ran"].append({
                        "rule": rule.get("name"), "action": action,
                        "at": now.isoformat(timespec="seconds"), "reason": reason,
                    })
                    if len(self.last_run_info["ran"]) > 50:
                        self.last_run_info["ran"] = self.last_run_info["ran"][-50:]
                except Exception as exc:  # noqa: BLE001 - runner không được chết
                    # Lỗi action: KHÔNG mark last_run → thử lại ở tick sau
                    # (trong catch-up window) thay vì mất lịch.
                    self.last_run_info["ran"].append({
                        "rule": rule.get("name"), "action": action,
                        "at": now.isoformat(timespec="seconds"), "error": str(exc)[:200],
                    })
