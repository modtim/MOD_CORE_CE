// Node-runnable check for the GSEC FX.106 p2b Blocked-IPs page
// (CORE/Dashboard/js/ip-blocks.js). ip-blocks.js is an ES module, so this
// script extracts the body (from `function esc(` to EOF, with the `export `
// keyword stripped) and runs it in a vm sandbox with stubbed document
// elements, ipBlocks + me (api.js), withButtonBusy (utils.js), window.showInfo
// and confirm(), then asserts:
//   (a) golden path: list + me load, rows render (reason, tripped/unblocks
//       clocks, left-seconds, 429 tally, Unblock action), the count line
//       tallies blocks + hits, and ib-scope prints the per-process honesty
//       line VERBATIM from the payload's scope field;
//   (b) settings: the read-only line renders the server's knobs (thresholds,
//       window, duration, exempt CIDRs) — the page never edits a knob;
//   (c) row unblock: confirm() is asked with the per-process warning, the
//       reason field is REQUIRED before the POST (empty reason -> error line,
//       no POST, no audit), reason set -> POST {ip, reason}; a declined
//       confirm sends nothing;
//   (d) form unblock: no IP typed -> error line, no POST; IP + reason ->
//       POST; the server's 404 detail ("nothing blocked on this process") is
//       surfaced verbatim in ib-error;
//   (e) bad IP: the server's 400 detail is surfaced verbatim, block intact;
//   (f) empty state: no blocks -> empty panel, no table rows; no hits ->
//       hits empty panel;
//   (g) platform-admin gate: me.is_platform_admin false -> no rows, gate line;
//   (h) escaping: a hostile ip/reason/detail cannot inject markup;
//   (i) hits table: 429 ring rows + auto_block/auto_unblock audit rows render
//       with epoch clocks, tenant id (or 'platform') and the server's detail.
//
//   node --test CORE/Dashboard/tests/ip-blocks-page.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'ip-blocks.js'), 'utf8');
const start = src.indexOf('function esc(s)');
assert.ok(start >= 0, 'could not locate the ip-blocks body');
const block = src.slice(start).replace('export async function', 'async function');

const IDS = ['ib-scope', 'ib-settings', 'ib-error', 'ib-count', 'ib-empty',
    'ib-list', 'ib-table-body', 'ib-hits-count', 'ib-hits-empty',
    'ib-hits', 'ib-hits-body', 'ib-unblock-ip', 'ib-unblock-reason',
    'ib-unblock-btn', 'ib-settings-link'];

function run(list, user, unblock, confirmOk, reasonValue, formIp) {
    const els = {};
    for (const id of IDS) {
        const el = { id, value: '', disabled: false, className: 'text-sm',
            textContent: '', innerHTML: '', hiddenRemoved: false, dataset: {}, listeners: {} };
        el.classList = { remove: (c) => { if (c === 'hidden') el.hiddenRemoved = true; },
            add: () => {}, toggle: () => {} };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        el.querySelectorAll = () => [];
        els[id] = el;
    }
    els['ib-unblock-reason'].value = reasonValue || '';
    els['ib-unblock-ip'].value = formIp || '';
    const calls = { list: [], unblock: [], info: [], confirms: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        confirm: (msg) => { calls.confirms.push(msg); return confirmOk; },
        document: { getElementById: (id) => els[id],
            querySelectorAll: () => [], querySelector: () => null },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        ipBlocks: {
            list: (opts) => { calls.list.push(opts); return Promise.resolve(list(opts)); },
            unblock: (ip, reason) => {
                calls.unblock.push({ ip, reason });
                // fetchApi throws on a non-2xx; a resolver returning a
                // {detail} body models that throw (404/400 paths).
                const res = unblock(ip, reason);
                if (res && res.detail !== undefined) {
                    return Promise.reject(res);
                }
                return Promise.resolve(res);
            },
        },
        me: { get: () => Promise.resolve(user()) },
        withButtonBusy: (btn, action) => {
            if (btn.disabled) return Promise.resolve(undefined);
            btn.disabled = true;
            const p = action();
            const restore = () => { btn.disabled = false; };
            return p.then(restore, restore);
        },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeIpBlocksPage, loadTable, loadHits, renderSettings, handleUnblock, handleFormUnblock, rowHtml, hitRowHtml, _settingsLine });`,
        sandbox, { filename: 'ip-blocks-page-body' });
    return { fns, els, calls };
}

const ADMIN = { parent_user_id: null, is_platform_admin: true };
const NON_ADMIN = { parent_user_id: 5, is_platform_admin: false };

const DAY = 86400000;
function iso(daysAgo) { return new Date(Date.now() - daysAgo * DAY).toISOString(); }
const EPOCH = (Date.now() - 3 * 3600 * 1000) / 1000;

const BLOCKS = [
    { ip: '198.51.100.20', reason: '25 x 429 in 15m',
      tripped_at: iso(0), expires_at: iso(-0.02), remaining_seconds: 1500,
      hit_count_429: 25 },
    { ip: '203.0.113.9', reason: '20 failed sign-ins',
      tripped_at: iso(0), expires_at: iso(-0.04), remaining_seconds: 2400,
      hit_count_429: 0 },
];
const HITS = [
    { kind: 'auto_block', timestamp: EPOCH, ip_address: '198.51.100.20',
      tenant_id: null, detail: '25 x 429 in 15m' },
    { kind: 'rate_limit_hit', timestamp: EPOCH + 60, ip_address: '198.51.100.20',
      tenant_id: 7, detail: '429 on bucket api-key:12 (limit 300/60s)' },
    { kind: 'auto_unblock', timestamp: EPOCH + 120, ip_address: '203.0.113.9',
      tenant_id: 7, detail: 'false positive: our own CI box' },
];
const SETTINGS = { auto_block_enabled: true, block_after_429s: 25,
    block_after_login_failures: 20, window_min: 15, duration_min: 60,
    exempt_cidrs: ['127.0.0.0/8', '100.64.0.0/10'] };

function payload() {
    return { scope: 'this_process', blocks: BLOCKS, recent_hits: HITS,
        settings: SETTINGS, counts: { active_blocks: 2, recent_hits: 3 } };
}

function okUnblock(ip) {
    return { ip, unblocked: true, scope: 'this_process',
        reason: 'false positive: our own CI box', prior_block_events: 1 };
}
function missingUnblock(ip) {
    return { detail: 'no active auto-block for ' + ip +
        ' on this process (it may already have expired, or the block lives on another API replica)' };
}
function badIpUnblock(ip) {
    return { detail: 'Invalid IP address: not an IP address' };
}

test.describe('ip-blocks-page', () => {
    test('golden path: rows render, scope line is honest', async () => {
        const { fns, els, calls } = run(payload, () => ADMIN, okUnblock, true,
            'false positive: our own CI box');
        await fns.initializeIpBlocksPage();
        assert.ok(calls.list.length === 1, 'one list GET');
        assert.equal(JSON.stringify(calls.list[0]), '{"limit":200}');
        const body = els['ib-table-body'].innerHTML;
        assert.ok(body.includes('198.51.100.20'), 'blocked IP renders');
        assert.ok(body.includes('25 x 429 in 15m'), 'reason renders');
        assert.ok(body.includes('data-ib-action="unblock"'), 'Unblock button renders');
        assert.ok(body.includes('>1500s<'), 'left-seconds renders');
        assert.ok(els['ib-count'].textContent === '2 blocked IP(s) · 3 recent hit(s)');
        const scope = els['ib-scope'].textContent;
        assert.ok(scope.startsWith('Scope: THIS API process only'),
            'the per-process scope is stated, never hidden');
        assert.ok(scope.includes('audit rows are the process-independent record'));
        assert.ok(els['ib-empty'].hiddenRemoved === false, 'no empty panel with rows');
    });

    test('settings render read-only from the server payload', async () => {
        const { fns, els } = run(payload, () => ADMIN, okUnblock, true, 'x');
        await fns.initializeIpBlocksPage();
        const line = els['ib-settings'].textContent;
        assert.ok(line.includes('auto-block ON'), 'enabled renders');
        assert.ok(line.includes('block after 25 x 429 or 20 failed sign-ins'));
        assert.ok(line.includes('window 15 min') && line.includes('block lasts 60 min'));
        assert.ok(line.includes('exempt 127.0.0.0/8, 100.64.0.0/10'));
    });

    test('row unblock: reason required before the POST', async () => {
        const { fns, els, calls } = run(payload, () => ADMIN, okUnblock, true, '');
        await fns.initializeIpBlocksPage();
        fns.handleUnblock('198.51.100.20', null);
        assert.strictEqual(calls.unblock.length, 0, 'no POST without a reason');
        assert.ok(els['ib-error'].textContent.includes('an unblock needs a reason')
            || els['ib-error'].textContent.includes('needs a reason'),
            'the reason requirement is explained');
        assert.ok(els['ib-error'].hiddenRemoved === true);
    });

    test('form unblock: no IP -> no POST', async () => {
        const { fns, els, calls } = run(payload, () => ADMIN, okUnblock, true,
            'because', '');
        await fns.initializeIpBlocksPage();
        await fns.handleFormUnblock();
        assert.strictEqual(calls.unblock.length, 0);
        assert.ok(els['ib-error'].textContent.includes('Type the IP to unblock'));
    });

    test('form unblock: IP + reason -> POST, confirm names the scope', async () => {
        const { fns, els, calls } = run(payload, () => ADMIN, okUnblock, true,
            'false positive: our own CI box', '198.51.100.20');
        await fns.initializeIpBlocksPage();
        await fns.handleFormUnblock();
        assert.strictEqual(calls.unblock.length, 1);
        assert.deepStrictEqual(calls.unblock[0],
            { ip: '198.51.100.20', reason: 'false positive: our own CI box' });
        assert.strictEqual(calls.confirms.length, 1);
        assert.ok(calls.confirms[0].includes('THIS process only'),
            'the confirm copy repeats the scope honesty');
        assert.ok(calls.info.some(m => m.includes('cleared')),
            'success line shown');
    });

    test('declined confirm sends nothing', async () => {
        const { fns, calls } = run(payload, () => ADMIN, okUnblock, false,
            'false positive: our own CI box', '198.51.100.20');
        await fns.initializeIpBlocksPage();
        await fns.handleFormUnblock();
        assert.strictEqual(calls.unblock.length, 0);
    });

    test('server 404 surfaced verbatim (nothing blocked here)', async () => {
        const { fns, els, calls } = run(payload, () => ADMIN, missingUnblock, true,
            'nothing there', '198.51.100.99');
        await fns.initializeIpBlocksPage();
        await fns.handleFormUnblock();
        assert.strictEqual(calls.unblock.length, 1);
        assert.ok(els['ib-error'].textContent.includes('this process'),
            'the 404 detail is shown, not swallowed');
    });

    test('server 400 surfaced verbatim (bad IP)', async () => {
        const { fns, els, calls } = run(payload, () => ADMIN, badIpUnblock, true,
            'typo in the address', 'not-an-ip');
        await fns.initializeIpBlocksPage();
        await fns.handleFormUnblock();
        assert.strictEqual(calls.unblock.length, 1);
        assert.ok(els['ib-error'].textContent.includes('Invalid IP address'));
    });

    test('empty state: no blocks, no hits', async () => {
        const emptyPayload = () => ({ scope: 'this_process', blocks: [],
            recent_hits: [], settings: SETTINGS,
            counts: { active_blocks: 0, recent_hits: 0 } });
        const { fns, els } = run(emptyPayload, () => ADMIN, okUnblock, true, 'x');
        await fns.initializeIpBlocksPage();
        assert.strictEqual(els['ib-table-body'].innerHTML, '');
        assert.ok(els['ib-empty'].hiddenRemoved === true);
        assert.ok(els['ib-hits-empty'].hiddenRemoved === true);
        assert.strictEqual(els['ib-hits-count'].textContent, '0 recent limit event(s)');
    });

    test('hits table renders ring + audit rows', async () => {
        const { fns, els } = run(payload, () => ADMIN, okUnblock, true, 'x');
        await fns.initializeIpBlocksPage();
        const body = els['ib-hits-body'].innerHTML;
        assert.ok(body.includes('auto_block') && body.includes('auto_unblock')
            && body.includes('rate_limit_hit'));
        assert.ok(body.includes('platform'), 'tenant_id null prints platform');
        assert.ok(body.includes('429 on bucket api-key:12'), 'server detail verbatim');
    });

    test('platform-admin gate: non-admin sees no rows', async () => {
        const { fns, els, calls } = run(payload, () => NON_ADMIN, okUnblock, true, 'x');
        await fns.initializeIpBlocksPage();
        assert.strictEqual(els['ib-table-body'].innerHTML, '');
        assert.ok(els['ib-error'].textContent.includes('platform-admin surface'));
    });

    test('escaping: hostile ip/reason/detail cannot inject markup', async () => {
        const hostile = () => ({ scope: 'this_process',
            blocks: [{ ip: '<img src=x onerror=alert(1)>', reason: '"><b>x',
                tripped_at: null, expires_at: null, remaining_seconds: 5,
                hit_count_429: 1 }],
            recent_hits: [], settings: SETTINGS,
            counts: { active_blocks: 1, recent_hits: 0 } });
        const { fns, els } = run(hostile, () => ADMIN, okUnblock, true, 'x');
        await fns.initializeIpBlocksPage();
        const body = els['ib-table-body'].innerHTML;
        assert.ok(!body.includes('<img'), 'no raw tag from the ip field');
        assert.ok(body.includes('&lt;img'), 'escaped');
    });
});
