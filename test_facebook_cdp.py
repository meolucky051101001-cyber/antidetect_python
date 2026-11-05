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
    return {"id": 62001, "result": {"result": {"value": value}}}


def _listing_snap(**overrides):
    snap = {
        "url": "https://www.facebook.com/bookmarks/pages",
        "loggedOut": False,
        "uiRecognized": True,
        "loading": False,
        "pages": [],
        "total": None,
        "endMarker": False,
        "emptyConfirmed": False,
        "accountId": None,
    }
    snap.update(overrides)
    return snap


def test_fetch_pages_cdp_reads_pages_and_closes_tab(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [
            {"id": 1, "method": "Page.loadEventFired"},
            _eval_ok(_listing_snap(
                pages=[{"name": "Page", "url": "https://www.facebook.com/mypage"}],
                endMarker=True,
            )),
        ]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    assert got["pages"][0]["name"] == "Page"
    assert got["pages"][0]["url"] == "https://www.facebook.com/mypage"
    assert got["logged_in"] is True
    assert got["discovery_status"] == "complete"
    assert got["completion_reason"] == "END_MARKER"
    assert got["observed_count"] == 1
    assert got["scan_id"] and got["collector_version"]
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
    module = _fake_websockets([_eval_ok(_listing_snap(loggedOut=True))])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    assert got["logged_in"] is False
    assert got["discovery_status"] == "failed"
    assert got["completion_reason"] == "LOGIN_LOST"
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
            {"id": 62001, "error": {"message": "Cannot find context with id"}},
            {"id": 62002, "result": {"result": {"value": _listing_snap(
                pages=[{"name": "Retry", "url": "https://www.facebook.com/retry"}],
                endMarker=True,
            )}}},
        ]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    assert got["pages"][0]["name"] == "Retry"
    assert any("/json/close/tab-1" in url for _, url in calls)


def test_fetch_pages_cdp_raises_and_closes_on_cdp_error(monkeypatch):
    _no_sleep(monkeypatch)
    calls = _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [{"id": 62001, "error": {"message": "Invalid parameters"}}]
    )
    monkeypatch.setitem(sys.modules, "websockets", module)

    with pytest.raises(RuntimeError, match="CDP từ chối"):
        asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))
    assert any("/json/close/tab-1" in url for _, url in calls)


def test_fetch_pages_cdp_raises_on_page_exception(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _routes(None)[:3])
    module = _fake_websockets(
        [{"id": 62001, "result": {"exceptionDetails": {"text": "boom"}}}]
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
    module = _fake_websockets([_eval_ok(_listing_snap(emptyConfirmed=True))])
    monkeypatch.setitem(sys.modules, "websockets", module)

    asyncio.run(facebook.fetch_pages_cdp(9333, timeout=2.0))

    close_methods = [method for method, url in calls if "/json/close/tab-1" in url]
    assert close_methods == ["PUT", "GET"]


def test_fetch_pages_cdp_accumulates_across_rounds_without_end_signal(monkeypatch):
    """Page rời DOM giữa các vòng vẫn được giữ; 3 vòng stable chỉ là partial."""
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _routes(None)[:3])
    url = "https://www.facebook.com/bookmarks/pages"
    snaps = [
        _listing_snap(url=url, pages=[{"name": "P1", "url": "https://www.facebook.com/p1"}], loading=True),
        _listing_snap(url=url, pages=[{"name": "P2", "url": "https://www.facebook.com/p2"}]),
        _listing_snap(url=url, pages=[{"name": "P3", "url": "https://www.facebook.com/p3"}]),
        _listing_snap(url=url, pages=[{"name": "P3", "url": "https://www.facebook.com/p3"}]),
        _listing_snap(url=url, pages=[{"name": "P3", "url": "https://www.facebook.com/p3"}]),
        _listing_snap(url=url, pages=[{"name": "P3", "url": "https://www.facebook.com/p3"}]),
    ]
    module = _fake_websockets([
        _eval_with_id(62001 + offset, snap) for offset, snap in enumerate(snaps)
    ])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=5.0))

    assert [p["name"] for p in got["pages"]] == ["P1", "P2", "P3"]
    assert got["discovery_status"] == "partial"
    assert got["completion_reason"] == "NO_END_SIGNAL"
    assert got["stability"] == "stable"
    assert got["rounds"] == 6


def test_fetch_pages_cdp_fails_on_identity_mismatch(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _routes(None)[:3])
    url = "https://www.facebook.com/bookmarks/pages"
    module = _fake_websockets([
        _eval_ok(_listing_snap(
            url=url,
            accountId="22222",
            pages=[{"name": "P1", "url": "https://www.facebook.com/p1"}],
        )),
    ])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(
        facebook.fetch_pages_cdp(
            9333, timeout=2.0, expected_account_id="11111"
        )
    )

    assert got["discovery_status"] == "failed"
    assert got["completion_reason"] == "LOGIN_IDENTITY_MISMATCH"
    assert got["pages"] == []


def test_fetch_pages_cdp_aborts_without_closing_user_tabs(monkeypatch):
    _no_sleep(monkeypatch)
    handlers = [
        (lambda url, method: "/json/new" in url, {"id": "tab-1"}),
        (
            lambda url, method: "/json/list" in url,
            [
                {
                    "id": "tab-user",
                    "url": "https://www.facebook.com/",
                    "webSocketDebuggerUrl": "ws://tab-user",
                },
                {
                    "id": "tab-1",
                    "url": "https://www.facebook.com/bookmarks/pages",
                    "webSocketDebuggerUrl": "ws://tab-1",
                },
            ],
        ),
        (lambda url, method: "/json/close/tab-1" in url, {}),
    ]
    calls = _fake_http(monkeypatch, handlers)
    module = _fake_websockets([])
    monkeypatch.setitem(sys.modules, "websockets", module)

    probes = {"n": 0}

    def should_abort():
        # Cho phép đọc /json/list một lần, abort ở vòng quét đầu tiên.
        probes["n"] += 1
        return probes["n"] > 1

    with pytest.raises(facebook.ScanAborted):
        asyncio.run(
            facebook.fetch_pages_cdp(
                9333, timeout=2.0, should_abort=should_abort
            )
        )

    closed = [url for method, url in calls if "/json/close/" in url]
    assert closed and all("tab-1" in url for url in closed)
    assert not any("tab-user" in url for url in closed)


def _deep_routes():
    return [
        (lambda url, method: "/json/new" in url, {"id": "tab-1"}),
        (
            lambda url, method: "/json/list" in url,
            [{"id": "tab-1", "url": "about:blank", "webSocketDebuggerUrl": "ws://tab-1"}],
        ),
        (lambda url, method: "/json/close/tab-1" in url, {}),
    ]


def _eval_with_id(msg_id, value):
    return {"id": msg_id, "result": {"result": {"value": value}}}


def test_fetch_pages_deep_records_observed_at_and_completion(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _deep_routes())
    page_url = "https://www.facebook.com/shoppage"
    about_url = page_url + "/about_profile_transparency"
    module = _fake_websockets([
        _eval_with_id(61002, {
            "url": page_url,
            "title": "Shop",
            "times": [{"n": 2, "u": "d", "raw": "2 ngày trước", "own": True}],
            "createdRaw": "Ngày tạo trang: 01/01/2024",
            "feedReady": True,
            "articleCount": 1,
            "parserVersion": "f3-test",
        }),
        _eval_with_id(61004, {
            "url": about_url,
            "createdRaw": "Ngày tạo trang: 01/01/2024",
            "feedReady": False,
        }),
    ])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_deep_cdp(
        9333, [{"url": page_url}], timeout=2.0
    ))

    entry = got[page_url]
    assert entry["completed"] is True
    assert entry["feed_ready"] is True
    assert entry["times"][0]["raw"] == "2 ngày trước"
    assert entry["created_raw"] == "Ngày tạo trang: 01/01/2024"
    assert entry["observed_at"]
    assert entry["parser_version"] == "f3-test"


def test_fetch_pages_deep_rejects_metrics_from_previous_page(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _deep_routes())
    page_url = "https://www.facebook.com/targetpage"
    about_url = page_url + "/about_profile_transparency"
    module = _fake_websockets([
        # Lần evaluate đầu còn DOM của page trước → phải bỏ và đọc lại.
        _eval_with_id(61002, {
            "url": "https://www.facebook.com/previouspage",
            "times": [{"n": 1, "u": "h", "own": True}],
            "feedReady": True,
        }),
        _eval_with_id(61003, {
            "url": page_url,
            "times": [{"n": 3, "u": "d", "own": True}],
            "feedReady": True,
            "articleCount": 1,
        }),
        _eval_with_id(61005, {
            "url": about_url,
            "createdRaw": "Page created on January 1, 2024",
            "feedReady": False,
        }),
    ])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_deep_cdp(
        9333, [{"url": page_url}], timeout=2.0
    ))

    entry = got[page_url]
    assert entry["completed"] is True
    assert entry["times"] == [{"n": 3, "u": "d", "own": True}]
    assert entry["created_raw"] == "Page created on January 1, 2024"


def test_fetch_pages_deep_discards_data_when_navigation_stays_wrong(monkeypatch):
    _no_sleep(monkeypatch)
    _fake_http(monkeypatch, _deep_routes())
    page_url = "https://www.facebook.com/neverloads"
    module = _fake_websockets([
        _eval_with_id(61002, {
            "url": "https://www.facebook.com/login",
            "times": [{"n": 5, "u": "m", "own": True}],
            "feedReady": True,
        }),
        _eval_with_id(61003, {
            "url": "https://www.facebook.com/login",
            "times": [{"n": 5, "u": "m", "own": True}],
            "feedReady": True,
        }),
        _eval_with_id(61005, {
            "url": "https://www.facebook.com/login",
            "createdRaw": "Page created on January 1, 2024",
            "feedReady": False,
        }),
        _eval_with_id(61006, {
            "url": "https://www.facebook.com/login",
            "createdRaw": "Page created on January 1, 2024",
            "feedReady": False,
        }),
    ])
    monkeypatch.setitem(sys.modules, "websockets", module)

    got = asyncio.run(facebook.fetch_pages_deep_cdp(
        9333, [{"url": page_url}], timeout=2.0
    ))

    entry = got[page_url]
    assert entry["times"] == []
    assert entry["created_raw"] is None
    assert entry["completed"] is False
