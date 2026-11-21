"""Synthetic failure paths; never launch or inspect a real user profile."""
from types import SimpleNamespace

import pytest

from antidetect import process_identity
from antidetect.launcher import BrowserLauncher
from antidetect.profile_lifecycle import stop_and_delete
from test_runtime_generation_cleanup import fixture_launcher


class FakeKernel:
    def __init__(self):
        self.open_ok = True
        self.image_ok = True
        self.birth = 10
        self.exited = False
        self.terminated = 0
        self.closed = 0

    def OpenProcess(self, *args):
        return 0x100000001 if self.open_ok else 0

    def GetExitCodeProcess(self, handle, code):
        assert handle == 0x100000001
        code._obj.value = 0 if self.exited else 259
        return 1

    def QueryFullProcessImageNameW(self, handle, flags, buf, size):
        buf.value = r"C:\fixture\chrome.exe"
        return int(self.image_ok)

    def GetProcessTimes(self, handle, birth, *args):
        birth._obj.dwLowDateTime = self.birth
        return 1

    def CloseHandle(self, handle):
        self.closed += 1
        return 1

    def TerminateProcess(self, handle, code):
        self.terminated += 1
        self.exited = True
        return 1


@pytest.fixture
def identity_fixture(monkeypatch, tmp_path):
    api = FakeKernel()
    process_identity.invalidate_identity()
    monkeypatch.setattr(process_identity.sys, "platform", "win32")
    monkeypatch.setattr(process_identity, "_kernel32", lambda: api)
    monkeypatch.setattr(process_identity, "_native_record", lambda *_: None)
    monkeypatch.setattr(process_identity, "_query_record", lambda _: {
        "Name": "chrome.exe", "CommandLine": "fixture", "CreationDate": "birth-A"})
    monkeypatch.setattr(process_identity, "windows_argv", lambda _: [
        "chrome.exe", "--user-data-dir=" + str(tmp_path), "--remote-debugging-port=9333"])
    yield api, str(tmp_path)
    process_identity.invalidate_identity()


def test_access_denied_is_unknown(identity_fixture, monkeypatch):
    api, directory = identity_fixture
    api.open_ok = False
    monkeypatch.setattr(process_identity.ctypes, "get_last_error", lambda: 5)
    assert process_identity.read_browser_identity(424242, directory)["state"] == "unknown"
    assert not process_identity._identity_cache


def test_image_query_failure_is_unknown(identity_fixture):
    api, directory = identity_fixture
    api.image_ok = False
    assert process_identity.read_browser_identity(424242, directory)["state"] == "unknown"
    assert api.closed == 1


def test_cache_is_scoped_to_directory_and_fresh_reads_birth(identity_fixture):
    api, directory = identity_fixture
    initial = process_identity.read_browser_identity(424242, directory)
    assert initial["state"] == "owned"
    initial["state"] = "mismatch"
    assert process_identity.read_browser_identity(424242, directory)["state"] == "owned"
    assert process_identity.read_browser_identity(424242, directory + "-other")["state"] == "mismatch"
    api.birth = 11
    assert process_identity.read_browser_identity(424242, directory, fresh=True)["birth_ticks"] == 11


def test_termination_revalidates_birth_and_uses_same_handle(identity_fixture):
    api, directory = identity_fixture
    initial = process_identity.read_browser_identity(424242, directory)
    api.birth += 1
    result = process_identity.terminate_verified_browser(424242, directory, initial)
    assert result["state"] == "unknown"
    assert api.terminated == 0
    current = process_identity.read_browser_identity(424242, directory, fresh=True)
    assert process_identity.terminate_verified_browser(424242, directory, current)["state"] == "absent"
    assert api.terminated == 1


@pytest.mark.parametrize("matches, state", [(True, "owned"), (False, "mismatch")])
def test_native_identity_path_returns_owned_or_mismatch(
    identity_fixture, monkeypatch, matches, state,
):
    api, directory = identity_fixture
    actual_dir = directory if matches else directory + "-other"
    monkeypatch.setattr(process_identity, "_native_record", lambda _pid, _ticks: {
        "Name": "chrome.exe",
        "Argv": ["chrome.exe", "--user-data-dir=" + actual_dir],
        "CreationDate": "birth-A",
    })
    monkeypatch.setattr(
        process_identity, "_query_record",
        lambda _pid: pytest.fail("native identity should avoid CIM"),
    )

    result = process_identity.read_browser_identity(424242, directory, fresh=True)

    assert result["state"] == state


def test_native_argv_is_preferred_to_command_line(identity_fixture, monkeypatch):
    _api, directory = identity_fixture
    monkeypatch.setattr(process_identity, "_native_record", lambda _pid, _ticks: {
        "Name": "chrome.exe",
        "Argv": ["chrome.exe", "--user-data-dir=" + directory],
        "CommandLine": "chrome.exe --user-data-dir=C:\\wrong",
        "CreationDate": "birth-A",
    })
    monkeypatch.setattr(
        process_identity, "windows_argv",
        lambda _command_line: pytest.fail("Argv should be used directly"),
    )

    assert process_identity.read_browser_identity(424242, directory, fresh=True)["state"] == "owned"


def test_cim_creation_date_matches_filetime_epoch():
    ticks = 116444736000000000 + 123456789 * 10000 + 9999

    assert process_identity._cim_creation_date(ticks) == "/Date(123456789)/"


def test_native_access_denied_falls_back_to_cim(identity_fixture, monkeypatch):
    _api, directory = identity_fixture

    def denied(pid):
        raise process_identity.psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(process_identity.psutil, "Process", denied)
    assert process_identity.read_browser_identity(424242, directory, fresh=True)["state"] == "owned"


def test_invalid_termination_input_does_not_raise():
    assert process_identity.terminate_verified_browser("not-a-pid", "", {})["state"] == "unknown"


def test_failed_process_scan_is_not_proof_of_idle(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(launcher, "is_running", lambda _: False)
    monkeypatch.setattr("antidetect.launcher.PROFILES_DATA_DIR", str(tmp_path))
    (tmp_path / "profile-1" / "user_data").mkdir(parents=True)
    monkeypatch.setattr("antidetect.launcher.browser_pids_using_dir", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="PROCESS_STATE_UNVERIFIED"):
        launcher.profile_data_in_use("profile-1")


def test_delete_checks_directory_use_after_runtime_stopped(tmp_path):
    launcher = fixture_launcher(tmp_path)
    launcher.manager._profile_locks = launcher._lifecycle_locks
    launcher.manager._validate_profile_id = lambda _: None
    launcher.manager.delete_profile = lambda _: pytest.fail("source must be preserved")
    launcher.stop = lambda _: {"success": True}
    launcher.is_running = lambda _: False
    launcher.profile_data_in_use = lambda _: True
    assert not stop_and_delete(launcher.manager, launcher, "profile-1")["success"]


def test_process_scan_missing_command_line_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(process_identity.sys, "platform", "win32")
    monkeypatch.setattr(
        process_identity.psutil, "pids",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("defer to CIM")),
    )
    monkeypatch.setattr(process_identity.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='[{"ProcessId":424242,"CommandLine":null}]'))
    monkeypatch.setattr(process_identity, "read_browser_identity", lambda *a, **k: {"state": "unknown"})
    assert process_identity.browser_pids_using_dir(str(tmp_path)) is None


def test_process_scan_missing_command_line_skips_only_confirmed_exited(monkeypatch, tmp_path):
    monkeypatch.setattr(process_identity.sys, "platform", "win32")
    monkeypatch.setattr(
        process_identity.psutil, "pids",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("defer to CIM")),
    )
    monkeypatch.setattr(process_identity.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='[{"ProcessId":424242,"CommandLine":null}]'))
    for state in ("owned", "unknown", "mismatch", "absent"):
        calls = []
        def identity(pid, directory, *, fresh):
            calls.append((pid, directory, fresh))
            return {"state": state}
        monkeypatch.setattr(process_identity, "read_browser_identity", identity)
        result = process_identity.browser_pids_using_dir(str(tmp_path))
        assert calls == [(424242, str(tmp_path), True)]
        assert result == (set() if state == "absent" else None)


def test_fast_process_scan_matches_and_excludes_pids(monkeypatch, tmp_path):
    monkeypatch.setattr(process_identity.sys, "platform", "win32")
    directory = str(tmp_path)
    processes = {
        101: ("chrome.exe", ["chrome.exe", "--user-data-dir=" + directory]),
        102: ("msedge.exe", ["msedge.exe", "--user-data-dir=" + directory]),
        103: (
            "chromium.exe",
            ["chromium.exe", "--user-data-dir=" + directory + "-other"],
        ),
        104: (
            "not-a-browser.exe",
            ["not-a-browser.exe", "--user-data-dir=" + directory],
        ),
    }
    monkeypatch.setattr(process_identity.psutil, "pids", lambda: list(processes))
    monkeypatch.setattr(
        process_identity.psutil,
        "Process",
        lambda pid: SimpleNamespace(
            name=lambda: processes[pid][0],
            cmdline=lambda: processes[pid][1],
        ),
    )
    monkeypatch.setattr(
        process_identity, "_browser_pids_using_dir_cim",
        lambda *_a, **_k: pytest.fail("complete native scan should not use CIM"),
    )

    assert process_identity.browser_pids_using_dir(directory, exclude_pids={102}) == {101}


def test_fast_process_scan_skips_process_that_exits_during_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(process_identity.sys, "platform", "win32")

    def process(pid):
        if pid == 100:
            raise process_identity.psutil.NoSuchProcess(pid=pid)
        return SimpleNamespace(
            name=lambda: "chrome.exe",
            cmdline=lambda: ["chrome.exe", "--user-data-dir=" + str(tmp_path)],
        )

    monkeypatch.setattr(process_identity.psutil, "pids", lambda: [100, 101])
    monkeypatch.setattr(process_identity.psutil, "Process", process)

    assert process_identity.browser_pids_using_dir(str(tmp_path)) == {101}


@pytest.mark.parametrize(
    ("failure", "parent_pid", "fallback"),
    [
        ("name-access-denied", "denied", True),
        ("empty-name", 4, False),
        ("empty-name", 1234, True),
        ("cmdline-access-denied", None, True),
        ("empty-cmdline", None, True),
    ],
)
def test_fast_process_scan_defers_to_cim_on_incomplete_process(
    monkeypatch, tmp_path, failure, parent_pid, fallback,
):
    monkeypatch.setattr(process_identity.sys, "platform", "win32")

    class Process:
        def name(self):
            if failure == "name-access-denied":
                raise process_identity.psutil.AccessDenied(pid=101)
            if failure == "empty-name":
                return ""
            return "chrome.exe"

        def ppid(self):
            if parent_pid == "denied":
                raise process_identity.psutil.AccessDenied(pid=101)
            return parent_pid

        def cmdline(self):
            if failure == "cmdline-access-denied":
                raise process_identity.psutil.AccessDenied(pid=101)
            if failure == "empty-cmdline":
                return []
            return ["chrome.exe", "--user-data-dir=" + str(tmp_path)]

    calls = []
    monkeypatch.setattr(process_identity.psutil, "pids", lambda: [101])
    monkeypatch.setattr(process_identity.psutil, "Process", lambda _pid: Process())
    monkeypatch.setattr(
        process_identity, "_browser_pids_using_dir_cim",
        lambda directory, excluded: calls.append((directory, excluded)) or {101},
    )

    result = process_identity.browser_pids_using_dir(str(tmp_path), {102})
    if fallback:
        assert result == {101}
        assert calls == [(str(tmp_path), {102})]
    else:
        assert result == set()
        assert calls == []


def test_fast_process_scan_uses_fresh_process_object_for_reused_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(process_identity.sys, "platform", "win32")
    directory = str(tmp_path)
    names = iter(["notepad.exe", "chrome.exe"])
    process_objects = []

    def process(pid):
        instance = SimpleNamespace(
            name=lambda: next(names),
            cmdline=lambda: ["chrome.exe", "--user-data-dir=" + directory],
        )
        process_objects.append(instance)
        return instance

    monkeypatch.setattr(process_identity.psutil, "pids", lambda: [101])
    monkeypatch.setattr(process_identity.psutil, "Process", process)
    monkeypatch.setattr(
        process_identity, "_browser_pids_using_dir_cim",
        lambda *_a, **_k: pytest.fail("complete native scan should not use CIM"),
    )

    assert process_identity.browser_pids_using_dir(directory) == set()
    assert process_identity.browser_pids_using_dir(directory) == {101}
    assert len(process_objects) == 2
    assert process_objects[0] is not process_objects[1]


def test_failed_launch_cleanup_retains_live_handle_and_runtime(tmp_path):
    launcher = fixture_launcher(tmp_path)
    launch_id = launcher.cdp_health.begin_launch("profile-1", "owner-A")
    launcher._runtime_store.upsert("profile-1", pid=424242, launch_id=launch_id,
                                   owner_instance="owner-A", user_data_dir=str(tmp_path))
    def denied():
        raise PermissionError("synthetic")
    proc = SimpleNamespace(pid=424242, poll=lambda: None, kill=denied)
    launcher.active_processes["profile-1"] = proc
    assert not launcher._cleanup_failed_launch("profile-1", proc, launch_id)
    assert launcher.active_processes["profile-1"] is proc
    assert launcher._runtime_store.get("profile-1")["cleanup_failed"] is True
