// R301-r3 — deterministic CE Sign Up posture, BEHAVIOURAL (no browser, no npm deps).
// Style of tests/admin-scim-token-lock.check.js: the REAL code is lifted out of
// js/api.js + js/ui.js and EXECUTED in a fresh node:vm sandbox per scenario, with
// `fetch` stubbed and a minimal DOM stub. R301-r2's witness was static source-text
// only, so the mutation `if (ent.error) return true;` inside _registrationAllowed
// survived it (every regex pin still matched). This one cannot: it runs the gate.
//
//   node CORE/Dashboard/tests/ce-signup-failclosed.check.js
//
// Pinned behaviour (the fail-closed contract of ui.js:2366-2386):
//   1) js/ui.js parses as a valid ES module and login.html still ships #signup-button.
//   2) CE + errored entitlements block  -> hidden, NO click listener.
//   3) fetch rejects                    -> hidden, NO click listener.
//   4) CE, no registration_allowed key  -> hidden, NO click listener.
//   5) saas success, no registration_allowed key -> SHOWN, listener attached EXACTLY once.
//   6) saas registration_allowed:false  -> hidden, NO click listener.
//   7) registration_allowed:true        -> SHOWN, listener attached exactly once.
//
// RED on the HEAD (old) ui.js (no _registrationAllowed helper to lift) and RED on
// the `if (ent.error) return true;` mutation (case 2 reveals).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const ui = read('js/ui.js');
const api = read('js/api.js');
const html = read('pages/login.html');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// --- 1) structural: the module still parses and the button is still shipped ----
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: ui, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
check('ui.js parses as a valid ES module', !modErr);
check('login.html still ships #signup-button', /id="signup-button"/.test(html));

// Lift the real code under test (no re-implementation).
function lift(src, marker) {
    const i = src.indexOf(marker);
    if (i < 0) return null;
    const j = src.indexOf('\n}', i);
    if (j < 0) return null;
    // vm runs a <script>, not a module: drop the `export` keyword (the only
    // thing the lift can carry that a script cannot parse).
    return src.slice(i, j + 2).replace(/^export /gm, '');
}
// api.js: cache + TTL + getPlatformFeatures are contiguous (312-334).
const apiLift = lift(api, 'const _featuresCache = {');
const regLift = lift(ui, 'function _registrationAllowed()');
const revealLift = lift(ui, 'function _revealSignupButtonWhenRegistrationAllowed()');
if (!apiLift || !regLift || !revealLift) {
    console.error('FAIL could not lift getPlatformFeatures/_registrationAllowed/'
        + '_revealSignupButtonWhenRegistrationAllowed — the gate is not in the fail-closed shape');
    process.exit(1);
}

// --- the scenario runner -------------------------------------------------------
// Fresh sandbox per scenario: api.js caches the payload, so each case needs a clean
// cache. The DOM stub records the display value and every addEventListener attach.
async function scenario(payload, opts) {
    const els = {};
    const el = (id) => els[id] || (els[id] = {
        id: id,
        display: '',
        listeners: [],
        style: {
            setProperty(p, v) { if (p === 'display') els[id].display = v; },
            removeProperty(p) { if (p === 'display') els[id].display = ''; },
        },
        addEventListener(type, fn) { els[id].listeners.push(type); },
    });
    const sandbox = {
        document: { getElementById: (id) => el(id) },
        window: {},
        handleSignup: () => {},
        console: console,
        fetch: opts.reject
            ? () => Promise.reject(new Error('offline'))
            : () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
        sleep: (ms) => new Promise((finish) => setTimeout(finish, ms)),
    };
    // api.js resolves API_BASE_URL from window.APP_CONFIG at module top (line 3),
    // which is not part of the lift — provide the same default the browser sees.
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + apiLift + '\n' + regLift + '\n' + revealLift + '\n'
        + `(async () => { _revealSignupButtonWhenRegistrationAllowed(); await sleep(25); })()`;
    vm.createContext(sandbox);
    await vm.runInContext(src, sandbox, { filename: 'ce-signup-gate.js' });
    const btn = el('signup-button');
    const clicks = btn.listeners.filter((t) => t === 'click').length;
    return { hidden: btn.display === 'none', shown: btn.display !== 'none', clicks: clicks };
}

function expectHidden(label, r) {
    check(`${label}: #signup-button hidden`, r.hidden);
    check(`${label}: no click listener`, r.clicks === 0);
}
function expectShown(label, r) {
    check(`${label}: #signup-button revealed`, r.shown);
    check(`${label}: click listener attached exactly once`, r.clicks === 1);
}

(async () => {
    // 2) CE with an UNRESOLVED entitlements block (main.py's error stub) — the exact
    //    path the old fail-open gate left Sign Up clickable. Must stay hidden.
    let r = await scenario({ edition: 'ce', entitlements: { error: 'unresolved' } }, {});
    expectHidden('ce + entitlements error', r);

    // 3) the entitlements fetch itself rejects (offline / HTTP error).
    r = await scenario({ entitlements: { edition: 'ce' } }, { reject: true });
    expectHidden('fetch rejects', r);

    // 4) CE, payload readable but no registration_allowed key (edition fallback).
    r = await scenario({ entitlements: { edition: 'ce', tier_name: 'core' } }, {});
    expectHidden('ce, no registration_allowed key', r);

    // 5) paid success payload (main.py sends NO registration_allowed key) — the
    //    edition fallback must reveal, and only once (no double attach, no leak).
    r = await scenario({ entitlements: { edition: 'saas', tier_name: 'team' } }, {});
    expectShown('saas success, no registration_allowed key', r);

    // 6) CE ships registration_allowed:false (core-realm.json) — server says no.
    r = await scenario({ entitlements: { edition: 'saas', registration_allowed: false } }, {});
    expectHidden('registration_allowed:false', r);

    // 7) the server positively allows registration.
    r = await scenario({ entitlements: { edition: 'saas', registration_allowed: true } }, {});
    expectShown('registration_allowed:true', r);

    console.log(`ce-signup-failclosed.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})();
