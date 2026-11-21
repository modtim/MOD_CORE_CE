// FIX-KNOWLEDGE-BASE-IN-CE — the Knowledge Base tab must PAINT on CE (part 1 of 2).
// Behavioural (no browser, no npm deps): the REAL gate region is EXTRACTED VERBATIM from
// the shipped js/settings.js and EXECUTED in a fresh node:vm sandbox per scenario (edition
// lookups stubbed, DOM stubbed), so the reveal/hide decision is decided by RUNNING the
// shipped gate, not by grepping it. Style of failopen-d.check.js / failopen-gates.check.js.
//
//   node CORE/Dashboard/tests/knowledge-base-ce-tab-present.check.js
//
// Pinned contract after the fix:
//   settings.js: const _isTenantAdmin = isAdmin || currentUser.parent_user_id === null;
//                if (_isTenantAdmin) { document.getElementById('main-tab-knowledge')
//                    ?.classList.remove('hidden'); }   <- NO edition gate
//   The backend ships in CE (open-core/manifest.yaml api_allow :766 + deny_exceptions
//   :2455 overriding the :1707 deny); the real boundary is server-side
//   (routers/assistant_knowledge.py _is_tenant_admin on every /assistant/knowledge route).
//
//   1) edition 'ce'   + tenant admin -> shown
//   2) edition 'saas' + tenant admin -> shown (paid path unchanged)
//   3) edition 'pro'  + tenant admin -> shown
//   4) errored entitlements {edition:'ce', error:'unresolved'} + tenant admin -> shown
//   5) lookup REJECTS + tenant admin -> shown
//   6) member (parent_user_id 7) on 'ce'   -> hidden
//   7) member (parent_user_id 7) on 'saas' -> hidden
//   8) member (parent_user_id 7) on 'pro'  -> hidden
//   9) shipped settings.js still contains '_isTenantAdmin'
//  10) shipped settings.js no longer contains the old edition-gated reveal needle
//
// RED on the HEAD (old) settings.js: the old gate wrapped the reveal in
// `if (await isPaidEdition())`, so 1,4,5 (CE/errored/reject + tenant admin) FAIL -> rc=1.

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const SETTINGS = 'js/settings.js';

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

// One fresh sandbox per scenario. The element starts hidden (the settings gate REMOVES
// 'hidden' to reveal), matching settings.html:45 <button id="main-tab-knowledge" ... hidden>.
async function scenario(opts) {
    const els = {};
    const el = (id) => els[id] || (els[id] = (() => {
        const e = { id, _cls: new Set(['hidden']), style: { setProperty: () => {}, removeProperty: () => {} } };
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
    const isAdmin = opts.admin ? 'true' : 'false';
    const parentId = opts.member ? '7' : 'null';
    const slice = extract(SETTINGS, 'const _isTenantAdmin =', "'main-tab-knowledge')?.classList.remove('hidden');");
    const snippet =
        'const isAdmin = ' + isAdmin + '; const currentUser = { is_platform_admin: false, parent_user_id: ' + parentId + ' }; ' +
        slice + (slice.includes('.then((ent) =>') ? '}); }' : ' }');
    const vmPromise = vm.runInContext('(async () => {' + snippet + '})()', sandbox, { filename: 'settings' });
    try { await vmPromise; } catch (vmErr) { console.log('VMREJ typeof=' + typeof vmErr + ' str=' + String(vmErr && (vmErr.stack || vmErr.message || vmErr))); throw vmErr; }
    await new Promise((done) => setTimeout(done, 0));
    return els;
}

function kbShown(r) { const e = r['main-tab-knowledge']; return !!e && !e._cls.has('hidden'); }

(async () => {
    // 1) CE positively reported + tenant admin -> the tab PAINTS (the owner fix).
    let r = await scenario({ edition: 'ce', admin: true });
    check('kb ce admin: #main-tab-knowledge shown', kbShown(r) === true);
    // 2) saas + tenant admin -> paid path unchanged.
    r = await scenario({ edition: 'saas', admin: true });
    check('kb saas admin: #main-tab-knowledge shown', kbShown(r) === true);
    // 3) pro + tenant admin -> shown.
    r = await scenario({ edition: 'pro', admin: true });
    check('kb pro admin: #main-tab-knowledge shown', kbShown(r) === true);
    // 4) errored entitlements payload + tenant admin -> shown (no edition gate left).
    r = await scenario({ edition: 'ce', error: true, admin: true });
    check('kb errored admin: #main-tab-knowledge shown', kbShown(r) === true);
    // 5) entitlements lookup REJECTS + tenant admin -> shown.
    r = await scenario({ reject: true, admin: true });
    check('kb reject admin: #main-tab-knowledge shown', kbShown(r) === true);
    // 6,7,8) tenant MEMBER (parent_user_id 7) on ANY edition -> hidden (guard intact).
    r = await scenario({ member: true, edition: 'ce' });
    check('kb member ce: #main-tab-knowledge hidden', kbShown(r) === false);
    r = await scenario({ member: true, edition: 'saas' });
    check('kb member saas: #main-tab-knowledge hidden', kbShown(r) === false);
    r = await scenario({ member: true, edition: 'pro' });
    check('kb member pro: #main-tab-knowledge hidden', kbShown(r) === false);
    // 9,10) the shipped file: tenant guard present, edition gate on the reveal gone.
    const shipped = fs.readFileSync(path.join(ROOT, SETTINGS), 'utf8');
    check('shipped settings.js still has the _isTenantAdmin guard', shipped.includes('_isTenantAdmin'));
    check('shipped settings.js has no edition-gated KB reveal', !shipped.includes("if (await isPaidEdition()) document.getElementById('main-tab-knowledge')"));

    console.log(`\nknowledge-base-ce-tab-present.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})().catch((err) => { console.log('CAUGHT typeof=' + (err === undefined ? 'UNDEF' : typeof err) + ' msg=' + (err && err.message) + ' stack=' + (err && err.stack)); process.exit(1); });
