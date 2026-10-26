"""Cookie normalization + CDP cookie helpers, không cần Chrome thật."""
import asyncio
import json
import sys
import types

import pytest

from antidetect import cookies


def _run(coro):
    return asyncio.run(coro)


def test_normalize_cookie_rejects_invalid_and_converts_types():
    assert cookies.normalize_cookie(None) is None
    assert cookies.normalize_cookie({"name": "only-name"}) is None
    assert cookies.normalize_cookie({"value": "only-value"}) is None

    got = cookies.normalize_cookie(
        {
            "name": 123,
            "value": 456,
            "expirationDate": 1_700_000_000_000,
            "sameSite": "no_restriction",
        }
    )
    assert got["name"] == "123"
    assert got["value"] == "456"
    assert got["expires"] == 1_700_000_000.0
    assert got["sameSite"] == "None"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("none", "None"),
        ("lax", "Lax"),
        ("strict", "Strict"),
    ],
)
def test_normalize_cookie_same_site(raw, expected):
    assert cookies.normalize_cookie(
        {"name": "sid", "value": "x", "sameSite": raw}
    )["sameSite"] == expected


def test_normalize_cookie_unspecified_same_site_is_removed():
    got = cookies.normalize_cookie(
        {"name": "sid", "value": "x", "sameSite": "unspecified"}
    )
    assert "sameSite" not in got


def test_normalize_cookies_filters_invalid_and_preserves_order():
    result = cookies.normalize_cookies(
        [
            {"name": "a", "value": "1"},
            {"name": "missing-value"},
            None,
            {"name": "b", "value": "2"},
        ]
    )
    assert [c["name"] for c in result] == ["a", "b"]


class _FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps(self.messages.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_websockets(messages):
    module = types.ModuleType("websockets")

    class _Connector:
        def __init__(self):
            self.urls = []

        def connect(self, url, max_size=None):
            self.urls.append(url)
            return _FakeWebSocket(messages)

    module.connect = _Connector().connect
    return module


def test_fetch_cookies_cdp_skips_unrelated_messages(monkeypatch):
    module = _fake_websockets(
        [
            {"id": 1, "method": "Page.loadEventFired"},
            {"id": 999, "result": {"cookies": [{"name": "a", "value": "1"}]}},
        ]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    async def fake_ws_url(port):
        assert port == 9222
        return "ws://fake"

    monkeypatch.setattr(cookies, "_browser_ws_url", fake_ws_url)
    got = _run(cookies.fetch_cookies_cdp(9222))
    assert got == [{"name": "a", "value": "1"}]


def test_set_cookies_cdp_returns_true(monkeypatch):
    module = _fake_websockets([{"id": 888, "result": {}}])
    monkeypatch.setitem(sys.modules, "websockets", module)

    async def fake_ws_url(port):
        return "ws://fake"

    monkeypatch.setattr(cookies, "_browser_ws_url", fake_ws_url)
    assert _run(cookies.set_cookies_cdp(9222, [{"name": "a", "value": "1"}])) is True


def test_set_cookies_cdp_raises_on_cdp_error(monkeypatch):
    module = _fake_websockets(
        [{"id": 888, "error": {"message": "invalid cookie"}}]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    async def fake_ws_url(port):
        return "ws://fake"

    monkeypatch.setattr(cookies, "_browser_ws_url", fake_ws_url)
    with pytest.raises(RuntimeError, match="Storage.setCookies"):
        _run(cookies.set_cookies_cdp(9222, []))


def test_browser_ws_url_reads_devtools_endpoint(monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
            ).encode("utf-8")

    monkeypatch.setattr(cookies.urllib.request, "urlopen", lambda req, timeout: _Response())
    assert _run(cookies._browser_ws_url(9222)).endswith("/devtools/browser/x")


def test_browser_ws_url_rejects_missing_endpoint(monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(cookies.urllib.request, "urlopen", lambda req, timeout: _Response())
    with pytest.raises(RuntimeError, match="CDP"):
        _run(cookies._browser_ws_url(9222))
