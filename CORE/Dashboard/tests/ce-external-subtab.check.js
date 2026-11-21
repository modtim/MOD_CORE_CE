'use strict';
// rc20-ce-external (owner 11-04) — the System -> External subtab (external
// orchestrators: Kubernetes / Qube / Deadline) must WORK in Community Edition.
// Static source-text asserts (style of tests/accept-invitation-ce.check.js).
//
//   node CORE/Dashboard/tests/ce-external-subtab.check.js
//
// Checks:
//   1) js/analytics.js parses as a valid ES module.
//   2) pages/analytics.html ships the External tab button + panel ids (tab not hidden).
//   3) the main-panel-external activation leg calls loadExternalPanel() with
//      NO isPaidEdition gate and no CE hide of the tab/panel.
//   4) loadExternalPanel() still GETs /system/external and stops polling on 403.
//   5) open-core/manifest.yaml: routers/system_external.py + services/external_load.py
//      are api_allow (ship in CE) and are NOT in the deny list.
//   6) CORE/API/main.py still mounts the router via _GATE4B_ROUTER_MOUNTS.
//   7) the router + service import nothing paid (CE-safe import closure).

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

// Harness nodetest mounts: CORE/<dir> -> /app, CORE/API -> /API, open-core -> /open-core.
// Local run: repo root is 3 levels above tests/. Resolve both.
const UP1 = path.join(__dirname, '..');
const REPO = path.join(__dirname, '..', '..', '..');
function readAny(...cands) {
    for (const p of cands) { try { return fs.readFileSync(p, 'utf8'); } catch (e) { /* next */ } }
    throw new Error('none of ' + cands.join(' | ') + ' readable');
}
const js = readAny(path.join(UP1, 'js', 'analytics.js'));
const html = readAny(path.join(UP1, 'pages', 'analytics.html'));
// APPLY-CE-DENY-4-PRIVATE-GUARD-DASHBOARD-TESTS: open-core never ships
// in the CE extract, so the manifest read falls back to '' and the manifest block below is skipped
// when absent (the CE-visible asserts above/below still run; on the PRIVATE tree the manifest IS
// readable, so behaviour is unchanged there).
let manifest = '';
try { manifest = readAny('/open-core/manifest.yaml', path.join(REPO, 'open-core', 'manifest.yaml')); } catch (e) { /* absent in CE extract */ }
const mainpy = readAny('/API/main.py', path.join(REPO, 'CORE', 'API', 'main.py'));
const routerPy = readAny('/API/routers/system_external.py', path.join(REPO, 'CORE', 'API', 'routers', 'system_external.py'));
const servicePy = readAny('/API/services/external_load.py', path.join(REPO, 'CORE', 'API', 'services', 'external_load.py'));

// 1) ES module syntax
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: js, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
assert.ok(!modErr, `analytics.js not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// 2) markup carries the tab + panel, tab visible by default
assert.ok(/id="main-tab-external"/.test(html), 'pages/analytics.html missing #main-tab-external');
assert.ok(/id="main-panel-external"/.test(html), 'pages/analytics.html missing #main-panel-external');
const tabTag = /<button[^>]*id="main-tab-external"[^>]*>/.exec(html);
assert.ok(tabTag && !/\bhidden\b/.test(tabTag[0]), 'External tab button must NOT ship hidden (CE shows it)');

// 3) CE-OPEN activation leg
const extLeg = /else if \(panelId === 'main-panel-external'\) \{([\s\S]*?)\r?\n {4}\}/.exec(js);
assert.ok(extLeg, 'analytics.js must have a main-panel-external activation leg');
const legBody = extLeg[1];
assert.ok(/loadExternalPanel\(\)/.test(legBody), 'External leg must call loadExternalPanel()');
assert.ok(!/isPaidEdition/.test(legBody), 'External leg must NOT gate on isPaidEdition (CE feature)');
assert.ok(!/main-tab-external/.test(legBody), 'External leg must NOT hide #main-tab-external');
assert.ok(!/getElementById\('main-panel-external'\)/.test(legBody), 'External leg must NOT hide #main-panel-external');

// 4) loader contract
assert.ok(/apiCall\('\/system\/external'\)/.test(js), 'loadExternalPanel must GET /system/external');
assert.ok(/status === 403/.test(js), 'loader must stop polling on a 403');

// 5) manifest classification (section = lines between top-level keys) - SKIPPED when the
//    manifest is absent (CE extract); see the skip-if-missing guard at the read above.
function section(name) {
    const lines = manifest.split(/\r?\n/);
    const out = []; let on = false;
    for (const l of lines) {
        if (/^[a-z_]+:/.test(l)) { on = new RegExp('^' + name + ':').test(l); if (on) continue; if (!on && out.length) break; continue; }
        if (on) out.push(l);
    }
    return out.join('\n');
}
if (manifest) {
const allowSec = section('api_allow');
const denySec = section('deny');
assert.ok(allowSec.length > 100, 'manifest api_allow section not found');
assert.ok(denySec.length > 100, 'manifest deny section not found');
assert.ok(/-\s*CORE\/API\/routers\/system_external\.py/.test(allowSec),
    'api_allow must ship CORE/API/routers/system_external.py in CE');
assert.ok(/-\s*CORE\/API\/services\/external_load\.py/.test(allowSec),
    'api_allow must ship CORE/API/services/external_load.py in CE');
assert.ok(!/^\s*-\s*CORE\/API\/routers\/system_external\.py/m.test(denySec),
    'deny must NOT carry CORE/API/routers/system_external.py');
assert.ok(!/^\s*-\s*CORE\/API\/\*\*\/external_load\.py/m.test(denySec),
    'deny must NOT carry CORE/API/**/external_load.py');
}

// 6) main.py mount stays (optional_paid mounts it iff present)
assert.ok(/\("system_external", "router", ""\)/.test(mainpy), 'main.py must keep the system_external router mount');

// 7) CE-safe import closure
assert.ok(!/stripe|billing|credit|wizard_ml|managed_ai|noah/i.test(routerPy),
    'system_external.py must not import paid modules');
assert.ok(!/stripe|billing|credit|wizard_ml|managed_ai|noah/i.test(servicePy),
    'external_load.py must not import paid modules');

console.log('ce-external-subtab.check.js: all asserts passed');
