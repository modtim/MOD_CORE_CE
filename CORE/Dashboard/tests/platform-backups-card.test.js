'use strict';
// Node-runnable check for the FX.84 p2c Backups card in Dashboard/js/
// platform-backups.js (System -> Settings child, platform-admin only).
// The module is an ES module, so this script extracts the contiguous
// block (from the "Backups card block" marker to EOF) and runs it in a
// vm sandbox with stubbed document elements, platformBackups (api.js),
// withButtonBusy (utils.js) and window.showInfo, then asserts:
//   (a) golden path: GET renders settings/last-run/restore-tests, a valid
//       submit PUTs only the filled fields, success toast, "Saved.";
//   (b) client-side validation: bad cron blocks the PUT (no API call);
//   (c) server 422 validator message surfaced verbatim;
//   (d) in-flight guard: Save disabled during the round-trip;
//   (e) role gate: non-platform-admins never reveal the card, GET never
//       called (mirrors the router's _require_platform_admin);
//   (f) secret values never rendered: masked credential shown as the
//       mask, unmasked value under a secret-ish key replaced by [hidden];
//   (g) ledger_readable=false reported, not a crash;
//   (h) FX.58 s5 — the "Second off-site copy" line renders only when the
//       GET payload carries a `secondary` object (unset target = no line).
//
//   node --test CORE/Dashboard/tests/platform-backups-card.test.js
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'platform-backups.js'), 'utf8');
const start = src.indexOf('// FX.84 p2c — Backups card block');
assert.ok(start >= 0, 'could not locate the backups block');
const block = src.slice(start).replace('export async function', 'async function');

function run(get, put) {
    const els = {};
    const ids = ['platform-backups-card', 'pb-readings', 'pb-banner', 'pb-last-run',
        'pb-restore-tests', 'pb-form', 'pb-status', 'pb-save-btn',
        'pb-f-schedule-enabled', 'pb-f-schedule-cron', 'pb-f-retention-days',
        'pb-f-monthly-keep', 'pb-f-offbox-target', 'pb-f-offbox-path',
        'pb-f-restore-test-monthly'];
    for (const id of ids) {
        const el = { id, value: '', disabled: false, className: 'text-sm', textContent: '', innerHTML: '', hiddenRemoved: false, dataset: {}, listeners: {} };
        el.classList = { remove: (c) => { if (c === 'hidden') el.hiddenRemoved = true; }, add: () => {} };
        el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
        els[id] = el;
    }

    const calls = { get: 0, put: [], info: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: { getElementById: (id) => els[id] },
        window: { showInfo: (msg, title) => { calls.info.push(msg); } },
        Date: global.Date,
        JSON: global.JSON,
        platformBackups: {
            get: () => { calls.get++; return Promise.resolve(get()); },
            update: (payload) => { calls.put.push(payload); return put(); },
        },
        withButtonBusy: (btn, action) => {
            if (btn.disabled) return Promise.resolve(undefined);
            btn.disabled = true;
            const p = action();
            const restore = () => { btn.disabled = false; };
            return p.then(restore, restore);
        },
    };
    const init = vm.runInNewContext(
        `"use strict";\n${block}\ninitializePlatformBackupsPanel;`, sandbox,
        { filename: 'platform-backups-card-block' });
    return { init, els, calls };
}

const PLATFORM_ADMIN = { parent_user_id: 7, is_platform_admin: true };
const TENANT_ADMIN = { parent_user_id: null, is_platform_admin: false };

const READINGS = () => ({
    settings: { schedule_enabled: true, schedule_cron: '0 2 * * *', retention_days: 35,
        monthly_keep: 12, offbox_target: 'none', offbox_path: '', restore_test_monthly: true },
    last_run: { last_success_utc: '2026-10-05T02:00:00Z', verified: true, overall: 'ok', last_error: null },
    restore_tests: [{ ts: '2026-10-01T00:10:00Z', backup_id: 'set-2026-10-01', ok: true, rto_seconds: 900.5, rpo_seconds: 120, error: null }],
    last_restore_test: { ts: '2026-10-01T00:10:00Z', backup_id: 'set-2026-10-01', ok: true },
    ledger_readable: true,
});

test('golden path: GET renders, valid submit PUTs, toasts, re-enables', async () => {
    const { init, els, calls } = run(READINGS, () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    assert.strictEqual(calls.get, 1, 'GET not called for platform admin');
    assert.ok(els['platform-backups-card'].hiddenRemoved, 'card not revealed');
    assert.ok(/cron 0 2 \* \* \*/.test(els['pb-readings'].innerHTML), 'settings not rendered');
    assert.ok(/last_success_utc: 2026-10-05/.test(els['pb-last-run'].textContent), 'last run not rendered');
    assert.ok(/passed/.test(els['pb-restore-tests'].innerHTML), 'restore tests not rendered');
    els['pb-f-retention-days'].value = '45';
    els['pb-f-monthly-keep'].value = '24';
    await els['pb-form'].listeners.submit({ preventDefault: () => {} });
    assert.strictEqual(calls.put.length, 1, 'PUT not sent');
    assert.deepEqual(calls.put[0], { retention_days: 45, monthly_keep: 24 });
    assert.ok(calls.info.includes('Backup settings saved.'));
    assert.ok(/Saved\./.test(els['pb-status'].textContent));
    assert.strictEqual(els['pb-save-btn'].disabled, false);
});

test('client-side validation: bad cron blocks the PUT', async () => {
    const { init, els, calls } = run(READINGS, () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    els['pb-f-schedule-cron'].value = 'every night';
    await els['pb-form'].listeners.submit({ preventDefault: () => {} });
    assert.strictEqual(calls.put.length, 0, 'PUT sent despite bad cron');
    assert.ok(/5 whitespace-separated cron/.test(els['pb-status'].textContent));
});

test('server 422 validator message surfaced verbatim', async () => {
    const { init, els, calls } = run(READINGS, () => {
        const e = new Error('API request failed with status 422');
        e.detail = 'backup.offbox_target must be one of (none, s3, nfs, disk, ssh), got \'ftp\'';
        throw e;
    });
    await init(PLATFORM_ADMIN);
    els['pb-f-offbox-target'].value = 'ftp';
    await els['pb-form'].listeners.submit({ preventDefault: () => {} });
    assert.strictEqual(calls.put.length, 1, 'PUT never attempted');
    assert.ok(/one of \(none, s3, nfs, disk, ssh\)/.test(els['pb-status'].textContent),
        'server detail not surfaced verbatim');
    assert.strictEqual(els['pb-save-btn'].disabled, false);
});

test('in-flight guard: Save disabled during the round-trip', async () => {
    const { init, els, calls } = run(READINGS, async () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    els['pb-f-retention-days'].value = '45';
    const pending = els['pb-form'].listeners.submit({ preventDefault: () => {} });
    await Promise.resolve();
    assert.strictEqual(els['pb-save-btn'].disabled, true, 'not disabled while in flight');
    await pending;
    assert.strictEqual(els['pb-save-btn'].disabled, false);
});

test('role gate: non-platform-admin never sees the card, GET never called', async () => {
    const { init, els, calls } = run(READINGS, () => ({ updated: {} }));
    await init(TENANT_ADMIN);
    assert.strictEqual(calls.get, 0, 'GET called for non-platform-admin');
    assert.ok(!els['platform-backups-card'].hiddenRemoved, 'card revealed to non-platform-admin');
});

test('secret values never rendered: mask kept, raw hidden', async () => {
    const { init, els, calls } = run(() => ({
        settings: READINGS().settings,
        last_run: { s3_access_key: 'AK****MASK****', s3_secret_key: 'raw-secret-value', overall: 'ok' },
        restore_tests: [], ledger_readable: true,
    }), () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    const rendered = els['pb-last-run'].textContent;
    assert.ok(/AK\*\*\*\*MASK\*\*\*\*/.test(rendered), 'mask not shown');
    assert.ok(!/raw-secret-value/.test(rendered), 'raw secret rendered');
    assert.ok(/\[hidden\]/.test(rendered), 'secret not replaced with [hidden]');
});

test('FX.58 s5: second off-site copy line rendered when secondary present', async () => {
    const { init, els } = run(() => ({
        settings: READINGS().settings, last_run: null, restore_tests: [],
        last_restore_test: null, ledger_readable: true,
        secondary: { overall: 'ok', verified: true, last_success_utc: '2026-10-06T02:00:00Z' },
    }), () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    const html = els['pb-readings'].innerHTML;
    assert.ok(/Second off-site copy/.test(html), 'secondary line not rendered');
    assert.ok(/ok · last verified 2026-10-06T02:00:00Z/.test(html), 'status/time not rendered');
});

test('FX.58 s5: no second off-site copy line when secondary is absent', async () => {
    const { init, els } = run(READINGS, () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    assert.ok(!/Second off-site copy/.test(els['pb-readings'].innerHTML),
        'secondary line rendered when the second target is unset');
});

test('ledger_readable=false reported, not a crash', async () => {
    const { init, els, calls } = run(() => ({
        settings: READINGS().settings, last_run: null, restore_tests: [], last_restore_test: null,
        ledger_readable: false,
    }), () => ({ updated: {} }));
    await init(PLATFORM_ADMIN);
    assert.ok(/ledger not readable/i.test(els['pb-restore-tests'].textContent));
    assert.ok(/No backup run recorded yet/.test(els['pb-last-run'].textContent));
});
