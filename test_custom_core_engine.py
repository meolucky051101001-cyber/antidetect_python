"""Step 3 tests: local SHA-256 custom C++ browser core trust & Fingerprint-Chromium flags."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from antidetect import browser_trust as trust
from antidetect.custom_cores import (
    build_cpp_engine_flags,
    list_custom_cores,
    pin_custom_core,
    remove_custom_core,
)
from antidetect.fingerprint import generate_fingerprint
from antidetect.profile_import import prepare_import_profile
from antidetect.stealth import get_stealth_capabilities, get_stealth_script
from test_browser_trust import launcher_fixture, pe_fixture
from tools import pin_custom_core as pin_cli


def test_unpinned_custom_chromium_rejected_and_pinned_sha256_accepted_with_write_lock(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows file sharing and trust policy")
    manifest = tmp_path / "browsers" / "trusted_cores.json"
    core_dir = tmp_path / "browsers" / "fingerprint-chromium-142.0.7444.59"
    core_dir.mkdir(parents=True)
    exe = core_dir / "chromium.exe"
    exe.write_bytes(pe_fixture())

    # Without a pin entry, unsigned chromium.exe is rejected before WinVerifyTrust.
    monkeypatch.setattr(trust, "_verified_signer", lambda *a: pytest.fail("Unsigned custom core must not pass unpinned"))
    with pytest.raises(trust.BrowserTrustError):
        trust.verify_browser_executable(exe, custom_manifest_path=manifest)

    # Pin via CLI & verify.
    assert pin_cli.main([
        "--manifest", str(manifest),
        "pin",
        "--exe", str(exe),
        "--engine", "fingerprint-chromium",
        "--version", "142.0.7444.59",
    ]) == 0
    assert len(list_custom_cores(manifest_path=str(manifest))) == 1

    with trust.hold_trusted_browser(exe, custom_manifest_path=manifest) as att:
        assert att.engine == "fingerprint-chromium"
        assert att.verification_mode == "local_sha256_pinned_custom_core"
        assert att.version == "142.0.7444.59"
        # Native handle denies concurrent overwrite while held.
        with pytest.raises(OSError):
            exe.write_bytes(b"tampered")

    # Mutating 1 byte after release must fail closed immediately with BROWSER_UNTRUSTED.
    tampered = bytearray(exe.read_bytes())
    tampered[-1] ^= 0xFF
    exe.write_bytes(bytes(tampered))
    with pytest.raises(trust.BrowserTrustError, match="SHA-256"):
        trust.verify_browser_executable(exe, custom_manifest_path=manifest)

    # Restore bytes, then revoke pin -> immediately untrusted again.
    exe.write_bytes(pe_fixture())
    assert remove_custom_core(str(exe), manifest_path=str(manifest)) is True
    with pytest.raises(trust.BrowserTrustError):
        trust.verify_browser_executable(exe, custom_manifest_path=manifest)


def test_cpp_engine_flags_and_stealth_capabilities_omit_redundant_js_canvas_hooks():
    fp = generate_fingerprint(target_os="windows")
    fp.canvas_noise_seed = 987654
    fp.hardware_concurrency = 12
    fp.timezone = "Asia/Ho_Chi_Minh"

    flags = build_cpp_engine_flags("fingerprint-chromium", fp)
    assert "--fingerprint=987654" in flags
    assert "--fingerprint-platform=windows" in flags
    assert "--fingerprint-hardware-concurrency=12" in flags
    assert "--timezone=Asia/Ho_Chi_Minh" in flags

    caps = get_stealth_capabilities(fp, cpp_engine="fingerprint-chromium")
    assert caps["cpp_engine"] == "fingerprint-chromium"
    assert caps["profile_separation"] == "cpp_engine_full"
    assert caps["canvas_2d"] == "cpp_seeded_blink"

    # JS payload disables JS canvas wrapper when C++ engine handles Canvas natively.
    js = get_stealth_script(fp, cpp_engine="fingerprint-chromium")
    assert '"canvas"' in js

    # Authentic mode (canvas disabled) omits --fingerprint seed flag.
    fp.stealth_disable = ["canvas"]
    flags_authentic = build_cpp_engine_flags("fingerprint-chromium", fp)
    assert not any(f.startswith("--fingerprint=") for f in flags_authentic)


def test_profile_import_still_rejects_custom_browser_path():
    with pytest.raises(ValueError, match="browser_path"):
        prepare_import_profile(
            {"name": "Imported", "browser_path": "fingerprint-chromium-142"},
            0,
            lambda pages: pages or [],
        )


def test_launcher_passes_cpp_flags_when_launching_pinned_fingerprint_chromium(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows trust policy")
    import antidetect.launcher as launcher_module

    manifest = tmp_path / "trusted_cores.json"
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(pe_fixture())
    pin_custom_core(
        str(exe),
        engine="fingerprint-chromium",
        version="142.0.7444.59",
        manifest_path=str(manifest),
    )

    launcher, profile = launcher_fixture(tmp_path, exe)
    profile.fingerprint.browser_version = "142.0.7444.59"
    profile.fingerprint.canvas_noise_seed = 555777
    profile.fingerprint.fingerprint_algorithm_version = 2
    profile.fingerprint.verification_mode = "compatibility"
    launcher.manager.get_profile_dir = lambda _pid: str(tmp_path / "prof")
    launcher.active_processes = {}
    launcher.active_ports = {}
    launcher._requested_ports = {}
    launcher._recovery_required = set()
    launcher._proxy_credentials = {}
    launcher._prepare_clean_chrome_preferences = Mock()
    launcher.instance_id = "cpp-test-instance"
    launcher._runtime_store = SimpleNamespace(upsert=Mock(), get=lambda _pid: {"port": 9888, "launch_id": "gen-cpp"})
    launcher.cdp_health = SimpleNamespace(
        begin_launch=lambda *a: "gen-cpp",
        set_process=Mock(),
        snapshot=lambda _pid: {"launch_id": "gen-cpp", "owner_instance": "cpp-test-instance", "state": "healthy"},
        mark_healthy=lambda *a: True,
    )
    launcher._persist_health_now = Mock(return_value=True)
    launcher._cdp_ready_events = {}
    launcher._core_verified_events = {}
    launcher._navigation_events = {}
    import threading
    launcher._native_brands_cache = {"142.0.7444.59": {"brands": [{"brand": "Chromium", "version": "142"}]}}
    launcher._native_brands_probe_lock = threading.Lock()

    monkeypatch.setenv("ANTIDETECT_CUSTOM_CORES_MANIFEST", str(manifest))
    monkeypatch.setattr(launcher_module, "browser_pids_using_dir", lambda _d: set())
    monkeypatch.setattr(launcher_module, "get_free_port", lambda **k: 9888)
    monkeypatch.setattr(launcher_module, "browser_process_birth", lambda _pid: 123456)
    monkeypatch.setattr(launcher_module, "normalize_fingerprint_for_launch", lambda fp, _p: fp)

    proc = SimpleNamespace(pid=8888, poll=lambda: None, wait=Mock(), terminate=Mock(), kill=Mock())
    spawn = Mock(return_value=proc)
    monkeypatch.setattr(launcher_module.subprocess, "Popen", spawn)

    observed_cdp_kwargs = {}

    def fake_start_cdp_thread(*args, **kwargs):
        observed_cdp_kwargs.update(kwargs)
        launcher._cdp_ready_events[profile.id].set()

    launcher._start_cdp_thread = fake_start_cdp_thread
    async def ok_ready(_port):
        return True

    async def ok_verify(_port, _fp):
        return {"ok": True, "checks": {"webdriver": True}}

    launcher._probe_page_ready = ok_ready
    launcher._verify_emulation = ok_verify

    res = launcher.launch(profile.id)
    assert res["success"] is True
    assert res["degraded"] is False
    assert res["fingerprint_capabilities"]["profile_separation"] == "cpp_engine_full"
    assert observed_cdp_kwargs.get("cpp_engine") == "fingerprint-chromium"
    spawned_args = spawn.call_args[0][0]
    assert "--fingerprint=555777" in spawned_args
    assert "--fingerprint-platform=windows" in spawned_args
