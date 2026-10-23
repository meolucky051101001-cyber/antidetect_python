"""Pure-function tests for the real-profile audit harness."""
import atexit
import os
import shutil
import tempfile

# Import the audit tools only after ANTIDETECT_DATA_DIR is set. Without this,
# importing tools.leak_audit binds antidetect.storage.DATA_DIR to the real
# data/ directory during pytest collection.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-audit-tools-")
os.environ["ANTIDETECT_DATA_DIR"] = _TEST_DATA_DIR
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)

from tools.detector_audit import detect_challenge, extract_detector_signals, parse_target
from tools.leak_audit import (
    classify_dns_verdict,
    classify_ip,
    extract_asns,
    matching_ips,
    parse_ip_text,
    summarize_webrtc,
)


def test_tests_use_isolated_data_dir():
    import antidetect.storage as storage_module

    repo_data = os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    )
    assert os.path.realpath(storage_module.DATA_DIR) != repo_data


def test_classify_ip_separates_local_public_and_mdns():
    assert classify_ip("192.168.1.20") == "local"
    assert classify_ip("10.0.0.5") == "local"
    assert classify_ip("fe80::1") == "local"
    assert classify_ip("8.8.8.8") == "public"
    assert classify_ip("2001:4860:4860::8888") == "public"
    assert classify_ip("0.0.0.0") == "unspecified"
    assert classify_ip("::") == "unspecified"
    assert classify_ip("4f3a2b1c-1234-5678-9abc-def012345678.local") == "mdns"
    assert classify_ip("not-an-ip") == "invalid"


def test_summarize_webrtc_extracts_candidates_and_sdp():
    payload = {
        "supported": True,
        "gatheringState": "complete",
        "candidates": [
            "candidate:1 1 UDP 2122260223 192.168.1.20 50000 typ host",
            "candidate:2 1 UDP 1686052607 8.8.8.8 50001 typ srflx",
        ],
        "sdp": "v=0\r\nc=IN IP4 192.168.1.20\r\n",
        "error": None,
    }
    summary = summarize_webrtc(payload)
    assert summary["supported"] is True
    assert summary["local"] == ["192.168.1.20"]
    assert summary["public"] == ["8.8.8.8"]
    assert len(summary["addresses"]) == 3
    assert any(item["source"] == "sdp" for item in summary["addresses"])


def test_summarize_webrtc_treats_zero_address_as_unspecified_not_local():
    payload = {
        "supported": True,
        "gatheringState": "complete",
        "candidates": [],
        "sdp": "v=0\r\nc=IN IP4 0.0.0.0\r\n",
        "error": None,
    }
    summary = summarize_webrtc(payload)
    assert summary["local"] == []
    assert summary["unspecified"] == ["0.0.0.0"]
    assert summary["candidate_count"] == 0


def test_parse_ip_text_handles_json_and_plain_text():
    assert parse_ip_text('{"ip":"198.51.100.7"}') == "198.51.100.7"
    assert parse_ip_text("Your IP is 8.8.8.8\n") == "8.8.8.8"
    assert parse_ip_text("no address here") is None


def test_parse_detector_target_and_challenge_markers():
    assert parse_target("https://example.com/path") == ("example.com", "https://example.com/path")
    assert parse_target("smoke=https://example.com/") == ("smoke", "https://example.com/")
    assert "complete the captcha" in detect_challenge("Please complete the CAPTCHA")
    assert detect_challenge("Learn about CAPTCHA and bot protection") is None
    assert detect_challenge("normal detector output") is None


def test_matching_ips_compares_ipv4_and_ipv6_sets():
    assert matching_ips(
        ["171.255.56.122", "2401:d800:25a:6c03:b127:9768:964e:b030"],
        ["171.255.56.122", "2001:db8::1"],
    ) == ["171.255.56.122"]
    assert matching_ips(
        ["2401:d800:25a:6c03:b127:9768:964e:b030"],
        ["2401:d800:25a:6c03:b127:9768:964e:b030"],
    ) == ["2401:d800:25a:6c03:b127:9768:964e:b030"]


def test_dns_verdict_requires_clicked_result_and_flags_different_isp():
    assert classify_dns_verdict("Run test\nWhat is a DNS leak?", False) == "MANUAL"
    assert classify_dns_verdict("Run test\nCompleted in 3 sec", True) == "MANUAL"
    assert classify_dns_verdict("[OK] DNS is not leaking.\nCompleted in 3.1 sec", True) == "CLEAN"
    assert (
        classify_dns_verdict(
            "[WARNING] DNS may be leaking.\nYou use different ISPs for DNS and internet access.\nCompleted in 5.3 sec",
            True,
        )
        == "LEAK"
    )
    assert (
        classify_dns_verdict(
            "DNS servers\nAS15169 Google LLC\nCompleted in 4 sec",
            True,
        )
        == "REVIEW"
    )


def test_extract_asns_stops_before_quoted_explanation():
    text = (
        'AS15169 Google LLC\n'
        'You use different ISPs for DNS and internet access. If "AS45903 CMC Telecom '
        'Infrastructure Company" is your VPN, then your connection is 100% secure.\n'
    )
    assert extract_asns(text) == [
        "AS15169 Google LLC",
        "AS45903 CMC Telecom Infrastructure Company",
    ]


def test_extract_detector_signals_keeps_verdicts_from_original_sites():
    pixelscan = extract_detector_signals(
        "Your Browser Fingerprint is inconsistent. Masking detected. "
        "No automated behavior detected.",
        "https://pixelscan.net/fingerprint-check",
    )
    assert pixelscan["pixelscan_fingerprint"] == "inconsistent"
    assert pixelscan["pixelscan_masking"] == "detected"
    assert pixelscan["pixelscan_bot_check"] == "no automated behavior detected"

    # The live 2026-10-23 Pixelscan page contains FAQ text mentioning
    # "masking detected" while the result card says "No masking detected".
    pixelscan_clean = extract_detector_signals(
        "Your Browser Fingerprint is consistent\nNo masking detected\n"
        "Why do I see inconsistent results? Masking detected is a common question.",
        "https://pixelscan.net/fingerprint-check",
    )
    assert pixelscan_clean["pixelscan_fingerprint"] == "consistent"
    assert pixelscan_clean["pixelscan_masking"] == "not detected"

    creepjs = extract_detector_signals(
        "25% like headless: fafdf9d1\n0% headless: 52defe05\n20% stealth: 71bd68ae\n"
        "0% resistance: 8ab41f90\nLies: 7",
        "https://abrahamjuliot.github.io/creepjs/",
    )
    assert creepjs["creepjs_like_headless"] == "25%"
    assert creepjs["creepjs_headless"] == "0%"
    assert creepjs["creepjs_stealth"] == "20%"
    assert creepjs["creepjs_resistance"] == "0%"
    assert creepjs["creepjs_lies_count"] == "7"
