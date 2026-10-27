import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Never touch the user's real data/profiles.json from tests.
TEST_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-unit-")
os.environ["ANTIDETECT_DATA_DIR"] = TEST_DATA_DIR
atexit.register(shutil.rmtree, TEST_DATA_DIR, True)

from antidetect.models import Profile, ProxyConfig, REDACTED_SECRET
from antidetect.storage import ProfileManager
from antidetect.fingerprint import generate_fingerprint, normalize_fingerprint_for_launch
from antidetect.stealth import get_stealth_script
from antidetect.launcher import find_browser_executable, BrowserLauncher


class _FakeProcess:
    def __init__(self, lifetime=0.6):
        self.finished_at = time.time() + lifetime

    def poll(self):
        return 0 if time.time() >= self.finished_at else None


class _QueueLauncher(BrowserLauncher):
    """Deterministic launcher dùng để test launch_all song song đúng limit.

    launch_all mới chạy các launch trong ThreadPoolExecutor; max_concurrent
    giới hạn số launch thực hiện đồng thời (browser mở xong vẫn giữ mở),
    nên concurrency đo bằng counter thread chứ không phải số process sống.
    """

    def __init__(self, manager):
        super().__init__(manager)
        import atexit

        atexit.unregister(self._cleanup_on_exit)
        self.started = []
        self.max_live = 0
        self._live = 0
        self._live_lock = threading.Lock()

    def launch(self, profile_id, open_url=None):
        with self._live_lock:
            self._live += 1
            self.max_live = max(self.max_live, self._live)
        time.sleep(0.05)  # để các worker chồng nhau thật sự
        with self._live_lock:
            self._live -= 1
        self.started.append(profile_id)
        self.active_processes[profile_id] = _FakeProcess()
        return {"success": True, "profile_id": profile_id}


class TestAntidetectCore(unittest.TestCase):

    def setUp(self):
        self.manager = ProfileManager()

    def test_fingerprint_generation(self):
        fp = generate_fingerprint(target_os="windows")
        self.assertEqual(fp.os, "windows")
        self.assertIn("Windows NT 10.0", fp.user_agent)
        self.assertGreater(fp.canvas_noise_seed, 0)
        self.assertGreater(fp.hardware_concurrency, 0)
        self.assertTrue(len(fp.webgl_renderer) > 0)

        fp_mac = generate_fingerprint(target_os="macos")
        self.assertEqual(fp_mac.os, "macos")
        self.assertIn("Macintosh", fp_mac.user_agent)

    def test_stealth_script_payload(self):
        fp = generate_fingerprint(target_os="windows")
        script = get_stealth_script(fp)
        self.assertIn("navigator.webdriver", script)
        self.assertIn("UNMASKED_VENDOR_WEBGL", script)
        self.assertIn("UNMASKED_RENDERER_WEBGL", script)
        self.assertIn(str(fp.canvas_noise_seed), script)

    def test_profile_lifecycle(self):
        p = self.manager.create_profile(
            name="Unit Test Profile",
            target_os="windows",
            notes="Test Note",
            tags=["mmo", "test"],
        )
        self.assertIsNotNone(p.id)
        self.assertEqual(p.name, "Unit Test Profile")

        # Get profile
        fetched = self.manager.get_profile(p.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.notes, "Test Note")

        # Duplicate
        dup = self.manager.duplicate_profile(p.id, "Bản sao Unit Test")
        self.assertIsNotNone(dup)
        self.assertNotEqual(p.id, dup.id)
        self.assertNotEqual(
            p.fingerprint.canvas_noise_seed, dup.fingerprint.canvas_noise_seed
        )

        # Cleanup
        self.manager.delete_profile(p.id)
        self.manager.delete_profile(dup.id)
        self.assertIsNone(self.manager.get_profile(p.id))

    def test_launcher_reports_missing_browser_without_crashing_app(self):
        # GĐ2: browsers/ có thể chứa browser pin thật trên đĩa — phải patch
        # CẢ BROWSERS_DIR để mô phỏng đúng "không có browser nào", nếu không
        # launch sẽ thành công bằng pin (và leak Chrome thật làm hỏng các
        # test sau trong cùng data dir).
        profile = self.manager.create_profile(name="Missing Browser Test")
        try:
            with patch(
                "antidetect.launcher.find_browser_executable",
                side_effect=FileNotFoundError("browser not installed"),
            ), patch(
                "antidetect.launcher.BROWSERS_DIR",
                os.path.join(tempfile.gettempdir(), "no-browsers-here"),
            ):
                launcher = BrowserLauncher(self.manager)
                result = launcher.launch(profile.id)
            self.assertFalse(result["success"])
            self.assertIn("Chrome hoặc Microsoft Edge", result["error"])
        finally:
            self.manager.delete_profile(profile.id)

    def test_start_all_queues_instead_of_rejecting_excess_profiles(self):
        profiles = [
            self.manager.create_profile(name=f"Queue Test {i}") for i in range(4)
        ]
        launcher = _QueueLauncher(self.manager)
        try:
            results = launcher.launch_all(delay=0, max_concurrent=2)
            self.assertEqual(len(results), 4)
            self.assertTrue(all(item["result"]["success"] for item in results))
            self.assertEqual(len(launcher.started), 4)
            self.assertLessEqual(launcher.max_live, 2)
        finally:
            launcher.active_processes.clear()
            for profile in profiles:
                self.manager.delete_profile(profile.id)

    def test_browser_executable_found(self):
        path = find_browser_executable()
        self.assertTrue(os.path.exists(path))
        print(f"\n[OK] Found browser: {path}")

    def test_proxy_password_is_redacted_by_default(self):
        proxy = ProxyConfig(
            enabled=True,
            protocol="http",
            host="1.2.3.4",
            port=8080,
            username="admin",
            password="secret_password",
        )
        public = proxy.model_dump()
        private = proxy.model_dump(context={"include_secrets": True})
        self.assertEqual(public["password"], REDACTED_SECRET)
        self.assertEqual(private["password"], "secret_password")
        self.assertNotIn("secret_password", proxy.to_display_url())
        self.assertIn("secret_password", proxy.to_url())

    def test_normalize_legacy_macos_fingerprint(self):
        fp = Profile(name="tmp").fingerprint
        fp.os = "macos"
        fp.device_pixel_ratio = 1.0
        fp.screen_width, fp.screen_height = 1366, 768
        fp.languages = ["vi;q=0.9", "vi-VN", "en-US"]
        fixed = normalize_fingerprint_for_launch(fp)
        self.assertEqual(fixed.device_pixel_ratio, 2.0)
        self.assertEqual((fixed.screen_width, fixed.screen_height), (1440, 900))
        self.assertEqual(fixed.languages, ["vi-VN", "vi", "en-US"])


if __name__ == "__main__":
    unittest.main()
