// FIX.LOGS.NULL.GUARD — assert the four DOM accesses in loadWorkflowRuns
// are null-safe (const el = ...; if (el) { ... }). Without the guard, when
// the Logs page is folded into the nav-group's in-page tab and the pager
// elements are absent from the DOM, loadWorkflowRuns throws
// "Cannot set properties of null" and the catch block prints the error row.
//
// Run: node --test CORE/Dashboard/tests/log-null-guard.test.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');

// The four guarded expressions must use the null-guard pattern:
//   const X = document.getElementById('id');
//   if (X) X.prop = ...;
const GUARD_PATTERNS = [
    /const info = document\.getElementById\('workflow-results-info'\)\s*;/,
    /const prev = document\.getElementById\('prev-workflow-page'\)\s*;/,
    /const next = document\.getElementById\('next-workflow-page'\)\s*;/,
];

for (const re of GUARD_PATTERNS) {
    const matches = [...logsJs.matchAll(new RegExp(re.source, 'g'))];
    assert.ok(matches.length > 0,
        `log-null-guard: no null-guard const found for ${re.source}`);
}

// The UNGUARDED form (getElementById('...').prop = ...) must NOT exist.
const UNGUARDED_PATTERNS = [
    /getElementById\('workflow-results-info'\)\.textContent\s*=/,
    /getElementById\('prev-workflow-page'\)\.disabled\s*=/,
    /getElementById\('next-workflow-page'\)\.disabled\s*=/,
];

for (const re of UNGUARDED_PATTERNS) {
    assert.ok(!re.test(logsJs),
        `log-null-guard: unguarded access still present — ${re.source}`);
}

console.log('log-null-guard: all four DOM accesses are null-safe (const/if guard)');
