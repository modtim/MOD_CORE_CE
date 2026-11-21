// rc20-batch — fix-dash-apps: the register-app modal's stack-select options
// must reflect stacks created AFTER page load. Static source-order assert
// (same style as apps-register-listener-order.check.js; no browser).
//
//   node CORE/Dashboard/tests/apps-stack-options-fresh.check.js
//
// Checks:
//   1) js/apps.js parses as a valid ES module.
//   2) openRegisterAppModal is async.
//   3) it awaits apiCall('/stacks') into _stacks BEFORE the modal body
//      (the openSimpleModal call) is built — so _stackOptions() reads the
//      refreshed list, not the one cached at page load.
//   4) the refresh is guarded (try/catch) so a /stacks failure degrades to
//      '(no stacks)' instead of throwing.
//   5) option labels are escaped with the file's escapeHtml helper.
// Non-vacuity: the old copy (HEAD+J37) has no await in openRegisterAppModal,
// so check 3 fails there (MCW_OVERRIDE old-file proof).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const js = read('js/apps.js');

// 1) ES module syntax
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: js, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
assert.ok(!modErr, `apps.js not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// 2) openRegisterAppModal is async
const openFn = js.indexOf('async function openRegisterAppModal(app)');
assert.ok(openFn >= 0, 'openRegisterAppModal must be async');

// 3) await apiCall('/stacks') before the modal body is built
const fetchIdx = js.search(/_stacks\s*=\s*await\s+apiCall\(\s*['"]\/stacks['"]/, openFn);
assert.ok(fetchIdx > 0,
    'openRegisterAppModal must await apiCall(\'/stacks\') into _stacks ' +
    '(stacks added after page load would never reach the dropdown)');
const modalIdx = js.indexOf('openSimpleModal(\'app-register-modal\'', openFn);
assert.ok(modalIdx > 0, 'openRegisterAppModal must open the app-register-modal');
assert.ok(fetchIdx < modalIdx,
    `stacks must be refreshed (await at char ${fetchIdx}) BEFORE the modal ` +
    `body is built (openSimpleModal at char ${modalIdx}) — otherwise the ` +
    `body template literal reads the stale page-load _stacks`);

// 4) guarded refresh: try/catch around the fetch, fallback to []
const between = js.slice(openFn, modalIdx);
assert.ok(/try\s*\{[\s\S]*_stacks\s*=\s*await\s+apiCall[\s\S]*\}\s*catch\s*\{[\s\S]*_stacks\s*=\s*\[\s*\];/.test(between),
    'the /stacks refresh must be wrapped in try/catch falling back to []');

// 5) option labels escaped with the file's escapeHtml helper
assert.ok(/function escapeHtml\(s\)/.test(js), 'apps.js must define escapeHtml');
const optFn = js.indexOf('function _stackOptions(');
assert.ok(optFn >= 0, '_stackOptions must exist');
const optBody = js.slice(optFn, optFn + 400);
assert.ok(/escapeHtml\(s\.name/.test(optBody),
    '_stackOptions must escape stack names with escapeHtml');

console.log('apps-stack-options-fresh.check.js: all asserts passed');
