"""CDP Fanpage helpers với Chrome/websocket giả lập."""
import asyncio
import json
import sys
import types

import pytest

from antidetect import facebook


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _fake_http(monkeypatch, handlers):
    calls = []

    def fake_urlopen(req, timeout=5):
        url = req.full_url
        method = req.get_method()
        calls.append((method, url))
        for predicate, payload in handlers:
            if predicate(url, method):
                if isinstance(payload, Exception):
                    raise payload
                return _Response(payload)
        raise AssertionError(f"URL không mong đợi: {method} {url}")

    monkeypatch.setattr(facebook.urllib.request, "urlopen", fake_urlopen)
    return calls


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
    connector = types.SimpleNamespace(urls=[])

    def connect(url, **kwargs):
        connector.urls.append(url)
        return _FakeWebSocket(messages)

    module.connect = connect
    module.connector = connector
    return module


def _no_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(facebook.asyncio, "sleep", _sleep)


def _routes(close_payload=None):
    close_payload = {} if close_payload is None else close_payload

    return [
        (lambda url, method: "/json/new" in url, {"id": "tab-1"}),
        (
            lambda url, method: "/json/list" in url,
            [
                {
                    "id": "tab-1",
                    "url": "https://www.facebook.com/bookmarks/pages",
                    "webSocketDebuggerUrl": "ws://tab-1",
                }
            ],
        ),
        (
            lambda url, method: "/json/close/tab-1" in url,
            close_payload,
        ),
    ]


def _eval_ok(value):
    return {"id": 60001, "result": {"result": {"value": value}}}


def test_fetch_pages_cdp_reads_pages_and_closes_tab(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [
            {"id": 1, "method": "Page.loadEventFired"},
            _eval_ok({"pages": [{"name": "Page"}], "loggedOut": False}),
        ]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    assert got["pages"] == [{"name": "Page"}]
    assert got["logged_in"] is True
    assert module.connector.urls == ["ws://tab-1"]
    assert calls[0][0] == "PUT"
    assert any("/json/close/tab-1" in url for _, url in calls)


def test_fetch_pages_cdp_falls_back_to_get_when_put_fails(monkeypatch):
    _no_sleep(monkeypatch)
    handlers = [
        (
            lambda url, method: "/json/new" in url and method == "PUT",
            RuntimeError("PUT unsupported"),
        ),
        (lambda url, method: "/json/new" in url, {"id": "tab-1"}),
        *(_routes(None)[1:3]),
    ]
    calls = _fake_http(monkeypatch, handlers)
    module = _fake_websockets([_eval_ok({"pages": [], "loggedOut": True})])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    assert got["logged_in"] is False
    assert [method for method, url in calls[:2] if "/json/new" in url] == ["PUT", "GET"]


def test_fetch_pages_cdp_rejects_missing_target(monkeypatch):
    _fake_http(monkeypatch, [(lambda url, method: "/json/new" in url, {})])
    with pytest.raises(RuntimeError, match="tab mới"):
        asyncio.run(facebook.fetch_pages_cdp(9333, timeout=1.0))


def test_fetch_pages_cdp_retries_when_context_not_ready(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [
            {"id": 60001, "error": {"message": "Cannot find context with id"}},
            _eval_ok({"pages": [{"name": "Retry"}], "loggedOut": False}),
        ]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    assert got["pages"] == [{"name": "Retry"}]
    assert any("/json/close/tab-1" in url for _, url in calls)


def test_fetch_pages_cdp_raises_and_closes_on_cdp_error(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [{"id": 60001, "error": {"message": "Invalid parameters"}}]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    with pytest.raises(RuntimeError, match="CDP từ chối"):
        asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))
    assert any("/json/close/tab-1" in url for _, url in calls)


def test_fetch_pages_cdp_raises_on_page_exception(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [{"id": 60001, "result": {"exceptionDetails": {"text": "boom"}}}]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    with pytest.raises(RuntimeError, match="Lỗi đọc trang"):
        asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))


def test_fetch_pages_cdp_times_out_without_websocket(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(
        monkeypatch,
        [
            (lambda url, method: "/json/new" in url, {"id": "tab-1"}),
            (
                lambda url, method: "/json/list" in url,
                [{"id": "tab-1", "url": "about:blank"}],
            ),
            (lambda url, method: "/json/close/tab-1" in url, {}),
        ],
    )
    with pytest.raises(RuntimeError, match="không tải được"):
        asyncio.run(facebook.fetch_pages_cdp(9333, timeout=0.05))


def test_fetch_pages_cdp_close_falls_back_to_get(monkeypatch):
    _no_sleep(monkeypatch)
    handlers = [
        (lambda url, method: "/json/new" in url, {"id": "tab-1"}),
        *(_routes(None)[1:2]),
        (
            lambda url, method: "/json/close/tab-1" in url and method == "PUT",
            RuntimeError("close failed"),
        ),
        (lambda url, method: "/json/close/tab-1" in url, {}),
    ]
    calls = _fake_http(monkeypatch, handlers)
    module = _fake_websockets([_eval_ok({"pages": [], "loggedOut": False})])
    monkeypatch.setitem(sys.modules, "websockets", module)

    asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    close_methods = [method for method, url in calls if "/json/close/tab-1" in url]
    assert close_methods == ["PUT", "GET"]
