'use strict';
// B89 — the Dashboard topology graph's "Graph library failed to load:
// .../vendor/cytoscape.esm.min.js" error stuck forever after a one-off
// failed dynamic import: the browser caches the failed module record in its
// module map, so the page stays broken until a hard refresh. Fix:
// js/analytics.js's _loadCytoscape() now retries the import up to 2 times
// with backoff (~500ms, 1500ms), using a cache-busting `?r=<attempt>` query
// on retries so the browser can't reuse the failed record; if all attempts
// fail, the existing error renders with a "Retry" button that re-runs the
// loader (no full page reload).
//
// There is no runnable JS test harness for the Dashboard (package.json ships
// only tailwindcss — no jsdom/mocha), so this follows the existing
// *.check.js style: static assertions + a node:vm harness (same pattern as
// assistant-panel-progress.check.js) that evaluates the real
// _importWithRetry helper against a stubbed loader + stubbed setTimeout.
//
//   node CORE/Dashboard/tests/bug-b89-topology-graph-retry.check.js
//
// Checks:
//   1. _importWithRetry / _loadCytoscape / Retry button exist in analytics.js
//   2. cache-busting `?r=` query used on retried imports
//   3. no console.log in the touched region
//   4. no full page reload on Retry (no location.reload)
//   5. behaviour: loadFn fails twice then succeeds -> resolves, 2 delays used
//   6. behaviour: loadFn fails 3x -> rejects with the last error, no 3rd delay

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const ANALYTICS_REL = 'js/analytics.js';
const analyticsSrc = fs.readFileSync(path.join(ROOT, ANALYTICS_REL), 'utf8');

// --- 0) ES module syntax check --------------------------------------------

let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: analyticsSrc, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr,
    `analytics.js is not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 1-4) static shape ------------------------------------------------------

assert.ok(/async function _importWithRetry\(/.test(analyticsSrc),
    '_importWithRetry helper must exist');
assert.ok(/async function _loadCytoscape\(/.test(analyticsSrc),
    '_loadCytoscape must exist');
assert.ok(/cytoscape = await _loadCytoscape\(\)/.test(analyticsSrc),
    '_renderTopologyGraph must call _loadCytoscape()');
assert.ok(/topo-graph-retry-btn/.test(analyticsSrc),
    'the failure path must render a Retry button (id=topo-graph-retry-btn)');
assert.ok(/topo-graph-retry-btn.*addEventListener\('click'/s.test(analyticsSrc) ||
    /addEventListener\('click', \(\) => \{\s*_renderTopologyGraph/.test(analyticsSrc),
    'the Retry button must re-run _renderTopologyGraph on click');
assert.ok(/vendor\/cytoscape\.esm\.min\.js\?r=\$\{attempt\}/.test(analyticsSrc),
    'retried imports must use a cache-busting ?r=<attempt> query');
assert.ok(!/location\.reload/.test(analyticsSrc),
    'Retry must not trigger a full page reload');
const retryRegionStart = analyticsSrc.indexOf('async function _importWithRetry(');
const retryRegionEnd = analyticsSrc.indexOf('async function _renderTopologyGraph(');
assert.ok(retryRegionStart >= 0 && retryRegionEnd > retryRegionStart,
    'could not locate the B89 retry-loader region in analytics.js');
assert.ok(!/console\.log/.test(analyticsSrc.slice(retryRegionStart, retryRegionEnd)),
    'the new retry-loader code must not use console.log');

// --- 5-6) behaviour harness -------------------------------------------------
// Pull the real _importWithRetry body out of analytics.js so the REAL retry
// logic runs, not a copy.
function bodyOf(name) {
    const start = analyticsSrc.indexOf('async function ' + name + '(');
    assert.ok(start >= 0, name + ' missing from ' + ANALYTICS_REL);
    let depth = 0, i = analyticsSrc.indexOf('{', start);
    for (let j = i; j < analyticsSrc.length; j++) {
        if (analyticsSrc[j] === '{') depth++;
        else if (analyticsSrc[j] === '}') { depth--; if (!depth) return analyticsSrc.slice(start, j + 1); }
    }
    throw new Error('unbalanced braces for ' + name);
}

const src = bodyOf('_importWithRetry');

// Stub setTimeout: fires immediately (no real waiting) but records the
// requested delay so we can assert the 500ms/1500ms backoff schedule.
const recordedDelays = [];
const ctx = vm.createContext({
    setTimeout: (fn, ms) => { recordedDelays.push(ms); fn(); },
    Promise,
});
vm.runInContext(src, ctx, { filename: ANALYTICS_REL });

async function run(expr, ...args) {
    ctx.__args = args;
    return vm.runInContext(`${expr}(...__args)`, ctx);
}

// a) fails twice, succeeds on the 3rd attempt -> resolves, 2 delays used
(async () => {
    recordedDelays.length = 0;
    let calls = 0;
    const loadFn = async (attempt) => {
        calls++;
        if (attempt < 2) throw new Error(`boom-${attempt}`);
        return `ok-${attempt}`;
    };
    const result = await run('_importWithRetry', loadFn);
    assert.strictEqual(result, 'ok-2', 'must resolve with the value from the successful attempt');
    assert.strictEqual(calls, 3, 'loadFn must be called for attempts 0, 1, 2');
    assert.deepStrictEqual(recordedDelays, [500, 1500],
        'must back off 500ms then 1500ms between failed attempts');

    // b) fails on every attempt (3x total: 0,1,2) -> rejects with the last
    //    error; no delay is scheduled after the final (non-retried) failure.
    recordedDelays.length = 0;
    let calls2 = 0;
    const alwaysFail = async (attempt) => { calls2++; throw new Error(`fail-${attempt}`); };
    await assert.rejects(
        () => run('_importWithRetry', alwaysFail),
        /fail-2/,
        'must reject with the last attempt\'s error after exhausting retries');
    assert.strictEqual(calls2, 3, 'loadFn must be called exactly 3 times (initial + 2 retries)');
    assert.deepStrictEqual(recordedDelays, [500, 1500],
        'only 2 backoff delays are scheduled for 3 total attempts');

    console.log('bug-b89-topology-graph-retry.check.js: all checks passed -> OK');
})().catch((e) => {
    process.exitCode = 1;
    throw e;
});
