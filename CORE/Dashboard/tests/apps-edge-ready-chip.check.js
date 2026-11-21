// 2400GX-c2-J6 (30.6.2) — static check for the Apps-panel `edge_ready` chip CE E2E:
// the assertion lives INSIDE the landed shard CORE/e2e/tests/dash-ce-batch18.spec.ts
// (D1 rec A) and the shard is now registered in CORE/e2e/ce-specs-full.txt.
// Same style as apps-auth-config-chip.check.js (ROOT = CORE/Dashboard); here the e2e
// tree is one level up: SPEC = ROOT/../e2e/tests/..., SPECLIST = ROOT/../e2e/....
//
// Failures are COLLECTED (not abort-on-first) so the MCW_OVERRIDE old-file proof run
// prints an exact F count; the final assert.ok is the rc gate.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');            // CORE/Dashboard

// The brief's path is ROOT/../e2e/... (ROOT = CORE/Dashboard). The nodetest sandbox
// mounts CORE/<dir> at /app, so the e2e tree is also reachable as /e2e/.. when the
// whole CORE is mounted; try both so the check works from the repo tree AND a CORE-rooted
// container. If none resolves, the C0 assertion fails loudly (no silent pass).
const SPEC_REL = ['e2e', 'tests', 'dash-ce-batch18.spec.ts'];
const LIST_REL = ['e2e', 'ce-specs-full.txt'];
// RUN-DASHBOARD-CHECKS-REST (lane:dell-2): the base is now spelled from __dirname
// (CORE/Dashboard/tests -> up 2 = CORE) so the e2e subjects resolve as CORE/e2e/...
// in the repo tree; the old ROOTS[0] (ROOT/'..') was the same value reached through
// ROOT, which collapses to '/' inside the nodetest sandbox (ROOT = /app/.. = /).
const ROOTS = [path.join(__dirname, '..', '..'), path.join(ROOT, '..'), ROOT, '/app/CORE', '/CORE', '/e2e/..'];
const pick = (rel) => {
    for (const r of ROOTS) {
        const p = path.join(r, ...rel);
        if (fs.existsSync(p)) return p;
    }
    return path.join(ROOT, '..', ...rel);
};
const SPEC = pick(SPEC_REL);
const SPECLIST = pick(LIST_REL);

// RUN-DASHBOARD-CHECKS-REST (lane:dell-2) leg 2: the nodetest sandbox mounts only
// CORE/Dashboard (/app), CORE/API (/API) and open-core (/open-core) - CORE/e2e is
// NOT mounted there, so the subjects below are unreachable and every subject-reading
// assertion would be a phantom red. Skip ONLY when no candidate ROOT resolves either
// subject at all (the sandbox); in the repo tree the subjects ARE reachable, so this
// gate never fires and a missing/wrong subject still fails loudly (no blanket skip).
const reachable = ROOTS.some((r) =>
    fs.existsSync(path.join(r, ...SPEC_REL)) || fs.existsSync(path.join(r, ...LIST_REL)));
if (!reachable) {
    console.log('SKIP: subjects not reachable (CORE/e2e not mounted)');
    process.exit(0);
}

const spec = fs.existsSync(SPEC) ? fs.readFileSync(SPEC, 'utf8') : '';
const specList = fs.existsSync(SPECLIST) ? fs.readFileSync(SPECLIST, 'utf8') : '';

const fails = [];
const soft = (name, cond) => { if (!cond) fails.push(name); };

const countLines = (text, needle) => {
    let n = 0;
    for (const line of text.split('\n')) {
        if (line.indexOf(needle) >= 0) n += 1;
    }
    return n;
};

// --- C0) the e2e tree was actually readable (no silent pass) ---------------
soft('C0 spec readable', spec.length > 0);
soft('C0 ce-specs list readable', specList.length > 0);

// --- C1) the new test name exists in the shard -----------------------------
soft('C1 test name present',
    /test\('apps panel: every App row carries an edge_ready chip word and the page echoes no X-Mod-\* header'/.test(spec));
soft('C1 guard label present',
    /guard\.assertClean\('apps edge-ready chip'\)/.test(spec));

// --- C2) the bool TYPE is asserted, never a fixed VALUE -------------------
soft('C2 typeof edge_ready boolean on the wire',
    /typeof [A-Za-z_.\[\]]*\.edge_ready\)\s*\.toBe\('boolean'\)/.test(spec));
soft('C2 no toBe(true|false) on edge_ready',
    !/edge_ready\)\s*\.toBe\((true|false)\)/.test(spec));
soft('C2 chip word SET asserted, not a fixed value',
    /toContainText\(\/edge \(can\|cannot\) verify\//.test(spec));

// --- C3) the leak assertions exist ----------------------------------------
// CLASS-A re-anchor (checkjs-triage-a-1030: the two needles below were stale):
// 9aa503c99 (BAN-J2) rewrote the DOM echo asserts — `page.evaluate('document.body.innerHTML')`
// + `expect(html).not.toContain(...)` is a banned pattern, so the shard now counts the
// text directly: `page.getByText(/X-Mod-/)).toHaveCount(0)` / `getByText(/ask_token/)).toHaveCount(0)`.
// Accept either shape so the gate survives the rewrite (same pattern as
// stack-login-gate-panel.check.js re-anchoring on a surviving marker).
soft("C3 no X-Mod- echo (DOM or wire)",
    /not\.toContain\('X-Mod-'\)/.test(spec) ||
    /getByText\(\/X-Mod-\/\)\)\.toHaveCount\(0\)/.test(spec));
soft("C3 ask_token absent from the DOM (innerHTML read or getByText count)",
    /expect\(html\)\.not\.toContain\('ask_token'\)/.test(spec) ||
    /getByText\(\/ask_token\/\)\)\.toHaveCount\(0\)/.test(spec));
soft("C3 not.toContain('ask_token') on the wire",
    /expect\(wire\)\.not\.toContain\('ask_token'\)/.test(spec));
soft("C3 no JWT prefix 'eyJ' in the response",
    /not\.toContain\('eyJ'\)/.test(spec));

// --- C4) registration: exactly one line each -------------------------------
soft('C4 ce-specs has exactly one dash-ce-batch18 line',
    countLines(specList, 'dash-ce-batch18') === 1);
// C4 re-pinned (RUN-DASHBOARD-CHECKS-REST, lane:dell-2): d12dc12af (lane:dell-1,
// "CE allowlists drop the hidden Apps DOM specs + ce-apps-tab-hidden witness")
// deliberately REMOVED tests/dash-apps.spec.ts and tests/dash-apps-auth-config.spec.ts
// from the CE allowlists (both lists: ce-specs-full.txt and ce-specs-env1fix.txt) once
// the Apps tab became hidden in CE - the witness tests/ce-apps-tab-hidden.spec.ts replaced
// them. The spec file itself is still tracked (paid keeps it), so the new truth is:
// NOT listed in ce-specs-full.txt AND the file still exists. The old "exactly one line"
// pin asserted the pre-d12dc12af contract and was stale, not a UI regression.
soft('C4 dash-apps-auth-config NOT listed in ce-specs-full.txt',
    countLines(specList, 'dash-apps-auth-config') === 0);
soft('C4 dash-apps-auth-config spec file still exists',
    fs.existsSync(path.join(path.dirname(SPECLIST), 'tests', 'dash-apps-auth-config.spec.ts')));

// --- C5) the ban regex appears nowhere in the shard ------------------------
// Built from fragments so this file never contains a ban-pattern literal itself.
const BAN = new RegExp([
    ':first' + '-child', ':last' + '-child', ':nth' + '-', ':first' + '-of-type',
    'if \\(!\\(await', '\\.first' + '\\(\\)', '\\.last' + '\\(\\)', '\\.nth' + '\\(\\)',
    'for' + 'ce:', 'waitFor' + 'Timeout', '\\.route' + '\\(', '\\.unroute' + '\\(',
    '\\.catch\\(\\(\\) => false\\)', 'filter\\(\\s*(async\\s*)?\\(',
].join('|'));
soft('C5 ban regex 0 hits', !BAN.test(spec));

// --- C6) no fixture file was touched by the shard's new test ---------------
soft('C6 no fixture path edit', !/fixtures\/(net|principals|base)\.ts\s*[:=]/.test(spec));

if (fails.length) {
    console.log(`FAILED (${fails.length}): ${fails.join(', ')}`);
}
assert.ok(fails.length === 0,
    `apps-edge-ready-chip.check.js: ${fails.length} failed — ${fails.join('; ') || 'none'}`);
console.log(`apps-edge-ready-chip.check.js: ${16 - fails.length}/16 assertions — All assertions passed`);
