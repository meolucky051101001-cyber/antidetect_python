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


def test_screen_hooks_are_gone():
    js = get_stealth_script(_windows_fp())
    worker_js = get_worker_stealth_script(_windows_fp())

    # Screen/DPR/media are supplied by CDP device metrics in the launcher.
    for payload in (js, worker_js):
        assert "Screen.prototype" not in payload
        assert "matchMedia" not in payload
        assert "availWidth" not in payload
        assert "availHeight" not in payload
        assert "Object.defineProperty(w, 'devicePixelRatio'" not in payload


def test_function_to_string_is_not_hooked():
    js = get_stealth_script(_windows_fp())
    worker_js = get_worker_stealth_script(_windows_fp())

    # CreepJS detects a Proxy on Function.prototype.toString. Leave the native
    # implementation untouched; do not try to fake "[native code]".
    for payload in (js, worker_js):
        assert "Function.prototype.toString =" not in payload
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
    js = get_stealth_script(_windows_fp())
    assert 'const DISABLED = new Set(["canvas", "webgl"])' in js
    assert "if (!DISABLED.has('webgl'))" in js
    assert "if (!DISABLED.has('canvas')" in js

    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "all")
    assert _disabled_stealth_groups() == set(STEALTH_GROUPS)

    monkeypatch.setenv("ANTIDETECT_STEALTH_DISABLE", "not-a-group")
    assert _disabled_stealth_groups() == set()


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
