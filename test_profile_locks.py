import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from antidetect.profile_locks import ProfileLocks, ProfileLockTimeout

REPO_ROOT = Path(__file__).resolve().parent


def test_acquire_creates_lock_file_and_keeps_it(tmp_path):
    locks = ProfileLocks(tmp_path, default_timeout=1)
    lock_file = tmp_path / "locks" / "profile-1.lock"

    with locks.acquire("profile-1", timeout=1):
        assert lock_file.is_file()

    assert lock_file.is_file()


def test_nested_acquire_is_reentrant_and_keeps_outer_lock(tmp_path):
    locks = ProfileLocks(tmp_path, default_timeout=1)
    outcomes = []

    def contender():
        try:
            with locks.acquire("profile-1", timeout=0.2):
                outcomes.append("acquired")
        except ProfileLockTimeout:
            outcomes.append("timeout")

    with locks.acquire("profile-1", timeout=1):
        with locks.acquire("profile-1", timeout=0.2):
            pass
        worker = threading.Thread(target=contender)
        worker.start()
        worker.join(2)
        assert outcomes == ["timeout"]


def test_second_thread_times_out_then_succeeds_after_release(tmp_path):
    holder_locks = ProfileLocks(tmp_path, default_timeout=1)
    waiter_locks = ProfileLocks(tmp_path, default_timeout=1)
    acquired = threading.Event()
    release = threading.Event()
    errors = []

    def holder():
        try:
            with holder_locks.acquire("profile-1", timeout=1):
                acquired.set()
                release.wait(3)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=holder)
    worker.start()
    assert acquired.wait(2)

    with pytest.raises(ProfileLockTimeout):
        with waiter_locks.acquire("profile-1", timeout=0.2):
            pass

    release.set()
    worker.join(3)
    assert not errors
    with waiter_locks.acquire("profile-1", timeout=1):
        pass


def test_acquire_many_releases_already_acquired_profiles_on_timeout(tmp_path):
    locks = ProfileLocks(tmp_path, default_timeout=1)
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with locks.acquire("profile-b", timeout=1):
            acquired.set()
            release.wait(3)

    worker = threading.Thread(target=holder)
    worker.start()
    assert acquired.wait(2)

    try:
        with pytest.raises(ProfileLockTimeout):
            with locks.acquire_many(["profile-b", "profile-a"], timeout=0.3):
                pass
    finally:
        release.set()
        worker.join(3)

    with locks.acquire("profile-a", timeout=1):
        pass


def test_invalid_profile_id_and_negative_timeout_are_rejected(tmp_path):
    locks = ProfileLocks(tmp_path, default_timeout=1)
    for bad_id in ("", "../escape", "a/b", "a b", "a" * 65):
        with pytest.raises(ValueError):
            locks.acquire(bad_id)
    with pytest.raises(ValueError):
        locks.acquire("profile-1", timeout=-1)
    assert not (tmp_path / "locks").exists()


def test_cross_process_lock_blocks_other_process(tmp_path):
    script = tmp_path / "holder.py"
    script.write_text(
        "import sys, time\n"
        "from antidetect.profile_locks import ProfileLocks\n"
        "locks = ProfileLocks(sys.argv[1], default_timeout=5)\n"
        "with locks.acquire('shared', timeout=5):\n"
        "    with open(sys.argv[2], 'w', encoding='utf-8') as handle:\n"
        "        handle.write('ready')\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    ready = tmp_path / "ready.txt"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), str(ready)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    locks = ProfileLocks(tmp_path, default_timeout=0.4)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            if process.poll() is not None:
                _, stderr = process.communicate(timeout=2)
                pytest.fail(f"holder process exited early: {stderr}")
            time.sleep(0.05)
        assert ready.exists(), "holder process did not acquire the lock in time"

        with pytest.raises(ProfileLockTimeout):
            with locks.acquire("shared", timeout=0.4):
                pass
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    with locks.acquire("shared", timeout=3):
        pass
