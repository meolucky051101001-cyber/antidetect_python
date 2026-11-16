"""A late synthetic DevTools discovery must not resurrect a stopped launch."""
import asyncio
import threading
from types import SimpleNamespace

import antidetect.launcher as launcher_module
from antidetect.models import Profile
from test_runtime_generation_cleanup import fixture_launcher


def test_late_devtools_port_write_cannot_resurrect_confirmed_cleanup(tmp_path, monkeypatch):
    launcher = fixture_launcher(tmp_path)
    launcher._native_brands_cache = {}
    profile = Profile(id="profile-1", name="synthetic-cleanup")
    launch_id = launcher.cdp_health.begin_launch(profile.id, launcher.instance_id)
    launcher.cdp_health.set_process(profile.id, launch_id, 424242)
    launcher._runtime_store.upsert(
        profile.id, pid=424242, port=9333, launch_id=launch_id,
        owner_instance=launcher.instance_id, user_data_dir=str(tmp_path),
    )
    exited = []
    proc = SimpleNamespace(
        pid=424242, poll=lambda: 0 if exited else None,
        kill=lambda: exited.append(True), wait=lambda **kwargs: 0,
    )
    launcher.active_processes[profile.id] = proc
    launcher.active_ports[profile.id] = 9333
    (tmp_path / "DevToolsActivePort").write_text("9444\n/devtools/browser/synthetic\n", encoding="utf-8")
    at_write = threading.Event()
    allow_write = threading.Event()
    failures = []

    def delay_port_write(original):
        def delayed(*args, **kwargs):
            if kwargs.get("cdp_port_source") == "DevToolsActivePort":
                at_write.set()
                assert allow_write.wait(5), "test did not release discovery writer"
            return original(*args, **kwargs)
        return delayed

    for method in ("upsert", "upsert_if_owner"):
        monkeypatch.setattr(
            launcher._runtime_store, method,
            delay_port_write(getattr(launcher._runtime_store, method)),
        )
    monkeypatch.setattr(launcher_module, "get_stealth_script", lambda *a, **k: "")
    monkeypatch.setattr(launcher_module, "get_worker_stealth_script", lambda *a, **k: "")

    class SyntheticTransportEnd(BaseException):
        pass

    def no_network(*args, **kwargs):
        raise SyntheticTransportEnd()

    monkeypatch.setattr(launcher_module, "open_loopback", no_network)

    def run_setup():
        try:
            asyncio.run(launcher._setup_cdp_stealth(
                9333, profile, user_data_dir=str(tmp_path), launch_id=launch_id,
            ))
        except SyntheticTransportEnd:
            pass
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run_setup, daemon=True)
    thread.start()
    try:
        assert at_write.wait(5), "DevTools discovery did not reach runtime persistence"
        assert launcher._cleanup_failed_launch(profile.id, proc, launch_id)
    finally:
        allow_write.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not failures
    assert launcher._runtime_store.get(profile.id) is None
    assert profile.id not in launcher.active_ports
    assert not launcher.is_running(profile.id)
