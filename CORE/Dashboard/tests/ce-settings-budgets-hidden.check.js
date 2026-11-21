#!/usr/bin/env node
// FIX-CE-HIDE-BUDGETS-TAB: the Settings > Budgets (workflow credit-spend caps) panel is
// hidden on CE — credit spend caps are a QUANTITY CAP, which CE must not have (owner
// decision owner 11-11 ~2:00 PM CT, answer B). The mount in js/settings.js must use the
// positive-PAID gate (same predicate as the OAuth panel / _ceNoMobile): ONLY a clean
// saas or pro edition shows the panel and calls initBudgetsPanel; CE, an unknown
// edition, an entitlements error payload and a rejected getPlatformFeatures() call all
// HIDE the container and skip the init (so no fetch hits the CE-denied /budgets
// router — workflow_budgets.py, manifest:1284). The old _ceNoMobile predicate
// (edition === 'ce' && !error) failed OPEN on unknown/errored editions.
// The REAL block is lifted from js/settings.js (no copy-paste) and run in a vm
// context with stubbed getPlatformFeatures / initBudgetsPanel / document.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const settings = fs.readFileSync(path.join(ROOT, 'js', 'settings.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// Whole-file syntax check of settings.js (runs on the test box, not the laptop).
let fullParse = false;
try {
    require('node:child_process').execFileSync(process.execPath, ['--check', path.join(ROOT, 'js', 'settings.js')], { stdio: 'pipe' });
    fullParse = true;
} catch (e) { fullParse = false; }
check('settings.js full-file node --check passes', fullParse);

// Lift the budgets mount block: the getPlatformFeatures().then(...) chain whose
// .then((isCe) => ...) body contains the initBudgetsPanel call.
function liftBudgetsBlock(src) {
    const anchor = src.indexOf("initBudgetsPanel(document.getElementById('int-budgets'))");
    if (anchor < 0) return '';
    const start = src.lastIndexOf('getPlatformFeatures().then((f) => {', anchor);
    if (start < 0) return '';
    const endTok = '\n    });';
    const end = src.indexOf(endTok, anchor);
    if (end < 0) return '';
    return src.slice(start, end + endTok.length);
}
const BUDGETS_BLOCK = liftBudgetsBlock(settings);
check('budgets mount block lifted from settings.js', BUDGETS_BLOCK.length > 0);
check('budgets block uses the positive-paid predicate (saas/pro)',
    /ent\.edition === 'saas' \|\| ent\.edition === 'pro'/.test(BUDGETS_BLOCK));
check('budgets block keeps .catch(() => true) (reject = hidden)',
    /\.catch\(\(\) => true\)/.test(BUDGETS_BLOCK));
check('budgets block keeps the async .catch() on initBudgetsPanel',
    /initBudgetsPanel\([\s\S]*?\)\.catch\(/.test(BUDGETS_BLOCK));

// Sandbox whose document records every element id that gets the 'hidden' class.
function makeSandbox(getPlatformFeatures, calls) {
    return {
        console,
        getPlatformFeatures,
        initBudgetsPanel() { calls.init++; return Promise.resolve(); },
        document: {
            getElementById(id) {
                return { classList: { add(c) { if (c === 'hidden') calls.hidden.push(id); } } };
            },
        },
    };
}
function newCalls() { return { init: 0, hidden: [] }; }
function flush() { return new Promise((resolve) => setTimeout(resolve, 0)); }

async function runBudgets(block, getPlatformFeatures) {
    const calls = newCalls();
    const sandbox = makeSandbox(getPlatformFeatures, calls);
    vm.createContext(sandbox);
    vm.runInContext(block, sandbox);
    await flush();
    return calls;
}

const ok = (v) => () => Promise.resolve(v);
const reject = () => Promise.reject(new Error('features unavailable'));
const ent = (entitlements) => ok({ entitlements });

// Mutation: a /tmp copy of settings.js with the OLD fail-open _ceNoMobile branch
// restored; the same cases must FAIL on the error/unknown editions there.
const OLD_BRANCH = [
    "if (_ceNoMobile) {\n",
    "        document.getElementById('int-budgets')?.classList.add('hidden');\n",
    "    } else {\n",
    "        try { initBudgetsPanel(document.getElementById('int-budgets')).catch((e) => console.error('budgets panel init failed:', e)); } catch (e) { console.error('budgets panel init failed:', e); }\n",
    "    }",
].join('');

(async () => {
    const BUDGETS_ID = 'int-budgets';
    if (BUDGETS_BLOCK) {
        // Fail closed: panel NOT initialised, hidden.
        const cases = [
            ['rejecting getPlatformFeatures', reject],
            ["edition 'ce'", ent({ edition: 'ce' })],
            ["edition 'ce' with entitlements error", ent({ edition: 'ce', error: 'entitlements unavailable' })],
            ['missing entitlements', ok({})],
            ["unknown edition 'foo'", ent({ edition: 'foo' })],
            ["unknown edition 'enterprise'", ent({ edition: 'enterprise' })],
        ];
        for (const [label, gpf] of cases) {
            const r = await runBudgets(BUDGETS_BLOCK, gpf);
            check(`budgets ${label}: init NOT called`, r.init === 0);
            check(`budgets ${label}: ${BUDGETS_ID} hidden`, r.hidden.includes(BUDGETS_ID));
        }
        // Paid (positive): panel initialised, not hidden.
        for (const edition of ['saas', 'pro']) {
            const r = await runBudgets(BUDGETS_BLOCK, ent({ edition }));
            check(`budgets paid '${edition}': init called`, r.init === 1);
            check(`budgets paid '${edition}': panel NOT hidden`, r.hidden.length === 0);
        }
    }

    // MUTATION PROOF: revert to the old _ceNoMobile predicate in a scratch copy;
    // the error/unknown-edition cases must now FAIL (they were fail-open).
    if (BUDGETS_BLOCK) {
        const mutated = settings.replace(BUDGETS_BLOCK, OLD_BRANCH);
        // The ORIGINAL fail-open predicate (pre s-dead-6-f1, commit 3e117784):
        // edition === 'ce' && !error, .catch(() => false) — error/unknown editions
        // are NOT CE, so the old branch showed the panel and called the init.
        const MUT_BLOCK = "const _ceNoMobile = await getPlatformFeatures().then((f) => {\n        const ent = f && f.entitlements;\n        return !!ent && ent.edition === 'ce' && !ent.error;\n    }).catch(() => false);\n    " + OLD_BRANCH;
        let mutationFails = 0;
        for (const [label, gpf] of [
            ["edition 'ce' with entitlements error", ent({ edition: 'ce', error: 'entitlements unavailable' })],
            ["unknown edition 'foo'", ent({ edition: 'foo' })],
            ["unknown edition 'enterprise'", ent({ edition: 'enterprise' })],
        ]) {
            const calls = newCalls();
            const sandbox = makeSandbox(gpf, calls);
            vm.createContext(sandbox);
            await vm.runInContext(`(async () => {\n${MUT_BLOCK}\n})()`, sandbox);
            await flush();
            // Old predicate: _ceNoMobile is false for error/unknown editions ->
            // panel NOT hidden and init CALLED: the new test's assertions fail.
            const wouldFail = !(calls.hidden.includes(BUDGETS_ID)) || calls.init !== 0;
            if (wouldFail) mutationFails++;
            console.log(`mutation ${label}: old predicate hides=${calls.hidden.includes(BUDGETS_ID)} init=${calls.init} -> new assertions ${wouldFail ? 'FAIL (caught)' : 'pass (MISSED)'}`);
        }
        check('mutation: old _ceNoMobile predicate fails the new assertions on >=3 error/unknown cases', mutationFails >= 3);
    }

    if (failures) {
        console.error(`${failures} failure(s)`);
        process.exit(1);
    }
    console.log('ALL OK');
})();
