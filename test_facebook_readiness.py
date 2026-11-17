"""Deterministic CDP regressions for blank documents and delayed Facebook SPA UI."""
import asyncio
import json
import sys
import types

import pytest

from antidetect import facebook
from antidetect.fanpage_scan import evaluate_discovery
from test_facebook_cdp import _fake_http, _listing_snap, _routes


LISTING = "https://www.facebook.com/bookmarks/pages"
PAGE = {"id": "10000000001", "name": "Fixture page", "url": "https://www.facebook.com/10000000001"}


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


def _collect(monkeypatch, snapshot_at, timeout=20):
    clock = Clock()
    requests = _fake_http(monkeypatch, _routes(None))
    monkeypatch.setattr(facebook, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(facebook.asyncio, "sleep", clock.sleep)

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, message):
            self.command = json.loads(message)

        async def recv(self):
            snapshot = snapshot_at(clock.now)
            if isinstance(snapshot, Exception):
                raise snapshot
            if "cdp_error" in snapshot:
                return json.dumps({"id": self.command["id"], "error": {"message": snapshot["cdp_error"]}})
            return json.dumps({"id": self.command["id"], "result": {
                "result": {"value": snapshot}}})

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda *a, **kw: Socket()))
    result = asyncio.run(facebook.fetch_pages_cdp(9333, timeout=timeout))
    closed = [url for _, url in requests if "/json/close/" in url]
    assert len(closed) == 1 and closed[0].endswith("/json/close/tab-1")
    return result, clock.now


def _shell(**overrides):
    return _listing_snap(readyState="complete", bodyTextLength=0,
                         uiRecognized=False, **overrides)


@pytest.mark.parametrize("initial_url", [LISTING, "about:blank", "https://www.facebook.com/"])
def test_delayed_blank_spa_is_not_classified_before_listing_appears(monkeypatch, initial_url):
    def snapshot(at):
        if at < 11:
            return _shell(url=initial_url)
        return _listing_snap(readyState="complete", bodyTextLength=1024, pages=[PAGE], endMarker=True)

    result, elapsed = _collect(monkeypatch, snapshot)
    assert elapsed >= 11
    assert result["completion_reason"] == "END_MARKER"
    assert result["discovery_status"] == "complete"
    assert result["pages"] == [{**PAGE, "id_status": "verified"}]
    assert result["logged_in"] is True
    assert result["scroll_rounds"] == 1


def test_loading_list_skeleton_does_not_establish_login_or_stability(monkeypatch):
    skeleton = _listing_snap(readyState="complete", bodyTextLength=30,
                             uiRecognized=True, loading=True)
    ready = _listing_snap(readyState="complete", bodyTextLength=1024, pages=[PAGE], endMarker=True)
    result, elapsed = _collect(monkeypatch, lambda at: skeleton if at < 11 else ready)
    assert elapsed >= 11
    assert result["completion_reason"] == "END_MARKER"
    assert result["observed_count"] == 1
    assert result["scroll_rounds"] == 1


@pytest.mark.parametrize("shell", [
    _shell(), _shell(url="about:blank"),
    _listing_snap(readyState="loading", bodyTextLength=30, uiRecognized=False),
    _listing_snap(readyState="complete", bodyTextLength=30, loading=True, uiRecognized=False),
    _listing_snap(readyState="complete", bodyTextLength=30, loading=True, uiRecognized=True),
])
def test_perpetual_shell_reaches_deadline_without_false_login_or_empty(monkeypatch, shell):
    result, elapsed = _collect(monkeypatch, lambda _: shell, timeout=40)
    assert 40 <= elapsed < 41
    assert result["completion_reason"] == "DEADLINE"
    assert result["discovery_status"] == "failed"
    assert result["pages"] == [] and result["observed_count"] == 0
    assert result["logged_in"] is None and result["login_status"] is None
    assert result["identity_status"] == "unknown"
    assert result["scroll_rounds"] == 0
    assert result["rounds"] > 50  # Readiness polls are not scroll attempts.


@pytest.mark.parametrize("transient", [
    asyncio.TimeoutError(), {"cdp_error": "Cannot find context with id"},
])
def test_navigation_context_can_recover_after_more_than_three_failed_rounds(monkeypatch, transient):
    ready = _listing_snap(readyState="complete", bodyTextLength=100, pages=[PAGE], endMarker=True)
    result, elapsed = _collect(monkeypatch, lambda at: transient if at < 11 else ready)
    assert elapsed >= 11
    assert result["completion_reason"] == "END_MARKER"
    assert result["observed_count"] == 1


@pytest.mark.parametrize("terminal", [
    _shell(url="https://www.facebook.com/login/"),
    _shell(loggedOut=True),
    _shell(url="https://www.facebook.com/checkpoint/123"),
])
def test_login_or_checkpoint_interrupts_wait_without_using_full_budget(monkeypatch, terminal):
    result, elapsed = _collect(monkeypatch, lambda at: _shell() if at < 2 else terminal)
    assert 2 <= elapsed < 3
    assert result["discovery_status"] == "failed"
    assert result["completion_reason"] in {"LOGIN_LOST", "NAVIGATION_ERROR"}
    assert result["logged_in"] is not True
    assert result["pages"] == []


def test_recognized_listing_still_stops_after_three_stable_rounds(monkeypatch):
    snapshot = _listing_snap(readyState="complete", bodyTextLength=1024, pages=[PAGE])
    result, elapsed = _collect(monkeypatch, lambda _: snapshot)
    assert result["completion_reason"] == "NO_END_SIGNAL"
    assert result["discovery_status"] == "partial"
    assert result["rounds"] == result["scroll_rounds"] == 4
    assert elapsed < 3
    assert len(result["pages"]) == 1


def test_loaded_nonempty_unrecognized_ui_is_not_false_complete(monkeypatch):
    snapshot = _listing_snap(readyState="complete", bodyTextLength=1024, uiRecognized=False)
    result, _ = _collect(monkeypatch, lambda _: snapshot)
    assert result["completion_reason"] == "UI_UNRECOGNIZED"
    assert result["discovery_status"] == "failed"
    assert result["rounds"] == 5
    assert result["logged_in"] is None


def test_unknown_login_at_readiness_deadline_is_not_reported_as_logout():
    result = evaluate_discovery(pages=[], ui_recognized=False, logged_in=None, deadline_hit=True)
    assert result["completion_reason"] == "DEADLINE"
    assert result["discovery_status"] == "failed"
    assert result["login_status"] is None
