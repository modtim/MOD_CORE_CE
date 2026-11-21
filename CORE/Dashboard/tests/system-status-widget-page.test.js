// Node-runnable check for the home-page System Status widget
// (Dashboard/js/system-status-widget.js, 900 lines, 0 test refs before this
// file). The module is an ES module (imports isPaidEdition from api.js), so
// this script extracts the body from `const apiUrl` to EOF with the `export `
// keyword stripped and runs it in a vm sandbox with a stubbed window. Asserts
// the pure helpers the render path is built from:
//   (a) aggregateState: STRICT aggregation - any 'down' -> red, else any
//       'degraded' -> yellow, else green (design.md D2);
//   (b) escapeHtml: all five entities, so a hostile container name cannot
//       inject markup into the card;
//   (c) _fmtBytes: null -> en-dash, B/KB/MB/GB/TB ladder, caps at TB;
//   (d) _fmtTimeAgo: null -> en-dash, unparseable -> verbatim, s/m/h/d buckets;
//   (e) _splitContainers: init/one-shot containers (my_vault_unseal, ...) are
//       NEVER flagged; exited/dead/unhealthy/restarting>=3 are problems;
//   (f) _dotClass: health 'unhealthy' beats state, unknown -> gray;
//   (g) _renderContainerRow: escaped name, manual-fix block only on problems;
//   (h) _aggBadge: unknown state -> 'Unknown' + gray, never an empty class.
//
//   node --test CORE/Dashboard/tests/system-status-widget-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'system-status-widget.js'), 'utf8');
const start = src.indexOf('const apiUrl =');
assert.ok(start >= 0, 'could not locate the widget body');
const block = src.slice(start).replace('export async function', 'async function');

const sandbox = {
    console: { log: () => {}, error: () => {} },
    window: { APP_CONFIG: { API_URL: 'http://api.localhost' } },
    isPaidEdition: () => false,
};
const fns = vm.runInNewContext(
    `"use strict";\n${block}\n({ aggregateState, escapeHtml, _aggBadge, _dot, _fmtBytes, _fmtTimeAgo, _splitContainers, _containerMetaText, _dotClass, _renderContainerRow, _INIT_NAMES });`,
    sandbox, { filename: 'system-status-widget-body' });

const DAY = 86400000;
function isoAgo(seconds) { return new Date(Date.now() - seconds * 1000).toISOString(); }

test('aggregateState: strict - any down -> red, else any degraded -> yellow, else green', () => {
    const r = fns.aggregateState([{ state: 'ok' }, { state: 'degraded' }]);
    assert.strictEqual(JSON.stringify(r), '{"state":"yellow","label":"Some components degraded"}');
    const d = fns.aggregateState([{ state: 'degraded' }, { state: 'down' }]);
    assert.strictEqual(JSON.stringify(d), '{"state":"red","label":"Outage detected"}');
    const g = fns.aggregateState([{ state: 'ok' }, { state: 'ok' }]);
    assert.strictEqual(JSON.stringify(g), '{"state":"green","label":"All systems normal"}');
    const e = fns.aggregateState([]);
    assert.strictEqual(e.state, 'green', 'empty probe set must not read as an outage');
});

test('escapeHtml: five entities, hostile name cannot inject markup', () => {
    const out = fns.escapeHtml(`<a href="x" onerror='y'>&`);
    assert.strictEqual(out, '&lt;a href=&quot;x&quot; onerror=&#39;y&#39;&gt;&amp;');
    assert.ok(!/<[a]/.test(out), 'raw tag left in output');
});

test('_fmtBytes: null dash, B/KB/MB/GB/TB ladder, caps at TB', () => {
    assert.strictEqual(fns._fmtBytes(null), '–');
    assert.strictEqual(fns._fmtBytes(0), '0.0 B');
    assert.strictEqual(fns._fmtBytes(512), '512.0 B');
    assert.strictEqual(fns._fmtBytes(1023), '1023.0 B');
    assert.strictEqual(fns._fmtBytes(1536), '1.5 KB');
    assert.strictEqual(fns._fmtBytes(5 * 1024 * 1024), '5.0 MB');
    assert.strictEqual(fns._fmtBytes(3 * 1024 ** 3), '3.0 GB');
    assert.strictEqual(fns._fmtBytes(40 * 1024 ** 4), '40.0 TB', 'ladder must stop at TB');
});

test('_fmtTimeAgo: null dash, unparseable verbatim, s/m/h/d buckets', () => {
    assert.strictEqual(fns._fmtTimeAgo(null), '–');
    assert.strictEqual(fns._fmtTimeAgo('not-a-date'), 'not-a-date');
    assert.strictEqual(fns._fmtTimeAgo(isoAgo(0)), '0s ago');
    assert.strictEqual(fns._fmtTimeAgo(isoAgo(59)), '59s ago');
    assert.strictEqual(fns._fmtTimeAgo(isoAgo(60)), '1m ago');
    assert.strictEqual(fns._fmtTimeAgo(isoAgo(3600)), '1h ago');
    assert.strictEqual(fns._fmtTimeAgo(isoAgo(86400)), '1d ago');
    assert.strictEqual(fns._fmtTimeAgo(isoAgo(-90000)), '0s ago', 'future date clamps to 0');
});

test('_splitContainers: init/one-shot containers never flagged; exited/dead/unhealthy/restart>=3 are', () => {
    const cs = [
        { name: 'my_vault_unseal', state: 'exited', health: null, restart_count: 0 },
        { name: 'my_api', state: 'running', health: 'healthy', restart_count: 0 },
        { name: 'my_worker', state: 'exited', health: null, restart_count: 0 },
        { name: 'my_gw', state: 'running', health: 'unhealthy', restart_count: 1 },
        { name: 'my_tail', state: 'restarting', health: null, restart_count: 3 },
        { name: 'my_tail2', state: 'restarting', health: null, restart_count: 2 },
    ];
    const { problems, ok } = fns._splitContainers(cs);
    assert.strictEqual(JSON.stringify(problems.map((c) => c.name)), JSON.stringify(['my_worker', 'my_gw', 'my_tail']));
    assert.strictEqual(JSON.stringify(ok.map((c) => c.name)), JSON.stringify(['my_vault_unseal', 'my_api', 'my_tail2']));
    assert.ok(fns._INIT_NAMES.has('my_tenant_harbor_prepare'), 'harbor prep must stay on the init list');
});

test('_dotClass: unhealthy health beats state; unknown state -> gray', () => {
    assert.strictEqual(fns._dotClass('running', 'unhealthy'), 'bg-red-500');
    assert.strictEqual(fns._dotClass('restarting', null), 'bg-yellow-500');
    assert.strictEqual(fns._dotClass('exited', null), 'bg-red-500');
    assert.strictEqual(fns._dotClass('unknown-state', null), 'bg-gray-400');
});

test('_renderContainerRow: escaped name, manual-fix only on problem rows', () => {
    const hostile = { name: '<img src=x onerror=alert(1)>', state: 'exited', health: null, restart_count: 0, exit_code: 13 };
    const html = fns._renderContainerRow(hostile, true);
    assert.ok(!/<img/.test(html), 'raw <img injected');
    assert.ok(/&lt;img/.test(html), 'name not escaped');
    assert.ok(/Manual fix:/.test(html), 'problem row missing manual-fix block');
    assert.ok(/exit 13/.test(html), 'exit code missing from meta');
    const clean = fns._renderContainerRow({ name: 'my_api', state: 'running', health: 'healthy', restart_count: 0 }, false);
    assert.ok(!/Manual fix:/.test(clean), 'healthy row carries a manual-fix block');
    assert.ok(!/restarts/.test(clean), 'zero restarts printed as meta');
});

test('_aggBadge: unknown state -> Unknown + gray, never an empty class', () => {
    const b = fns._aggBadge('bogus');
    assert.ok(/>Unknown</.test(b), 'label wrong');
    assert.ok(/bg-gray-100/.test(b), 'fallback class missing');
    assert.ok(/Outage/.test(fns._aggBadge('down')), 'down label wrong');
});
