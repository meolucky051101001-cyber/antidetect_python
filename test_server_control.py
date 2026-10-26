"""Identity/PID-file helpers của server_control, không chạy PowerShell thật."""
import json
import sys
import types
import urllib.error

import pytest

import antidetect.server_control as sc


@pytest.fixture(autouse=True)
def _clean_cache():
    sc._DETAILS_CACHE.clear()
    yield
    sc._DETAILS_CACHE.clear()


@pytest.fixture
def pid_file(tmp_path, monkeypatch):
    path = tmp_path / "server.pid"
    monkeypatch.setattr(sc, "PID_FILE", str(path))
    monkeypatch.setattr(sc, "DATA_DIR", str(tmp_path))
    return path


def _details(**overrides):
    data = {
        "exe": "C:/Python310/python.exe",
        "cmdline": "python run.py --port 8766",
        "created": "2026-10-26T00:00:00",
    }
    data.update(overrides)
    return data


def test_cmdline_digest_normalizes_whitespace_and_case():
    assert sc._cmdline_digest("  Python   RUN.py\t--port 8766 ") == sc._cmdline_digest(
        "python run.py --port 8766"
    )
    assert sc._cmdline_digest("") == sc._cmdline_digest(None)
    assert sc._cmdline_digest("python run.py") != sc._cmdline_digest(
        "python run.py --x"
    )


def test_read_server_info_missing_empty_and_invalid(pid_file):
    assert sc.read_server_info() is None

    pid_file.write_text("", encoding="utf-8")
    assert sc.read_server_info() is None

    pid_file.write_text("{bad", encoding="utf-8")
    assert sc.read_server_info() is None

    pid_file.write_text("not-a-pid", encoding="utf-8")
    assert sc.read_server_info() is None

    pid_file.write_text("[1,2]", encoding="utf-8")
    assert sc.read_server_info() is None


def test_read_server_info_json_and_legacy(pid_file):
    pid_file.write_text(
        json.dumps({"pid": 123, "port": 8766, "instance_id": "i"}),
        encoding="utf-8",
    )
    assert sc.read_server_info() == {
        "pid": 123,
        "port": 8766,
        "instance_id": "i",
    }

    pid_file.write_text("4321\n", encoding="utf-8")
    assert sc.read_server_info() == {"pid": 4321, "port": 8000, "legacy": True}


def test_write_and_remove_server_info(pid_file, monkeypatch):
    monkeypatch.setattr(sc, "get_process_details", lambda pid: _details())

    sc.write_server_info(123, 8766, "inst-1")

    data = json.loads(pid_file.read_text(encoding="utf-8"))
    assert data["pid"] == 123
    assert data["port"] == 8766
    assert data["instance_id"] == "inst-1"
    assert data["cmdline_digest"] == sc._cmdline_digest(_details()["cmdline"])
    assert data["process_created"] == "2026-10-26T00:00:00"
    assert not list(pid_file.parent.glob("server.pid.tmp.*"))

    sc.remove_server_info()
    assert pid_file.exists() is False
    sc.remove_server_info()


def test_write_server_info_without_process_details(pid_file, monkeypatch):
    monkeypatch.setattr(sc, "get_process_details", lambda pid: None)

    sc.write_server_info(1, 8000, "inst-2")

    data = json.loads(pid_file.read_text(encoding="utf-8"))
    assert data["process_created"] == ""
    assert data["cmdline_digest"] == ""


def test_get_process_details_rejects_missing_pid():
    assert sc.get_process_details(0) is None
    assert sc.get_process_image_name(0) is None
    assert sc.get_process_command_line(0) is None


def test_get_process_details_returns_none_without_image(monkeypatch):
    monkeypatch.setattr(sc, "get_process_image_name", lambda pid: None)
    assert sc.get_process_details(42) is None


def test_get_process_details_caches_repeated_lookups(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sc,
        "get_process_image_name",
        lambda pid: calls.append(pid) or "C:/Python310/python.exe",
    )
    monkeypatch.setattr(
        sc, "get_process_command_line", lambda pid: "python run.py"
    )
    if sys.platform == "win32":
        monkeypatch.setattr(
            sc.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout="2026-10-26\n"),
        )
    first = sc.get_process_details(42)
    second = sc.get_process_details(42)

    assert first == second
    assert calls == [42]
    assert first["created"] == ("2026-10-26" if sys.platform == "win32" else "")


def test_verify_server_identity_accepts_matching_record(monkeypatch):
    details = _details()
    monkeypatch.setattr(sc, "get_process_details", lambda pid: details)

    assert (
        sc.verify_server_identity(
            {
                "pid": 1,
                "cmdline_digest": sc._cmdline_digest(details["cmdline"]),
                "process_created": details["created"],
            }
        )
        is True
    )


def test_verify_server_identity_rejects_bad_records(monkeypatch):
    assert sc.verify_server_identity({}) is False

    monkeypatch.setattr(sc, "get_process_details", lambda pid: None)
    assert sc.verify_server_identity({"pid": 1}) is False

    monkeypatch.setattr(
        sc, "get_process_details", lambda pid: _details(exe="C:/Windows/notepad.exe")
    )
    assert sc.verify_server_identity({"pid": 1}) is False

    monkeypatch.setattr(
        sc, "get_process_details", lambda pid: _details(cmdline="python other.py")
    )
    assert sc.verify_server_identity({"pid": 1}) is False

    monkeypatch.setattr(sc, "get_process_details", lambda pid: _details())
    assert sc.verify_server_identity({"pid": 1, "cmdline_digest": "bad"}) is False
    assert sc.verify_server_identity({"pid": 1, "process_created": "different"}) is False
    assert (
        sc.verify_server_identity(
            {"pid": 1, "cmdline_digest": sc._cmdline_digest(_details()["cmdline"])}
        )
        is True
    )


class _OkResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_check_server_health_success_and_failure(monkeypatch):
    monkeypatch.setattr(
        sc.urllib.request,
        "urlopen",
        lambda req, timeout: _OkResponse({"application": "antidetect-python"}),
    )
    assert sc.check_server_health(8766) == {"application": "antidetect-python"}

    def fail(req, timeout):
        raise OSError("offline")

    monkeypatch.setattr(sc.urllib.request, "urlopen", fail)
    assert sc.check_server_health(8766) is None


def test_stop_server_without_pid_file(pid_file):
    assert sc.stop_server()["success"] is True
    assert "server.pid" in sc.stop_server()["message"]


def test_stop_server_keeps_pid_when_identity_unverified(pid_file, monkeypatch):
    removed = []
    monkeypatch.setattr(sc, "read_server_info", lambda: {"pid": 7})
    monkeypatch.setattr(sc, "verify_server_identity", lambda info: False)
    monkeypatch.setattr(sc, "get_process_details", lambda pid: _details())
    monkeypatch.setattr(sc, "remove_server_info", lambda: removed.append(True))

    result = sc.stop_server()

    assert result["success"] is False
    assert "giữ server.pid" in result["message"]
    assert removed == []


def test_stop_server_cleans_stale_pid(pid_file, monkeypatch):
    removed = []
    monkeypatch.setattr(sc, "read_server_info", lambda: {"pid": 7})
    monkeypatch.setattr(sc, "verify_server_identity", lambda info: False)
    monkeypatch.setattr(sc, "get_process_details", lambda pid: None)
    monkeypatch.setattr(sc, "remove_server_info", lambda: removed.append(True))

    result = sc.stop_server()

    assert result["success"] is True
    assert removed == [True]


def test_stop_server_refuses_other_instance(pid_file, monkeypatch):
    monkeypatch.setattr(
        sc, "read_server_info", lambda: {"pid": 7, "port": 8766, "instance_id": "A"}
    )
    monkeypatch.setattr(sc, "verify_server_identity", lambda info: True)
    monkeypatch.setattr(
        sc,
        "check_server_health",
        lambda port: {"application": "antidetect-python", "instance_id": "B"},
    )
    monkeypatch.setattr(
        sc, "remove_server_info", lambda: pytest.fail("must not remove")
    )

    result = sc.stop_server()

    assert result["success"] is False
    assert "instance khác" in result["message"]


def test_stop_server_shutdown_api_success(pid_file, monkeypatch):
    removed = []
    monkeypatch.setattr(
        sc, "read_server_info", lambda: {"pid": 7, "port": 8766, "instance_id": "A"}
    )
    monkeypatch.setattr(sc, "verify_server_identity", lambda info: True)
    monkeypatch.setattr(
        sc,
        "check_server_health",
        lambda port: {"application": "antidetect-python", "instance_id": "A"},
    )
    monkeypatch.setattr(
        sc.urllib.request, "urlopen", lambda req, timeout: _OkResponse({})
    )
    import antidetect.runtime_auth as runtime_auth

    monkeypatch.setattr(runtime_auth, "read_runtime", lambda: {"access_key": "k"})
    monkeypatch.setattr(sc, "_wait_for_process_exit", lambda pid, timeout: True)
    monkeypatch.setattr(sc, "remove_server_info", lambda: removed.append(True))

    result = sc.stop_server()

    assert result["success"] is True
    assert "qua API" in result["message"]
    assert removed == [True]


def test_stop_server_refuses_409_without_kill(pid_file, monkeypatch):
    monkeypatch.setattr(
        sc, "read_server_info", lambda: {"pid": 7, "port": 8766, "instance_id": "A"}
    )
    monkeypatch.setattr(sc, "verify_server_identity", lambda info: True)
    monkeypatch.setattr(
        sc,
        "check_server_health",
        lambda port: {"application": "antidetect-python", "instance_id": "A"},
    )

    def conflict(req, timeout):
        raise urllib.error.HTTPError("http://127.0.0.1:8766", 409, "Conflict", {}, None)

    monkeypatch.setattr(sc.urllib.request, "urlopen", conflict)
    monkeypatch.setattr(
        sc, "remove_server_info", lambda: pytest.fail("must not remove")
    )
    monkeypatch.setattr(
        sc, "_wait_for_process_exit", lambda pid, timeout: pytest.fail("must not wait")
    )

    result = sc.stop_server()

    assert result["success"] is False
    assert "profile đang chạy" in result["message"]


def test_stop_server_falls_back_to_verified_kill(pid_file, monkeypatch):
    removed = []
    killed = []
    monkeypatch.setattr(
        sc, "read_server_info", lambda: {"pid": 7, "port": 8766, "instance_id": "A"}
    )
    monkeypatch.setattr(sc, "verify_server_identity", lambda info: True)
    monkeypatch.setattr(
        sc,
        "check_server_health",
        lambda port: {"application": "antidetect-python", "instance_id": "A"},
    )

    def offline(req, timeout):
        raise OSError("control path dead")

    monkeypatch.setattr(sc.urllib.request, "urlopen", offline)
    monkeypatch.setattr(
        sc.subprocess, "run", lambda *a, **k: killed.append(a[0] if a else None)
    )
    monkeypatch.setattr(sc, "_wait_for_process_exit", lambda pid, timeout: True)
    monkeypatch.setattr(sc, "remove_server_info", lambda: removed.append(True))

    result = sc.stop_server()

    assert result["success"] is True
    assert "thành công" in result["message"]
    assert removed == [True]
    if sys.platform == "win32":
        assert killed and killed[0][0] == "taskkill"
