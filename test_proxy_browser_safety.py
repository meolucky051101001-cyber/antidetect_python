"""Regression coverage for R03, R10 and R11 using isolated fixtures only."""
import collections
import base64
import hashlib
import io
import os
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from antidetect.models import Profile, ProxyConfig
from antidetect.proxy import check_proxy, parse_proxy_line
from antidetect.launcher import BrowserLauncher
from antidetect.profile_locks import ProfileLocks
from antidetect.launch_gate import evaluate_profile
from tools import install_browser


@pytest.mark.parametrize("changes", [
    {"port": 0}, {"host": ""}, {"host": "proxy host"},
    {"host": "http://host"}, {"host": "host:8000"},
    {"protocol": "htpt"}, {"host": "host/user"}, {"host": "proxy;direct"},
])
def test_legacy_proxy_is_readable_but_cannot_build_launch_command(changes):
    proxy = ProxyConfig.model_validate({"enabled": True, "host": "localhost", "port": 8000, **changes})
    assert proxy.configuration_error()
    with pytest.raises(ValueError):
        proxy.to_server_arg()
    with pytest.raises(ValueError):
        ProxyConfig.from_input(proxy)
    proxy.enabled = False
    assert proxy.to_server_arg() == ""


@pytest.mark.parametrize("mode", ["off", "warn", "block"])
@pytest.mark.parametrize("force", [False, True])
def test_launcher_rejects_incomplete_proxy_despite_gate_policy(tmp_path, monkeypatch, mode, force):
    profile = Profile(proxy=ProxyConfig(enabled=True, host="localhost", port=0))
    launcher = BrowserLauncher.__new__(BrowserLauncher)
    launcher._lifecycle_locks = ProfileLocks(str(tmp_path))
    launcher._launch_locks = collections.defaultdict(threading.RLock)
    launcher.manager = SimpleNamespace(get_profile=lambda _: profile, get_profile_dir=Mock())
    launcher._resolve_browser_path = lambda _: __file__
    launcher.is_running = lambda _: False
    spawn = Mock(side_effect=AssertionError("Must not spawn"))
    monkeypatch.setattr("antidetect.launcher.subprocess.Popen", spawn)
    gate = evaluate_profile(profile, browser_paths={profile.id: __file__}, mode=mode, force=force)
    if mode == "block" and not force:
        assert not gate.allowed
        assert any(issue["code"] == "proxy_incomplete" and issue["severity"] == "critical" for issue in gate.issues)
    result = launcher.launch(profile.id)
    assert result["code"] == "INVALID_PROXY_CONFIG"
    assert result["success"] is False
    spawn.assert_not_called()
    launcher.manager.get_profile_dir.assert_not_called()


@pytest.mark.parametrize("protocol", ["http", "https", "socks4", "socks5"])
@pytest.mark.parametrize("user,password", [
    ("user@domain:one", "p@ss:word%+"),
    ("literal%40", "%40%25"),
    ("user+plus", "space + plus"),
    ("người dùng", "mật khẩu"),
    ("", "password"), ("user", ""), ("user", None), (None, None),
])
def test_proxy_url_roundtrip_decodes_credentials_once(protocol, user, password):
    proxy = ProxyConfig(enabled=True, protocol=protocol, host="localhost", port=8888, username=user, password=password)
    parsed = parse_proxy_line(proxy.to_url())
    assert (parsed.protocol, parsed.username, parsed.password) == (protocol, user, password)
    assert parsed.to_url() == proxy.to_url()
    assert "@" not in parsed.to_server_arg()


def test_raw_proxy_fields_are_not_percent_decoded_and_ipv6_is_bracketed():
    proxy = ProxyConfig.from_input({"enabled": True, "host": "::1", "port": 8080, "username": "a%40b", "password": "%40"})
    assert proxy.username == "a%40b"
    assert proxy.password == "%40"
    assert proxy.to_server_arg() == "http://[::1]:8080"
    assert parse_proxy_line(proxy.to_url()).host == "::1"


@pytest.mark.parametrize("port", [-1, 65536, True, "private-secret"])
def test_proxy_input_validation_errors_hide_model_input(port):
    with pytest.raises(ValueError) as exc:
        ProxyConfig.from_input({"enabled": True, "host": "localhost", "port": port, "password": "private-secret"})
    assert "private-secret" not in str(exc.value)


@pytest.mark.parametrize("url", [
    "http://private-user:private-secret@host", "http://private-user:private-secret@host:bad",
    "wrong://private-user:private-secret@host:8080", "http://private-user:private-secret@host:8080/path",
    "http://private-user:private-secret@[bad]:8080",
])
def test_proxy_parse_error_never_echoes_credentials(url):
    with pytest.raises(ValueError) as exc:
        parse_proxy_line(url)
    assert "private-user" not in str(exc.value)
    assert "private-secret" not in str(exc.value)


def test_check_proxy_invalid_transport_never_queries_network(monkeypatch):
    fetch = Mock(side_effect=AssertionError("No network"))
    monkeypatch.setattr("antidetect.proxy.get_public_ip_info", fetch)
    result = check_proxy(ProxyConfig(enabled=True, host="localhost", port=8080, protocol="invalid"))
    assert result["code"] == "INVALID_PROXY_CONFIG"
    fetch.assert_not_called()


@pytest.mark.parametrize("version", [
    "123/../../data-fixture", r"123\..\..\data", "..", r"C:\data", r"\\server\share",
    "155.0.1.2:stream", "155", "155.0.1.2/", "155.0.1.2\n", "155.0.1.*",
])
def test_browser_remove_rejects_invalid_version_before_delete(tmp_path, monkeypatch, version):
    root = tmp_path / "browsers"
    external = tmp_path / "data-fixture"
    external.mkdir()
    marker = external / "session"
    marker.write_text("preserve")
    monkeypatch.setattr(install_browser, "BROWSERS_DIR", str(root))
    delete = Mock(side_effect=AssertionError("Unexpected delete"))
    monkeypatch.setattr(install_browser.shutil, "rmtree", delete)
    with pytest.raises(SystemExit, match="INVALID_BROWSER_VERSION"):
        install_browser.remove_browser(version)
    delete.assert_not_called()
    assert marker.read_text() == "preserve"
    assert not root.exists()


def test_browser_remove_deletes_only_selected_version(tmp_path, monkeypatch):
    root = tmp_path / "browsers"
    for version in ("155.0.1.2", "155.0.1.3"):
        target = root / f"chrome-{version}"
        target.mkdir(parents=True)
        (target / "chrome.exe").write_text("fixture")
    monkeypatch.setattr(install_browser, "BROWSERS_DIR", str(root))
    install_browser.remove_browser("155.0.1.2")
    assert not (root / "chrome-155.0.1.2").exists()
    assert (root / "chrome-155.0.1.3" / "chrome.exe").read_text() == "fixture"


@pytest.mark.parametrize("position", ["root", "target", "child"])
def test_browser_remove_refuses_junctions(tmp_path, monkeypatch, position):
    root = tmp_path / "browsers"
    external = tmp_path / "outside"
    external.mkdir()
    marker = external / "session"
    marker.write_text("preserve")
    target = root / "chrome-155.0.1.2"
    link = {"root": root, "target": target, "child": target / "linked"}[position]
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # A fixture-only junction; no shell expansion of user input.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        link.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(install_browser, "BROWSERS_DIR", str(root))
    try:
        with pytest.raises(SystemExit, match="UNSAFE_BROWSER_PATH"):
            install_browser.remove_browser("155.0.1.2")
        assert marker.read_text() == "preserve"
    finally:
        # Remove only the junction entry, never recursively follow it.
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


def test_browser_remove_rechecks_path_before_recursive_delete(tmp_path, monkeypatch):
    root = tmp_path / "browsers"
    target = root / "chrome-155.0.1.2"
    target.mkdir(parents=True)
    (target / "chrome.exe").write_text("fixture")
    monkeypatch.setattr(install_browser, "BROWSERS_DIR", str(root))
    checked = install_browser._checked_browser_destination
    calls = 0

    def replace_at_final_check(version):
        nonlocal calls
        calls += 1
        if calls == 4:
            target.rename(root / "retained")
            target.mkdir()
        return checked(version)

    monkeypatch.setattr(install_browser, "_checked_browser_destination", replace_at_final_check)
    delete = Mock(side_effect=AssertionError("Unexpected delete"))
    monkeypatch.setattr(install_browser.shutil, "rmtree", delete)
    with pytest.raises(SystemExit, match="đã thay đổi"):
        install_browser.remove_browser("155.0.1.2")
    delete.assert_not_called()
    assert (root / "retained" / "chrome.exe").is_file()


def test_official_browser_install_path_and_checksum_still_work(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("chrome-win64/chrome.exe", b"browser fixture")
    payload = archive.getvalue()
    monkeypatch.setattr(install_browser, "BROWSERS_DIR", str(tmp_path / "browsers"))
    monkeypatch.setattr(install_browser, "fetch_metadata", lambda _: {
        "size": str(len(payload)),
        "md5Hash": base64.b64encode(hashlib.md5(payload).digest()).decode("ascii"),
    })

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

    monkeypatch.setattr(install_browser.urllib.request, "urlopen", lambda *a, **k: Response(payload))
    dest = install_browser.download_and_extract({"version": "155.0.1.2", "url": "https://storage.googleapis.com/fixture.zip"})
    assert (Path(dest) / "chrome-win64" / "chrome.exe").read_bytes() == b"browser fixture"
    assert not Path(dest + ".zip").exists()
    assert install_browser.download_and_extract({"version": "155.0.1.2", "url": "unused"}) == dest


def test_failed_browser_archive_does_not_install_executable(tmp_path, monkeypatch):
    root = tmp_path / "browsers"
    monkeypatch.setattr(install_browser, "BROWSERS_DIR", str(root))
    monkeypatch.setattr(install_browser, "fetch_metadata", lambda _: {"size": "3", "md5Hash": "mismatch"})

    class Response(io.BytesIO):
        headers = {"Content-Length": "3"}

    monkeypatch.setattr(install_browser.urllib.request, "urlopen", lambda *a, **k: Response(b"bad"))
    with pytest.raises(SystemExit, match="Checksum"):
        install_browser.download_and_extract({"version": "155.0.1.2", "url": "https://storage.googleapis.com/fixture.zip"})
    assert not (root / "chrome-155.0.1.2").exists()
    assert not (root / "chrome-155.0.1.2.zip").exists()
