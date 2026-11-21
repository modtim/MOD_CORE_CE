'use strict';
// FIX-CE-NAV-REGROUP-CLASS-C — the folded Logs tab
// (dash-admin-nav-regroup.spec.ts "System/Logs/Marketplace fold their children
// into in-page tabs") surfaced `Error loading workflow runs: TypeError:
// Cannot set properties of null (setting 'textContent')` from the catch in
// logs.js loadWorkflowRuns. The only unguarded getElementById(...).textContent
// write on the loader path is showLoadingOverlay's #loading-message write
// (logs.js:673): with the folded layout the overlay can exist while its
// #loading-message span was moved/removed, so the setter threw on null and the
// catch painted the red row. Prove the shipped copy now guards it.
//
//   node CORE/Dashboard/tests/test_logs_workflow_runs_null_safe.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');

// --- 1) logs.js still parses as an ES module (the Dashboard loads js/* as modules)
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: logsJs, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the old unguarded write is gone --------------------------------------
assert.ok(
    !/document\.getElementById\('loading-message'\)\.textContent\s*=/.test(logsJs),
    'logs.js must not write #loading-message through an unguarded getElementById',
);

// --- 3) the guarded replacement is present -----------------------------------
assert.ok(/const msgEl = document\.getElementById\('loading-message'\);/.test(logsJs),
    'showLoadingOverlay holds the element in a const');
assert.ok(/if \(msgEl\) msgEl\.textContent = message;/.test(logsJs),
    'the write is null-guarded');

// --- 4) behaviour unchanged when the element exists: the overlay is still
//     re-shown right after the guarded write.
assert.ok(/if \(msgEl\) msgEl\.textContent = message;\s*\n\s*overlay\.classList\.remove\('hidden'\);/.test(logsJs),
    'overlay.classList.remove(hidden) still follows the guarded write');

// --- 5) the throw shape itself: a null-element write is a TypeError; the
//     guarded form must not throw when the element is missing.
let threw = null;
try {
    const el = null; // folded layout: #loading-message absent
    el.textContent = 'Loading...';
} catch (e) {
    threw = e;
}
assert.ok(threw instanceof TypeError,
    'unguarded null write throws TypeError (the observed error shape)');
const msgEl = null;
if (msgEl) msgEl.textContent = 'Loading...'; // the fixed form: must not throw
