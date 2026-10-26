"""Regression tests for the 2026-10-23 P0 fix and R3/R4 behaviour.

Static checks over the launcher source and the dashboard template:

- P0: reader_loop must never await send(); a dedicated keepalive_loop pings
  the browser instead. A send() from inside the reader deadlocks it (send
  waits on a future that only reader_loop resolves), the response timeout
  breaks the loop, and closing the CDP session makes Chrome reset the
  deviceMetrics/timezone/UA overrides after ~35-40s.
- R4: the launch handshake waits for core verification before navigation,
  dispatches Page.loadEventFired, and requires Client Hints on HTTPS.
- R3: the dashboard surfaces data.warnings/data.warning even when the
  launch succeeded (cdp_ready !== false).
"""
import os

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAUNCHER_SRC = os.path.join(BASE_DIR, "antidetect", "launcher.py")
DASHBOARD_JS_SRC = os.path.join(BASE_DIR, "web", "static", "js", "dashboard.js")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _between(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_reader_loop_never_awaits_send():
    src = _read(LAUNCHER_SRC)
    reader = _between(src, "async def reader_loop()", "async def keepalive_loop()")
    # The idle branch must only continue; awaiting send() here deadlocked
    # the reader after 30s and dropped the CDP session (P0).
    timeout_branch = _between(
        reader, "except asyncio.TimeoutError:", "except Exception:"
    )
    assert "send(" not in timeout_branch
    assert "continue" in timeout_branch


def test_keepalive_pings_from_a_separate_task():
    src = _read(LAUNCHER_SRC)
    keepalive = _between(
        src, "async def keepalive_loop()", "reader_task = asyncio.create_task"
    )
    assert "asyncio.sleep(" in keepalive
    assert 'await send("Browser.getVersion")' in keepalive
    # The reader stays the sole consumer of responses; the keepalive runs as
    # its own task and is cancelled when the reader loop exits.
    create_pos = src.index(
        "keepalive_task = asyncio.create_task(keepalive_loop())"
    )
    await_reader_pos = src.index("await reader_task", create_pos)
    cancel_pos = src.index("keepalive_task.cancel()", await_reader_pos)
    assert cancel_pos > await_reader_pos


def test_r4_handshake_waits_for_core_verification_before_navigation():
    src = _read(LAUNCHER_SRC)
    configure = _between(
        src, "async def configure_page(", "async def configure_iframe("
    )
    # cdp_ready is only set after the initial document is resumed...
    resume_pos = configure.index('"Runtime.runIfWaitingForDebugger"')
    ready_pos = configure.index("self._cdp_ready_events[profile.id].set()")
    assert ready_pos > resume_pos
    # ...and navigation waits for the main thread to verify core values on
    # about:blank first, so a slow page cannot invalidate verification.
    core_wait_pos = configure.index("core_event.wait")
    navigate_pos = configure.index('"Page.navigate"')
    assert core_wait_pos < navigate_pos
    # addScriptToEvaluateOnNewDocument only covers future documents; the
    # initial about:blank needs an explicit Runtime.evaluate.
    assert '"Runtime.evaluate"' in configure


def test_r4_load_event_dispatch_feeds_navigation_wait():
    src = _read(LAUNCHER_SRC)
    dispatch = _between(src, "async def dispatch_event(", "async def reader_loop()")
    assert 'method == "Page.loadEventFired"' in dispatch
    assert "self._page_load_events" in dispatch

    configure = _between(
        src, "async def configure_page(", "async def configure_iframe("
    )
    # The navigation wait must be bounded so a hanging page cannot block
    # the nav_event handshake forever.
    assert "load_event.wait()" in configure
    assert "timeout=10.0" in configure
    assert "self._page_load_events.pop(session_id, None)" in configure


def test_r4_https_launch_requires_client_hints():
    src = _read(LAUNCHER_SRC)
    # Core verification on about:blank cannot see secure-context Client
    # Hints; only the post-navigation HTTPS pass may require them.
    assert "require_client_hints=False" in src  # _verify_emulation default
    launch_src = src[src.index("def launch("):]
    https_pos = launch_src.index('target_url.startswith("https://")')
    ch_pos = launch_src.index("require_client_hints=True")
    assert ch_pos > https_pos
    assert 'target_url != "about:blank"' in launch_src


def _js_function_block(html, name):
    start = html.index(f"async function {name}(")
    nxt = html.find("\n        async function ", start + 1)
    return html[start: nxt if nxt != -1 else len(html)]


@pytest.mark.parametrize("fn", ["startProfile", "testFingerprint"])
def test_r3_ui_shows_warnings_even_when_cdp_ready(fn):
    js = _read(DASHBOARD_JS_SRC)
    block = _js_function_block(js, fn)
    # R3: warnings must surface on a successful launch too. They are an
    # else-if chain after the cdp_ready === false branch, not part of the
    # error path, and the success toast comes last.
    not_ready_pos = block.index("data.cdp_ready === false")
    warnings_pos = block.index("else if (data.warnings && data.warnings.length)")
    warning_pos = block.index("else if (data.warning)")
    success_pos = block.index("'success'")
    assert not_ready_pos < warnings_pos < warning_pos < success_pos
