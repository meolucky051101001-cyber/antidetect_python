"""Opt-in actual Chromium network validation; fresh synthetic data only."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt" or os.environ.get("ANTIDETECT_RUN_LIVE_NETWORK") != "1",
                    reason="Explicit Windows localhost browser network opt-in required.")
def test_browserlauncher_local_proxy_and_stun_contracts(tmp_path):
    report = tmp_path / "browser-network.json"
    command = [sys.executable, str(Path(__file__).parent / "tools" / "browser_network_probe.py"),
               "--data-dir", str(tmp_path / "fresh-network"), "--report", str(report)]
    if os.environ.get("ANTIDETECT_LIVE_BROWSER"):
        command += ["--browser", os.environ["ANTIDETECT_LIVE_BROWSER"]]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=360)
    if os.environ.get("ANTIDETECT_LIVE_NETWORK_REPORT") and report.exists():
        destination = Path(os.environ["ANTIDETECT_LIVE_NETWORK_REPORT"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(report.read_bytes())
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
