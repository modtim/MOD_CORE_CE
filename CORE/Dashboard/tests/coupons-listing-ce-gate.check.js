// Node-runnable check for the CE edition gate on the Listing-coupons section of
// the Coupons page (Dashboard/js/coupons.js, rc20 fix-coupons-ce-gate).
// coupons_router is declared in CORE/API/routers/admin_plans_payment_settings.py,
// which open-core/manifest.yaml:1281 (`CORE/API/routers/admin_plans*.py`) denies on
// CE, so GET /v1/coupons/listing is not a route at all (404) and loadListingCoupons()'s
// catch painted a red "Failed to load listing coupons" row.
//
// Same technique as admin-nodes-packages-ce-gate.check.js: the initializer is
// EXTRACTED VERBATIM from the shipped source text and run against a stub DOM, so
// the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/coupons-listing-ce-gate.check.js
//
// Asserts: CE -> loadListingCoupons()/wireListingCouponForm() never run (no fetch,
// no listeners on absent routes) and the listing-coupon loading container is hidden;
// paid/unknown edition -> both still run (Pro behaviour unchanged).
'use strict';
const fs = require('fs');
const path = require('path');

// CE carve (FIX-CE-DASH-SWEEP-C): js/coupons.js is denied on CE, so every
// assertion here (which reads that module's shipped source) has nothing to
// check; SKIP the whole file when it is absent. On a tree that ships it,
// nothing changes.
if (!fs.existsSync(path.join(__dirname, '..', 'js', 'coupons.js'))) {
    console.log('SKIP coupons-listing-ce-gate.check.js: js/coupons.js absent (CE carve)');
    process.exit(0);
}
const couponsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'coupons.js'), 'utf8');
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

const EXPORT_HDR = 'export async function initializeCouponsPage()';
const initAt = couponsSrc.indexOf(EXPORT_HDR);
if (initAt < 0) throw new Error('initializeCouponsPage not found in async form (old-file state: export function, no gate)');
const HDR = EXPORT_HDR.replace(/^export /, '');
const initSrc = `${HDR} ${blockAt(couponsSrc, couponsSrc.indexOf('{', initAt + EXPORT_HDR.length))}`;

const GATE_NEEDLE = 'if (await isPaidEdition()) {';
const gateAt = initSrc.indexOf(GATE_NEEDLE);
if (gateAt < 0) throw new Error('CE gate not found in initializeCouponsPage (old-file state)');
const loadAt = initSrc.indexOf('loadListingCoupons();');
const wireAt = initSrc.indexOf('wireListingCouponForm();');
if (!(loadAt > gateAt && wireAt > gateAt)) throw new Error('gate not before loadListingCoupons/wireListingCouponForm');

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

function makeDom() {
    const seed = {
        'listing-coupons-loading': [],
        'listing-coupons-table': ['hidden'],
        'listing-coupons-empty': ['hidden'],
    };
    const mk = (id) => ({ id, classes: new Set(seed[id] || []), style: { setProperty: () => {} }, dataset: {}, addEventListener: () => {} });
    const els = {};
    Object.keys(seed).forEach((id) => { els[id] = mk(id); });
    const doc = {
        getElementById: (id) => {
            const e = els[id] || (els[id] = mk(id));
            e.classList = {
                remove: (c) => e.classes.delete(c),
                add: (c) => e.classes.add(c),
                toggle: (c) => e.classes.has(c) ? e.classes.delete(c) : e.classes.add(c),
            };
            return e;
        },
    };
    const visible = (id) => { const e = els[id]; return !!e && !e.classes.has('hidden'); };
    return { doc, visible };
}

async function runInit(paid) {
    const dom = makeDom();
    const calls = { listing: 0, wireListing: 0, money: 0, bonus: 0, redeem: 0, wireMoney: 0, wireBonus: 0 };
    const stub = (k) => () => { calls[k]++; };
    const fn = new Function(
        'document', 'isPaidEdition',
        'loadMoneyCoupons', 'loadBonusCoupons', 'loadListingCoupons',
        'wireMoneyCouponForm', 'wireBonusCouponForm', 'wireListingCouponForm', 'wireRedeemDialog',
        `${initSrc}\nreturn initializeCouponsPage();`);
    await fn(
        dom.doc,
        () => ({ then: (cb) => cb(paid) }),
        stub('money'), stub('bonus'), stub('listing'),
        stub('wireMoney'), stub('wireBonus'), stub('wireListing'), stub('redeem'));
    return { calls, dom };
}

(async function () {
    const ce = await runInit(false);
    check('CE: loadListingCoupons() never runs (no GET /coupons/listing, no 404)', ce.calls.listing === 0);
    check('CE: wireListingCouponForm() never runs (no listeners on CE-absent controls)', ce.calls.wireListing === 0);
    check('CE: listing-coupons loading container hidden', !ce.dom.visible('listing-coupons-loading'));
    check('CE: listing table + empty state stay hidden', !ce.dom.visible('listing-coupons-table') && !ce.dom.visible('listing-coupons-empty'));
    check('CE: money/bonus/redeem wiring untouched', ce.calls.money === 1 && ce.calls.bonus === 1 && ce.calls.wireMoney === 1 && ce.calls.wireBonus === 1 && ce.calls.redeem === 1);

    const paid = await runInit(true);
    check('paid: loadListingCoupons() still runs (Pro behaviour unchanged)', paid.calls.listing === 1);
    check('paid: wireListingCouponForm() still runs', paid.calls.wireListing === 1);
    check('paid: loading container left visible for the loader', paid.dom.visible('listing-coupons-loading'));

    const unknown = await runInit(true);
    check('unknown edition is fail-open (treated as paid)', unknown.calls.listing === 1);

    check('coupons.js imports the existing isPaidEdition from ./api.js (no new helper)',
        /import\s*\{\s*isPaidEdition\s*\}\s*from\s*'\.\/api\.js';/.test(couponsSrc));
    check('api.js exports isPaidEdition (the SAME helper admin-nodes.js:11 uses)',
        /export async function isPaidEdition\(\)/.test(apiSrc));
    check('no new edition helper invented in coupons.js',
        !/function\s+_?ceEdition|const\s+_ceEdition/.test(couponsSrc));
    // FAILOPEN-C (working tree): api.js isPaidEdition is fail-CLOSED (unknown
    // payload -> false). The old check pinned `/return true;/`; pin the NEW
    // shape instead: positive saas/pro test + no unconditional `return true`.
    const paidHdrAt = apiSrc.indexOf('export async function isPaidEdition');
    if (paidHdrAt < 0) throw new Error('isPaidEdition not exported in api.js');
    const paidHelper = `export async function isPaidEdition ${blockAt(apiSrc, apiSrc.indexOf('{', paidHdrAt + 'export async function isPaidEdition'.length))}`;
    check('api.js isPaidEdition is fail-closed (positive saas/pro test)',
        /ent\.edition === 'saas' \|\| ent\.edition === 'pro'/.test(paidHelper));
    check('api.js isPaidEdition has no unconditional return true (fail-open shape gone)',
        !/return true;/.test(paidHelper));

    const elseAt = initSrc.indexOf('else {', gateAt);
    if (elseAt < 0) throw new Error('CE else-branch not found');
    const elseBlock = blockAt(initSrc, initSrc.indexOf('{', elseAt));
    check('CE else-branch touches only classList state (no innerHTML, no fetch)',
        !/innerHTML|fetch\(/.test(elseBlock));
    check('CE else-branch hides the loading line (DOM classList, no server text into DOM)',
        /classList\.add\('hidden'\)/.test(elseBlock));

    const listingFetches = couponsSrc.split('\n').filter((l) => l.includes('/coupons/listing') && l.includes('fetch('));
    check('3 /coupons/listing fetch sites remain in the file (init GET gated; deactivate + create POST intact)',
        listingFetches.length === 3);

    if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
    console.log('\nall checks passed');
})();
