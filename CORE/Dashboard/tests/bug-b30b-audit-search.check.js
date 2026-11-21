'use strict';
// BUG.B30b (owner 10-08, /#audit-logs): "Audit: add a search bar".
// The Audit Logs page only had three select filters (action / resource type / date
// range) — no way to type "alice@" or "revoked" and find the rows. The API side
// (routers/audit_logs.py GET /audit-logs/permissions) gains a free-text `q` that
// matches action, resource type and actor/target email; this check guards the FE
// wiring:
//   * pages/audit-logs.html carries a full-width type="search" input id="filter-search"
//     above the filter selects, with the placeholder + aria-label,
//   * js/audit-logs.js reads #filter-search and appends `q` to the request params,
//   * Enter in the box applies the filters (like #apply-filters), typing debounces
//     400 ms then applies, and Reset clears the box.
//
//   node CORE/Dashboard/tests/bug-b30b-audit-search.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'pages', 'audit-logs.html'), 'utf8');
const js = fs.readFileSync(path.join(ROOT, 'js', 'audit-logs.js'), 'utf8');

// --- 1) audit-logs.js still parses as an ES module --------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: js, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/audit-logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the search input exists, above the selects -------------------------
const input = html.match(/<input[^>]*id="filter-search"[^>]*\/?>/);
assert.ok(input, 'pages/audit-logs.html: no id="filter-search" input');
assert.ok(/type="search"/.test(input[0]), 'the search bar must be type="search"');
assert.ok(/aria-label="Search audit logs"/.test(input[0]), 'the search bar needs the aria-label');
assert.ok(/placeholder="Search action, resource or user email…"/.test(input[0]),
    'the placeholder must name action / resource / user email');
assert.ok(/class="w-full border border-gray-300 rounded-lg px-3 py-2"/.test(input[0]),
    'the search bar is full width, bordered like the other controls');

const firstSelect = html.search(/id="filter-(action|resource-type|date-range)"/);
assert.ok(firstSelect > 0, 'the filter selects exist');
assert.ok(html.indexOf('id="filter-search"') < firstSelect,
    'the search bar sits ABOVE the filter selects');
assert.ok(html.indexOf('id="filter-search"') > html.indexOf('>Filters<'),
    'the search bar lives inside the Filters card');

// --- 3) audit-logs.js reads #filter-search and sets `q` -------------------
assert.ok(/getElementById\('filter-search'\)/.test(js),
    'js/audit-logs.js: nothing reads #filter-search');
const getter = js.match(/function getSearchValue\(\)\s*\{([\s\S]*?)\n\s*\}/);
assert.ok(getter, 'js/audit-logs.js: getSearchValue() helper missing');
assert.ok(/getElementById\('filter-search'\)/.test(getter[1]),
    'getSearchValue() must read #filter-search');
assert.ok(/\.trim\(\)/.test(getter[1]), 'the search value is trimmed before it is sent');

// The params may be built inline in loadAuditLogs() or by the shared
// buildFilterParams() helper (BUG.B29c Part C, so the export fallback honours the
// same filters) — either way `q` must come from getSearchValue().
const builder = js.match(/function buildFilterParams\([^)]*\)\s*\{([\s\S]*?)\n\s*\}/);
const req = js.match(/const params = (?:new URLSearchParams\(\{[\s\S]*?|buildFilterParams\([^)]*\))[\s\S]*?apiCall\(`\/audit-logs\/permissions\?\$\{params\.toString\(\)\}`\)/);
assert.ok(req, 'js/audit-logs.js: the /audit-logs/permissions request not found');
const qSource = builder ? builder[1] : req[0];
assert.ok(/params\.append\('q',\s*searchValue\)/.test(qSource),
    'the request must append q from the search bar');
assert.ok(/const searchValue = getSearchValue\(\);/.test(qSource),
    'q comes from getSearchValue()');
assert.ok(/if \(searchValue\) params\.append/.test(qSource),
    'q is only sent when the search bar is non-empty');

// --- 4) Enter applies, typing debounces 400 ms, reset clears --------------
const searchBlock = js.slice(js.indexOf('const searchInput = document.getElementById'));
assert.ok(searchBlock.length > 0, 'js/audit-logs.js: no search-input wiring block');
assert.ok(/addEventListener\('keydown'/.test(searchBlock), 'the search bar listens for keydown');
assert.ok(/e\.key === 'Enter'/.test(searchBlock), 'Enter in the box applies the filters');
assert.ok(/applySearch\(\)/.test(searchBlock), 'Enter calls applySearch()');
assert.ok(/currentPage = 0;/.test(searchBlock.slice(searchBlock.indexOf('const applySearch'))),
    'changing the search returns to the first page');
assert.ok(/loadAuditLogs\(\);/.test(searchBlock.slice(searchBlock.indexOf('const applySearch'))),
    'applySearch() reloads the logs');

assert.ok(/setTimeout\(\(\) => \{[\s\S]*?\}, 400\)/.test(searchBlock),
    'typing debounces 400 ms before applying');
assert.ok(/scheduleSearchApply\(\)/.test(searchBlock), 'a keystroke schedules the debounced apply');
assert.ok(/addEventListener\('input', scheduleSearchApply\)/.test(searchBlock),
    'the input event also debounces');
assert.ok(/clearTimeout\(searchDebounceTimer\)/.test(searchBlock),
    'the pending debounce is cancelled so a burst is one request');
assert.ok(/if \(q === lastAppliedSearch\) return;/.test(searchBlock),
    'no extra request when the search did not change');

// B29c fix: null-safe wiring — getElementById stored in a const, then guarded.
const reset = js.match(/btnReset = document\.getElementById\('reset-filters'\)[\s\S]*?addEventListener\('click', \(\) => \{([\s\S]*?)\n\s*\}\);/);
assert.ok(reset, 'js/audit-logs.js: #reset-filters handler missing');
assert.ok(/getElementById\('filter-search'\)/.test(reset[1]),
    'Reset must clear the search bar too');
assert.ok(/\.value = '';/.test(reset[1]), 'Reset blanks the search bar');

console.log('bug-b30b-audit-search: OK');
