// Node-runnable check for _psFilterSidecarLogs in js/persistent-stacks.js.
// The Dashboard has no JS unit-test runner, and persistent-stacks.js is an ES
// module (imports ./api.js), so this script extracts the two pure functions
// from the source text and asserts them directly.
//
//   node CORE/Dashboard/tests/ps-sidecar-filter.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

// Both functions close with a `}` at column 0; nested closers are indented, so
// a non-greedy match to the first column-0 close brace is exact.
const isSidecarSrc = src.match(/function _psIsSidecarName[\s\S]*?\n}/)[0];
const filterSrc = src.match(/function _psFilterSidecarLogs[\s\S]*?\n}/)[0];
const _psFilterSidecarLogs = new Function(
    `${isSidecarSrc}\n${filterSrc}\nreturn _psFilterSidecarLogs;`
)();

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// --- Shape (a): compose ps -a table + `=== <service> ===` sections ---------
const shapeA = [
    '   Name                     Command               State    Ports',
    '------------------------------------------------------------------------------------',
    'modstack31-llama-server-1  ./run.sh              running   8000/tcp',
    'modstack31-ts-llama-server-1  /bin/tailscaled...  restarting  ',
    'modstack31-tsegress-llama-server-1  sleep ...   running',
    '',
    '=== llama-server ===',
    'ts=2026-09-29T10:00:00Z level=INFO msg="serving on :8000"',
    'ts=2026-09-29T10:00:01Z level=INFO msg="model loaded"',
    '',
    '=== tsegress-llama-server ===',
    'INFO: [runner] enrolling node',
    'WARN: [runner] connection error, retrying in 5s',
    'FATAL: [runner] panic: ...',
    '',
    '=== ts-llama-server ===',
    'tailscale: TUN device tun0 not up',
    'tailscale: re-authenticating...',
    '',
    '=== ingress-llama-server ===',
    '203.0.113.7 - "GET / HTTP/1.1" 502',
].join('\n');

let r = _psFilterSidecarLogs(shapeA, false);
check('a: app section kept', r.text.includes('=== llama-server ===') && r.text.includes('model loaded'));
check('a: sidecar sections dropped', !/=== (ts|tsegress|ingress)-/.test(r.text));
// (the ps -a table row for the ts- container itself is kept — only its
//  section is dropped)
check('a: sidecar content dropped', !r.text.includes('tailscale:') && !r.text.includes('connection error') && !r.text.includes('502'));
check('a: ps -a table kept', r.text.includes('Name') && r.text.includes('modstack31-llama-server-1'));
// 3 sidecar section headers + their body lines (3/2/1) + the 3 blank
// separators inside the dropped spans = 11
check('a: hidden count', r.hidden === 11, r.hidden);
check('a: no runaway blank stacks', !/\n{3,}/.test(r.text));

r = _psFilterSidecarLogs(shapeA, true);
check('a: show=true untouched', r.text === shapeA && r.hidden === 0);

// --- Shape (b): combined stream, `<service>-<n>  | ` line prefixes ---------
const shapeB = [
    'llama-server-1  | ts=2026-09-29T10:00:00Z level=INFO msg="serving on :8000"',
    'ts-llama-server-1  | tailscale: TUN device tun0 not up',
    'ts-llama-server-1  | tailscale: re-authenticating...',
    'tsegress-site-llama-server-1  | WARN: connection error, retrying in 5s',
    'ingress-llama-server-1  | 203.0.113.7 - "GET / HTTP/1.1" 502',
    'llama-server-1  | ts=2026-09-29T10:00:01Z level=INFO msg="model loaded"',
].join('\n');

r = _psFilterSidecarLogs(shapeB, false);
check('b: app lines kept in order',
    r.text.split('\n').length === 2
    && r.text.includes('serving on :8000') && r.text.includes('model loaded'));
check('b: sidecar lines dropped', !r.text.includes('tailscale') && !r.text.includes('502'));
check('b: hidden count', r.hidden === 4, r.hidden);

r = _psFilterSidecarLogs(shapeB, true);
check('b: show=true untouched', r.text === shapeB && r.hidden === 0);

// --- Everything was sidecar output ------------------------------------------
const allSidecar = [
    'ts-app-1  | tailscale: re-authenticating...',
    'tsegress-cloud-app-1  | FATAL: panic',
].join('\n');
r = _psFilterSidecarLogs(allSidecar, false);
check('all-sidecar: empty text', r.text === '');
check('all-sidecar: hidden count', r.hidden === 2, r.hidden);

// --- Look-alikes must survive -----------------------------------------------
const lookalikes = [
    '=== tstool ===',
    'tstool-1  | not a sidecar',
    'timestamp-1  | starts with ts but is not one',
    '=== ts-app ===',
    'ts-app-1  | real sidecar',
].join('\n');
r = _psFilterSidecarLogs(lookalikes, false);
check('lookalikes: tstool section kept', r.text.includes('=== tstool ===') && r.text.includes('not a sidecar'));
check('lookalikes: timestamp line kept', r.text.includes('starts with ts but is not one'));
check('lookalikes: real ts- section dropped', !r.text.includes('real sidecar'));
check('lookalikes: hidden count', r.hidden === 2, r.hidden);

// --- Degenerate inputs --------------------------------------------------------
check('empty in', JSON.stringify(_psFilterSidecarLogs('', false)) === JSON.stringify({ text: '', hidden: 0 }));
check('null in', JSON.stringify(_psFilterSidecarLogs(null, false)) === JSON.stringify({ text: '', hidden: 0 }));
check('no sidecars', _psFilterSidecarLogs('app-1  | hello\n', false).hidden === 0);

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
