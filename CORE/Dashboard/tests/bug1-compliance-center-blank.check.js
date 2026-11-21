// BUG.1 (owner 10-08): /#compliance-center rendered BLANK for the owner (superadmin)
// after the R39.7 hub regroup shipped. Root cause: js/hub-compliance.js declared
// `const _CE_CARVED` TWICE (a duplicated comment block + declaration landed when the
// FX.163 p3b2b 'data-export' tab was folded in). `node --check` parses the file in
// SCRIPT mode, where a repeated top-level `const` is legal, so the build passed; the
// Dashboard loads these files as ES MODULES (they use `import`/`export`), where a
// repeated top-level const is a SyntaxError. The module never evaluates,
// initializeComplianceHubPage never runs, every main-tab-* button keeps its
// `hidden` class from pages/compliance-center.html and no main-panel-* is ever
// un-hidden -> the page is blank with no console app error.
//
// This check therefore runs the MODULE-mode syntax check (node --input-type=module
// --check, file on stdin) over every hub/js file touched by the R39.7 regroup, and
// pins the BUG.1 invariant: for a superadmin / tenant admin, hub-compliance.js
// always reveals a default tab (its 'tenant' gate tabs survive every CE carve, so
// _visibleTabs() is never empty and switchToTab() un-hides a panel).
//
//   node CORE/Dashboard/tests/bug1-compliance-center-blank.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

// --- 1) module-mode syntax: the class of bug that shipped -------------------
// Files the R39.7 hub regroup touched (plus the hubs it sits beside). Missing
// files are skipped so the check stays honest on a trimmed tree.
const MODULE_CHECK_FILES = [
    'js/hub-compliance.js',
    'js/security-hub.js',
    'js/users.js',
    'js/logs.js',
    'js/marketplace.js',
    'js/analytics.js',
    'js/help.js',
    'js/settings.js',
    'js/ui.js',
];

for (const rel of MODULE_CHECK_FILES) {
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) continue;
    let err = null;
    try {
        execFileSync(process.execPath,
            ['--input-type=module', '--check'],
            { input: fs.readFileSync(abs, 'utf8'), encoding: 'utf8' });
    } catch (e) {
        err = String(e && (e.stderr || e.message));
    }
    assert.ok(!err, `${rel}: not valid as an ES module — ${(err || '').split('\n')[0] || 'unknown'}`);
}

// --- 2) BUG.1 invariant: a default tab is always revealed -------------------
const compJs = read('js/hub-compliance.js');
const compHtml = read('pages/compliance-center.html');

const tabKeys = (compJs.match(/const TAB_KEYS = \[([\s\S]*?)\];/) || [])[1];
assert.ok(tabKeys, 'hub-compliance.js: TAB_KEYS literal not found');
const keys = tabKeys.match(/'([a-z-]+)'/g).map((s) => s.slice(1, -1));
assert.ok(keys.length >= 7, `hub-compliance.js: TAB_KEYS must carry the R39.7 hub tabs (got ${keys.length})`);

// exactly one declaration of every top-level const/let/set the hub relies on —
// the duplicate that made the module fail to load.
for (const [name, kw] of [['TAB_KEYS', 'const|let'], ['_CE_CARVED', 'const'],
                          ['_CE_R397_TABS', 'const'], ['_CE_FX163_TABS', 'const'],
                          ['_CHILD_INIT', 'const'], ['_TAB_GATE', 'const'],
                          ['_visibleTabs', 'function'], ['_tabAllowed', 'function']]) {
    const decls = compJs.match(new RegExp('^(?:' + kw + ')\\s+' + name + '\\b', 'gm')) || [];
    assert.strictEqual(decls.length, 1,
        `hub-compliance.js: '${name}' declared ${decls.length} times (a duplicate is a module-mode SyntaxError -> blank page)`);
}

// every tab button + panel exists in the page, and starts hidden (the hub JS,
// not the markup, decides visibility)
for (const k of keys) {
    assert.ok(new RegExp(`id="main-tab-${k}"[^>]*\\bhidden\\b`).test(compHtml),
        `pages/compliance-center.html: main-tab-${k} must exist and start hidden`);
    assert.ok(new RegExp(`id="main-panel-${k}" class="main-panel hidden"`).test(compHtml),
        `pages/compliance-center.html: main-panel-${k} must exist and start hidden`);
}

// the tenant gate (superadmin OR platform admin) must survive every CE carve, so
// a superadmin / tenant admin always has at least one visible tab -> switchToTab
// never early-returns and always un-hides a panel.
const ceSets = ['_CE_CARVED', '_CE_R397_TABS', '_CE_FX163_TABS'].map((n) => {
    const body = (compJs.match(new RegExp(`const ${n} = new Set\\(\\[([^\\]]*)\\]\\)`)) || [])[1];
    assert.ok(body !== undefined, `hub-compliance.js: ${n} literal missing`);
    return (body.match(/'([a-z-]+)'/g) || []).map((s) => s.slice(1, -1));
});
const gateBlock = compJs.match(/const _TAB_GATE = \{([\s\S]*?)\n\};/);
assert.ok(gateBlock, 'hub-compliance.js: _TAB_GATE literal not found');
const tenantTabs = [...gateBlock[1].matchAll(/'([a-z-]+)':\s*'tenant'/g)].map((m) => m[1]);
assert.ok(tenantTabs.length >= 1, 'hub-compliance.js: no tenant-gated tab -> a superadmin could see nothing');
const carved = new Set(ceSets.flat());
const alwaysVisible = tenantTabs.filter((k) => !carved.has(k));
assert.ok(alwaysVisible.length >= 1,
    'BUG.1: every tenant-gated tab is CE-carved -> superadmin/tenant-admin gets an empty tab strip (blank page)');

// the reveal path itself: default tab selection + panel un-hide must be present
assert.ok(/switchToTab\(visible\.includes\(wanted\) \? wanted : visible\[0\]\)/.test(compJs),
    'hub-compliance.js: init must fall back to the first visible tab when ?tab= is missing');
assert.ok(/document\.querySelectorAll\('\.main-panel'\)\.forEach\(p => p\.classList\.add\('hidden'\)\)/.test(compJs),
    'hub-compliance.js: panels must be hidden then the active one un-hidden');
assert.ok(/panel\?\.classList\.remove\('hidden'\)/.test(compJs),
    'hub-compliance.js: the active panel must be un-hidden');

// ui.js must still reach the hub with no tab requested (/#compliance-center)
const ui = read('js/ui.js');
assert.ok(/targetPageId === 'compliance-center'/.test(ui),
    'ui.js: the compliance-center dispatch must exist');
assert.ok(/_loadComplianceHubTab\(null\)/.test(ui),
    "ui.js: /#compliance-center with no ?tab= must call _loadComplianceHubTab(null) (no early return)");

console.log('bug1-compliance-center-blank: ' +
    `${MODULE_CHECK_FILES.length} module-mode syntax checks, ${keys.length} hub tabs, ` +
    `superadmin default tab always revealed -> OK`);
