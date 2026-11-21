'use strict';
// FIX-CE-HEADER-CREDIT-PILL — the Community Edition
// header must NOT show the credit-balance pill. Credit metering is a paid
// concept (owner rule: never advertise paid features). On CE,
// credits_enabled and billing_enabled are false, so renderHeaderCredits took
// its "Unlimited" branch and un-hid #header-credit-indicator. The guard
// `if (await _isCeEdition()) return;` in js/ui.js renderHeaderCredits keeps the
// pill hidden on CE; paid editions fall through and render as before.
//
// Technique (as ce-hides-paid-pages.check.js): the shipped helpers are
// EXTRACTED VERBATIM from js/ui.js and run in a vm-style Function sandbox with
// a stub getPlatformFeatures, a stub fetchApi and a tiny DOM.
//
//   node CORE/Dashboard/tests/ce-header-credit-pill.check.js
//
// Asserts:
//   A1 CE edition, credits off  -> pill stays hidden, text untouched, no fetch
//   A2 CE edition, credits on   -> still hidden (CE never meters), no fetch
//   A3 unknown/errored entitlements -> hidden (fail-closed, unknown == CE)
//   A4 paid edition (pro), credits off -> pill renders "Unlimited" (unchanged)
//   A5 paid edition (saas), credits on -> pill renders balance from /subscriptions
//   A6 the guard line exists inside renderHeaderCredits after the element check
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const uiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

// --- extract the shipped helpers -------------------------------------------
const edStart = uiSrc.indexOf('function _isCeEdition() {');
assert.ok(edStart >= 0, '_isCeEdition not found in ui.js');
const edEnd = uiSrc.indexOf('function _hideCeComplianceNav() {', edStart);
assert.ok(edEnd > edStart, 'end marker for the edition helpers not found');
const edBlock = uiSrc.slice(edStart, edEnd);

const rhStart = uiSrc.indexOf('async function renderHeaderCredits(isPlatformAdmin) {');
assert.ok(rhStart >= 0, 'renderHeaderCredits not found in ui.js');
const rhEnd = uiSrc.indexOf('// --- User Display ---', rhStart);
assert.ok(rhEnd > rhStart, 'end marker for renderHeaderCredits not found');
const rhBlock = uiSrc.slice(rhStart, rhEnd);

// --- stub DOM + stub API ----------------------------------------------------
function makeEl() {
    const cls = new Set(['hidden']);
    const attrs = {};
    return {
        textContent: '—',
        cls,
        attrs,
        classList: {
            add: (c) => cls.add(c),
            remove: (c) => cls.delete(c),
            contains: (c) => cls.has(c),
        },
        setAttribute: (k, v) => { attrs[k] = v; },
        removeAttribute: (k) => { delete attrs[k]; },
    };
}

async function run(features, subscriptions) {
    const wrap = makeEl();
    wrap.attrs.href = '#billing';
    const valueEl = makeEl();
    const doc = {
        getElementById: (id) => (id === 'header-credit-indicator' ? wrap
            : id === 'header-credit-value' ? valueEl : null),
    };
    const calls = [];
    const getPlatformFeatures = () => Promise.resolve(features);
    const fetchApi = (p) => { calls.push(p); return Promise.resolve(subscriptions); };
    const fn = new Function('document', 'getPlatformFeatures', 'fetchApi',
        `${edBlock}\n${rhBlock}\nreturn renderHeaderCredits;`);
    const renderHeaderCredits = fn(doc, getPlatformFeatures, fetchApi);
    await renderHeaderCredits(false);
    return { wrap, valueEl, calls };
}

(async function () {
    const CE_OFF = { entitlements: { edition: 'ce' }, credits_enabled: false, billing_enabled: false };
    const CE_ON = { entitlements: { edition: 'ce' }, credits_enabled: true, billing_enabled: true };
    const BROKEN = { entitlements: { edition: 'ce', error: 'unresolved' } };
    const PRO_OFF = { entitlements: { edition: 'pro' }, credits_enabled: false, billing_enabled: false };
    const SAAS_ON = { entitlements: { edition: 'saas' }, credits_enabled: true, billing_enabled: true };
    const SUB = { credits_remaining: 1200, credits_total: 5000 };
    let passed = 0;

    // A1
    {
        const r = await run(CE_OFF, SUB);
        assert.ok(r.wrap.cls.has('hidden'), 'A1: CE pill must stay hidden');
        assert.ok(!r.wrap.cls.has('flex'), 'A1: CE pill must not be shown (flex)');
        assert.strictEqual(r.valueEl.textContent, '—', 'A1: CE must not write "Unlimited"');
        assert.deepStrictEqual(r.calls, [], 'A1: CE must not fetch /subscriptions');
        passed++;
    }
    // A2
    {
        const r = await run(CE_ON, SUB);
        assert.ok(r.wrap.cls.has('hidden') && !r.wrap.cls.has('flex'), 'A2: CE with credits on must stay hidden');
        assert.strictEqual(r.valueEl.textContent, '—', 'A2: CE must not write a balance');
        assert.deepStrictEqual(r.calls, [], 'A2: CE must not fetch /subscriptions');
        passed++;
    }
    // A3
    {
        const r = await run(BROKEN, SUB);
        assert.ok(r.wrap.cls.has('hidden') && !r.wrap.cls.has('flex'), 'A3: errored entitlements must stay hidden');
        assert.deepStrictEqual(r.calls, [], 'A3: unknown edition must not fetch /subscriptions');
        passed++;
    }
    // A4
    {
        const r = await run(PRO_OFF, SUB);
        assert.ok(r.wrap.cls.has('flex') && !r.wrap.cls.has('hidden'), 'A4: paid pill must render');
        assert.strictEqual(r.valueEl.textContent, 'Unlimited', 'A4: paid onprem shows "Unlimited"');
        assert.strictEqual(r.wrap.attrs.href, undefined, 'A4: href removed when billing disabled');
        passed++;
    }
    // A5
    {
        const r = await run(SAAS_ON, SUB);
        assert.ok(r.wrap.cls.has('flex') && !r.wrap.cls.has('hidden'), 'A5: saas pill must render');
        assert.strictEqual(r.valueEl.textContent, '1,200 credits', 'A5: saas shows remaining balance');
        assert.deepStrictEqual(r.calls, ['/subscriptions'], 'A5: saas fetches /subscriptions once');
        passed++;
    }
    // A6
    {
        const guard = 'if (await _isCeEdition()) return;';
        const at = rhBlock.indexOf(guard);
        assert.ok(at > 0, 'A6: CE guard line missing from renderHeaderCredits');
        assert.ok(rhBlock.indexOf('if (!wrap || !valueEl) return;') < at, 'A6: guard must follow the element check');
        assert.ok(rhBlock.indexOf('getPlatformFeatures()') > at, 'A6: guard must precede the feature/fetch path');
        passed++;
    }
    console.log(`ce-header-credit-pill: ${passed} passed, 0 failed`);
})().catch((e) => {
    console.error('ce-header-credit-pill: FAILED\n' + (e && e.message ? e.message : e));
    process.exit(1);
});
