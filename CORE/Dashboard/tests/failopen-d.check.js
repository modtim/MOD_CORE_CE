// rc20 failopen-d — the CE edition gates on the Settings page (Knowledge Base tab,
// settings.js) and the Users page (Identity & Access tab carve, users.js), BEHAVIOURAL
// (no browser, no npm deps). Style of failopen-gates.check.js: the REAL gate regions are
// EXTRACTED VERBATIM from the shipped files and EXECUTED in a fresh node:vm sandbox per
// scenario (edition lookups stubbed, DOM stubbed), so the gate's reveal/hide decision is
// decided by RUNNING it, not by grepping it.
//
//   node CORE/Dashboard/tests/failopen-d.check.js
//
// Pinned contract after FIX-KNOWLEDGE-BASE-IN-CE (re-pinned 11-07):
//   settings.js: reveal #main-tab-knowledge for TENANT ADMINS on EVERY edition — the
//                edition gate is dropped (the backend ships in CE: manifest.yaml
//                api_allow :766 + deny_exceptions :2455 overrides the :1707 deny).
//                The _isTenantAdmin guard (isAdmin || parent_user_id === null) stays.
//   users.js:    _ceEdition = !(await isPaidEdition()); if (_ceEdition) hide #main-tab-identity
//   (api.js isPaidEdition is fail closed: only a positive 'saas'/'pro' counts as paid;
//    an errored entitlements payload {edition:'ce', error:'unresolved'} -> false.)
//
//   1) settings.js Knowledge Base tab, errored entitlements, tenant admin -> shown
//   2) settings.js Knowledge Base tab, lookup REJECTS, tenant admin       -> shown
//   3) settings.js Knowledge Base tab, edition 'ce', tenant admin         -> shown
//   4) settings.js Knowledge Base tab, edition 'saas', tenant admin       -> shown
//   5) settings.js Knowledge Base tab, edition 'pro', tenant admin        -> shown
//   6) settings.js Knowledge Base tab, member (parent_user_id 7), ANY     -> hidden
//   7) users.js Identity & Access tab, errored entitlements  -> hidden
//   8) users.js Identity & Access tab, lookup REJECTS        -> hidden
//   9) users.js Identity & Access tab, edition 'ce'          -> hidden
//  10) users.js Identity & Access tab, edition 'saas'        -> shown
//  11) users.js Identity & Access tab, edition 'pro'         -> shown
//
// RED on the HEAD (old) copies: the old settings gate wrapped the reveal in
// `if (await isPaidEdition())`, so scenarios 1,2,3 (CE/errored/reject + tenant admin)
// and 6 (member on the old file still hidden -> passes, but the old file has no member
// scenario) — net: 1,2,3 FAIL -> rc=1. The users.js half is byte-identical.

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const SETTINGS = 'js/settings.js';
const USERS = 'js/users.js';

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// Slice the shipped source between two unique markers (inclusive of the end line).
function extract(rel, startMarker, endMarker) {
    const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    const a = src.indexOf(startMarker);
    if (a < 0) throw new Error(`${startMarker} not found in ${rel}`);
    const b = src.indexOf(endMarker, a);
    if (b < 0) throw new Error(`${endMarker} not found in ${rel}`);
    const eol = src.indexOf('\n', b + endMarker.length);
    return src.slice(a, eol < 0 ? src.length : eol);
}

// One fresh sandbox per scenario. The element starts hidden (settings gate REMOVES
// 'hidden' to reveal); the identity tab starts shown (users gate SETS display:none).
async function scenario(kind, opts) {
    const els = {};
    const mkStyle = () => {
        const s = { _props: {} };
        s.setProperty = (k, v) => { s._props[k] = v; if (k === 'display') s.display = v; };
        s.removeProperty = (k) => { delete s._props[k]; if (k === 'display') s.display = undefined; };
        return s;
    };
    const el = (id) => els[id] || (els[id] = (() => {
        const e = { id, _cls: new Set(id === 'main-tab-knowledge' ? ['hidden'] : []), style: mkStyle() };
        e.classList = {
            add(c) { e._cls.add(c); },
            remove(c) { e._cls.delete(c); },
            contains(c) { return e._cls.has(c); },
        };
        return e;
    })());
    const paid = opts.reject ? false : (opts.edition === 'saas' || opts.edition === 'pro');
    const gp = opts.reject
        ? () => Promise.reject(new Error('entitlements lookup failed'))
        : () => Promise.resolve({ entitlements: { edition: opts.edition, ...(opts.error ? { error: 'unresolved' } : {}) } });
    const sandbox = {
        document: { getElementById: (id) => el(id) },
        isPaidEdition: () => Promise.resolve(paid),
        getPlatformFeatures: gp,
        console: console,
    };
    vm.createContext(sandbox);
    let snippet;
    if (kind === 'settings') {
        snippet =
            'const isAdmin = ' + (opts.admin ? 'true' : 'false') + '; const currentUser = { is_platform_admin: false, parent_user_id: ' + (opts.member ? '7' : 'null') + ' }; ' +
            (() => {
                const slice = extract(SETTINGS, 'const _isTenantAdmin =', "'main-tab-knowledge')?.classList.remove('hidden');");
                // The OLD gate wraps the reveal in an un-awaited .then chain; close it too.
                return slice + (slice.includes('.then((ent) =>') ? '}); }' : ' }');
            })();
    } else {
        const slice = extract(USERS, 'R37.4 part 2 — reveal', "document.getElementById('main-tab-identity')?.style.setProperty('display', 'none');");
        snippet = 'let ' + slice.slice(slice.indexOf('_ceEdition'));
    }
    const vmPromise = vm.runInContext('(async () => {' + snippet + '})()', sandbox, { filename: kind });
    try { await vmPromise; } catch (vmErr) { console.log('VMREJ typeof=' + (vmErr === undefined ? 'UNDEF' : typeof vmErr) + ' str=' + String(vmErr && (vmErr.stack || vmErr.message || vmErr))); throw vmErr; }
    // The OLD settings gate chains un-awaited .then() callbacks; drain the
    // microtask queue (one macrotask tick) before asserting on the DOM.
    await new Promise((done) => setTimeout(done, 0));
    return els;
}

function kbShown(r) { const e = r['main-tab-knowledge']; return !!e && !e._cls.has('hidden'); }
function identityHidden(r) { const e = r['main-tab-identity']; return !!e && e.style.display === 'none'; }

(async () => {
    // --- settings.js: Knowledge Base tab --------------------------------------
    // RE-PINNED by FIX-KNOWLEDGE-BASE-IN-CE: the edition gate is gone, so a TENANT ADMIN
    // sees the tab on every edition (the backend ships in CE per manifest api_allow :766
    // + deny_exceptions :2455). The tenant guard is the fail-closed half.
    // 1) errored entitlements + tenant admin -> shown (no edition gate left).
    let r = await scenario('settings', { edition: 'ce', error: true });
    check('settings KB errored: #main-tab-knowledge shown', kbShown(r) === true);
    // 2) lookup REJECTS + tenant admin -> shown.
    r = await scenario('settings', { reject: true });
    check('settings KB reject: #main-tab-knowledge shown', kbShown(r) === true);
    // 3) CE positively reported + tenant admin -> shown (the owner fix).
    r = await scenario('settings', { edition: 'ce' });
    check('settings KB ce: #main-tab-knowledge shown', kbShown(r) === true);
    // 4) saas -> the paid path unchanged: tab revealed.
    r = await scenario('settings', { edition: 'saas' });
    check('settings KB saas: #main-tab-knowledge shown', kbShown(r) === true);
    // 5) pro -> shown.
    r = await scenario('settings', { edition: 'pro' });
    check('settings KB pro: #main-tab-knowledge shown', kbShown(r) === true);
    // 6) tenant MEMBER (parent_user_id 7) on ANY edition -> hidden (guard intact).
    r = await scenario('settings', { member: true, edition: 'ce' });
    check('settings KB member ce: #main-tab-knowledge hidden', kbShown(r) === false);
    r = await scenario('settings', { member: true, edition: 'saas' });
    check('settings KB member saas: #main-tab-knowledge hidden', kbShown(r) === false);

    // --- users.js: Identity & Access tab ---------------------------------------
    // 6) errored entitlements must keep the CE carve: identity tab hidden.
    r = await scenario('users', { edition: 'ce', error: true });
    check('users identity errored: #main-tab-identity hidden', identityHidden(r) === true);
    // 7) lookup REJECTS -> hidden.
    r = await scenario('users', { reject: true });
    check('users identity reject: #main-tab-identity hidden', identityHidden(r) === true);
    // 8) CE positively reported -> hidden.
    r = await scenario('users', { edition: 'ce' });
    check('users identity ce: #main-tab-identity hidden', identityHidden(r) === true);
    // 9) saas -> paid path unchanged: tab shown.
    r = await scenario('users', { edition: 'saas' });
    check('users identity saas: #main-tab-identity shown', identityHidden(r) === false);
    // 10) pro -> shown.
    r = await scenario('users', { edition: 'pro' });
    check('users identity pro: #main-tab-identity shown', identityHidden(r) === false);

    console.log(`\nfailopen-d.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})().catch((err) => { console.log('CAUGHT typeof=' + (err === undefined ? 'UNDEF' : typeof err) + ' msg=' + (err && err.message) + ' stack=' + (err && err.stack)); process.exit(1); });
