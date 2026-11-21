// rc20-batch — TOKMERGE T3: the create-api-key modal must AWAIT the stack-option
// population, otherwise the Playwright spec reads the select before the /stacks
// fetch appends its options (picker empty for every created stack id).
// Static source-text asserts, no browser (same style as
// CORE/Dashboard/tests/apps-stack-options-fresh.check.js).
//
//   node CORE/Dashboard/tests/tokmerge-t3-picker-await.check.js
//   harness: <test harness> nodetest Dashboard tests/tokmerge-t3-picker-await.check.js
//
// Checks:
//   1) js/settings.js parses as a valid ES module (an `await` outside an async
//      function is a SyntaxError, so this is the non-vacuity guard for m1).
//   2) openCreateApiKeyModal is async.
//   3) its body awaits populateApiKeyStackOptions().
//   4) no un-awaited bare call of populateApiKeyStackOptions() remains anywhere.
//   5) the click listener still passes the handler by reference (unchanged).
//   6) populateApiKeyStackOptions itself stays async (the /stacks fetch is its own).
// Non-vacuity: the HEAD copy (no async, no await) fails 2, 3 and 4.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'js', 'settings.js'), 'utf8');

// 1) ES module syntax (m1: dropping `async` while keeping `await` breaks this)
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: src, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
assert.ok(!modErr, `settings.js not a valid ES module — ${(modErr || 'unknown').split('\n')[0]}`);

// 2) openCreateApiKeyModal is async
assert.ok(/async function openCreateApiKeyModal\(\)/.test(src),
    'openCreateApiKeyModal must be declared async');

// 3) its body awaits the picker population (m2: dropping `await` breaks 3 and 4)
const fn = /async function openCreateApiKeyModal\(\)\s*\{[\s\S]*?\n\}/.exec(src);
assert.ok(fn, 'openCreateApiKeyModal body must be found');
assert.ok(/await\s+populateApiKeyStackOptions\(\)\s*;/.test(fn[0]),
    'openCreateApiKeyModal must await populateApiKeyStackOptions()');

// 4) no un-awaited bare call anywhere in the file
assert.ok(!/^\s*populateApiKeyStackOptions\(\)\s*;/m.test(src),
    'no un-awaited populateApiKeyStackOptions() call may remain');

// 5) the listener is untouched: handler by reference, no invocation
assert.ok(/addEventListener\(\s*'click'\s*,\s*openCreateApiKeyModal\s*\)/.test(src),
    "the 'click' listener must still pass openCreateApiKeyModal by reference");

// 6) the populator stays async
assert.ok(/async function populateApiKeyStackOptions\(\)/.test(src),
    'populateApiKeyStackOptions must stay async');

console.log('OK tokmerge-t3-picker-await: 6 checks');
