// rc20 failopen-a2 — CE edition gates, BEHAVIOURAL (no browser, no npm deps).
// Style of tests/ce-signup-failclosed.check.js: the REAL code is lifted out of
// js/api.js + js/ui.js and EXECUTED in a fresh node:vm sandbox per scenario, with
// `fetch` stubbed. Static source-text pins cannot catch the fail-open shape (the
// old `!!ent && ent.edition === 'ce' && !ent.error` returns FALSE for main.py's
// entitlements FAILURE stub, so every CE-denied surface was REVEALED); this one
// runs the gate and the nav-hide/panel helpers that consume it.
//
//   node CORE/Dashboard/tests/failopen-ui-gates.check.js
//
// Pinned behaviour (the fail-closed contract of ui.js:284-312):
//   1) ui.js parses as a valid ES module and carries _ceEditionState + the
//      `!== 'paid'` resolution in _isCeEdition.
//   2) {edition:'ce', error:'unresolved'} -> CE (nav hidden, links hidden)
//   3) fetch rejects / payload null       -> CE
//   4) {edition:'pro'}                    -> paid (nav NOT hidden)
//   5) {edition:'saas'}                   -> paid
//   6) {edition:'<unknown string>'}       -> CE
//
// RED on the pre-failopen-a ui.js (cases 2/3 resolve paid there).

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const ui = read('js/ui.js');
const api = read('js/api.js');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// --- 1) structural: the module still parses and the gate is in shape ----------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: ui, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
check('ui.js parses as a valid ES module', !modErr);
check('ui.js carries the _ceEditionState helper', /function _ceEditionState\(\)/.test(ui));
check("ui.js _isCeEdition resolves on !== 'paid'",
    /return _ceEditionState\(\)\.then\(\(s\) => s !== 'paid'\);/.test(ui));

// Lift the real code under test (no re-implementation).
function lift(src, marker) {
    const i = src.indexOf(marker);
    if (i < 0) return null;
    const j = src.indexOf('\n}', i);
    if (j < 0) return null;
    return src.slice(i, j + 2).replace(/^export /gm, '');
}
const apiLift = lift(api, 'const _featuresCache = {');
const isCeLift = lift(ui, 'function _isCeEdition()');
const stateLift = lift(ui, 'function _ceEditionState()');
const navLift = lift(ui, 'function _hideCeComplianceNav()');
const paidNavLift = lift(ui, 'function _hideCePaidComplianceNav()');
const linksLift = lift(ui, 'function _hideCeGatedPageLinks(');
function liftSet(src, name) {
    const i = src.indexOf(`const ${name} = new Set([`);
    if (i < 0) return null;
    const j = src.indexOf('\n]);', i);
    if (j < 0) return null;
    return src.slice(i, j + 4);
}
const setCompliance = liftSet(ui, '_CE_COMPLIANCE_PAGES');
const setPaidCompliance = liftSet(ui, '_CE_PAID_COMPLIANCE_PAGES');
const setCov4b = liftSet(ui, '_CE_COV4B_PAGES');
if (!apiLift || !isCeLift || !navLift || !paidNavLift || !linksLift
    || !setCompliance || !setPaidCompliance || !setCov4b) {
    console.error('FAIL could not lift getPlatformFeatures/_isCeEdition/'
        + '_hideCeComplianceNav/_hideCePaidComplianceNav/_hideCeGatedPageLinks — '
        + 'the CE gate is not in the liftable shape');
    process.exit(1);
}

// --- the scenario runner ------------------------------------------------------
// Fresh sandbox per scenario: api.js caches the /platform/features payload, so
// each case needs a clean cache. The DOM stub records every nav-link display and
// every in-page anchor display.
async function scenario(payload, opts) {
    const els = {};
    const el = (id) => els[id] || (els[id] = {
        id: id,
        style: { display: '' },
        querySelectorAll: (sel) => (id === 'page-content' ? anchors : []),
    });
    const mkAnchor = (href) => ({
        href: href,
        getAttribute: (name) => (name === 'href' ? href : null),
        style: { display: '' },
    });
    const anchors = [mkAnchor('#business'), mkAnchor('#pricing-config'), mkAnchor('#workflows')];
    const sandbox = {
        console: console,
        document: { getElementById: (id) => el(id), querySelectorAll: () => [] },
        window: {},
        root: { querySelectorAll: () => anchors },
        _syncNavGroupHeadings: () => {},
        sleep: (ms) => new Promise((finish) => setTimeout(finish, ms)),
        fetch: opts.reject
            ? () => Promise.reject(new Error('offline'))
            : () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
    };
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + setCompliance + '\n' + setPaidCompliance + '\n' + setCov4b + '\n'
        + apiLift + '\n'
        + (stateLift ? stateLift + '\n' : '')
        + isCeLift + '\n' + navLift + '\n' + paidNavLift + '\n' + linksLift + '\n'
        + `(async () => {
              const ce = await _isCeEdition();
              await _hideCeComplianceNav();
              await _hideCePaidComplianceNav();
              await _hideCeGatedPageLinks(root);
              RESULT.ce = ce;
           })()`;
    vm.createContext(sandbox);
    const RESULT = { ce: null };
    sandbox.RESULT = RESULT;
    try {
        await vm.runInContext(src, sandbox, { filename: 'failopen-ui-gates.js' });
    } catch (e) {
        RESULT.error = String(e && (e.message || e));
        console.error(`# runner-error: ${RESULT.error} | anchors: `
            + anchors.map((a) => `${a.href}=${a.style.display}`).join(','));
    }
    const hidden = (id) => el(id).style.display === 'none';
    return {
        ce: RESULT.ce,
        navHidden: hidden('compliance-nav-link') && hidden('release-approvals-nav-link'),
        linksHidden: anchors.every((a) => a.style.display === 'none'
            || (a.href === '#workflows')),
    };
}

function expectCe(label, r) {
    check(`${label}: _isCeEdition resolves CE (fail-closed)`, r.ce === true);
    check(`${label}: CE nav links hidden`, r.navHidden);
    check(`${label}: in-page CE links hidden`, r.linksHidden);
}
function expectPaid(label, r) {
    check(`${label}: _isCeEdition resolves paid`, r.ce === false);
    check(`${label}: CE nav links NOT hidden`, r.navHidden === false);
    check(`${label}: in-page CE links NOT hidden`, r.linksHidden === false);
}

(async () => {
    // (a) main.py's entitlements FAILURE stub: edition 'ce' + error 'unresolved'.
    // The OLD fail-open gate resolved FALSE (paid) here and revealed every CE-denied
    // surface. Must resolve CE.
    let r = await scenario({ entitlements: { edition: 'ce', error: 'unresolved' } }, {});
    expectCe('a) edition:ce + error:unresolved', r);

    // (b1) the entitlements fetch itself rejects (offline / HTTP error).
    r = await scenario({ entitlements: { edition: 'ce' } }, { reject: true });
    expectCe('b1) fetch rejects', r);

    // (b2) the payload is null (fetch resolves null JSON).
    r = await scenario(null, {});
    expectCe('b2) payload null', r);

    // (c) positive paid edition: pro.
    r = await scenario({ entitlements: { edition: 'pro', tier_name: 'pro' } }, {});
    expectPaid('c) edition:pro', r);

    // (d) positive paid edition: saas (the live SaaS payload).
    r = await scenario({ entitlements: { edition: 'saas', tier_name: 'team' } }, {});
    expectPaid('d) edition:saas', r);

    // (e) an edition string neither set recognises -> unknown -> CE.
    r = await scenario({ entitlements: { edition: 'something-else' } }, {});
    expectCe('e) unknown edition string', r);

    console.log(`failopen-ui-gates.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})();
