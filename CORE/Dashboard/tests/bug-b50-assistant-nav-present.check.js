'use strict';
// BUG.B50 (owner 10-08): GET /js/vendor/assistant-nav.js 404s on the live
// Dashboard, so the browser refuses it (nginx serves text/html) and
// assistant-launcher.js / assistant-panel.js log "ModAssistantNav unavailable"
// — the in-app assistant launcher + panel are silently disabled.
//
// Root cause: js/vendor/assistant-nav.js is GENERATED (by
// CORE/WorkflowBuilder/lib/assistant-nav/build-dashboard.js) and the root
// .gitignore `vendor/` rule excluded it, so it never survived into a fresh
// git checkout / off-box docker build context. The Dashboard Dockerfile never
// runs the generator either (its input, navActions.ts, lives in WorkflowBuilder).
//
// Checks:
//   1) index.html references js/vendor/assistant-nav.js.
//   2) The referenced file is present in the source tree.
//   3) The file is NOT git-ignored (it must be committed, or an off-box build
//      context silently drops it — this was the bug).
//   4) The file is syntactically valid JS.
//   5) The Dockerfile ships the js tree (COPY --from=builder /app/js ./js) and
//      hard-fails the build if the generated file is missing (no silent 404).
//   6) The generator's canonical source still exists in WorkflowBuilder.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync, spawnSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');            // CORE/Dashboard
const REPO = path.join(ROOT, '..', '..');           // repo root (CORE/..)

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const htmlSrc = read('index.html');
const dockerfile = read('Dockerfile');

// --- 1) index.html references the vendor module -----------------------------
const refMatch = /<script[^>]+src="([^"]*assistant-nav\.js)"/.exec(htmlSrc);
assert.ok(refMatch, 'index.html must load the assistant-nav vendor script');
const refPath = refMatch[1];
assert.ok(
    refPath === 'js/vendor/assistant-nav.js',
    `index.html assistant-nav src must be exactly js/vendor/assistant-nav.js, got ${refPath}`
);

// --- 2) present in the source tree -----------------------------------------
const vendorAbs = path.join(ROOT, refPath);
assert.ok(fs.existsSync(vendorAbs),
    `${refPath} missing from the Dashboard source tree — the live site 404s it`);

const vendorSrc = fs.readFileSync(vendorAbs, 'utf8');
assert.ok(vendorSrc.length > 0, `${refPath} must not be empty`);
assert.ok(
    vendorSrc.includes('ModAssistantNav'),
    `${refPath} must assign window.ModAssistantNav (assistant-launcher/panel depend on it)`
);

// --- 3) not git-ignored ----------------------------------------------------
// Class-A fix (environment-specific): the r9700/vm harness run tree is rsynced
// WITHOUT .git (r9700-run.sh EXCLUDES=(--exclude=.git)), so `git check-ignore`
// there dies with status 128 ("not a git repository") — an environment failure,
// not a product one. Where git CAN evaluate the path we assert exactly as
// before; where it cannot (no repo metadata in the run tree) the equivalent
// guarantee is the Dockerfile build guard asserted in check 5 — a missing
// generated file fails the build instead of 404ing live — so we note the
// unavailable assertion instead of failing the check.
const relVendor = path.join('CORE', 'Dashboard', refPath);
const ign = spawnSync('git', ['check-ignore', '-q', '--', relVendor], { cwd: REPO });
// Two shapes of "git cannot evaluate here": no repo metadata (status 128 /
// "not a git repository" — the rsynced run tree excludes .git) and no git
// binary at all in the node:20 test container (spawnSync ENOENT, status null).
const noGitRepo = ign.status === 128
    || ign.code === 'ENOENT'
    || ign.status === null
    || String(ign.stderr || '').includes('not a git repository');
if (noGitRepo) {
    console.log(`note: no git metadata in this run tree — check-ignore assertion for ${relVendor} unavailable (covered by the Dockerfile guard in check 5)`);
} else {
    assert.ok(ign.status === 0 || ign.status === 1,
        `git check-ignore failed (status=${ign.status}) — cannot evaluate ${relVendor}`);
    const ignoredFlag = ign.status === 0;
    assert.ok(!ignoredFlag,
        `${refPath} is git-ignored — a fresh checkout / off-box build context drops it (BUG.B50)`);
}

// --- 4) valid JS -----------------------------------------------------------
let jsErr = null;
try {
    execFileSync(process.execPath, ['--check', vendorAbs],
        { stdio: ['ignore', 'pipe', 'pipe'] });
} catch (e) {
    jsErr = String(e && (e.stderr || e.message));
}
assert.ok(!jsErr, `${refPath}: not valid JS — ${(jsErr || '').split('\n')[0] || 'unknown'}`);

// --- 5) Dockerfile ships it + fails loudly when it is missing --------------
assert.ok(
    /COPY\s+--from=builder\s+\/app\/js\s+\.\/js/.test(dockerfile),
    'Dockerfile must COPY --from=builder /app/js ./js (carries js/vendor/assistant-nav.js)'
);
assert.ok(
    dockerfile.includes(refPath),
    'Dockerfile must assert the generated assistant-nav file exists at build time ' +
    '(RUN test -e ...), so a missing file fails the build instead of 404ing live'
);

// --- 6) generator source intact -------------------------------------------
// Class-A fix (environment-specific, same shape as check 3): the harness
// nodetest container mounts only CORE/Dashboard (r9700-run.sh: -v $RUN_ROOT/CORE/<dir>:/app),
// so the WorkflowBuilder sibling tree is not visible in the run environment.
// Where the sibling app root IS visible we assert exactly as before; where it
// is not we note the unavailable assertion (the generator lives in the other
// app, and the Dockerfile guard in check 5 is the build-time guarantee that
// covers a missing generated file).
const appRoot = path.join(ROOT, '..', 'WorkflowBuilder');
const genSrc = path.join(appRoot, 'lib', 'assistant-nav', 'navActions.ts');
// Visibility probe must be a FILE: the nodetest container mounts
// -v mcw-node-modules-workflowbuilder:/WorkflowBuilder/node_modules, which
// creates the /WorkflowBuilder directory even when the app tree is absent.
if (!fs.existsSync(path.join(appRoot, 'package.json'))) {
    console.log(`note: WorkflowBuilder sibling tree not mounted in this run environment — generator assertion for ${genSrc} unavailable`);
} else {
    assert.ok(fs.existsSync(genSrc),
        'CORE/WorkflowBuilder/lib/assistant-nav/navActions.ts (canonical source) must exist');
}

console.log('bug-b50-assistant-nav-present: OK');
