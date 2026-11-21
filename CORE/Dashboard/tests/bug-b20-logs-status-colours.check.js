'use strict';
// BUG.B20-B25 + B32 (owner 10-08, /#logs): unreadable status colours.
// "failed" was red text on a red background (bg-red-100 text-red-800), the run-details
// PASS/FAIL bubbles were green-on-green / red-on-red, the failure-reason box was red-on-red
// and the system-tasks tab status text was unreadable.
// Wanted: SOLID background + WHITE text — green states bg-green-600 text-white, red states
// bg-red-600 text-white, amber/running/queued keep their hue but solid, failure-reason box
// bg-red-600 text-white.
//
//   node CORE/Dashboard/tests/bug-b20-logs-status-colours.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');

// --- 1) logs.js still parses as an ES module (the Dashboard loads js/* as modules)
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: logsJs, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) no light-on-light status pairs left ---------------------------------
// The bug class: a *-100 background paired with a *-800/900 text of the SAME hue.
const BAD_PAIRS = [
    /bg-green-100\s+text-green-/,
    /bg-red-100\s+text-red-/,
    /bg-yellow-100\s+text-yellow-/,
    /bg-blue-100\s+text-blue-/,
    /bg-orange-100\s+text-orange-/,
];
for (const re of BAD_PAIRS) {
    assert.ok(!re.test(logsJs),
        `js/logs.js: still pairs a -100 background with same-hue text (${re}) -> unreadable status badge`);
}

// --- 3) the shared helper returns solid + white for every status ------------
const helper = logsJs.match(/function getStatusColor\(status\)\s*\{([\s\S]*?)\n\}/);
assert.ok(helper, 'js/logs.js: getStatusColor helper not found');
const helperBody = helper[1];
for (const key of ['completed', 'success', 'failed', 'queued', 'running']) {
    const line = new RegExp(`'${key}':\\s*'([^']*)'`).exec(helperBody);
    assert.ok(line, `getStatusColor: no entry for '${key}'`);
    assert.ok(/text-white/.test(line[1]),
        `getStatusColor('${key}') must use text-white (got ${line[1]})`);
    assert.ok(/bg-(green|red|amber|sky|blue|orange|gray)-[56]00/.test(line[1]),
        `getStatusColor('${key}') must use a SOLID -500/-600 background (got ${line[1]})`);
}
assert.ok(/bg-green-600 text-white/.test(helperBody), "getStatusColor: completed/success -> bg-green-600 text-white");
assert.ok(/bg-red-600 text-white/.test(helperBody), "getStatusColor: failed -> bg-red-600 text-white");
assert.ok(/return colors\[status\] \|\| 'bg-gray-500 text-white'/.test(helperBody),
    'getStatusColor: unknown-status fallback must be solid gray + text-white');

// --- 4) PASS / FAIL bubbles (run-details modal + node rows + system tasks) --
assert.ok(/derivedLabel = 'PASS'; derivedClass = 'bg-green-600 text-white'/.test(logsJs),
    "node rows: PASS badge must be bg-green-600 text-white");
assert.ok(/derivedLabel = 'FAIL'; derivedClass = 'bg-red-600 text-white'/.test(logsJs),
    "node rows: FAIL badge must be bg-red-600 text-white");
assert.ok(/rounded-full bg-green-600 text-white font-medium">PASS/.test(logsJs),
    'run-details summary: PASS bubble must be bg-green-600 text-white');
assert.ok(/rounded-full bg-red-600 text-white font-medium">FAIL/.test(logsJs),
    'run-details summary: FAIL bubble must be bg-red-600 text-white');
assert.ok(/statusClass = 'bg-green-600 text-white';\s*statusLabel = 'PASS'/.test(logsJs),
    'system tasks tab: PASS badge must be bg-green-600 text-white');
assert.ok(/statusClass = 'bg-red-600 text-white';\s*statusLabel = 'FAIL'/.test(logsJs),
    'system tasks tab: FAIL badge must be bg-red-600 text-white');

// --- 5) failure-reason box: solid red + white, monospace kept ---------------
const reason = logsJs.match(/bg-red-600 rounded-lg[\s\S]{0,400}?Failure reason[\s\S]{0,200}?font-mono/);
assert.ok(reason, 'js/logs.js: failure-reason box must be bg-red-600 with white text (was bg-red-50 text-red-900)');
assert.ok(/text-sm text-white font-mono/.test(reason[0]),
    'failure-reason box: message line must be text-white and stay monospace');

// --- 6) every class we now emit exists in the built Tailwind -----------------
const tw = fs.readFileSync(path.join(ROOT, 'tailwind.css'), 'utf8');
for (const cls of ['bg-green-600', 'bg-red-600', 'bg-amber-500', 'bg-sky-600',
                   'bg-orange-600', 'bg-gray-500', 'text-white']) {
    assert.ok(tw.includes(`.${cls} {`),
        `tailwind.css: '.${cls}' missing from the built stylesheet (purge list) -> badge would be unstyled`);
}

console.log('bug-b20-logs-status-colours: OK');
