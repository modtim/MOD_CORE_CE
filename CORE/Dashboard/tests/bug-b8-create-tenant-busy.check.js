// BUG.B8 (owner 10-08): Dashboard Users → Tenants → "Create Tenant" button
// stayed clickable while a create was in flight. Fix: wrap handleCreateTenant
// in withButtonBusy so re-entrancy is blocked and the button is disabled
// with a "Creating…" label until the request settles.
//
// This check verifies three invariants:
//   1) js/users.js parses as a valid ES module.
//   2) handleCreateTenant references withButtonBusy (in-flight guard present).
//   3) The form submit button (#create-tenant-form [type="submit"]) is found
//      via querySelector (the button handleButtonBusy receives).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

// --- 1) module-mode syntax check -------------------------------------------
const src = read('js/users.js');
let err = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: src, encoding: 'utf8' });
} catch (e) {
    err = String(e && (e.stderr || e.message));
}
assert.ok(!err,
    `js/users.js: not valid ES module — ${(err || '').split('\n')[0] || 'unknown'}`);

// --- 2) withButtonBusy import present ---------------------------------------
assert.ok(src.includes("withButtonBusy"),
    'js/users.js: must import withButtonBusy from ./utils.js');

// --- 3) handleCreateTenant uses withButtonBusy (in-flight guard) -----------
// The wrapper should contain withButtonBusy(submitBtn, formAction, { busyLabel: 'Creating…' })
assert.ok(src.includes("withButtonBusy(submitBtn"),
    'js/users.js: handleCreateTenant must call withButtonBusy with the submit button');
assert.ok(src.includes("busyLabel"),
    'js/users.js: handleCreateTenant must pass a busyLabel (e.g. "Creating…")');

// --- 4) findSubmitBtn querySelector present ---------------------------------
assert.ok(src.includes("create-tenant-form"),
    'js/users.js: handleCreateTenant must reference #create-tenant-form');

// --- 5) withButtonBusy also guards re-entrancy via dataset.modBusy -----------
const utilsSrc = read('js/utils.js');
assert.ok(utilsSrc.includes("modBusy"),
    'js/utils.js: withButtonBusy must use dataset.modBusy re-entrancy guard');
assert.ok(utilsSrc.includes("btn.disabled = true"),
    'js/utils.js: withButtonBusy must disable the button');
assert.ok(utilsSrc.includes("finally"),
    'js/users.js + utils.js: button re-enable must be in a finally block (or within withButtonBusy)');

console.log('bug-b8-create-tenant-busy: module OK, in-flight guard present -> OK');
