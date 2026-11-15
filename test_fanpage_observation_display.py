"""Observation age and provenance remain visible in the UI and CSV export."""
import csv
import io
from pathlib import Path
import shutil
import subprocess

import pytest

from antidetect.fanpages.service import FanpageService


def test_csv_includes_each_observation_time_and_preserves_zero(monkeypatch):
    service = FanpageService.__new__(FanpageService)
    rows = [{
        "followers_count": 0, "likes_count": 15, "field_observations": {
            "followers": {"observed_at": "2026-11-10T10:00:00Z", "source": "facebook_detail"},
            "likes": {"observed_at": "2026-11-11T11:00:00Z", "source": "=unsafe-source"},
            "creation": {"observed_at": None, "source": "legacy_cache"},
        },
    }]
    monkeypatch.setattr(service, "_load_enriched_catalog", lambda **kwargs: rows)
    monkeypatch.setattr(service, "_filter_rows", lambda items, **kwargs: items)
    monkeypatch.setattr(service, "_sort_rows", lambda items, **kwargs: items)
    result = service.export_csv()
    assert result.startswith("\ufeff")
    row = next(csv.DictReader(io.StringIO(result.removeprefix("\ufeff"))))
    assert row["Follower"] == "0"
    assert row["Follower Quan Sát (UTC)"] == "2026-11-10T10:00:00Z"
    assert row["Like Quan Sát (UTC)"] == "2026-11-11T11:00:00Z"
    assert row["Nguồn Like"] == "'=unsafe-source"
    assert row["Ngày Tạo Quan Sát (UTC)"] == ""
    assert row["Hoạt Động Quan Sát (UTC)"] == ""


def test_metric_age_uses_group_observation_never_last_sync():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to verify browser timestamp formatting")
    script = r"""
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');
const context = vm.createContext({document: {addEventListener() {}}, Date, URL, URLSearchParams});
vm.runInContext(fs.readFileSync('web/static/js/fanpages.js', 'utf8'), context);
const now = Date.parse('2026-11-15T12:00:00Z');
const page = {last_synced_at: now / 1000, followers_count: 0, field_observations: {
 followers: {observed_at: '2026-11-13T12:00:00Z'},
 likes: {observed_at: null},
 creation: {observed_at: '2099-01-01T00:00:00Z'}
}};
assert.ok(context.formatMetricObservation(page, 'followers', now).includes('2 ngày trước'));
assert.strictEqual(context.formatMetricObservation(page, 'likes', now), 'Chưa rõ thời điểm quan sát');
assert.strictEqual(context.formatMetricObservation(page, 'creation', now), 'Chưa rõ thời điểm quan sát');
assert.strictEqual(context.formatMetricObservation({last_synced_at: now / 1000}, 'followers', now), 'Chưa rõ thời điểm quan sát');
assert.strictEqual(context.formatNumberVi(0, 'exact'), '0');
"""
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8",
                            cwd=str(Path(__file__).parent), capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
