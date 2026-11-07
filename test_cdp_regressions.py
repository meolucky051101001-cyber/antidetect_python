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


def test_session_tabs_restore_occurs_after_r4_core_verification():
    src = _read(LAUNCHER_SRC)
    configure = _between(
        src, "async def configure_page(", "async def configure_iframe("
    )
    core_wait_pos = configure.index("core_event.wait")
    blank_branch_pos = configure.index('if target_url == "about:blank":')
    restore_pos = configure.index("self._load_session_tabs")
    assert core_wait_pos < blank_branch_pos < restore_pos


def test_session_tabs_save_load_and_filtering(tmp_path):
    from antidetect.launcher import BrowserLauncher, MAX_RESTORED_TABS

    launcher = object.__new__(BrowserLauncher)
    user_data_dir = str(tmp_path / "user_data")
    os.makedirs(user_data_dir, exist_ok=True)

    raw_urls = [
        "about:blank",
        "chrome://newtab/",
        "http://127.0.0.1:9222/json/version",
        "https://www.facebook.com/",
        "https://www.facebook.com/",  # consecutive duplicate
        "https://www.facebook.com/pages/?category=your_pages",
        "edge://settings",
        "devtools://devtools/bundled/inspector.html",
    ]
    assert launcher._save_session_tabs(user_data_dir, raw_urls) is True
    loaded = launcher._load_session_tabs(user_data_dir)
    assert loaded == [
        "https://www.facebook.com/",
        "https://www.facebook.com/pages/?category=your_pages",
    ]

    # Empty / about:blank-only updates must NOT wipe out previously saved tabs
    # when the browser window is closing and detaching tabs.
    assert launcher._save_session_tabs(user_data_dir, ["about:blank", "chrome://newtab/"]) is False
    assert launcher._load_session_tabs(user_data_dir) == [
        "https://www.facebook.com/",
        "https://www.facebook.com/pages/?category=your_pages",
    ]

    # Cap at MAX_RESTORED_TABS
    many_urls = [f"https://example.com/page-{i}" for i in range(MAX_RESTORED_TABS + 5)]
    assert launcher._save_session_tabs(user_data_dir, many_urls) is True
    assert len(launcher._load_session_tabs(user_data_dir)) == MAX_RESTORED_TABS


def test_snapshot_tabs_via_http_ignores_transient_scan_tabs(tmp_path, monkeypatch):
    import io
    import json
    import urllib.request
    from antidetect.facebook import TRANSIENT_TARGET_IDS
    from antidetect.launcher import BrowserLauncher

    launcher = object.__new__(BrowserLauncher)
    user_data_dir = str(tmp_path / "user_data")
    os.makedirs(user_data_dir, exist_ok=True)

    fake_targets = [
        {
            "id": "scan-temp-tab",
            "type": "page",
            "url": "https://www.facebook.com/bookmarks/pages",
        },
        {
            "id": "user-tab-2",
            "type": "page",
            "url": "https://www.facebook.com/myfanpage",
        },
        {
            "id": "user-tab-1",
            "type": "page",
            "url": "https://www.facebook.com/",
        },
        {
            "id": "sw-1",
            "type": "service_worker",
            "url": "https://www.facebook.com/sw.js",
        },
    ]

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(fake_targets).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResp())
    TRANSIENT_TARGET_IDS.add("scan-temp-tab")
    try:
        saved = launcher._snapshot_tabs_via_http(9222, user_data_dir)
    finally:
        TRANSIENT_TARGET_IDS.discard("scan-temp-tab")

    assert saved == [
        "https://www.facebook.com/",
        "https://www.facebook.com/myfanpage",
    ]
    assert launcher._load_session_tabs(user_data_dir) == [
        "https://www.facebook.com/",
        "https://www.facebook.com/myfanpage",
    ]


def test_prepare_clean_chrome_preferences_clears_crash_flag(tmp_path):
    import json
    from antidetect.launcher import BrowserLauncher

    user_data_dir = tmp_path / "user_data"
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True)
    prefs_file = default_dir / "Preferences"
    prefs_file.write_text(
        json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False, "name": "P1"}}),
        encoding="utf-8",
    )

    BrowserLauncher._prepare_clean_chrome_preferences(str(user_data_dir))
    updated = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert updated["profile"]["exit_type"] == "Normal"
    assert updated["profile"]["exited_cleanly"] is True
    assert updated["profile"]["name"] == "P1"

