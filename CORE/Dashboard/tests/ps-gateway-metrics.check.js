// Node-runnable check for the 27.5b gateway-queue renderer in
// js/persistent-stacks.js (_psGatewayLine + its pure helpers _psGwNum,
// _psGwWait, _psGwCache, _psGwPerWorker).
// The Dashboard has no JS unit-test runner and persistent-stacks.js is an ES
// module (imports ./api.js), so this script extracts the pure block from the
// source text and asserts it directly — same pattern as
// ps-secrets.check.js / ps-sidecar-filter.check.js.
//
//   node CORE/Dashboard/tests/ps-gateway-metrics.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

const escStart = src.indexOf('function _psEsc(s) {');
const escEnd = src.indexOf('\nfunction _psMsg');
const start = src.indexOf('// 27.5b: the gateway snapshot OMITS unknown fields');
const end = src.indexOf('// A stack\'s main data row as a single reusable unit');
if (escStart < 0 || escEnd < 0 || start < 0 || end < 0 || end <= start) {
    console.error('FAIL could not locate the gateway-metrics pure block in persistent-stacks.js');
    process.exit(1);
}
const block = src.slice(escStart, escEnd) + '\n' + src.slice(start, end);
const F = new Function(`${block}\nreturn {
    _psGatewayLine, _psGwNum, _psGwWait, _psGwCache, _psGwPerWorker,
};`)();

let failures = 0;
function check(name, cond, actual) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}${actual !== undefined ? ' — got: ' + JSON.stringify(actual) : ''}`); }
}

const line = F._psGatewayLine;

// --- absent metrics: nothing rendered, never a zero --------------------------
check('absent: null renders nothing', line(null) === '');
check('absent: undefined renders nothing', line(undefined) === '');
check('absent: empty object renders nothing', line({}) === '');
check('absent: non-object renders nothing', line('gateway') === '' && line(7) === '');

// --- full snapshot ----------------------------------------------------------
const full = {
    queue_depth: 4, wait_p50_s: 0.31, wait_p95_s: 2.4, cache_hit_rate: 0.667,
    replicas_total: 3, replicas_healthy: 2,
    per_worker: [
        { url: 'http://w0:30000', healthy: true, active: 2 },
        { url: 'http://w1:30000', healthy: true, active: 1 },
        { url: 'http://w2:30000', healthy: false, active: 0 },
    ],
};
let h = line(full);
check('full: renders', h.includes('data-testid="ps-gateway-metrics"'));
check('full: queue depth', /queue 4/.test(h));
check('full: p50 one decimal', h.includes('p50 0.3s'));
check('full: p95 one decimal', h.includes('p95 2.4s'));
check('full: cache percent', h.includes('cache 67%'));
check('full: replicas healthy/total', h.includes('2/3'));
check('full: in-flight per replica list', h.includes('in-flight 2 · 1 · 0'));
check('full: per-worker tooltip', h.includes('title="http://w0:30000: active 2 (healthy)'));
check('full: down replica in tooltip', h.includes('http://w2:30000: active 0 (down)'));
check('full: unhealthy replicas amber', h.includes('text-amber-600 font-semibold">2/3<'));

// --- partial: only queue_depth known → only that field is shown ------------
const onlyQ = { queue_depth: 9 };
h = line(onlyQ);
check('partial: queue shown', h.includes('queue 9'));
check('partial: unknown fields omitted', !h.includes('p50') && !h.includes('p95')
    && !h.includes('replicas') && !h.includes('cache') && !h.includes('in-flight'));
check('partial: queue 9 not amber (no total)', !h.includes('queue <span class="text-amber'));

// --- real zeros are printed, not dashed ------------------------------------
h = line({ queue_depth: 0, cache_hit_rate: 0, replicas_total: 2, replicas_healthy: 2 });
check('zero: queue 0 prints 0', h.includes('queue 0'));
check('zero: cache 0 prints 0%', h.includes('cache 0%'));
check('zero: healthy replicas not amber', h.includes('replicas 2/2'));

// --- unknown must never become 0 -------------------------------------------
check('null: explicit nulls render nothing', line({ queue_depth: null, wait_p95_s: null, cache_hit_rate: null }) === '');
check('null: no stray zero', !/queue 0|cache 0%|p95 0\.0s/.test(line({ queue_depth: null, wait_p95_s: null, cache_hit_rate: null })));
check('junk: non-numeric values are unknown', line({ queue_depth: 'n/a', cache_hit_rate: true }) === '');
check('junk: replicas_healthy alone still shows', line({ replicas_healthy: 1 }).includes('replicas 1/–'));

// --- escaping: HTML in a worker name/url is escaped ------------------------
const xss = {
    queue_depth: 1,
    per_worker: [{ url: '<img src=x onerror=alert(1)>', healthy: true, active: 1 }],
};
h = line(xss);
check('escape: raw tag never emitted', !h.includes('<img'));
check('escape: url escaped in tooltip', h.includes('&lt;img src=x onerror=alert(1)&gt;'));

// --- helpers ---------------------------------------------------------------
check('helper: _psGwNum nulls junk', F._psGwNum({ a: null, b: '', c: 'x', d: '4' }, 'a') === null
    && F._psGwNum({ b: '' }, 'b') === null && F._psGwNum({ c: 'x' }, 'c') === null
    && F._psGwNum({ d: '4' }, 'd') === 4);
check('helper: _psGwWait rounds to 1dp', F._psGwWait(1.25) === '1.3s' && F._psGwWait(null) === '–');
check('helper: _psGwCache clamps + <1%', F._psGwCache(1.4) === '100%' && F._psGwCache(0.004) === '<1%' && F._psGwCache(null) === '–');
check('helper: _psGwPerWorker drops junk', F._psGwPerWorker({ per_worker: [null, 'x', { url: 'u' }] }).length === 1);

console.log(failures ? `FAIL ${failures} check(s)` : 'ok all checks passed');
process.exit(failures ? 1 : 0);
