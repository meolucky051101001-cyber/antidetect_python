"""Regression tests for the 2026-10-23 S1-S8 consistency fixes.

These tests are intentionally static: they verify the injected payload does
(or does not) contain a hook family and that the host-derived values used by
the launcher stay on real Chrome buckets. Browser-level behaviour is covered
by the E2E harness.
"""
import atexit
import os
import shutil
import sys
import tempfile

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

TEST_DATA_DIR = tempfile.mkdtemp(prefix="antidetect-consistency-")
os.environ["ANTIDETECT_DATA_DIR"] = TEST_DATA_DIR
atexit.register(shutil.rmtree, TEST_DATA_DIR, True)

from antidetect.fingerprint import (
    clamp_screen_to_host,
    device_memory_bucket,
    generate_fingerprint,
    get_host_device_memory,
    get_host_screen_info,
)
from antidetect.stealth import (
    STEALTH_GROUPS,
    _disabled_stealth_groups,
    _host_os,
    _screen_work_area,
    _webgl_limits,
    get_stealth_script,
    get_worker_stealth_script,
)


def _windows_fp():
    fp = generate_fingerprint("windows")
    # generate_fingerprint normalizes, but keep the assertion independent of
    # random preset choice.
    fp.os = "windows"
    return fp


def test_same_os_webgl_limits_are_native():
    fp = _windows_fp()
    assert _webgl_limits(fp) is None

    js = get_stealth_script(fp)
    # A same-OS Windows profile must not inject a fake ANGLE limit table.
    assert '"webglLimits": null' in js
    assert "Math.min(32768" not in js


def test_cross_os_webgl_limits_are_measured():
    fp = _windows_fp()

    fp.os = "macos"
    mac = _webgl_limits(fp)
    assert mac is not None
    assert mac["maxTextureSize"] == 16384
    assert mac["maxViewportDims"] == [16384, 16384]

    fp.os = "linux"
    fp.webgl_renderer = "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630)"
    linux_intel = _webgl_limits(fp)
    assert linux_intel["maxTextureSize"] == 16384
    assert linux_intel["maxViewportDims"] == [16384, 16384]

    fp.webgl_renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11)"
    linux_nvidia = _webgl_limits(fp)
    # The renderer says Direct3D, but the profile claims Linux; the measured
    # Linux NVIDIA limit is 32768, not the old universal 32768 table.
    assert linux_nvidia["maxTextureSize"] == 32768


def test_device_memory_uses_host_bucket():
    host = get_host_device_memory()
    assert host in (2, 4, 8, 16, 32)
    assert device_memory_bucket(15.6) == 16
    assert device_memory_bucket(8.0) == 8
    assert device_memory_bucket(1.0) == 2

    fp = _windows_fp()
    assert fp.device_memory == host
    js = get_stealth_script(fp)
    assert f'"deviceMemory": {host}' in js
    assert "Math.min(8" not in js


def test_screen_work_area_hook_is_scoped():
    fp = _windows_fp()
    js = get_stealth_script(fp)
    worker_js = get_worker_stealth_script(fp)

    # R2: CDP still owns screen.*, matchMedia and DPR. Only the work-area
    # getters that differ from full-screen emulation are replaced.
    area = _screen_work_area(fp)
    if area:
        assert area["availWidth"] <= fp.screen_width
        assert area["availHeight"] <= fp.screen_height
        assert area["availWidth"] == fp.screen_width or area["availLeft"] > 0
    assert "installScreenWorkArea" in js
    assert "screenWorkArea" in js
    assert "availHeight" in js
    assert "matchMedia(" not in js
    assert "Object.defineProperty(w, 'devicePixelRatio'" not in js

    # Workers have no Screen object, so the page hook must not leak there.
    assert "Screen.prototype" not in worker_js
    assert "availWidth" not in worker_js
    assert "availHeight" not in worker_js


def test_function_to_string_registry_is_present():
    js = get_stealth_script(_windows_fp())
    worker_js = get_worker_stealth_script(_windows_fp())

    # R1: wrappers are registered in a WeakMap and Function.prototype.toString
    # is a concise method, not a Proxy. It must not be assigned with a plain
    # `Function.prototype.toString = ...` (which would expose a writable alias).
    for payload in (js, worker_js):
        assert "const nativeSources = new WeakMap();" in payload
        assert "function makeNativeToString" in payload
        assert "registerNativeSource(target, name)" in payload
        assert "Function.prototype, 'toString'" in payload
        assert "function toString() { [native code] }" in payload
        assert "Function.prototype.toString = function" not in payload
        assert "proxyToString" not in payload


def test_webrtc_hooks_are_gone():
    js = get_stealth_script(_windows_fp())
    worker_js = get_worker_stealth_script(_windows_fp())

    for payload in (js, worker_js):
        assert "RTCPeerConnection" not in payload
        assert "mozRTCPeerConnection" not in payload
        assert "webkitRTCPeerConnection" not in payload


def test_canvas_complexity_gate_present():
    js = get_stealth_script(_windows_fp())
    worker_js = get_worker_stealth_script(_windows_fp())

    for payload in (js, worker_js):
        assert "function canvasIsComplex" in payload
        assert "seen.size >= 64" in payload
        assert "canvasIsComplex(data)" in payload


def test_disabled_groups_are_honoured(monkeypatch):
    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "webgl, canvas")
    disabled = _disabled_stealth_groups()
    assert disabled == {"webgl", "canvas"}

    # Group names are canonical; lower-case env input must still work.
    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "NAVIGATOR")
    assert _disabled_stealth_groups() == {"navigator"}

    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "webgl, canvas")
    monkeypatch.delenv("ANTIDETECT_STEALTH_ENABLE", raising=False)
    js = get_stealth_script(_windows_fp())
    # battery/network là default-off nên DISABLED luôn chứa cả 4 nhóm này.
    assert 'const DISABLED = new Set(["battery", "canvas", "network", "webgl"])' in js
    assert "if (!CONTRACT_V2 && !DISABLED.has('webgl'))" in js
    assert "if (DISABLED.has('canvas')) return;" in js

    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "all")
    assert _disabled_stealth_groups() == set(STEALTH_GROUPS)

    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "not-a-group")
    assert _disabled_stealth_groups() == set()


def test_battery_and_connection_profiles_are_deterministic(monkeypatch):
    """Battery/NetworkInformation: hooks opt-in (mặc định tắt) + giá trị đúng spec."""
    from antidetect.stealth import (
        _battery_profile,
        _connection_profile,
    )

    # Mặc định: hai nhóm này tắt (Pixelyscan phát hiện wrapper dù giá trị
    # native) — trang thấy battery/connection native của máy. Code hook vẫn
    # nằm trong payload nhưng bị guard DISABLED chặn ở runtime.
    monkeypatch.delenv("ANTIDETECT_STEALTH_DISABLE", raising=False)
    monkeypatch.delenv("ANTIDETECT_STEALTH_ENABLE", raising=False)
    from antidetect.stealth import _effective_disabled_groups
    assert {"battery", "network"} <= _effective_disabled_groups(_windows_fp())

    # Opt-in qua env: hooks hoạt động trở lại.
    monkeypatch.setenv("ANTIDETECT_STEALTH_ENABLE", "battery,network")
    enabled_groups = _effective_disabled_groups(_windows_fp())
    assert "battery" not in enabled_groups and "network" not in enabled_groups
    js = get_stealth_script(_windows_fp())
    assert "getBattery" in js and "BatteryManager.prototype" in js
    assert "get connection" in js and "effectiveType" in js
    worker_js = get_worker_stealth_script(_windows_fp())
    assert "getBattery" not in worker_js

    fp = _windows_fp()
    b = _battery_profile(fp)
    c = _connection_profile(fp)
    # spec NetworkInformation: rtt bội 25, downlink bội 0.25, 4g wifi.
    assert c["rtt"] % 25 == 0
    assert abs(c["downlink"] * 4 - round(c["downlink"] * 4)) < 1e-9
    assert c["effectiveType"] == "4g" and c["saveData"] is False
    # Battery: level hợp lệ; đang sạc ⇔ dischargingTime Infinity, và ngược lại.
    assert 0.4 <= b["level"] <= 1.0
    assert (b["charging"] and b["dischargingTime"] == float("inf")) or (
        not b["charging"] and b["chargingTime"] == float("inf")
    )
    # Cùng seed → cùng giá trị giữa các phiên (detector so sánh theo thời gian).
    fp2 = _windows_fp()
    fp2.canvas_noise_seed = fp.canvas_noise_seed
    assert _battery_profile(fp2) == b and _connection_profile(fp2) == c


def test_cross_os_fonts_and_voices():
    """Cross-OS: danh sách font/voice chuẩn của OS đích; same-OS để native."""
    from antidetect.stealth import _cross_os_data

    assert "battery" in STEALTH_GROUPS and "network" in STEALTH_GROUPS

    fp = _windows_fp()
    if _host_os() == "windows":
        # Same-OS: không ship danh sách (font host đã đúng OS đích).
        assert _cross_os_data(fp) is None
        assert '"crossOs": null' in get_stealth_script(fp)

    fp_mac = _windows_fp()
    fp_mac.os = "macos"
    data = _cross_os_data(fp_mac)
    assert data is not None
    assert "Segoe UI" in data["hostFonts"]  # font host phải bị che
    assert any(f in data["targetFonts"] for f in ("PingFang SC", "Helvetica Neue"))
    assert data["voices"] and data["voices"][0]["name"] == "Samantha"

    fp_linux = _windows_fp()
    fp_linux.os = "linux"
    # Linux thật thường không có speech voice — rỗng là đúng thật.
    assert _cross_os_data(fp_linux)["voices"] == []

    js_mac = get_stealth_script(fp_mac)
    # Cross-OS: fonts.check strict theo OS đích + measureText che font host.
    assert "crossOs.targetFonts" in js_mac
    assert "crossOs.hostFonts" in js_mac
    # Voices chuẩn macOS được dựng Object.create(SpeechSynthesisVoice.prototype)
    # để instanceof vẫn pass.
    assert "Samantha" in js_mac
    assert "Object.create(VoiceProto)" in js_mac


def test_screen_clamp_fits_host_display():
    host_w, host_h, _ = get_host_screen_info()
    assert host_w > 0 and host_h > 0

    # A same-OS profile cannot claim more CSS pixels than the display holds.
    width, height, clamped = clamp_screen_to_host(host_w * 2, host_h * 2, 1.0, "windows")
    assert clamped is True
    assert width <= host_w
    assert height <= host_h

    # Cross-OS profiles are not clamped against an unrelated host display.
    width, height, clamped = clamp_screen_to_host(2560, 1440, 1.0, "macos")
    assert clamped is False
    assert (width, height) == (2560, 1440)


def test_stealth_payload_is_valid_javascript():
    """Review-GD1 regression: một lỗi f-string brace từng làm cả payload vỡ
    syntax (mọi hook câm lặng vì try/catch nuốt lỗi parse-time... thực tế
    script không parse gì cả). node --check phải pass cho cả page + worker."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node không có mặt trên máy")
    for name, build in (
        ("page", lambda: get_stealth_script(_windows_fp())),
        ("worker", lambda: get_worker_stealth_script(_windows_fp())),
    ):
        path = os.path.join(tempfile.gettempdir(), f"stealth-syntax-{name}.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(build())
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert result.returncode == 0, f"{name} payload lỗi syntax: {result.stderr[:400]}"
