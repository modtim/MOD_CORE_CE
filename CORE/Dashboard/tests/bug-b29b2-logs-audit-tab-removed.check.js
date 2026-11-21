'use strict';
// BUG.B29b2 (owner 10-08, /#logs): the audit log moved to the Compliance hub's
// "Audit log" tab (pages/audit-logs.html, BUG.B29a — search + CSV/JSON export),
// so the Logs page's duplicate "Audit" tab had to go. Wanted: no Audit tab
// button and no permission-audit panel on the Logs page, a signpost line in the
// panel's old spot linking to #compliance-center?tab=audit-logs, the other tabs
// (Workflow Runs / System Tasks / Log Streaming) untouched, logs.js free of the
// audit-only code, and an old ?tab=permissions deep link falling back to the
// default tab.
//
//   node CORE/Dashboard/tests/bug-b29b2-logs-audit-tab-removed.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const logsHtml = fs.readFileSync(path.join(ROOT, 'pages', 'logs.html'), 'utf8');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');

// --- 1) logs.js still parses as an ES module (Dashboard loads js/* as modules)
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: logsJs, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the Audit tab button and its panel are gone -------------------------
assert.ok(!logsHtml.includes('logs-tab-permissions'),
    'pages/logs.html: the Audit tab button (id="logs-tab-permissions") is still there');
assert.ok(!logsHtml.includes('logs-panel-permissions'),
    'pages/logs.html: the permission-audit panel (id="logs-panel-permissions") is still there');
for (const id of ['permission-audit-tbody', 'permission-results-info',
                  'prev-permission-page', 'next-permission-page',
                  'filter-permission-action', 'filter-permission-date',
                  'apply-permission-filters', 'reset-permission-filters',
                  'download-audit-csv', 'download-audit-json', 'audit-export-status']) {
    assert.ok(!logsHtml.includes(`id="${id}"`),
        `pages/logs.html: leftover audit-tab element id="${id}"`);
}

// --- 3) the moved-notice signpost, in the panel's old spot ------------------
// Edition-aware (FIX-CE-DASH-SWEEP-B): the CE cut denies the Compliance hub, so
// manifest scrub rows rewrite this signpost (row 4075: the link goes to
// #audit-logs and reads "Audit log"; row 4076: "The audit log is on its own page:" ->
// "The audit log is on its own page:"). The paid tree keeps the original
// wording, so detect the edition by a paid-only file and assert per edition.
const paidTree = fs.existsSync(path.join(ROOT, 'js', 'seller.js'));
const notice = paidTree
    ? /<a href="#compliance-center\?tab=audit-logs"[^>]*>Compliance[^<]*Audit log<\/a>/.exec(logsHtml)
    : /<a href="#audit-logs"[^>]*>Audit log<\/a>/.exec(logsHtml);
assert.ok(notice, paidTree
    ? 'pages/logs.html: no link to #compliance-center?tab=audit-logs'
    : 'pages/logs.html: no link to #audit-logs');
const noticePhrase = paidTree ? 'The audit log is on its own page:' : 'The audit log is on its own page:';
assert.ok(logsHtml.includes(noticePhrase),
    `pages/logs.html: missing the "${noticePhrase}" line`);
// It must sit inside the tab-panel container, between the runs panel and the
// System Tasks panel (where the old panel lived).
const panelStart = logsHtml.indexOf('id="logs-panel-workflows"');
const sysStart = logsHtml.indexOf('id="logs-panel-system"');
const noticeAt = logsHtml.indexOf(noticePhrase);
assert.ok(panelStart >= 0 && sysStart > panelStart && noticeAt > panelStart && noticeAt < sysStart,
    'the moved-notice line must sit in the tab-panel area, after the Workflow runs panel');

// --- 4) the surviving tabs are untouched ------------------------------------
for (const id of ['logs-tab-workflows', 'logs-tab-system', 'logs-tab-log-streams',
                  'logs-panel-workflows', 'logs-panel-system', 'logs-panel-log-streams']) {
    assert.ok(logsHtml.includes(`id="${id}"`), `pages/logs.html: tab/panel id="${id}" was lost`);
}

// --- 5) logs.js no longer carries audit-only code ---------------------------
for (const name of ['loadPermissionAudit', 'applyPermissionFilters', 'resetPermissionFilters',
                    'downloadAuditExport', '_auditSetStatus', '_auditRowsToRecords',
                    '_recordsToCsv', '_downloadBlob', '_csvCell',
                    'permissionPage', 'permissionPageSize', 'permissionFilters']) {
    assert.ok(!logsJs.includes(name), `js/logs.js: leftover audit-tab code referencing ${name}`);
}
// Nothing in logs.js may still reach for a removed element id.
for (const id of ['permission-audit-tbody', 'permission-results-info',
                  'prev-permission-page', 'next-permission-page',
                  'filter-permission-action', 'filter-permission-date',
                  'apply-permission-filters', 'reset-permission-filters',
                  'download-audit-csv', 'download-audit-json', 'audit-export-status',
                  'logs-panel-permissions', 'logs-tab-permissions']) {
    assert.ok(!logsJs.includes(id), `js/logs.js: still references removed element id "${id}"`);
}

// --- 6) a ?tab=permissions deep link falls back to the default tab ----------
const restore = /const wantedTabEl[\s\S]{0,400}?wantedTabEl\.click\(\)/;
assert.ok(restore.test(logsJs), 'js/logs.js: sub-tab restore block not found');
assert.ok(/wantedTab === 'permissions'/.test(logsJs),
    'js/logs.js: ?tab=permissions must be neutralised so it falls back to Workflow Runs');
assert.ok(/loadWorkflowRuns\(\)/.test(logsJs), 'js/logs.js: default-tab load (loadWorkflowRuns) lost');

// --- 7) null-guards where a panel can be missing ----------------------------
assert.ok(/const panel = document\.getElementById\(panelId\);\s*\n\s*if \(panel\)/.test(logsJs),
    'js/logs.js: panel lookup after tab click must be null-guarded');
assert.ok(logsJs.includes('if (!activePanel) return;'),
    'js/logs.js: refresh handler must null-guard the active panel');

console.log('BUG.B29b2 OK: Logs page Audit tab removed, signpost link present, logs.js clean.');
