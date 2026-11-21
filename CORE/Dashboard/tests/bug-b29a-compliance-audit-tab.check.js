// BUG.B29a Part A — Audit log tab added to the Compliance hub.
//
// Verifies:
//   1. compliance-center.html has the audit-logs tab button + panel
//   2. hub-compliance.js has 'audit-logs' in TAB_KEYS
//   3. audit-logs is NOT in any CE carve set (visible on CE and paid)
//   4. hub-compliance.js imports and wires initializeAuditLogsPage
//   5. Both modules parse cleanly in ES module mode
//
//   node CORE/Dashboard/tests/bug-b29a-compliance-audit-tab.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const hubHtml = read('pages/compliance-center.html');
const hubJs   = read('js/hub-compliance.js');
const auditJs = read('js/audit-logs.js');

// --- 1) Tab button + panel exist in the markup -----------------------------
assert.ok(/id="main-tab-audit-logs"[^>]*\bhidden\b/.test(hubHtml),
    'compliance-center.html: main-tab-audit-logs button must exist and start hidden');
assert.ok(/main-panel-audit-logs/.test(hubHtml),
    'compliance-center.html: main-panel-audit-logs panel must exist');
assert.ok(/main-panel-audit-logs[^>]*\bhidden\b/.test(hubHtml),
    'compliance-center.html: main-panel-audit-logs must start hidden');
// Verify the tab button label text
assert.ok(/>[\s]*Audit log[\s]*</m.test(hubHtml),
    'compliance-center.html: tab button label must say "Audit log"');

// --- 2) TAB_KEYS includes 'audit-logs' --------------------------------------
const tabKeysMatch = hubJs.match(/const TAB_KEYS = \[([\s\S]*?)\];/);
assert.ok(tabKeysMatch, 'hub-compliance.js: TAB_KEYS literal must exist');
const keys = tabKeysMatch[1].match(/'([a-z-]+)'/g).map(s => s.slice(1, -1));
assert.ok(keys.includes('audit-logs'),
    'hub-compliance.js: TAB_KEYS must include \'audit-logs\'');

// --- 3) audit-logs is NOT in any CE carve set ------------------------------
const ceCarved = (hubJs.match(/const _CE_CARVED = new Set\(\[([^\]]*)\]\)/) || [])[1] || '';
const ceR397   = (hubJs.match(/const _CE_R397_TABS = new Set\(\[([^\]]*)\]\)/) || [])[1] || '';
const ceFx163  = (hubJs.match(/const _CE_FX163_TABS = new Set\(\[([^\]]*)\]\)/) || [])[1] || '';
const allCE = [...(ceCarved.match(/'([a-z-]+)'/g) || []),
               ...(ceR397.match(/'([a-z-]+)'/g) || []),
               ...(ceFx163.match(/'([a-z-]+)'/g) || [])].map(s => s.slice(1, -1));
assert.ok(!allCE.includes('audit-logs'),
    'audit-logs must NOT be in any CE carve set (visible on CE and paid)');

// --- 4) hub-compliance.js imports + wires initializeAuditLogsPage -----------
assert.ok(/import\s*\{[^}]*initializeAuditLogsPage[^}]*\}\s*from\s*['"]\.\/audit-logs\.js['"]/.test(hubJs),
    'hub-compliance.js must import initializeAuditLogsPage from audit-logs.js');
assert.ok(/'audit-logs':\s*\(\)\s*=>\s*initializeAuditLogsPage\(\)/.test(hubJs),
    "hub-compliance.js _CHILD_INIT must have 'audit-logs': () => initializeAuditLogsPage()");
assert.ok(/'audit-logs':\s*'tenant'.*,'audit-logs':\s*'tenant'/m.test(hubJs) ||
          /'audit-logs':\s*'tenant',/.test(hubJs),
    'hub-compliance.js _TAB_GATE must have \'audit-logs\': \'tenant\'');

// --- 5) audit-logs.js exports initializeAuditLogsPage -----------------------
assert.ok(/export\s+function\s+initializeAuditLogsPage\s*\(\s*\)/.test(auditJs),
    'audit-logs.js must export function initializeAuditLogsPage()');

// --- 6) audit-logs.js is import-safe: no top-level _wire(), uses fetchApi, no sessionStorage -------
// No top-level _wire() call — only called inside _wire() / initializeAuditLogsPage().
// Must NOT be at module top level (no leading whitespace on the line).
const topLevelCalls = auditJs.match(/^_wire\s*\(\s*\);/m);
assert.ok(!topLevelCalls,
    'audit-logs.js must NOT call _wire() at module top level (would break hub on import)');
assert.ok(/import\s*\{[^}]*\bfetchApi\b[^}]*\}\s*from\s*['"]\.\/api\.js['"]/.test(auditJs),
    'audit-logs.js must import fetchApi from ./api.js');
assert.ok(!/sessionStorage\.getItem/.test(auditJs),
    'audit-logs.js must not access sessionStorage directly (delegates to fetchApi)');
assert.ok(/async function apiCall/.test(auditJs),
    'audit-logs.js must still define apiCall() for callers');

// --- 7) Module-mode syntax check for both files ----------------------------
function checkModule(rel) {
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) return;
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
checkModule('js/hub-compliance.js');
checkModule('js/audit-logs.js');

console.log('bug-b29a-compliance-audit-tab: OK');
