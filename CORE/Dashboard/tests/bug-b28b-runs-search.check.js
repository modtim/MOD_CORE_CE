'use strict';
// BUG.B28b (owner 10-08, /#logs): "Logs -> Workflow runs: no search".
// The Workflow runs panel had status/date/page-size filters but no free-text
// search, so finding one run by name / actor / status / run # meant paging
// through everything. Wanted: a #runs-search input above the runs table that
// feeds the `q` param on GET /road/runs, Enter applies at once, typing
// debounces 400 ms, changing the search returns to the first page, clearing
// the box reloads unfiltered. Also renames the runs-table header "Progress"
// to "Outcome" (that column shows the outcome summary, not a percentage).
//
//   node CORE/Dashboard/tests/bug-b28b-runs-search.check.js
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

// --- 2) the search input exists in the Workflow runs panel ------------------
const input = /<input[^>]*id="runs-search"[^>]*\/>/.exec(logsHtml);
assert.ok(input, 'pages/logs.html: no id="runs-search" input in the Workflow runs panel');
const inputTag = input[0];
for (const attr of ['type="search"',
                    'placeholder="Search workflow, user, status or run #…"',
                    'aria-label="Search workflow runs"',
                    'id="runs-search"']) {
    assert.ok(inputTag.includes(attr), `runs-search input: missing ${attr}`);
}
const tw = fs.readFileSync(path.join(ROOT, 'tailwind.css'), 'utf8');
for (const cls of ['w-full', 'border-gray-300', 'rounded-lg', 'px-3', 'py-2', 'mb-4']) {
    assert.ok(tw.includes(`.${cls} {`), `tailwind.css: '.${cls}' missing (purge list) -> input unstyled`);
}
// The input must sit in the Workflow runs panel, before the runs table.
const panelStart = logsHtml.indexOf('id="logs-panel-workflows"');
const tableStart = logsHtml.indexOf('<!-- Workflow Runs Table -->');
assert.ok(panelStart >= 0 && tableStart > panelStart && logsHtml.indexOf('id="runs-search"') > panelStart
    && logsHtml.indexOf('id="runs-search"') < tableStart,
    'runs-search input must live inside the Workflow runs panel, above the runs table');

// --- 3) the runs-table header says Outcome, not Progress --------------------
const runsTable = logsHtml.slice(tableStart, logsHtml.indexOf('</table>', tableStart));
assert.ok(/>Outcome<\/th>/.test(runsTable), 'Workflow runs table header must read "Outcome"');
assert.ok(!/>Progress<\/th>/.test(runsTable), 'Workflow runs table header still says "Progress"');

// --- 4) logs.js reads #runs-search and appends q to the runs request --------
const reader = logsJs.match(/function getRunsSearchValue\(\)\s*\{([\s\S]*?)\n\}/);
assert.ok(reader, 'js/logs.js: getRunsSearchValue() helper not found');
assert.ok(/getElementById\('runs-search'\)/.test(reader[1]),
    'getRunsSearchValue() must read the #runs-search element');
assert.ok(/return el \? String\(el\.value \|\| ''\)\.trim\(\) : ''/.test(reader[1]),
    'getRunsSearchValue() must null-guard the element (page can unmount mid-load, BUG.4) and trim');

const runsReq = logsJs.match(/const params = new URLSearchParams\(\{[\s\S]*?limit: workflowPageSize \+ 1,[\s\S]*?\}\);([\s\S]*?)runs = await apiCall\(`\/road\/runs\?\$\{params\.toString\(\)\}`/);
assert.ok(runsReq, 'js/logs.js: server-paginated /road/runs request not found');
assert.ok(/const runsSearch = getRunsSearchValue\(\);/.test(runsReq[1]),
    'the runs request must read the search box');
assert.ok(/if \(runsSearch\) \{\s*params\.set\('q', runsSearch\);\s*\}/.test(runsReq[1]),
    "the runs request must append 'q' only when the search box is non-empty (cleared box = unfiltered)");

// --- 5) Enter / debounce / first-page reset wiring --------------------------
const wire = logsJs.match(/const runsSearchInput = document\.getElementById\('runs-search'\);\s*if \(runsSearchInput\) \{([\s\S]*?)\n    \}\n/);
assert.ok(wire, "js/logs.js: no #runs-search listener wiring block (if (runsSearchInput) null-guard)");
const body = wire[1];
assert.ok(/let lastAppliedRunsSearch = null;/.test(body), 'wiring must track the last applied search');
assert.ok(/let runsSearchDebounceTimer = null;/.test(body), 'wiring must own a debounce timer slot');
assert.ok(/const applyRunsSearch = \(\) => \{/.test(body), 'wiring must define applyRunsSearch()');
assert.ok(/workflowPage = 0;\s*loadWorkflowRuns\(\);/.test(body),
    'changing the search must reset to the first page before reloading');
assert.ok(/const scheduleRunsSearchApply = \(\) => \{/.test(body), 'wiring must define scheduleRunsSearchApply()');
assert.ok(/runsSearchDebounceTimer = setTimeout\(\(\) => \{[\s\S]*?\}, 400\);/.test(body),
    'typing must debounce 400 ms before applying');
assert.ok(/clearTimeout\(runsSearchDebounceTimer\)/.test(body),
    'the debounce must be cancellable (clearTimeout) so Enter applies at once');
assert.ok(/runsSearchInput\.addEventListener\('keydown', \(e\) => \{\s*if \(e\.key === 'Enter'\)/.test(body),
    "Enter in the box must be handled on 'keydown'");
assert.ok(/} else if \(!e\.key \|\| e\.key\.length === 1\) \{\s*scheduleRunsSearchApply\(\);/.test(body),
    'ordinary keystrokes must re-arm the debounce');
assert.ok(/runsSearchInput\.addEventListener\('input', scheduleRunsSearchApply\)/.test(body),
    "the 'input' event must re-arm the debounce (backspace/clear)");
assert.ok(/runsSearchInput\.addEventListener\('paste', scheduleRunsSearchApply\)/.test(body),
    "the 'paste' event must re-arm the debounce");

// --- 6) the background poll stays unfiltered-by-page but honours the search -
// checkAndRefreshWorkflowRuns() probes limit=500&offset=0 with no filters; it
// must not be turned into a filtered request (it only fingerprints runs).
assert.ok(/apiCall\(`\/road\/runs\?limit=500&offset=0`\)/.test(logsJs),
    'js/logs.js: the smart-refresh fingerprint probe must stay as it was');

console.log('bug-b28b-runs-search: OK');
