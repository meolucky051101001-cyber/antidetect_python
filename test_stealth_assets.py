"""P1-6b: JS payloads sống trong antidetect/assets, loader chỉ thay placeholder."""
import json
import os
import re
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from antidetect.fingerprint import generate_fingerprint
from antidetect.stealth import (
    _PAGE_TEMPLATE,
    _WORKER_TEMPLATE,
    get_stealth_script,
    get_worker_stealth_script,
)

_ASSETS = os.path.join(BASE_DIR, "antidetect", "assets")
_PLACEHOLDERS = (
    "__CFG__",
    "__DISABLED__",
    "__PLATFORM__",
    "__CH_PLATFORM__",
    "__CH_PLATFORM_VER__",
    "__CH_ARCH__",
    "__WORKER_JS__",
)


@pytest.fixture
def fp():
    fingerprint = generate_fingerprint("windows")
    fingerprint.os = "windows"
    return fingerprint


def test_assets_exist_and_templates_have_placeholders():
    for name in ("stealth_page.js", "stealth_worker.js"):
        path = os.path.join(_ASSETS, name)
        assert os.path.getsize(path) > 5000, f"{name} rỗng hoặc thiếu"
    assert "__CFG__" in _PAGE_TEMPLATE
    assert "__DISABLED__" in _PAGE_TEMPLATE
    assert "__WORKER_JS__" in _PAGE_TEMPLATE
    assert "__CFG__" in _WORKER_TEMPLATE
    assert "__PLATFORM__" in _WORKER_TEMPLATE


def test_rendered_payloads_leave_no_placeholder(fp):
    for js in (
        get_stealth_script(fp),
        get_worker_stealth_script(fp),
    ):
        for token in _PLACEHOLDERS:
            assert token not in js, f"placeholder {token} chưa được thay"


def test_page_embeds_worker_payload_and_valid_config(fp):
    page = get_stealth_script(fp)
    worker = get_worker_stealth_script(fp)
    # Worker chạy trong page phải là payload đã render, không phải template.
    assert json.dumps(worker) in page

    cfg_match = re.search(r"const CFG = (\{.*?\});\n", page)
    assert cfg_match, "không tìm thấy CFG trong payload"
    cfg = json.loads(cfg_match.group(1))
    assert cfg["os"] == "windows"

    disabled_match = re.search(r"const DISABLED = new Set\((\[.*?\])\);", page)
    assert disabled_match, "không tìm thấy DISABLED trong payload"
    disabled = json.loads(disabled_match.group(1))
    assert isinstance(disabled, list)
    assert all(isinstance(group, str) for group in disabled)


def test_worker_render_replaces_platform_values(fp):
    worker = get_worker_stealth_script(fp)
    assert "const platformStr = \"Win32\";" in worker
    assert "const chPlatform = \"Windows\";" in worker
    assert "const chPlatformVer = \"15.0.0\";" in worker
    assert "const chArch = \"x86\";" in worker
