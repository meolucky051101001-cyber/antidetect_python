"""Real SOCKS5 protocol checks with a local fixture and synthetic credentials."""
import socketserver
import threading
from contextlib import contextmanager

import pytest
import requests

from antidetect.models import ProxyConfig
from antidetect.proxy import parse_proxy_line


def _read_exact(sock, length):
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise EOFError("SOCKS fixture peer disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


@contextmanager
def socks_fixture(*, auth, reject=False):
    observed = {}
    finished = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            try:
                version, count = _read_exact(self.request, 2)
                methods = _read_exact(self.request, count)
                assert version == 5 and (2 if auth else 0) in methods
                self.request.sendall(bytes([5, 2 if auth else 0]))
                if auth:
                    version, length = _read_exact(self.request, 2)
                    assert version == 1
                    observed["username"] = _read_exact(self.request, length).decode()
                    length = _read_exact(self.request, 1)[0]
                    observed["password"] = _read_exact(self.request, length).decode()
                    self.request.sendall(bytes([1, 1 if reject else 0]))
                    if reject:
                        return
                version, command, _reserved, address_type = _read_exact(self.request, 4)
                assert (version, command, address_type) == (5, 1, 3)
                length = _read_exact(self.request, 1)[0]
                observed["destination"] = _read_exact(self.request, length).decode()
                observed["port"] = int.from_bytes(_read_exact(self.request, 2), "big")
                self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(_read_exact(self.request, 1))
                observed["request_line"] = bytes(request).split(b"\r\n", 1)[0].decode()
                body = b'{"transport":"socks5"}'
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                                     + body)
            except Exception as exc:
                observed["fixture_error"] = type(exc).__name__
            finally:
                finished.set()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], observed, finished
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("auth", [False, True])
def test_socks5_uses_remote_dns_and_preserves_credentials(auth):
    username, password = "user+name@example", "p:@%+ space"
    with socks_fixture(auth=auth) as (port, observed, finished):
        config = ProxyConfig(enabled=True, protocol="socks5", host="127.0.0.1", port=port,
                             username=username if auth else None, password=password if auth else None)
        parsed = parse_proxy_line(config.to_url())
        with requests.Session() as session:
            session.trust_env = False
            response = session.get("http://release-fixture.invalid/observation",
                                   proxies={"http": parsed.to_url()}, timeout=5)
        assert response.json() == {"transport": "socks5"}
        assert finished.wait(5)
        assert "fixture_error" not in observed
        assert observed["destination"] == "release-fixture.invalid"
        assert observed["port"] == 80
        assert observed["request_line"] == "GET /observation HTTP/1.1"
        if auth:
            assert (observed["username"], observed["password"]) == (username, password)


def test_rejected_socks_auth_does_not_attempt_direct_connection():
    direct_connection = threading.Event()

    class Origin(socketserver.BaseRequestHandler):
        def handle(self):
            direct_connection.set()
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    origin = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Origin)
    origin.daemon_threads = True
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    try:
        with socks_fixture(auth=True, reject=True) as (port, observed, finished):
            config = ProxyConfig(enabled=True, protocol="socks5", host="127.0.0.1", port=port,
                                 username="fixture", password="bad-fixture-password")
            with requests.Session() as session:
                session.trust_env = False
                with pytest.raises(requests.exceptions.ConnectionError):
                    session.get(f"http://127.0.0.1:{origin.server_address[1]}/",
                                proxies={"http": config.to_url()}, timeout=5)
            assert finished.wait(5)
            assert "destination" not in observed
            assert "fixture_error" not in observed
    finally:
        origin.shutdown()
        origin.server_close()
        thread.join(5)
    assert not direct_connection.is_set()
