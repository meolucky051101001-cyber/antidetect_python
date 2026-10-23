"""Regression tests for the 2026-10-23 P0/P1 hardening fixes."""
import atexit
import os
import shutil
import sys
import tempfile
import time

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

TEST_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-hardening-")
os.environ["ANTIDETECT_DATA_DIR"] = TEST_DATA_DIR
atexit.register(shutil.rmtree, TEST_DATA_DIR, True)

from antidetect.models import Profile, ProxyConfig
from antidetect.storage import ProfileManager, ProfileDeletionError
from antidetect.secrets import (
    SecretStoreCorruption,
    get_proxy_password,
    set_proxy_password,
)
from antidetect.fingerprint import (
    build_user_agent,
    detect_browser_brand,
    locale_for_country,
    normalize_fingerprint_for_launch,
)
from antidetect.launcher import (
    BrowserLauncher,
    BrowserRuntimeStore,
    build_webrtc_flags,
    get_free_port,
    release_port,
)
import antidetect.storage as storage_module
import antidetect.secrets as secrets_module
import antidetect.launcher as launcher_module
import antidetect.server_control as server_control


# Use one manager per test file; profile IDs are unique so tests do not collide.
manager = ProfileManager()


@pytest.fixture(autouse=True)
def _pin_data_dir():
    """Keep secrets.py's dynamic env lookup aligned with storage.DATA_DIR."""
    old = os.environ.get("ANTIDETECT_DATA_DIR")
    os.environ["ANTIDETECT_DATA_DIR"] = storage_module.DATA_DIR
    yield
    if old is None:
        os.environ.pop("ANTIDETECT_DATA_DIR", None)
    else:
        os.environ["ANTIDETECT_DATA_DIR"] = old


def _proxy(host="1.2.3.4", port=8080, password="secret"):
    return ProxyConfig(
        enabled=True,
        protocol="http",
        host=host,
        port=port,
        username="user",
        password=password,
    )


def test_proxy_password_is_bound_to_identity():
    p = manager.create_profile("secret-identity", proxy=_proxy())
    assert get_proxy_password(p.id, identity=p.proxy.identity_key()) == "secret"

    # Same endpoint, blank password: keep the old secret.
    loaded = manager.get_profile(p.id)
    loaded.proxy.password = ""
    updated = manager.update_profile(loaded)
    assert updated.proxy.password == "secret"

    # Changed endpoint, blank password: the old secret must not follow.
    loaded = manager.get_profile(p.id)
    loaded.proxy.host = "9.9.9.9"
    loaded.proxy.port = 9090
    loaded.proxy.password = ""
    updated = manager.update_profile(loaded)
    assert updated.proxy.password is None
    assert get_proxy_password(p.id, identity=updated.proxy.identity_key()) is None
    assert get_proxy_password(p.id, identity=_proxy().identity_key()) is None

    # A new password is bound to the new identity only.
    loaded = manager.get_profile(p.id)
    loaded.proxy.password = "new-secret"
    updated = manager.update_profile(loaded)
    assert get_proxy_password(p.id, identity=updated.proxy.identity_key()) == "new-secret"
    assert get_proxy_password(p.id, identity=_proxy().identity_key()) is None
    manager.delete_profile(p.id)


def test_delete_failure_preserves_metadata_and_secret(monkeypatch):
    p = manager.create_profile("delete-locked", proxy=_proxy())
    assert get_proxy_password(p.id, identity=p.proxy.identity_key()) == "secret"

    original_rmtree = storage_module.shutil.rmtree
    monkeypatch.setattr(storage_module.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr(storage_module.time, "sleep", lambda *_: None)
    try:
        with pytest.raises(ProfileDeletionError):
            manager.delete_profile(p.id)
    finally:
        monkeypatch.setattr(storage_module.shutil, "rmtree", original_rmtree)

    assert manager.get_profile(p.id) is not None
    assert get_proxy_password(p.id, identity=p.proxy.identity_key()) == "secret"
    assert manager.delete_profile(p.id) is True
    assert get_proxy_password(p.id) is None


def test_corrupt_secret_store_is_backed_up_and_not_overwritten():
    p = manager.create_profile("corrupt-store", proxy=_proxy(password="original"))
    target = os.path.join(storage_module.DATA_DIR, "secrets.dpapi")
    original = open(target, "rb").read()
    try:
        with open(target, "wb") as f:
            f.write(b"definitely not a DPAPI blob")
        corrupt_bytes = open(target, "rb").read()

        assert get_proxy_password(p.id) is None
        with pytest.raises(SecretStoreCorruption):
            set_proxy_password(p.id, "attacker", identity=p.proxy.identity_key())
        assert open(target, "rb").read() == corrupt_bytes
        backups = [n for n in os.listdir(storage_module.DATA_DIR) if ".corrupt." in n]
        assert backups
    finally:
        with open(target, "wb") as f:
            f.write(original)
        secrets_module._store_corrupted = False
        manager.delete_profile(p.id)


def test_interprocess_lock_timeout_is_fail_closed(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows msvcrt lock test")
    import msvcrt
    from antidetect.storage import InterProcessLock

    monkeypatch.setattr(msvcrt, "locking", lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(TimeoutError):
        with InterProcessLock(timeout=0.15):
            pass


def test_runtime_state_recovery_and_port_reservation(monkeypatch):
    p = manager.create_profile("runtime-recovery")
    user_data_dir = os.path.join(storage_module.DATA_DIR, "profiles", p.id, "user_data")
    store = BrowserRuntimeStore(os.path.join(storage_module.DATA_DIR, "browser_runtime.json"))
    store.upsert(
        p.id,
        pid=424242,
        port=9333,
        requested_port=9222,
        user_data_dir=user_data_dir,
        exe="chrome.exe",
        started_at=1,
    )

    monkeypatch.setattr(
        launcher_module,
        "read_browser_identity",
        lambda pid, udd: {"state": "owned", "port": 9444, "created": "creation"},
    )
    launcher = BrowserLauncher(manager)
    assert launcher.is_running(p.id) is True
    assert launcher.active_ports[p.id] == 9444

    first = get_free_port()
    try:
        second = get_free_port()
        assert first != second
    finally:
        release_port(first)
        release_port(second)
    manager.delete_profile(p.id)


def test_second_instance_otc_never_mints_locally(monkeypatch):
    import run
    import web.app as web_app

    monkeypatch.setattr(run, "read_access_key", lambda: "server-key")

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"unlock_code":"otc_from_server"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(run.urllib.request, "urlopen", lambda req, timeout=0: FakeResponse())
    assert run.get_unlock_code_for_ui(8000, allow_local=False) == "otc_from_server"

    def fail_local(*args, **kwargs):
        raise AssertionError("second instance must not create a local OTC")

    monkeypatch.setattr(web_app, "create_unlock_code", fail_local)
    monkeypatch.setattr(
        run.urllib.request,
        "urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(OSError("server down")),
    )
    assert run.get_unlock_code_for_ui(8000, allow_local=False) is None


def test_server_identity_requires_digest_and_creation_time(monkeypatch):
    command_line = "python.exe C:\\tool\\run.py --port 8000"
    digest = server_control._cmdline_digest(command_line)
    details = {
        "exe": "C:\\Python310\\python.exe",
        "cmdline": command_line,
        "created": "2026-10-23 10:00:00",
    }
    monkeypatch.setattr(server_control, "get_process_details", lambda pid: dict(details))

    good = {
        "pid": 1234,
        "cmdline_digest": digest,
        "process_created": details["created"],
    }
    assert server_control.verify_server_identity(good) is True
    assert server_control.verify_server_identity({**good, "cmdline_digest": "bad"}) is False
    assert server_control.verify_server_identity({**good, "process_created": "other"}) is False

    details["cmdline"] = "python.exe C:\\other\\unrelated.py"
    assert server_control.verify_server_identity(good) is False


def test_stop_server_409_does_not_taskkill(monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        server_control,
        "read_server_info",
        lambda: {"pid": 4321, "port": 8000, "instance_id": "inst"},
    )
    monkeypatch.setattr(server_control, "verify_server_identity", lambda info: True)
    monkeypatch.setattr(
        server_control,
        "check_server_health",
        lambda port: {"application": "antidetect-python", "instance_id": "inst"},
    )

    def conflict(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

    monkeypatch.setattr(server_control.urllib.request, "urlopen", conflict)
    calls = []
    monkeypatch.setattr(
        server_control.subprocess,
        "run",
        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("taskkill called")),
    )
    result = server_control.stop_server()
    assert result["success"] is False
    assert "profile" in result["message"].lower()
    assert calls == []


def test_stop_server_unverified_alive_preserves_pid(monkeypatch):
    info = {"pid": 9876, "port": 8000, "instance_id": "inst"}
    details = {"exe": "C:\\Python310\\python.exe", "cmdline": "", "created": ""}
    removed = []
    monkeypatch.setattr(server_control, "read_server_info", lambda: dict(info))
    monkeypatch.setattr(server_control, "verify_server_identity", lambda value: False)
    monkeypatch.setattr(server_control, "get_process_details", lambda pid: details)
    monkeypatch.setattr(server_control, "remove_server_info", lambda: removed.append(True))

    result = server_control.stop_server()
    assert result["success"] is False
    assert removed == []

    # A stale PID whose process is gone can be cleaned up.
    details = None
    result = server_control.stop_server()
    assert result["success"] is True
    assert removed == [True]


def test_webrtc_flags_follow_mode_and_proxy():
    from antidetect.fingerprint import generate_fingerprint

    fp = generate_fingerprint(target_os="windows", locale="en-US")
    profile = manager.create_profile("webrtc-flags", proxy=_proxy())

    fp.webrtc_mode = "auto"
    assert build_webrtc_flags(profile, fp) == []
    fp.webrtc_mode = "disable_non_proxied"
    assert any("force-webrtc" in flag for flag in build_webrtc_flags(profile, fp))

    profile.proxy.enabled = False
    assert build_webrtc_flags(profile, fp) == []
    manager.delete_profile(profile.id)


def test_edge_brand_and_locale_mapping(monkeypatch):
    assert detect_browser_brand(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") == "edge"
    assert detect_browser_brand(r"C:\Program Files\Google\Chrome\Application\chrome.exe") == "chrome"
    edge_ua = build_user_agent("windows", "155.0.8059.85", "edge")
    assert "Edg/155.0.0.0" in edge_ua
    assert locale_for_country("US") == "en-US"
    assert locale_for_country("VN") == "vi-VN"
    assert locale_for_country("ZZ", default="vi-VN") == "vi-VN"

    monkeypatch.setattr(
        "antidetect.fingerprint.get_version_from_browser_path",
        lambda path: ("msedge", "155.0.8059.85"),
    )
    from antidetect.fingerprint import generate_fingerprint
    fp = generate_fingerprint(target_os="windows", locale="en-US")
    normalized = normalize_fingerprint_for_launch(fp, r"C:\Edge\msedge.exe")
    assert normalized.browser_brand == "edge"
    assert "Edg/155.0.0.0" in normalized.user_agent
