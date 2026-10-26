"""Process-local counters/timings for operational visibility (no PII)."""
import threading
import time
from typing import Any, Dict

_LOCK = threading.Lock()
_STARTED_AT = time.time()
_COUNTERS: Dict[str, float] = {}
_TIMINGS: Dict[str, Dict[str, float]] = {}


def inc(name: str, amount: float = 1.0) -> None:
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0.0) + float(amount)


def observe_seconds(name: str, seconds: float) -> None:
    value = max(0.0, float(seconds))
    with _LOCK:
        entry = _TIMINGS.setdefault(name, {"count": 0.0, "total_seconds": 0.0})
        entry["count"] += 1.0
        entry["total_seconds"] += value


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        counters = {key: int(value) for key, value in _COUNTERS.items()}
        timings = {}
        for key, entry in _TIMINGS.items():
            count = entry["count"]
            total = entry["total_seconds"]
            timings[key] = {
                "count": int(count),
                "total_seconds": round(total, 3),
                "avg_seconds": round(total / count, 3) if count else None,
            }
    return {
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "counters": counters,
        "timings": timings,
    }


def reset() -> None:
    """Test helper: clear counters but keep process uptime."""
    with _LOCK:
        _COUNTERS.clear()
        _TIMINGS.clear()
