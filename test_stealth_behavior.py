"""Behavioral synthetic Web API regression tests; no browser or real profile."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from antidetect.fingerprint import generate_fingerprint
from antidetect.stealth import get_stealth_script, get_worker_stealth_script


def run_behavior(root, seed=12345, revision=2):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for synthetic JavaScript contract tests")
    root.mkdir(parents=True, exist_ok=True)
    fp = generate_fingerprint("windows").model_copy(update={"fingerprint_algorithm_version": revision})
    fp.canvas_noise_seed = seed
    paths = []
    for name, renderer in (("page", get_stealth_script), ("worker", get_worker_stealth_script)):
        path = root / (name + ".js")
        path.write_text(renderer(fp), encoding="utf-8")
        paths.append(str(path))
    result = subprocess.run(
        [node, str(Path(__file__).parent / "tools" / "fixtures" / "stealth_contracts.cjs"), *paths],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def behavior(tmp_path_factory):
    return run_behavior(tmp_path_factory.mktemp("stealth-contract"))


@pytest.mark.parametrize("realm", ["page", "worker"])
def test_no_public_installation_marker(behavior, realm):
    assert not behavior[realm]["marker"]


@pytest.mark.parametrize("realm", ["page", "worker"])
def test_canvas_crop_matches_full_read(behavior, realm):
    assert behavior[realm]["noisy"]
    assert behavior[realm]["fullTileEqual"]
    assert behavior[realm]["negativeCropEqual"]


@pytest.mark.parametrize("realm", ["page", "worker"])
def test_export_preserves_source_and_repeatability(behavior, realm):
    assert not behavior[realm]["temporarilyChanged"]
    assert behavior[realm]["sourcePreserved"]
    assert behavior[realm]["repeatedExportsEqual"]
    assert behavior[realm]["drawPreserved"]


def test_v2_preserves_native_interfaces(behavior):
    for flag in ("rectsNative", "mediaNative", "adapterNative", "audioNative"):
        assert behavior["page"][flag], flag


@pytest.mark.parametrize("realm", ["page", "worker"])
def test_native_webgl_offsets_and_error_behavior(behavior, realm):
    assert behavior[realm]["pixels"] == [9] * 4 + [77] * 4 + [9] * 12
    assert behavior[realm]["invalidUnchanged"]


@pytest.mark.parametrize("realm", ["page", "worker"])
def test_export_preserves_native_error_and_uninitialized_context(behavior, realm):
    assert behavior[realm]["taintedError"]
    assert behavior[realm]["untouchedContext"]

def test_capabilities_expose_native_v2_and_legacy_limits():
    from antidetect.stealth import get_stealth_capabilities
    fp = generate_fingerprint("windows").model_copy(update={"fingerprint_algorithm_version": 2})
    report = get_stealth_capabilities(fp)
    assert report["profile_separation"] == "canvas_2d_only"
    assert {"webgl", "audio", "fonts", "webgpu", "media_devices"} <= set(report["native_surfaces"])
    assert report["cross_os_rendering"] == "unsupported"
    legacy = fp.model_copy(update={"fingerprint_algorithm_version": 1})
    assert get_stealth_capabilities(legacy)["profile_separation"] == "legacy_unverified"
    fp.stealth_disable = ["canvas"]
    assert get_stealth_capabilities(fp)["profile_separation"] == "none_from_render_hooks"


@pytest.mark.parametrize("revision,disabled,simple,complex_value", [
    (1, False, 0, 12), (2, False, 7, 12), (2, True, 0, 0),
])
def test_live_verifier_checks_selected_canvas_policy(revision, disabled, simple, complex_value):
    from tools.verify_live import _canvas_policy_checks
    fp = generate_fingerprint("windows").model_copy(update={"fingerprint_algorithm_version": revision})
    fp.stealth_disable = ["canvas"] if disabled else []
    checks = _canvas_policy_checks(fp, {"simpleDiff": simple, "complexDiff": complex_value})
    assert all(check["ok"] for check in checks)


@pytest.mark.parametrize("realm", ["page", "worker"])
def test_canvas_preserves_conversion_errors_and_alpha(behavior, realm):
    result = behavior[realm]
    assert result["conversions"] == 1
    assert result["zeroSizeError"] and result["illegalReceiver"]
    assert result["alphaUnchanged"] and result["outsideUnchanged"]


def test_v2_seed_zero_is_distinct_and_legacy_fallback_is_preserved(tmp_path, behavior):
    zero = run_behavior(tmp_path / "zero", seed=0)
    for realm in ("page", "worker"):
        assert zero[realm]["noisy"]
        assert zero[realm]["canvasPixels"] != behavior[realm]["canvasPixels"]
        assert zero[realm]["fullTileEqual"] and zero[realm]["negativeCropEqual"]
    assert zero["page"]["canvasPixels"] == zero["worker"]["canvasPixels"]
    legacy_zero = run_behavior(tmp_path / "legacy-zero", seed=0, revision=1)
    legacy_default = run_behavior(tmp_path / "legacy-default", seed=12345, revision=1)
    for realm in ("page", "worker"):
        assert legacy_zero[realm]["canvasPixels"] == legacy_default[realm]["canvasPixels"]
        assert legacy_zero[realm]["sourcePreserved"]
        assert not legacy_zero[realm]["temporarilyChanged"]
