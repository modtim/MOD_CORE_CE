'use strict';
// BUG.B29c Part C (owner 10-08): the standalone Audit Logs page became the
// Compliance hub's "Audit log" tab, but it lost the CSV/JSON export the old
// Logs-page Audit tab had. This check guards the ported export:
//   * pages/audit-logs.html carries the status span + Download CSV / Download JSON
//     buttons, with the audit-page-* ids (the only export UI now that B29b2 removed
//     the Logs-page Audit tab),
//   * js/audit-logs.js wires both buttons through withButtonBusy,
//   * the export tries the server-side /audit-logs/export first and falls back to
//     client-side rows honouring THIS page's active filters + search (shared
//     buildFilterParams()),
//   * apiCall passes the path through to fetchApi unchanged (fetchApi's base URL
//     already carries /v1 — prefixing here produced /v1/v1/…),
//   * the module parses as an ES module.
//
//   node CORE/Dashboard/tests/bug-b29c-audit-page-export.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'pages', 'audit-logs.html'), 'utf8');
const js = fs.readFileSync(path.join(ROOT, 'js', 'audit-logs.js'), 'utf8');

// --- 1) module parses -------------------------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: js, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/audit-logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) markup: status span + both buttons, new ids ------------------------
assert.ok(/id="audit-page-export-status"/.test(html),
    'pages/audit-logs.html: no export status span');
const csvBtn = html.match(/<button[^>]*id="audit-page-download-csv"[^>]*>([\s\S]*?)<\/button>/);
const jsonBtn = html.match(/<button[^>]*id="audit-page-download-json"[^>]*>([\s\S]*?)<\/button>/);
assert.ok(csvBtn, 'pages/audit-logs.html: no Download CSV button');
assert.ok(jsonBtn, 'pages/audit-logs.html: no Download JSON button');
assert.ok(/Download CSV/.test(csvBtn[1]), 'the CSV button must read "Download CSV"');
assert.ok(/Download JSON/.test(jsonBtn[1]), 'the JSON button must read "Download JSON"');
assert.ok(/class="inline-flex items-center gap-1\.5/.test(csvBtn[0]),
    'the export buttons use the same classes as the old Logs-page Audit tab');
assert.ok(html.indexOf('id="audit-page-export-status"') > html.indexOf('id="reset-filters"'),
    'the export row sits in the filters card, next to Apply/Reset');

// B29b2 removed the Audit tab from logs.html, so the audit page is the only source
// of the export UI: its ids live in pages/audit-logs.html and are wired in js/audit-logs.js.
const logsHtml = fs.readFileSync(path.join(ROOT, 'pages', 'logs.html'), 'utf8');
assert.ok(!logsHtml.includes('download-audit-csv'),
    'logs.html must no longer contain download-audit-csv — the Audit tab was removed (B29b2)');
assert.ok(html.includes('id="audit-page-download-csv"') && html.includes('id="audit-page-download-json"'),
    'pages/audit-logs.html owns the export ids audit-page-download-csv / audit-page-download-json');
assert.ok(!html.includes('id="download-audit-csv"') && !html.includes('id="download-audit-json"'),
    'audit-logs.html must use NEW ids, not the retired logs.html Audit-tab ones');
assert.ok(/getElementById\('audit-page-download-csv'\)/.test(js) &&
    /getElementById\('audit-page-download-json'\)/.test(js),
    'js/audit-logs.js must wire both export ids from the source markup');

// --- 3) wiring: both buttons through withButtonBusy ------------------------
assert.ok(/import \{ withButtonBusy \} from '\.\/utils\.js'/.test(js),
    "js/audit-logs.js must import withButtonBusy from './utils.js'");
const wire = js.slice(js.indexOf('function _wire()'));
assert.ok(/getElementById\('audit-page-download-csv'\)/.test(wire),
    'the CSV button is not wired in _wire()');
assert.ok(/getElementById\('audit-page-download-json'\)/.test(wire),
    'the JSON button is not wired in _wire()');
assert.ok(/withButtonBusy\(e\.currentTarget, \(\) => downloadExport\('csv'\), \{ busyLabel: 'Exporting…' \}\)/.test(wire),
    'the CSV button must call downloadExport("csv") through withButtonBusy with busyLabel Exporting…');
assert.ok(/withButtonBusy\(e\.currentTarget, \(\) => downloadExport\('json'\), \{ busyLabel: 'Exporting…' \}\)/.test(wire),
    'the JSON button must call downloadExport("json") through withButtonBusy with busyLabel Exporting…');
assert.ok(/if \(csvBtn\)/.test(wire) && /if \(jsonBtn\)/.test(wire),
    'the export buttons are null-guarded so the hub tab renders without them');

// --- 4) export: server first, client-side fallback honouring the filters ---
// Slice the whole function body (nested braces make a non-greedy regex stop early).
const fnStart = js.indexOf('async function downloadExport(format)');
assert.ok(fnStart >= 0, 'js/audit-logs.js: downloadExport(format) missing');
const fn = js.slice(fnStart, js.indexOf('function _wire()'));
assert.ok(/apiCall\('\/audit-logs\/export'\)/.test(fn),
    'the export must call /audit-logs/export first');
assert.ok(/catch \(err\)/.test(fn), 'the server export must be try/caught');
assert.ok(/buildFilterParams\(10000, 0\)/.test(fn),
    'the fallback must reuse this page\'s filter/search builder, not duplicate it');
assert.ok(/apiCall\(`\/audit-logs\/permissions\?\$\{params\.toString\(\)\}`\)/.test(fn),
    'the fallback must read the permission-audit rows this page can already read');
assert.ok(/_setExportStatus\('Preparing export…'\)/.test(fn),
    'the status span is updated while preparing');
assert.ok(/Export failed — could not load audit logs\./.test(fn),
    'a failed export reports on the status span');
assert.ok(/No audit records to export for the current filters\./.test(fn),
    'an empty result reports on the status span');
assert.ok(/Exported \$\{records\.length\} record/.test(fn),
    'a successful export reports the record count on the status span');

// The shared builder is what both the page load and the fallback use.
const builder = js.match(/function buildFilterParams\([^)]*\)\s*\{([\s\S]*?)\n\s*\}/);
assert.ok(builder, 'js/audit-logs.js: buildFilterParams() helper missing');
assert.ok(/params\.append\('q',\s*searchValue\)/.test(builder[1]),
    'buildFilterParams() must include the search bar');
assert.ok(/getSearchValue\(\)/.test(builder[1]), 'buildFilterParams() reads the search bar');
assert.ok(/const params = buildFilterParams\(pageSize, currentPage \* pageSize\)/.test(js),
    'loadAuditLogs() must use the shared builder too');

// Status span id used by the JS matches the markup.
assert.ok(/getElementById\('audit-page-export-status'\)/.test(js),
    'the status setter must read #audit-page-export-status');

// CSV + JSON paths exist.
assert.ok(/_downloadBlob\(_recordsToCsv\(records\), `audit-log-\$\{stamp\}\.csv`/.test(fn),
    'the CSV path must download a blob');
assert.ok(/_downloadBlob\(JSON\.stringify\(exportData, null, 2\), `audit-log-\$\{stamp\}\.json`/.test(fn),
    'the JSON path must download a blob');
assert.ok(/function _csvCell/.test(js) && /function _recordsToCsv/.test(js),
    'CSV helpers missing');
assert.ok(/function _rowsToRecords/.test(js), 'record-flattening helper missing');

// --- 5) apiCall must not double-prefix /v1 ---------------------------------
const apiCall = js.match(/async function apiCall\([\s\S]*?\n\s*\}/);
assert.ok(apiCall, 'js/audit-logs.js: apiCall() missing');
assert.ok(/fetchApi\(path/.test(apiCall[0]), 'apiCall must pass the path straight to fetchApi');
assert.ok(!/\/v1/.test(apiCall[0]),
    'apiCall must not prepend /v1 — fetchApi\'s API_BASE_URL already includes it');

console.log('bug-b29c-audit-page-export: OK');
