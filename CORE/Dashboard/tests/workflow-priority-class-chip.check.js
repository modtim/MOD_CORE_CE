'use strict';
// UI-PRIORITY-CLASS-BADGE (lane r9700-1, 11-24) — the NB2 API pair
// PUT/GET /v1/road/{workflow_id}/priority-class (commit 699f0e1e39) had no
// UI surface (F-4 in NB2-WORKFLOW-PRIORITY-CLASS-SETTABLE.md). Prove the
// shipped Dashboard copy now carries: an api.js endpoint group mirroring the
// pair, a priority chip per run row in the Logs -> Workflow Runs table
// (one batched GET per unique workflow_id, failure-tolerant), and a set
// control whose help text says the classes order capacity-queued runs,
// route interactive tasks through the lighter placement, and do NOT make
// an ordinary run start faster.
//
//   node CORE/Dashboard/tests/workflow-priority-class-chip.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const apiJs = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');
const logsHtml = fs.readFileSync(path.join(ROOT, 'pages', 'logs.html'), 'utf8');

// --- 1) both scripts still parse as ES modules (Dashboard loads js/* as modules)
for (const [name, src] of [['js/api.js', apiJs], ['js/logs.js', logsJs]]) {
    let modErr = null;
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'],
            { input: src, encoding: 'utf8' });
    } catch (e) {
        modErr = String(e && (e.stderr || e.message));
    }
    assert.ok(!modErr, `${name}: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);
}

// --- 2) api.js exports the endpoint group mirroring the API pair -----------
assert.ok(/export const workflowPriority = \{/.test(apiJs),
    'api.js exports workflowPriority');
assert.ok(/get: \(workflowId\) => fetchApi\(`\/road\/\$\{encodeURIComponent\(workflowId\)\}\/priority-class`\)/
    .test(apiJs), 'workflowPriority.get hits GET /road/{id}/priority-class');
assert.ok(/set: \(workflowId, priorityClass\)/.test(apiJs)
    && /method: 'PUT', body: JSON\.stringify\(\{ priority_class: priorityClass \}\)/.test(apiJs),
    'workflowPriority.set PUTs {priority_class} to the same route');
assert.ok(/clear: \(workflowId\)/.test(apiJs)
    && /method: 'PUT', body: JSON\.stringify\(\{ clear: true \}\)/.test(apiJs),
    'workflowPriority.clear PUTs {clear: true} (the API reset form)');

// --- 3) logs.js imports the group and uses it (no string-URL duplication in the handler)
assert.ok(/import \{[^}]*workflowPriority[^}]*\} from '\.\/api\.js';/.test(logsJs),
    "logs.js imports workflowPriority from './api.js'");
assert.ok(/workflowPriority\.set\(wfId, cls\)/.test(logsJs)
    && /workflowPriority\.clear\(wfId\)/.test(logsJs),
    'the Apply handler calls set/clear through the group');

// --- 4) the batched per-page GET: unique workflow_ids, one request each, failure-tolerant
assert.ok(/new Set\(pageRuns\.map\(r => r\.workflow_id\)/.test(logsJs),
    'one lookup per unique workflow_id on the page');
assert.ok(/await apiCall\(`\/road\/\$\{id\}\/priority-class`\)/.test(logsJs),
    'the batch reads GET /road/{id}/priority-class');
assert.ok(/catch \(\w+\) \{ \/\* unknown: no chip \*\/ \}/.test(logsJs)
    && /catch \(\w+\) \{ \/\* whole batch failed: no chips \*\/ \}/.test(logsJs),
    'a failed lookup leaves the class unknown (no chip), never a wrong chip');

// --- 5) the chip renders next to the workflow name, class-coloured, unknown = none
assert.ok(/const pClass = priorityByWorkflow\[run\.workflow_id\] \|\| run\.priority_class;/.test(logsJs),
    'row class comes from the batched lookup (run payload field as fallback)');
assert.ok(/chipColors\[pClass\][\s\S]{0,200}rounded-full \$\{chipColors\[pClass\]\}">\$\{escapeHtml\(pClass\)\}/.test(logsJs),
    'the chip is a rounded pill coloured by class, class value escaped with escapeHtml (parity)');
assert.ok(/pClass && chipColors\[pClass\]/.test(logsJs),
    'unknown or unlisted class renders no chip (never an invented one)');

// --- 5b) REWORK (UI-PRIORITY-CLASS-BADGE-CLOSE F-1): 'normal' gets NO chip
// The API GET /road/{id}/priority-class returns "normal" as the DEFAULT for
// an unset workflow row, so a chip for normal would put a gray pill on EVERY
// run row.
// Spec: only interactive/high/low render a chip; normal (and missing/unknown)
// render none.
const chipBlockMatch = logsJs.match(/const chipColors = \{[\s\S]*?\};/);
assert.ok(chipBlockMatch, 'chipColors map present');
assert.ok(!/'normal'\s*:/.test(chipBlockMatch[0]),
    "'normal' is NOT in chipColors (no chip for the API default class)");
for (const cls of ['interactive', 'high', 'low']) {
    assert.ok(new RegExp(`'${cls}': 'bg-`).test(chipBlockMatch[0]),
        `chipColors lists class ${cls}`);
}
assert.ok(/'normal' is the API\s*\/\/ default/.test(logsJs) && /gets NO chip/.test(logsJs),
    "comment states 'normal' is the API default and gets no chip");

// --- 6) the set control exists in the page with the honest help text
assert.ok(/id="apply-priority-class"/.test(logsHtml), 'Apply button present');
assert.ok(/id="priority-class-workflow-id"/.test(logsHtml)
    && /id="priority-class-select"/.test(logsHtml),
    'workflow-ID input and class select present');
for (const opt of ['interactive', 'high', 'normal', 'low']) {
    assert.ok(new RegExp(`<option value="${opt}"`).test(logsHtml),
        `select offers class ${opt}`);
}
assert.ok(/<option value="">clear \(back to normal\)<\/option>/.test(logsHtml),
    'select offers the clear form');
assert.ok(/does NOT make an ordinary run\s*\n?\s*start faster/.test(logsHtml),
    'help text says the classes do NOT make an ordinary run start faster');
assert.ok(/orders runs that are held at the capacity limit/.test(logsHtml),
    'help text says the classes order capacity-queued runs');
assert.ok(/lighter\s*\n?\s*placement/.test(logsHtml),
    'help text mentions the lighter placement for interactive tasks');

// --- 7) the Apply listener guards a bad/empty ID before any fetch
assert.ok(/if \(!Number\.isFinite\(wfId\) \|\| wfId <= 0\) return;/.test(logsJs),
    'non-numeric or non-positive workflow ID never reaches the API');

console.log('workflow-priority-class-chip.check.js: 7 groups, all assertions green');

// --- CPU4-UI-REPRO 11-25: a 429 on /auth/refresh-token must not log the Dashboard out
assert.ok(/let _lastRefreshStatus = 0;/.test(apiJs), 'api.js tracks the last refresh status');
assert.ok(/_lastRefreshStatus = response\.status;/.test(apiJs), 'refresh failure records its status');
assert.ok(/if \(_lastRefreshStatus === 429\) \{[^]{0,300}throw new ApiError\([^]{0,160}429[^]{0,200}\}\s*\/\/ Refresh failed - log out/.test(apiJs),
    'a rate-limited (429) refresh throws a retry error BEFORE performLogout()');
console.log('ok: 429 refresh keeps the session');
