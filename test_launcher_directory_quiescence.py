"""Synthetic parent/child exit and graceful-close regression boundaries."""
import json
import subprocess
from types import SimpleNamespace

import pytest

import antidetect.launcher as launcher_module
from antidetect.launcher import BrowserLauncher
from test_runtime_generation_cleanup import fixture_launcher


@pytest.fixture
def directory_launcher(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    profile_id = "profile-1"
    data_dir = tmp_path / "profiles" / profile_id / "user_data"
    data_dir.mkdir(parents=True)
    sentinel = data_dir / "synthetic-session.txt"
    sentinel.write_text("preserve synthetic data", encoding="utf-8")
    monkeypatch.setattr(launcher_module, "PROFILES_DATA_DIR", str(tmp_path / "profiles"))
    launch_id = launcher.cdp_health.begin_launch(profile_id, launcher.instance_id)
    launcher.cdp_health.set_process(profile_id, launch_id, 424242)
    launcher._runtime_store.upsert(
        profile_id, pid=424242, launch_id=launch_id, owner_instance=launcher.instance_id,
        user_data_dir=str(data_dir), port=9333,
    )
    launcher.active_ports[profile_id] = 9333
    launcher._snapshot_tabs_via_http = lambda *a: []
    return launcher, profile_id, launch_id, sentinel


def _immediate_scan(launcher, monkeypatch, observed):
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a, **_k: observed)
    monkeypatch.setattr(
        launcher, "_wait_directory_idle",
        lambda directory, timeout=5: BrowserLauncher._wait_directory_idle(directory, timeout=0),
    )


@pytest.mark.parametrize("observed", [{424243}, None], ids=["child-running", "scan-unknown"])
@pytest.mark.parametrize("branch", ["parent-exited", "identity-absent", "missing-pid"])
def test_stop_refuses_success_without_directory_quiescence(directory_launcher, monkeypatch, observed, branch):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    _immediate_scan(launcher, monkeypatch, observed)
    if branch == "parent-exited":
        launcher.active_processes[profile_id] = SimpleNamespace(pid=424242, poll=lambda: 0)
    elif branch == "identity-absent":
        monkeypatch.setattr(launcher, "_identity_for_stop", lambda *a: {"state": "absent"})
    else:
        launcher._runtime_store.remove(profile_id)
    result = launcher.stop(profile_id)
    assert not result["success"]
    assert result["code"] == "STOP_FAILED"
    if branch != "missing-pid":
        record = launcher._runtime_store.get(profile_id)
        assert record["pid"] == 424242 and record["launch_id"] == launch_id
    assert sentinel.read_text(encoding="utf-8") == "preserve synthetic data"


@pytest.mark.parametrize("observed", [{424243}, None], ids=["child-running", "scan-unknown"])
def test_clear_preserves_runtime_and_local_handles_while_directory_not_idle(directory_launcher, monkeypatch, observed):
    launcher, profile_id, _launch_id, sentinel = directory_launcher
    proc = SimpleNamespace(pid=424242, poll=lambda: 0)
    launcher.active_processes[profile_id] = proc
    record = launcher._runtime_store.get(profile_id)
    _immediate_scan(launcher, monkeypatch, observed)
    assert not launcher._clear_process(profile_id, expected_record=record)
    assert launcher._runtime_store.get(profile_id) == record
    assert launcher.active_processes[profile_id] is proc
    assert launcher.active_ports[profile_id] == 9333
    assert sentinel.exists()


def test_late_child_exit_allows_clear_after_fresh_scan(directory_launcher, monkeypatch):
    launcher, profile_id, _launch_id, sentinel = directory_launcher
    scans = []
    def scan(_directory):
        scans.append(True)
        return {424243} if len(scans) == 1 else set()
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", scan)
    monkeypatch.setattr(launcher_module.time, "sleep", lambda *_a: None)
    launcher.active_processes[profile_id] = SimpleNamespace(pid=424242, poll=lambda: 0)
    assert launcher._clear_process(profile_id)
    assert len(scans) == 2
    assert launcher._runtime_store.get(profile_id) is None
    assert profile_id not in launcher.active_processes
    assert profile_id not in launcher.active_ports
    assert sentinel.exists()


@pytest.mark.parametrize("results", [[None, set()], [None, {424243}, set()]])
def test_transient_unknown_scan_requires_later_confirmed_empty(tmp_path, monkeypatch, results):
    observations = iter(results)
    calls = []
    def scan(directory):
        calls.append(directory)
        return next(observations)
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", scan)
    monkeypatch.setattr(launcher_module.time, "sleep", lambda *_a: None)
    assert BrowserLauncher._wait_directory_idle(str(tmp_path))
    assert len(calls) == len(results)


def test_identity_for_falsy_pid_is_absent(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(
        launcher_module, "read_browser_identity",
        lambda *_a, **_k: pytest.fail("a falsy PID is already absent"),
    )

    assert launcher._identity_for(0, str(tmp_path)) == {
        "state": "absent", "port": None, "created": None,
    }


def test_identity_for_stop_retries_unknown_until_non_unknown(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    identities = iter([
        {"state": "unknown"},
        {"state": "mismatch"},
        {"state": "owned"},
    ])
    calls = []
    sleeps = []
    monkeypatch.setattr(
        launcher, "_identity_for",
        lambda pid, directory: calls.append((pid, directory)) or next(identities),
    )
    monkeypatch.setattr(launcher_module.time, "sleep", sleeps.append)

    result = launcher._identity_for_stop(42, str(tmp_path), attempts=5)

    assert result == {"state": "mismatch"}
    assert calls == [(42, str(tmp_path))] * 2
    assert sleeps == [0.2]


def test_identity_for_owner_retries_unknown_until_identity_is_known(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    identities = iter([{"state": "unknown"}, {"state": "owned"}])
    sleeps = []
    monkeypatch.setattr(
        launcher, "_identity_for", lambda *_a: next(identities),
    )
    monkeypatch.setattr(launcher_module.time, "sleep", sleeps.append)

    assert launcher._identity_for_owner(42, str(tmp_path), timeout=1) == {"state": "owned"}
    assert sleeps == [0.25]


def test_identity_for_stop_returns_unknown_after_all_attempts(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    sleeps = []
    monkeypatch.setattr(
        launcher, "_identity_for", lambda *_a: {"state": "unknown"},
    )
    monkeypatch.setattr(launcher_module.time, "sleep", sleeps.append)

    assert launcher._identity_for_stop(42, str(tmp_path), attempts=2) == {
        "state": "unknown",
    }
    assert sleeps == [0.2]


def test_wait_directory_idle_returns_true_for_missing_or_empty_directory(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        launcher_module, "browser_pids_using_dir",
        lambda *_a, **_k: pytest.fail("missing directory needs no scan"),
    )
    assert BrowserLauncher._wait_directory_idle(None)

    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda _directory: set())
    assert BrowserLauncher._wait_directory_idle(str(tmp_path))


@pytest.mark.parametrize("scan_result", [None, {424243}], ids=["unknown", "process-remains"])
def test_wait_directory_idle_returns_false_at_deadline(tmp_path, monkeypatch, scan_result):
    monkeypatch.setattr(
        launcher_module, "browser_pids_using_dir", lambda _directory: scan_result,
    )
    times = iter([0.0, 1.0])
    monkeypatch.setattr(launcher_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(launcher_module.time, "sleep", lambda *_a: None)

    assert not BrowserLauncher._wait_directory_idle(str(tmp_path), timeout=0.5)


@pytest.mark.parametrize(
    "branch",
    [
        "local-mismatch", "not-idle", "record-changed", "remove-error",
        "success", "extension-error",
    ],
)
def test_clear_process_locked_branches(tmp_path, monkeypatch, branch):
    launcher = fixture_launcher(tmp_path)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id="generation-A",
        owner_instance="owner-A",
        pid=424242,
        port=9444,
        requested_port=9333,
        user_data_dir=str(tmp_path),
    )
    if branch == "local-mismatch":
        launcher.cdp_health.begin_launch("profile-1", "generation-B")
        assert not launcher._clear_process_locked("profile-1")
        assert launcher._runtime_store.get("profile-1") == record
        return

    monkeypatch.setattr(
        launcher, "_wait_directory_idle",
        lambda _directory: branch != "not-idle",
    )
    if branch == "record-changed":
        monkeypatch.setattr(launcher._runtime_store, "remove_if_matches", lambda *_a: False)
    elif branch == "remove-error":
        def fail_remove(*_args):
            raise OSError("runtime store unavailable")
        monkeypatch.setattr(launcher._runtime_store, "remove_if_matches", fail_remove)
    released = []
    monkeypatch.setattr(launcher_module, "release_port", released.append)

    if branch in ("success", "extension-error"):
        if branch == "extension-error":
            launcher.manager.get_profile_dir = lambda _profile_id: (_ for _ in ()).throw(
                OSError("profile directory unavailable")
            )
        process = SimpleNamespace(pid=424242, poll=lambda: None)
        launcher.active_processes["profile-1"] = process
        launcher.active_ports["profile-1"] = 9444
        launcher._requested_ports["profile-1"] = 9333
        launcher._recovery_required.add("profile-1")
        assert launcher._clear_process_locked("profile-1", expected_record=record)
        assert launcher._runtime_store.get("profile-1") is None
        assert "profile-1" not in launcher.active_processes
        assert "profile-1" not in launcher._recovery_required
        assert released == [9333, 9444]
        return

    assert not launcher._clear_process_locked("profile-1", expected_record=record)
    assert launcher._runtime_store.get("profile-1") == record
    assert released == []


@pytest.mark.parametrize(
    "identity",
    [
        {"state": "owned", "created": "different", "birth_ticks": 10, "port": 9444},
        {"state": "owned", "created": "stored", "birth_ticks": 11, "port": 9444},
    ],
    ids=["created-mismatch", "birth-mismatch"],
)
def test_recovery_rejects_mismatched_process_identity(tmp_path, monkeypatch, identity):
    launcher = fixture_launcher(tmp_path)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id="generation-A",
        owner_instance="owner-A",
        pid=424242,
        port=9333,
        user_data_dir=str(tmp_path),
        created="stored",
        birth_ticks=10,
    )
    monkeypatch.setattr(launcher_module, "read_browser_identity", lambda *_a, **_k: identity)
    clear_calls = []
    monkeypatch.setattr(
        launcher, "_clear_process",
        lambda profile_id, *, expected_record: clear_calls.append(
            (profile_id, expected_record)
        ) or False,
    )

    assert launcher._recover_runtime("profile-1") == record
    assert clear_calls == [("profile-1", record)]
    assert "profile-1" not in launcher._recovery_required


def test_recovery_of_owned_process_restores_port_and_requires_recovery(
    tmp_path, monkeypatch,
):
    launcher = fixture_launcher(tmp_path)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id="generation-A",
        owner_instance="owner-A",
        pid=424242,
        port=9333,
        user_data_dir=str(tmp_path),
        created="stored",
        birth_ticks=10,
    )
    monkeypatch.setattr(launcher_module, "read_browser_identity", lambda *_a, **_k: {
        "state": "owned", "created": "stored", "birth_ticks": 10, "port": 9444,
    })

    assert launcher._recover_runtime("profile-1") == record
    assert launcher.active_ports["profile-1"] == 9444
    assert "profile-1" in launcher._recovery_required


def test_recovery_keeps_owned_state_when_runtime_write_fails(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id="generation-A",
        owner_instance="owner-A",
        pid=424242,
        port=9333,
        user_data_dir=str(tmp_path),
        created="stored",
    )
    monkeypatch.setattr(launcher_module, "read_browser_identity", lambda *_a, **_k: {
        "state": "owned", "created": "stored", "port": 9444,
    })
    monkeypatch.setattr(
        launcher._runtime_store, "upsert_if_owner",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("runtime store unavailable")),
    )

    assert launcher._recover_runtime("profile-1") == record
    assert launcher.active_ports["profile-1"] == 9444
    assert "profile-1" in launcher._recovery_required


def test_persist_health_now_rejects_stale_state_and_logs_write_failure(
    tmp_path, monkeypatch,
):
    launcher = fixture_launcher(tmp_path)
    healthy = {
        "state": launcher_module.STATE_HEALTHY,
        "owner_instance": launcher.instance_id,
        "pid": 424242,
    }

    assert not launcher._persist_health_now("profile-1", None, healthy)
    assert not launcher._persist_health_now("profile-1", "generation-A", {})
    assert not launcher._persist_health_now(
        "profile-1", "generation-A", {"state": launcher_module.STATE_STOPPED}
    )
    assert not launcher._persist_health_now(
        "profile-1",
        "generation-A",
        {**healthy, "owner_instance": "owner-B"},
    )
    monkeypatch.setattr(
        launcher._runtime_store, "upsert_if_owner",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("runtime store unavailable")),
    )

    assert not launcher._persist_health_now("profile-1", "generation-A", healthy)
    assert "profile-1:OSError" in launcher._health_persist_warned


def test_cdp_guard_reports_recovery_and_not_running_states(tmp_path):
    launcher = fixture_launcher(tmp_path)
    not_running = launcher.cdp_guard("profile-1", running=False)
    assert not_running["code"] == "NOT_RUNNING"

    assert launcher.cdp_health_snapshot("unknown-profile", running=True)["state"] == (
        launcher_module.STATE_RECOVERY_REQUIRED
    )

    launch_id = launcher.cdp_health.begin_launch("profile-1", "owner-B")
    assert launcher.cdp_health.mark_healthy(
        "profile-1", launch_id, require_session=False,
    )
    guard = launcher.cdp_guard("profile-1", running=True)
    assert guard["code"] == "RECOVERY_REQUIRED"
    assert "profile-1" in launcher._recovery_required
    result = launcher._running_launch_result("profile-1")
    assert result["recovery_required"] is True


def test_runtime_match_and_stale_process_forgetting(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    assert launcher._runtime_matches_local("profile-1", {})
    launch_id = launcher.cdp_health.begin_launch("profile-1", launcher.instance_id)
    launcher.cdp_health.set_process("profile-1", launch_id, 424242)
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=424243)
    assert not launcher._runtime_matches_local(
        "profile-1", {"pid": 424242, "launch_id": launch_id}
    )
    launcher.active_processes["profile-1"] = SimpleNamespace(pid=424242)
    assert not launcher._runtime_matches_local(
        "profile-1", {"pid": 424242, "launch_id": "generation-B"}
    )
    assert launcher._runtime_matches_local(
        "profile-1", {"pid": 424242, "launch_id": launch_id}
    )

    monkeypatch.setattr(launcher.cdp_health, "forget_if_launch", lambda *_a: False)
    launcher._forget_stale_local_process("profile-1")
    assert "profile-1" in launcher.active_processes

    released = []
    monkeypatch.setattr(launcher.cdp_health, "forget_if_launch", lambda *_a: True)
    monkeypatch.setattr(launcher_module, "release_port", released.append)
    launcher.active_ports["profile-1"] = 9444
    launcher._requested_ports["profile-1"] = 9555
    launcher._recovery_required.add("profile-1")
    launcher._proxy_credentials["profile-1"] = "synthetic"
    launcher._forget_stale_local_process("profile-1")
    assert "profile-1" not in launcher.active_processes
    assert released == [9555, 9444]
    assert "profile-1" not in launcher._recovery_required


def test_failed_launch_cleanup_terminates_only_the_matching_generation(
    tmp_path, monkeypatch,
):
    launcher = fixture_launcher(tmp_path)
    record = launcher._runtime_store.upsert(
        "profile-1", launch_id="generation-A", pid=424242,
    )

    class Process:
        pid = 424242

        def __init__(self):
            self.exited = False
            self.killed = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout):
            if timeout == 3:
                raise subprocess.TimeoutExpired("edge", timeout)

        def kill(self):
            self.killed = True
            self.exited = True

    process = Process()
    monkeypatch.setattr(
        launcher.cdp_health, "request_browser_close", lambda *_a: True,
    )
    cleared = []
    monkeypatch.setattr(
        launcher, "_clear_process",
        lambda profile_id, *, expected_record: cleared.append(
            (profile_id, expected_record)
        ) or True,
    )

    assert launcher._cleanup_failed_launch("profile-1", process, "generation-A")
    assert process.killed
    assert cleared == [("profile-1", record)]


def test_failed_launch_cleanup_preserves_replacement_runtime(tmp_path):
    launcher = fixture_launcher(tmp_path)
    launcher._runtime_store.upsert(
        "profile-1", launch_id="generation-B", pid=424243,
    )
    process = SimpleNamespace(pid=424242, poll=lambda: None)

    assert not launcher._cleanup_failed_launch("profile-1", process, "generation-A")
    assert launcher._runtime_store.get("profile-1")["launch_id"] == "generation-B"


def test_failed_launch_cleanup_logs_runtime_write_failure(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    launch_id = launcher.cdp_health.begin_launch("profile-1", launcher.instance_id)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id=launch_id,
        owner_instance=launcher.instance_id,
        pid=424242,
    )
    process = SimpleNamespace(pid=424242, poll=lambda: 0)
    monkeypatch.setattr(launcher, "_clear_process", lambda *_a, **_k: False)
    monkeypatch.setattr(
        launcher._runtime_store, "upsert_if_owner",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("runtime store unavailable")),
    )

    assert not launcher._cleanup_failed_launch("profile-1", process, launch_id)
    assert launcher._runtime_store.get("profile-1") == record


def test_session_tabs_filter_save_and_load(tmp_path):
    launcher = fixture_launcher(tmp_path)
    user_data_dir = tmp_path / "user_data"
    user_data_dir.mkdir()
    urls = [
        None,
        " ",
        "about:blank",
        "http://localhost:9222/json/list",
        "http://127.0.0.1:9222/devtools/page/1",
        "https://example.test",
        "https://example.test",
        "https://second.test",
    ]

    assert launcher._filter_restorable_urls(None) == []
    assert launcher._filter_restorable_urls(urls) == [
        "https://example.test", "https://second.test",
    ]
    assert launcher._save_session_tabs(str(user_data_dir), urls)
    assert launcher._load_session_tabs(str(user_data_dir)) == [
        "https://example.test", "https://second.test",
    ]
    assert not launcher._save_session_tabs(str(user_data_dir), ["chrome://settings"])
    assert not launcher._save_session_tabs(None, ["https://example.test"])
    assert not launcher._save_session_tabs(str(tmp_path / "missing"), ["https://example.test"])

    many_urls = [f"https://example.test/{index}" for index in range(
        launcher_module.MAX_RESTORED_TABS + 1
    )]
    assert len(launcher._filter_restorable_urls(many_urls)) == launcher_module.MAX_RESTORED_TABS

    tabs_file = launcher._session_tabs_path(str(user_data_dir))
    with open(tabs_file, "w", encoding="utf-8") as stream:
        json.dump(["https://list.test", "javascript:alert(1)"], stream)
    assert launcher._load_session_tabs(str(user_data_dir)) == ["https://list.test"]
    with open(tabs_file, "w", encoding="utf-8") as stream:
        stream.write("{")
    assert launcher._load_session_tabs(str(user_data_dir)) == []
    assert launcher._load_session_tabs(None) == []


def test_session_tabs_save_failure_removes_temporary_file(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    user_data_dir = tmp_path / "user_data"
    user_data_dir.mkdir()
    monkeypatch.setattr(
        launcher_module.os, "replace",
        lambda *_a: (_ for _ in ()).throw(OSError("replace failed")),
    )

    assert not launcher._save_session_tabs(str(user_data_dir), ["https://example.test"])
    assert not list(user_data_dir.glob("*.tmp"))


def test_session_tabs_save_logs_temporary_file_removal_failure(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    user_data_dir = tmp_path / "user_data"
    user_data_dir.mkdir()
    monkeypatch.setattr(
        launcher_module.os, "replace",
        lambda *_a: (_ for _ in ()).throw(OSError("replace failed")),
    )
    monkeypatch.setattr(
        launcher_module.os, "remove",
        lambda *_a: (_ for _ in ()).throw(OSError("remove failed")),
    )

    assert not launcher._save_session_tabs(str(user_data_dir), ["https://example.test"])
    assert len(list(user_data_dir.glob("*.tmp"))) == 1


def test_snapshot_tabs_filters_targets_and_persists_urls(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    transient_id = "transient-tab"
    monkeypatch.setattr(launcher_module, "TRANSIENT_TARGET_IDS", {transient_id})
    targets = [
        {"type": "page", "id": "one", "url": "https://first.test"},
        {"type": "page", "id": transient_id, "url": "https://transient.test"},
        {"type": "service_worker", "id": "worker", "url": "https://worker.test"},
        {"type": "page", "id": "two", "url": "https://second.test"},
    ]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(targets).encode("utf-8")

    monkeypatch.setattr(launcher_module, "open_loopback", lambda *_a, **_k: Response())
    saved = []
    monkeypatch.setattr(
        launcher, "_save_session_tabs",
        lambda directory, urls: saved.append((directory, urls)) or True,
    )

    result = launcher._snapshot_tabs_via_http(9333, str(tmp_path))

    assert result == ["https://second.test", "https://first.test"]
    assert saved == [(str(tmp_path), result)]
    assert launcher._snapshot_tabs_via_http(None, str(tmp_path)) == []


def test_snapshot_tabs_handles_loopback_failure(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(
        launcher_module, "open_loopback",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("loopback unavailable")),
    )

    assert launcher._snapshot_tabs_via_http(9333, str(tmp_path)) == []


def test_clean_chrome_preferences_marks_profile_as_exited(tmp_path):
    user_data_dir = tmp_path / "user_data"
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True)
    preferences = default_dir / "Preferences"
    preferences.write_text('{"profile": {"name": "test"}}', encoding="utf-8")

    BrowserLauncher._prepare_clean_chrome_preferences(str(user_data_dir))
    data = json.loads(preferences.read_text(encoding="utf-8"))
    assert data["profile"]["exit_type"] == "Normal"
    assert data["profile"]["exited_cleanly"] is True

    BrowserLauncher._prepare_clean_chrome_preferences(None)


@pytest.mark.parametrize(
    "contents",
    ['{"profile": {"exit_type": "Normal", "exited_cleanly": true}}', "[]", "{"],
)
def test_clean_chrome_preferences_handles_existing_variants(tmp_path, contents):
    user_data_dir = tmp_path / "user_data"
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True)
    preferences = default_dir / "Preferences"
    preferences.write_text(contents, encoding="utf-8")

    BrowserLauncher._prepare_clean_chrome_preferences(str(user_data_dir))
    assert preferences.exists()

    empty_user_data = tmp_path / "empty"
    empty_user_data.mkdir()
    BrowserLauncher._prepare_clean_chrome_preferences(str(empty_user_data))


def test_open_tab_impl_uses_runtime_port_and_fallback(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    assert not launcher._open_tab_impl("profile-1", "https://example.test")
    launcher._runtime_store.upsert("profile-1", port=9444)

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    opened = []

    def open_loopback(url, **_kwargs):
        opened.append(url)
        if len(opened) == 1:
            raise OSError("PUT unsupported")
        return Response()

    monkeypatch.setattr(launcher_module, "open_loopback", open_loopback)
    assert launcher._open_tab_impl("profile-1", "https://example.test/path?a=1")
    assert launcher.active_ports["profile-1"] == 9444
    assert len(opened) == 2

    monkeypatch.setattr(
        launcher_module, "open_loopback",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("loopback unavailable")),
    )
    assert not launcher._open_tab_impl("profile-1", "https://example.test")


def test_stop_impl_terminates_synthetic_local_process(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    launch_id = launcher.cdp_health.begin_launch("profile-1", launcher.instance_id)
    launcher.cdp_health.set_process("profile-1", launch_id, 424242)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id=launch_id,
        owner_instance=launcher.instance_id,
        pid=424242,
        user_data_dir=str(tmp_path),
    )

    class Process:
        pid = 424242

        def __init__(self):
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

        def terminate(self):
            self.exited = True

        def wait(self, timeout):
            return None

    process = Process()
    launcher.active_processes["profile-1"] = process
    monkeypatch.setattr(launcher.cdp_health, "request_browser_close", lambda *_a: False)
    monkeypatch.setattr(launcher, "_snapshot_tabs_via_http", lambda *_a: [])
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: True)
    monkeypatch.setattr(launcher, "_persist_health_now", lambda *_a, **_k: None)

    result = launcher._stop_impl("profile-1")

    assert result["success"] is True
    assert launcher._runtime_store.get("profile-1") is None
    assert launcher.active_processes.get("profile-1") is None


def test_stop_impl_clears_already_exited_synthetic_process(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    launch_id = launcher.cdp_health.begin_launch("profile-1", launcher.instance_id)
    launcher.cdp_health.set_process("profile-1", launch_id, 424242)
    launcher._runtime_store.upsert(
        "profile-1",
        launch_id=launch_id,
        owner_instance=launcher.instance_id,
        pid=424242,
        user_data_dir=str(tmp_path),
    )
    launcher.active_processes["profile-1"] = SimpleNamespace(
        pid=424242, poll=lambda: 0,
    )
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: True)
    monkeypatch.setattr(launcher, "_clear_process", lambda *_a, **_k: True)

    result = launcher._stop_impl("profile-1")

    assert result["success"] is True


def test_stop_impl_clears_absent_recovered_process_without_termination(
    tmp_path, monkeypatch,
):
    launcher = fixture_launcher(tmp_path)
    launcher._runtime_store.upsert(
        "profile-1", pid=424242, user_data_dir=str(tmp_path),
    )
    monkeypatch.setattr(
        launcher, "_identity_for_stop",
        lambda *_a: {"state": "absent", "created": None},
    )
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: True)
    monkeypatch.setattr(launcher, "_clear_process", lambda *_a, **_k: True)

    result = launcher._stop_impl("profile-1")

    assert result["success"] is True


def test_stop_impl_terminates_owned_recovered_synthetic_process(
    tmp_path, monkeypatch,
):
    launcher = fixture_launcher(tmp_path)
    launcher._runtime_store.upsert(
        "profile-1",
        pid=424242,
        user_data_dir=str(tmp_path),
        created="stored",
        birth_ticks=10,
    )
    monkeypatch.setattr(
        launcher, "_identity_for_stop",
        lambda *_a: {
            "state": "owned", "created": "stored", "birth_ticks": 10,
        },
    )
    terminated = []
    monkeypatch.setattr(
        launcher_module, "terminate_verified_browser",
        lambda *_a: terminated.append(True) or {"state": "absent"},
    )
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: True)
    monkeypatch.setattr(launcher, "_clear_process", lambda *_a, **_k: True)

    result = launcher._stop_impl("profile-1")

    assert result["success"] is True
    assert terminated == [True]


def test_stop_impl_kills_synthetic_process_after_wait_timeouts(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    launch_id = launcher.cdp_health.begin_launch("profile-1", launcher.instance_id)
    launcher.cdp_health.set_process("profile-1", launch_id, 424242)
    launcher._runtime_store.upsert(
        "profile-1",
        launch_id=launch_id,
        owner_instance=launcher.instance_id,
        pid=424242,
        user_data_dir=str(tmp_path),
    )

    class Process:
        pid = 424242

        def __init__(self):
            self.exited = False
            self.killed = False

        def poll(self):
            return 0 if self.exited else None

        def terminate(self):
            return None

        def wait(self, timeout):
            if not self.exited:
                raise subprocess.TimeoutExpired("synthetic-edge", timeout)

        def kill(self):
            self.killed = True
            self.exited = True

    process = Process()
    launcher.active_processes["profile-1"] = process
    monkeypatch.setattr(launcher.cdp_health, "request_browser_close", lambda *_a: True)
    monkeypatch.setattr(launcher, "_persist_health_now", lambda *_a, **_k: None)
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: True)
    monkeypatch.setattr(launcher, "_clear_process", lambda *_a, **_k: True)

    result = launcher._stop_impl("profile-1")

    assert result["success"] is True
    assert process.killed


@pytest.mark.parametrize(
    "identity",
    [
        {"state": "owned", "created": "different", "birth_ticks": 10},
        {"state": "owned", "created": "stored", "birth_ticks": 11},
    ],
)
def test_stop_impl_refuses_recovered_process_with_changed_birth(
    tmp_path, monkeypatch, identity,
):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    launch_id = launcher.cdp_health.begin_launch("profile-1", launcher.instance_id)
    launcher.cdp_health.set_process("profile-1", launch_id, 424242)
    record = launcher._runtime_store.upsert(
        "profile-1",
        launch_id=launch_id,
        owner_instance=launcher.instance_id,
        pid=424242,
        user_data_dir=str(tmp_path),
        created="stored",
        birth_ticks=10,
    )
    monkeypatch.setattr(launcher, "_identity_for_stop", lambda *_a: identity)
    monkeypatch.setattr(launcher, "_persist_health_now", lambda *_a, **_k: None)
    monkeypatch.setattr(
        launcher_module, "terminate_verified_browser",
        lambda *_a: pytest.fail("mismatched identity must not be terminated"),
    )

    result = launcher._stop_impl("profile-1")

    assert result["success"] is False
    assert result["code"] == "STOP_FAILED"
    assert launcher._runtime_store.get("profile-1") == record


def test_stop_impl_clears_runtime_without_pid_when_directory_is_idle(
    tmp_path, monkeypatch,
):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(launcher, "_wait_directory_idle", lambda *_a: True)
    monkeypatch.setattr(
        launcher, "_clear_process",
        lambda *_a, **_k: True,
    )

    result = launcher._stop_impl("profile-1")

    assert result["success"] is True


@pytest.mark.parametrize("observed", [{424243}, None], ids=["child-running", "scan-unknown"])
def test_failed_launch_cleanup_retains_evidence_until_children_exit(directory_launcher, monkeypatch, observed):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    _immediate_scan(launcher, monkeypatch, observed)
    proc = SimpleNamespace(pid=424242, poll=lambda: 0)
    launcher.active_processes[profile_id] = proc
    assert not launcher._cleanup_failed_launch(profile_id, proc, launch_id)
    assert launcher._runtime_store.get(profile_id)["cleanup_failed"] is True
    assert launcher.active_processes[profile_id] is proc
    assert sentinel.exists()


@pytest.mark.parametrize("operation", ["stop", "failed-launch-cleanup"])
def test_graceful_close_waits_and_skips_native_termination(directory_launcher, monkeypatch, operation):
    launcher, profile_id, launch_id, sentinel = directory_launcher
    monkeypatch.setattr(launcher_module.sys, "platform", "win32")
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda *_a, **_k: set())
    calls = []
    exited = []
    def graceful():
        calls.append("close")
        exited.append(True)
        return True
    session = launcher.cdp_health.bind_session(profile_id, launch_id)
    assert session.bind_browser_close(graceful)
    proc = SimpleNamespace(
        pid=424242, poll=lambda: 0 if exited else None,
        wait=lambda **kwargs: calls.append("wait"),
        terminate=lambda: pytest.fail("graceful exit must not terminate"),
        kill=lambda: pytest.fail("graceful exit must not kill"),
    )
    launcher.active_processes[profile_id] = proc
    if operation == "stop":
        assert launcher.stop(profile_id)["success"]
    else:
        assert launcher._cleanup_failed_launch(profile_id, proc, launch_id)
    assert calls == ["close", "wait"]
    assert launcher._runtime_store.get(profile_id) is None
    assert profile_id not in launcher.active_processes
    assert sentinel.exists()
