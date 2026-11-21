// Node-runnable check for the Wizard Queue panel helpers
// (Dashboard/js/wizard-queue.js, 510 lines, 0 test refs before this file).
// The module is an ES module (imports fetchApi + me from api.js), so this
// script extracts the pure helper block (from `const PAGE_SIZE` to the end of
// isOwnScope) and runs it in a vm sandbox. Asserts:
//   (a) isOwnScope (the belt-and-braces client scope filter mirroring
//       services/wizard_queue.py::can_access): platform admin sees platform-
//       level runs ONLY; a tenant admin sees their tenant's non-platform runs;
//       a regular member sees their own runs only; null run/user -> false;
//   (b) escapeHtml: five entities, null -> '' (run names are user text);
//   (c) _formatDuration: null -> '', clamps negatives, s / m+s / h+m buckets;
//   (d) TERMINAL_STATUSES: succeeded/failed/canceled are terminal, running is
//       not (drives the cancel/retry buttons on a row);
//   (e) STATUS_BADGE_CLASS + WIZARD_TYPE_LABEL: every status the API can
//       return has a brand-token badge class, every wizard type has copy.
//
//   node --test CORE/Dashboard/tests/wizard-queue-scope.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'wizard-queue.js'), 'utf8');
const start = src.indexOf('const PAGE_SIZE =');
const end = src.indexOf('// --- Resilient list-refresh poller');
assert.ok(start >= 0 && end > start, 'could not locate the wizard-queue helper block');
const block = src.slice(start, end).replace(/^export /gm, '');

const sandbox = {
    console: { log: () => {}, error: () => {} },
    Math: global.Math,
};
const fns = vm.runInNewContext(
    `"use strict";\n${block}\n({ PAGE_SIZE, WIZARD_TYPE_LABEL, STATUS_BADGE_CLASS, TERMINAL_STATUSES, escapeHtml, _formatDuration, isOwnScope });`,
    sandbox, { filename: 'wizard-queue-body' });

const ADMIN = { id: 1, tenant_id: 7, parent_user_id: null, is_platform_admin: true };
const TENANT_ADMIN = { id: 2, tenant_id: 7, parent_user_id: null, is_platform_admin: false };
const MEMBER = { id: 3, tenant_id: 7, parent_user_id: 2, is_platform_admin: false };
const OTHER_MEMBER = { id: 4, tenant_id: 7, parent_user_id: 2, is_platform_admin: false };

const PLATFORM_RUN = { is_platform_level: true, tenant_id: 7, owner_user_id: 1 };
const TENANT_RUN = { is_platform_level: false, tenant_id: 7, owner_user_id: 3 };
const OTHER_TENANT_RUN = { is_platform_level: false, tenant_id: 9, owner_user_id: 3 };

test('isOwnScope: platform admin sees platform-level runs only', () => {
    assert.strictEqual(fns.isOwnScope(PLATFORM_RUN, ADMIN), true);
    assert.strictEqual(fns.isOwnScope(TENANT_RUN, ADMIN), false,
        'platform admin must not see a tenant run');
    assert.strictEqual(fns.isOwnScope(OTHER_TENANT_RUN, ADMIN), false);
});

test('isOwnScope: tenant admin sees own tenant non-platform runs only', () => {
    assert.strictEqual(fns.isOwnScope(TENANT_RUN, TENANT_ADMIN), true);
    assert.strictEqual(fns.isOwnScope(OTHER_TENANT_RUN, TENANT_ADMIN), false,
        'other tenant leaked');
    assert.strictEqual(fns.isOwnScope(PLATFORM_RUN, TENANT_ADMIN), false,
        'platform-level run leaked to a tenant admin');
});

test('isOwnScope: regular member sees own runs only', () => {
    assert.strictEqual(fns.isOwnScope(TENANT_RUN, MEMBER), true);
    assert.strictEqual(fns.isOwnScope({ ...TENANT_RUN, owner_user_id: 9 }, MEMBER), false,
        'another member run leaked');
    assert.strictEqual(fns.isOwnScope({ ...TENANT_RUN, owner_user_id: null }, MEMBER), false,
        'null owner must not match');
    assert.strictEqual(fns.isOwnScope(PLATFORM_RUN, MEMBER), false);
    assert.strictEqual(fns.isOwnScope({ ...OTHER_TENANT_RUN, owner_user_id: 9 }, MEMBER), false);
});

test('isOwnScope: null run or null user -> false', () => {
    assert.strictEqual(fns.isOwnScope(null, ADMIN), false);
    assert.strictEqual(fns.isOwnScope(TENANT_RUN, null), false);
    assert.strictEqual(fns.isOwnScope(null, null), false);
});

test('escapeHtml: five entities, null -> empty', () => {
    assert.strictEqual(fns.escapeHtml(null), '');
    assert.strictEqual(fns.escapeHtml(`<a href="x" onerror='y'>&`),
        '&lt;a href=&quot;x&quot; onerror=&#39;y&#39;&gt;&amp;');
});

test('_formatDuration: null blank, negatives clamp, s / m+s / h+m buckets', () => {
    assert.strictEqual(fns._formatDuration(null), '');
    assert.strictEqual(fns._formatDuration(-30), '0s');
    assert.strictEqual(fns._formatDuration(0), '0s');
    assert.strictEqual(fns._formatDuration(59.4), '59s');
    assert.strictEqual(fns._formatDuration(60), '1m 0s');
    assert.strictEqual(fns._formatDuration(90), '1m 30s');
    assert.strictEqual(fns._formatDuration(3599), '59m 59s');
    assert.strictEqual(fns._formatDuration(3600), '1h 0m');
    assert.strictEqual(fns._formatDuration(7320), '2h 2m');
});

test('TERMINAL_STATUSES: succeeded/failed/canceled only', () => {
    const t = fns.TERMINAL_STATUSES;
    for (const s of ['succeeded', 'failed', 'canceled']) assert.ok(t.has(s), `${s} missing`);
    for (const s of ['queued', 'running', 'verifying', 'waiting-for-worker'])
        assert.ok(!t.has(s), `${s} must not be terminal`);
});

test('STATUS_BADGE_CLASS and WIZARD_TYPE_LABEL cover the API vocabulary', () => {
    const badge = fns.STATUS_BADGE_CLASS;
    for (const s of ['queued', 'waiting-for-worker', 'running', 'verifying',
                     'waiting-for-user', 'succeeded', 'failed', 'canceled']) {
        assert.ok(typeof badge[s] === 'string' && badge[s], `no badge for ${s}`);
        assert.ok(!/#[0-9a-fA-F]{3,8}/.test(badge[s]), 'badge must be a token class, not a hex literal');
    }
    const label = fns.WIZARD_TYPE_LABEL;
    assert.strictEqual(label.node, 'Node Wizard');
    assert.strictEqual(label.stack, 'Stack Wizard');
    assert.strictEqual(label['agent-project'], 'Agent / Project');
    assert.strictEqual(label['build-repair'], 'Build Repair');
    assert.strictEqual(label['container-adapter'], 'Container Adapter');
    assert.strictEqual(fns.PAGE_SIZE, 20, 'page size must match the API page size');
});
