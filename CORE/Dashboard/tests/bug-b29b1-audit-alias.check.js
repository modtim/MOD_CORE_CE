// BUG.B29b1 Part B — old audit-log routes redirect to the Compliance hub tab.
//
// The audit log moved from its own page (#audit-logs) and from the Logs page's
// "Audit" tab (#logs?tab=permissions) to the Compliance hub's "Audit log" tab
// (page id compliance-center, tab key audit-logs, wired in hub-compliance.js).
// ui.js must route both old bookmarks there through its EXISTING mechanisms:
//   1. pageFileMap alias 'audit-logs' -> 'compliance-center'
//   2. a dispatch branch landing #audit-logs on the hub tab via _loadComplianceHubTab
//   3. #logs?tab=permissions rewritten to hash compliance-center + tab=audit-logs
//   4. no sidebar row added for audit-logs (owner rule: hub TABS only)
//   5. ui.js still parses as an ES module
//
//   node CORE/Dashboard/tests/bug-b29b1-audit-alias.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const ui = fs.readFileSync(path.join(ROOT, 'js', 'ui.js'), 'utf8');
const shell = fs.readFileSync(path.join(ROOT, 'pages', 'dashboard-shell.html'), 'utf8');

// --- 1) pageFileMap alias: #audit-logs resolves to the hub page ------------
assert.ok(/'audit-logs':\s*'compliance-center',/.test(ui),
    "ui.js pageFileMap must alias 'audit-logs' -> 'compliance-center'");

// --- 2) dispatch branch lands #audit-logs on the hub's audit-logs tab ------
assert.ok(/targetPageId === 'audit-logs'/.test(ui),
    "ui.js dispatch must have a targetPageId === 'audit-logs' branch");
assert.ok(/_loadComplianceHubTab\('audit-logs'\)/.test(ui),
    "ui.js must land #audit-logs on the hub tab via _loadComplianceHubTab('audit-logs')");

// --- 3) #logs?tab=permissions redirects to the hub + audit-logs tab --------
assert.ok(/const _requestedTab = getActiveTab\(\)/.test(ui),
    'ui.js must resolve the requested sub-tab from getActiveTab() (query form)');
assert.ok(/new URLSearchParams\(targetHash\.substring\(_qIdx \+ 1\)\)\.get\('tab'\)/.test(ui),
    "ui.js must also read the tab off the hash-carried query (#logs?tab=permissions)");
assert.ok(/targetPageId === 'logs' && _requestedTab === 'permissions'/.test(ui),
    "ui.js must branch on #logs?tab=permissions (targetPageId === 'logs' && _requestedTab === 'permissions')");
assert.ok(/import\s*\{[^}]*getActiveTab[^}]*\}\s*from\s*'\.\/tab-state\.js'/.test(ui),
    "ui.js must import getActiveTab from './tab-state.js'");
const logsBranch = ui.match(
    /targetPageId === 'logs' && _requestedTab === 'permissions'[\s\S]*?return;/);
assert.ok(logsBranch, 'ui.js #logs?tab=permissions branch body must exist');
const body = logsBranch[0];
assert.ok(/window\.location\.hash = 'compliance-center';/.test(body),
    'the #logs?tab=permissions branch must rewrite the hash to compliance-center');
assert.ok(/window\.location\.search = 'tab=audit-logs';/.test(body),
    "the #logs?tab=permissions branch must set search to 'tab=audit-logs'");
// The redirect must run BEFORE the plain logs branch, otherwise #logs?tab=permissions
// still opens the Logs page.
const redirectAt = ui.indexOf("targetPageId === 'logs' && _requestedTab === 'permissions'");
const plainAt = ui.indexOf("} else if (targetPageId === 'logs') {");
assert.ok(redirectAt < plainAt,
    'the #logs?tab=permissions redirect must precede the plain logs branch');

// --- 4) no sidebar row for audit-logs -------------------------------------
assert.ok(!/id="audit-logs-nav-link"/.test(shell),
    'audit-logs must NOT have its own sidebar row (hub tabs only, owner 10-08)');

// --- 5) ui.js parses in ES module mode ------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: ui, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/ui.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

console.log('bug-b29b1-audit-alias: OK');
