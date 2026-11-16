"""Dashboard behavior with synthetic DOM/API fixtures; no browser launched."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_edit_identity_and_launch_warning_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for dashboard behavior tests")
    script = r"""
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');
const nodes = new Map();
const doc = {
  addEventListener() {}, querySelectorAll() {return [];},
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', checked: false, options: [],
      classList: {add() {}, remove() {}, toggle() {}},
    });
    return nodes.get(id);
  }
};
let payload;
const context = vm.createContext({document: doc, window: {},
  fetch: async (url, options) => {payload = JSON.parse(options.body); return {ok: true};}
});
// Suppress only page-load rendering; invoke the production edit/launch functions below.
const source = fs.readFileSync('web/static/js/dashboard.js', 'utf8');
vm.runInContext(source.replace(/setFilter\('all'\);\s*$/, ''), context);
vm.runInContext('showToast = () => {}; refreshProfiles = () => {};', context);
const original = {id: 'synthetic-id', name: 'Original', revision: 7,
  fingerprint: {os: 'windows', canvas_noise_seed: 0, audio_noise_seed: 7654321,
    fingerprint_algorithm_version: 1, verification_mode: 'compatibility',
    stealth_disable: ['audio'], webgl_renderer: 'Native synthetic GPU',
    user_agent: 'Chrome/155.0.1.0', custom_future_field: 'keep-me'}
};
vm.runInContext('currentProfiles = ' + JSON.stringify([original]), context);
(async () => {
  context.openEditModal(original.id);
  assert.equal(doc.getElementById('edit-fp-algorithm').value, '1');
  assert.equal(doc.getElementById('edit-fp-verification').value, 'compatibility');
  // DOM input values are always strings in an actual browser.
  for (const element of nodes.values()) element.value = String(element.value);
  doc.getElementById('edit-name').value = 'Unrelated name edit';
  await context.submitSaveProfileEdit();
  assert.equal(payload.expected_revision, 7);
  assert.equal(payload.fingerprint.fingerprint_algorithm_version, 1);
  assert.equal(payload.fingerprint.verification_mode, 'compatibility');
  assert.equal(payload.fingerprint.canvas_noise_seed, 0);
  assert.equal(payload.fingerprint.audio_noise_seed, 7654321);
  assert.equal(payload.fingerprint.custom_future_field, 'keep-me');
  context.openEditModal(original.id);
  for (const element of nodes.values()) element.value = String(element.value);
  doc.getElementById('edit-fp-algorithm').value = '2';
  doc.getElementById('edit-fp-verification').value = 'strict';
  await context.submitSaveProfileEdit();
  assert.equal(payload.fingerprint.fingerprint_algorithm_version, 2);
  assert.equal(payload.fingerprint.verification_mode, 'strict');
  assert.equal(payload.fingerprint.canvas_noise_seed, 0);
  assert.equal(payload.fingerprint.audio_noise_seed, 7654321);
  for (const field of ['configuration_verified', 'navigation_allowed', 'browser_started', 'cdp_ready']) {
    assert.equal(context.launchOutcomeNotice({[field]: false}, 'OK').type, 'warning', field);
  }
  for (const field of ['degraded', 'cleanup_failed']) {
    assert.equal(context.launchOutcomeNotice({[field]: true}, 'OK').type, 'warning', field);
  }
  for (const separation of ['canvas_2d_only', 'none_from_render_hooks', 'legacy_unverified']) {
    assert.equal(context.launchOutcomeNotice({fingerprint_capabilities: {profile_separation: separation}}, 'OK').type, 'warning');
  }
  const combined = context.launchOutcomeNotice({gate: {status: 'warning', message: 'GATE'},
    configuration_verified: false, warnings: ['RUNTIME'], fingerprint_capabilities: {profile_separation: 'canvas_2d_only'}}, 'OK');
  assert.ok(combined.message.includes('GATE') && combined.message.includes('RUNTIME') && combined.message.includes('canvas 2D'));
  const failed = context.launchOutcomeNotice({detail: {code: 'TEST', message: 'Sanitized failure'}, cleanup_failed: true}, 'fallback', false);
  assert.equal(failed.type, 'error');
  assert.ok(failed.message.includes('Sanitized failure') && !failed.message.includes('[object Object]'));
  assert.equal(context.launchOutcomeNotice({configuration_verified: true, navigation_allowed: true}, 'OK').type, 'success');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8",
                            cwd=Path(__file__).parent, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
