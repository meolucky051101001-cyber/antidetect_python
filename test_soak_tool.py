"""Regression tests cho retry và log lỗi 409 của soak runner."""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pytest

import tools.soak_test as soak


class _Args:
    hours = 1.0
    profiles = 1
    min_active = 0
    port = 1
    data_dir = ""
    results_dir = ""


@pytest.fixture
def runner(tmp_path, monkeypatch):
    args = _Args()
    args.data_dir = str(tmp_path / "data")
    args.results_dir = str(tmp_path / "results")
    os.makedirs(args.results_dir, exist_ok=True)
    instance = soak.SoakRunner(args)
    instance.base = "http://127.0.0.1:1"
    instance.token = "token"
    instance.soak_base = "http://127.0.0.1:2"
    instance.profiles = [
        {"id": "profile-1", "name": "Soak 1", "active": True, "port": 9555}
    ]
    monkeypatch.setattr(soak, "OPEN_TAB_RETRY_DELAY", 0)
    return instance


def _response(status, body):
    return status, body, 0.01, None if status == 200 else f"HTTP {status}"


def test_open_tab_409_logs_code_and_detail(runner, monkeypatch):
    monkeypatch.setattr(
        soak,
        "_http",
        lambda *_a, **_k: _response(
            409,
            {
                "success": False,
                "code": "RECOVERY_REQUIRED",
                "detail": "CDP chưa được xác minh quyền điều khiển.",
            },
        ),
    )
    ok, failure, code = runner._open_soak_tab(runner.profiles[0], "http://soak/x")
    assert ok is False
    assert code == "RECOVERY_REQUIRED"
    assert "HTTP 409" in failure
    assert "CDP chưa được xác minh quyền điều khiển." in failure


def test_open_tab_retries_transient_conflict_once(runner, monkeypatch):
    calls = {"count": 0}

    def fake_http(*_a, **_k):
        calls["count"] += 1
        if calls["count"] == 1:
            return _response(
                409,
                {
                    "success": False,
                    "code": "CDP_UNHEALTHY",
                    "detail": "CDP không khỏe",
                },
            )
        return _response(200, {"success": True})

    monkeypatch.setattr(soak, "_http", fake_http)
    ok, failure, code = runner._open_soak_tab(runner.profiles[0], "http://soak/x")
    assert ok is True
    assert failure is None and code is None
    assert calls["count"] == 2
    events = [
        json.loads(line) for line in open(runner.events_path, encoding="utf-8")
    ]
    assert any(event.get("event") == "open_tab_retry_ok" for event in events)


def test_open_tab_does_not_retry_terminal_conflict(runner, monkeypatch):
    calls = {"count": 0}

    def fake_http(*_a, **_k):
        calls["count"] += 1
        return _response(
            409,
            {
                "success": False,
                "code": "DETACHED_NO_CDP",
                "detail": "Chrome chạy detached.",
            },
        )

    monkeypatch.setattr(soak, "_http", fake_http)
    ok, failure, code = runner._open_soak_tab(runner.profiles[0], "http://soak/x")
    assert ok is False
    assert code == "DETACHED_NO_CDP"
    assert calls["count"] == 1


def test_tab_cycle_records_error_code_after_retry_fails(runner, monkeypatch):
    monkeypatch.setattr(
        soak,
        "_http",
        lambda *_a, **_k: _response(
            409,
            {
                "success": False,
                "code": "CDP_UNHEALTHY",
                "detail": "CDP không khỏe",
            },
        ),
    )
    monkeypatch.setattr(soak, "_close_soak_tabs", lambda *_a, **_k: (0, 0, None))
    runner._tab_cycle()
    assert len(runner.errors) == 1
    record = runner.errors[0]
    assert record["scope"] == "open_tab"
    assert record["code"] == "CDP_UNHEALTHY"
    assert "CDP không khỏe" in record["message"]
