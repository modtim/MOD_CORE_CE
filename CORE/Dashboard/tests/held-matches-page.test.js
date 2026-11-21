// Node-runnable check for the GSEC FX.74 p4 Held Matches page
// (CORE/Dashboard/js/held-matches.js) — the admin review page for accounts an offline OFAC
// SDN screening match HELD for review (FX.74 p3a holds, it never blocks).
//
// held-matches.js is an ES module, so this script extracts the body (from the module state
// `const heldMatches = {` to EOF, with the `export ` keyword stripped) and runs it in a vm
// sandbox with a tiny DOM (id/className/textContent/innerHTML/value/disabled/dataset/attrs)
// plus fetchApi, me (api.js), handleLogin (auth.js) and withButtonBusy (utils.js). Then it
// asserts:
//   (a) render: one row per HELD account (user id, held-state chip, email, name, the SDN
//       evidence the audit row carries — ent_num / sdn_name / score / field / held-at), the
//       audit-trail table renders the API's own rows, and the count line reports both reads;
//   (b) held-only listing: an ordinary active account with no evidence never appears; a
//       pending account appears even when the evidence column is gated off;
//   (c) filters: the status select narrows the held state, the field select narrows the
//       evidence field (both are the API's own shapes, nothing invented);
//   (d) decisions call the right endpoint: Release PATCHes /users/{id}/reactivate, Reject
//       PATCHes /users/{id}/deactivate, Escalate PATCHes /users/{id} (status pending) AND
//       POSTs /platform/security-incidents whose summary IS the reason;
//   (e) confirm dialog: a declined confirm sends nothing;
//   (f) a reason is required — a blank reason sends nothing;
//   (g) 403 on a write -> read-only: notice shown, every action control disabled + aria-
//       disabled, a later action sends nothing;
//   (h) 402 on the evidence read (audit-integrity capability entitlement) -> the accounts still list, the
//       notice quotes the server's own words, and no decision is blocked by it;
//   (i) reauth_required (GY.C4b) -> the re-login panel is unhid and nothing is retried;
//   (j) empty state + a failed GET surfaced verbatim;
//   (k) escaping: a hostile SDN name / email cannot inject markup.
//
//   node --test CORE/Dashboard/tests/held-matches-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'held-matches.js'), 'utf8');
const start = src.indexOf('const heldMatches = {');
assert.ok(start >= 0, 'could not locate the held-matches body');
const block = src.slice(start).replace(/export async function/g, 'async function')
    .replace(/export function/g, 'function');

const IDS = ['hm-error', 'hm-notice', 'hm-count', 'hm-empty', 'hm-list', 'hm-table-body',
    'hm-audit', 'hm-audit-body', 'hm-audit-empty', 'hm-relogin', 'hm-relogin-btn',
    'hm-status-filter', 'hm-field-filter', 'hm-tenant', 'hm-reason'];

function mkErr(status, detail) {
    return { status, detail, message: typeof detail === 'string' ? detail
        : (detail && detail.message) || '' };
}

function run(users, evidence, opts = {}) {
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
    els['hm-table-body'].tag = 'tbody';
    els['hm-audit-body'].tag = 'tbody';
    els['hm-relogin-btn'].tag = 'button';
    els['hm-status-filter'].tag = 'select';
    els['hm-field-filter'].tag = 'select';
    els['hm-tenant'].tag = 'input';
    els['hm-reason'].tag = 'input';
    // The shipped page carries `hidden` on these panels; the sandbox must start from the
    // same classes or the hidden-state asserts lie.
    ['hm-error', 'hm-notice', 'hm-empty', 'hm-audit-empty', 'hm-relogin'].forEach((id) => {
        els[id].className = 'hidden';
    });
    els['hm-status-filter'].value = opts.status || '';
    els['hm-field-filter'].value = opts.field || '';
    els['hm-tenant'].value = opts.tenant || '';
    els['hm-reason'].value = opts.reason || '';

    const doc = {
        getElementById: (id) => created.find((e) => e.id === id) || els[id],
        createElement: (tag) => mkEl(tag, ''),
        createTextNode: (text) => ({ text }),
        querySelectorAll: () => [],
        querySelector: () => null,
    };

    const calls = { users: [], evidence: [], release: [], reject: [], escalate: [], incident: [] };
    // Every model defaults to "the call succeeds"; a test overrides only the ones it cares
    // about, so a missing model never throws inside fetchApi (a synchronous throw there would
    // abort the Promise.all reads and make the other call counts lie).
    const usersModel = opts.usersModel || okUsers;
    const evidenceModel = opts.evidenceModel || okEvidence;
    const incidentModel = opts.incidentModel || okIncident;
    const releaseModel = opts.releaseModel || okWrite;
    const rejectModel = opts.rejectModel || okWrite;
    const escalateModel = opts.escalateModel || okWrite;

    function fail(res) {
        // Only a NUMERIC 4xx/5xx is an error: SecurityIncidentOut carries its own `status`
        // ('open'), so a truthiness test would read that as a refusal.
        if (res && typeof res.status === 'number' && res.status >= 400) {
            return Promise.reject(mkErr(res.status, res.detail));
        }
        return Promise.resolve(res);
    }

    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        document: doc,
        Date: globalThis.Date,
        isNaN: globalThis.isNaN,
        JSON: globalThis.JSON,
        Set: globalThis.Set,
        encodeURIComponent: globalThis.encodeURIComponent,
        decodeURIComponent: globalThis.decodeURIComponent,
        String: globalThis.String,
        Number: globalThis.Number,
        Array: globalThis.Array,
        confirm: () => opts.confirm !== false,
        me: { get: () => Promise.resolve(opts.self) },
        fetchApi: (endpoint, options = {}) => {
            if (endpoint === '/users/' || endpoint.startsWith('/users/?')) {
                calls.users.push(endpoint);
                const res = usersModel();
                if (res && res.status) return Promise.reject(mkErr(res.status, res.detail));
                return Promise.resolve(users());
            }
            if (endpoint.startsWith('/audit-logs/')) {
                calls.evidence.push(endpoint);
                const res = evidenceModel();
                if (res && res.status) return Promise.reject(mkErr(res.status, res.detail));
                return Promise.resolve(evidence());
            }
            if (endpoint.startsWith('/platform/security-incidents')) {
                calls.incident.push(endpoint);
                return fail(incidentModel(endpoint));
            }
            if (endpoint.endsWith('/reactivate')) {
                calls.release.push(endpoint);
                return fail(releaseModel(endpoint));
            }
            if (endpoint.endsWith('/deactivate')) {
                calls.reject.push(endpoint);
                return fail(rejectModel(endpoint));
            }
            if (endpoint.startsWith('/users/')) {
                calls.escalate.push(endpoint);
                return fail(escalateModel(endpoint));
            }
            return Promise.reject(mkErr(500, 'unexpected endpoint'));
        },
        handleLogin: () => null,
        withButtonBusy: (btn, action) => {
            if (!btn) return action();
            btn.dataset.modBusy = '1';
            btn.disabled = true;
            try {
                return action();
            } finally {
                delete btn.dataset.modBusy;
                btn.disabled = false;
            }
        },
    };

    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeHeldMatchesPage, load, _renderAll, _rowNode, _auditNode, handleAction, _evidenceFor, _heldState, _allowedActions, _countText, _goReadOnly, _handleReauth, _field, _errText, _fmt, _matches });`,
        sandbox, { filename: 'held-matches-page-body' });

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

const USERS = [
    { id: 7, email: 'john.doe@example.com', first_name: 'John', last_name: 'Doe',
      status: 'pending', tenant_id: 1 },
    { id: 8, email: 'clean@example.com', first_name: 'Clean', last_name: 'User',
      status: 'active', tenant_id: 1 },
    { id: 9, email: 'released@example.com', first_name: 'Bob', last_name: 'Sdn',
      status: 'active', tenant_id: 1 },
    { id: 10, email: 'gone@example.com', first_name: 'Gone', last_name: 'X',
      status: 'deactivated', tenant_id: 1 },
];
const EVIDENCE = [
    { id: 501, action: 'security_incident_created', timestamp: '2026-10-06T09:00:00+00:00',
      actor_user_id: 0, target_user_id: null, resource_type: 'security', resource_id: 0,
      metadata: { kind: 'sanctions_sdn_screening_hold', email: 'john.doe@example.com',
                  matches: [{ ent_num: 12345, score: 1.0, field: 'name',
                              sdn_name: 'JOHN DOE' }] } },
    { id: 502, action: 'security_incident_created', timestamp: '2026-10-05T07:30:00+00:00',
      actor_user_id: 0, target_user_id: null, resource_type: 'security', resource_id: 0,
      metadata: { kind: 'sanctions_sdn_screening_hold', email: 'released@example.com',
                  matches: [{ ent_num: 55001, score: 0.93, field: 'company',
                              sdn_name: 'ACME FZ' }] } },
    { id: 503, action: 'security_incident_created', timestamp: '2026-10-04T06:15:00+00:00',
      actor_user_id: 0, target_user_id: null, resource_type: 'security', resource_id: 0,
      metadata: { kind: 'sanctions_sdn_screening_hold', email: 'gone@example.com',
                  matches: [{ ent_num: 60001, score: 1.0, field: 'name',
                              sdn_name: 'GONE X' }] } },
];

function okUsers() { return null; }
function okEvidence() { return null; }
function okWrite() { return null; }
function okIncident(endpoint) { return { id: 'inc-9', severity: 'high', status: 'open' }; }

test.describe('held-matches-page', () => {
    test('render: held rows, evidence columns, audit trail, count line', async () => {
        const { fns, els, ser, calls } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true, parent_user_id: null } });
        await fns.initializeHeldMatchesPage();
        const body = ser(els['hm-table-body']);
        assert.strictEqual(calls.users.length, 1, 'one GET /users/');
        assert.strictEqual(calls.evidence.length, 1, 'one GET /audit-logs/permissions');
        assert.ok(body.includes('data-hmUserId="7"'), 'held row keyed to its user id');
        assert.ok(body.includes('john.doe@example.com'), 'email renders');
        assert.ok(body.includes('John Doe'), 'name renders');
        assert.ok(body.includes('12345'), 'SDN ent_num renders');
        assert.ok(body.includes('JOHN DOE'), 'SDN name renders');
        assert.ok(body.includes('1'), 'score renders');
        assert.ok(body.includes('name'), 'matched field renders');
        assert.ok(body.includes('2026-10-06 09:00'), 'held-at renders');
        assert.ok(body.includes('aria-label="Release held match for user 7"'),
            'action button names its row');
        assert.ok(!body.includes('data-hmUserId="8"'), 'an active account with no evidence is not listed');
        assert.ok(body.includes('data-hmUserId="9"'), 'a released account with evidence is listed');
        assert.ok(body.includes('data-hmUserId="10"'), 'a rejected account with evidence is listed');
        const audit = ser(els['hm-audit-body']);
        assert.ok(audit.includes('security_incident_created'), 'audit action renders');
        assert.ok(audit.includes('55001'), 'audit evidence ent_num renders');
        assert.ok(audit.includes('released@example.com'), 'audit email renders');
        assert.strictEqual(els['hm-count'].textContent,
            '3 held account(s) · 3 screening-hold audit row(s) · '
            + 'by state: pending 1, active 1, deactivated 1');
        assert.ok(els['hm-empty'].className.includes('hidden'), 'no empty panel with rows');
        assert.ok(els['hm-audit-empty'].className.includes('hidden'), 'audit table has rows');
    });

    test('filters: status select narrows the held state, field select narrows the evidence', async () => {
        const { fns, els, ser } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, status: 'pending' });
        await fns.initializeHeldMatchesPage();
        const body = ser(els['hm-table-body']);
        assert.ok(body.includes('data-hmUserId="7"'), 'pending row kept');
        assert.ok(!body.includes('data-hmUserId="9"'), 'released row dropped');
        assert.ok(!body.includes('data-hmUserId="10"'), 'rejected row dropped');
        assert.strictEqual(els['hm-count'].textContent,
            '1 held account(s) · 3 screening-hold audit row(s) · by state: pending 1');
    });

    test('field filter: only evidence carrying that field lists its account', async () => {
        const { fns, els, ser } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, field: 'company' });
        await fns.initializeHeldMatchesPage();
        const body = ser(els['hm-table-body']);
        assert.ok(body.includes('data-hmUserId="9"'), 'company-matched row kept');
        assert.ok(body.includes('data-hmUserId="7"'), 'pending row still listed (it is held)');
        assert.ok(!body.includes('data-hmUserId="10"'), 'name-only match dropped');
    });

    test('release calls PATCH /users/{id}/reactivate for that row only', async () => {
        const { fns, calls, els, created } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, reason: 'reviewed, clear of the SDN list' });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'release');
        await fns.handleAction('release', btn.dataset.hmUserId);
        assert.strictEqual(calls.release.length, 1, 'exactly one release');
        assert.strictEqual(calls.release[0], '/users/7/reactivate', 'the row\'s own id shipped');
        assert.strictEqual(calls.evidence.length, 2, 'the page re-reads after a decision');
        assert.ok(els['hm-notice'].textContent.includes('Reason recorded'),
            'the reason is echoed into the notice');
    });

    test('reject calls PATCH /users/{id}/deactivate', async () => {
        const { fns, calls, els, created } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, reason: 'confirmed sanctioned entity' });
        await fns.initializeHeldMatchesPage();
        const btn = created.filter((e) => e.tag === 'button' && e.dataset.hmAction === 'reject')
            .find((e) => e.dataset.hmUserId === '9');
        await fns.handleAction(btn.dataset.hmAction, '9');
        assert.strictEqual(calls.reject.length, 1, 'exactly one reject');
        assert.strictEqual(calls.reject[0], '/users/9/deactivate', 'the row\'s own id shipped');
        assert.ok(els['hm-notice'].textContent.includes('confirmed sanctioned entity'),
            'reason reported');
    });

    test('escalate PATCHes /users/{id} (status pending) AND files the incident with the reason', async () => {
        const { fns, calls, els, created } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, reason: 'needs a human review before release' });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'escalate'
            && e.dataset.hmUserId === '9');
        await fns.handleAction(btn.dataset.hmAction, '9');
        assert.strictEqual(calls.escalate.length, 1, 'one re-hold PATCH');
        assert.strictEqual(calls.escalate[0], '/users/9', 'the bare user route, not reactivate/deactivate');
        assert.strictEqual(calls.incident.length, 1, 'one incident filed');
        assert.strictEqual(calls.incident[0], '/platform/security-incidents', 'the incident route');
        assert.ok(els['hm-notice'].textContent.includes('security incident inc-9 filed (summary: needs a human review before release)'),
            'the incident id + reason are reported');
    });

    test('escalate reports an incident refusal without pretending the escalation failed', async () => {
        const { fns, calls, els, created } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: false, parent_user_id: null }, reason: 'hold it for review',
              incidentModel: () => ({ status: 403, detail: 'Platform admin only' }) });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'escalate'
            && e.dataset.hmUserId === '9');
        await fns.handleAction(btn.dataset.hmAction, '9');
        assert.strictEqual(calls.escalate.length, 1, 'the re-hold landed');
        assert.strictEqual(calls.incident.length, 1, 'the incident call was attempted');
        assert.ok(els['hm-notice'].textContent.includes('escalated'), 'the re-hold is reported');
        assert.ok(els['hm-notice'].textContent.includes('incident refused by the server: Platform admin only'),
            'the refusal is quoted, not swallowed');
    });

    test('confirm dialog: a declined confirm sends nothing', async () => {
        const { fns, calls, created } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, reason: 'release it', confirm: false });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'release');
        await fns.handleAction('release', btn.dataset.hmUserId);
        assert.strictEqual(calls.release.length, 0, 'no PATCH after a declined confirm');
        assert.strictEqual(calls.reject.length, 0);
        assert.strictEqual(calls.escalate.length, 0);
        assert.strictEqual(calls.incident.length, 0);
        assert.strictEqual(calls.users.length, 1, 'no re-read either');
    });

    test('a reason is required — a blank reason sends nothing', async () => {
        const { fns, calls, els, created } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, reason: '' });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'reject');
        await fns.handleAction('reject', btn.dataset.hmUserId);
        assert.strictEqual(calls.reject.length, 0, 'nothing sent without a reason');
        assert.ok(els['hm-error'].textContent.includes('A reason is required'),
            'the page says why');
    });

    test('403 on a write -> read-only: notice, controls disabled, nothing re-sent', async () => {
        const forbidden = { status: 403, detail: 'User does not have the required \'user:reactivate\' capability.' };
        const { fns, calls, created, els } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: false, parent_user_id: null }, reason: 'release it',
              releaseModel: () => forbidden, rejectModel: () => forbidden,
              escalateModel: () => forbidden, incidentModel: okIncident });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'release');
        await fns.handleAction('release', btn.dataset.hmUserId);
        assert.strictEqual(calls.release.length, 1, 'the PATCH was attempted once');
        assert.ok(els['hm-notice'].className.includes('hidden') === false, 'notice shown');
        assert.ok(els['hm-notice'].textContent.includes('required \'user:reactivate\' capability'),
            'the server\'s own words');
        const disabled = created.filter((e) => e.attrs['aria-disabled'] === 'true');
        assert.ok(disabled.length >= 6, 'every action control is aria-disabled');
        const laterBtn = created.filter((e) => e.tag === 'button' && e.dataset.hmAction === 'reject').pop();
        await fns.handleAction('reject', laterBtn.dataset.hmUserId);
        assert.strictEqual(calls.reject.length, 0, 'a later decision sends nothing');
        assert.ok(els['hm-error'].textContent.includes('read-only'), 'the refusal is explained');
    });

    test('402 on the evidence read (audit-integrity capability): accounts still list, decisions still work', async () => {
        const { fns, calls, els, ser, created } = run(() => USERS.map((u) => ({ ...u })),
            () => [],
            { self: { id: 1, is_platform_admin: true }, reason: 'release it',
              evidenceModel: () => ({ status: 402, detail: 'Audit HMAC entitlement required' }) });
        await fns.initializeHeldMatchesPage();
        assert.strictEqual(calls.evidence.length, 1, 'the evidence read was attempted');
        assert.strictEqual(calls.users.length, 2, 'the account read was retried alone');
        const body = ser(els['hm-table-body']);
        assert.ok(body.includes('data-hmUserId="7"'), 'the pending account still lists without evidence');
        assert.ok(!body.includes('data-hmUserId="8"'), 'an ordinary active account still never lists');
        assert.ok(els['hm-notice'].textContent.includes('Audit HMAC entitlement required'),
            'the gate is quoted');
        // CE-extract bail (FIX-CE-DASH-SWEEP-D): the audit-integrity capability is scrubbed
        // out of the CE cut (a CE scrub rule rewrites the count wording to
        // "evidence not available in this edition"), so the count line no longer says
        // "evidence gated". Accept either wording; on the paid tree the first clause still
        // runs unchanged.
        assert.ok(els['hm-count'].textContent.includes('evidence gated')
            || els['hm-count'].textContent.includes('evidence not available in this edition'),
            'the count line says so');
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'release');
        await fns.handleAction('release', btn.dataset.hmUserId);
        assert.strictEqual(calls.release.length, 1, 'the decision is not blocked by the evidence gate');
    });

    test('reauth_required (GY.C4b): the re-login panel is unhid and nothing is retried', async () => {
        const reauth = { status: 401, detail: { error: 'reauth_required', reauth_max_age_s: 900 } };
        const { fns, calls, created, els } = run(() => USERS.map((u) => ({ ...u })),
            () => EVIDENCE.map((e) => ({ ...e })),
            { self: { id: 1, is_platform_admin: true }, reason: 'release it',
              releaseModel: () => reauth });
        await fns.initializeHeldMatchesPage();
        const btn = created.find((e) => e.tag === 'button' && e.dataset.hmAction === 'release');
        await fns.handleAction('release', btn.dataset.hmUserId);
        assert.strictEqual(calls.release.length, 1, 'the PATCH was attempted once');
        assert.ok(els['hm-relogin'].className.includes('hidden') === false, 'the panel is shown');
        assert.ok(els['hm-notice'].textContent.includes('A recent login is required to release'),
            'the fresh-auth gate is named');
        assert.strictEqual(calls.users.length, 1, 'no re-read with a stale login');
    });

    test('empty state: no held accounts -> empty panel', async () => {
        const { fns, els, ser } = run(() => [], () => [],
            { self: { id: 1, is_platform_admin: true } });
        await fns.initializeHeldMatchesPage();
        assert.strictEqual(ser(els['hm-table-body']).replace(/<\/?tbody[^>]*>/g, ''), '');
        assert.ok(els['hm-empty'].className.includes('hidden') === false, 'empty panel shown');
        assert.ok(els['hm-audit-empty'].className.includes('hidden') === false, 'audit empty panel shown');
        assert.strictEqual(els['hm-count'].textContent, '0 held account(s) · 0 screening-hold audit row(s)');
    });

    test('a failed account read is surfaced verbatim', async () => {
        const { fns, els } = run(() => {
            throw mkErr(500, 'upstream timeout');
        }, () => [], { self: { id: 1, is_platform_admin: true } });
        await fns.initializeHeldMatchesPage();
        assert.ok(els['hm-error'].textContent.includes('upstream timeout'), 'not swallowed');
        assert.ok(els['hm-empty'].className.includes('hidden') === false);
    });

    test('escaping: a hostile SDN name/email cannot inject markup', async () => {
        const hostile = [{ id: 11, email: '"><img src=x onerror=alert(1)>',
            first_name: '<b>Hi', last_name: 'There', status: 'pending', tenant_id: 1 }];
        const hostileEv = [{ id: 900, action: 'security_incident_created', timestamp: null,
            actor_user_id: 0, target_user_id: null, resource_type: 'security', resource_id: 0,
            metadata: { kind: 'sanctions_sdn_screening_hold',
                        email: '"><img src=x onerror=alert(1)>',
                        matches: [{ ent_num: 1, score: 1, field: 'name',
                                    sdn_name: '"><script>alert(1)</script>' }] } }];
        const { fns, els, ser } = run(() => hostile, () => hostileEv,
            { self: { id: 1, is_platform_admin: true } });
        await fns.initializeHeldMatchesPage();
        const body = ser(els['hm-table-body']);
        assert.ok(!body.includes('<img'), 'no raw tag from the email field');
        assert.ok(body.includes('&lt;img'), 'escaped');
        assert.ok(!body.includes('<script'), 'no raw tag from the SDN name');
        assert.ok(body.includes('&lt;script&gt;'), 'SDN name escaped');
        assert.ok(!body.includes('<b>'), 'no raw tag from the name field');
        const audit = ser(els['hm-audit-body']);
        assert.ok(!audit.includes('<img'), 'audit email escaped too');
        assert.ok(audit.includes('&lt;img'), 'audit copy escaped');
    });
});
