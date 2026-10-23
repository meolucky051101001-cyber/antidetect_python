import os
import sys
import unittest

DIR = os.path.dirname(os.path.abspath(__file__))
if DIR not in sys.path:
    sys.path.insert(0, DIR)

import test_api

def run():
    print("--- 1. UNIT TESTS ---")
    suite = unittest.defaultTestLoader.discover(DIR, pattern="test_antidetect.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)

    print("\n--- 2. API TESTS ---")
    test_api.test_api_routes()
    print("[PASS] test_api_routes")
    test_api.test_api_rejects_cross_origin_browser_requests()
    print("[PASS] test_api_rejects_cross_origin_browser_requests")
    test_api.test_cookie_api_runs_blocking_cdp_calls_off_event_loop()
    print("[PASS] test_cookie_api_runs_blocking_cdp_calls_off_event_loop")
    test_api.test_start_all_api_defaults_to_eight_and_reports_queue()
    print("[PASS] test_start_all_api_defaults_to_eight_and_reports_queue")
    test_api.test_proxy_password_is_redacted_by_api()
    print("[PASS] test_proxy_password_is_redacted_by_api")
    test_api.test_browser_start_and_stop_run_off_event_loop()
    print("[PASS] test_browser_start_and_stop_run_off_event_loop")
    test_api.test_p1_features()
    print("[PASS] test_p1_features (404/409 codes, zoneinfo validation, start-all queue & cancel)")
    test_api.test_p2_features()
    print("[PASS] test_p2_features (CSP headers, st_mtime_ns cache speed, WebRTC stealth)")

    print("\nALL UNIT & API TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run()
