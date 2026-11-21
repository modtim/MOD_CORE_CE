// BUG.B11b — Dashboard "Data requests" → category picker for the DSAR export
// (OWNER Q-B11=C: pick categories, then export).
//
// Verifies:
//   1) js/data-requests.js parses as a valid ES module.
//   2) pages/data-requests.html has the picker: category list container,
//      Select all / Clear buttons, above the Export button.
//   3) The categories are loaded from GET /tenant/dsar/export/categories.
//   4) One checkbox per category, label + description as help text.
//   5) All categories are checked by default.
//   6) Select all / Clear flip every checkbox.
//   7) Export is disabled while none are checked (button + refusal message).
//   8) Export sends the checked keys as repeated categories= params, no /v1.
//   9) The 422 detail message is surfaced on error.
//  10) No module-level DOM access (the page loads inside the Compliance hub).

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

const htmlSrc = read('pages/data-requests.html');

// --- 2) Picker markup exists, above the Export button ----------------------
assert.ok(htmlSrc.includes('id="dr-category-picker"'),
    'pages/data-requests.html: picker wrapper id="dr-category-picker" must exist');
assert.ok(htmlSrc.includes('id="dr-category-list"'),
    'pages/data-requests.html: category list container id="dr-category-list" must exist');
assert.ok(htmlSrc.includes('id="dr-cat-select-all"'),
    'pages/data-requests.html: Select all button id="dr-cat-select-all" must exist');
assert.ok(htmlSrc.includes('id="dr-cat-clear"'),
    'pages/data-requests.html: Clear button id="dr-cat-clear" must exist');
assert.ok(htmlSrc.indexOf('id="dr-category-list"') < htmlSrc.indexOf('id="dr-export-btn"'),
    'pages/data-requests.html: the picker must sit ABOVE the Export button');

// --- 3) Categories come from the categories endpoint -----------------------
assert.ok(jsSrc.includes("fetchApi('/tenant/dsar/export/categories')"),
    'js/data-requests.js: must load categories via fetchApi(\'/tenant/dsar/export/categories\')');
assert.ok(jsSrc.includes('res.categories') || jsSrc.includes('Array.isArray(res.categories)'),
    'js/data-requests.js: must read the categories array from the response');

// --- 4) One checkbox per category, label + description ---------------------
assert.ok(jsSrc.includes("createElement('input')") && jsSrc.includes("type = 'checkbox'"),
    'js/data-requests.js: must create one <input type=checkbox> per category');
assert.ok(jsSrc.includes('`dr-cat-${key}`'),
    'js/data-requests.js: checkbox ids must be dr-cat-<key>');
assert.ok(jsSrc.includes('c.label') && jsSrc.includes('c.description'),
    'js/data-requests.js: must read label + description from each category');
assert.ok(jsSrc.includes('aria-label'),
    'js/data-requests.js: checkbox must carry an aria-label (the category label)');

// --- 5) All checked by default ---------------------------------------------
assert.ok(jsSrc.includes("checkbox.value = 'checked'"),
    'js/data-requests.js: every checkbox starts checked (all on by default)');

// --- 6) Select all / Clear --------------------------------------------------
assert.ok(jsSrc.includes("dr-cat-select-all") && jsSrc.includes("dr-cat-clear"),
    'js/data-requests.js: must wire the Select all + Clear buttons');
assert.ok(jsSrc.includes("row.checkbox.value = 'checked'") && jsSrc.includes("row.checkbox.value = ''"),
    'js/data-requests.js: Select all checks every box, Clear unchecks every box');

// --- 7) Export disabled when none checked -----------------------------------
assert.ok(jsSrc.includes('_catCheckedCount'),
    'js/data-requests.js: must count the checked categories');
assert.ok(jsSrc.includes('_setExportEnabled'),
    'js/data-requests.js: must enable/disable the Export button from the checked count');
assert.ok(jsSrc.includes('Select at least one category to export.'),
    'js/data-requests.js: a click with none checked must refuse with a visible message');

// --- 8) Repeated categories= params, no /v1 ---------------------------------
assert.ok(jsSrc.includes('`categories=${encodeURIComponent(k)}`') ||
          jsSrc.includes('categories=${encodeURIComponent(k)}'),
    'js/data-requests.js: checked keys must go out as repeated categories= params');
assert.ok(jsSrc.includes('/tenant/dsar/export?'),
    'js/data-requests.js: the export call must hit /tenant/dsar/export (no /v1 prefix)');
assert.ok(!jsSrc.includes("fetchApi('/v1") && !jsSrc.includes('fetchApi(`/v1'),
    'js/data-requests.js: fetchApi paths must never include /v1');

// --- 9) 422 detail surfaced --------------------------------------------------
assert.ok(jsSrc.includes('_errText(err)'),
    'js/data-requests.js: the export failure must show the server detail (422 unknown key)');

// --- 10) No module-level DOM access ------------------------------------------
const beforeInit = jsSrc.split('export async function initializeDataRequestsPage')[0];
assert.ok(!beforeInit.includes('document.'),
    'js/data-requests.js: no module-level DOM access (page loads inside the Compliance hub)');

console.log('bug-b11b-export-category-picker: all assertions passed -> OK');
