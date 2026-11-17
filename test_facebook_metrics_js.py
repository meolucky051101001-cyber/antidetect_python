"""Execute the metrics parser against small synthetic DOM fixtures."""
import json
import shutil
import subprocess
import unicodedata

import pytest

from antidetect import facebook


def _metrics(body_text, *, url="https://www.facebook.com/profile.php?id=111111111", links=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the Facebook DOM parser")
    fixture = json.dumps({"body": body_text, "url": url, "links": links or []})
    script = r"""
const fixture = JSON.parse(process.argv[2]);
global.location = new URL(fixture.url);
let clock = 0;
Date.now = () => clock;
global.setTimeout = (callback, delay) => { clock += delay; callback(); };
const links = fixture.links.map(item => ({
    href: item.url,
    textContent: item.label,
    getAttribute: name => ({'aria-label': item.label, title: item.title,
        'aria-describedby': item.description ? 'timestamp-tooltip' : null})[name] || null
}));
const articles = links.length ? [{
    innerText: 'Synthetic page post',
    parentElement: {closest: () => null},
    querySelectorAll: () => links
}] : [];
global.document = {
    title: 'Synthetic page',
    readyState: 'complete',
    body: {innerText: fixture.body},
    documentElement: {outerHTML: ''},
    getElementById: id => id === 'timestamp-tooltip' ? {
        textContent: fixture.links.find(item => item.description)?.description || ''
    } : null,
    querySelector: selector => selector === 'div[role="feed"]' && articles.length ? {} : null,
    querySelectorAll: selector => selector === 'div[role="article"]' ? articles : []
};
""" + "\nPromise.resolve(" + facebook._FB_PAGE_METRICS_JS + ").then(result => console.log(JSON.stringify(result)));"
    result = subprocess.run(
        [node, "-", fixture], input=script, text=True, encoding="utf-8",
        capture_output=True, timeout=10, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("text", [
    "Ngày tạo trang: 01/01/2024",
    unicodedata.normalize("NFD", "Ngày tạo trang: 01/01/2024"),
    "Ngày tạo trang\n1 tháng 1, 2024",
    "Page created\nJanuary 1, 2024",
])
def test_creation_date_reads_unicode_and_adjacent_date_line(text):
    result = _metrics(text)
    assert facebook.parse_fb_created_date(result["createdRaw"]).isoformat() == "2024-01-01"


def test_creation_heading_without_a_date_is_not_an_observation():
    assert _metrics("Page created\nSee more")['createdRaw'] is None


@pytest.mark.parametrize("page_url,author_id,expected_times", [
    ("https://www.facebook.com/profile.php?id=111111111", "111111111", 1),
    ("https://www.facebook.com/profile.php?id=111111111", "999999999", 0),
    ("https://www.facebook.com/111111111", "111111111", 1),
    ("https://www.facebook.com/111111111", "999999999", 0),
])
def test_activity_requires_matching_profile_identity(page_url, author_id, expected_times):
    result = _metrics("Synthetic page post", url=page_url, links=[
        {"url": f"https://www.facebook.com/profile.php?id={author_id}", "label": "Author"},
        {"url": f"https://www.facebook.com/permalink.php?story_fbid=12345&id={author_id}",
         "label": "2 days ago"},
    ])
    assert len(result["times"]) == expected_times


def test_activity_rejects_other_profiles_timestamp_inside_own_post():
    result = _metrics("Synthetic page post", links=[
        {"url": "https://www.facebook.com/profile.php?id=111111111", "label": "Author"},
        {"url": "https://www.facebook.com/permalink.php?story_fbid=12345&id=999999999",
         "label": "2 days ago"},
    ])
    assert result["times"] == []


@pytest.mark.parametrize("attribute", ["label", "title", "description"])
def test_activity_reads_explicit_year_and_time_from_own_anchor(attribute):
    timestamp = {"url": "https://www.facebook.com/permalink.php?story_fbid=12345&id=111111111",
                 "label": "29 Tháng 6"}
    timestamp[attribute] = "Thứ Hai, 29 Tháng 6, 2026 lúc 23:46"
    result = _metrics("Synthetic post", links=[
        {"url": "https://www.facebook.com/profile.php?id=111111111", "label": "Author"},
        timestamp,
    ])
    assert len(result["times"]) == 1
    assert result["times"][0]["raw"] == timestamp[attribute]


def test_activity_does_not_guess_year_for_short_calendar_label():
    result = _metrics("Synthetic post", links=[
        {"url": "https://www.facebook.com/profile.php?id=111111111", "label": "Author"},
        {"url": "https://www.facebook.com/permalink.php?story_fbid=12345&id=111111111",
         "label": "29 Tháng 6"},
    ])
    assert result["times"] == []
