#!/usr/bin/env node
// Licence-card runtime guard on the workers page (lead 11-11, CE wording defence).
// The #license-summary-card must start hidden and render ONLY on a positive paid edition
// (saas or pro). ce, unknown (no entitlements), error (entitlements.error set) and rejected
// (getPlatformFeatures throws) all keep it hidden, and no /workers/license-summary call may
// happen before the gate passes on CE.
// The REAL blocks are lifted from js/workers.js and js/api.js (no copy-paste) and run in a vm
// context with stubbed getPlatformFeatures and document. Paid behaviour is the same gate.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const workers = fs.readFileSync(path.join(ROOT, 'js', 'workers.js'), 'utf8');
const api = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// Whole-file syntax check of workers.js (runs on the test box, not the laptop).
let fullParse = false;
try {
    require('node:child_process').execFileSync(process.execPath, ['--check', path.join(ROOT, 'js', 'workers.js')], { stdio: 'pipe' });
    fullParse = true;
} catch (e) { fullParse = false; }
check('workers.js full-file node --check passes', fullParse);

// Lift [startMark .. endToken] from a source string; '' if either marker is missing.
function liftBlock(src, startMark, endToken) {
    const start = src.indexOf(startMark);
    if (start < 0) return '';
    const end = src.indexOf(endToken, start);
    if (end < 0) return '';
    return src.slice(start, end + endToken.length);
}

// Real isPaidEdition() from api.js (export keyword stripped for vm script context).
const PAID_BLOCK = liftBlock(api, 'export async function isPaidEdition() {', '\n}\n')
    .replace(/^export /, '');
// Real gate prefix of loadLicenseSummary() from workers.js (up to the card reveal).
const GATE_BLOCK = liftBlock(workers,
    "    const summaryCard = document.getElementById('license-summary-card');",
    "summaryCard?.classList.remove('hidden');");

check('isPaidEdition lifted from api.js', PAID_BLOCK.includes('ent.edition'));
check('licence-card gate lifted from workers.js', GATE_BLOCK.includes('isPaidEdition()'));

// Wrapper position check: the gate must return before loadLicenseSummary() is called,
// and the card starts hidden before the gate runs.
const wStart = workers.indexOf('async function loadLicenseSummaryIfPaid() {');
const wBody = wStart < 0 ? '' : workers.slice(wStart, workers.indexOf('\n}\n', wStart));
const gateAt = wBody.indexOf('if (!await isPaidEdition()) {');
// D74: the Add Worker action row lives inside the card; the non-paid branch must move it out.
check('non-paid branch moves #workers-header-actions out of the hidden card',
    wBody.indexOf("getElementById('workers-header-actions')") > gateAt
    && wBody.indexOf("insertAdjacentElement('afterend', actions)") > gateAt
    && wBody.indexOf('return;') > wBody.indexOf("insertAdjacentElement('afterend', actions)")
    && wBody.indexOf("insertAdjacentElement('afterend', actions)") < wBody.indexOf('await loadLicenseSummary();'));
const callAt = wBody.indexOf('await loadLicenseSummary();');
check('gate returns before loadLicenseSummary() is called', gateAt > 0 && callAt > gateAt);
check('card starts hidden before the gate runs', wBody.indexOf("classList.add('hidden')") >= 0
    && wBody.indexOf("classList.add('hidden')") < gateAt);
// Every caller goes through the gated wrapper: the only ungated call is inside the wrapper.
check('no ungated loadLicenseSummary() caller',
    workers.split('await loadLicenseSummary();').length - 1 === 1);
// The licence-summary fetch itself is still in loadLicenseSummary (not moved).
const fnStart = workers.indexOf('async function loadLicenseSummary() {');
check('licence-summary fetch stays in loadLicenseSummary', fnStart > 0
    && workers.slice(fnStart, workers.indexOf('\n}\n', fnStart))
        .includes('fetch(`${apiUrl}/workers/license-summary`'));

// Run the gate for one getPlatformFeatures outcome; returns { hidden, returned, fetched }.
async function runGate(mode) {
    const classes = new Set();
    const card = {
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
        },
    };
    let fetched = false;
    const payloads = {
        ce: { entitlements: { edition: 'ce' } },
        unknown: {},
        error: { entitlements: { edition: 'saas', error: 'entitlements unavailable' } },
        saas: { entitlements: { edition: 'saas' } },
        pro: { entitlements: { edition: 'pro' } },
    };
    const ctx = {
        console,
        fetch: () => { fetched = true; return Promise.resolve({}); },
        document: { getElementById: (id) => (id === 'license-summary-card' ? card : null) },
        getPlatformFeatures: mode === 'rejected'
            ? async () => { throw new Error('network down'); }
            : async () => payloads[mode],
    };
    vm.createContext(ctx);
    vm.runInContext(`${PAID_BLOCK}\nasync function __gate() {\n${GATE_BLOCK}\nreturn 'proceed';\n}`, ctx);
    const returned = await vm.runInContext('__gate()', ctx);
    return { hidden: classes.has('hidden'), returned, fetched };
}

(async () => {
    for (const mode of ['ce', 'unknown', 'error', 'rejected']) {
        const r = await runGate(mode);
        check(`${mode}: card stays hidden`, r.hidden === true);
        check(`${mode}: gate returns without proceeding`, r.returned === undefined);
    }
    for (const mode of ['saas', 'pro']) {
        const r = await runGate(mode);
        check(`${mode}: card is revealed`, r.hidden === false);
        check(`${mode}: gate proceeds to the fetch`, r.returned === 'proceed');
    }
    if (failures) {
        console.error(`\n${failures} check(s) failed`);
        process.exit(1);
    }
    console.log('\nall ce-workers-licence-card-failclosed checks passed');
})().catch((e) => { console.error(e); process.exit(1); });
