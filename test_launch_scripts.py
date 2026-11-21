import inspect
from pathlib import Path

import run

ROOT = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_setup_installs_hash_locked_runtime_into_venv():
    text = _read("setup.bat")
    assert "-m venv .venv" in text
    assert "--require-hashes --only-binary=:all: -r requirements-win-py310.lock.txt" in text
    assert "pip check" in text
    assert "sys.version_info[:2]==(3,10)" in text and "calcsize('P')==8" in text


def test_launchers_prefer_venv_interpreter():
    assert ".venv\Scripts\python.exe" in _read("start.bat")
    assert ".venv\Scripts\python.exe" in _read("stop.bat")
    assert ".venv\Scripts\pythonw.exe" in _read("run_hidden.vbs")


def test_stop_script_runs_both_steps_with_selected_interpreter():
    text = _read("stop.bat")
    assert '"%PYEXE%" cli.py stop-all' in text
    assert '"%PYEXE%" -m antidetect.server_control stop' in text
    assert "\npython " not in text.replace("\r\n", "\n")


def test_unlock_request_timeout_tolerates_loaded_host():
    default = inspect.signature(run.request_unlock_code_from_server).parameters["timeout"].default
    assert default >= 2.0
