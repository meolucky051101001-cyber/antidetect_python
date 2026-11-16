"""Opt-in browser behavior validation using only fresh TEMP and localhost."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("ANTIDETECT_RUN_LIVE_FINGERPRINT") != "1",
    reason="Explicit Windows synthetic fingerprint browser opt-in required.",
)


def test_browserlauncher_canvas_contracts(tmp_path):
    report = tmp_path / "fingerprint-contracts.json"
    command = [
        sys.executable, str(Path(__file__).parent / "tools" / "fingerprint_contract_probe.py"),
        "--data-dir", str(tmp_path / "fresh-data"), "--report", str(report),
    ]
    browser = os.environ.get("ANTIDETECT_LIVE_BROWSER")
    if browser:
        command += ["--browser", browser]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=300)
    saved_report = os.environ.get("ANTIDETECT_LIVE_FINGERPRINT_REPORT")
    if saved_report and report.exists():
        destination = Path(saved_report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(report.read_bytes())
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-2000:]
