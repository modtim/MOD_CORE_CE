// rc20-batch — apps.js register-app-button listener must attach AFTER the
// initial loadApps() await, so the e2e dash-apps click can't race the list
// load (static source-order assert, no browser).
// Also verifies openRegisterAppModal is async and re-fetches /stacks before
// rendering the dropdown (CE fix-dash-apps: stack created in another tab
// would otherwise be invisible in the modal).
//
//   node CORE/Dashboard/tests/apps-register-listener-order.check.js
//
// Checks:
//   1) js/apps.js parses as a valid ES module.
//   2) initializeAppsPage awaits loadApps() before wiring register-app-button.
//   3) The register-app-button listener is still attached (not just removed).
//   4) No register-app-button addEventListener appears before the await.
//   5) openRegisterAppModal is async and fetches stacks before _stackOptions.

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

// Isolate initializeAppsPage body (up to the next top-level section).
const start = js.indexOf('export async function initializeAppsPage');
assert.ok(start >= 0, 'initializeAppsPage must exist');
const bodyEnd = js.indexOf('// ---------------------------------------------------------------------------', start + 10);
const body = js.slice(start, bodyEnd > start ? bodyEnd : js.length);

// 2) await loadApps() before register-app-button wiring
const awaitIdx = body.search(/await\s+Promise\.all\(\[loadApps\(\)/);
assert.ok(awaitIdx >= 0, 'initializeAppsPage must await loadApps() (Promise.all form)');
const listenerIdx = body.search(/addEventListener\('click', \(\) => openRegisterAppModal\(\)\)/);
assert.ok(listenerIdx >= 0, 'register-app-button click listener must be attached');
assert.ok(listenerIdx > awaitIdx,
    'register-app-button listener must be attached AFTER the loadApps await ' +
    `(listener at char ${listenerIdx}, await at char ${awaitIdx})`);

// 3) listener still wired to the button lookup
assert.ok(/getElementById\('register-app-button'\)/.test(body),
    'initializeAppsPage must still look up #register-app-button');

// 4) no early wiring anywhere before the await in the body
const beforeAwait = body.slice(0, awaitIdx);
assert.ok(!/register-app-button/.test(beforeAwait),
    'nothing referencing register-app-button may run before the loadApps await');

// 5) openRegisterAppModal is async and re-fetches /stacks before rendering
const openFn = js.indexOf('async function openRegisterAppModal(app)');
assert.ok(openFn >= 0, 'openRegisterAppModal must be async');
const endBody = js.indexOf('} else {', openFn + 10);
const openBody = endBody > openFn ? js.slice(openFn, endBody) : js.slice(openFn, openFn + 2000);
assert.ok(/_stacks\s*=\s*await\s+apiCall\s*\(.*/.test(openBody),
    'openRegisterAppModal must await apiCall on /stacks');
assert.ok(/\/stacks/.test(openBody), 'must fetch stacks from the API');

console.log('apps-register-listener-order.check.js: all asserts passed');
