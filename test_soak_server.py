"""Private soak bootstrap isolation and trust warmup boundaries."""
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import pytest

from tools import soak_server


@pytest.fixture
def paths(tmp_path):
    data = tmp_path / "data"
    results = tmp_path / "results"
    data.mkdir()
    results.mkdir()
    return data, results / "trust-warmup.json"


def test_trust_warmup_records_cold_and_warm_before_only_production_entrypoint(paths):
    data, report = paths
    observed = []
    counts = iter((200, 332))
    operation = str(uuid.uuid4())
    old_arguments = list(sys.argv)
    old_data = os.environ.get("ANTIDETECT_DATA_DIR")
    browser = str(data / "synthetic-not-executed-chrome.exe")

    def metrics():
        observed.append("metrics")
        return {"pid": os.getpid(), "handles": next(counts)}

    def verify(path):
        assert path == Path(browser)
        observed.append("trust")
        return {"path": browser, "sha256": "a" * 64, "publisher": "Google LLC", "version": "157.0.1.2"}

    def execute(path, run_name):
        observed.append("production")
        assert Path(path) == soak_server.ROOT / "run.py" and run_name == "__main__"
        assert sys.argv == [str(soak_server.ROOT / "run.py"), "--port=9123"]
        assert os.environ["ANTIDETECT_DATA_DIR"] == str(data.resolve())
        assert os.environ["ANTIDETECT_NO_BROWSER"] == "1"
        assert os.environ["ANTIDETECT_BROWSER_PATH"] == browser
        saved = json.loads(report.read_text(encoding="utf-8"))
        assert saved["cold_before_trust"]["handles"] == 200
        assert saved["warm_after_trust"]["handles"] == 332

    result = soak_server.start(data, report, operation, 9123, browser,
                              metrics=metrics, verify=verify, execute=execute)
    assert observed == ["metrics", "trust", "metrics", "production"]
    assert result["trust_handles_delta"] == 132
    assert result["operation_id"] == operation
    assert sys.argv == old_arguments and os.environ.get("ANTIDETECT_DATA_DIR") == old_data


@pytest.mark.parametrize("bad", ["outside", "report_name", "existing_report", "operation", "port"])
def test_invalid_fixture_arguments_refuse_before_trust_or_application(paths, bad):
    data, report = paths
    operation, port = str(uuid.uuid4()), 9123
    if bad == "outside":
        data = Path(tempfile.gettempdir()).anchor
    elif bad == "report_name":
        report = report.with_name("another.json")
    elif bad == "existing_report":
        report.write_text("{}", encoding="utf-8")
    elif bad == "operation":
        operation = "not-a-uuid"
    elif bad == "port":
        port = 0
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No trust or application code may run before fixture validation")
    with pytest.raises((soak_server.SoakServerError, ValueError)):
        soak_server.start(data, report, operation, port, "synthetic-chrome.exe",
                          metrics=forbidden, verify=forbidden, execute=forbidden)


def test_failed_trust_never_imports_or_executes_application(paths):
    data, report = paths
    def denied(_path):
        raise RuntimeError("synthetic untrusted browser")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Untrusted browser must never start application")
    with pytest.raises(RuntimeError, match="untrusted"):
        soak_server.start(data, report, str(uuid.uuid4()), 9123, "synthetic-chrome.exe",
                          metrics=lambda: {"pid": os.getpid(), "handles": 200}, verify=denied, execute=forbidden)
    assert not report.exists()


def test_wrong_process_metric_rejects_before_application(paths):
    data, report = paths
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Wrong-process evidence must never start application")
    with pytest.raises(soak_server.SoakServerError, match="process"):
        soak_server.start(data, report, str(uuid.uuid4()), 9123, "synthetic-chrome.exe",
                          metrics=lambda: {"pid": os.getpid() + 1, "handles": 200},
                          verify=lambda _p: {"path": "synthetic", "sha256": "a" * 64,
                                             "publisher": "Synthetic", "version": "1.2.3.4"},
                          execute=forbidden)
    assert not report.exists()
