// Node-runnable check for the CE Cloud tab (#main-tab-burst-accounts) - IMPL-CE-CLOUD-TAB-VISIBLE.
// Owner decision (owner 11-11, answer B): the tenant's OWN cloud account tab is VISIBLE and
// USABLE in CE; MOD-hosted burst (the sibling #main-tab-burst spill tab) stays CE-hidden.
// This supersedes Tim B3 item 5 (11-06), which hid the Cloud tab on CE.
//
// Same technique as analytics-burst-ce-gate.check.js / burst-accounts.check.js: the gate code
// is EXTRACTED VERBATIM from the shipped source and run against a stub DOM, so the assertions
// test the shipped source, not a re-typed copy.
//
//   node CORE/Dashboard/tests/ce-cloud-tab-visible.check.js
//
// Asserts ON CE: the checkUserRole reveal shows the tab for a tenant admin (role gate only,
// no edition gate), the panel initializer reveals #main-tab-burst-accounts AND calls
// renderBurstAccountsSection() with isPaidEdition() = false, the initializer adds NO 'hidden'
// class to the tab or panel, the deep link ?tab=burst-accounts is NOT redirected to System for
// a tenant admin and IS for a non-admin, and the Connect button is NOT locked on CE.
// ON PAID: identical reveal + render, and the Connect lock IS present (paid behaviour unchanged).
// The sibling operator Burst tab stays CE-hidden (the gate was not widened to it).
'use strict';
const fs = require('fs');
const path = require('path');

const analyticsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'analytics.js'), 'utf8');
const baSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'burst-accounts.js'), 'utf8');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

function revealGate(src, needle) {
    const at = src.indexOf(needle);
    if (at < 0) throw new Error(`needle '${needle}' not found`);
    const cond = src.lastIndexOf('if (', at);
    const open = src.indexOf('{', cond);
    return src.slice(cond, open + blockAt(src, open).length);
}

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// DOM that records every add('hidden') per element id.
function makeTrackingDom() {
    const els = {};
    const added = {};
    const doc = {
        getElementById: (id) => {
            const e = els[id] || (els[id] = {
                classes: new Set(['hidden']),
                style: { setProperty: () => {} },
                dataset: {},
                addEventListener: () => {},
                display: null,
            });
            e.classList = {
                remove: (c) => e.classes.delete(c),
                add: (c) => { if (c === 'hidden') added[id] = (added[id] || 0) + 1; e.classes.add(c); },
                toggle: (c) => e.classes.has(c) ? e.classes.delete(c) : e.classes.add(c),
            };
            return e;
        },
    };
    const visible = (id) => { const e = els[id]; return !!e && e.display !== 'none' && !e.classes.has('hidden'); };
    return { doc, visible, added };
}

// --- 1. checkUserRole reveal: role gate only, no edition gate ------------------
const CLOUD_REVEAL = "\n            document.getElementById('main-tab-burst-accounts')?.classList.remove('hidden');";
const cloudGate = revealGate(analyticsSrc, CLOUD_REVEAL);
function runReveal(paid, isPlatformAdmin, isSuperAdmin) {
    const dom = makeTrackingDom();
    new Function('document', 'isPlatformAdmin', 'isSuperAdmin', '_ceEditionAn',
        cloudGate)(dom.doc, isPlatformAdmin, isSuperAdmin, !paid);
    return dom;
}
check('CE + platform admin: Cloud tab REVEALED (owner 11-11 answer B)',
    runReveal(false, true, false).visible('main-tab-burst-accounts'));
check('CE + super admin (tenant root): Cloud tab REVEALED',
    runReveal(false, false, true).visible('main-tab-burst-accounts'));
check('CE + regular tenant member: Cloud tab still hidden (role gate intact)',
    !runReveal(false, false, false).visible('main-tab-burst-accounts'));
check('paid + platform admin: Cloud tab revealed (paid behaviour unchanged)',
    runReveal(true, true, false).visible('main-tab-burst-accounts'));
check('paid + super admin: Cloud tab revealed (paid behaviour unchanged)',
    runReveal(true, false, true).visible('main-tab-burst-accounts'));
check('paid + regular tenant member: Cloud tab still hidden (role gate intact)',
    !runReveal(true, false, false).visible('main-tab-burst-accounts'));
check('reveal statement in the shipped source carries NO edition gate',
    /if \(isPlatformAdmin \|\| isSuperAdmin\)\s*\{\s*document\.getElementById\('main-tab-burst-accounts'\)/.test(analyticsSrc));

// --- 2. panel initializer: reveals + renders in EVERY edition ------------------
const initMarker = "} else if (panelId === 'main-panel-burst-accounts') {";
const mIdx = analyticsSrc.indexOf(initMarker);
if (mIdx < 0) throw new Error('panel-init branch not found');
const open = mIdx + initMarker.length - 1;
const cond = initMarker.slice(initMarker.indexOf('if (') + 4, initMarker.lastIndexOf(')'));
const initBa = `if (${cond}) ${blockAt(analyticsSrc, open)}`;

function resolvedEdition(paid) {
    return () => { const o = { catch: () => o, then: (cb) => cb(paid) }; return o; };
}
function rejectedEdition() {
    return () => ({ catch: (h) => ({ then: (cb) => cb(h()) }), then: () => undefined });
}

function runInitBa(edition) {
    const dom = makeTrackingDom();
    // switchToTab shows the panel before the initializer runs (same as the
    // existing analytics-burst-ce-gate check): the initializer must not re-hide it.
    dom.doc.getElementById('main-panel-burst-accounts').classList.remove('hidden');
    let rendered = 0;
    new Function('document', 'panelId', 'isPaidEdition', 'renderBurstAccountsSection',
        initBa)(dom.doc, 'main-panel-burst-accounts', edition, () => { rendered++; });
    return { rendered, dom };
}
const ceInit = runInitBa(resolvedEdition(false));
check('CE tab activation: renderBurstAccountsSection IS called (own-cloud burst usable on CE)',
    ceInit.rendered === 1);
check('CE tab activation: #main-tab-burst-accounts revealed',
    ceInit.dom.visible('main-tab-burst-accounts'));
check('CE tab activation: #main-panel-burst-accounts revealed',
    ceInit.dom.visible('main-panel-burst-accounts'));
check('CE tab activation: NO hidden class added to the tab',
    !ceInit.dom.added['main-tab-burst-accounts']);
check('CE tab activation: NO hidden class added to the panel',
    !ceInit.dom.added['main-panel-burst-accounts']);
const unknownInit = runInitBa(rejectedEdition());
check('unknown edition (isPaidEdition rejects): Cloud tab still revealed + rendered (no edition gate)',
    unknownInit.rendered === 1 && unknownInit.dom.visible('main-panel-burst-accounts'));
const paidInit = runInitBa(resolvedEdition(true));
check('paid tab activation: renderBurstAccountsSection called (identical to CE)',
    paidInit.rendered === 1 && paidInit.dom.visible('main-tab-burst-accounts'));

// --- 3. deep-link fallback (?tab=burst-accounts) ------------------------------
const dlMarker = "    if (wanted && ['system', 'capture', 'settings', 'workers', 'burst', 'burst-accounts'";
const dlIdx = analyticsSrc.indexOf(dlMarker);
if (dlIdx < 0) throw new Error('deep-link block not found');
const dlOpen = analyticsSrc.indexOf('{', analyticsSrc.indexOf(')', dlIdx));
const dlBlock = blockAt(analyticsSrc, dlOpen);

function runDeepLink(wanted, isPlatformAdmin, isSuperAdmin, paid) {
    const dom = makeTrackingDom();
    let target = null;
    new Function('wanted', 'settingsChild', 'currentUser', 'isSuperAdmin', '_ceEditionAn',
        'switchToTab', 'console', dlBlock)(
        wanted, null, { is_platform_admin: isPlatformAdmin }, isSuperAdmin, !paid,
        (t) => { target = t; }, { log: () => {} });
    return { target, dom };
}
check('CE tenant-admin deep link #burst-accounts: NOT redirected to System',
    runDeepLink('burst-accounts', true, false, false).target === 'burst-accounts');
check('CE tenant-root deep link #burst-accounts: NOT redirected to System',
    runDeepLink('burst-accounts', false, true, false).target === 'burst-accounts');
check('CE non-admin deep link #burst-accounts: falls back to System (tenantAdminOnly intact)',
    runDeepLink('burst-accounts', false, false, false).target === 'system');
check('paid non-admin deep link #burst-accounts: still falls back (role gate intact)',
    runDeepLink('burst-accounts', false, false, true).target === 'system');
check('CE mesh deep links still fall back: topology',
    runDeepLink('topology', true, false, false).target === 'system');
check('CE mesh deep links still fall back: managers',
    runDeepLink('managers', true, false, false).target === 'system');
check('CE Apps deep link still falls back (B3 item 6 unchanged)',
    runDeepLink('apps', false, true, false).target === 'system');
check('shipped source: burst-accounts is NOT in the _ceEditionAn fallback clause',
    !/_ceEditionAn\s*&&\s*\([^)]*burst-accounts[^)]*\)/.test(analyticsSrc));
check('shipped source: apps IS still in the _ceEditionAn fallback clause',
    /_ceEditionAn\s*&&\s*\([^)]*apps[^)]*\)/.test(analyticsSrc));
check('shipped source: tenantAdminOnly role gate intact',
    /tenantAdminOnly\.includes\(wanted\) && !\(currentUser\?\.is_platform_admin \|\| isSuperAdmin\)/.test(analyticsSrc));

// --- 4. Connect lock: paid-only, CE never locked ------------------------------
const lockNeedle = "if (paid) MODEntitlements.lockElement(connectBtn, 'burst', 'Cloud burst');";
if (!baSrc.includes(lockNeedle)) throw new Error('Connect lock statement not found in burst-accounts.js');
const lockIdx = baSrc.indexOf('const connectBtn =');
const lockIfIdx = baSrc.indexOf('if (connectBtn)', lockIdx);
const lockDecl = baSrc.slice(lockIdx, lockIfIdx); // 'const connectBtn = $('ba-connect-submit');\n        '
const lockBlock = lockDecl + blockAt(baSrc, baSrc.indexOf('{', lockIfIdx));
// The shipped Connect lock chains .then(...).catch(...): the stubs must be
// chain-safe (then() returns something carrying catch()).
function resolvedChain(paid) {
    const p = { then: (cb) => { cb(paid); return p; }, catch: (h) => p };
    return () => p;
}
function rejectedChain() {
    // models an unreachable /platform/features: the .catch handler decides.
    return () => ({ then: (cb) => ({ catch: (h) => { h(); return {}; } }) });
}
function runLock(isPaidEdition) {
    const calls = [];
    // burst-accounts.js uses its `$` id helper; the extracted block needs it.
    const $ = (id) => ({ dataset: {}, classList: { add: () => {}, remove: () => {} } });
    new Function('$', 'isPaidEdition', 'MODEntitlements', lockBlock)(
        $, isPaidEdition, { lockElement: (el, cap, label) => { calls.push([cap, label]); } });
    return calls;
}
check('CE: Connect button NOT locked (Cloud burst unlocked on CE)',
    runLock(resolvedChain(false)).length === 0);
check('unknown edition (isPaidEdition rejects): Connect NOT locked (fail closed = CE)',
    runLock(rejectedChain()).length === 0);
check('paid: Connect button locked with the burst capability (paid behaviour unchanged)',
    runLock(resolvedChain(true)).length === 1);

// --- 5. sibling operator Burst tab (MOD-hosted spill) stays CE-hidden ---------
const SIB_REVEAL = "\n            document.getElementById('main-tab-burst')?.classList.remove('hidden');";
const sibGate = revealGate(analyticsSrc, SIB_REVEAL);
function runSibling(paid, isPlatformAdmin) {
    const dom = makeTrackingDom();
    new Function('document', 'isPlatformAdmin', '_ceEditionAn', sibGate)(
        dom.doc, isPlatformAdmin, !paid);
    return dom;
}
check('CE: sibling #main-tab-burst (MOD-hosted spill) stays hidden',
    !runSibling(false, true).visible('main-tab-burst'));
check('paid: sibling #main-tab-burst still revealed',
    runSibling(true, true).visible('main-tab-burst'));

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
