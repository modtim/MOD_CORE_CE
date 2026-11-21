// Node-runnable check for the GSEC FX.112 p4a Active Sessions page
// (CORE/Dashboard/js/active-sessions.js). active-sessions.js is an ES module,
// so this script extracts the body (from the module state `let _sessions = [];`
// to EOF, with the `export ` keyword stripped) and runs it in a vm sandbox with
// a tiny DOM (id/className/textContent/innerHTML/disabled/dataset/attrs,
// createElement/createTextNode/getElementById) plus me (api.js: getSessions /
// getPermissions / get / revokeSession), fetchApi and a confirm() stub. Then it
// asserts:
//   (a) render: one row per session (device shortened from the stored UA, IP,
//       started, last active, a "This session" badge, a Sign out button),
//       newest-active first, count line shown;
//   (b) current-session marking: the API has no is_current field today, so the
//       newest-active row — exactly the row me_auth_apikeys_service.revoke_all_other_sessions
//       keeps — is marked; an explicit is_current flag is honoured when present;
//   (c) revoke calls the right endpoint: DELETE /me/sessions/{id} for that row
//       only (me.revokeSession), and the row's own id is what ships;
//   (d) confirm dialog: a declined confirm sends nothing;
//   (e) revoking the CURRENT session ends the page (read-only, no further
//       writes) because the browser's own token is dead;
//   (f) sign out everywhere POSTs /me/signout-everywhere once, reports
//       revoked_sessions + reauth_required, and never re-GETs;
//   (g) tenant-admin picker: offered only when USER:deactivate is held; rows
//       exclude the caller; revoke-user POSTs /users/{id}/signout-everywhere;
//       the picker is absent for a plain user;
//   (h) 403 -> read-only: notice shown, every control disabled + aria-disabled,
//       a later action sends nothing;
//   (i) empty state + GET failure surfaced verbatim;
//   (j) escaping: a hostile device_info/IP cannot inject markup.
//
//   node --test CORE/Dashboard/tests/active-sessions-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'active-sessions.js'), 'utf8');
const start = src.indexOf('let _sessions = [];');
assert.ok(start >= 0, 'could not locate the active-sessions body');
const block = src.slice(start).replace(/export async function/g, 'async function')
    .replace(/export function/g, 'function');

const IDS = ['as-error', 'as-notice', 'as-count', 'as-empty', 'as-list',
    'as-table-body', 'as-self', 'as-signout-everywhere-btn', 'as-self-status',
    'as-picker', 'as-picker-body'];

function mkErr(status, detail) {
    return { status, detail, message: typeof detail === 'string' ? detail : (detail && detail.message) || '' };
}

function run(sessions, perms, users, selfId, opts = {}) {
    const created = [];

    function mkEl(tag, id) {
        const el = { tag, id: id || '', className: '', textContent: '',
            innerHTML: '', disabled: false, checked: null, value: null,
            dataset: {}, attrs: {}, children: [], listeners: {} };
        el.classList = { add: (c) => { el.className = `${el.className} ${c}`.trim(); },
            remove: (c) => { el.className = el.className.split(/\s+/)
                .filter((x) => x !== c).join(' '); },
            toggle: () => {} };
        el.appendChild = (child) => { el.children.push(child); return child; };
        el.setAttribute = (k, v) => { el.attrs[k] = v; };
        el.getAttribute = (k) => el.attrs[k];
        el.removeAttribute = (k) => { delete el.attrs[k]; };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.querySelectorAll = () => [];
        el.querySelector = () => null;
        created.push(el);
        return el;
    }

    const els = {};
    for (const id of IDS) els[id] = mkEl('div', id);
    els['as-table-body'].tag = 'tbody';
    // The shipped page carries `hidden` on these three panels; the sandbox must
    // start from the same classes or the hidden-state asserts lie.
    // The shipped page carries `hidden` on these four panels (the picker panel
    // starts hidden and js/active-sessions.js unhides it only when USER:deactivate
    // is held), so the sandbox must start from the same classes.
    ['as-error', 'as-notice', 'as-empty', 'as-picker'].forEach((id) => {
        els[id].className = 'hidden';
    });
    // The self-service button ships in the markup, disabled until wired.
    els['as-signout-everywhere-btn'].tag = 'button';
    els['as-signout-everywhere-btn'].disabled = true;

    const doc = {
        getElementById: (id) => created.find((e) => e.id === id),
        createElement: (tag) => mkEl(tag, ''),
        createTextNode: (text) => ({ text }),
        querySelectorAll: () => [],
        querySelector: () => null,
    };

    const calls = { get: [], revoke: [], revokeUser: [], everywhere: [], users: [] };
    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        document: doc,
        Date: globalThis.Date,
        isNaN: globalThis.isNaN,
        JSON: globalThis.JSON,
        Set: globalThis.Set,
        String: globalThis.String,
        Number: globalThis.Number,
        Array: globalThis.Array,
        confirm: () => opts.confirm !== false,
        me: {
            getSessions: () => { calls.get.push('/me/sessions'); return Promise.resolve(sessions()); },
            getPermissions: () => Promise.resolve(perms()),
            get: () => Promise.resolve({ id: selfId }),
            revokeSession: (sid) => {
                calls.revoke.push(sid);
                const res = opts.revokeModel(sid);
                if (res && res.status) return Promise.reject(mkErr(res.status, res.detail));
                return Promise.resolve(null);
            },
        },
        fetchApi: (endpoint, options = {}) => {
            if (endpoint === '/users/') {
                calls.users.push(endpoint);
                return Promise.resolve(users());
            }
            if (endpoint.endsWith('/signout-everywhere')) {
                calls.everywhere.push(endpoint);
                const res = opts.everywhereModel(endpoint);
                if (res && res.status) return Promise.reject(mkErr(res.status, res.detail));
                return Promise.resolve(res);
            }
            return Promise.reject(mkErr(500, 'unexpected endpoint'));
        },
    };

    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeActiveSessionsPage, loadSessions, renderRows, _rowNode, _pickerNode, handleRevoke, handleSignOutEverywhere, handleRevokeUser, wireSelfButton, _rerender, _goReadOnly, _currentOf, _deviceText, _errText });`,
        sandbox, { filename: 'active-sessions-page-body' });

    function ser(el) {
        const esc = (s) => String(s).replace(/&/g, '&amp;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');
        if (el.text != null) return esc(el.text);
        const attrs = [];
        if (el.id) attrs.push(`id="${el.id}"`);
        if (el.className) attrs.push(`class="${el.className}"`);
        Object.keys(el.attrs).forEach((k) => attrs.push(`${k}="${esc(el.attrs[k])}"`));
        Object.keys(el.dataset).forEach((k) => attrs.push(`data-${k}="${esc(el.dataset[k])}"`));
        if (el.checked != null) attrs.push(`checked=${el.checked}`);
        if (el.value != null && el.value !== '') attrs.push(`value="${el.value}"`);
        if (el.disabled) attrs.push('disabled');
        const open = `<${el.tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>`;
        const own = el.textContent ? esc(el.textContent) : '';
        const inner = el.children.map(ser).join('');
        return `${open}${own}${inner}</${el.tag}>`;
    }

    return { fns, els, calls, doc, ser, created };
}

const SESSIONS = [
    { id: 41, device_info: 'Mozilla/5.0 (Windows NT 10.0) Chrome/120.0',
      ip_address: '203.0.113.10', created_at: '2026-10-06T09:00:00+00:00',
      last_active_at: '2026-10-07T11:30:00+00:00', keycloak_session_id: 'kc-a' },
    { id: 37, device_info: 'Mozilla/5.0 (Macintosh) Firefox/131',
      ip_address: '203.0.113.11', created_at: '2026-10-01T08:00:00+00:00',
      last_active_at: '2026-10-05T07:15:00+00:00', keycloak_session_id: 'kc-b' },
];
const ADMIN_PERMS = [{ name: 'user:view' }, { name: 'user:deactivate' },
    { name: 'tenant:view_settings' }];
const PLAIN_PERMS = [{ name: 'user:view' }, { name: 'workflow:run' }];
const TENANT_USERS = [{ id: 7, username: 'alice', email: 'a@x.io' },
    { id: 9, username: 'bob' }, { id: 3, username: 'me' }];

function okRevoke() { return null; }
function okEverywhere(endpoint) {
    return { signed_out: true, revoked_sessions: 2, keycloak_revoked: true,
        reauth_required: true, user_id: endpoint.startsWith('/users/') ? 9 : null };
}

test.describe('active-sessions-page', () => {
    test('render: rows, current-session badge, sign-out buttons', async () => {
        const { fns, els, ser, calls } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const body = ser(els['as-table-body']);
        assert.strictEqual(calls.get.length, 1, 'one GET /me/sessions');
        assert.ok(body.includes('Chrome on Windows'), 'UA shortened for row 1');
        assert.ok(body.includes('Firefox on macOS'), 'UA shortened for row 2');
        assert.ok(body.includes('203.0.113.10'), 'IP renders');
        assert.ok(body.includes('2026-10-06 09:00'), 'started renders');
        assert.ok(body.includes('2026-10-07 11:30'), 'last active renders');
        assert.ok(body.includes('This session'), 'current session marked');
        assert.ok(body.includes('data-asSessionId="41"'), 'row keyed to its session id');
        assert.ok(body.includes('Sign out this session (#41)'), 'current row named as such');
        assert.ok(body.includes('Sign out session #37'), 'other row keyed by id');
        assert.strictEqual(els['as-count'].textContent, '2 session(s) on your account');
        assert.ok(els['as-empty'].className.includes('hidden'), 'no empty panel with rows');
    });

    test('current-session marking follows the API\'s own keep-one policy', async () => {
        const { fns, els, ser } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => PLAIN_PERMS, () => [], 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const body = ser(els['as-table-body']);
        assert.ok(fns._currentOf(SESSIONS) === '41', 'newest-active row is the current one');
        assert.ok(body.includes('Sign out this session (#41)'), 'row 0 marked');
        assert.ok(!body.includes('Sign out this session (#37)'), 'row 1 not marked');
    });

    test('an explicit is_current flag wins when the API ships one', async () => {
        const flagged = SESSIONS.map((s) => ({ ...s, is_current: s.id === 37 }));
        const { fns, els, ser } = run(() => flagged, () => PLAIN_PERMS, () => [], 3,
            { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const body = ser(els['as-table-body']);
        assert.ok(body.includes('Sign out this session (#37)'), 'flagged row marked, not row 0');
        assert.ok(!body.includes('Sign out this session (#41)'));
    });

    test('revoke calls DELETE /me/sessions/{id} for that row only', async () => {
        const { fns, calls, created } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.asSessionId === '37');
        await fns.handleRevoke(btn);
        assert.strictEqual(calls.revoke.length, 1, 'exactly one revoke');
        assert.strictEqual(calls.revoke[0], '37', 'the row\'s own session id shipped');
        assert.strictEqual(calls.get.length, 2, 'the list is refreshed after a non-current revoke');
    });

    test('confirm dialog: declining sends nothing', async () => {
        const { fns, calls, created } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3,
            { confirm: false, revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.asSessionId === '37');
        await fns.handleRevoke(btn);
        assert.strictEqual(calls.revoke.length, 0, 'no DELETE after a declined confirm');
        await fns.handleSignOutEverywhere(fns.wireSelfButton());
        assert.strictEqual(calls.everywhere.length, 0, 'no POST either');
    });

    test('revoking the CURRENT session ends the page (token is dead)', async () => {
        const { fns, calls, created, els } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.asSessionId === '41');
        await fns.handleRevoke(btn);
        assert.strictEqual(calls.revoke.length, 1, 'the DELETE landed');
        assert.strictEqual(calls.get.length, 1, 'no re-GET with a dead token');
        assert.ok(els['as-notice'].textContent.includes('read-only until you sign in again'));
        assert.ok(els['as-self-status'].textContent.includes('the one you are using'),
            'the status line names the dead session');
        const laterBtn = created.filter((e) => e.tag === 'button' && e.dataset.asSessionId === '37').pop();
        await fns.handleRevoke(laterBtn);
        assert.strictEqual(calls.revoke.length, 1, 'a later Revoke sends nothing');
        assert.ok(els['as-error'].textContent.includes('read-only'));
    });

    test('sign out everywhere POSTs /me/signout-everywhere once, never re-GETs', async () => {
        const { fns, calls, els, created } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const btn = created.find((e) => e.tag === 'button' && e.id === 'as-signout-everywhere-btn');
        assert.ok(btn.disabled === false, 'button enabled while sessions exist');
        assert.ok(btn.dataset.wired === '1', 'wired exactly once by the page');
        await fns.handleSignOutEverywhere(btn);
        assert.strictEqual(calls.everywhere.length, 1, 'one POST');
        assert.strictEqual(calls.get.length, 1, 'no re-GET after signing the current session out');
        assert.ok(els['as-self-status'].textContent.includes('2 session(s) ended'),
            'revoked_sessions reported');
        assert.ok(els['as-self-status'].textContent.includes('you must sign in again'),
            'reauth_required surfaced');
        assert.ok(btn.disabled === true && btn.attrs['aria-disabled'] === 'true',
            'the button is dead after the call');
        await fns.handleSignOutEverywhere(btn);
        assert.strictEqual(calls.everywhere.length, 1, 'a second click sends nothing');
    });

    test('tenant-admin picker: only with USER:deactivate, never the caller', async () => {
        const { fns, els, ser, calls } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const picker = ser(els['as-picker-body']);
        assert.ok(picker.includes('alice (user #7)'), 'tenant user offered');
        assert.ok(picker.includes('bob (user #9)'), 'second tenant user offered');
        assert.ok(!picker.includes('me (user #3)'), 'the caller is never offered');
        assert.ok(picker.includes('data-asUserId="9"'), 'picker button keyed to the user');
        assert.ok(els['as-picker'].className.includes('hidden') === false,
            'the admin panel is shown when USER:deactivate is held');
        assert.strictEqual(calls.users.length, 1, 'one GET /users/ for the picker');

        const btn = (await Promise.resolve(els['as-picker-body'])).children
            .find((c) => c && c.children.some((x) => x && x.dataset.asUserId === '9'))
            .children.find((x) => x && x.dataset.asUserId === '9');
        await fns.handleRevokeUser(btn);
        assert.strictEqual(calls.everywhere.length, 1, 'revoke-user POSTs signout-everywhere');
        assert.strictEqual(calls.everywhere[0], '/users/9/signout-everywhere',
            'the tenant-admin route, not the self-service one');
        assert.ok(els['as-self-status'].textContent.includes('User #9: 2 session(s) ended'));
    });

    test('plain user: no picker rows, revoke-user refused client-side', async () => {
        const { fns, els, ser, calls } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => PLAIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        assert.strictEqual(calls.users.length, 0, 'no /users/ fetch without the capability');
        assert.strictEqual(ser(els['as-picker-body']).replace(/<\/?div[^>]*>/g, ''), '',
            'picker body empty for a plain user');
        const hand = (await Promise.resolve(els['as-picker-body'])).children;
        assert.strictEqual(hand.length, 0, 'no picker controls exist');
        assert.ok(els['as-picker'].className.includes('hidden'),
            'the admin panel is hidden for a plain user');
    });

    test('403 -> read-only: notice, controls disabled, nothing re-sent', async () => {
        const forbidden = { status: 403, detail: 'Missing required capability: USER:deactivate' };
        const { fns, calls, created, els } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3,
            { revokeModel: () => forbidden, everywhereModel: () => forbidden });
        await fns.initializeActiveSessionsPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.asSessionId === '37');
        await fns.handleRevoke(btn);
        assert.strictEqual(calls.revoke.length, 1, 'the DELETE was attempted once');
        assert.ok(els['as-notice'].textContent.includes('cannot revoke them'),
            'the read-only notice is shown');
        assert.ok(els['as-notice'].className.includes('hidden') === false);
        const disabled = created.filter((e) => e.attrs['aria-disabled'] === 'true');
        assert.ok(disabled.length >= 4, 'every session + picker control is aria-disabled');
        const selfBtn = created.find((e) => e.id === 'as-signout-everywhere-btn');
        await fns.handleSignOutEverywhere(selfBtn);
        assert.strictEqual(calls.everywhere.length, 0, 'the self-service call sends nothing once read-only');
        const pickerBtn = created.find((e) => e.dataset.asAction === 'revoke-user');
        await fns.handleRevokeUser(pickerBtn);
        assert.strictEqual(calls.everywhere.length, 0, 'nor does the tenant-admin one');
        assert.ok(els['as-count'].textContent.includes('read-only'));
    });

    test('404 on a stale row surfaced verbatim', async () => {
        const { fns, calls, created, els } = run(() => SESSIONS.map((s) => ({ ...s })),
            () => ADMIN_PERMS, () => TENANT_USERS, 3,
            { revokeModel: (sid) => ({ status: 404, detail: 'Session not found' }),
              everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.asSessionId === '37');
        await fns.handleRevoke(btn);
        assert.strictEqual(calls.revoke.length, 1, 'the DELETE was attempted');
        assert.ok(els['as-error'].textContent.includes('Session not found'),
            'the server detail is shown, not swallowed');
        assert.ok(els['as-notice'].className.includes('hidden'), 'not read-only on a 404');
    });

    test('empty state: no sessions -> empty panel, button disabled', async () => {
        const { fns, els, ser, calls } = run(() => [], () => ADMIN_PERMS, () => TENANT_USERS, 3,
            { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        assert.strictEqual(ser(els['as-table-body']).replace(/<\/?tbody[^>]*>/g, ''), '');
        assert.ok(els['as-empty'].className.includes('hidden') === false);
        assert.strictEqual(els['as-count'].textContent, '0 session(s) on your account');
        const btn = els['as-signout-everywhere-btn'];
        assert.ok(btn.disabled === true && btn.attrs['aria-disabled'] === 'true',
            'nothing to sign out everywhere');
    });

    test('GET failure surfaced verbatim', async () => {
        const { fns, els } = run(() => {
            const e = mkErr(500, 'upstream timeout');
            throw e;
        }, () => ADMIN_PERMS, () => TENANT_USERS, 3, { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        assert.ok(els['as-error'].textContent.includes('upstream timeout'));
        assert.ok(els['as-empty'].className.includes('hidden') === false);
    });

    test('escaping: hostile device_info/IP cannot inject markup', async () => {
        const hostile = [{ id: 5, device_info: '<img src=x onerror=alert(1)>',
            ip_address: '"><b>hi', created_at: null, last_active_at: null,
            keycloak_session_id: null }];
        const { fns, els, ser } = run(() => hostile, () => PLAIN_PERMS, () => [], 3,
            { revokeModel: okRevoke, everywhereModel: okEverywhere });
        await fns.initializeActiveSessionsPage();
        const body = ser(els['as-table-body']);
        assert.ok(!body.includes('<img'), 'no raw tag from the device field');
        assert.ok(body.includes('&lt;img'), 'escaped');
        assert.ok(!body.includes('<b>'), 'no raw tag from the IP field');
        assert.ok(body.includes('&lt;b&gt;hi'), 'IP escaped too');
    });
});
