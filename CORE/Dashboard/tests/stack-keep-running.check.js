// cUI-2b (30.6.15) — Dashboard stack "Keep running" toggle + pinned badge check.
// Same style as CORE/Dashboard/tests/admin-ingress-allowlist.check.js (see its
// header: no browser available, so every in-browser behaviour is asserted on the
// module's SOURCE TEXT, and the module is syntax-checked with
// `node --input-type=module --check`).
//
//   node CORE/Dashboard/tests/stack-keep-running.check.js
//
// Checks:
//   C1) js/persistent-stacks.js parses as a valid ES module.
//   C2) the checkbox markup: id="ps-keep-running-${s.id}",
//       data-ps-act="keep-running-toggle", data-testid="ps-keep-running-${s.id}",
//       type="checkbox", and the help text (no money words).
//   C3) the handler PUTs /stacks/{id}/keep-running with a JSON {enabled: <bool>}.
//   C4) the error path reverts the checkbox and messages (403 wording + server
//       detail through the _psMsg textContent path).
//   C5) the "pinned running" badge renders ONLY when s.keep_running is truthy,
//       carrying data-testid="ps-keep-running-badge-${s.id}".
//   C6) no bg-blue-600 / text-blue-600 anywhere (house rule).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const jsSrc = read('js/persistent-stacks.js');

// ==========================================================================
// C1) ES module syntax check
// ==========================================================================

let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: jsSrc, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
} catch (e) {
    modErr = e;
}
assert.ok(!modErr, `js/persistent-stacks.js must parse: ${(modErr && modErr.stderr) || modErr}`);

// ==========================================================================
// C2) the checkbox markup (row actions, next to the other row controls)
// ==========================================================================

assert.ok(/<input\s+type="checkbox"\s+id="ps-keep-running-\$\{sid\}"/.test(jsSrc),
    'C2: checkbox input with id="ps-keep-running-${sid}" must exist');
assert.ok(jsSrc.includes('data-ps-act="keep-running-toggle"'),
    'C2: checkbox must carry data-ps-act="keep-running-toggle"');
assert.ok(jsSrc.includes('data-testid="ps-keep-running-${sid}"'),
    'C2: checkbox must carry data-testid="ps-keep-running-${sid}"');
assert.ok(jsSrc.includes('data-ps-id="${sid}"'),
    'C2: checkbox must carry data-ps-id for the row dispatcher');
assert.ok(/Keep this stack running: idle reclaim will not stop it\./.test(jsSrc)
    || /Keep this stack running: idle reclaim will not stop it/.test(jsSrc),
    'C2: help text must say idle reclaim will not stop the stack');
for (const money of ['credit', 'cost', 'price', 'billed', 'payment']) {
    const near = jsSrc.includes('keep-running') && jsSrc.includes('idle reclaim will not stop it');
    assert.ok(!(near && new RegExp(`\\b${money}`, 'i').test(
        jsSrc.split('\n').filter(l => /keep-running|keep_running|Keep running|idle reclaim/.test(l)).join('\n'))),
        `C2: no money words near the Keep running control (${money})`);
}

// ==========================================================================
// C3) the handler PUTs /keep-running with an {enabled} boolean
// ==========================================================================

assert.ok(/async function _psKeepRunningSet\(/.test(jsSrc),
    'C3: handler _psKeepRunningSet(id, enabled) must exist');
const handler = jsSrc.slice(jsSrc.indexOf('async function _psKeepRunningSet('));
const hbody = handler.slice(0, handler.indexOf('\n}\n', 200) + 3);
assert.ok(/apiCall\(`\/stacks\/\$\{id\}\/keep-running`/.test(hbody),
    'C3: handler must call apiCall(`/stacks/${id}/keep-running`)');
assert.ok(/method:\s*'PUT'/.test(hbody), "C3: handler must use method: 'PUT'");
assert.ok(/JSON\.stringify\(\{\s*enabled:\s*on\s*\}/.test(hbody),
    'C3: body must be JSON.stringify({ enabled: <bool> })');
assert.ok(/const on = !!enabled;/.test(hbody),
    'C3: enabled must be coerced to a real boolean before the PUT');
assert.ok(/await _psLoadStacks\(\);/.test(hbody),
    'C3: handler must refresh the stack list after the PUT');

// ==========================================================================
// C4) error path: revert the checkbox + message (403 wording, server detail)
// ==========================================================================

assert.ok(/catch \(e\) \{/.test(hbody), 'C4: handler must catch API errors');
assert.ok(/cb\.checked = !on;/.test(hbody),
    'C4: error path must revert the checkbox (cb.checked = !on)');
assert.ok(/document\.querySelector\(`\[data-ps-act="keep-running-toggle"\]\[data-ps-id="\$\{id\}"\]`\)/.test(hbody),
    'C4: error path must find the checkbox through the data-ps-act/data-ps-id pair');
assert.ok(/Only the stack's owners can change this\./.test(hbody),
    "C4: 403 must show \"Only the stack's owners can change this.\"");
assert.ok(/e\.status === 403/.test(hbody), 'C4: 403 must be detected via e.status');
assert.ok(/_psMsg\(detail \|\|/.test(hbody),
    'C4: server detail must be surfaced through _psMsg (textContent path)');
assert.ok(/_psMsg\(`Only the stack's owners can change this\.`/.test(hbody),
    'C4: 403 message must go through _psMsg');

// ==========================================================================
// C5) badge renders only when keep_running is truthy
// ==========================================================================

const badgeIdx = jsSrc.indexOf('ps-keep-running-badge-');
assert.ok(badgeIdx > -1, 'C5: badge testid ps-keep-running-badge-${sid} must exist');
assert.ok(jsSrc.includes('data-testid="ps-keep-running-badge-${sid}"'),
    'C5: badge must carry data-testid="ps-keep-running-badge-${sid}"');
assert.ok(/const keepRunningBadge = s\.keep_running\s*\n?\s*\?\s*`/.test(jsSrc),
    'C5: badge must be guarded by s.keep_running (truthy server field only)');
const badgeLine = jsSrc.split('\n').filter(l => l.includes('ps-keep-running-badge-')).join('\n');
assert.ok(badgeLine.includes('pinned running'), 'C5: badge text is "pinned running"');
assert.ok(!/keepRunningBadge\s*\?\s*`/.test(jsSrc),
    'C5: badge must not be rendered from a client-side guess');
assert.ok(/span>\$\{intent\}\$\{keepRunningBadge\}/.test(jsSrc),
    'C5: badge must sit with the desired-state badges in the status cell');

// ==========================================================================
// C6) no blue-600 (house rule, admin-ingress-allowlist.check.js:16)
// ==========================================================================

assert.ok(!jsSrc.includes('bg-blue-600'), 'C6: no bg-blue-600');
assert.ok(!jsSrc.includes('text-blue-600'), 'C6: no text-blue-600');

console.log('stack-keep-running.check.js: all checks passed -> OK');
