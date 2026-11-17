"""Candidate orchestration boundary tests; stage bodies are synthetic."""
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from tools import audit_candidate as candidate


def clean_repo(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Synthetic Fixture"], check=True)
    (repo / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "synthetic fixture"], check=True)
    return repo


def attest(_repo, path):
    return {"path": str(path), "sha256": candidate.sha256(path), "version": "157.0.1.2", "publisher": "Google LLC"}


def browser_fixture(tmp_path):
    path = tmp_path / "chrome.exe"
    path.write_bytes(b"synthetic binary fixture; never executed")
    return path


def matrix_report(spec):
    names = {"page", "offscreen", "same_origin_frame", "cross_origin_frame", "cross_site_oopif",
             "classic_worker", "module_worker", "nested_worker", "transferred_offscreen_worker",
             "shared_worker", "service_worker", "reloaded_page"}
    def realm(mode):
        return {name: {"readRepetitions": 10, "decodedExportMatchesRead": True,
                       "initialRealmRead": {"noisy": mode == "unique"}}
                for name in names}
    scenarios = []
    for seed in (0, 12345, 987654321):
        scenarios.append({"browser": "chrome", "seed": seed,
                          "evidence": {"status": "passed", "source_commit": spec["candidate_commit"],
                                       "browser_binary_sha256": spec["browser_inventory"][0]["sha256"],
                                       "browser_version": "157.0.1.2", "modes": {
                                           "native": {"realms": realm("authentic")},
                                           **{mode: {"runs": [{"realms": realm(mode),
                                                                  "final_health": {"state": "healthy"}}
                                                         for _ in range(4)]}
                                              for mode in ("unique", "authentic")}}}})
    spec["report"].write_text(json.dumps({"status": "passed", "exhaustive_gate": "passed",
                                          "installed_browser_gate": "passed",
                                          "plan": {"seeds": [0, 12345, 987654321], "reads": 10, "restarts": 3},
                                          "browser_inventory": [{"name": "chrome", "status": "available",
                                                                 "sha256": spec["browser_inventory"][0]["sha256"]}],
                                          "scenarios": scenarios}), encoding="utf-8")


def passed_report(spec):
    if spec["name"] == "restore":
        spec["report"].write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        xml = ET.Element("testsuites")
        suite = ET.SubElement(xml, "testsuite")
        ET.SubElement(suite, "testcase", name="test_offline_v1_v3_backup_restore_round_trip")
        ET.ElementTree(xml).write(spec["directory"] / "tests.xml")
    elif spec["name"] == "fingerprint":
        spec["report"].write_text(json.dumps({"status": "passed", "exhaustive_gate": "passed",
                                              "installed_browser_gate": "passed",
                                              "plan": {"seeds": [0, 12345, 987654321], "reads": 10, "restarts": 3},
                                              "browser_inventory": [{"name": "chrome", "status": "available"}],
                                              "scenarios": []}), encoding="utf-8")
    else:
        summary = {"passed": True, "completed_duration": True,
                   "planned_hours": {"preflight": .25, "long": 2, "overnight": 24}[spec["name"]],
                   "errors": 0, "orphan_processes": 0,
                   "cleanup_checks": {"exit": True}, "workload_checks": {"canvas": True}}
        spec["report"].parent.mkdir(parents=True)
        spec["report"].write_text(json.dumps(summary), encoding="utf-8")


def test_candidate_requires_clean_tracked_and_untracked_tree(tmp_path):
    repo = clean_repo(tmp_path)
    (repo / "untracked fixture.txt").write_text("synthetic", encoding="utf-8")
    with pytest.raises(candidate.CandidateError, match="clean tracked and untracked"):
        candidate.inspect_source(repo)


def test_source_manifest_excludes_browser_profile_payloads(tmp_path, monkeypatch):
    repo = clean_repo(tmp_path)
    cookie = repo / "profiles" / "synthetic" / "Default" / "Cookies"
    database = repo / "fixtures" / "synthetic-profile.sqlite"
    cookie.parent.mkdir(parents=True)
    database.parent.mkdir(parents=True)
    cookie.write_bytes(b"synthetic-only cookie fixture")
    database.write_bytes(b"synthetic-only database fixture")
    subprocess.run(["git", "-C", str(repo), "add", "profiles", "fixtures"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "synthetic ignored payload fixtures"], check=True)
    reads = []
    real_sha = candidate.sha256
    monkeypatch.setattr(candidate, "sha256", lambda path: (reads.append(Path(path)), real_sha(path))[1])
    manifest = candidate.inspect_source(repo)
    assert manifest["excluded_sensitive_tracked_entries"] == 2
    assert set(manifest["files"]) == {"source.py"}
    assert cookie not in reads and database not in reads


@pytest.mark.parametrize("selected", ["long", "overnight", "restore,restore", "fingerprint,restore"])
def test_selected_stages_must_be_ordered_complete_prefix_for_longrun(selected):
    with pytest.raises(candidate.CandidateError):
        candidate.selected_stages(selected)


def test_omitted_stages_remain_not_run_and_candidate_never_claims_complete(tmp_path):
    repo = clean_repo(tmp_path)
    output = tmp_path / "output"
    browser = browser_fixture(tmp_path)
    state = candidate.run_candidate(candidate.Options(output, browser, ("restore",)),
                                    repo=repo, attest=attest,
                                    inventory=lambda: [{"name": "chrome", "path": str(browser), "status": "available"}],
                                    execute=lambda spec, *_args: (passed_report(spec) or 0))
    assert state["status"] == "partial"
    assert state["complete"] is False
    assert state["stages"]["restore"]["status"] == "passed"
    assert all(state["stages"][name]["status"] == "not_run" for name in candidate.STAGES[1:])


def test_partial_candidate_returns_nonzero_cli_status(tmp_path, monkeypatch):
    browser = browser_fixture(tmp_path)
    monkeypatch.setattr(candidate, "run_candidate", lambda _options: {"status": "partial", "complete": False})
    assert candidate.main(["run", "--output-root", str(tmp_path / "output"), "--browser", str(browser),
                           "--stages", "restore"]) == 1


def test_stage_failure_prevents_every_later_soak_stage(tmp_path):
    repo = clean_repo(tmp_path)
    calls = []
    browser = browser_fixture(tmp_path)
    def execute(spec, _repo, update, _stop):
        calls.append(spec["name"])
        update({"pid": 1234, "exit_code": 1})
        return 1
    state = candidate.run_candidate(candidate.Options(tmp_path / "output", browser,
                                                      ("restore", "fingerprint", "preflight")),
                                    repo=repo, attest=attest,
                                    inventory=lambda: [{"name": "chrome", "path": str(browser), "status": "available"}],
                                    execute=execute)
    assert calls == ["restore"]
    assert state["status"] == "failed"
    assert state["stages"]["restore"]["status"] == "failed"
    assert state["stages"]["fingerprint"]["status"] == "not_run"
    assert state["stages"]["preflight"]["status"] == "not_run"


def test_candidate_stops_when_frozen_source_changes_between_stages(tmp_path):
    repo = clean_repo(tmp_path)
    browser = browser_fixture(tmp_path)
    calls = []
    inspect_calls = 0
    real_inspect = candidate.inspect_source
    def mutate_after_first_stage(root):
        nonlocal inspect_calls
        inspect_calls += 1
        snapshot = real_inspect(root)
        if inspect_calls == 3:
            (root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        return snapshot
    def execute(spec, *_args):
        calls.append(spec["name"])
        passed_report(spec)
        return 0
    state = candidate.run_candidate(candidate.Options(tmp_path / "output", browser,
                                                      ("restore", "fingerprint")),
                                    repo=repo, inspect=mutate_after_first_stage, attest=attest,
                                    inventory=lambda: [{"name": "chrome", "path": str(browser), "status": "available"}], execute=execute)
    assert calls == ["restore"]
    assert state["status"] == "failed"
    assert state["stages"]["restore"]["status"] == "passed"
    assert state["stages"]["fingerprint"]["status"] == "not_run"
    assert "Git state" in state["failure"] or "source or HEAD changed" in state["failure"]


def test_missing_browser_matrix_evidence_cannot_pass_exhaustive_gate(tmp_path):
    browser = browser_fixture(tmp_path)
    spec = {"name": "fingerprint", "report": tmp_path / "fingerprint.json", "candidate_commit": "fixture",
            "browser_inventory": [{"name": "chrome", "sha256": candidate.sha256(browser), "status": "available"}]}
    report = {"status": "passed", "exhaustive_gate": "passed", "installed_browser_gate": "passed",
              "plan": {"seeds": [0, 12345, 987654321], "reads": 10, "restarts": 3},
              "browser_inventory": [{"name": "chrome", "status": "available"}], "scenarios": []}
    spec["report"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(candidate.CandidateError, match="missing, incomplete"):
        candidate.require_report(spec)


def test_stage_exit_without_machine_readable_evidence_fails(tmp_path):
    report = tmp_path / "results" / "soak_summary.json"
    spec = {"name": "preflight", "report": report, "soak_state": tmp_path / "missing-state.json"}
    report.parent.mkdir()
    report.write_text(json.dumps({"passed": True, "completed_duration": True, "planned_hours": .25,
                                  "errors": 0, "orphan_processes": 0,
                                  "cleanup_checks": {"exit": True}, "workload_checks": {"canvas": True}}), encoding="utf-8")
    with pytest.raises(candidate.CandidateError, match="missing, incomplete"):
        candidate.require_report(spec)


def test_complete_matrix_requires_native_and_all_three_modes_in_twelve_realms(tmp_path):
    browser = browser_fixture(tmp_path)
    spec = {"name": "fingerprint", "report": tmp_path / "fingerprint.json", "candidate_commit": "fixture",
            "browser_inventory": [{"name": "chrome", "sha256": candidate.sha256(browser), "status": "available"}]}
    matrix_report(spec)
    candidate.require_report(spec)


def test_stage_commands_use_serial_acceptance_schedule_and_temp_data(tmp_path):
    repo = clean_repo(tmp_path)
    browser = browser_fixture(tmp_path)
    output = tmp_path / "candidate-output"
    options = candidate.Options(output, browser)
    restore = candidate.stage_spec("restore", repo, output, options)
    fingerprint = candidate.stage_spec("fingerprint", repo, output, options)
    preflight = candidate.stage_spec("preflight", repo, output, options)
    long = candidate.stage_spec("long", repo, output, options)
    overnight = candidate.stage_spec("overnight", repo, output, options)
    assert restore["command"][1:4] == ["-m", "pytest", "-q"]
    assert "--exhaustive" in fingerprint["command"]
    assert fingerprint["command"][-2:] == ["--browser-matrix", "installed"]
    for spec, hours, cycle, copy in ((preflight, "0.25", "180", "240"),
                                     (long, "2", "600", "900"),
                                     (overnight, "24", "1800", "3600")):
        command = spec["command"]
        assert command[command.index("--hours") + 1] == hours
        assert command[command.index("--profiles") + 1] == "2"
        assert command[command.index("--min-active") + 1] == "1"
        assert command[command.index("--sample-interval") + 1] == "15"
        assert command[command.index("--tab-interval") + 1] == "60"
        assert command[command.index("--cycle-interval") + 1] == cycle
        assert command[command.index("--copy-interval") + 1] == copy
        assert command[command.index("--min-free-mb") + 1] == "900.0"
        assert command[command.index("--shed-free-mb") + 1] == "650.0"
        assert Path(spec["environment"]["ANTIDETECT_DATA_DIR"]).is_relative_to(output)


def test_stop_only_writes_validated_fixed_paths_and_never_kills_pid(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    state_path = root / candidate.STATE_NAME
    state_path.write_text(json.dumps({"schema": 1, "output_root": str(root), "current_stage": "long",
                                      "stages": {"long": {"pid": 99999999}}}), encoding="utf-8")
    result = candidate.request_stop(state_path)
    assert result["process_action"].startswith("no kill")
    assert (root / "STOP").exists()
    assert (root / "long" / "results" / "STOP").exists()


@pytest.mark.parametrize("status", ["failed", "controller_interrupted", "stopped", "partial"])
def test_resumed_state_never_becomes_a_pass_from_saved_stage_status(tmp_path, status):
    state = {"status": status, "complete": False,
             "stages": {name: {"status": "passed" if name == "restore" else "not_run"} for name in candidate.STAGES}}
    assert state["complete"] is False
    assert any(item["status"] != "passed" for item in state["stages"].values())
