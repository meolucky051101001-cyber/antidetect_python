"""Exercise real HTTP proxy auth transport against an isolated loopback fixture."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import requests

from antidetect.models import ProxyConfig
from antidetect.proxy import parse_proxy_line


def test_percent_encoded_credentials_authenticate_over_real_proxy_socket():
    user, password = "user+name@example", "pass:@%+ space"
    received = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Proxy-Authorization")))
            body = b'{"through_fixture_proxy":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        original = ProxyConfig(enabled=True, host="127.0.0.1", port=server.server_port,
                               username=user, password=password)
        parsed = parse_proxy_line(original.to_url())
        with requests.Session() as session:
            session.trust_env = False
            response = session.get("http://fixture.invalid/observation",
                                   proxies={"http": parsed.to_url()}, timeout=3)
        assert response.json() == {"through_fixture_proxy": True}
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        assert received == [("http://fixture.invalid/observation", expected)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
