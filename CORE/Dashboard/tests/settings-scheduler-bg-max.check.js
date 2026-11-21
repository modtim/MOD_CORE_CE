'use strict';
// rc20-batch R03 — dash-ce-batch16.spec.ts:88 (#scheduler-background-max).
//
// _refreshSchedulerSubTab() re-reads /platform/scheduler-runtime-config and
// resets the four slider <input>s and their value labels. Before the fix it
// reset capacity-floor / packer-full / variance-penalty but NOT
// scheduler-background-max (or its -value label), so after Save + page.reload()
// the Background slider still showed the HTML default (settings.html:1007
// value="40") instead of the persisted background_max_fleet_pct — the e2e
// `toHaveValue(want.bg)` at :88 timed out.
//
//   node CORE/Dashboard/tests/settings-scheduler-bg-max.check.js
//   MCW_OVERRIDE old-file proof: the harness reads the run-tree settings.js, so
//   overriding it with the pre-fix copy makes the background asserts fail.
//
// Checks:
//   1. settings.js still parses as a valid ES module
//   2. _refreshSchedulerSubTab resets the background-max input .value AND the
//      -value label textContent from snap.background_max_fleet_pct
//   3. the other three sliders are still reset (no regression)
//   4. behaviour: the REAL function body, run in node:vm against a DOM shim,
//      leaves every slider + label equal to the snapshot (bg-max would stay at
//      the HTML default 40 on the pre-fix file), sets the preset dropdown, and
//      hides the source hint only when source === 'db'

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const SETTINGS_ABS = path.join(__dirname, '..', 'js', 'settings.js');
const src = fs.readFileSync(process.argv[2] || SETTINGS_ABS, 'utf8');

// --- 1) still a valid ES module ----------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: src, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
assert.ok(!modErr, `settings.js not a valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

function bodyOf(name) {
    const start = src.indexOf('async function ' + name + '(');
    assert.ok(start >= 0, name + ' must exist in settings.js');
    let depth = 0;
    const i = src.indexOf('{', start);
    for (let j = i; j < src.length; j++) {
        if (src[j] === '{') depth++;
        else if (src[j] === '}') { depth--; if (!depth) return src.slice(start, j + 1); }
    }
    throw new Error('unbalanced braces for ' + name);
}

const fnSrc = bodyOf('_refreshSchedulerSubTab');

// --- 2+3) static shape -------------------------------------------------
assert.ok(/getElementById\('scheduler-background-max'\)\.value\s*=\s*snap\.background_max_fleet_pct/.test(fnSrc),
    'R03: _refreshSchedulerSubTab must set the scheduler-background-max input from snap.background_max_fleet_pct');
assert.ok(/getElementById\('scheduler-background-max-value'\)\.textContent\s*=\s*snap\.background_max_fleet_pct/.test(fnSrc),
    'R03: _refreshSchedulerSubTab must set the scheduler-background-max-value label from snap.background_max_fleet_pct');
for (const id of ['scheduler-capacity-floor', 'scheduler-packer-full', 'scheduler-variance-penalty']) {
    assert.ok(new RegExp(`getElementById\\('${id}'\\)\\.value\\s*=`).test(fnSrc),
        `regression: ${id} input must still be reset`);
}

// --- 4) behaviour harness ---------------------------------------------
const SLIDERS = {
    'scheduler-capacity-floor': 'capacity_floor_pct',
    'scheduler-packer-full': 'packer_full_above_util_pct',
    'scheduler-variance-penalty': 'variance_penalty_s',
    'scheduler-background-max': 'background_max_fleet_pct',
};
// HTML defaults (settings.html:966/978/990/1007 inputs + the -value spans).
const HTML_DEFAULT = {
    'scheduler-capacity-floor': '30', 'scheduler-packer-full': '70',
    'scheduler-variance-penalty': '15', 'scheduler-background-max': '40',
};

const els = {};
for (const id in HTML_DEFAULT) {
    els[id] = { id, value: HTML_DEFAULT[id], textContent: HTML_DEFAULT[id] };
    els[id + '-value'] = { id: id + '-value', value: '', textContent: HTML_DEFAULT[id] };
}
const customOpt = { hidden: true, disabled: true };
els['scheduler-preset'] = { id: 'scheduler-preset', value: 'normal', textContent: '', querySelector: () => customOpt };
const hint = { id: 'scheduler-source-hint', hidden: true };
hint.classList = { add(c) { if (c === 'hidden') hint.hidden = true; }, remove(c) { if (c === 'hidden') hint.hidden = false; } };
els['scheduler-source-hint'] = hint;

const document = {
    createElement: () => null,
    getElementById: (id) => {
        assert.ok(els[id], `getElementById('${id}') must exist`);
        return els[id];
    },
};

async function runRefresh(snapshot) {
    const loadSchedulerRuntimeConfig = async () => snapshot;
    const ctx = vm.createContext({ document, loadSchedulerRuntimeConfig });
    vm.runInContext(fnSrc, ctx, { filename: 'settings.js' });
    await vm.runInContext('_refreshSchedulerSubTab()', ctx);
}

(async () => {
    // a) persisted light preset (bg 20) — the pre-fix file leaves bg at 40.
    const light = { preset_name: 'light', capacity_floor_pct: 50, packer_full_above_util_pct: 50, variance_penalty_s: 30, background_max_fleet_pct: 20, source: 'db' };
    await runRefresh(light);
    for (const id in SLIDERS) {
        assert.strictEqual(els[id].value, light[SLIDERS[id]],
            `${id} input must equal the snapshot ${SLIDERS[id]} after refresh`);
        assert.strictEqual(els[id + '-value'].textContent, light[SLIDERS[id]],
            `${id}-value label must equal the snapshot after refresh`);
    }
    assert.strictEqual(els['scheduler-preset'].value, 'light', 'preset dropdown must follow the snapshot');
    assert.strictEqual(customOpt.disabled, true, 'the custom option must stay disabled');
    assert.strictEqual(hint.hidden, true, "source hint must stay hidden when source === 'db'");

    // b) aggressive via env (bg 60) — source hint shows, custom option hidden.
    for (const id in HTML_DEFAULT) { els[id].value = HTML_DEFAULT[id]; els[id].textContent = HTML_DEFAULT[id]; }
    const agg = { preset_name: 'aggressive', capacity_floor_pct: 10, packer_full_above_util_pct: 85, variance_penalty_s: 5, background_max_fleet_pct: 60, source: 'env' };
    await runRefresh(agg);
    assert.strictEqual(els['scheduler-background-max'].value, 60,
        'background-max must reset to the snapshot even when the HTML default was 40');
    assert.strictEqual(els['scheduler-background-max-value'].textContent, 60,
        'background-max label must reset too');
    assert.strictEqual(els['scheduler-preset'].value, 'aggressive', 'preset dropdown must follow the snapshot');
    assert.strictEqual(hint.hidden, false, 'source hint must show when source !== db');

    console.log('settings-scheduler-bg-max.check.js: all asserts passed');
})().catch(e => { console.error('settings-scheduler-bg-max.check.js FAILED: ' + String(e).split('\n')[0]); process.exit(1); });
