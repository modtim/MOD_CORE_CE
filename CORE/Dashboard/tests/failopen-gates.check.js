// rc20-b-r2 — the two CE edition gates on the accept pages, BEHAVIOURAL (no browser,
// no npm deps). Style of ce-signup-failclosed.check.js: the REAL gate files are
// loaded into a fresh node:vm sandbox per scenario (imports stripped, the edition
// lookup stubbed, DOM stubbed) and EXECUTED, so the gate's catch value is decided
// by RUNNING it. rc20-b's own check was a static grep (src.includes('.catch(() =>
// true)')) and survived the mutation; this one cannot.
//
//   node CORE/Dashboard/tests/failopen-gates.check.js
//
// Pinned contract (accept-invitation.js:19-22 / accept-external-invite.js:48-51):
//   const isCe = await getPlatformFeatures().then((f) => ...edition === 'ce'...)
//                    .catch(() => true);   // unknown -> CE -> BLOCK
//
//   1) both gate files still parse as valid ES modules.
//   2) accept-invitation.js, entitlements lookup REJECTS -> blocked: no click
//      listener on #accept-button (the Keycloak registration redirect is never
//      wired), button hidden, CE form path taken instead.
//   3) accept-invitation.js, entitlements edition 'saas' -> the normal accept
//      path: click listener attached EXACTLY once, button not hidden.
//   4) accept-invitation.js, entitlements edition 'ce' -> blocked (as 2).
//   5) accept-external-invite.js, lookup REJECTS -> blocked: CE message rendered
//      in #ext-error-message, #ext-error shown, externalInvites.byToken NEVER
//      called (the 404-by-denial path is not reached).
//   6) accept-external-invite.js, edition 'saas' -> normal accept path: byToken
//      called once, #ext-form-wrap shown, no error rendered.
//   7) accept-external-invite.js, edition 'ce' -> blocked (as 5).
//
// RED on the HEAD (old) copies: both gates were `.catch(() => false)`, so the
// errored scenarios (2 and 5) take the PAID path -> 5 FAIL lines, rc=1.

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const URLSearchParamsCtor = require('url').URLSearchParams;
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const INVITE = 'js/accept-invitation.js';
const EXTERNAL = 'js/accept-external-invite.js';

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

function parsesModule(rel) {
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'],
            { input: read(rel), encoding: 'utf8' });
        return true;
    } catch (e) { return false; }
}

// The gate files import their helpers; a vm runs a <script>, so drop the import
// lines and supply the same names the sandbox would see after the import.
function stripImports(src) { return src.replace(/^import[^\n]*;[ \t]*$/gm, ''); }

function mkEl(id) {
    const el = { id, textContent: '', value: '', disabled: false, style: {}, listeners: [], _cls: new Set() };
    el.classList = {
        add(c) { el._cls.add(c); },
        remove(c) { el._cls.delete(c); },
        toggle(c, on) { if (on) el._cls.add(c); else el._cls.delete(c); },
        contains(c) { return el._cls.has(c); },
    };
    el.addEventListener = (type, fn) => { el.listeners.push(type); };
    return el;
}

// One fresh sandbox per scenario: the edition lookup is stubbed (rejects / paid /
// CE) and the DOM records every listener attach + section visibility.
async function scenario(file, opts) {
    const els = {};
    const el = (id) => els[id] || (els[id] = mkEl(id));
    const handlers = [];
    const calls = { byToken: 0, accept: 0, getRegistrationUrl: 0 };
    const gp = opts.reject
        ? () => Promise.reject(new Error('entitlements lookup failed'))
        : () => Promise.resolve({ entitlements: { edition: opts.edition } });
    // A vm context gets no globals from the outer realm: the gate files use the
    // Web global URLSearchParams (and the browser's DOM/btoa), so supply them.
    const sandbox = {
        URLSearchParams: URLSearchParamsCtor,
        btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
        document: {
            getElementById: (id) => el(id),
            addEventListener: (t, fn) => { if (t === 'DOMContentLoaded') handlers.push(fn); },
        },
        window: {
            APP_CONFIG: null,
            location: { search: '?token=inv-token-1', origin: 'http://localhost', href: '' },
            dispatchEvent: () => {},
        },
        fetch: () => Promise.resolve({
            ok: true, status: 200, statusText: 'OK',
            json: () => Promise.resolve({ email: 'invited@invalid.invalid', id: 'inv-1' }),
        }),
        getPlatformFeatures: gp,
        getRegistrationUrl: () => { calls.getRegistrationUrl++; return Promise.resolve('http://localhost/realms/r/protocol/openid-connect/registrations'); },
        handleLogin: () => {},
        externalInvites: {
            byToken: async () => { calls.byToken++; return { id: 'inv-1', cog_name: 'a shared folder', permission_level: 'viewer', email: 'invited@invalid.invalid' }; },
            accept: async () => { calls.accept++; return { status: 'accepted' }; },
        },
        console: console,
    };
    vm.createContext(sandbox);
    await vm.runInContext(stripImports(read(file)), sandbox, { filename: path.basename(file) });
    if (handlers.length !== 1) {
        console.error(`FAIL ${file} registers exactly one DOMContentLoaded handler`);
        process.exit(1);
    }
    for (const h of handlers) await h();
    return { els, calls };
}

function clicks(r, id) {
    const btn = r.els[id];
    if (!btn) return -1;
    return btn.listeners.filter((t) => t === 'click').length;
}
function shown(r, id) { const e = r.els[id]; return !!e && !e._cls.has('hidden'); }
function errText(r, id) { const e = r.els[id]; return e ? String(e.textContent) : ''; }

(async () => {
    check('accept-invitation.js parses as a valid ES module', parsesModule(INVITE));
    check('accept-external-invite.js parses as a valid ES module', parsesModule(EXTERNAL));

    // --- accept-invitation.js -------------------------------------------------
    // 2) errored entitlements (main.py's failure stub) must BLOCK: the paid path
    //    would hand the invitee a Keycloak registration redirect that CE denies.
    let r = await scenario(INVITE, { reject: true });
    check('accept-invitation errored: #accept-button click listener NOT wired', clicks(r, 'accept-button') === 0);
    check('accept-invitation errored: #accept-button hidden (CE form path taken)', r.els['accept-button'].style.display === 'none');

    // 3) paid edition -> the normal accept path, listener attached exactly once.
    r = await scenario(INVITE, { edition: 'saas' });
    check('accept-invitation saas: normal accept path, click listener attached exactly once', clicks(r, 'accept-button') === 1);
    check('accept-invitation saas: #accept-button not hidden', r.els['accept-button'].style.display !== 'none');

    // 4) CE positively reported -> blocked, same as the errored case.
    r = await scenario(INVITE, { edition: 'ce' });
    check('accept-invitation ce: #accept-button click listener NOT wired', clicks(r, 'accept-button') === 0);
    check('accept-invitation ce: #accept-button hidden', r.els['accept-button'].style.display === 'none');

    // --- accept-external-invite.js --------------------------------------------
    // 5) errored entitlements must BLOCK before the by-token lookup (the endpoint
    //    is denied on CE, so the paid path can only ever 404 there).
    r = await scenario(EXTERNAL, { reject: true });
    check('accept-external-invite errored: CE message rendered in #ext-error-message', errText(r, 'ext-error-message') !== '');
    check('accept-external-invite errored: #ext-error section shown', shown(r, 'ext-error'));
    check('accept-external-invite errored: externalInvites.byToken never called', r.calls.byToken === 0);

    // 6) paid edition -> the normal accept path.
    r = await scenario(EXTERNAL, { edition: 'saas' });
    check('accept-external-invite saas: by-token lookup performed once', r.calls.byToken === 1);
    check('accept-external-invite saas: #ext-form-wrap shown', shown(r, 'ext-form-wrap'));
    check('accept-external-invite saas: no error rendered', errText(r, 'ext-error-message') === '');

    // 7) CE positively reported -> blocked.
    r = await scenario(EXTERNAL, { edition: 'ce' });
    check('accept-external-invite ce: CE message rendered in #ext-error-message', errText(r, 'ext-error-message') !== '');
    check('accept-external-invite ce: #ext-error section shown', shown(r, 'ext-error'));
    check('accept-external-invite ce: externalInvites.byToken never called', r.calls.byToken === 0);

    console.log(`\nfailopen-gates.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})().catch((err) => { console.error('FAIL ' + String(err && err.message)); process.exit(1); });
