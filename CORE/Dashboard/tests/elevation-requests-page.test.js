// GSEC FX.120 p5a — node-runnable check for the Elevation Requests page
// (CORE/Dashboard/js/elevation-requests.js), the Dashboard half of the held
// platform_elevation router (approve/deny/revoke also carry require_fresh_auth).
//
// elevation-requests.js is an ES module, so this script extracts the body
// (from the module state `let _mine = [];` to EOF, with the `export ` keyword
// stripped) and runs it in a vm sandbox with a tiny DOM (tag/id/className/
// textContent/innerHTML/disabled/dataset/attrs/children/listeners/value),
// plus fetchApi + me (api.js), handleLogin (auth.js) and withButtonBusy
// (utils.js) — the module's own endpoint object is built on fetchApi, so the
// sandbox only has to stub fetchApi.
//
// Asserts:
//   (a) render: BOTH tables render (own rows from the no-param read, the
//       pending queue from ?all=true), each row showing id, scope, state chip
//       + plain copy, reason, requested-by, decided-by, window start/end and
//       time remaining; Approve/Deny on pending rows, Revoke on approved/
//       active rows, nothing on terminal (denied/expired/revoked) rows; every
//       generated button carries an aria-label naming the row;
//   (b) approve -> the right endpoint: exactly one POST to
//       /platform/elevation-requests/{id}/approve with NO body (the click is
//       the decision), and the deny/revoke paths send {reason};
//   (c) 403 (not platform staff) -> read-only: notice shown, every remaining
//       action button disabled + aria-disabled, a later action sends nothing;
//   (d) fresh-auth (GY.C4b) 401 {detail:{error:'reauth_required'}} -> the
//       "recent login is required" message, the re-login panel unhidden, and
//       nothing retried;
//   (e) self-approval refusal is the SERVER's sentence, shown verbatim (the
//       page never decides legality itself);
//   (f) escaping: a hostile reason/scope cannot inject markup (textContent
//       only);
//   (g) the create DTO is exactly {scope, reason, ttl_minutes?} — a blank
//       minutes field omits ttl_minutes (the platform default applies);
//   (h) the endpoint object builds only the paths/params the router has
//       (?all=true, ?limit=, id-encoded path segments).
//
//   node --test CORE/Dashboard/tests/elevation-requests-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'elevation-requests.js'), 'utf8');
// The body starts at the module's own endpoint object (the fetchApi-shaped
// route helpers) so the sandbox gets the whole module minus its imports.
const start = src.indexOf('const elevationRequests = {');
assert.ok(start >= 0, 'could not locate the elevation-requests body');
const block = src.slice(start).replace(/export async function/g, 'async function');

const IDS = ['el-error', 'el-notice', 'el-count', 'el-relogin', 'el-relogin-btn',
    'el-empty-mine', 'el-mine', 'el-mine-body', 'el-empty-pending', 'el-pending',
    'el-pending-body', 'el-new-btn', 'el-new-scope', 'el-new-reason', 'el-new-ttl',
    'el-decision-reason'];

function mkErr(status, detail) {
    return { status, detail, message: typeof detail === 'string' ? detail : 'see detail' };
}

function run(listMine, listAll, actModel, userAdmin) {
    const created = [];

    function mkEl(tag, id) {
        const el = { tag, id: id || '', className: '', textContent: '',
            innerHTML: '', disabled: false, checked: null, value: null,
            dataset: {}, attrs: {}, children: [], listeners: {} };
        el.classList = { add: (c) => {
            if (!el.className.split(/\s+/).includes(c)) {
                el.className = `${el.className} ${c}`.trim();
            }
        },
            remove: (c) => {
                el.className = el.className.split(/\s+/)
                    .filter((x) => x !== c).join(' ');
            },
            toggle: () => {} };
        el.appendChild = (child) => { el.children.push(child); return child; };
        el.setAttribute = (k, v) => { el.attrs[k] = v; };
        el.getAttribute = (k) => el.attrs[k];
        // withButtonBusy clears the busy flag the real DOM way; the sandbox
        // must have it or the assert would lie about the button's state.
        el.removeAttribute = (k) => { delete el.attrs[k]; };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.querySelectorAll = () => [];
        el.querySelector = () => null;
        Object.defineProperty(el, 'innerHTML', {
            get: () => el._html,
            set: (v) => { el._html = v; if (v === '') el.children = []; },
        });
        el._html = '';
        created.push(el);
        return el;
    }

    const els = {};
    for (const id of IDS) els[id] = mkEl('div', id);
    ['el-mine-body', 'el-pending-body'].forEach((id) => els[id].tag = 'tbody');
    ['el-new-btn', 'el-relogin-btn'].forEach((id) => els[id].tag = 'button');
    ['el-new-scope', 'el-new-reason', 'el-new-ttl', 'el-decision-reason'].forEach((id) => {
        els[id].tag = 'input';
    });
    // The shipped page carries `hidden` on these panels; the sandbox starts
    // from the same classes or the hidden-state asserts lie.
    ['el-error', 'el-notice', 'el-empty-mine', 'el-empty-pending', 'el-relogin']
        .forEach((id) => els[id].className = 'hidden');

    const doc = {
        getElementById: (id) => created.find((e) => e.id === id),
        createElement: (tag) => mkEl(tag, ''),
        createTextNode: (text) => ({ text }),
        querySelectorAll: () => [],
        querySelector: () => null,
    };

    const calls = { list: [], act: [], create: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: doc,
        confirm: () => true,
        me: { get: () => Promise.resolve({ is_platform_admin: userAdmin }) },
        handleLogin: () => { calls.login = (calls.login || 0) + 1; },
        withButtonBusy: (btn, action) => {
            if (!btn) return action();
            if (btn.dataset.modBusy === '1') return undefined;
            btn.dataset.modBusy = '1';
            btn.disabled = true;
            btn.setAttribute('aria-busy', 'true');
            try { return action(); } finally {
                delete btn.dataset.modBusy;
                btn.disabled = false;
                btn.removeAttribute('aria-busy');
            }
        },
        fetchApi: (url, options = {}) => {
            const method = (options && options.method) || 'GET';
            const rec = { url, method, body: options && options.body };
            if (method === 'GET') {
                calls.list.push(rec);
                const isAll = /all=true/.test(url);
                const res = (isAll ? listAll : listMine)();
                // A rejected promise, not a synchronous throw: loadRows()
                // awaits THREE of these in one Promise.all, and a thrown
                // sibling leaves the other two rejections unhandled (the
                // runner then reports phantom post-test failures).
                if (res && res.__throw) return Promise.reject(mkErr(res.status, res.detail));
                return Promise.resolve(res);
            }
            (rec.url.endsWith('/approve') || rec.url.endsWith('/deny')
                || rec.url.endsWith('/revoke') ? calls.act : calls.create).push(rec);
            const res = actModel(url, method);
            if (res && res.__throw) return Promise.reject(mkErr(res.status, res.detail));
            return Promise.resolve(res);
        },
        // URLSearchParams is a Node global, not a built-in of a fresh vm
        // context, so the module's own query-string builder needs it handed
        // in (Date/Math/JSON/Object are real built-ins and come along).
        URLSearchParams: globalThis.URLSearchParams,
    };

    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeElevationRequestsPage, loadRows, _renderAll, _rowNode, handleAction, handleCreate, _goReadOnly, _handleReauth, _countText, _allowedActions, _remaining, _window, elevationRequests });`,
        sandbox, { filename: 'elevation-requests-page-body' });

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

const ROWS = [
    { id: 41, requested_by_user_id: 9, scope: 'db_admin', reason: 'prod db read for the outage',
      requested_ttl_minutes: 120, status: 'pending', approved_by_user_id: null,
      self_approved: false, approved_at: null, activated_at: null, expires_at: null,
      revoked_at: null, revoke_reason: null, created_at: '2026-10-07T09:00:00+00:00' },
    { id: 42, requested_by_user_id: 9, scope: 'host_shell', reason: 'stuck worker needs a shell',
      requested_ttl_minutes: 60, status: 'approved', approved_by_user_id: 12,
      self_approved: false, approved_at: '2026-10-07T10:00:00+00:00',
      activated_at: '2026-10-07T10:05:00+00:00',
      expires_at: '2099-01-01T00:00:00+00:00', revoked_at: null,
      revoke_reason: null, created_at: '2026-10-07T09:30:00+00:00' },
    { id: 43, requested_by_user_id: 9, scope: 'docker', reason: 'docker socket for the rebuild',
      requested_ttl_minutes: 30, status: 'denied', approved_by_user_id: null,
      self_approved: false, approved_at: null, activated_at: null, expires_at: null,
      revoked_at: null, revoke_reason: 'no production access needed',
      created_at: '2026-10-06T22:00:00+00:00' },
];

function onePage(rows) {
    return () => ({ mode: 'mine', items: rows.map((r) => ({ ...r })), counts: {} });
}
function onePending(rows) {
    return () => ({ mode: 'pending_for_approval',
        items: rows.filter((r) => r.status === 'pending').map((r) => ({ ...r })),
        counts: { pending: rows.filter((r) => r.status === 'pending').length } });
}

function okAct() {
    return { id: 41, status: 'approved',
        approved_at: '2026-10-07T10:15:00+00:00',
        approved_by_user_id: 12, self_approved: false };
}

test.describe('elevation-requests-page', () => {
    test('render: both tables, state chips, window + time remaining, named buttons', async () => {
        const { fns, els, ser } = run(onePage(ROWS), onePending(ROWS), okAct, true);
        await fns.initializeElevationRequestsPage();
        const mine = ser(els['el-mine-body']);
        const pend = ser(els['el-pending-body']);
        assert.ok(mine.includes('db_admin'), 'scope renders');
        assert.ok(mine.includes('prod db read for the outage'), 'reason renders as text');
        assert.ok(mine.includes('#9'), 'requested-by renders');
        assert.ok(mine.includes('#12'), 'decided-by renders when decided');
        assert.ok(mine.includes('bg-amber-100 text-amber-800'), 'pending state chip');
        assert.ok(mine.includes('bg-green-100 text-green-700'), 'approved state chip');
        assert.ok(mine.includes('bg-red-100 text-red-700'), 'denied state chip');
        assert.ok(mine.includes('2026-10-07 10:05:00'), 'window start (activated_at) renders');
        assert.ok(mine.includes('2099-01-01 00:00:00'), 'window end (expires_at) renders');
        assert.ok(mine.includes('left') || mine.includes('window lapsed'),
            'time remaining is stated for the live window');
        assert.ok(mine.includes('aria-label="Approve elevation request 41"'),
            'approve button is named for its row');
        assert.ok(mine.includes('data-elAction="approve"'), 'approve button on the pending row');
        assert.ok(mine.includes('data-elAction="deny"'), 'deny button on the pending row');
        assert.ok(mine.includes('data-elAction="revoke"'), 'revoke button on the approved row');
        assert.ok(!mine.includes('data-elId="43"'),
            'no action buttons at all on the denied (terminal) row');
        assert.ok(mine.includes('denied — decided'), 'terminal row states it');
        assert.ok(pend.includes('data-elId="41"'), 'the ?all=true queue rendered its pending row');
        assert.ok(!pend.includes('data-elId="42"'), 'the queue holds only pending rows');
        assert.strictEqual(els['el-count'].textContent,
            '3 own row(s) (mode mine) · 1 pending(s) awaiting a decision '
            + '(mode pending_for_approval) · own rows by state: pending 1, approved 1, denied 1');
        assert.ok(els['el-empty-mine'].className.includes('hidden'), 'no empty panel with rows');
        assert.ok(els['el-empty-pending'].className.includes('hidden'), 'no empty queue panel');
    });

    test('approve -> one POST to /{id}/approve with NO body; deny/revoke send {reason}', async () => {
        const { fns, calls, els } = run(onePage(ROWS), onePending(ROWS), okAct, true);
        await fns.initializeElevationRequestsPage();
        await fns.handleAction('approve', 41);
        assert.strictEqual(calls.act.length, 1, 'exactly one decision POST');
        assert.strictEqual(calls.act[0].url, '/platform/elevation-requests/41/approve',
            'the approve endpoint the router actually has');
        assert.strictEqual(calls.act[0].method, 'POST', 'a POST, not a GET');
        assert.strictEqual(calls.act[0].body, undefined,
            'approve sends NO body (the click is the decision)');
        els['el-decision-reason'].value = 'not on the outage path';
        await fns.handleAction('deny', 41);
        assert.strictEqual(calls.act.length, 2, 'the deny POST followed');
        assert.strictEqual(calls.act[1].url, '/platform/elevation-requests/41/deny',
            'the deny endpoint');
        assert.equal(JSON.stringify(JSON.parse(calls.act[1].body)),
            JSON.stringify({ reason: 'not on the outage path' }),
            'deny sends the REQUIRED reason');
        await fns.handleAction('revoke', 42);
        assert.strictEqual(calls.act.length, 3, 'the revoke POST followed');
        assert.strictEqual(calls.act[2].url, '/platform/elevation-requests/42/revoke',
            'the revoke endpoint');
        assert.equal(JSON.stringify(JSON.parse(calls.act[2].body)),
            JSON.stringify({ reason: 'not on the outage path' }),
            'revoke sends a reason too (the service requires one)');
    });

    test('403 on a decision -> read-only: notice, every button disabled, nothing re-sent', async () => {
        const forbidden = () => ({ __throw: true, status: 403,
            detail: 'production elevation is platform-admin only' });
        const { fns, els, calls, created } = run(onePage(ROWS), onePending(ROWS), forbidden, true);
        await fns.initializeElevationRequestsPage();
        await fns.handleAction('approve', 41);
        assert.strictEqual(calls.act.length, 1, 'the POST was attempted once');
        assert.ok(els['el-notice'].textContent.includes('platform-admin only')
            || els['el-notice'].textContent.includes('not decide them'),
            'the read-only notice is shown');
        assert.ok(!els['el-notice'].className.includes('hidden'), 'notice visible');
        assert.ok(els['el-count'].textContent.includes('read-only'),
            'the count line says read-only');
        const disabled = created.filter((e) => e.attrs['aria-disabled'] === 'true');
        assert.ok(disabled.length >= 2, 'the remaining action buttons are aria-disabled');
        assert.ok(disabled.every((e) => e.disabled === true), 'and really disabled');
        await fns.handleAction('deny', 41);
        assert.strictEqual(calls.act.length, 1, 'the second decision sends nothing');
        assert.ok(els['el-error'].textContent.includes('read-only'));
    });

    test('fresh-auth 401 reauth_required -> the re-login message, panel unhidden, nothing retried', async () => {
        const stale = () => ({ __throw: true, status: 401,
            detail: { error: 'reauth_required', reauth_max_age_s: 300 } });
        const { fns, els, calls, created } = run(onePage(ROWS), onePending(ROWS), stale, true);
        await fns.initializeElevationRequestsPage();
        await fns.handleAction('approve', 41);
        assert.strictEqual(calls.act.length, 1, 'the POST was attempted once, nothing retried');
        assert.ok(els['el-notice'].textContent.includes('A recent login is required to approve'),
            'the fresh-auth message names the action');
        assert.ok(els['el-notice'].textContent.includes('GY.C4b'),
            'the page names the gate the server enforced');
        assert.ok(!els['el-notice'].className.includes('hidden'), 'notice visible');
        assert.ok(!els['el-relogin'].className.includes('hidden'),
            'the re-login panel is unhidden');
        const reBtn = created.find((e) => e.id === 'el-relogin-btn');
        assert.ok(reBtn && reBtn.listeners.click, 'the re-login button is wired');
        reBtn.listeners.click();
        assert.strictEqual(calls.login, 1, 'clicking it takes the auth.js handleLogin path');
        assert.strictEqual(calls.act.length, 1, 'and it fires no elevation call');
    });

    test('self-approval refusal is the SERVER\'s sentence, shown verbatim', async () => {
        const refused = () => ({ __throw: true, status: 403,
            detail: 'elevation needs a SECOND platform admin (security.elevation_solo_mode off)' });
        const { fns, els, calls } = run(onePage(ROWS), onePending(ROWS), refused, true);
        await fns.initializeElevationRequestsPage();
        await fns.handleAction('approve', 41);
        assert.strictEqual(calls.act.length, 1, 'the attempt really was sent (the page never pre-decides)');
        assert.ok(els['el-error'].textContent.includes(
            'elevation needs a SECOND platform admin'), 'the server detail is shown verbatim');
        assert.ok(!els['el-error'].className.includes('hidden'), 'error visible');
        assert.ok(!els['el-notice'].textContent.includes('approved'),
            'nothing claims the approval happened');
    });

    test('escaping: a hostile reason/scope cannot inject markup', async () => {
        const hostile = [{ id: 7, requested_by_user_id: 9,
            scope: '<img src=x onerror=alert(1)>',
            reason: '"><b>hi <script>alert(1)</script>',
            requested_ttl_minutes: 60, status: 'pending', approved_by_user_id: null,
            self_approved: false, approved_at: null, activated_at: null,
            expires_at: null, revoked_at: null, revoke_reason: null,
            created_at: '2026-10-07T09:00:00+00:00' }];
        const { fns, els, ser } = run(onePage(hostile), onePending(hostile), okAct, true);
        await fns.initializeElevationRequestsPage();
        const body = ser(els['el-mine-body']) + ser(els['el-pending-body']);
        assert.ok(!body.includes('<img'), 'no raw tag from the scope field');
        assert.ok(!body.includes('<script'), 'no raw tag from the reason field');
        assert.ok(body.includes('&lt;img'), 'scope escaped');
        assert.ok(body.includes('&lt;script'), 'reason escaped');
    });

    test('create DTO is exactly {scope, reason, ttl_minutes?}', async () => {
        const { fns, els, calls } = run(onePage(ROWS), onePending(ROWS), okAct, true);
        await fns.initializeElevationRequestsPage();
        els['el-new-scope'].value = 'vault_admin';
        els['el-new-reason'].value = 'vault role mint for the audit review';
        els['el-new-ttl'].value = '';
        await fns.handleCreate();
        assert.strictEqual(calls.create.length, 1, 'one POST to the create route');
        assert.strictEqual(calls.create[0].url, '/platform/elevation-requests',
            'the create route the router has');
        assert.equal(JSON.stringify(JSON.parse(calls.create[0].body)),
            JSON.stringify({ scope: 'vault_admin',
                reason: 'vault role mint for the audit review' }),
            'blank minutes omits ttl_minutes (the platform default applies)');
        els['el-new-ttl'].value = '45';
        await fns.handleCreate();
        assert.equal(JSON.stringify(JSON.parse(calls.create[1].body)),
            JSON.stringify({ scope: 'vault_admin',
                reason: 'vault role mint for the audit review',
                ttl_minutes: 45 }),
            'a numeric ttl_minutes is sent as a NUMBER');
        els['el-new-ttl'].value = 'two hours';
        await fns.handleCreate();
        assert.strictEqual(calls.create.length, 2, 'a non-numeric TTL is refused client-side');
        assert.ok(els['el-error'].textContent.includes('whole number'), 'and says so');
        els['el-new-reason'].value = 'short';
        await fns.handleCreate();
        assert.strictEqual(calls.create.length, 2, 'a short reason never reaches the API');
        assert.ok(els['el-error'].textContent.includes('at least 10 characters'),
            'the page states the server\'s own floor');
    });

    test('empty state: no rows -> both empty panels, no rows', async () => {
        const { fns, els, ser } = run(onePage([]), onePending([]), okAct, true);
        await fns.initializeElevationRequestsPage();
        assert.strictEqual(ser(els['el-mine-body']).replace(/<\/?tbody[^>]*>/g, ''), '');
        assert.strictEqual(ser(els['el-pending-body']).replace(/<\/?tbody[^>]*>/g, ''), '');
        assert.ok(!els['el-empty-mine'].className.includes('hidden'));
        assert.ok(!els['el-empty-pending'].className.includes('hidden'));
        assert.strictEqual(els['el-count'].textContent,
            '0 own row(s) (mode mine) · 0 pending(s) awaiting a decision '
            + '(mode pending_for_approval)');
    });

    test('GET failure surfaced verbatim', async () => {
        const boom = () => ({ __throw: true, status: 500,
            detail: 'Could not read the elevation request' });
        const { fns, els } = run(boom, boom, okAct, true);
        await fns.initializeElevationRequestsPage();
        assert.ok(els['el-error'].textContent.includes('Could not read the elevation request'),
            'the server detail is shown, not swallowed');
    });
});

// api-level check: the module's own endpoint object must build only the paths
// and params the router actually has (?all=true, ?limit=, id-encoded segments,
// approve bodyless, deny/revoke with a JSON body).
test('elevation-requests endpoint object builds only the router\'s paths/params', async () => {
    const at = src.indexOf('const elevationRequests = {');
    assert.ok(at >= 0, 'the endpoint object lives in the module');
    const end = src.indexOf('};', at);
    const objSrc = src.slice(at, end + 2);
    const seen = [];
    const fetchApi = (url, options = {}) => { seen.push([url, options.method || 'GET', options.body]); return Promise.resolve({}); };
    const obj = vm.runInNewContext(`${objSrc}\nelevationRequests;`,
        { fetchApi, seen, URLSearchParams: globalThis.URLSearchParams });
    await obj.list({});
    assert.strictEqual(seen[seen.length - 1][0], '/platform/elevation-requests',
        'no params -> bare path (mode mine)');
    await obj.list({ all: true, limit: 200 });
    assert.strictEqual(seen[seen.length - 1][0],
        '/platform/elevation-requests?all=true&limit=200',
        'the pending queue asks ?all=true (all/limit only)');
    await obj.list({ all: false, limit: 0 });
    assert.strictEqual(seen[seen.length - 1][0], '/platform/elevation-requests',
        'false/zero values are omitted');
    await obj.approve(41);
    assert.strictEqual(seen[seen.length - 1][0], '/platform/elevation-requests/41/approve',
        'approve path');
    assert.strictEqual(seen[seen.length - 1][2], undefined, 'approve is bodyless');
    await obj.deny(41, 'no production access');
    assert.strictEqual(seen[seen.length - 1][0], '/platform/elevation-requests/41/deny');
    assert.strictEqual(seen[seen.length - 1][2], JSON.stringify({ reason: 'no production access' }));
    await obj.revoke(42, 'ended early');
    assert.strictEqual(seen[seen.length - 1][0], '/platform/elevation-requests/42/revoke');
    assert.strictEqual(seen[seen.length - 1][2], JSON.stringify({ reason: 'ended early' }));
    await obj.create({ scope: 'docker', reason: 'rebuild needs the socket' });
    assert.strictEqual(seen[seen.length - 1][0], '/platform/elevation-requests');
    assert.strictEqual(seen[seen.length - 1][1], 'POST', 'create is a POST');
});
