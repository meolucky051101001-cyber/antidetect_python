"""Synthetic regressions for loopback capabilities and token persistence."""
import contextlib
import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@contextlib.contextmanager
def _server(*, redirect=None):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            hits.append((self.path, self.headers.get("Authorization")))
            if redirect:
                self.send_response(302)
                self.send_header("Location", redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps({"application": "antidetect-python", "unlock_code": "otc_fixture"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_unlock_capability_ignores_environment_proxy(monkeypatch):
    import run
    with _server() as (direct_port, direct_hits), _server() as (proxy_port, proxy_hits):
        proxy = f"http://127.0.0.1:{proxy_port}"
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy})
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        monkeypatch.setattr(urllib.request, "_opener", None)
        monkeypatch.setattr(run, "read_access_key", lambda: "synthetic-capability")
        assert run.request_unlock_code_from_server(direct_port) == "otc_fixture"
        assert not proxy_hits, "a control request reached the configured proxy"
        assert len(direct_hits) == 1


def test_unlock_does_not_follow_redirect_to_another_origin(monkeypatch):
    import run
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(run, "read_access_key", lambda: "synthetic-capability")
    with _server() as (target_port, target_hits):
        with _server(redirect=f"http://127.0.0.1:{target_port}/collect") as (port, _):
            assert run.request_unlock_code_from_server(port) is None
        assert not target_hits


def test_revoke_propagates_storage_failure(tmp_path, monkeypatch):
    from antidetect import runtime_auth
    path = tmp_path / "api_token.dpapi"
    path.write_bytes(b"synthetic encrypted fixture")
    monkeypatch.setattr(runtime_auth, "API_TOKEN_FILE", path)
    native_unlink = Path.unlink

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic denied")
        return native_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError):
        runtime_auth.delete_api_token_hash()
    assert path.exists()


@pytest.fixture
def token_api(tmp_path, monkeypatch):
    from antidetect import runtime_auth
    import web.app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(runtime_auth, "API_TOKEN_FILE", tmp_path / "api_token.dpapi")
    monkeypatch.setattr(runtime_auth, "_crypt", lambda payload, protect: payload)
    monkeypatch.setattr(app_module, "_token_rotate_times", [])
    client = TestClient(app_module.app, base_url="http://127.0.0.1:8000")
    headers = {"Authorization": "Bearer " + app_module.app.state.access_key}
    yield runtime_auth, app_module, client, headers
    client.close()


def test_api_revoke_reports_failure_without_leaking_error(token_api, monkeypatch):
    runtime_auth, _, client, headers = token_api
    runtime_auth.write_api_token_hash(hashlib.sha256(b"fixture-token").hexdigest())

    def denied():
        raise PermissionError("synthetic-private-file-path")

    monkeypatch.setattr(runtime_auth, "delete_api_token_hash", denied)
    response = client.delete("/api/auth/token", headers=headers)
    assert response.status_code == 503
    assert "synthetic-private-file-path" not in response.text
    assert runtime_auth.read_api_token_hash() is not None



@pytest.mark.parametrize("url", [
    "http://example.invalid:8000/private",
    "http://localhost:8000/private",
    "file:///synthetic",
    "http://127.0.0.1/private",
    "http://127.0.0.1:0/private",
    "http://fixture@127.0.0.1:8000/private",
    "http://127.0.0.1:8000/private#fragment",
])
def test_control_transport_rejects_nonliteral_or_ambiguous_origin(url, monkeypatch):
    from antidetect.loopback import open_loopback
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *args: pytest.fail("must not open a connection")
    )
    with pytest.raises(ValueError):
        open_loopback(url, timeout=1)


@pytest.mark.parametrize("timeout", [None, True, 0, -1, float("inf"), float("nan")])
def test_control_transport_requires_finite_positive_timeout(timeout):
    from antidetect.loopback import open_loopback
    with pytest.raises(ValueError):
        open_loopback("http://127.0.0.1:9/private", timeout=timeout)


def test_control_transport_rejects_host_override():
    from antidetect.loopback import open_loopback
    req = urllib.request.Request(
        "http://127.0.0.1:9/private", headers={"Host": "elsewhere.invalid"}
    )
    with pytest.raises(ValueError):
        open_loopback(req, timeout=1)


def test_revoke_is_idempotent_and_new_requests_reject_old_token(token_api):
    runtime_auth, _, client, headers = token_api
    token = "synthetic-persistent-token"
    runtime_auth.write_api_token_hash(hashlib.sha256(token.encode()).hexdigest())
    response = client.delete("/api/auth/token", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert client.get(
        "/api/profiles", headers={"Authorization": "Bearer " + token}
    ).status_code == 401
    assert client.delete("/api/auth/token", headers=headers).status_code == 200


def test_rotate_does_not_return_unpersisted_token(token_api, monkeypatch):
    runtime_auth, _, client, headers = token_api
    old = hashlib.sha256(b"synthetic-old-token").hexdigest()
    runtime_auth.write_api_token_hash(old)

    def denied(value):
        raise PermissionError("synthetic-private-path")

    monkeypatch.setattr(runtime_auth, "write_api_token_hash", denied)
    response = client.post("/api/auth/token/rotate", headers=headers)
    assert response.status_code == 503
    assert "token" not in response.json()
    assert "synthetic-private-path" not in response.text
    assert runtime_auth.read_api_token_hash() == old


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_token_mutation_rechecks_authority_after_waiting_for_lock(token_api, monkeypatch, operation):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    from starlette.requests import Request

    runtime_auth, app_module, _, _ = token_api
    token = "synthetic-old-token"
    old = hashlib.sha256(token.encode()).hexdigest()
    new = hashlib.sha256(b"synthetic-replacement-token").hexdigest()
    runtime_auth.write_api_token_hash(old)
    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"authorization", ("Bearer " + token).encode())],
    })
    authenticated = threading.Event()
    native_authorized = app_module.authorized

    def authorized(req):
        result = native_authorized(req)
        if result:
            authenticated.set()
        return result

    monkeypatch.setattr(app_module, "authorized", authorized)
    endpoint = app_module.rotate_api_token if operation == "rotate" else app_module.revoke_api_token
    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime_auth.api_token_lock():
            future = pool.submit(lambda: asyncio.run(endpoint(request)))
            assert authenticated.wait(1.5), "request did not pass initial authorization"
            # This transaction completes before the queued mutation acquires its lock.
            if operation == "rotate":
                runtime_auth.delete_api_token_hash()
            else:
                runtime_auth.write_api_token_hash(new)
        with pytest.raises(HTTPException) as error:
            future.result(timeout=5)
    assert error.value.status_code == 401
    assert runtime_auth.read_api_token_hash() == (None if operation == "rotate" else new)


def test_token_mutations_share_lock_between_processes(tmp_path, monkeypatch):
    import subprocess
    import sys
    from antidetect import runtime_auth

    token_path = tmp_path / "api_token.dpapi"
    monkeypatch.setattr(runtime_auth, "API_TOKEN_FILE", token_path)
    monkeypatch.setattr(runtime_auth, "_crypt", lambda payload, protect: payload)
    code = (
        "import sys; from pathlib import Path; "
        "from antidetect import runtime_auth as ra; "
        "ra.API_TOKEN_FILE = Path(sys.argv[1]); "
        "ra._crypt = lambda payload, protect: payload; "
        "print('ready', flush=True); "
        "ra.delete_api_token_hash(); print('revoked', flush=True)"
    )
    process = None
    try:
        with runtime_auth.api_token_lock():
            runtime_auth.write_api_token_hash("synthetic-hash")
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(token_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert process.stdout.readline().strip() == "ready"
            assert process.poll() is None
            assert token_path.exists()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "revoked" in stdout
        assert not token_path.exists()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_websocket_transport_disables_proxy_and_refuses_redirect():
    import asyncio
    from antidetect.loopback import connect_loopback
    from websockets.exceptions import InvalidStatus

    async def connect(port):
        async with connect_loopback(f"ws://127.0.0.1:{port}/devtools/browser/test"):
            pytest.fail("redirected control socket must not connect")

    with _server() as (target_port, target_hits):
        with _server(redirect=f"ws://127.0.0.1:{target_port}/collect") as (port, _):
            with pytest.raises(InvalidStatus):
                asyncio.run(connect(port))
        assert not target_hits


def test_websocket_connector_explicit_proxy_policy(monkeypatch):
    import types
    import websockets
    from antidetect.loopback import connect_loopback
    seen = {}
    def connector(url, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace()
    monkeypatch.setattr(websockets, "connect", connector)
    connection = connect_loopback("ws://127.0.0.1:9222/devtools/browser/test")
    assert seen["proxy"] is None
    assert seen["open_timeout"] > 0 and seen["close_timeout"] > 0
    failure = RuntimeError("synthetic")
    assert connection.process_redirect(failure) is failure


@pytest.mark.parametrize("url,options", [
    ("ws://outside.invalid:9222/x", {}),
    ("ws://127.0.0.1:9222/x", {"proxy": True}),
    ("ws://127.0.0.1:9222/x", {"host": "outside.invalid"}),
    ("ws://127.0.0.1:9222/x", {"open_timeout": None}),
])
def test_websocket_rejects_remote_or_unbounded_transport(url, options):
    from antidetect.loopback import connect_loopback
    with pytest.raises(ValueError):
        connect_loopback(url, **options)
