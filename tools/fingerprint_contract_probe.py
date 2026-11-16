"""Local-only BrowserLauncher canvas contracts with fresh synthetic data."""
from __future__ import annotations

import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import platform
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]



class PassivePage:
    """Page-only CDP client that never auto-attaches or resumes worker targets.

    Playwright connect_over_cdp configures Target.setAutoAttach and resumes
    service workers independently of the product controller, invalidating an
    early-injection test. This client uses an existing page's direct socket.
    """

    def __init__(self, port, expected_url, timeout=20, target_type="page"):
        from websockets.sync.client import connect
        from antidetect.loopback import _validated_parts

        deadline = time.monotonic() + timeout
        while True:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request('GET', '/json/list')
                response = connection.getresponse()
                targets = json.loads(response.read())
            finally:
                connection.close()
            target = next((item for item in targets if item.get('type') == target_type
                           and item.get('url', '').rstrip('/') == expected_url.rstrip('/')), None)
            if target:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Product fixture target did not appear: ' + target_type + '; listed types=' + repr([item.get('type') for item in targets]))
            time.sleep(0.05)
        socket_url = target["webSocketDebuggerUrl"]
        endpoint = _validated_parts(socket_url, {"ws"})
        if endpoint.hostname != "127.0.0.1" or endpoint.port != port:
            raise ValueError("Fixture CDP endpoint must stay on its original literal loopback address and port.")
        self.socket = connect(socket_url, proxy=None, open_timeout=5, max_size=16 * 1024 * 1024)
        self.target_type = target["type"]
        self.target_id = target["id"]
        self.sequence = 0
        self.contexts = {}
        self.call('Runtime.enable')

    def call(self, method, params=None):
        self.sequence += 1
        message_id = self.sequence
        self.last_method = method
        self.socket.send(json.dumps({'id': message_id, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + 25
        while True:
            message = json.loads(self.socket.recv(timeout=max(0.01, deadline - time.monotonic())))
            if message.get('method') == 'Runtime.executionContextCreated':
                context = message['params']['context']
                self.contexts[context['id']] = context
            elif message.get('method') == 'Runtime.executionContextDestroyed':
                self.contexts.pop(message['params']['executionContextId'], None)
            elif message.get('method') == 'Runtime.executionContextsCleared':
                self.contexts.clear()
            if message.get('id') != message_id:
                continue
            if 'error' in message:
                raise RuntimeError(method + ': ' + json.dumps(message['error']))
            return message.get('result', {})

    def evaluate(self, expression, argument=None, context_id=None):
        script = ('(() => {const candidate = (' + expression + '); return typeof candidate === "function"'
                  + ' ? candidate(' + json.dumps(argument) + ') : candidate;})()')
        params = {'expression': script, 'awaitPromise': True, 'returnByValue': True}
        if context_id is not None:
            params['contextId'] = context_id
        response = self.call('Runtime.evaluate', params)
        if 'exceptionDetails' in response:
            raise RuntimeError('Fixture evaluation failed: ' + json.dumps(response['exceptionDetails']))
        return response.get('result', {}).get('value')

    def wait_for_function(self, expression, timeout=20000, context_id=None):
        deadline = time.monotonic() + timeout / 1000
        while not self.evaluate(expression, context_id=context_id):
            if time.monotonic() >= deadline:
                raise TimeoutError('Fixture condition did not become true.')
            time.sleep(0.05)

    def wait_for_url(self, url, timeout=20000):
        self.wait_for_function('location.href === ' + json.dumps(url), timeout=timeout)

    def frame_evaluate(self, frame_name, expression):
        deadline = time.monotonic() + 15
        while True:
            tree = self.call('Page.getFrameTree')['frameTree']
            frames = list(tree.get('childFrames', []))
            frame = next((item['frame'] for item in frames if item['frame'].get('name') == frame_name), None)
            if frame:
                context = next((item for item in self.contexts.values()
                                if item.get('auxData', {}).get('isDefault')
                                and item.get('auxData', {}).get('frameId') == frame['id']), None)
                if context and self.evaluate('typeof canvasContract === "function"', context_id=context['id']):
                    return self.evaluate(expression, context_id=context['id'])
            if time.monotonic() >= deadline:
                raise TimeoutError('Fixture frame did not initialize: ' + frame_name)
            time.sleep(0.05)

    def reload(self):
        old_loader = self.call('Page.getFrameTree')['frameTree']['frame']['loaderId']
        self.call('Page.reload')
        deadline = time.monotonic() + 20
        while self.call('Page.getFrameTree')['frameTree']['frame']['loaderId'] == old_loader:
            if time.monotonic() >= deadline:
                raise TimeoutError('Fixture reload did not navigate.')
            time.sleep(0.05)
        self.wait_for_function('document.readyState === "complete" && typeof canvasContract === "function"')

    def close(self):
        self.socket.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--browser")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in data_dir.parents or data_dir.exists():
        raise SystemExit("A fresh, nonexistent child of TEMP is required.")
    data_dir.mkdir(parents=True)
    os.environ["ANTIDETECT_DATA_DIR"] = str(data_dir)
    os.environ["ANTIDETECT_NO_BROWSER"] = "1"
    if args.browser:
        os.environ["ANTIDETECT_BROWSER_PATH"] = args.browser
    sys.path.insert(0, str(ROOT))
    from antidetect.fingerprint import generate_fingerprint
    from antidetect.launcher import BrowserLauncher, find_browser_executable
    from antidetect.storage import ProfileManager
    from antidetect.stealth import get_stealth_capabilities
    from playwright.sync_api import sync_playwright

    request_counts = {"forbidden": 0}
    fixture = (ROOT / "tools" / "fixtures" / "fingerprint_browser.js").read_bytes()
    first_worker_read = b"globalThis.fixtureFirstRead=(()=>{const c=new OffscreenCanvas(8,8),ctx=c.getContext('2d');ctx.fillStyle='rgb(101,151,201)';ctx.fillRect(0,0,8,8);const pixels=Array.from(ctx.getImageData(0,0,8,8).data);return {pixels,noisy:pixels.some((v,i)=>v!==[101,151,201,255][i%4])};})();"
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/forbidden":
                request_counts["forbidden"] += 1
            if path in ("/", "/frame", "/forbidden"):
                body = b'<!doctype html><meta charset="utf-8"><script src="/contract.js"></script><body>synthetic fingerprint contract</body>'
                mime = "text/html"
            elif path == "/contract.js":
                body, mime = fixture, "text/javascript"
            elif path == "/worker.js":
                body = first_worker_read + b"importScripts('/contract.js');onmessage=async event=>postMessage(await canvasContract(true,event.data.canvas));"
                mime = "text/javascript"
            elif path == "/nested-worker.js":
                body = first_worker_read + b"onmessage=e=>{const child=new Worker('/worker.js');child.onmessage=r=>{r.data.outerWorkerRead=globalThis.fixtureFirstRead;postMessage(r.data);child.terminate();};child.onerror=e=>{throw new Error(e.message);};child.postMessage({});};"
                mime = "text/javascript"
            elif path == "/shared-worker.js":
                body = first_worker_read + b"importScripts('/contract.js');onconnect=e=>{const p=e.ports[0];p.onmessage=async()=>p.postMessage(await canvasContract(true));p.start();};"
                mime = "text/javascript"
            elif path == "/service-worker.js":
                body = first_worker_read + b"importScripts('/contract.js');self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('message',e=>e.waitUntil(canvasContract(true).then(r=>e.ports[0].postMessage(r))));"
                mime = "text/javascript"
            elif path == "/module-worker.js":
                body = first_worker_read + b"import '/contract.js';onmessage=async event=>postMessage(await canvasContract(true,event.data.canvas));"
                mime = "text/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass

    class FixtureServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            if isinstance(sys.exception() if hasattr(sys, "exception") else sys.exc_info()[1], ConnectionResetError):
                return
            super().handle_error(request, client_address)

    server = FixtureServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_port)
    frame_server = FixtureServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=frame_server.serve_forever, daemon=True).start()
    frame_base = "http://127.0.0.1:" + str(frame_server.server_port)
    cross_site_base = "http://localhost:" + str(frame_server.server_port)
    manager = ProfileManager()
    launcher = BrowserLauncher(manager)
    report = {"scope": "fresh synthetic profiles, localhost only; not an egress isolation audit", "modes": {}, "cleanup": []}
    report["launcher_warnings"] = []

    class FixtureLogCapture(logging.Handler):
        def emit(self, record):
            if len(report["launcher_warnings"]) < 50:
                report["launcher_warnings"].append(record.levelname + ": " + record.getMessage())

    logging.getLogger("antidetect").addHandler(FixtureLogCapture(level=logging.WARNING))
    report["measurement_control"] = "Direct page CDP; no Target.setAutoAttach or worker resume commands from probe"
    report["scope_checks"] = {}
    checks = ("readStable", "fullTileEqual", "negativeCropEqual", "unchangedWhilePending", "unchangedAfterExport", "drawPreserved", "repeatedExportsEqual")
    def summarize(result, decoder):
        import base64
        # Decode with the clean comparator. Reading with the instrumented page
        # would apply a second readout transform to the PNG being verified.
        decoded = decoder.evaluate("""async b64=>{
            const raw=atob(b64),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
            const image=await createImageBitmap(new Blob([bytes],{type:'image/png'}));
            const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;
            const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);image.close();
            return Array.from(ctx.getImageData(0,0,canvas.width,canvas.height).data);
        }""", base64.b64encode(bytes(result["png"])).decode())
        result["decodedExportMatchesRead"] = decoded == result["pixels"]
        for key in ("initialWorkerRead", "outerWorkerRead"):
            if result.get(key):
                result[key]["pixelHash"] = hashlib.sha256(bytes(result[key].pop("pixels"))).hexdigest()
        result["pixelHash"] = hashlib.sha256(bytes(result.pop("pixels"))).hexdigest()
        result.pop("png")
        return result

    report["platform"] = platform.platform()
    report["source_files"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "antidetect/models.py", "antidetect/fingerprint.py", "antidetect/launcher.py",
            "antidetect/stealth.py", "antidetect/assets/stealth_page.js",
            "antidetect/assets/stealth_worker.js", "antidetect/assets/stealth_canvas.js",
            "tools/fingerprint_contract_probe.py", "tools/fixtures/fingerprint_browser.js",
        )
    }
    git = subprocess.run(
        ["git", "-c", "safe.directory=" + str(ROOT), "rev-parse", "HEAD"],
        cwd=ROOT, capture_output=True, text=True,
    )
    report["source_commit"] = git.stdout.strip() if git.returncode == 0 else None
    report["source_scope"] = "worktree file hashes; commit alone does not include uncommitted fixes"
    code = 0
    try:
        with sync_playwright() as pw:
            binary = args.browser or find_browser_executable()
            report["browser_binary"] = str(binary)
            report["browser_binary_sha256"] = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
            native = pw.chromium.launch_persistent_context(str(data_dir / "native"), executable_path=binary, headless=False, args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
            try:
                native_page = native.pages[0]
                native_page.goto(base)
                report["browser"] = native_page.evaluate("navigator.userAgent")
                report["browser_version"] = native.browser.version if native.browser else None
                report["native_environment"] = native_page.evaluate("""()=>{
                    const gl=document.createElement('canvas').getContext('webgl');
                    const ext=gl&&gl.getExtension('WEBGL_debug_renderer_info');
                    return {locale:navigator.language,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
                        screen:[screen.width,screen.height],viewport:[innerWidth,innerHeight],dpr:devicePixelRatio,
                        renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
                }""")
                native_result = summarize(native_page.evaluate("canvasContract(false)"), native_page)
                assert not native_result["noisy"], "Clean comparator unexpectedly transforms fixture pixels."
                assert all(native_result[key] for key in checks) and native_result["decodedExportMatchesRead"]
                report["modes"]["native"] = native_result
                report["current_stage"] = "strict_rejection"
                rejected_launcher = BrowserLauncher(manager)
                rejected_fp = generate_fingerprint("windows")
                rejected_fp.verification_mode = "strict"
                rejected = manager.create_profile("synthetic-strict-rejection", fingerprint=rejected_fp)

                async def reject_verification(_port, _fp):
                    return {"ok": False, "checks": {"injected_failure": False}, "actual": None}

                rejected_launcher._verify_on_local_origin = reject_verification
                denied = rejected_launcher.launch(rejected.id, open_url=base + "/forbidden")
                try:
                    rejected_running = rejected_launcher.is_running(rejected.id)
                    report["scope_checks"]["strict_rejection"] = {
                        "success": denied.get("success"),
                        "cleanup_failed": denied.get("cleanup_failed"), "process_running": rejected_running,
                        "health_state": (denied.get("cdp_health") or {}).get("state"),
                        "health_reason": (denied.get("cdp_health") or {}).get("reason_code"),
                        "code": denied.get("code"), "navigation_allowed": denied.get("navigation_allowed"),
                        "forbidden_requests": request_counts["forbidden"],
                        "cleanup_confirmed": not denied.get("cleanup_failed") and not rejected_running,
                    }
                finally:
                    if rejected_launcher.is_running(rejected.id):
                        residual = rejected_launcher.stop(rejected.id)
                        report["scope_checks"]["strict_rejection"]["residual_cleanup_success"] = residual.get("success")
                        assert residual.get("success"), "Residual synthetic cleanup failed."
                for mode in ("unique", "authentic"):
                    fp = generate_fingerprint("windows")
                    fp.fingerprint_algorithm_version = 2
                    fp.canvas_noise_seed = 12345
                    if mode == "authentic":
                        fp.stealth_disable = ["canvas", "webgl", "audio", "media", "worker"]
                    profile = manager.create_profile("synthetic-contract-" + mode, fingerprint=fp)
                    result = launcher.launch(profile.id, open_url=base)
                    session = result.get("launch_id") or (result.get("cdp_health") or {}).get("launch_id")
                    try:
                        assert result.get("success") and not result.get("degraded"), "Synthetic launcher did not become healthy: " + str(result.get("code"))
                        report["current_stage"] = mode + ":connect_page"
                        page = PassivePage(result["port"], base)
                        page.wait_for_url(base + "/", timeout=20000)
                        page.wait_for_function("typeof canvasContract === 'function'")
                        report["current_stage"] = mode + ":page_canvas"
                        realms = {"page": summarize(page.evaluate("canvasContract(false)"), native_page), "offscreen": summarize(page.evaluate("canvasContract(true)"), native_page)}
                        report["modes"][mode] = {"capabilities": get_stealth_capabilities(fp), "realms": realms}
                        for frame_label, frame_url in (("same_origin_frame", base + "/frame"), ("cross_origin_frame", frame_base + "/frame"), ("cross_site_oopif", cross_site_base + "/frame")):
                            report["current_stage"] = mode + ":" + frame_label
                            frame_id = frame_label
                            page.evaluate("""({id,url})=>{
                                const f=document.createElement('iframe');f.id=id;f.name=id;f.src=url;document.body.appendChild(f);
                            }""", {"id":frame_id, "url":frame_url})
                            if frame_label == "cross_site_oopif":
                                child = PassivePage(result["port"], frame_url, target_type="iframe")
                                try:
                                    child.wait_for_function("typeof canvasContract === 'function'")
                                    value = child.evaluate("canvasContract(false)")
                                    value["target_type"] = child.target_type
                                    assert child.target_type == "iframe", "Cross-site fixture did not produce an OOPIF target."
                                finally:
                                    child.close()
                            else:
                                value = page.frame_evaluate(frame_id, "()=>canvasContract(false)")
                            realms[frame_label] = summarize(value, native_page)
                        for worker_mode in ("classic", "module", "nested"):
                            report["current_stage"] = mode + ":" + worker_mode + "_worker"

                            value = page.evaluate("""async type=>{
                                const worker=new Worker(type==='module'?'/module-worker.js':type==='nested'?'/nested-worker.js':'/worker.js',type==='module'?{type:'module'}:{});
                                try{return await new Promise((resolve,reject)=>{
                                    const timer=setTimeout(()=>reject(new Error('worker timeout')),15000);
                                    worker.onmessage=e=>{clearTimeout(timer);resolve(e.data);};
                                    worker.onerror=e=>{clearTimeout(timer);reject(new Error(e.message));};
                                    worker.postMessage({});
                                });}finally{worker.terminate();}
                            }""", worker_mode)
                            realms[worker_mode + "_worker"] = summarize(value, native_page)
                        report["current_stage"] = mode + ":transferred_offscreen_worker"
                        transferred = page.evaluate("""async ()=>{
                            const worker=new Worker('/worker.js');
                            try{return await new Promise((resolve,reject)=>{
                                const timer=setTimeout(()=>reject(new Error('transferred canvas timeout')),15000);
                                worker.onmessage=e=>{clearTimeout(timer);resolve(e.data);};
                                worker.onerror=e=>{clearTimeout(timer);reject(new Error(e.message));};
                                const canvas=new OffscreenCanvas(32,32);worker.postMessage({canvas},[canvas]);
                            });}finally{worker.terminate();}
                        }""")
                        realms["transferred_offscreen_worker"] = summarize(transferred, native_page)
                        report["current_stage"] = mode + ":shared_worker"
                        shared = page.evaluate("""async ()=>{
                            const worker=new SharedWorker('/shared-worker.js');
                            try{return await new Promise((resolve,reject)=>{
                                const timer=setTimeout(()=>reject(new Error('shared worker timeout')),15000);
                                worker.port.onmessage=e=>{clearTimeout(timer);resolve(e.data);};
                                worker.onerror=e=>{clearTimeout(timer);reject(new Error(e.message));};
                                worker.port.start();worker.port.postMessage({});
                            });}finally{worker.port.close();}
                        }""")
                        realms["shared_worker"] = summarize(shared, native_page)
                        report["current_stage"] = mode + ":service_worker"
                        service = page.evaluate("""async ()=>{
                            const bounded=(promise,phase)=>Promise.race([promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error('service worker '+phase+' timeout')),12000))]);
                            const registration=await bounded(navigator.serviceWorker.register('/service-worker.js'),'registration');
                            try{
                                const ready=await bounded(navigator.serviceWorker.ready,'activation');
                                return await new Promise((resolve,reject)=>{
                                    const channel=new MessageChannel();
                                    const timer=setTimeout(()=>reject(new Error('service worker timeout')),15000);
                                    channel.port1.onmessage=e=>{clearTimeout(timer);channel.port1.close();resolve(e.data);};
                                    ready.active.postMessage({},[channel.port2]);
                                });
                            }finally{await registration.unregister();}
                        }""")
                        realms["service_worker"] = summarize(service, native_page)
                        report["current_stage"] = mode + ":reload"
                        page.reload()
                        realms["reloaded_page"] = summarize(page.evaluate("canvasContract(false)"), native_page)
                        report["modes"][mode] = {"capabilities": get_stealth_capabilities(fp), "realms": realms}
                        for label, value in realms.items():
                            assert not value["marker"], label + " leaked a marker"
                            assert all(value[key] for key in checks), label + " failed canvas contract: " + ",".join(key for key in checks if not value[key])
                            assert value["decodedExportMatchesRead"], label + " PNG differs from readback"
                            assert value["noisy"] == (mode == "unique"), label + " mode not applied"
                            if label.endswith("_worker"):
                                assert value["initialWorkerRead"]["noisy"] == (mode == "unique"), label + " first script ran before configuration"
                            if label == "nested_worker":
                                assert value["outerWorkerRead"]["noisy"] == (mode == "unique"), "Outer nested worker ran before configuration"
                                assert value["outerWorkerRead"]["pixelHash"] == value["initialWorkerRead"]["pixelHash"], "Nested parent and child first-script pixels differ"

                        assert len({value["pixelHash"] for value in realms.values()}) == 1, "Window/worker pixels differ"
                        assert len({value["initialWorkerRead"]["pixelHash"] for label, value in realms.items() if label.endswith("_worker")}) == 1, "First worker script pixels differ"
                        report["modes"][mode] = {"capabilities": get_stealth_capabilities(fp), "realms": realms}
                        mode_health = launcher.cdp_health.snapshot(profile.id)
                        report["modes"][mode]["final_health"] = {"state": mode_health.get("state"), "reason_code": mode_health.get("reason_code")}
                        assert mode_health.get("state") == "healthy", "Product control became unhealthy during realm checks."
                        if mode == "unique":
                            report["current_stage"] = mode + ":restart"
                            page.close()
                            stopped = launcher.stop(profile.id, expected_launch_id=session)
                            assert stopped.get("success"), "Pre-restart cleanup failed."
                            result = launcher.launch(profile.id, open_url=base)
                            session = result.get("launch_id") or (result.get("cdp_health") or {}).get("launch_id")
                            assert result.get("success") and not result.get("degraded"), "Restart launch not healthy."
                            restarted = PassivePage(result["port"], base)
                            restarted.wait_for_url(base + "/", timeout=20000)
                            restarted.wait_for_function("typeof canvasContract === 'function'")
                            repeat = summarize(restarted.evaluate("canvasContract(false)"), native_page)
                            assert all(repeat[key] for key in checks) and repeat["decodedExportMatchesRead"]
                            assert repeat["pixelHash"] == realms["page"]["pixelHash"], "Restart changed same-revision canvas."
                            report["modes"][mode]["restart"] = repeat
                            restarted.close()

                    finally:
                        if "page" in locals():
                            page.close()
                        cleanup = launcher.stop(profile.id, expected_launch_id=session) if session else launcher.stop(profile.id)
                        report["cleanup"].append({"mode": mode, "success": cleanup.get("success"), "code": cleanup.get("code")})
                        assert cleanup.get("success"), "Synthetic browser cleanup did not complete."
                report["scope_checks"]["canvas_realms"] = "passed"
                rejection = report["scope_checks"]["strict_rejection"]
                assert rejection["success"] is False
                assert rejection["code"] == "CONFIGURATION_UNVERIFIED"
                assert rejection["navigation_allowed"] is False
                assert rejection["forbidden_requests"] == 0, "Strict failure allowed target navigation."
                assert rejection["cleanup_confirmed"], "Strict failure did not confirm browser cleanup."

            finally:
                native.close()
    except Exception as exc:
        report["failure"] = type(exc).__name__ + ": " + str(exc)
        report["failed_stage"] = report.get("current_stage")
        report["last_page_cdp_command"] = getattr(locals().get("page"), "last_method", None)
        code = 1
    finally:
        server.shutdown()
        server.server_close()
        frame_server.shutdown()
        frame_server.server_close()
        report["status"] = "passed" if code == 0 else "failed"
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "modes_completed": list(report["modes"]), "cleanup": report["cleanup"], "failure": report.get("failure"), "failed_stage": report.get("failed_stage")}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
