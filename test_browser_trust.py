"""Executable trust tests: synthetic files, native verification, no browser launch."""
import io
import ctypes
import hashlib
import collections
import json
import os
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from antidetect import browser_trust as trust


def pe_fixture():
    body = bytearray(1024)
    body[:2] = b"MZ"
    struct.pack_into("<I", body, 60, 128)
    body[128:132] = b"PE\x00\x00"
    struct.pack_into("<HH", body, 132, 0x8664, 1)
    struct.pack_into("<HH", body, 148, 240, 0x22)
    struct.pack_into("<H", body, 152, 0x20B)
    struct.pack_into("<H", body, 220, 2)
    return bytes(body)


@pytest.fixture
def synthetic_browser(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows file sharing and trust policy")
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(pe_fixture())
    metadata = {"OriginalFilename": "chrome.exe", "ProductName": "Google Chrome",
                "CompanyName": "Google LLC", "ProductVersion": "157.0.1.2"}
    monkeypatch.setattr(trust, "_version_info", lambda path: metadata)
    monkeypatch.setattr(trust, "_verified_signer", lambda path, handle: ("Google LLC", "1" * 64))
    trust._CACHE.clear()
    yield executable, metadata
    trust._CACHE.clear()


@pytest.mark.parametrize("payload", [b"not a PE executable", b"MZ" + b"\x00" * 62])
def test_renamed_nonbrowser_rejected_before_signature_or_metadata(tmp_path, monkeypatch, payload):
    if os.name != "nt":
        pytest.skip("Windows path policy")
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(payload)
    monkeypatch.setattr(trust, "_verified_signer", lambda *a: pytest.fail("Do not trust filename or query signature for malformed PE"))
    monkeypatch.setattr(trust, "_version_info", lambda *a: pytest.fail("Do not trust filename metadata"))
    with pytest.raises(trust.BrowserTrustError, match="PE"):
        trust.verify_browser_executable(executable)


@pytest.mark.parametrize("offset,value", [(132, 0), (134, 0), (148, 0), (150, 0x2002), (152, 0), (220, 1)])
def test_nonapplication_pe_rejected(offset, value):
    body = bytearray(pe_fixture())
    struct.pack_into("<H", body, offset, value)
    with pytest.raises(trust.BrowserTrustError):
        trust._validate_pe(io.BytesIO(body))


@pytest.mark.parametrize("candidate", [r"\\server\share\chrome.exe", r"\\?\C:\chrome.exe",
                                       r"\\.\C:\chrome.exe", r"C:\chrome.exe:stream", "chrome.exe", r"C:chrome.exe"])
def test_nonlocal_or_ambiguous_paths_rejected_before_open(candidate, monkeypatch):
    monkeypatch.setattr(trust.os, "lstat", lambda *a: pytest.fail("Unsafe path must be rejected before filesystem access"))
    with pytest.raises(trust.BrowserTrustError):
        trust._checked_path(candidate)


def test_unsigned_or_invalid_signature_cannot_fall_back_to_version_metadata(synthetic_browser, monkeypatch):
    executable, _metadata = synthetic_browser
    def rejected(*args):
        raise trust.BrowserTrustError("Untrusted signature fixture")
    monkeypatch.setattr(trust, "_verified_signer", rejected)
    monkeypatch.setattr(trust, "_version_info", lambda *a: pytest.fail("Metadata is not signature evidence"))
    with pytest.raises(trust.BrowserTrustError, match="signature fixture"):
        trust.verify_browser_executable(executable)


def test_trusted_os_signature_from_other_publisher_is_not_browser_trust(synthetic_browser, monkeypatch):
    executable, _metadata = synthetic_browser
    monkeypatch.setattr(trust, "_verified_signer", lambda *a: ("Unrelated Trusted Publisher", "2" * 64))
    with pytest.raises(trust.BrowserTrustError, match="identity"):
        trust.verify_browser_executable(executable)


@pytest.mark.parametrize("changes", [{"OriginalFilename": "other.exe"}, {"ProductName": "Unrelated App"},
                                     {"CompanyName": "Other Corp"}, {"ProductVersion": "157.0.1"},
                                     {"ProductVersion": "157.0.1.2 arbitrary"}])
def test_signed_product_identity_must_match_selected_browser(synthetic_browser, changes):
    executable, metadata = synthetic_browser
    metadata.update(changes)
    with pytest.raises(trust.BrowserTrustError, match="identity"):
        trust.verify_browser_executable(executable)


def test_cache_rehashes_even_same_size_timestamp_content(synthetic_browser, monkeypatch):
    executable, _metadata = synthetic_browser
    calls = []
    monkeypatch.setattr(trust, "_verified_signer", lambda *a: (calls.append(True) or "Google LLC", "1" * 64))
    first = trust.verify_browser_executable(executable)
    assert trust.verify_browser_executable(executable) == first
    assert len(calls) == 1
    original = executable.stat()
    changed = bytearray(executable.read_bytes())
    changed[-1] = 1
    executable.write_bytes(changed)
    os.utime(executable, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = trust.verify_browser_executable(executable)
    assert first.file_identity == second.file_identity
    assert first.sha256 != second.sha256
    assert len(calls) == 2


def test_cache_expires_to_recheck_local_trust_state(synthetic_browser, monkeypatch):
    executable, _metadata = synthetic_browser
    now, calls = [0], []
    monkeypatch.setattr(trust.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(trust, "_verified_signer", lambda *a: (calls.append(True) or "Google LLC", "1" * 64))
    trust.verify_browser_executable(executable)
    now[0] = 61
    trust.verify_browser_executable(executable)
    assert len(calls) == 2


def test_held_context_blocks_file_write_and_replacement_until_release(synthetic_browser, tmp_path):
    executable, _metadata = synthetic_browser
    replacement = tmp_path / "replacement.exe"
    replacement.write_bytes(pe_fixture())
    with trust.hold_trusted_browser(executable) as attestation:
        assert attestation.path == str(executable.resolve())
        assert len(attestation.sha256) == 64
        with pytest.raises(OSError):
            executable.write_bytes(b"overwritten")
        with pytest.raises(OSError):
            os.replace(replacement, executable)
    os.replace(replacement, executable)
    assert executable.read_bytes() == pe_fixture()


def test_failure_releases_native_file_handle(synthetic_browser, monkeypatch):
    executable, _metadata = synthetic_browser
    def rejected(*args):
        raise trust.BrowserTrustError("fixture signature failure")
    monkeypatch.setattr(trust, "_verified_signer", rejected)
    with pytest.raises(trust.BrowserTrustError):
        trust.verify_browser_executable(executable)
    executable.write_bytes(b"handle released")
    assert executable.read_bytes() == b"handle released"


@pytest.mark.parametrize("signature_ok", [False, True])
def test_native_signer_only_comes_from_successful_verified_chain_and_state_is_closed(monkeypatch, signature_ok):
    encoded = ctypes.create_string_buffer(b"synthetic certificate encoded bytes")
    certificate = trust._CertContext()
    certificate.encoded = ctypes.cast(encoded, ctypes.POINTER(ctypes.c_ubyte))
    certificate.length = len(encoded.raw)
    provider_certificate = trust._ProviderCert()
    provider_certificate.certificate = ctypes.pointer(certificate)
    signer = trust._ProviderSigner()
    signer.certificate_count = 1
    signer.certificates = ctypes.pointer(provider_certificate)
    actions, chain_reads = [], []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def verify(_window, _action, pointer):
        state = ctypes.cast(pointer, ctypes.POINTER(trust._TrustData)).contents
        actions.append(state.action)
        if state.action == 1:
            assert state.flags & 0x1000 and state.ui == 2  # No network retrieval or UI.
            assert state.file.contents.handle == 123
            state.state = 456
            return 0 if signature_ok else 0x800B0100
        assert state.action == 2 and state.state == 456
        return 0

    def from_state(state):
        assert signature_ok, "Unverified signer must never be inspected"
        chain_reads.append(state)
        return 789

    def from_chain(provider, index, countersigner, counter_index):
        assert (provider, index, countersigner, counter_index) == (789, 0, False, 0)
        return ctypes.pointer(signer)

    def organization(context, kind, flags, oid, output, size):
        assert ctypes.addressof(context.contents) == ctypes.addressof(certificate)
        assert kind == 3 and flags == 0 and ctypes.cast(oid, ctypes.c_char_p).value == b"2.5.4.10"
        output.value = "Google LLC"
        return len(output.value) + 1

    libraries = {
        "wintrust": SimpleNamespace(WinVerifyTrust=Function(verify), WTHelperProvDataFromStateData=Function(from_state),
                                    WTHelperGetProvSignerFromChain=Function(from_chain)),
        "crypt32": SimpleNamespace(CertGetNameStringW=Function(organization)),
    }
    monkeypatch.setattr(trust.ctypes, "WinDLL", lambda name, **kwargs: libraries[name])
    if signature_ok:
        assert trust._verified_signer("synthetic-browser.exe", 123) == (
            "Google LLC", hashlib.sha256(encoded.raw).hexdigest())
        assert chain_reads == [456]
    else:
        with pytest.raises(trust.BrowserTrustError, match="Authenticode"):
            trust._verified_signer("synthetic-browser.exe", 123)
        assert not chain_reads
    assert actions == [1, 2]


def test_ancestor_junction_is_rejected_without_reading_outside(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows junction")
    from test_updater import _directory_link, _unlink_directory_link
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chrome.exe").write_bytes(pe_fixture())
    linked = tmp_path / "linked"
    _directory_link(linked, outside)
    monkeypatch.setattr(trust, "_verified_signer", lambda *a: pytest.fail("Reparse candidate must never reach signature verifier"))
    try:
        with pytest.raises(trust.BrowserTrustError, match="reparse"):
            trust.verify_browser_executable(linked / "chrome.exe")
    finally:
        _unlink_directory_link(linked)


def launcher_fixture(tmp_path, executable):
    from antidetect.launcher import BrowserLauncher
    from antidetect.models import Profile
    from antidetect.profile_locks import ProfileLocks
    profile = Profile(name="synthetic-executable-trust")
    launcher = object.__new__(BrowserLauncher)
    launcher._lifecycle_locks = ProfileLocks(str(tmp_path))
    launcher._launch_locks = collections.defaultdict(threading.RLock)
    launcher.manager = SimpleNamespace(get_profile=lambda _: profile, get_profile_dir=Mock())
    launcher._resolve_browser_path = lambda _: str(executable)
    launcher.is_running = lambda _: False
    return launcher, profile


def test_launch_untrusted_binary_rejects_before_profile_directory_or_native_probe(tmp_path, monkeypatch):
    import antidetect.launcher as launcher_module
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"renamed text file")
    launcher, profile = launcher_fixture(tmp_path, executable)
    spawn = Mock(side_effect=AssertionError("Untrusted executable must never run"))
    monkeypatch.setattr(launcher_module.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher_module, "normalize_fingerprint_for_launch", lambda *a: pytest.fail("No binary use before trust"))
    launcher._measure_native_brands_standalone = lambda *a: pytest.fail("No native browser probe before trust")
    result = launcher.launch(profile.id)
    assert result["code"] == "BROWSER_UNTRUSTED"
    assert result["success"] is False and result["browser_started"] is False and result["navigation_allowed"] is False
    launcher.manager.get_profile_dir.assert_not_called()
    spawn.assert_not_called()


def test_launch_rechecks_changed_binary_after_initial_trust_before_spawn(synthetic_browser, tmp_path, monkeypatch):
    import antidetect.launcher as launcher_module
    executable, _metadata = synthetic_browser
    launcher, profile = launcher_fixture(tmp_path, executable)
    profile_directory = tmp_path / "synthetic-profile"
    def swap_after_initial_verification(_profile_id):
        executable.write_bytes(b"changed after initial attestation")
        return str(profile_directory)
    launcher.manager.get_profile_dir.side_effect = swap_after_initial_verification
    launcher.active_ports = {}
    launcher._requested_ports = {}
    launcher._prepare_clean_chrome_preferences = Mock()
    launcher.instance_id = "synthetic-instance"
    launcher.cdp_health = SimpleNamespace(begin_launch=lambda *a: "synthetic-generation")
    launcher._clear_process = Mock(return_value=True)
    launcher._cdp_ready_events = {}
    launcher._core_verified_events = {}
    launcher._navigation_events = {}
    monkeypatch.setattr(launcher_module, "get_free_port", lambda **k: 9876)
    monkeypatch.setattr(launcher_module, "normalize_fingerprint_for_launch", lambda fp, path: fp)
    spawn = Mock(side_effect=AssertionError("Changed executable must never run"))
    monkeypatch.setattr(launcher_module.subprocess, "Popen", spawn)
    result = launcher.launch(profile.id)
    assert result["success"] is False
    assert result["code"] == "BROWSER_UNTRUSTED"
    assert result["browser_started"] is False and result["navigation_allowed"] is False
    launcher._clear_process.assert_called_once_with(profile.id)
    spawn.assert_not_called()


def test_standalone_native_brand_probe_cannot_execute_untrusted_file(tmp_path, monkeypatch):
    import antidetect.launcher as launcher_module
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"untrusted native probe fixture")
    launcher = object.__new__(launcher_module.BrowserLauncher)
    launcher._wait_directory_idle = lambda *a: True
    spawn = Mock(side_effect=AssertionError("Untrusted native baseline executable must never run"))
    monkeypatch.setattr(launcher_module.subprocess, "Popen", spawn)
    assert launcher._measure_native_brands_standalone(str(executable)) is None
    spawn.assert_not_called()


@pytest.mark.parametrize("read_fails", [False, True])
@pytest.mark.parametrize("directory_idle", [False, True])
def test_native_brand_probe_closes_owned_socket_on_success_or_read_failure_and_requires_idle(
        synthetic_browser, tmp_path, monkeypatch, read_fails, directory_idle):
    import antidetect.launcher as launcher_module
    import tempfile
    executable, _metadata = synthetic_browser
    probe_directory = tmp_path / "synthetic-native-probe"
    probe_directory.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **k: str(probe_directory))
    monkeypatch.setattr(launcher_module, "get_free_port", lambda: 9876)
    release = Mock()
    monkeypatch.setattr(launcher_module, "release_port", release)
    proc = SimpleNamespace(wait=Mock(), terminate=Mock(), kill=Mock())
    spawn = Mock(return_value=proc)
    monkeypatch.setattr(launcher_module.subprocess, "Popen", spawn)
    targets = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9876/devtools/page/synthetic-owned"}]
    monkeypatch.setattr(launcher_module, "open_loopback", lambda *a, **k: io.BytesIO(json.dumps(targets).encode()))
    messages, sockets = [], []
    brands = {"brands": [{"brand": "Chromium", "version": "157"}]}

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            messages.append(json.loads(message))

        async def recv(self):
            if read_fails:
                raise ConnectionError("Synthetic native read failure")
            return json.dumps({"id": 70002, "result": {"result": {"value": brands}}})

    def connect(url, **kwargs):
        sockets.append(url)
        return Socket()

    monkeypatch.setattr(launcher_module, "connect_loopback", connect)
    launcher = object.__new__(launcher_module.BrowserLauncher)
    observed_directories = []
    def idle(directory):
        observed_directories.append(directory)
        return directory_idle
    launcher._wait_directory_idle = idle
    if directory_idle:
        assert launcher._measure_native_brands_standalone(str(executable)) == (None if read_fails else brands)
        assert not probe_directory.exists()
        diagnostic = launcher._native_brand_probe_diagnostic
        assert diagnostic["status"] == ("failed" if read_fails else "passed")
        assert diagnostic["stage"] == "native_read"
        assert diagnostic["error"] == ("PROBE_TRANSPORT_OR_RUNTIME_ERROR" if read_fails else None)
    else:
        with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
            launcher._measure_native_brands_standalone(str(executable))
        assert probe_directory.exists(), "Unconfirmed process state must retain its directory"
    assert [message["method"] for message in messages] == ["Page.navigate", "Runtime.evaluate", "Browser.close"]
    assert sockets == [targets[0]["webSocketDebuggerUrl"]], "Cleanup must use the existing owned socket"
    assert observed_directories == [str(probe_directory)]
    spawned_args = spawn.call_args[0][0]
    for flag in (
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-sync",
    ):
        assert flag in spawned_args
    proc.wait.assert_called_once_with(timeout=5)
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()
    release.assert_called_once_with(9876)


def test_native_probe_uses_total_read_budget_and_distinct_evaluation_ids(synthetic_browser, tmp_path, monkeypatch):
    import asyncio
    import tempfile
    import antidetect.launcher as launcher_module

    executable, _metadata = synthetic_browser
    directory = tmp_path / "synthetic-native-read-budget"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kwargs: str(directory))
    monkeypatch.setattr(launcher_module, "get_free_port", lambda: 9876)
    monkeypatch.setattr(launcher_module, "release_port", lambda port: None)
    proc = SimpleNamespace(wait=Mock(), terminate=Mock(), kill=Mock())
    monkeypatch.setattr(launcher_module.subprocess, "Popen", lambda *a, **k: proc)
    target = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9876/devtools/page/synthetic"}]
    monkeypatch.setattr(launcher_module, "open_loopback", lambda *a, **k: io.BytesIO(json.dumps(target).encode()))
    messages, timeouts = [], []
    brands = {"brands": [{"brand": "Chromium", "version": "157"}]}

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            messages.append(json.loads(message))

        async def recv(self):
            command = messages[-1]
            value = None if command["id"] == 70002 else brands
            return json.dumps({"id": command["id"], "result": {"result": {"value": value}}})

    original_wait = asyncio.wait_for
    async def observe_wait(awaitable, timeout):
        timeouts.append(timeout)
        return await original_wait(awaitable, timeout)

    monkeypatch.setattr(launcher_module, "connect_loopback", lambda *a, **k: Socket())
    monkeypatch.setattr(asyncio, "wait_for", observe_wait)
    launcher = object.__new__(launcher_module.BrowserLauncher)
    launcher._wait_directory_idle = lambda *_args: True

    assert launcher._measure_native_brands_standalone(str(executable)) == brands
    evaluations = [message["id"] for message in messages if message["method"] == "Runtime.evaluate"]
    assert evaluations == [70002, 70003]
    assert all(2 < timeout <= 6 for timeout in timeouts), "Read must use the unchanged six-second aggregate budget"
    assert messages[-1] == {"id": 79999, "method": "Browser.close"}
    assert launcher._native_brand_probe_diagnostic["status"] == "passed"


@pytest.mark.skipif(os.name != "nt" or os.environ.get("ANTIDETECT_RUN_BROWSER_TRUST") != "1",
                    reason="Explicit installed-browser read-only Authenticode opt-in")
@pytest.mark.parametrize("relative,publisher", [("Google/Chrome/Application/chrome.exe", "Google LLC"),
                                                ("Microsoft/Edge/Application/msedge.exe", "Microsoft Corporation")])
def test_installed_browser_signature_readonly(relative, publisher):
    paths = [Path(os.environ[key]) / relative for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
             if os.environ.get(key)]
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        pytest.skip("Browser is not installed")
    result = trust.verify_browser_executable(path)
    assert result.publisher == publisher
    assert result.original_filename.lower() == path.name.lower()
    assert len(result.sha256) == len(result.certificate_sha256) == 64
