#!/usr/bin/env node
// S-dead-6 F1 (+ REV FIX LIST items 1-2): the settings.js OAuth connections panel and the
// _ceNoMobile gate must fail CLOSED. Unknown / errored / missing entitlements = CE = hidden;
// only a clean saas or pro edition is paid (mirrors isPaidEdition() in api.js).
// The REAL blocks are lifted from js/settings.js (no copy-paste) and run in a vm context
// with stubbed getPlatformFeatures / initializeSettingsOauthConnections / document.
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

// Lift [startMark .. endToken] from settings.js; '' if either marker is missing.
function liftBlock(startMark, endToken) {
    const start = settings.indexOf(startMark);
    if (start < 0) return '';
    const end = settings.indexOf(endToken, start);
    if (end < 0) return '';
    return settings.slice(start, end + endToken.length);
}

const OAUTH_BLOCK = liftBlock(
    "    getPlatformFeatures().then((f) => {\n        const ent = f && f.entitlements;\n        return !(ent && !ent.error && (ent.edition === 'saas' || ent.edition === 'pro'));\n    }).catch(() => true).then((isCe) => {",
    '\n    });\n');
const MOBILE_BLOCK = liftBlock(
    "    const _ceNoMobile = await getPlatformFeatures().then((f) => {\n        const ent = f && f.entitlements;\n        return !(ent && !ent.error && (ent.edition === 'saas' || ent.edition === 'pro'));\n    }).catch(() => true);",
    "        document.getElementById('int-subpanel-mobile')?.classList.add('hidden');\n    }\n");
check('oauth block lifted from settings.js', OAUTH_BLOCK.trimEnd().endsWith('});'));
check('_ceNoMobile block lifted from settings.js', MOBILE_BLOCK.length > 0);

// Sandbox whose document records every element id that gets the 'hidden' class.
function makeSandbox(getPlatformFeatures, calls) {
    return {
        console,
        getPlatformFeatures,
        initializeSettingsOauthConnections() { calls.init++; },
        document: {
            getElementById(id) {
                return { classList: { add(c) { if (c === 'hidden') calls.hidden.push(id); } } };
            },
        },
    };
}
function newCalls() { return { init: 0, hidden: [] }; }
function flush() { return new Promise((resolve) => setTimeout(resolve, 0)); }

// Run the lifted OAuth block once; return the observed effects.
async function runOauth(getPlatformFeatures) {
    const calls = newCalls();
    const sandbox = makeSandbox(getPlatformFeatures, calls);
    vm.createContext(sandbox);
    vm.runInContext(OAUTH_BLOCK, sandbox);
    await flush();
    return calls;
}

// Run the lifted _ceNoMobile block (it awaits, so wrap it in an async function).
async function runMobile(getPlatformFeatures) {
    const calls = newCalls();
    const sandbox = makeSandbox(getPlatformFeatures, calls);
    vm.createContext(sandbox);
    const wrapped = `(async () => {\n${MOBILE_BLOCK}\nreturn _ceNoMobile;\n})()`;
    const ceNoMobile = await vm.runInContext(wrapped, sandbox);
    return { ceNoMobile, calls };
}

const ok = (v) => () => Promise.resolve(v);
const reject = () => Promise.reject(new Error('features unavailable'));
const ent = (entitlements) => ok({ entitlements });

(async () => {
    if (OAUTH_BLOCK && MOBILE_BLOCK) {
        const OAUTH_ID = 'int-oauth-connections';
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
            const r = await runOauth(gpf);
            check(`oauth ${label}: panel init NOT called`, r.init === 0);
            check(`oauth ${label}: ${OAUTH_ID} hidden`, r.hidden.includes(OAUTH_ID));
        }
        // Paid (positive): panel initialised, not hidden.
        for (const edition of ['saas', 'pro']) {
            const r = await runOauth(ent({ edition }));
            check(`oauth paid '${edition}': panel init called`, r.init === 1);
            check(`oauth paid '${edition}': panel NOT hidden`, r.hidden.length === 0);
        }

        // _ceNoMobile: fail closed; the Mobile sub-tab and its panel are hidden.
        const MOBILE_IDS = ['int-tab-mobile', 'int-subpanel-mobile'];
        const mobileCases = [
            ['rejecting getPlatformFeatures', reject],
            ["edition 'ce' with entitlements error", ent({ edition: 'ce', error: 'entitlements unavailable' })],
            ['missing entitlements', ok({})],
            ["unknown edition 'foo'", ent({ edition: 'foo' })],
            ["unknown edition 'enterprise'", ent({ edition: 'enterprise' })],
            ["edition 'ce'", ent({ edition: 'ce' })],
        ];
        for (const [label, gpf] of mobileCases) {
            const r = await runMobile(gpf);
            check(`_ceNoMobile ${label}: true`, r.ceNoMobile === true);
            check(`_ceNoMobile ${label}: Mobile tab and panel hidden`,
                MOBILE_IDS.every((id) => r.calls.hidden.includes(id)));
        }
        for (const edition of ['saas', 'pro']) {
            const r = await runMobile(ent({ edition }));
            check(`_ceNoMobile paid '${edition}': false`, r.ceNoMobile === false);
            check(`_ceNoMobile paid '${edition}': Mobile NOT hidden`, r.calls.hidden.length === 0);
        }

        // ---- TEST-HARDEN additions: edition / entitlements matrix ----
        const PAID = new Set(['saas', 'pro']);
        const editions = [['ce', 'ce'], ['saas', 'saas'], ['pro', 'pro'], ['team', 'team'],
            ['enterprise', 'enterprise'], ['business', 'business'], ['empty string', ''],
            ['null', null], ['undefined', undefined], ['number 1', 1], ['number 0', 0]];
        for (const [label, edition] of editions) {
            const paid = PAID.has(edition);
            const o = await runOauth(ent({ edition }));
            check(`matrix oauth edition ${label}: init ${paid ? 'called' : 'NOT called'}`, o.init === (paid ? 1 : 0));
            check(`matrix oauth edition ${label}: panel ${paid ? 'NOT hidden' : 'hidden'}`,
                paid ? o.hidden.length === 0 : o.hidden.includes('int-oauth-connections'));
            const m = await runMobile(ent({ edition }));
            check(`matrix _ceNoMobile edition ${label}: ${!paid}`, m.ceNoMobile === !paid);
            check(`matrix _ceNoMobile edition ${label}: Mobile ${paid ? 'shown' : 'hidden'}`,
                paid ? m.calls.hidden.length === 0
                    : ['int-tab-mobile', 'int-subpanel-mobile'].every((id) => m.calls.hidden.includes(id)));
        }
        const entCases = [
            ['entitlements null', ok({ entitlements: null })],
            ['entitlements undefined', ok({ entitlements: undefined })],
            ['entitlements {}', ok({ entitlements: {} })],
            ["entitlements {error:'x'}", ok({ entitlements: { error: 'x' } })],
            ["entitlements {edition:'pro', error:'x'}", ok({ entitlements: { edition: 'pro', error: 'x' } })],
            ["entitlements {edition:'saas', error:'x'}", ok({ entitlements: { edition: 'saas', error: 'x' } })],
            ['features null', ok(null)],
            ['features undefined', ok(undefined)],
            ['features is a string', ok('pro')],
            ['entitlements is a string', ok({ entitlements: 'pro' })],
        ];
        for (const [label, gpf] of entCases) {
            const o = await runOauth(gpf);
            check(`entitlements oauth ${label}: init NOT called`, o.init === 0);
            check(`entitlements oauth ${label}: hidden`, o.hidden.includes('int-oauth-connections'));
            const m = await runMobile(gpf);
            check(`entitlements _ceNoMobile ${label}: true`, m.ceNoMobile === true);
            check(`entitlements _ceNoMobile ${label}: Mobile hidden`,
                ['int-tab-mobile', 'int-subpanel-mobile'].every((id) => m.calls.hidden.includes(id)));
        }
        // Late resolution: a pending promise leaves the panel un-initialised; resolving paid
        // afterwards initialises it; resolving CE afterwards hides it.
        for (const [label, value, paid] of [['pro', { entitlements: { edition: 'pro' } }, true],
            ['ce', { entitlements: { edition: 'ce' } }, false]]) {
            let resolveIt;
            const pending = new Promise((r) => { resolveIt = r; });
            const calls = newCalls();
            const sb = makeSandbox(() => pending, calls);
            vm.createContext(sb);
            vm.runInContext(OAUTH_BLOCK, sb);
            await flush(); await flush();
            check(`late ${label}: before resolve init NOT called`, calls.init === 0);
            check(`late ${label}: before resolve nothing hidden or shown yet`, calls.hidden.length === 0);
            resolveIt(value);
            await flush(); await flush();
            check(`late ${label}: after resolve init ${paid ? 'called once' : 'NOT called'}`, calls.init === (paid ? 1 : 0));
            check(`late ${label}: after resolve ${paid ? 'not hidden' : 'hidden'}`,
                paid ? calls.hidden.length === 0 : calls.hidden.includes('int-oauth-connections'));
        }
        {   // late mobile: the awaited predicate does not settle until resolved
            let resolveIt;
            const pending = new Promise((r) => { resolveIt = r; });
            const calls = newCalls();
            const sb = makeSandbox(() => pending, calls);
            vm.createContext(sb);
            let settled = false;
            const p = vm.runInContext('(async () => {\n' + MOBILE_BLOCK + '\nreturn _ceNoMobile;\n})()', sb).then((v) => { settled = true; return v; });
            await flush(); await flush();
            check('late _ceNoMobile: not settled and nothing hidden before resolve', !settled && calls.hidden.length === 0);
            resolveIt({ entitlements: { edition: 'pro' } });
            check('late _ceNoMobile: pro afterwards -> false, not hidden', (await p) === false && calls.hidden.length === 0);
        }
        {   // late mobile resolving CE afterwards hides the sub-tab
            let resolveIt;
            const pending = new Promise((r) => { resolveIt = r; });
            const calls = newCalls();
            const sb = makeSandbox(() => pending, calls);
            vm.createContext(sb);
            const p = vm.runInContext('(async () => {\n' + MOBILE_BLOCK + '\nreturn _ceNoMobile;\n})()', sb);
            await flush();
            resolveIt({ entitlements: { edition: 'ce' } });
            check('late _ceNoMobile: ce afterwards -> true, hidden',
                (await p) === true && calls.hidden.includes('int-tab-mobile') && calls.hidden.includes('int-subpanel-mobile'));
        }
        // Idempotent per mount: one run of the block reads features once and inits once.
        for (const edition of ['saas', 'pro']) {
            const calls = newCalls();
            let n = 0;
            const sb = makeSandbox(() => { n++; return Promise.resolve({ entitlements: { edition } }); }, calls);
            vm.createContext(sb);
            vm.runInContext(OAUTH_BLOCK, sb);
            await flush(); await flush(); await flush();
            check(`idempotent '${edition}': one mount -> init exactly once`, calls.init === 1);
            check(`idempotent '${edition}': one mount -> exactly one features read`, n === 1);
        }
        {   // a throwing panel init is contained and neither hides nor duplicates
            const calls = newCalls();
            const sb = makeSandbox(ok({ entitlements: { edition: 'pro' } }), calls);
            sb.initializeSettingsOauthConnections = () => { calls.init++; throw new Error('init boom'); };
            sb.console = { error() {}, log() {} };
            vm.createContext(sb);
            vm.runInContext(OAUTH_BLOCK, sb);
            await flush(); await flush();
            check('throwing init: called once, contained, panel not hidden', calls.init === 1 && calls.hidden.length === 0);
        }
        {   // the same-file consumers share isPaidEdition() from api.js: lift and run its matrix
            const apiSrc = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');
            const a0 = apiSrc.indexOf('export async function isPaidEdition()');
            const a1 = apiSrc.indexOf('\n}\n', a0);
            check('isPaidEdition lifted from api.js', a0 >= 0 && a1 > a0);
            const fnSrc = apiSrc.slice(a0, a1 + 3).replace('export async function', 'async function');
            const runPaid = async (gpf) => {
                const sb = { getPlatformFeatures: gpf };
                vm.createContext(sb);
                return vm.runInContext('(' + fnSrc + ')()', sb);
            };
            const paidCases = [
                ['rejecting', reject, false], ['entitlements null', ok({ entitlements: null }), false],
                ['entitlements {}', ok({ entitlements: {} }), false], ['features null', ok(null), false],
                ["{edition:'pro',error:'x'}", ent({ edition: 'pro', error: 'x' }), false],
                ["edition 'ce'", ent({ edition: 'ce' }), false], ["edition 'enterprise'", ent({ edition: 'enterprise' }), false],
                ["edition ''", ent({ edition: '' }), false], ['edition null', ent({ edition: null }), false],
                ["edition 'saas'", ent({ edition: 'saas' }), true], ["edition 'pro'", ent({ edition: 'pro' }), true],
            ];
            for (const [label, gpf, want] of paidCases) {
                check('isPaidEdition ' + label + ': ' + want, (await runPaid(gpf)) === want);
            }
            check('isPaidEdition sync throw: false (never throws)', (await runPaid(() => { throw new Error('boom'); })) === false);
            // Every settings.js consumer is a bare `isPaidEdition()` call (no local try/catch that
            // could swallow into a fail-open value): assert no consumer has an inline catch-to-true.
            const failOpenCatch = /isPaidEdition\(\)[^;]*\.catch\(\s*\(\)\s*=>\s*true\s*\)/.test(settings);
            check('settings.js: no isPaidEdition().catch(() => true) fail-open consumer', !failOpenCatch);
            // Count-driven (FIX-CE-HIDE-BUDGETS-TAB lane:gpu1-2 added a THIRD direct
            // consumer — the Settings > Budgets mount): every direct call site must
            // carry the EXACT fail-closed positive-paid predicate text (saas/pro +
            // .catch(() => true)), so a new consumer is asserted fail-closed, never
            // skipped. `await getPlatformFeatures().then(` is matched by the same
            // regex (substring), so the call-site count needs no separate await term.
            const gpfCallSites = (settings.match(/getPlatformFeatures\(\)\.then\(/g) || []).length;
            const predicateRe = /getPlatformFeatures\(\)\.then\(\(f\) => \{\n\s*const ent = f && f\.entitlements;\n\s*return !\(ent && !ent\.error && \(ent\.edition === 'saas' \|\| ent\.edition === 'pro'\)\);\n\s*\}\)\.catch\(\(\) => true\)/g;
            const gpfFailClosed = (settings.match(predicateRe) || []).length;
            check('settings.js: every direct getPlatformFeatures() consumer uses the fail-closed predicate',
                gpfCallSites >= 2 && gpfFailClosed === gpfCallSites);
        }
    }
    if (failures) {
        console.error(`${failures} failure(s)`);
        process.exit(1);
    }
    console.log('ALL OK');
})();
