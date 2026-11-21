// Node-runnable check for the GSEC FX.112 p3d Security Alerts page
// (CORE/Dashboard/js/security-alerts.js). security-alerts.js is an ES module,
// so this script extracts the body (from the module state `let _alerts = [];`
// to EOF, with the `export ` keyword stripped) and runs it in a vm sandbox
// with a tiny DOM (elements with tag/id/className/textContent/innerHTML/
// disabled/dataset/attrs/children, document.createElement/createTextNode/
// getElementById) plus securityAlerts (api.js). Then it asserts:
//   (a) render: one row per alert (first/last seen, rule_key, severity chip
//       + plain copy, subject, flattened JSONB evidence, trip count, status
//       chip Open/Acked, acked-by + acked-at) and an Acknowledge button on
//       OPEN rows only — an already-acked row carries none;
//   (b) ack -> POST + row updated IN PLACE: exactly one POST to that alert
//       id, no re-GET, the row's status chip flips to Acked, acked_by/acked_at
//       are filled from the SERVER's stamps, and the button is replaced by a
//       plain note;
//   (c) 403 -> read-only: the notice is shown, every Acknowledge button is
//       disabled + aria-disabled, and a later Acknowledge sends nothing;
//   (d) GET failure surfaced verbatim (the server's own detail, not swallowed);
//   (e) empty state: no alerts -> empty panel, no rows;
//   (f) escaping: hostile subject/details evidence cannot inject markup
//       (everything is written with textContent);
//   (g) filters -> the query params the API actually has: status open/acked
//       map to acked=false/true, 'all' omits it; severity NEVER adds a query
//       param (the router has none) and narrows the collected rows only;
//   (h) paging: total beyond one window -> repeated GETs with growing offset
//       until the server's `total` is covered.
//
//   node --test CORE/Dashboard/tests/security-alerts-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'security-alerts.js'), 'utf8');
const start = src.indexOf('let _alerts = [];');
assert.ok(start >= 0, 'could not locate the security-alerts body');
const block = src.slice(start).replace(/export async function/g, 'async function');

const IDS = ['sa-error', 'sa-notice', 'sa-count', 'sa-empty', 'sa-list',
    'sa-table-body', 'sa-filter-all', 'sa-filter-open', 'sa-filter-acked',
    'sa-filter-sev-all', 'sa-filter-sev-low', 'sa-filter-sev-medium',
    'sa-filter-sev-high', 'sa-filter-sev-critical'];

function mkErr(status, detail) {
    return { status, detail, message: detail };
}

function run(listModel, ackModel) {
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
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.querySelectorAll = () => [];
        el.querySelector = () => null;
        // innerHTML='' must really clear the children (the real DOM does), so
        // the "button is gone after the in-place update" asserts are honest.
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
    els['sa-table-body'].tag = 'tbody';
    els['sa-list'].tag = 'div';
    // The shipped page carries `hidden` on these three panels; the sandbox
    // must start from the same classes or the hidden-state asserts lie.
    ['sa-error', 'sa-notice', 'sa-empty'].forEach((id) => {
        els[id].className = 'hidden';
    });
    // The shipped page bakes the filter buttons; the sandbox mirrors that so
    // the click-wiring and the active-chip asserts are honest.
    ['sa-filter-all', 'sa-filter-open', 'sa-filter-acked',
        'sa-filter-sev-all', 'sa-filter-sev-low', 'sa-filter-sev-medium',
        'sa-filter-sev-high', 'sa-filter-sev-critical'].forEach((id) => {
        els[id].tag = 'button';
    });

    const doc = {
        getElementById: (id) => created.find((e) => e.id === id),
        createElement: (tag) => mkEl(tag, ''),
        createTextNode: (text) => ({ text }),
        querySelectorAll: () => [],
        querySelector: () => null,
    };

    const calls = { list: [], ack: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: doc,
        securityAlerts: {
            list: (opts) => {
                calls.list.push(opts || {});
                const res = listModel(calls.list.length);
                if (res && res.__throw) throw mkErr(res.status, res.detail);
                return Promise.resolve(res);
            },
            ack: (alertId) => {
                calls.ack.push({ alert_id: alertId });
                const res = ackModel(alertId);
                if (res && res.__throw) {
                    return Promise.reject(mkErr(res.status, res.detail));
                }
                return Promise.resolve(res);
            },
        },
    };

    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeSecurityAlertsPage, loadAlerts, _renderAll, _rerender, _rowNode, handleAck, _updateRowInPlace, _goReadOnly, _ackedParam, _countText, _visibleAlerts, _evidenceText });`,
        sandbox, { filename: 'security-alerts-page-body' });

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

const ALERTS = [
    { id: 101, rule_key: 'signin_failures', severity: 'critical',
      tenant_id: 7, subject: 'user:u-1', details: { window_min: 60,
        failures: 4, ip_address: '198.51.100.20' },
      first_seen_at: '2026-10-07T09:00:00+00:00',
      last_seen_at: '2026-10-07T09:30:00+00:00', count: 4,
      acked_at: null, acked_by: null },
    { id: 102, rule_key: 'admin_role_change', severity: 'medium',
      tenant_id: 7, subject: 'grant:role:admin:user:u-2',
      details: { action: 'grant_role' },
      first_seen_at: '2026-10-06T22:00:00+00:00',
      last_seen_at: '2026-10-06T22:05:00+00:00', count: 1,
      acked_at: '2026-10-07T08:00:00+00:00', acked_by: 'user:u-9' },
];

function onePage(alerts) {
    return (n) => ({ items: alerts.map((a) => ({ ...a })),
        total: alerts.length, limit: 50, offset: 0 });
}

function okAck(alertId) {
    return { alert_id: alertId, already_acked: false,
        acked_at: '2026-10-07T10:15:00+00:00', acked_by: 'user:u-9' };
}

test.describe('security-alerts-page', () => {
    test('render: rows, chips, evidence text, ack button on open rows only', async () => {
        const { fns, els, ser } = run(onePage(ALERTS), okAck);
        await fns.initializeSecurityAlertsPage();
        const body = ser(els['sa-table-body']);
        assert.ok(body.includes('signin_failures'), 'rule_key renders');
        assert.ok(body.includes('user:u-1'), 'subject renders as text');
        assert.ok(body.includes('window_min=60'), 'JSONB evidence flattened to text');
        assert.ok(body.includes('ip_address=198.51.100.20'), 'nested evidence keys printed');
        assert.ok(body.includes('bg-red-100 text-red-700'), 'critical severity chip');
        assert.ok(body.includes('bg-amber-100 text-amber-700'), 'medium severity chip');
        assert.ok(body.includes('data-saAction="ack"'), 'Acknowledge button on the open row');
        assert.ok(body.includes('data-saId="101"'), 'button keyed to the alert id');
        assert.ok(!body.includes('data-saId="102"'), 'no Acknowledge button on the acked row');
        assert.ok(body.includes('Already acknowledged'), 'acked row states it');
        assert.ok(body.includes('user:u-9'), 'acked_by renders');
        assert.ok(body.includes('2026-10-07 08:00:00'), 'acked_at renders');
        assert.strictEqual(els['sa-count'].textContent,
            '2 alert(s) for this tenant (status filter: all)');
        assert.ok(els['sa-empty'].className.includes('hidden'),
            'no empty panel while rows exist');
    });

    test('ack -> exactly one POST, row updated in place, no re-GET', async () => {
        const { fns, els, calls, created, ser } = run(onePage(ALERTS), okAck);
        await fns.initializeSecurityAlertsPage();
        const btn = created.find((e) => e.dataset.saId === '101'
            && e.dataset.saAction === 'ack');
        assert.ok(btn, 'the open row carries an Acknowledge button');
        await fns.handleAck(btn);
        assert.strictEqual(calls.ack.length, 1, 'exactly one POST');
        assert.strictEqual(calls.ack[0].alert_id, 101, 'for that alert id');
        assert.strictEqual(calls.list.length, 1, 'no re-GET: the row is patched in place');
        const body = ser(els['sa-table-body']);
        assert.ok(body.includes('Acked'), 'status chip flipped to Acked');
        assert.ok(body.includes('user:u-9 · 2026-10-07 10:15:00'),
            'the server\'s own stamps filled the acked-by/at cell');
        assert.ok(!body.includes('data-saId="101"'), 'the Acknowledge button is gone');
        assert.ok(els['sa-count'].textContent.includes('2 alert(s)'),
            'the row count is unchanged (ack never hides the alert)');
        assert.ok(els['sa-error'].className.includes('hidden'), 'no error left behind');
    });

    test('403 on ack -> read-only: notice, buttons disabled, nothing re-sent', async () => {
        const forbidden = () => ({ __throw: true, status: 403,
            detail: 'Missing required capability: TENANT:manage_settings' });
        const { fns, els, calls, created } = run(onePage(ALERTS), forbidden);
        await fns.initializeSecurityAlertsPage();
        const btn = created.find((e) => e.dataset.saId === '101'
            && e.dataset.saAction === 'ack');
        await fns.handleAck(btn);
        assert.strictEqual(calls.ack.length, 1, 'the POST was attempted once');
        assert.ok(els['sa-notice'].textContent.includes('cannot acknowledge them'),
            'the read-only notice is shown');
        assert.ok(!els['sa-notice'].className.includes('hidden'), 'notice visible');
        assert.ok(els['sa-count'].textContent.includes('read-only'),
            'the count line says read-only');
        const disabled = created.filter((e) => e.attrs['aria-disabled'] === 'true');
        assert.ok(disabled.length >= 1, 'the Acknowledge button is aria-disabled');
        assert.ok(disabled.every((e) => e.disabled === true), 'and really disabled');
        const live = created.filter((e) => e.dataset.saAction === 'ack').pop();
        await fns.handleAck(live);
        assert.strictEqual(calls.ack.length, 1, 'the second Acknowledge sends nothing');
        assert.ok(els['sa-error'].textContent.includes('read-only'));
    });

    test('GET failure surfaced verbatim', async () => {
        const boom = () => ({ __throw: true, status: 500,
            detail: 'Could not list the security alerts' });
        const { fns, els } = run(boom, okAck);
        await fns.initializeSecurityAlertsPage();
        assert.ok(els['sa-error'].textContent.includes('Could not list the security alerts'),
            'the server detail is shown, not swallowed');
        assert.ok(!els['sa-empty'].className.includes('hidden'), 'empty panel shown too');
    });

    test('empty state: no alerts -> empty panel, no rows', async () => {
        const { fns, els, ser } = run(onePage([]), okAck);
        await fns.initializeSecurityAlertsPage();
        assert.strictEqual(ser(els['sa-table-body']).replace(/<\/?tbody[^>]*>/g, ''), '');
        assert.ok(!els['sa-empty'].className.includes('hidden'));
        assert.strictEqual(els['sa-count'].textContent,
            '0 alert(s) for this tenant (status filter: all)');
    });

    test('escaping: hostile subject/evidence cannot inject markup', async () => {
        const hostile = [{ id: 7, rule_key: 'x', severity: 'high', tenant_id: 7,
            subject: '<img src=x onerror=alert(1)>',
            details: { '"><b>hi': '<script>alert(1)</script>', n: 1 },
            first_seen_at: '2026-10-07T09:00:00+00:00',
            last_seen_at: '2026-10-07T09:00:00+00:00', count: 1,
            acked_at: null, acked_by: null }];
        const { fns, els, ser } = run(onePage(hostile), okAck);
        await fns.initializeSecurityAlertsPage();
        const body = ser(els['sa-table-body']);
        assert.ok(!body.includes('<img'), 'no raw tag from the subject field');
        assert.ok(!body.includes('<script'), 'no raw tag from an evidence value');
        assert.ok(body.includes('&lt;img'), 'escaped');
        assert.ok(body.includes('&lt;script'), 'evidence escaped too');
    });

    test('status filter -> the API\'s own acked param; severity adds none', async () => {
        const { fns, els, calls, ser } = run(onePage(ALERTS), okAck);
        await fns.initializeSecurityAlertsPage();
        assert.equal(JSON.stringify(calls.list[0]),
            JSON.stringify({ acked: null, limit: 50, offset: 0 }),
            'all statuses omits acked (api.js drops a null acked)');
        // The click handler calls loadAlerts() itself (fire-and-forget, like a
        // real click), so the test just lets its microtasks drain — it must not
        // issue its own GET, or the param asserts would lie.
        const tick = () => new Promise((r) => setTimeout(r, 0));
        let mark = calls.list.length;
        els['sa-filter-open'].listeners.click();
        await tick();
        assert.ok(calls.list.length > mark, 'the click re-reads the window');
        assert.ok(calls.list.slice(mark).every((c) => c.acked === false),
            'Open only asks the server for acked=false');
        mark = calls.list.length;
        els['sa-filter-acked'].listeners.click();
        await tick();
        assert.ok(calls.list.slice(mark).every((c) => c.acked === true),
            'Acknowledged only asks for acked=true');
        assert.ok(els['sa-filter-acked'].className.includes('bg-slate-200'),
            'the active status button carries the chip');
        assert.ok(!els['sa-filter-open'].className.includes('bg-slate-200'),
            'only one status button is active');
        // Back to all statuses so the severity assert sees the full window.
        mark = calls.list.length;
        els['sa-filter-all'].listeners.click();
        await tick();
        assert.ok(calls.list.slice(mark).every((c) => c.acked === null),
            'All statuses omits acked again');
        const before = calls.list.length;
        els['sa-filter-sev-critical'].listeners.click();
        await tick();
        assert.strictEqual(calls.list.length, before,
            'severity adds NO request — the API has no severity param');
        const body = ser(els['sa-table-body']);
        assert.ok(body.includes('signin_failures'), 'the critical-severity row is shown');
        assert.ok(!body.includes('admin_role_change'),
            'the medium row is narrowed out browser-side');
        assert.ok(els['sa-count'].textContent.includes('the API has no severity param'),
            'the page states where the narrowing happened');
    });

    test('paging: total beyond one window -> offset walks to total', async () => {
        const pages = [
            { items: [{ id: 1, rule_key: 'a', severity: 'high', tenant_id: 7,
                subject: 's1', details: null, first_seen_at: '2026-10-07T09:00:00+00:00',
                last_seen_at: '2026-10-07T09:00:00+00:00', count: 1,
                acked_at: null, acked_by: null },
                { id: 2, rule_key: 'b', severity: 'low', tenant_id: 7,
                    subject: 's2', details: null, first_seen_at: '2026-10-06T09:00:00+00:00',
                    last_seen_at: '2026-10-06T09:00:00+00:00', count: 1,
                    acked_at: null, acked_by: null }],
              total: 5, limit: 2, offset: 0 },
            { items: [{ id: 3, rule_key: 'c', severity: 'high', tenant_id: 7,
                subject: 's3', details: null, first_seen_at: '2026-10-05T09:00:00+00:00',
                last_seen_at: '2026-10-05T09:00:00+00:00', count: 1,
                acked_at: null, acked_by: null },
                { id: 4, rule_key: 'd', severity: 'medium', tenant_id: 7,
                    subject: 's4', details: null, first_seen_at: '2026-10-04T09:00:00+00:00',
                    last_seen_at: '2026-10-04T09:00:00+00:00', count: 1,
                    acked_at: null, acked_by: null }],
              total: 5, limit: 2, offset: 2 },
            { items: [{ id: 5, rule_key: 'e', severity: 'critical', tenant_id: 7,
                subject: 's5', details: null, first_seen_at: '2026-10-03T09:00:00+00:00',
                last_seen_at: '2026-10-03T09:00:00+00:00', count: 1,
                acked_at: null, acked_by: null }],
              total: 5, limit: 2, offset: 4 },
        ];
        let n = 0;
        const { fns, els, calls, ser } = run(() => pages[n++], okAck);
        await fns.initializeSecurityAlertsPage();
        assert.strictEqual(calls.list.length, 3, 'three windows walked');
        assert.strictEqual(els['sa-count'].textContent,
            '5 alert(s) for this tenant (status filter: all)');
        const body = ser(els['sa-table-body']);
        assert.ok(body.includes('s5'), 'the last page\'s row is on screen too');
    });
});

// api.js-level check: the exported securityAlerts.list() must build the query
// string the router actually accepts (acked/limit/offset) and ack() must POST
// with NO body (the endpoint has no note field).
test('api.js securityAlerts builds only the params the router has', async () => {
    const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');
    const at = apiSrc.indexOf('export const securityAlerts = {');
    assert.ok(at >= 0, 'securityAlerts is exported from api.js');
    const end = apiSrc.indexOf('};', at);
    const objSrc = apiSrc.slice(at + 'export '.length, end + 2);
    const seen = [];
    const fetchApi = (url) => { seen.push(url); return Promise.resolve({}); };
    const obj = vm.runInNewContext(`${objSrc}\nsecurityAlerts;`,
        { fetchApi, seen, URLSearchParams: globalThis.URLSearchParams });
    await obj.list({});
    assert.strictEqual(seen[seen.length - 1], '/tenant/security-alerts',
        'no params -> bare path');
    await obj.list({ acked: false, limit: 50, offset: 50 });
    assert.strictEqual(seen[seen.length - 1],
        '/tenant/security-alerts?acked=false&limit=50&offset=50',
        'acked/limit/offset only');
    await obj.list({ acked: true });
    assert.strictEqual(seen[seen.length - 1], '/tenant/security-alerts?acked=true');
    await obj.list({ acked: null, limit: 0, offset: 0 });
    assert.strictEqual(seen[seen.length - 1], '/tenant/security-alerts',
        'null/zero values are omitted');
    await obj.ack(101);
    assert.strictEqual(seen[seen.length - 1], '/tenant/security-alerts/101/ack',
        'ack path is id-encoded and bodyless');
});
