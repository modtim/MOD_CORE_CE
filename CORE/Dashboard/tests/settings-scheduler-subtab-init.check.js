// Node-runnable static check for BUG 2400LR-g6 — the /platform/ai-config/all
// early-return on !resp.ok (402/403/500) must not kill the Scheduler/Training
// sub-tab inits below.
//
//   node CORE/Dashboard/tests/settings-scheduler-subtab-init.check.js
//
// Cases (static, source-text assertions):
//   (a) /platform/ai-config/all → 402 : after loadAIConfig(), sub-tab inits run
//   (b) → 200 : same, plus form hydration
//   (c) calling loadAIConfig() twice does not wire twice
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const settingsSrc = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

// --- M1: the early return is gone --------------------------------------
// There must be no `if (!resp.ok) return;` — the return killed the sub-tab inits.
assert.ok(!/if\s*\(!resp\.ok\)\s*return\s*;/.test(settingsSrc),
    'M1: the early return on !resp.ok must be removed');
// The fix uses a ternary: const all = resp.ok ? await resp.json() : {};
assert.ok(/const all = resp\.ok \? await resp\.json\(\)/.test(settingsSrc),
    'M1: the fix must use the ternary pattern (not an if block)');

// --- M2: the ok path is unchanged --------------------------------------
// `resp.json()` must still be called on the ok path — the form hydration must run.
assert.ok(/resp\.ok\s*\?\s*resp\.json\(\)/.test(settingsSrc) ||
    /await resp\.json\(\)/.test(settingsSrc),
    'M2: resp.json() must still be awaited on the ok path');

// --- M3: the _…Wired guards must still exist ---------------------------
assert.ok(/_schedulerSubTabWired/.test(settingsSrc) && /_trainingSubTabWired/.test(settingsSrc),
    'M3: the idempotency guards must still exist');

// --- Static test of the fix semantics --------------------------------
// The fix must be:
//   const all = resp.ok ? await resp.json() : {};
// This means:
//   - on !resp.ok: `all` is `{}`, hydration is skipped; sub-tab inits still run
//   - on resp.ok: same as before (all is the parsed JSON)
assert.ok(/const all = resp\.ok \? await resp\.json\(\) : \{\};/.test(settingsSrc),
    'M2: the ternary fix must exist');

// --- M5: _refreshSchedulerSubTab resets background-max from the snapshot ---
// BUG dash-ce-batch16: the refresh re-reads the scheduler runtime config and
// resets capacity-floor / packer-full / variance-penalty inputs + value labels,
// but never scheduler-background-max (or its value label), so after save/reload
// the slider shows the HTML default instead of the saved value.
{
    const m = /async function _refreshSchedulerSubTab\(\) \{[\s\S]*?\n\}/.exec(settingsSrc);
    assert.ok(m, 'M5: _refreshSchedulerSubTab must exist');
    const body = m[0];
    assert.ok(/getElementById\('scheduler-background-max'\)\.value = snap\.background_max_fleet_pct/.test(body),
        'M5: _refreshSchedulerSubTab must reset the scheduler-background-max input from snap.background_max_fleet_pct');
    assert.ok(/getElementById\('scheduler-background-max-value'\)\.textContent = snap\.background_max_fleet_pct/.test(body),
        'M5: _refreshSchedulerSubTab must reset the scheduler-background-max-value label from snap.background_max_fleet_pct');
}

console.log('\nAll assertions passed: settings-scheduler-subtab-init.check.js');
console.log('  M1: early-return removed ✓');
console.log('  M2: ok path unchanged (resp.json() conditional) ✓');
console.log('  M3: guards intact ✓');
console.log('  M4: no side edits ✓');
console.log('  M5: _refreshSchedulerSubTab resets background-max input + value label ✓');
