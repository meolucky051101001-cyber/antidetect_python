"""Serial synthetic acceptance on one clean commit; stdlib orchestration only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("restore", "fingerprint", "preflight", "long", "overnight")
STATE_NAME = "candidate-state.json"
SENSITIVE_ROOTS = {"data", "profiles", "browsers", "exports", "backups", "workspace", "workspace_backup_safety"}
SENSITIVE_PARTS = {"profiles", "user_data", "browser_data", "cookies", "sessions", "local storage",
                   "indexeddb", "webstorage", "service worker", "cache", "code cache"}
SENSITIVE_SUFFIXES = {".dpapi", ".pid", ".key", ".pem", ".pfx", ".sqlite", ".sqlite3", ".db", ".wal", ".shm"}


class CandidateError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = no_reparse(path)
    no_reparse(path.parent)
    pending = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    try:
        with pending.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def no_reparse(path):
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        try:
            entry = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
            raise CandidateError("Candidate paths must not contain symlinks or reparse points.")
    return absolute.resolve()


def temp_path(path, *, fresh=False):
    result = no_reparse(path)
    if Path(tempfile.gettempdir()).resolve() not in result.parents:
        raise CandidateError("Output/state must be inside a child of TEMP.")
    if fresh and result.exists():
        raise CandidateError("Output root must be fresh and nonexistent.")
    return result


def sha256(path):
    result = hashlib.sha256()
    with no_reparse(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def git(repo, *arguments):
    result = subprocess.run(["git", "-c", "safe.directory=" + str(repo), *arguments], cwd=repo,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise CandidateError("Cannot inspect candidate Git state.")
    return result.stdout


def inspect_source(repo):
    """Hash tracked source only; never read excluded profile/runtime trees."""
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise CandidateError("Candidate requires clean tracked and untracked Git state.")
    commit = git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    files, excluded = {}, 0
    for raw in git(repo, "ls-files", "-z").split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        parts = Path(relative).parts
        folded = {part.lower() for part in parts}
        if (parts[0].lower() in SENSITIVE_ROOTS or folded & SENSITIVE_PARTS
                or Path(relative).suffix.lower() in SENSITIVE_SUFFIXES
                or Path(relative).name.lower() in {"cookies", "cookies-journal", "cookies-wal"}):
            excluded += 1
            continue
        path = no_reparse(Path(repo) / relative)
        if Path(repo).resolve() not in path.parents or not path.is_file():
            raise CandidateError("Tracked source must be a regular file inside the checkout.")
        files[relative] = sha256(path)
    return {"commit": commit, "files": files, "excluded_sensitive_tracked_entries": excluded}


def installed_browsers():
    roots = [os.environ.get(key) for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    inventory = []
    for name, relative in (("chrome", "Google/Chrome/Application/chrome.exe"),
                           ("edge", "Microsoft/Edge/Application/msedge.exe")):
        found = next((Path(root) / relative for root in roots if root and (Path(root) / relative).is_file()), None)
        inventory.append({"name": name, "path": str(found.resolve()) if found else None,
                          "status": "available" if found else "not_installed"})
    return inventory


def attest_browser(repo, path):
    # Load only the stdlib trust module by filename. Do not import the package,
    # storage, configuration, real data, site hooks, or the candidate executable.
    script = ("import dataclasses,importlib.util,json,sys;"
              "s=importlib.util.spec_from_file_location('audit_browser_trust',sys.argv[1]);"
              "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);"
              "print(json.dumps(dataclasses.asdict(m.verify_browser_executable(sys.argv[2]))))")
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script,
                             str(Path(repo) / "antidetect" / "browser_trust.py"), str(path)],
                            cwd=tempfile.gettempdir(), capture_output=True, text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise CandidateError("Browser attestation failed; choose a supported signed local Chrome/Edge executable.")
    try:
        attestation = json.loads(result.stdout)
        if not all(attestation.get(key) for key in ("path", "sha256", "version", "publisher")):
            raise ValueError()
        return attestation
    except (ValueError, TypeError, AttributeError):
        raise CandidateError("Browser attestation did not produce complete identity evidence.") from None


@dataclass(frozen=True)
class Options:
    output_root: Path
    browser: Path
    stages: tuple = STAGES
    min_free_mb: float = 900.0
    shed_free_mb: float = 650.0


def selected_stages(raw):
    selected = tuple(raw.split(","))
    if not selected or len(set(selected)) != len(selected) or selected != tuple(name for name in STAGES if name in selected):
        raise CandidateError("Stages must be a nonempty ordered subset of restore,fingerprint,preflight,long,overnight.")
    if ("long" in selected and "preflight" not in selected) or ("overnight" in selected and "long" not in selected):
        raise CandidateError("long requires preflight; overnight requires both preflight and long in this run.")
    return selected


def stage_spec(name, repo, output, options):
    directory = output / name
    report = directory / ("restore.json" if name == "restore" else "fingerprint.json" if name == "fingerprint" else "results/soak_summary.json")
    environment = dict(os.environ)
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", ANTIDETECT_NO_BROWSER="1",
                       ANTIDETECT_BROWSER_PATH=str(options.browser), ANTIDETECT_LIVE_BROWSER=str(options.browser))
    # Keep every application import in a child bound to an unused TEMP path.
    environment["ANTIDETECT_DATA_DIR"] = str(directory / "bootstrap-data")
    if name == "restore":
        environment["ANTIDETECT_RESTORE_REPORT"] = str(report)
        command = [sys.executable, "-m", "pytest", "-q", "test_release_restore.py",
                   "--basetemp=" + str(directory / "pytest-temp"), "--junitxml=" + str(directory / "tests.xml")]
    elif name == "fingerprint":
        command = [sys.executable, str(repo / "tools/fingerprint_contract_probe.py"),
                   "--data-dir", str(directory / "data"), "--report", str(report),
                   "--exhaustive", "--browser-matrix", "installed"]
        # The matrix probes its installed inventory explicitly, not an inherited
        # default that could select a different managed browser.
        environment.pop("ANTIDETECT_BROWSER_PATH", None)
    else:
        hours, cycle, copy = {"preflight": (0.25, 180, 240), "long": (2, 600, 900), "overnight": (24, 1800, 3600)}[name]
        command = [sys.executable, str(repo / "tools/soak_test.py"), "run", "--hours", str(hours),
                   "--profiles", "2", "--min-active", "1", "--sample-interval", "15", "--tab-interval", "60",
                   "--cycle-interval", str(cycle), "--copy-interval", str(copy), "--ramp-delay", "2",
                   "--min-free-mb", str(options.min_free_mb), "--shed-free-mb", str(options.shed_free_mb),
                   "--data-dir", str(directory / "data"), "--results-dir", str(directory / "results")]
    return {"name": name, "directory": directory, "report": report, "command": command,
            "environment": environment, "log": directory / "stage.log",
            "soak_state": directory / "results/soak_state.json" if name in STAGES[2:] else None}


def require_report(spec):
    try:
        report = json.loads(spec["report"].read_text(encoding="utf-8"))
        if spec["name"] == "restore":
            root = ET.parse(spec["directory"] / "tests.xml").getroot()
            cases = root.findall(".//testcase")
            expected = any(case.get("name") == "test_offline_v1_v3_backup_restore_round_trip" for case in cases)
            if report.get("status") != "passed" or not expected or not cases or any(
                    case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")):
                raise ValueError()
        elif spec["name"] == "fingerprint":
            inventory = report.get("browser_inventory")
            scenarios = report.get("scenarios")
            available = {item.get("name") for item in inventory or [] if item.get("status") == "available"}
            actual = {(item.get("browser"), item.get("seed")) for item in scenarios or []}
            expected = {(browser, seed) for browser in available for seed in (0, 12345, 987654321)}
            realm_names = {"page", "offscreen", "same_origin_frame", "cross_origin_frame", "cross_site_oopif",
                           "classic_worker", "module_worker", "nested_worker", "transferred_offscreen_worker",
                           "shared_worker", "service_worker", "reloaded_page"}
            contracts_complete = len(scenarios or []) == len(expected)
            for scenario in scenarios or []:
                evidence = scenario.get("evidence") or {}
                browser_row = next((item for item in spec.get("browser_inventory", [])
                                    if item.get("name") == scenario.get("browser")), None)
                if (evidence.get("status") != "passed" or evidence.get("source_commit") != spec.get("candidate_commit")
                        or not browser_row
                        or evidence.get("browser_binary_sha256") != browser_row.get("sha256")
                        or not evidence.get("browser_version")):
                    contracts_complete = False
                    continue
                for mode in ("unique", "authentic"):
                    runs = evidence.get("modes", {}).get(mode, {}).get("runs", [])
                    if len(runs) != 4:
                        contracts_complete = False
                        continue
                    for run in runs:
                        realms = run.get("realms", {})
                        if (set(realms) != realm_names or run.get("final_health", {}).get("state") != "healthy" or any(
                                value.get("readRepetitions") != 10 or value.get("decodedExportMatchesRead") is not True
                                or value.get("initialRealmRead", {}).get("noisy") != (mode == "unique")
                                for value in realms.values())):
                            contracts_complete = False
                native = evidence.get("modes", {}).get("native", {}).get("realms", {})
                if set(native) != realm_names or any(value.get("readRepetitions") != 10 for value in native.values()):
                    contracts_complete = False
            if (report.get("status") != "passed" or report.get("exhaustive_gate") != "passed"
                    or report.get("installed_browser_gate") != "passed"
                    or report.get("plan") != {"seeds": [0, 12345, 987654321], "reads": 10, "restarts": 3}):
                raise ValueError()
            frozen_available = {item.get("name") for item in spec.get("browser_inventory", []) if item.get("status") == "available"}
            if not available or available != frozen_available or actual != expected or not contracts_complete:
                raise ValueError()
        else:
            run_state = json.loads(spec["soak_state"].read_text(encoding="utf-8"))
            requested_hours = {"preflight": .25, "long": 2, "overnight": 24}[spec["name"]]
            if (report.get("passed") is not True or report.get("completed_duration") is not True
                    or report.get("planned_hours") != requested_hours
                    or report.get("duration_seconds", 0) < requested_hours * 3600
                    or report.get("errors") != 0 or report.get("orphan_processes") != 0
                    or not report.get("cleanup_checks") or not all(report["cleanup_checks"].values())
                    or not report.get("workload_checks") or any(value is False for value in report["workload_checks"].values())
                    or run_state.get("target_profiles") != 2 or run_state.get("min_active") != 1
                    or run_state.get("hours") != requested_hours or run_state.get("min_lifecycle_cycles") != 2
                    or run_state.get("min_copy_cycles") != 2
                    or report.get("lifecycle_cycles", 0) < 2 or report.get("copy_cycles", 0) < 2
                    or report.get("server_cooldown", {}).get("passed") is not True
                    or report.get("min_available_mb") is None):
                raise ValueError()
        return report
    except (OSError, ValueError, TypeError, AttributeError, KeyError, ET.ParseError):
        raise CandidateError("Stage evidence is missing, incomplete, skipped, or failed: " + spec["name"]) from None


def execute_stage(spec, repo, on_update, stop_requested):
    """Keep a child handle until exit; interruption never silently kills a soak."""
    with spec["log"].open("xb", buffering=0) as log:
        child = subprocess.Popen(spec["command"], cwd=repo, env=spec["environment"], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        on_update({"pid": child.pid, "exit_code": None, "child_started_at": now()})
        while child.poll() is None:
            if stop_requested() and spec["soak_state"]:
                stop_file = no_reparse(spec["soak_state"].parent / "STOP")
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text(now(), encoding="utf-8")
            time.sleep(1)
        on_update({"exit_code": child.returncode, "child_finished_at": now()})
        return child.returncode


def run_candidate(options, *, repo=ROOT, inspect=inspect_source, attest=attest_browser,
                  inventory=installed_browsers, execute=execute_stage):
    selected_stages(",".join(options.stages))
    if not 0 < options.shed_free_mb < options.min_free_mb:
        raise CandidateError("Resource thresholds require 0 < shed-free-mb < min-free-mb.")
    output = temp_path(options.output_root, fresh=True)
    repo = no_reparse(repo)
    frozen_source = inspect(repo)
    primary = attest(repo, options.browser)
    browsers = inventory()
    for browser in browsers:
        if browser["status"] == "available":
            result = attest(repo, browser["path"])
            browser.update({"sha256": result.get("sha256"), "version": result.get("version"),
                            "publisher": result.get("publisher")})
    if not any(item.get("status") == "available" and item.get("sha256") == primary.get("sha256")
               and Path(item["path"]).resolve() == Path(primary["path"]).resolve() for item in browsers):
        raise CandidateError("Explicit browser must match an installed Chrome or Edge binary in the captured matrix.")
    output.mkdir()
    state_path = output / STATE_NAME
    state = {"schema": 1, "status": "running", "controller_pid": os.getpid(), "started_at": now(),
             "output_root": str(output), "source_commit": frozen_source["commit"], "current_stage": None,
             "stop_requested": False, "selected_stages": list(options.stages),
             "stages": {name: {"status": "not_run", "pid": None, "exit_code": None} for name in STAGES},
             "scope": "synthetic TEMP acceptance; two profiles with resource adaptation; no seven-profile certificate",
             "external_proxy_dns_stun": "unverified_no_controlled_endpoint", "complete": False}
    manifest = {"schema": 1, "recorded_at": now(), "source": frozen_source, "primary_browser": primary,
                "installed_browsers": browsers, "python": {"executable": sys.executable, "version": sys.version},
                "resource_budget": {"min_free_mb": options.min_free_mb, "shed_free_mb": options.shed_free_mb},
                "stages": list(options.stages), "time_policy": "Each stage is independent; durations from different runs or commits are never added."}
    atomic_json(output / "manifest.json", manifest)
    atomic_json(state_path, state)

    def stopped():
        return (output / "STOP").exists()

    def unchanged():
        if inspect(repo) != frozen_source:
            raise CandidateError("Candidate source or HEAD changed after freeze.")
        records = [primary, *({"path": item["path"], "sha256": item["sha256"]}
                              for item in browsers if item.get("status") == "available")]
        for record in records:
            if sha256(record["path"]) != record["sha256"]:
                raise CandidateError("Browser executable changed after freeze.")

    try:
        for name in options.stages:
            if stopped():
                state["status"] = "stopped"
                state["stop_requested"] = True
                break
            unchanged()
            spec = stage_spec(name, repo, output, options)
            spec["directory"].mkdir()
            spec["candidate_commit"] = frozen_source["commit"]
            spec["browser_inventory"] = browsers
            entry = state["stages"][name]
            entry.update(status="running", started_at=now(), report=str(spec["report"]), log=str(spec["log"]),
                         command=spec["command"], soak_state=str(spec["soak_state"]) if spec["soak_state"] else None)
            state["current_stage"] = name
            atomic_json(state_path, state)

            def update(fields):
                entry.update(fields)
                atomic_json(state_path, state)

            exit_code = execute(spec, repo, update, stopped)
            entry["exit_code"] = exit_code
            entry["finished_at"] = now()
            unchanged()
            if stopped():
                entry["status"] = "stopped"
                state["status"] = "stopped"
                state["stop_requested"] = True
                break
            if exit_code:
                raise CandidateError("Stage exited unsuccessfully: " + name)
            require_report(spec)
            entry["status"] = "passed"
            entry["report_sha256"] = sha256(spec["report"])
            atomic_json(state_path, state)
        else:
            state["complete"] = all(item["status"] == "passed" for item in state["stages"].values())
            state["status"] = "complete" if state["complete"] else "partial"
    except KeyboardInterrupt:
        state["status"] = "controller_interrupted"
        state["failure"] = "Controller interrupted; child PID retained. Verify child/soak cleanup before another run."
    except Exception as error:
        active = state["stages"].get(state["current_stage"])
        if active and active["status"] == "running":
            active["status"] = "failed"
        state["status"] = "failed"
        state["failure"] = str(error) if isinstance(error, CandidateError) else type(error).__name__
    finally:
        state["finished_at"] = now()
        atomic_json(state_path, state)
    return state


def load_state(path):
    path = temp_path(path)
    if path.name != STATE_NAME:
        raise CandidateError("Use the candidate-state.json path.")
    with path.open(encoding="utf-8") as stream:
        state = json.load(stream)
    if state.get("schema") != 1 or no_reparse(state.get("output_root", "")) != path.parent:
        raise CandidateError("Candidate state belongs to a different output root.")
    return path, state


def status(path):
    state_path, state = load_state(path)
    state["stop_file_present"] = (state_path.parent / "STOP").exists()
    state["active_stage"] = state.get("stages", {}).get(state.get("current_stage"), {})
    return state


def request_stop(path):
    path, state = load_state(path)
    no_reparse(path.parent / "STOP").write_text(now(), encoding="utf-8")
    stage = state.get("current_stage")
    if stage in STAGES[2:]:
        # Derive the location from validated root + fixed stage name. Never
        # follow a path or PID read from an editable state file to write/kill.
        results = no_reparse(path.parent / stage / "results")
        results.mkdir(parents=True, exist_ok=True)
        no_reparse(results / "STOP").write_text(now(), encoding="utf-8")
    return {"stop_requested": True, "state": str(path), "process_action": "no kill; active soak receives its STOP file"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--output-root", required=True, type=Path)
    run.add_argument("--browser", required=True, type=Path)
    run.add_argument("--stages", default=",".join(STAGES))
    run.add_argument("--min-free-mb", type=float, default=900)
    run.add_argument("--shed-free-mb", type=float, default=650)
    for action in ("status", "stop"):
        sub.add_parser(action).add_argument("--state", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            print(json.dumps(status(args.state), ensure_ascii=False, indent=2))
            return 0
        if args.action == "stop":
            print(json.dumps(request_stop(args.state), ensure_ascii=False))
            return 0
        options = Options(args.output_root, args.browser, selected_stages(args.stages), args.min_free_mb, args.shed_free_mb)
        state = run_candidate(options)
        print(json.dumps({"status": state["status"], "complete": state["complete"],
                          "state": str(options.output_root / STATE_NAME), "failure": state.get("failure")}))
        return 0 if state["status"] in ("complete", "partial") else 1
    except (CandidateError, OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "error": str(error) if isinstance(error, CandidateError) else type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
