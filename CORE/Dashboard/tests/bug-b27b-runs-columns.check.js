'use strict';
// BUG.B27b (owner 10-08, /#logs): "Logs -> Workflow runs: a username alone
// can't identify who ran a run". With hundreds of users, the same handle can
// exist in several tenants and the owner compares rows across the whole
// tenant, so each runs row must show Tenant ID, User ID, username (plus the
// tenant name when the API carries it) and an "Ended" time next to "Started"
// ("—" when the run never finished, "running" while it is still going; the
// existing Duration column stays).
//
//   node CORE/Dashboard/tests/bug-b27b-runs-columns.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const logsHtml = fs.readFileSync(path.join(ROOT, 'pages', 'logs.html'), 'utf8');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');

// --- 1) logs.js still parses as an ES module ---------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: logsJs, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the runs-table header carries the new columns, in order -------------
const tableStart = logsHtml.indexOf('<!-- Workflow Runs Table -->');
assert.ok(tableStart >= 0, 'pages/logs.html: Workflow runs table marker missing');
const runsTable = logsHtml.slice(tableStart, logsHtml.indexOf('</table>', tableStart));
const headers = [...runsTable.matchAll(/<th\b[^>]*>([^<]*)<\/th>/g)]
    .map(m => m[1].trim());
assert.ok(headers.length === 9, `Workflow runs table must have 9 columns, got ${headers.length}`);
assert.ok(headers.join('|') === 'Workflow|Tenant|User|Status|Started|Ended|Duration|Outcome|Actions',
    `Workflow runs headers must be Workflow|Tenant|User|Status|Started|Ended|Duration|Outcome|Actions, got ${headers.join('|')}`);
// "Ended" must sit immediately right of "Started" (the owner asked for it next
// to Started) and Duration must still be there.
const startedIdx = headers.indexOf('Started');
assert.ok(headers[startedIdx + 1] === 'Ended', '"Ended" must be the column right after "Started"');
assert.ok(headers.includes('Duration'), 'the existing "Duration" column must stay');

// --- 3) placeholder rows span the widened table -----------------------------
const loadingRow = /<td colspan="(\d+)" class="px-4 py-8 text-center text-gray-400">Loading workflow runs\.\.\.<\/td>/.exec(logsHtml);
assert.ok(loadingRow && loadingRow[1] === '9', 'static loading row must span 9 columns');
for (const jsCol of [...logsJs.matchAll(/colspan="(\d+)" class="py-(?:4|8) text-center/g)]) {
    assert.ok(jsCol[1] === '9' || jsCol[1] === '7',
        `js/logs.js: a runs/system placeholder row spans ${jsCol[1]} columns (runs table is 9)`);
}

// --- 4) tailwind classes the new cells use are actually shipped -------------
const tw = fs.readFileSync(path.join(ROOT, 'tailwind.css'), 'utf8');
for (const cls of ['py-3', 'px-4', 'text-sm', 'text-xs', 'text-gray-500', 'text-gray-600']) {
    assert.ok(tw.includes(`.${cls} {`), `tailwind.css: '.${cls}' missing (purge list) -> new cells unstyled`);
}

// --- 5) logs.js renders tenant + user identity per row ----------------------
const tenantDef = /const tenantLabel = run\.tenant_name\s*\|\|\s*\(run\.tenant_id != null \? `Tenant #\$\{run\.tenant_id\}` : ''\);/.exec(logsJs);
assert.ok(tenantDef, 'js/logs.js: tenantLabel must fall back to "Tenant #<id>" when no name is served');
const userDef = /const userLabel = run\.triggered_by_username\s*\|\|\s*\(run\.triggered_by_user_id != null \? `User #\$\{run\.triggered_by_user_id\}` : actor\);/.exec(logsJs);
assert.ok(userDef, 'js/logs.js: userLabel must fall back to "User #<id>", then to the human actor');
assert.ok(/run\.triggered_by_username/.test(logsJs), 'js/logs.js must read the new triggered_by_username field');
assert.ok(/run\.tenant_name/.test(logsJs), 'js/logs.js must read the new tenant_name field');

// --- 6) the row emits Tenant / User / Ended cells ---------------------------
const rowTpl = /row\.innerHTML = `([\s\S]*?)`;\s*\n\s*\}\s*\n\s*tbody\.appendChild\(row\);/.exec(logsJs);
assert.ok(rowTpl, 'js/logs.js: runs row template not found');
const tpl = rowTpl[1];
const cellCount = (tpl.match(/<td\b/g) || []).length;
assert.ok(cellCount === 9, `runs row must emit 9 cells, got ${cellCount}`);
assert.ok(/<div class="text-sm">\$\{escapeHtml\(tenantLabel\)\}<\/div>/.test(tpl),
    'Tenant cell must render the tenant name (escaped)');
assert.ok(/<div class="text-xs text-gray-500">T\$\{run\.tenant_id/.test(tpl),
    'Tenant cell must also show the Tenant ID');
assert.ok(/<div class="text-sm">\$\{escapeHtml\(userLabel\)\}<\/div>/.test(tpl),
    'User cell must render the username (escaped)');
assert.ok(/<div class="text-xs text-gray-500">U\$\{run\.triggered_by_user_id/.test(tpl),
    'User cell must also show the User ID');
assert.ok(/\$\{endedAt\}/.test(tpl), 'the Ended cell must render endedAt');
const endedIdx = tpl.indexOf('${endedAt}');
const startedIdxTpl = tpl.indexOf('${formatTimestamp(run.started_at)}');
assert.ok(startedIdxTpl >= 0 && endedIdx > startedIdxTpl,
    'the Ended cell must come right after the Started cell');
assert.ok(tpl.includes('${duration}'), 'the Duration cell must stay');

// --- 7) Ended honesty: never invent a finish time ---------------------------
const endedCalc = /const endedAt = run\.completed_at\s*\n?\s*\? formatTimestamp\(run\.completed_at\)\s*\n?\s*: \(run\.status === 'running' \? 'running' : '—'\);/.exec(logsJs);
assert.ok(endedCalc, 'js/logs.js: endedAt must be completed_at, else "running" while running, else "—"');
assert.ok(!/const endedAt = formatTimestamp/.test(logsJs),
    'endedAt must not be derived from started_at (a queued/failed run has not ended)');

console.log('bug-b27b-runs-columns: OK');
