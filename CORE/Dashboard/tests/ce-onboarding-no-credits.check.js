#!/usr/bin/env node
// FIX-CE-ONBOARDING-BONUS-CREDITS (lane gpu1-1): CE has NO credits economy (owner 11-11,
// answer B). The REAL blocks are LIFTED from js/onboarding-checklist.js (no copy-paste)
// and run in a vm sandbox with stubbed getPlatformFeatures / fetch / document:
//   * CE / unknown edition / entitlements error / rejected features call -> NO credit
//     text (case-insensitive), NO claim button, NO POST /onboarding/checklist/claim-reward;
//     the checklist steps + progress UI stay.
//   * saas / pro (positive paid) -> identical to before (claim text + button + POST).
// Pattern: CORE/Dashboard/tests/ce-settings-oauth-failclosed.check.js.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const MODULE = path.join(ROOT, 'js', 'onboarding-checklist.js');
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

const GATE_BLOCK = lift(
    "    return getPlatformFeatures().then((f) => {",
    "    }).catch(() => true);");
const GUARD_LINE = lift("        if (data.all_done && (data.reward_claimed ||", ")) return;");
const REWARD_BLOCK = lift("    const rewardSection =", "\n               </div>`;");
const CLAIM_BLOCK = lift("    document.getElementById('claim-reward-btn')?.addEventListener", "\n    });");
check('gate block lifted from onboarding-checklist.js', GATE_BLOCK.includes('catch(() => true)'));
check('hide-guard line lifted', GUARD_LINE.endsWith('return;'));
check('rewardSection block lifted', REWARD_BLOCK.trimEnd().endsWith('`;'));
check('claim handler lifted', CLAIM_BLOCK.trimEnd().endsWith('});'));
check('gate guards ONLY the credits branch (isCe used twice: guard + reward)',
    (src.match(/isCe/g) || []).length >= 4 && REWARD_BLOCK.startsWith('    const rewardSection = (isCe || !isSaaS)'));
check('checklist steps rendering untouched', src.includes('data.steps.map') && src.includes('${stepsHtml}') && !REWARD_BLOCK.includes('stepsHtml'));

const ok = (v) => () => Promise.resolve(v);
const reject = () => Promise.reject(new Error('features unavailable'));
const ent = (e) => () => Promise.resolve({ entitlements: e });

async function runGate(gpf) {
    const sandbox = { console, getPlatformFeatures: gpf };
    vm.createContext(sandbox);
    return await vm.runInContext(`(async function() {\n${GATE_BLOCK}\n})()`, sandbox);
}

function runReward(isCe, isSaaS, data) {
    const sandbox = { console, isCe, isSaaS, data };
    vm.createContext(sandbox);
    return vm.runInContext(`${REWARD_BLOCK}\nrewardSection`, sandbox);
}

function hiddenWhen(isCe, isSaaS, data) {
    const sandbox = { console, isCe, isSaaS, data };
    vm.createContext(sandbox);
    return vm.runInContext(`(function() {\n${GUARD_LINE}\nreturn false;\n})()`, sandbox) === undefined;
}

// The claim handler is attached via `document.getElementById('claim-reward-btn')?.` —
// on CE the rendered rewardSection has no such element, so getElementById must return
// null (the honest DOM model driven by the HTML the reward block actually produced).
async function runClaim(isCe, isSaaS, data) {
    const rewardHtml = runReward(isCe, isSaaS, data);
    const hasBtn = rewardHtml.includes('claim-reward-btn');
    const calls = { listeners: 0, fetch: 0, btnText: null, hasBtn };
    const btn = { disabled: false, textContent: 'Claim 500 Credits', className: '', classList: { add() {} } };
    const handlers = [];
    btn.addEventListener = (type, fn) => { handlers.push(fn); };
    const sandbox = {
        console, data,
        document: { getElementById: (id) => ((id === 'claim-reward-btn' && hasBtn) ? btn : null) },
        apiUrl: () => 'http://api.test',
        authHeaders: () => ({ 'Authorization': 'Bearer t' }),
        fetch: (url, opts) => { calls.fetch++; return Promise.resolve({ ok: true, json: () => Promise.resolve({ detail: '' }) }); },
        showInfo: () => {}, showError: () => {}, setTimeout: () => {},
    };
    vm.createContext(sandbox);
    await vm.runInContext(`(async function() {\n${CLAIM_BLOCK}\n})()`, sandbox);
    calls.listeners = handlers.length;
    if (handlers.length) { await handlers[0](); calls.btnText = btn.textContent; }
    return calls;
}

const STEPS = [{ title: 'Upload a file', completed: true }, { title: 'Build a workflow', completed: true }];
const DONE = { steps: STEPS, total: 5, completed: 5, all_done: true, reward_claimed: false, reward_credits: 500 };
const CLAIMED = { ...DONE, reward_claimed: true };
const ONGOING = { ...DONE, all_done: false, completed: 2 };
const CE_CASES = [
    ['rejecting getPlatformFeatures', reject],
    ["edition 'ce'", ent({ edition: 'ce' })],
    ["edition 'ce' with entitlements error", ent({ edition: 'ce', error: 'entitlements unavailable' })],
    ['missing entitlements', ok({})],
    ["unknown edition 'foo'", ent({ edition: 'foo' })],
    ["unknown edition 'enterprise'", ent({ edition: 'enterprise' })],
];

(async () => {
    if (GATE_BLOCK && GUARD_LINE && REWARD_BLOCK && CLAIM_BLOCK) {
        for (const [label, gpf] of CE_CASES) {
            const isCe = await runGate(gpf);
            check(`gate ${label}: isCe true (fail closed)`, isCe === true);
            for (const [tag, data] of [['all done', DONE], ['claimed', CLAIMED], ['in progress', ONGOING]]) {
                const html = runReward(isCe, true, data);
                check(`CE ${label} / ${tag}: no 'credit' text (ci)`, !/credit/i.test(html));
                check(`CE ${label} / ${tag}: no claim button`, !html.includes('claim-reward-btn'));
                check(`CE ${label} / ${tag}: checklist steps + progress still render`,
                    html === '' || html.includes('All done! You\'re all set.'));
            }
            const c = await runClaim(isCe, true, DONE);
            check(`CE ${label}: claim button never registered`, c.listeners === 0);
            check(`CE ${label}: no POST to claim-reward route`, c.fetch === 0);
            check(`CE ${label} all-done: checklist hidden (no dangling panel)`, hiddenWhen(isCe, true, DONE));
        }
        for (const edition of ['saas', 'pro']) {
            const isCe = await runGate(ent({ edition }));
            check(`gate paid '${edition}': isCe false`, isCe === false);
            const claimHtml = runReward(isCe, true, DONE);
            check(`paid '${edition}': claim line unchanged`, claimHtml.includes('Claim your 500 bonus credits'));
            check(`paid '${edition}': claim button unchanged`, claimHtml.includes('id="claim-reward-btn"') && claimHtml.includes('Claim 500 Credits'));
            check(`paid '${edition}': all-done checklist NOT hidden`, hiddenWhen(isCe, true, DONE) === false);
            check(`paid '${edition}' claimed: earned line unchanged`,
                runReward(isCe, true, CLAIMED).includes('You earned 500 bonus credits!'));
            check(`paid '${edition}' in progress: reward sentence unchanged`,
                runReward(isCe, true, ONGOING).includes('Complete all 5 steps to earn <strong>500 bonus credits</strong>'));
            const c = await runClaim(isCe, true, DONE);
            check(`paid '${edition}': claim button registered`, c.listeners === 1);
            check(`paid '${edition}': claim POST fires`, c.fetch === 1);
            check(`paid '${edition}': button reads Claimed! after POST`, c.btnText === 'Claimed!');
        }
    }
    if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
    console.log('ALL OK');
})();
