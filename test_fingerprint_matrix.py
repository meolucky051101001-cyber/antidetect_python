"""Offline acceptance checks for the optional browser/seed evidence matrix."""
import json
from types import SimpleNamespace

from tools import fingerprint_contract_probe as probe


def test_installed_browser_discovery_reports_missing_without_downloading(tmp_path):
    chrome = tmp_path / "Google/Chrome/Application/chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"synthetic binary path")
    inventory = probe.installed_browser_candidates({"PROGRAMFILES": str(tmp_path)})
    assert inventory[0]["path"] == str(chrome.resolve())
    assert inventory[1] == {"name": "edge", "path": None, "status": "not_installed"}


def test_matrix_rejects_identical_unique_hashes_across_seeds(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))
    seen = []

    def child(command, **kwargs):
        seed = int(command[command.index("--seed") + 1])
        seen.append(seed)
        assert command[command.index("--read-repetitions") + 1] == "10"
        assert command[command.index("--restart-count") + 1] == "3"
        report = {"status": "passed", "modes": {"unique": {"realms": {"page": {"pixelHash": "same-for-every-seed"}}}}}
        path = command[command.index("--report") + 1]
        probe.Path(path).write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(probe.subprocess, "run", child)
    output = tmp_path / "matrix.json"
    args = SimpleNamespace(data_dir=str(tmp_path / "fresh"), report=str(output),
                           exhaustive=True, browser_matrix=None, browser="synthetic.exe")
    assert probe.run_matrix(args) == 1
    result = json.loads(output.read_text())
    assert seen == [0, 12345, 987654321]
    assert result["exhaustive_gate"] == "failed"
    assert result["browser_inventory"][0]["unique_seed_separation"] == "failed"


def test_matrix_missing_browsers_never_claims_success(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(probe, "installed_browser_candidates", lambda: [
        {"name": "chrome", "path": None, "status": "not_installed"},
        {"name": "edge", "path": None, "status": "not_installed"},
    ])
    output = tmp_path / "matrix.json"
    args = SimpleNamespace(data_dir=str(tmp_path / "fresh"), report=str(output),
                           exhaustive=True, browser_matrix="installed", browser=None)
    assert probe.run_matrix(args) == 1
    result = json.loads(output.read_text())
    assert result["scenarios"] == []
    assert result["installed_browser_gate"] == "partial_missing_browser"
