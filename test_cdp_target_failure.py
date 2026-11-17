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
