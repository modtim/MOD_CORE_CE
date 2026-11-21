#!/usr/bin/env node
// FIX-ONBOARDING-CHECKLIST-JS-CE-ALL-DONE-REMOVE-FAB: in CE, once the
// checklist is all done the early return in initOnboardingChecklist used to leave the
// floating button (#checklist-fab) and the slide-out panel (#checklist-panel) on screen
// with stale numbers until a reload (AUDIT-CE-JOURNEY-ONBOARDING-CHECKLIST F2). The fix
// removes both in that branch, guarded on the fail-closed CE gate (_isCeEdition -> isCe)
// ONLY, so PAID (saas/pro, isCe false) is byte-for-byte unchanged.
// Pattern: CORE/Dashboard/tests/ce-onboarding-no-credits.check.js (lift the REAL blocks,
// run them in a vm sandbox with a stubbed document).
// MCW_OVERRIDE=<path> points the check at another copy of the module (mutation proof).
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const MODULE = process.env.MCW_OVERRIDE || path.join(ROOT, 'js', 'onboarding-checklist.js');
const src = fs.readFileSync(MODULE, 'utf8');

let failures = 0;
function check(name, cond) { if (cond) { console.log(`ok   ${name}`); } else { failures++; console.error(`FAIL ${name}`); } }

let fullParse = false;
try {
    require('node:child_process').execFileSync(process.execPath, ['--check', MODULE], { stdio: 'pipe' });
    fullParse = true;
} catch (e) { fullParse = false; }
check('onboarding-checklist.js full-file node --check passes', fullParse);

function lift(startMark, endToken) {
    const start = src.indexOf(startMark);
    if (start < 0) return '';
    const end = src.indexOf(endToken, start);
    if (end < 0) return '';
    return src.slice(start, end + endToken.length);
}

const REMOVAL_BLOCK = lift(
    "        if (data.all_done && isCe) {",
    "\n        }");
const GUARD_LINE = lift("        if (data.all_done && (data.reward_claimed ||", ")) return;");

check('CE all-done removal block lifted', REMOVAL_BLOCK.includes("getElementById('checklist-fab')?.remove()"));
check('removal block removes BOTH elements', REMOVAL_BLOCK.includes("getElementById('checklist-panel')?.remove()"));
check('removal guarded on isCe ONLY (no reward_claimed / !isSaaS in the guard)',
    REMOVAL_BLOCK.startsWith('        if (data.all_done && isCe) {'));
check('guard line itself unchanged (paid hide logic byte-for-byte)',
    src.includes('if (data.all_done && (data.reward_claimed || isCe || !isSaaS)) return;'));
check('paid claim handler still removes both 2s after a claim',
    src.includes("document.getElementById('checklist-fab')?.remove();") &&
    src.includes("document.getElementById('checklist-panel')?.remove();"));
check('elements still created by the renderers',
    src.includes("btn.id = 'checklist-fab';") && src.includes("panel.id = 'checklist-panel';"));

// Stub DOM: getElementById hands back a live element stub; remove() marks it gone.
function runBranch(isCe, isSaaS, data) {
    const els = {};
    function getElementById(id) { return els[id] || (els[id] = { id, removed: false, remove() { this.removed = true; } }); }
    const fab = getElementById('checklist-fab');
    const panel = getElementById('checklist-panel');
    const sandbox = { console, isCe, isSaaS, data, document: { getElementById } };
    vm.createContext(sandbox);
    vm.runInContext(`(function() {\n${REMOVAL_BLOCK}\n${GUARD_LINE}\nreturn 'reached';\n})()`, sandbox);
    return { fab: !fab.removed, panel: !panel.removed };
}

const DONE = { all_done: true, reward_claimed: false };
const CLAIMED = { all_done: true, reward_claimed: true };
const ONGOING = { all_done: false, reward_claimed: false };

(async () => {
    if (REMOVAL_BLOCK && GUARD_LINE) {
        // CE (fail-closed gate true): all done -> both elements gone, no stale UI.
        for (const saas of [true, false]) {
            const r = runBranch(true, saas, DONE);
            check(`CE all-done (saas=${saas}): fab removed`, r.fab === false);
            check(`CE all-done (saas=${saas}): panel removed`, r.panel === false);
            const c = runBranch(true, saas, CLAIMED);
            check(`CE claimed (saas=${saas}): fab removed`, c.fab === false);
            check(`CE claimed (saas=${saas}): panel removed`, c.panel === false);
            const o = runBranch(true, saas, ONGOING);
            check(`CE in progress (saas=${saas}): fab still present`, o.fab === true);
            check(`CE in progress (saas=${saas}): panel still present`, o.panel === true);
        }
        // PAID (helper patched False = _isCeEdition() -> false): today's behaviour exactly.
        for (const edition of ['saas', 'pro']) {
            const r = runBranch(false, true, DONE);
            check(`paid '${edition}' all-done not claimed: fab present (path unchanged)`, r.fab === true);
            check(`paid '${edition}' all-done not claimed: panel present (path unchanged)`, r.panel === true);
            const c = runBranch(false, true, CLAIMED);
            check(`paid '${edition}' claimed re-init: fab present (claim handler owns removal)`, c.fab === true);
            check(`paid '${edition}' claimed re-init: panel present`, c.panel === true);
            const o = runBranch(false, true, ONGOING);
            check(`paid '${edition}' in progress: fab present`, o.fab === true);
        }
    }
    if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
    console.log('ALL OK');
})();
