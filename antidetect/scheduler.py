"""Lịch tự động cho farm profile (Giai đoạn C).

Rules lưu `data/schedules.json` (atomic + fsync, IPC lock riêng):
    {"id": str, "name": str, "time": "HH:MM", "action": "start_all"|"stop_all"|"sync_location_proxy",
     "profile_ids": [str] (rỗng = tất cả), "enabled": bool, "last_run_date": "YYYY-MM-DD"|null}

Runner là daemon thread dậy mỗi 30 giây: rule đến giờ được claim bền vững
trước khi gọi callback. Claim bỏ dở sau crash sẽ được tiếp quản khi hết lease.
Catch-up: rule đúng giờ trong 60 phút trước mà chưa chạy hôm nay → chạy lại.
"""
import copy
import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .storage import InterProcessLock, _replace_with_retry

VALID_ACTIONS = ("start_all", "stop_all", "sync_location_proxy")
MAX_RULES = 20


class ScheduleStoreCorruption(RuntimeError):
    pass


def _due_reason(rule: Dict[str, Any], now: datetime,
                catchup_minutes: int) -> Optional[str]:
    if not rule.get("enabled"):
        return None
    today = now.strftime("%Y-%m-%d")
    if rule.get("last_run_date") == today:
        return None
    try:
        hh, mm = (int(x) for x in str(rule.get("time", "")).split(":"))
        rule_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (TypeError, ValueError):
        return None
    if now < rule_dt:
        return None
    if (now - rule_dt).total_seconds() > catchup_minutes * 60:
        return None
    return "due" if rule.get("last_run_date") is None else "catchup"


class ScheduleStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._ipc_lock = InterProcessLock(f"{path}.lock")
        self._rules: List[Dict[str, Any]] = []
        self._load()

    def _read_rules(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ScheduleStoreCorruption(
                f"File lịch không hợp lệ: {self.path}"
            ) from exc
        rules = data.get("rules") if isinstance(data, dict) else data
        rules_ok = isinstance(rules, list) and not any(
            not isinstance(rule, dict) or not rule.get("id") for rule in rules
        )
        if not rules_ok:
            raise ScheduleStoreCorruption(
                f"Cấu trúc file lịch không hợp lệ: {self.path}"
            )
        return rules

    def _load(self) -> None:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()

    def _save_locked(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rules": self._rules}, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, self.path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _public_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(rule)
        result.pop("_run_claim", None)
        return result

    def list(self) -> List[Dict[str, Any]]:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
            return [self._public_rule(rule) for rule in self._rules]

    def add(
        self, name: str, rule_time: str, action: str,
        profile_ids: Optional[List[str]] = None, enabled: bool = True,
    ) -> Dict[str, Any]:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
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
            self._save_locked()
            return self._public_rule(rule)

    def update(self, rule_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
            for index, rule in enumerate(self._rules):
                if rule.get("id") != rule_id:
                    continue
                updated = copy.deepcopy(rule)
                if "enabled" in fields:
                    updated["enabled"] = bool(fields["enabled"])
                if "time" in fields:
                    if not self._valid_time(fields["time"]):
                        raise ValueError(f"Giờ không hợp lệ: {fields['time']!r}")
                    updated["time"] = fields["time"]
                if "action" in fields:
                    if fields["action"] not in VALID_ACTIONS:
                        raise ValueError("action không hợp lệ")
                    updated["action"] = fields["action"]
                if "profile_ids" in fields:
                    updated["profile_ids"] = list(fields["profile_ids"] or [])
                if "name" in fields:
                    updated["name"] = str(fields["name"] or updated["name"])
                self._rules[index] = updated
                self._save_locked()
                return self._public_rule(updated)
            return None

    def delete(self, rule_id: str) -> bool:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
            remaining = [rule for rule in self._rules if rule.get("id") != rule_id]
            if len(remaining) == len(self._rules):
                return False
            self._rules = remaining
            self._save_locked()
            return True

    def claim_due(
        self, rule_id: str, now: datetime, catchup_minutes: int,
        token: str, lease_seconds: float = 600.0,
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
            for rule in self._rules:
                if rule.get("id") != rule_id:
                    continue
                reason = _due_reason(rule, now, catchup_minutes)
                if not reason:
                    return None
                claim = rule.get("_run_claim")
                if isinstance(claim, dict):
                    try:
                        claimed_at = float(claim.get("at"))
                        if now.timestamp() < claimed_at + lease_seconds:
                            return None
                    except (TypeError, ValueError):
                        pass
                rule["_run_claim"] = {
                    "token": token,
                    "at": now.timestamp(),
                    "date": now.strftime("%Y-%m-%d"),
                }
                self._save_locked()
                return self._public_rule(rule), reason
            return None

    def complete_claim(self, rule_id: str, token: str, today: str) -> bool:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
            for rule in self._rules:
                claim = rule.get("_run_claim")
                if rule.get("id") != rule_id or not isinstance(claim, dict):
                    continue
                if claim.get("token") != token:
                    return False
                rule["last_run_date"] = today
                rule.pop("_run_claim", None)
                self._save_locked()
                return True
            return False

    def release_claim(self, rule_id: str, token: str) -> bool:
        with self._lock, self._ipc_lock:
            self._rules = self._read_rules()
            for rule in self._rules:
                claim = rule.get("_run_claim")
                if rule.get("id") != rule_id or not isinstance(claim, dict):
                    continue
                if claim.get("token") != token:
                    return False
                rule.pop("_run_claim", None)
                self._save_locked()
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

    def __init__(
        self, store: ScheduleStore, callbacks: Dict[str, Callable],
        check_interval: float = 30.0, catchup_minutes: int = 60,
        claim_lease_seconds: float = 600.0,
    ):
        super().__init__(daemon=True, name="antidetect-scheduler")
        self.store = store
        self.callbacks = callbacks  # action -> callable(profile_ids: Optional[list])
        self.check_interval = check_interval
        self.catchup_minutes = catchup_minutes
        self.claim_lease_seconds = claim_lease_seconds
        self._stop_event = threading.Event()
        self.last_run_info: Dict[str, Any] = {"last_tick": None, "ran": []}

    def stop(self) -> None:
        self._stop_event.set()

    def _due(self, rule: Dict[str, Any], now: datetime) -> Optional[str]:
        """Trả lý do chạy ('due' | 'catchup') hoặc None."""
        return _due_reason(rule, now, self.catchup_minutes)

    def _record(self, event: Dict[str, Any]) -> None:
        self.last_run_info["ran"].append(event)
        if len(self.last_run_info["ran"]) > 50:
            self.last_run_info["ran"] = self.last_run_info["ran"][-50:]

    def run(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            now = datetime.now()
            tick_at = now.isoformat(timespec="seconds")
            self.last_run_info["last_tick"] = tick_at
            try:
                rules = self.store.list()
            except Exception as exc:  # noqa: BLE001 - runner không được chết
                self._record({"rule": None, "action": None, "at": tick_at,
                              "error": f"store: {str(exc)[:200]}"})
                continue
            for rule in rules:
                if not self._due(rule, now):
                    continue
                if self.callbacks.get(rule.get("action")) is None:
                    continue
                token = uuid.uuid4().hex
                try:
                    claimed = self.store.claim_due(
                        rule["id"], now, self.catchup_minutes, token,
                        lease_seconds=self.claim_lease_seconds,
                    )
                except Exception as exc:  # noqa: BLE001 - runner không được chết
                    self._record({
                        "rule": rule.get("name"), "action": rule.get("action"),
                        "at": tick_at, "error": f"claim: {str(exc)[:200]}",
                    })
                    continue
                if claimed is None:
                    continue
                claimed_rule, reason = claimed
                action = claimed_rule.get("action")
                cb = self.callbacks.get(action)
                if cb is None:
                    try:
                        self.store.release_claim(rule["id"], token)
                    except Exception as exc:  # noqa: BLE001
                        self._record({
                            "rule": claimed_rule.get("name"), "action": action,
                            "at": tick_at, "error": f"release: {str(exc)[:200]}",
                        })
                    continue
                ids = claimed_rule.get("profile_ids") or None
                today = now.strftime("%Y-%m-%d")
                try:
                    cb(ids)
                except Exception as exc:  # noqa: BLE001 - callback được retry
                    error = str(exc)[:200]
                    try:
                        self.store.release_claim(rule["id"], token)
                    except Exception as release_exc:  # noqa: BLE001
                        error = f"{error}; release: {str(release_exc)[:100]}"
                    self._record({
                        "rule": claimed_rule.get("name"), "action": action,
                        "at": tick_at, "error": error,
                    })
                    continue
                try:
                    completed = self.store.complete_claim(rule["id"], token, today)
                except Exception as exc:  # noqa: BLE001 - lease cho phép retry
                    self._record({
                        "rule": claimed_rule.get("name"), "action": action,
                        "at": tick_at, "error": f"complete: {str(exc)[:200]}",
                    })
                    continue
                if not completed:
                    self._record({
                        "rule": claimed_rule.get("name"), "action": action,
                        "at": tick_at, "error": "complete: claim không còn hợp lệ",
                    })
                    continue
                self._record({
                    "rule": claimed_rule.get("name"), "action": action,
                    "at": tick_at, "reason": reason,
                })
