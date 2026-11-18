"""Protocol failures must not release an unconfigured strict worker."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from antidetect.fingerprint import generate_fingerprint
from test_runtime_generation_cleanup import fixture_launcher


@pytest.mark.parametrize("mode", ["strict", "compatibility"])
@pytest.mark.parametrize("target_type", ["worker", "service_worker"])
def test_worker_evaluation_exception_controls_resume(tmp_path, monkeypatch, mode, target_type):
    import antidetect.launcher as module

    launcher = fixture_launcher(tmp_path)
    launcher._native_brands_cache = {}
    launcher._page_load_events = {}
    monkeypatch.setattr(launcher, "_cdp_process_alive", lambda _: False)
    monkeypatch.setattr(module, "get_stealth_script", lambda *a, **k: "page fixture")
    monkeypatch.setattr(module, "get_worker_stealth_script", lambda *a, **k: "worker fixture")
    monkeypatch.setattr(module, "open_loopback", lambda *a, **k: io.BytesIO(
        b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/fixture"}'))
    calls = []

    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.service_started = False
            self.startup_responses = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, raw):
            message = json.loads(raw)
            calls.append(message)
            result = {"exceptionDetails": {"text": "synthetic evaluation failure"}} if message["method"] == "Runtime.evaluate" else {}
            if message["method"] == "Debugger.setInstrumentationBreakpoint":
                result = {"breakpointId": "before-script"}
            if target_type == "service_worker" and message["method"] in {
                "Debugger.enable", "Debugger.setInstrumentationBreakpoint"
            }:
                # Chrome defers these replies until startup is released. This
                # catches the deadlock caused by awaiting Debugger.enable first.
                self.startup_responses.append({"id": message["id"], "result": result})
                return
            await self.queue.put({"id": message["id"], "result": result})
            if message["method"] == "Target.setAutoAttach" and not message.get("sessionId"):
                await self.queue.put({"method": "Target.attachedToTarget", "params": {
                    "sessionId": "worker-session", "waitingForDebugger": True,
                    "targetInfo": {"targetId": "worker-target", "type": target_type, "url": "http://127.0.0.1:8123/worker.js"},
                }})
                await self.queue.put({"method": "Target.attachedToTarget", "params": {
                    "sessionId": "duplicate-session", "waitingForDebugger": True,
                    "targetInfo": {"targetId": "worker-target", "type": target_type, "url": "http://127.0.0.1:8123/worker.js"},
                }})
            if message["method"] in {"Target.closeTarget", "Runtime.runIfWaitingForDebugger"}:
                if (target_type == "service_worker" and not self.service_started
                        and message["method"] == "Runtime.runIfWaitingForDebugger"):
                    self.service_started = True
                    for response in self.startup_responses:
                        await self.queue.put(response)
                    await self.queue.put({"method": "Debugger.paused", "sessionId": "worker-session", "params": {}})
                else:
                    await self.queue.put(None)

        async def recv(self):
            value = await self.queue.get()
            if value is None:
                # Let the guarded setup task consume its final response.
                await asyncio.sleep(0.01)
                raise OSError("synthetic connection closed")
            return json.dumps(value)

    monkeypatch.setattr(module, "connect_loopback", lambda *a, **k: Socket())
    fp = generate_fingerprint()
    fp.verification_mode = mode
    profile = SimpleNamespace(id="synthetic-profile", fingerprint=fp)
    asyncio.run(asyncio.wait_for(launcher._setup_cdp_stealth(9222, profile), 2))
    methods = [entry["method"] for entry in calls]
    assert methods.count("Runtime.evaluate") == 1
    assert any(entry["method"] == "Target.detachFromTarget" and entry["params"] == {
        "sessionId": "duplicate-session"} for entry in calls)
    assert not any(entry.get("sessionId") == "duplicate-session" for entry in calls)
    if target_type == "service_worker":
        assert methods.index("Debugger.setInstrumentationBreakpoint") < methods.index("Runtime.runIfWaitingForDebugger")
        assert methods.index("Debugger.removeBreakpoint") < methods.index("Runtime.evaluate")
        assert "Debugger.resume" not in methods
    if mode == "strict":
        assert methods.count("Runtime.runIfWaitingForDebugger") == (1 if target_type == "service_worker" else 0)
        close = next(entry for entry in calls if entry["method"] == "Target.closeTarget")
        assert close["params"] == {"targetId": "worker-target"}
    else:
        assert "Runtime.runIfWaitingForDebugger" in methods
        assert "Target.closeTarget" not in methods


def test_nonpage_target_timeout_does_not_leak_unretrieved_task_exception(tmp_path, monkeypatch):
    """Regression for server.log Task exception was never retrieved on type='other' targets."""
    import gc
    import antidetect.launcher as module

    launcher = fixture_launcher(tmp_path)
    launcher._native_brands_cache = {}
    launcher._page_load_events = {}
    monkeypatch.setattr(launcher, "_cdp_process_alive", lambda _: False)
    monkeypatch.setattr(module, "HEARTBEAT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "get_stealth_script", lambda *a, **k: "page fixture")
    monkeypatch.setattr(module, "get_worker_stealth_script", lambda *a, **k: "worker fixture")
    monkeypatch.setattr(module, "open_loopback", lambda *a, **k: io.BytesIO(
        b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/fixture"}'))

    unhandled = []

    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, raw):
            message = json.loads(raw)
            if message["method"] == "Target.setAutoAttach" and not message.get("sessionId"):
                await self.queue.put({"id": message["id"], "result": {}})
                await self.queue.put({"method": "Target.attachedToTarget", "params": {
                    "sessionId": "other-session",
                    "waitingForDebugger": True,
                    "targetInfo": {"targetId": "other-target", "type": "other", "url": "chrome://gpuclean"},
                }})
                await self.queue.put({"method": "Fetch.requestPaused", "sessionId": "other-session", "params": {
                    "requestId": "req-1",
                }})
                return
            # Intentionally never reply to Runtime.runIfWaitingForDebugger or Fetch.continueRequest
            # so both time out before the socket closes.

        async def recv(self):
            try:
                value = await asyncio.wait_for(self.queue.get(), timeout=0.15)
            except asyncio.TimeoutError:
                raise OSError("synthetic socket closed after reply timeout")
            return json.dumps(value)

    monkeypatch.setattr(module, "connect_loopback", lambda *a, **k: Socket())
    fp = generate_fingerprint()
    profile = SimpleNamespace(id="other-target-profile", fingerprint=fp)

    async def run_check():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        await asyncio.wait_for(launcher._setup_cdp_stealth(9222, profile), 2)
        await asyncio.sleep(0.05)
        gc.collect()

    asyncio.run(run_check())
    assert unhandled == []


@pytest.mark.parametrize("target_still_alive", [False, True])
def test_worker_timeout_on_closed_vs_live_target_health_isolation(tmp_path, monkeypatch, target_still_alive):
    """Regression for 2h soak 04:33:31Z: closed tab worker timeout must not poison healthy CDP."""
    import antidetect.launcher as module
    from antidetect.cdp_health import REASON_TARGET_CONFIG_FAILED, STATE_DEGRADED, STATE_HEALTHY

    launcher = fixture_launcher(tmp_path)
    launcher._native_brands_cache = {}
    launcher._page_load_events = {}
    alive = {"value": True}
    monkeypatch.setattr(launcher, "_cdp_process_alive", lambda _: alive["value"])
    monkeypatch.setattr(module, "HEARTBEAT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(module, "get_stealth_script", lambda *a, **k: "page fixture")
    monkeypatch.setattr(module, "get_worker_stealth_script", lambda *a, **k: "worker fixture")
    monkeypatch.setattr(module, "open_loopback", lambda *a, **k: io.BytesIO(
        b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/fixture"}'))

    profile_id = "soak-tab-cycle-profile"
    launch_id = launcher.cdp_health.begin_launch(profile_id, launcher.instance_id)
    launcher.cdp_health.set_process(profile_id, launch_id, 4242)
    observed_during_run = {}
    calls = []

    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, raw):
            message = json.loads(raw)
            calls.append(message)
            method = message["method"]
            if method == "Target.setAutoAttach" and not message.get("sessionId"):
                await self.queue.put({"id": message["id"], "result": {}})
                # Mark launch healthy after bind_session inside _setup_cdp_stealth.
                launcher.cdp_health.mark_healthy(profile_id, launch_id)
                await self.queue.put({"method": "Target.attachedToTarget", "params": {
                    "sessionId": "dying-worker-session",
                    "waitingForDebugger": True,
                    "targetInfo": {"targetId": "dying-worker-target", "type": "worker",
                                   "url": "http://127.0.0.1:58586/worker.js"},
                }})
                return
            if method == "Runtime.enable":
                # Simulate dying worker renderer never answering Runtime.enable.
                return
            if method == "Target.closeTarget":
                # Worker subtarget returns success=False when parent page already closed or cannot close worker.
                await self.queue.put({"id": message["id"], "result": {"success": False}})
                return
            if method == "Target.getTargets":
                infos = (
                    [{"targetId": "dying-worker-target", "type": "worker"}]
                    if target_still_alive
                    else [{"targetId": "main-page", "type": "page"}]
                )
                await self.queue.put({"id": message["id"], "result": {"targetInfos": infos}})
                await self.queue.put(None)
                return
            await self.queue.put({"id": message["id"], "result": {}})

        async def recv(self):
            value = await self.queue.get()
            if value is None:
                await asyncio.sleep(0.03)
                observed_during_run["snapshot"] = launcher.cdp_health.snapshot(profile_id)
                alive["value"] = False
                raise OSError("synthetic end of test")
            return json.dumps(value)

    monkeypatch.setattr(module, "connect_loopback", lambda *a, **k: Socket())
    fp = generate_fingerprint()
    fp.verification_mode = "strict"
    profile = SimpleNamespace(id=profile_id, fingerprint=fp)
    asyncio.run(asyncio.wait_for(launcher._setup_cdp_stealth(9222, profile, launch_id=launch_id), 2))

    methods = [entry["method"] for entry in calls]
    assert "Runtime.runIfWaitingForDebugger" not in methods
    assert "Target.closeTarget" in methods
    assert "Target.getTargets" in methods
    snap = observed_during_run["snapshot"]
    if target_still_alive:
        assert snap["state"] == STATE_DEGRADED
        assert snap["reason"] == REASON_TARGET_CONFIG_FAILED
    else:
        assert snap["state"] == STATE_HEALTHY
        assert snap["reason"] is None
