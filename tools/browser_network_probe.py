"""Observe actual Chromium proxy routes with synthetic localhost endpoints only.

The fixture hostname is mapped to loopback by a test-only resolver rule. This
avoids Chromium's implicit localhost proxy bypass and makes direct fallback
observable. Proxy handlers never forward to any remote endpoint.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from contextlib import ExitStack, contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOST = "browser-proxy-fixture.invalid"
BODY = b'<!doctype html><title>synthetic network fixture</title><body>local only</body>'
AUTH = "Basic " + base64.b64encode(b"synthetic-user:synthetic-password").decode()


def read_exact(peer, count):
    result = bytearray()
    while len(result) < count:
        block = peer.recv(count - len(result))
        if not block:
            raise EOFError()
        result.extend(block)
    return bytes(result)


class QuietServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def handle_error(self, *_args):
        pass  # Browser cancellation/disconnect is expected; no request logging.


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def http_fixture(observed, *, proxy=False, auth=False, reject=False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            destination = urlsplit(self.path)
            if proxy and destination.hostname != HOST:
                self.send_error(502)
                return
            if destination.path != "/observation":
                self.send_error(404)
                return
            observed["requests"] += 1
            if auth and (reject or self.headers.get("Proxy-Authorization") != AUTH):
                observed["challenges"] += 1
                if self.headers.get("Proxy-Authorization") == AUTH:
                    observed["rejected_credentials"] = observed.get("rejected_credentials", 0) + 1
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="synthetic fixture"')
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if auth:
                observed["authenticated"] += 1
            observed["served"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def do_CONNECT(self):
            self.send_error(502)  # Never tunnel browser background requests.

        def log_message(self, *_args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *_args):
            pass

    return Server(("127.0.0.1", 0), Handler)


def socks_fixture(observed, *, reject=False):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            peer = self.request
            peer.settimeout(5)
            version, count = read_exact(peer, 2)
            methods = read_exact(peer, count)
            if version != 5 or 0 not in methods:
                return
            peer.sendall(b"\x05\x00")
            version, command, _, kind = read_exact(peer, 4)
            if kind == 3:
                destination = read_exact(peer, read_exact(peer, 1)[0]).decode("ascii")
            elif kind == 1:
                destination = socket.inet_ntop(socket.AF_INET, read_exact(peer, 4))
            elif kind == 4:
                destination = socket.inet_ntop(socket.AF_INET6, read_exact(peer, 16))
            else:
                return
            read_exact(peer, 2)
            if (version, command) != (5, 1) or destination != HOST:
                peer.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            observed["requests"] += 1
            observed["remote_dns_name_received"] = kind == 3
            if reject:
                peer.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            peer.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            request = bytearray()
            while b"\r\n\r\n" not in request and len(request) < 16384:
                request.extend(read_exact(peer, 1))
            if not request.startswith(b"GET /observation "):
                return
            observed["served"] += 1
            peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         + f"Content-Length: {len(BODY)}\r\n\r\n".encode() + BODY)

    return QuietServer(("127.0.0.1", 0), Handler)


class StunObserver(socketserver.ThreadingUDPServer):
    daemon_threads = True

    def __init__(self):
        self.binding_requests = 0
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                packet, peer = self.request
                if len(packet) >= 20 and packet[:2] == b"\x00\x01" and packet[4:8] == b"\x21\x12\xa4\x42":
                    owner.binding_requests += 1
                    # Return the observed local address only to Chromium; reports
                    # contain aggregate request counts, never ICE addresses.
                    address, port = self.client_address
                    encoded = bytes(a ^ b for a, b in zip(socket.inet_aton(address), b"\x21\x12\xa4\x42"))
                    attribute = struct.pack("!HHBBH", 0x20, 8, 0, 1, port ^ 0x2112) + encoded
                    peer.sendto(b"\x01\x01" + struct.pack("!H", len(attribute)) + packet[4:20] + attribute, self.client_address)

        super().__init__(("127.0.0.1", 0), Handler)


def counters():
    return {"requests": 0, "challenges": 0, "authenticated": 0, "served": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--browser")
    args = parser.parse_args()
    root = Path(args.data_dir).resolve()
    if root.exists() or Path(tempfile.gettempdir()).resolve() not in root.parents:
        parser.error("Use a fresh nonexistent directory under TEMP.")
    root.mkdir(parents=True)
    os.environ["ANTIDETECT_DATA_DIR"] = str(root)
    os.environ["ANTIDETECT_NO_BROWSER"] = "1"
    if args.browser:
        os.environ["ANTIDETECT_BROWSER_PATH"] = args.browser
    sys.path.insert(0, str(ROOT))
    from antidetect.fingerprint import generate_fingerprint
    from antidetect.launcher import BrowserLauncher
    from antidetect.models import ProxyConfig
    from antidetect.storage import ProfileManager
    from tools.fingerprint_contract_probe import PassivePage
    import antidetect.launcher as launcher_module

    report = {"status": "running", "scenarios": [], "external_proxy_dns_stun": "unverified_no_controlled_endpoint",
              "scope": "BrowserLauncher localhost HTTP/SOCKS5 routes and local STUN observation; no total-egress claim",
              "test_only_flags": ["--host-resolver-rules=MAP " + HOST + " 127.0.0.1"],
              "https_proxy_transport": "not_run", "socks4": "not_run"}
    source = subprocess.run(["git", "-c", "safe.directory=" + str(ROOT), "rev-parse", "HEAD"],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    report["source_commit"] = source.stdout.strip() if source.returncode == 0 else None
    report["source_sha256"] = {relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                               for relative in ("antidetect/launcher.py", "antidetect/models.py", "antidetect/proxy_auth.py",
                                                "antidetect/browser_env.py", "tools/browser_network_probe.py")}
    manager = ProfileManager()
    launcher = BrowserLauncher(manager)
    original_popen = launcher_module.subprocess.Popen

    def fixture_popen(command, *positional, **kwargs):
        if isinstance(command, list) and any(str(arg).startswith("--user-data-dir=") for arg in command):
            command = [*command, "--host-resolver-rules=MAP " + HOST + " 127.0.0.1"]
        return original_popen(command, *positional, **kwargs)

    try:
        with patch.object(launcher_module.subprocess, "Popen", fixture_popen):
            for name in ("direct", "http", "http_auth", "http_auth_reject", "http_down", "socks5", "socks5_reject", "socks5_down"):
                direct, routed = counters(), counters()
                scenario = {"name": name, "direct": direct, "proxy": routed}
                report["scenarios"].append(scenario)
                with ExitStack() as stack:
                    origin = stack.enter_context(serving(http_fixture(direct)))
                    stun = stack.enter_context(serving(StunObserver()))
                    config = ProxyConfig()
                    if name != "direct":
                        protocol = "socks5" if name.startswith("socks5") else "http"
                        if name.endswith("down"):
                            unused = stack.enter_context(socket.socket())
                            unused.bind(("127.0.0.1", 0))
                            port = unused.getsockname()[1]
                        else:
                            server = (socks_fixture(routed, reject=name.endswith("reject")) if protocol == "socks5"
                                      else http_fixture(routed, proxy=True, auth="auth" in name, reject=name.endswith("reject")))
                            proxy = stack.enter_context(serving(server))
                            port = proxy.server_address[1]
                        config = ProxyConfig(enabled=True, protocol=protocol, host="127.0.0.1", port=port,
                                             username="synthetic-user" if "auth" in name else None,
                                             password="synthetic-password" if "auth" in name else None)
                    fp = generate_fingerprint("windows")
                    fp.webrtc_mode = "auto" if name == "direct" else "disable_non_proxied"
                    profile = manager.create_profile("synthetic-network-" + name, fingerprint=fp, proxy=config)
                    result = launcher.launch(profile.id)
                    page = None
                    try:
                        assert result.get("success") and not result.get("degraded"), "Launch did not verify: " + str(result.get("code"))
                        connection = http.client.HTTPConnection("127.0.0.1", result["port"], timeout=5)
                        try:
                            connection.request("GET", "/json/version")
                            scenario["browser_version"] = json.loads(connection.getresponse().read()).get("Browser")
                        finally:
                            connection.close()
                        page = PassivePage(result["port"], "about:blank")
                        # Navigation is observed through a page-only CDP connection;
                        # the product's Fetch authentication controller remains owner.
                        target = f"http://{HOST}:{origin.server_port}/observation"
                        navigation = page.call("Page.navigate", {"url": target})
                        expected_failure = name.endswith(("reject", "down"))
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            if expected_failure and navigation.get("errorText"):
                                break
                            if not expected_failure and direct["served"] + routed["served"]:
                                break
                            time.sleep(0.05)
                        scenario["navigation_failed"] = bool(navigation.get("errorText"))
                        if name == "direct":
                            assert direct["served"] > 0
                        elif expected_failure:
                            assert navigation.get("errorText"), "Rejected route did not fail navigation."
                            assert direct["requests"] == 0 and routed["served"] == 0, "Unexpected direct fallback or accepted reject."
                        else:
                            assert routed["served"] > 0 and direct["requests"] == 0, "Proxy route was not exclusive for fixture."
                        if not expected_failure:
                            assert not navigation.get("errorText"), "Successful proxy response did not complete navigation."
                            page.wait_for_url(target)
                            page.wait_for_function("document.title === 'synthetic network fixture' && document.body.textContent === 'local only'")
                        if name == "http_auth":
                            assert routed["authenticated"] > 0 and routed["challenges"] > 0
                        if name == "http_auth_reject":
                            assert routed["requests"] > 0 and routed["challenges"] > 0 and routed.get("rejected_credentials", 0) > 0, "Auth rejection fixture did not observe credentials."
                        if name in ("socks5", "socks5_reject"):
                            assert routed.get("remote_dns_name_received") is True, "SOCKS5 did not receive the destination name."
                        if name in ("direct", "http", "socks5"):
                            observation = page.evaluate("""async port => {
                                const pc = new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:'+port}]});
                                let candidates=0; pc.onicecandidate=e=>{if(e.candidate)candidates++};
                                try {pc.createDataChannel('synthetic'); await pc.setLocalDescription(await pc.createOffer());
                                    await new Promise(resolve=>{const timer=setTimeout(resolve,3500);
                                        pc.onicegatheringstatechange=()=>{if(pc.iceGatheringState==='complete'){clearTimeout(timer);resolve();}};});
                                    return {candidate_count:candidates,gathering_state:pc.iceGatheringState};
                                } finally {pc.close();}
                            }""", stun.server_address[1])
                            scenario["stun"] = {**observation, "binding_requests": stun.binding_requests}
                            if name == "direct":
                                assert stun.binding_requests > 0, "Local STUN positive control was not observed."
                            else:
                                assert stun.binding_requests == 0, "Non-proxied local STUN request observed."
                        scenario["status"] = "passed"
                    finally:
                        if page:
                            page.close()
                        stopped = launcher.stop(profile.id, expected_launch_id=result.get("launch_id"))
                        scenario["cleanup_confirmed"] = bool(stopped.get("success"))
                        scenario["cleanup_code"] = stopped.get("code")
                        final_cleanup = stopped
                        if not stopped.get("success"):
                            # Preserve the first failed stop as a test failure,
                            # but continue bounded cleanup of this same fixture.
                            time.sleep(3)
                            retry = launcher.stop(profile.id)
                            scenario["cleanup_retry_confirmed"] = bool(retry.get("success"))
                            scenario["cleanup_retry_code"] = retry.get("code")
                            final_cleanup = retry
                        scenario["cleanup_confirmed"] = bool(final_cleanup.get("success"))
                        assert final_cleanup.get("success"), "Synthetic browser cleanup failed after bounded confirmation retry."
                Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(json.dumps({"scenario": name, "status": scenario.get("status")}), flush=True)
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = type(exc).__name__ + ": " + str(exc)
    finally:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "failure": report.get("failure")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
