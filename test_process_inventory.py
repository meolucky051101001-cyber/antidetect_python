"""Shared read-only browser inventory keeps incomplete scans fail-closed."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from antidetect import process_identity as identity
from tools import soak_test as soak


@pytest.fixture
def inventory_query(monkeypatch):
    calls = []
    response = {"returncode": 0, "stdout": "[]"}

    def query(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(**response)

    monkeypatch.setattr(identity.sys, "platform", "win32")
    monkeypatch.setattr(identity.subprocess, "run", query)
    monkeypatch.setattr(identity, "windows_argv", json.loads)
    return calls, response


def row(pid, directory):
    return {"ProcessId": pid, "CommandLine": json.dumps([
        "chrome.exe", "--user-data-dir=" + str(directory)])}


def test_all_directories_share_one_fresh_inventory_with_exact_paths(tmp_path, inventory_query):
    calls, response = inventory_query
    directories = {"one": str(tmp_path / "profile with spaces"), "two": str(tmp_path / "profile two")}
    response["stdout"] = json.dumps([
        row(11, directories["one"]), row(12, directories["two"]),
        row(13, directories["one"] + "-sibling"), row(14, directories["one"]),
    ])
    assert identity.browser_pids_using_dirs(directories) == {"one": {11, 14}, "two": {12}}
    assert len(calls) == 1
    response["stdout"] = "[]"
    assert identity.browser_pids_using_dirs(directories) == {"one": set(), "two": set()}
    assert len(calls) == 2, "A later scan must not reuse an earlier snapshot"


@pytest.mark.parametrize("payload", ["null", "broken json", '[{"ProcessId":0}]', '[null]'])
def test_invalid_inventory_marks_every_directory_unknown(tmp_path, inventory_query, payload):
    _calls, response = inventory_query
    response["stdout"] = payload
    directories = {"one": str(tmp_path / "one"), "two": str(tmp_path / "two")}
    assert identity.browser_pids_using_dirs(directories) == {"one": None, "two": None}


@pytest.mark.parametrize("failure", ["timeout", "query_exit"])
def test_query_failure_is_unknown_for_all_directories(tmp_path, inventory_query, monkeypatch, failure):
    _calls, response = inventory_query
    directories = {"one": str(tmp_path / "one"), "two": str(tmp_path / "two")}
    if failure == "timeout":
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("synthetic inventory", 8)
        monkeypatch.setattr(identity.subprocess, "run", timeout)
    else:
        response["returncode"] = 1
    assert identity.browser_pids_using_dirs(directories) == {"one": None, "two": None}


@pytest.mark.parametrize("state", ["owned", "unknown", "mismatch", "absent"])
def test_unreadable_command_line_only_skips_native_confirmed_exit(
        tmp_path, inventory_query, monkeypatch, state):
    _calls, response = inventory_query
    directories = {"one": str(tmp_path / "one"), "two": str(tmp_path / "two")}
    response["stdout"] = json.dumps([row(11, directories["two"]), {"ProcessId": 22, "CommandLine": None}])
    checks = []

    def native(pid, directory, *, fresh):
        checks.append((pid, directory, fresh))
        return {"state": state}

    monkeypatch.setattr(identity, "read_browser_identity", native)
    result = identity.browser_pids_using_dirs(directories)
    assert checks == [(22, directories["one"], True)]
    assert result == ({"one": set(), "two": {11}} if state == "absent" else {"one": None, "two": None})


def test_single_directory_exclusions_remain_compatible(tmp_path, inventory_query):
    _calls, response = inventory_query
    response["stdout"] = json.dumps([row(11, tmp_path), row(12, tmp_path), {"ProcessId": 13, "CommandLine": None}])
    assert identity.browser_pids_using_dir(str(tmp_path), exclude_pids=["11", 13]) == {12}
    assert identity.browser_pids_using_dir(None) is None


def test_soak_maps_one_inventory_without_requerying_each_profile(monkeypatch):
    calls = []
    directories = {"one": "synthetic one", "two": "synthetic two"}
    monkeypatch.setattr(soak, "browser_pids_using_dirs", lambda dirs: (
        calls.append(dict(dirs)) or {"one": {11}, "two": {12}}))
    processes = {pid: SimpleNamespace(pid=pid, is_running=lambda: True,
                                     children=lambda **kwargs: [],
                                     memory_info=lambda: SimpleNamespace(rss=1048576))
                 for pid in (11, 12)}
    monkeypatch.setattr(soak.psutil, "Process", lambda pid: processes[pid])
    result = soak._chrome_processes(directories)
    assert calls == [directories]
    assert result["one"]["pids"] == [11]
    assert result["two"]["pids"] == [12]
    assert all(value["observation_complete"] is True for value in result.values())


def test_soak_inventory_unknown_does_not_become_empty_proof(monkeypatch):
    monkeypatch.setattr(soak, "browser_pids_using_dirs", lambda dirs: {key: None for key in dirs})
    result = soak._chrome_processes({"one": "synthetic one", "two": "synthetic two"})
    assert set(result) == {"one", "two"}
    assert all(value["observation_complete"] is False and not value["pids"] for value in result.values())
