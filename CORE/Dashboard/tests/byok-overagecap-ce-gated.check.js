// Node-runnable check for the CE gate on the Settings Tenant BYOK tab and the
// Subscription page's overage-cap fetch (PRODRED-B567A, rc20 class b: "CE frontend
// calls a CE-absent endpoint"). On CE, CORE/API/routers/tenant/byok.py and
// CORE/API/routers/overage_cap.py are api_deny (open-core/manifest.yaml), so
// GET /tenant/byok and GET /billing/overage-cap 404 and the e2e net-guard flags
// them (dash-ce-batch5:62 `#main-tab-byok` toBeHidden timed out;
// dash-ce-batch7:113 guardPage 404s on /v1/billing/overage-cap).
//
// Same technique as admin-nodes-packages-ce-gate.check.js: the gate code is
// EXTRACTED VERBATIM from the shipped source text and run against a stub DOM, so
// the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/byok-overagecap-ce-gated.check.js
//
// Asserts: CE -> _openByokTab() never fetches (no markup fetch, no tenant BYOK init
// -> no fetchApi('/tenant/byok')) and the tab + panel stay hidden; loadOverageCap() never
// fetches and hides the card. Paid -> both still fetch (Pro behaviour unchanged).
'use strict';
const fs = require('fs');
const path = require('path');

const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
// CE carve (FIX-CE-DASH-SWEEP-C): js/subscription.js is denied on CE. When it is
// absent the loadOverageCap() half of this file has nothing to assert (SKIP); the
// BYOK half (settings.js/api.js) stays live.
const hasSub = fs.existsSync(path.join(__dirname, '..', 'js', 'subscription.js'));
const subSrc = hasSub ? fs.readFileSync(path.join(__dirname, '..', 'js', 'subscription.js'), 'utf8') : '';
const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

function extractFn(src, hdr) {
    const at = src.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found in shipped source`);
    return `${hdr} ${blockAt(src, src.indexOf('{', at + hdr.length))}`;
}

const openByokSrc = extractFn(settingsSrc, 'async function _openByokTab()');
const loadCapSrc = hasSub ? extractFn(subSrc, 'async function loadOverageCap()') : null;

// The gate must sit BEFORE the first fetch in each loader (old-file state has no
// gate -> throw -> non-zero exit = FAIL).
const BYOK_GATE = 'if (!await isPaidEdition()) {';
const byokGateAt = openByokSrc.indexOf(BYOK_GATE);
if (byokGateAt < 0) throw new Error('CE gate not found in _openByokTab (old-file state)');
const byokFetchAt = openByokSrc.indexOf("fetch('/pages/tenant-byok.html')");
if (!(byokFetchAt > byokGateAt)) throw new Error('byok gate not before the markup fetch');

const CAP_GATE = 'billing_enabled === false';
let capGateAt = -1;
let capFetchAt = -1;
if (hasSub) {
    capGateAt = loadCapSrc.indexOf(CAP_GATE);
    if (capGateAt < 0) throw new Error('billing gate not found in loadOverageCap (old-file state)');
    capFetchAt = loadCapSrc.indexOf('`${apiUrl}/billing/overage-cap`');
    if (!(capFetchAt > capGateAt)) throw new Error('billing gate not before the overage-cap fetch');
}

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

function makeDom(seed) {
    const els = {};
    const doc = {
        getElementById: (id) => {
            const e = els[id] || (els[id] = {
                classes: new Set(seed[id] || []),
                style: { width: null },
                dataset: {},
                textContent: '',
                className: '',
                disabled: false,
                innerHTML: '',
                addEventListener: () => {},
            });
            e.classList = {
                remove: (c) => e.classes.delete(c),
                add: (c) => e.classes.add(c),
            };
            return e;
        },
    };
    const visible = (id) => { const e = els[id]; return !!e && !e.classes.has('hidden'); };
    return { doc, visible };
}

async function runByok(paid) {
    const dom = makeDom({ 'main-tab-byok': ['hidden'], 'main-panel-byok': ['hidden'] });
    let markupFetches = 0, apiFetches = 0, inits = 0;
    const fn = new Function('document', 'isPaidEdition', 'fetch', 'initializeTenantByokPage',
        `let _byokLoaded = false;\n${openByokSrc}\nreturn _openByokTab();`);
    await fn(
        dom.doc,
        () => ({ then: (cb) => cb(paid) }),
        (url, opts) => { markupFetches++; return { ok: true, text: () => '<div>byok</div>' }; },
        () => { inits++; apiFetches++; return { provider: '', key_ref: '' }; });
    return { markupFetches, apiFetches, inits, dom };
}

async function runCap(billingOn) {
    const dom = makeDom({ 'overage-cap-card': ['hidden'] });
    let fetched = 0;
    const fn = new Function('document', 'getPlatformFeatures', 'fetch', 'sessionStorage', 'apiUrl',
        `${loadCapSrc}\nreturn loadOverageCap();`);
    await fn(
        dom.doc,
        () => ({ then: (cb) => cb({ billing_enabled: billingOn }) }),
        (url, opts) => { fetched++; return { ok: true, json: () => ({ cap_cents: 1000, accrued_overage_cents: 500 }) }; },
        { getItem: () => 'tok' },
        'http://api.localhost');
    return { fetched, dom };
}

(async function () {
    const ce = await runByok(false);
    check('CE: no /tenant/byok fetch (no 404 on a carved route)', ce.apiFetches === 0);
    check('CE: no tenant-byok.html markup fetch', ce.markupFetches === 0);
    check('CE: initializeTenantByokPage never runs', ce.inits === 0);
    check('CE: #main-tab-byok stays hidden', !ce.dom.visible('main-tab-byok'));
    check('CE: #main-panel-byok stays hidden', !ce.dom.visible('main-panel-byok'));

    const paid = await runByok(true);
    // FIX-CE-DASH-SWEEP-C2: the CE scrub rewrites the PAID branch of _openByokTab
    // (open-core/manifest.yaml row settings.js:7790 replaces `await initializeTenantByokPage();`
    // with a CE comment), so on a CE cut the loader has nothing left that could call
    // fetchApi('/tenant/byok') and the paid assert below cannot hold on that tree.
    // The assert stays VERBATIM on the paid tree (detected from the shipped source text
    // still carrying the call - same tree-shape style as the hasSub guard above); on the
    // scrubbed extract the loader must instead stay inert (no throw, no denied-route
    // fetch) and the pinned paid wording prints a SKIP.
    const paidByokBranch = openByokSrc.includes('await initializeTenantByokPage();');
    if (!paidByokBranch) {
        console.log('SKIP paid: the BYOK loader still fetches (js/settings.js scrubbed: the paid branch is not in this tree)');
        console.log('SKIP paid: markup fetch still happens (same scrub)');
        check('CE-scrubbed: the BYOK loader does not throw and never reaches the denied /tenant/byok fetch',
            paid.apiFetches === 0 && paid.inits === 0);
    } else {
    check('paid: the BYOK loader still fetches (Pro behaviour unchanged)', paid.apiFetches === 1);
    check('paid: markup fetch still happens', paid.markupFetches === 1);
    }

    if (!hasSub) {
        console.log('SKIP loadOverageCap checks: js/subscription.js absent (CE carve)');
    } else {
    const capCe = await runCap(false);
    check('CE: loadOverageCap makes no fetch (no 404 on /billing/overage-cap)', capCe.fetched === 0);
    check('CE: overage-cap card stays hidden', !capCe.dom.visible('overage-cap-card'));

    const capPaid = await runCap(true);
    check('paid: loadOverageCap still fetches', capPaid.fetched === 1);
    }

    // --- reveal site + helper reuse (no invented gate) ----------------------
    const revealLine = settingsSrc.split('\n').find((l) => l.includes("main-tab-byok')?.classList.remove('hidden')"));
    if (!revealLine) throw new Error('byok tab reveal site not found');
    check('the tab reveal is guarded by the existing isPaidEdition() (same idiom as Knowledge Base)',
        /^\s*if \(await isPaidEdition\(\)\) document\.getElementById\('main-tab-byok'\)\?\.classList\.remove\('hidden'\);\s*$/.test(revealLine));
    check('settings.js imports isPaidEdition from ./api.js',
        /import\s*\{[^}]*\bisPaidEdition\b[^}]*\}\s*from\s*'\.\/api\.js';/.test(settingsSrc));
    if (hasSub) check('subscription.js imports getPlatformFeatures from ./api.js (no new detection)',
        /import\s*\{[^}]*\bgetPlatformFeatures\b[^}]*\}\s*from\s*'\.\/api\.js';/.test(subSrc));
    check('no new edition/billing detection helper invented in the two files',
        !/function\s+_?ceEdition|const\s+_ceEdition|function\s+_?billingDisabled/.test(settingsSrc + subSrc));
    check('api.js exports isPaidEdition (the SAME helper account.js:24 uses)',
        /export async function isPaidEdition\(\)/.test(apiSrc));
    check('api.js exports getPlatformFeatures (the SAME helper subscription.js:112 uses)',
        /export (async )?function getPlatformFeatures\(\)/.test(apiSrc));

    // --- the fetch sites that remain ----------------------------------------
    const byokApiSites = settingsSrc.split('\n').filter((l) => l.includes("fetch('/pages/tenant-byok.html')"));
    check('exactly 1 tenant-byok markup fetch site remains (inside _openByokTab, after the gate)',
        byokApiSites.length === 1 && openByokSrc.includes("fetch('/pages/tenant-byok.html')"));
    if (hasSub) {
    const capSites = subSrc.split('\n').filter((l) => l.includes('`${apiUrl}/billing/overage-cap`'));
    check('the overage-cap GET is the guarded one (inside loadOverageCap, after the gate)',
        capSites.length >= 1 && loadCapSrc.includes('`${apiUrl}/billing/overage-cap`'));
    }

    // --- the CE gate block touches only classList state ---------------------
    const gateBlock = blockAt(openByokSrc, openByokSrc.indexOf('{', byokGateAt));
    check('CE gate block touches only classList state (no innerHTML, no fetch)',
        !/innerHTML|fetch\(/.test(gateBlock));
    if (hasSub) {
    const capTryAt = loadCapSrc.lastIndexOf('try {', capGateAt);
    const capGateBlock = blockAt(loadCapSrc, capTryAt + 4);
    check('billing gate block touches only classList state (no innerHTML, no fetch)',
        !/innerHTML|fetch\(/.test(capGateBlock));
    }

    if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
    console.log('\nall checks passed');
})();
