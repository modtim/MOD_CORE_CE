// rc20e — Compliance Center shows ZERO tabs on CE: audit-logs.js used to HIDE
// #main-tab-audit-logs / #main-panel-audit-logs / #audit-logs-container when
// isPaidEdition() said CE. That contradicted open-core/manifest.yaml:2182
// (deny_exception re-shipping the hub shell "hosting the CE-core audit-logs tab")
// and hub-compliance.js _TAB_GATE['audit-logs'] = 'tenant' ("Visible on CE and
// paid"): audit-logs is TAB_KEYS[0] and every other hub tab is CE-carved, so
// hiding it leaves zero visible tabs. The router ships on CE (manifest.yaml:165
// api_allow CORE/API/routers/audit_logs.py; main.py:2120 mount) and the paid
// surface (tamper-evident read/export, audit-integrity capability) is gated SERVER-SIDE with
// a 402 the page already renders as #audit-plan-gate (loadAuditLogs :179,
// downloadExport :376/:398). Fix: wire on every edition, hide nothing here.
//
//   node --test CORE/Dashboard/tests/rc20e-audit-logs-ce-tab.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'audit-logs.js'), 'utf8');

// --- 1) static shape: the export wires directly and hides nothing ----------
const initIdx = src.search(/export\s+function\s+initializeAuditLogsPage\s*\(\s*\)\s*\{/);
assert.ok(initIdx >= 0, 'audit-logs.js must export initializeAuditLogsPage()');
const initBody = src.slice(initIdx, initIdx + 1600);
assert.ok(/_wire\(\);/.test(initBody),
    'initializeAuditLogsPage() must invoke _wire()');
for (const id of ['main-tab-audit-logs', 'main-panel-audit-logs', 'audit-logs-container']) {
    assert.ok(!new RegExp(`getElementById\\('${id}'\\)\\s*\\?\\.classList\\.add\\('hidden'\\)`).test(initBody),
        `initializeAuditLogsPage() must NOT hide #${id} on CE (CE-core tab; rc20e zero-tab bug)`);
}

// --- 2) behavioural: run _wire()+init in a vm sandbox, CE and paid ---------
function makeEl(id) {
    const el = { id, value: '', disabled: false, className: '', dataset: {}, listeners: {} };
    el.classList = {
        add: (c) => { if (c === 'hidden') el._hidden = true; },
        remove: (c) => { if (c === 'hidden') el._hidden = false; },
        toggle: () => {},
    };
    el.addEventListener = (type, fn) => { el.listeners[type] = fn; };
    return el;
}

async function drain(n) { for (let i = 0; i < n; i++) await Promise.resolve(); }

function runInit(paid) {
    const els = {};
    for (const id of ['audit-logs-container', 'main-tab-audit-logs', 'main-panel-audit-logs',
        'close-detail-modal', 'apply-filters', 'reset-filters', 'filter-search',
        'filter-action', 'filter-resource-type', 'filter-date-range', 'audit-plan-gate',
        'audit-page-download-csv', 'audit-page-download-json', 'log-detail-modal',
        'prev-page', 'next-page', 'results-info']) els[id] = makeEl(id);
    const calls = { loadAuditLogs: 0 };
    const sandbox = {
        console,
        document: { getElementById: (id) => els[id] || null, querySelectorAll: () => [] },
        window: {},
        setTimeout: (fn, ms) => { void fn; void ms; return 1; },
        clearTimeout: () => {},
        withButtonBusy: (btn, action) => action(),
        loadAuditLogs: () => { calls.loadAuditLogs++; },
        getSearchValue: () => '',
        copyAuditMeta: () => {},
        downloadExport: () => {},
        isPaidEdition: () => Promise.resolve(paid),
    };
    const start = src.indexOf('function _wire()');
    assert.ok(start >= 0, 'audit-logs.js must define _wire()');
    const block = src.slice(start).replace(/export /g, '');
    const init = vm.runInNewContext(
        `"use strict";\nlet currentFilters = {}; let currentPage = 0; let searchDebounceTimer = null;\n${block}\ninitializeAuditLogsPage;`,
        sandbox, { filename: 'audit-logs-init' });
    const ret = init();
    if (ret !== undefined) sandbox.isPaidEdition = () => Promise.resolve(paid);
    return { ret, els, calls };
}

test('CE: audit-logs tab + panel stay visible (not hidden), wiring runs', async () => {
    const r = runInit(false);
    if (r.ret !== undefined) await r.ret;
    await drain(50);
    assert.strictEqual(r.els['main-tab-audit-logs']._hidden, undefined,
        'on CE, #main-tab-audit-logs must NOT be hidden (CE-core tab)');
    assert.strictEqual(r.els['main-panel-audit-logs']._hidden, undefined,
        'on CE, #main-panel-audit-logs must NOT be hidden');
    assert.strictEqual(r.els['audit-logs-container']._hidden, undefined,
        'on CE, #audit-logs-container must NOT be hidden');
    assert.ok(r.calls.loadAuditLogs >= 1,
        'on CE, the tab must wire (first loadAuditLogs() runs) — paid read stays 402-gated in-page');
});

test('paid: tab + panel visible, wiring runs (unchanged)', async () => {
    const r = runInit(true);
    if (r.ret !== undefined) await r.ret;
    await drain(50);
    for (const id of ['main-tab-audit-logs', 'main-panel-audit-logs', 'audit-logs-container'])
        assert.strictEqual(r.els[id]._hidden, undefined, `on paid, #${id} must NOT be hidden`);
    assert.ok(r.calls.loadAuditLogs >= 1, 'on paid, the tab must wire');
});

console.log('rc20e-audit-logs-ce-tab: OK');
