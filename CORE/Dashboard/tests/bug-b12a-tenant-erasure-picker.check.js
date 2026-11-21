// BUG.B12a — Dashboard "Data requests" → Tenant erasure picker.
//
// Verifies:
//   1) js/data-requests.js parses as a valid ES module.
//   2) The confirm input has NO datalist (typed-confirm safeguard preserved).
//   3) A separate "Tenant to erase" picker (input + datalist) exists.
//   4) The picker is shown only to platform admins.
//   5) An unmatched picker value blocks the button (inline error).
//   6) The confirm box compares against the picked (or default) tenant name.
//   7) The erasure request carries tenant_id when a target is picked.

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
const jsSrc = read('js/data-requests.js');
let err = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: jsSrc, encoding: 'utf8' });
} catch (e) {
    err = String(e && (e.stderr || e.message));
}
assert.ok(!err,
    `js/data-requests.js: not valid ES module — ${(err || '').split('\n')[0] || 'unknown'}`);

// --- 2) Confirm input has NO datalist --------------------------------------
assert.ok(!jsSrc.includes("list=\"dr-tenant-datalist\"") ||
          jsSrc.includes('id="dr-tenant-picker"'),
    'js/data-requests.js: datalist must NOT be on the confirm input (checked via HTML)');

const htmlSrc = read('pages/data-requests.html');

// The confirm input must NOT have a list attribute.
// Find the confirm input line and verify no list= on it.
const confirmInputMatch = htmlSrc.match(
    /id="dr-tenant-name-input"[^>]*>/s
);
assert.ok(confirmInputMatch,
    'pages/data-requests.html: must have confirm input id="dr-tenant-name-input"');
const confirmLine = confirmInputMatch[0];
assert.ok(!confirmLine.includes('list='),
    'pages/data-requests.html: confirm input must NOT have list= attribute');
assert.ok(confirmLine.includes('autocomplete="off"'),
    'pages/data-requests.html: confirm input must keep autocomplete="off"');

// --- 3) Separate picker exists (BUG.B12c: normal styled <select>) ----------
assert.ok(htmlSrc.includes('id="dr-tenant-picker"'),
    'pages/data-requests.html: must have tenant picker id="dr-tenant-picker"');
assert.ok(htmlSrc.includes('<select id="dr-tenant-picker"'),
    'pages/data-requests.html: tenant picker must be a <select> (normal dropdown)');
assert.ok(!htmlSrc.includes('dr-tenant-datalist'),
    'pages/data-requests.html: type-ahead datalist must be gone (BUG.B12c)');
assert.ok(htmlSrc.includes('id="dr-picker-wrap"'),
    'pages/data-requests.html: picker wrapper div must exist (for show/hide)');
assert.ok(htmlSrc.includes('id="dr-picker-error"'),
    'pages/data-requests.html: picker error container must exist');

// --- 4) Picker is conditional on platform admin ---------------------------
assert.ok(jsSrc.includes('is_platform_admin'),
    'js/data-requests.js: must check user.is_platform_admin to show picker');
assert.ok(jsSrc.includes('me.get()'),
    'js/data-requests.js: must call me.get() to check role');
assert.ok(jsSrc.includes("dr-picker-wrap") && jsSrc.includes("classList.remove('hidden')") ||
          jsSrc.includes("classList.remove(\"hidden\")"),
    'js/data-requests.js: must show picker wrapper for platform admins');

// --- 5) Unmatched picker value blocks submit ------------------------------
assert.ok(jsSrc.includes("No matching tenant found") ||
          jsSrc.includes('No matching tenant'),
    'js/data-requests.js: must show inline error for unmatched picker value');
assert.ok(jsSrc.includes('dr-picker-error'),
    'js/data-requests.js: must write error to dr-picker-error element');
assert.ok(jsSrc.includes("nameInput.disabled") &&
          jsSrc.includes('true') &&
          jsSrc.includes("_targetTenantId = null"),
    'js/data-requests.js: unmatched value must disable name input and clear tenant_id');
assert.ok(jsSrc.includes("erasureBtn.disabled") &&
          jsSrc.includes('true'),
    'js/data-requests.js: unmatched value must disable erasure button');

// --- 6) Picker sets _tenantName for confirm comparison --------------------
assert.ok(jsSrc.includes('_tenantName = match.value') ||
          jsSrc.includes("_tenantName = match.value"),
    'js/data-requests.js: matched picker must set _tenantName from matched value');
assert.ok(jsSrc.includes('erasureBtn.disabled = true'),
    'js/data-requests.js: after picking a tenant, erasure button must be re-disabled (needs confirm)');

// --- 7) Picker sets _targetTenantId and sends to API ----------------------
assert.ok(jsSrc.includes('_targetTenantId'),
    'js/data-requests.js: must have _targetTenantId variable');
assert.ok(jsSrc.includes("tenantDsar.requestErasure(_targetTenantId)"),
    'js/data-requests.js: must pass _targetTenantId to requestErasure()');

// --- 8) Dropdown population from /tenants ----------------------------------
assert.ok(jsSrc.includes("fetchApi('/tenants')"),
    'js/data-requests.js: must load tenants via shared fetchApi(\'/tenants\')');
assert.ok(!jsSrc.includes('sessionStorage'),
    'js/data-requests.js: must not hand-roll auth via sessionStorage accessToken');
assert.ok(!jsSrc.includes('apiUrl'),
    'js/data-requests.js: must not build a raw apiUrl (skips /v1 base + token refresh)');
assert.ok(jsSrc.includes('<option') ||
          jsSrc.includes("createElement('option')"),
    'js/data-requests.js: must create <option> elements from tenants list');
assert.ok(jsSrc.includes('label'),
    'js/data-requests.js: options must carry a label (tenant id)');

// --- 9) Typed-confirmation safeguard still present ------------------------
assert.ok(jsSrc.includes("nameInput.value.trim() !== _tenantName"),
    'js/data-requests.js: confirm box must still require exact tenant name match');
assert.ok(jsSrc.includes("Type \"\${_tenantName}\" to enable") ||
          jsSrc.includes(`Type "${'_tenantName'}" to enable`) ||
          jsSrc.includes('Type "' + '_tenantName' + '" to enable'),
    'js/data-requests.js: placeholder must reference _tenantName');

console.log('bug-b12a-tenant-erasure-picker: all assertions passed -> OK');
