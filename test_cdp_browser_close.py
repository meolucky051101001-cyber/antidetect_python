"""Generation-bound graceful-close requests without any browser or network."""
import threading

import pytest

from antidetect.cdp_health import CdpHealthRegistry, REASON_SOCKET_CLOSED


def bound_session(registry, profile="fixture"):
    launch = registry.begin_launch(profile, "synthetic-owner")
    return launch, registry.bind_session(profile, launch)


def test_close_request_uses_exact_current_open_session_even_while_stopping():
    registry = CdpHealthRegistry()
    launch, session = bound_session(registry)
    calls = []
    handler = lambda: calls.append("queued on original websocket")
    assert session.bind_browser_close(handler)
    assert session.bind_browser_close(handler)
    assert not session.bind_browser_close(lambda: None)
    assert registry.mark_stopping("fixture")
    assert registry.request_browser_close("fixture", launch)
    assert registry.request_browser_close("fixture", launch)
    assert calls == ["queued on original websocket"]
    assert session.is_open(), "A scheduling acknowledgement must not pretend the socket closed."


def test_missing_unbound_closed_and_stale_sessions_do_not_schedule():
    registry = CdpHealthRegistry()
    assert not registry.request_browser_close("missing", "unknown")
    old_launch, old_session = bound_session(registry)
    assert not registry.request_browser_close("fixture", old_launch)
    calls = []
    assert old_session.bind_browser_close(lambda: calls.append("old"))
    launch, session = bound_session(registry)
    assert session.bind_browser_close(lambda: calls.append("current"))
    assert not registry.request_browser_close("fixture", old_launch)
    assert not old_session.request_browser_close()
    assert not old_session.bind_browser_close(lambda: None)
    assert calls == []
    session.mark_closed()
    assert not registry.request_browser_close("fixture", launch)
    assert calls == []


def test_rebinding_a_socket_in_one_generation_invalidates_previous_handler():
    registry = CdpHealthRegistry()
    launch, first = bound_session(registry)
    calls = []
    first.bind_browser_close(lambda: calls.append("first"))
    current = registry.bind_session("fixture", launch)
    current.bind_browser_close(lambda: calls.append("current"))
    assert not first.request_browser_close()
    assert registry.request_browser_close("fixture", launch)
    assert calls == ["current"]


@pytest.mark.parametrize("ending", ["stopped", "recovery", "forgotten", "cleared"])
def test_lifecycle_invalidation_removes_the_close_capability(ending):
    registry = CdpHealthRegistry()
    launch, session = bound_session(registry)
    calls = []
    session.bind_browser_close(lambda: calls.append("unexpected"))
    if ending == "stopped":
        registry.mark_stopped("fixture", launch)
    elif ending == "recovery":
        registry.mark_recovery_required("fixture", REASON_SOCKET_CLOSED,
                                        owner_instance="synthetic-owner", launch_id=launch, force=True)
    elif ending == "forgotten":
        registry.forget_if_launch("fixture", launch)
    else:
        registry.clear()
    assert not registry.request_browser_close("fixture", launch)
    assert not session.request_browser_close()
    assert calls == []


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_unavailable_scheduler_fails_closed_and_can_retry(failure):
    registry = CdpHealthRegistry()
    launch, session = bound_session(registry)
    calls = []

    def handler():
        calls.append(1)
        if len(calls) == 1:
            if failure == "exception":
                raise RuntimeError("synthetic event loop closed")
            return False
        return True

    session.bind_browser_close(handler)
    assert not registry.request_browser_close("fixture", launch)
    assert registry.request_browser_close("fixture", launch)
    assert registry.request_browser_close("fixture", launch)
    assert len(calls) == 2


def test_callback_holds_no_registry_or_session_lock_and_replacement_cancels_request():
    registry = CdpHealthRegistry()
    old_launch, session = bound_session(registry)
    entered, release, replaced = threading.Event(), threading.Event(), threading.Event()
    results = []
    replacement = []

    def handler():
        entered.set()
        return release.wait(3)

    session.bind_browser_close(handler)
    request_thread = threading.Thread(target=lambda: results.append(
        registry.request_browser_close("fixture", old_launch)))

    def replace():
        replacement.append(registry.begin_launch("fixture", "synthetic-owner"))
        replaced.set()

    replacement_thread = threading.Thread(target=replace)
    request_thread.start()
    try:
        assert entered.wait(1)
        assert not registry.request_browser_close("fixture", old_launch), "Concurrent pending dispatch must not be reported accepted."
        replacement_thread.start()
        assert replaced.wait(1), "Close handler blocked the registry/session lock."
    finally:
        release.set()
        request_thread.join(2)
        if replacement_thread.ident is not None:
            replacement_thread.join(2)
    assert results == [False], "A superseded scheduling request must not report a current close."
    assert replacement[0] != old_launch
    assert not registry.request_browser_close("fixture", old_launch)
    assert registry.current_launch_id("fixture") == replacement[0]
