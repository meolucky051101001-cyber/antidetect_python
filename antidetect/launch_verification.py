"""Generation-bound launch result and a transient local verification origin."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import uuid


class LaunchVerification:
    def __init__(self, launch_id):
        self.launch_id = launch_id
        self.state = "pending"
        self._event = threading.Event()
        self._lock = threading.Lock()

    def complete(self, launch_id, verified):
        with self._lock:
            if launch_id != self.launch_id or self.state != "pending":
                return False
            self.state = "verified" if verified else "failed"
            self._event.set()
            return True

    def wait(self, timeout):
        if not self._event.wait(timeout):
            with self._lock:
                if self.state == "pending":
                    self.state = "timeout"
                    self._event.set()
        return self.state == "verified"


@contextmanager
def verification_origin():
    """Serve no user data; the origin only enables native secure-context APIs."""
    path = "/__antidetect_verify__/" + uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != path:
                self.send_error(404)
                return
            body = b"<!doctype html><title>Browser configuration check</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Accept-CH", "Sec-CH-UA-Platform, Sec-CH-UA-Full-Version-List")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{path}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
