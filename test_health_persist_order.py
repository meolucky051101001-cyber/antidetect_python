"""Delayed same-generation health writes cannot undo synchronous detach."""
from copy import deepcopy
import threading

import pytest

from antidetect.cdp_health import CdpHealthRegistry, STATE_HEALTHY, STATE_RECOVERY_REQUIRED
from antidetect.launcher import BrowserLauncher


class MemoryRuntime:
    def __init__(self, record):
        self.record = dict(record)
        self.lock = threading.Lock()
        self.before_write = lambda fields: None
        self.after_write = lambda fields: None

    def get(self, profile_id):
        with self.lock:
            return deepcopy(self.record)

    def upsert_if_owner(self, profile_id, *, launch_id, pid, owner_instance, **fields):
        self.before_write(fields)
        with self.lock:
            if any(self.record.get(key) != value for key, value in (
                    ("launch_id", launch_id), ("pid", pid), ("owner_instance", owner_instance))):
                return None
            self.record.update(deepcopy(fields))
            result = deepcopy(self.record)
        self.after_write(fields)
        return result


@pytest.fixture
def launcher():
    value = BrowserLauncher.__new__(BrowserLauncher)
    value.instance_id = "synthetic-health-owner"
    value._recovery_required = set()
    value._health_persist_cond = threading.Condition(threading.Lock())
    value._health_persist_write_lock = threading.Lock()
    value._health_persist_pending = {}
    value._health_persist_thread = None
    value._health_persist_stop = False
    value._health_persist_last = {}
    value._health_persist_warned = set()
    value.cdp_health = CdpHealthRegistry()
    generation = value.cdp_health.begin_launch("fixture", value.instance_id)
    value.cdp_health.set_process("fixture", generation, 42)
    value.cdp_health.bind_session("fixture", generation)
    assert value.cdp_health.mark_healthy("fixture", generation)
    value._runtime_store = MemoryRuntime({"launch_id": generation, "pid": 42,
                                          "owner_instance": value.instance_id})
    yield value, generation
    with value._health_persist_cond:
        value._health_persist_stop = True
        value._health_persist_cond.notify_all()
    if value._health_persist_thread is not None:
        value._health_persist_thread.join(timeout=3)
        assert not value._health_persist_thread.is_alive()


def test_dequeued_healthy_snapshot_cannot_overwrite_completed_detach(launcher, monkeypatch):
    value, generation = launcher
    dequeued, release, finished = (threading.Event() for _ in range(3))
    original = value._persist_health_now

    def paused(profile_id, launch_id, snapshot, extra=None):
        older_healthy = snapshot.get("state") == STATE_HEALTHY
        if older_healthy:
            dequeued.set()
            assert release.wait(timeout=3)
        try:
            return original(profile_id, launch_id, snapshot, extra)
        finally:
            if older_healthy:
                finished.set()

    monkeypatch.setattr(value, "_persist_health_now", paused)
    value._queue_health_change("fixture", generation, value.cdp_health.snapshot("fixture"), "healthy")
    try:
        assert dequeued.wait(timeout=3)
        assert value.mark_detached("fixture")["state"] == STATE_RECOVERY_REQUIRED
    finally:
        release.set()
    assert finished.wait(timeout=3)
    record = value._runtime_store.get("fixture")
    assert record["detached"] is True
    assert record["cdp_health"]["state"] == STATE_RECOVERY_REQUIRED


def test_inflight_healthy_write_finishes_before_detach_commit(launcher):
    value, generation = launcher
    old_write, release, rendezvous = (threading.Event() for _ in range(3))
    errors = []

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.attempts = 0

        def __enter__(self):
            self.attempts += 1
            if self.attempts == 2:
                rendezvous.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    value._health_persist_write_lock = ObservedLock()

    def before(fields):
        if fields["cdp_health"]["state"] == STATE_HEALTHY:
            old_write.set()
            assert release.wait(timeout=3)

    def after(fields):
        # On the old code detach can commit while the healthy writer is
        # paused. On the corrected code the second lock attempt is the signal.
        if fields["cdp_health"]["state"] == STATE_RECOVERY_REQUIRED:
            rendezvous.set()

    value._runtime_store.before_write = before
    value._runtime_store.after_write = after

    def capture(action):
        try:
            action()
        except Exception as error:
            errors.append(error)

    stale = value.cdp_health.snapshot("fixture")
    older = threading.Thread(target=capture, args=(lambda: value._persist_health_now("fixture", generation, stale),))
    detached = threading.Thread(target=capture, args=(lambda: value.mark_detached("fixture"),))
    older.start()
    try:
        assert old_write.wait(timeout=3)
        detached.start()
        assert rendezvous.wait(timeout=3)
    finally:
        release.set()
        older.join(timeout=3)
        if detached.ident is not None:
            detached.join(timeout=3)
    assert not errors
    assert not older.is_alive() and not detached.is_alive()
    record = value._runtime_store.get("fixture")
    assert record["detached"] is True
    assert record["cdp_health"]["state"] == STATE_RECOVERY_REQUIRED


def test_queued_old_generation_cannot_write_new_registry_generation(launcher):
    value, generation = launcher
    stale = value.cdp_health.snapshot("fixture")
    value.cdp_health.begin_launch("fixture", value.instance_id)
    assert value._persist_health_now("fixture", generation, stale) is False
    assert "cdp_health" not in value._runtime_store.get("fixture")


def test_runtime_cas_rejects_replacement_during_health_commit(launcher):
    value, generation = launcher
    replacement = {"launch_id": "replacement-generation", "pid": 84,
                   "owner_instance": "replacement-owner", "sentinel": "preserve"}

    def replace_before_cas(fields):
        with value._runtime_store.lock:
            value._runtime_store.record = dict(replacement)

    value._runtime_store.before_write = replace_before_cas
    assert value._persist_health_now("fixture", generation, value.cdp_health.snapshot("fixture")) is False
    assert value._runtime_store.get("fixture") == replacement
