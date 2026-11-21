// Node-runnable check for the FX.88 p4 admin Reset 2FA button in
// Dashboard/js/users.js (the GY.C4.MFA / GSEC FX.88 admin 2FA reset UI).
//
// users.js is an ES module, so this script extracts the contiguous block
// holding _escapeHtml / _currentUsersTenantId / the action handlers (from the
// escaper comment down to the Locked-accounts banner) and runs it in a vm
// sandbox with stubbed showConfirm / showInfo / showError / loadUsers /
// users.resetMfa and a fake button, then asserts:
//   (a) golden path: confirm -> POST body sent, success toast, users reloaded,
//       button re-enabled;
//   (b) cancel path: no API call, no toast, button re-enabled;
//   (c) error path: the API's own detail (409 exempt service account) is
//       surfaced verbatim in the error toast, button re-enabled;
//   (d) in-flight guard: the button is disabled while the request runs;
//   (e) role gate: the button markup is emitted only for platform admins /
//       tenant admins (isPlatformAdmin || isSuperAdmin).
//
//   node --test CORE/Dashboard/tests/reset-mfa-button.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');
const start = src.indexOf('// Minimal HTML escaper');
const end = src.indexOf('// Locked accounts (GSEC R-096');
assert.ok(start >= 0 && end > start, 'could not locate the users action block');
const block = src.slice(start, end);

// _formatApiError lives above the action block; extract it too so the error
// path exercises the real formatter (string / array / object details).
const fmtStart = src.indexOf('// Format a FastAPI error body for human display.');
const fmtEnd = src.indexOf('export async function initializeUsersPageListeners');
assert.ok(fmtStart >= 0 && fmtEnd > fmtStart, 'could not locate _formatApiError');
const fmtBlock = src.slice(fmtStart, fmtEnd);

function run(resetMfa, confirm) {
    const calls = { reset: [], info: [], error: [], load: [] };
    const button = {
        disabled: false,
        classList: { add: () => {}, remove: () => {} },
        getAttribute: () => '42',
    };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: { getElementById: () => null },
        isPlatformAdmin: true,
        isSuperAdmin: true,
        showConfirm: () => Promise.resolve(confirm),
        showInfo: (msg, title) => { calls.info.push(msg); },
        showError: (msg, title) => { calls.error.push(msg); },
        loadUsers: (tenantId) => { calls.load.push(tenantId); return Promise.resolve(); },
        _currentUsersTenantId: () => 7,
        users: {
            resetMfa: (id, reason) => {
                calls.reset.push([id, reason]);
                return resetMfa();
            },
        },
    };
    const handler = vm.runInNewContext(
        `"use strict";\n${fmtBlock}\n${block}\nhandleResetMfaUser;`, sandbox,
        { filename: 'users-action-block' });
    return { handler, calls, button };
}

test('golden path: confirm resets, toasts, refreshes, re-enables', async () => {
    const { handler, calls, button } = run(() => ({
        user_id: 42,
        otp_credentials_removed: ['otp-1'],
        webauthn_credentials_removed: [],
        reenroll_queued: true,
    }), true);
    await handler({ target: button });
    assert.strictEqual(calls.reset.length, 1, 'resetMfa not called');
    assert.strictEqual(calls.reset[0][0], '42');
    assert.strictEqual(calls.reset[0][1], 'admin reset from Users page');
    assert.strictEqual(calls.info.length, 1, 'no success toast');
    assert.ok(/2FA reset for user 42/.test(calls.info[0]));
    assert.strictEqual(calls.load.length, 1, 'status not refreshed');
    assert.strictEqual(button.disabled, false, 'button left disabled');
});

test('cancel path: declining the confirm calls nothing', async () => {
    const { handler, calls, button } = run(() => ({}), false);
    await handler({ target: button });
    assert.strictEqual(calls.reset.length, 0, 'API called after cancel');
    assert.strictEqual(calls.info.length, 0);
    assert.strictEqual(calls.error.length, 0);
    assert.strictEqual(button.disabled, false);
});

test('error path: the API detail is surfaced verbatim', async () => {
    const { handler, calls, button } = run(() => {
        const e = new Error('API request failed with status 409');
        e.detail = 'Service accounts are exempt from 2FA - no authenticator is queued for this account';
        throw e;
    }, true);
    await handler({ target: button });
    assert.strictEqual(calls.error.length, 1, 'no error toast');
    assert.ok(/exempt from 2FA/.test(calls.error[0]), 'detail not surfaced');
    assert.ok(button.disabled === false, 'button left disabled after failure');
});

test('in-flight guard: button disabled during the round-trip', async () => {
    const { handler, calls, button } = run(async () => ({ user_id: 42 }), true);
    const pending = handler({ target: button });
    assert.strictEqual(button.disabled, true, 'not disabled while in flight');
    await pending;
    assert.strictEqual(button.disabled, false);
});

test('role gate: button markup only for platform/tenant admins', async () => {
    const gate = src.slice(
        src.indexOf('// FX.88 p4 — the Reset 2FA route needs'),
        src.indexOf('const isActive = String(user.status)'),
    );
    assert.ok(gate.length > 0, 'role-gate comment marker missing');
    assert.ok(/const canResetMfa = isPlatformAdmin \|\| isSuperAdmin;/.test(gate),
        'role predicate missing');
    const render = (pa, sa) => vm.runInNewContext(
        `"use strict";\n${gate}\nresetMfaButton;`,
        { isPlatformAdmin: pa, isSuperAdmin: sa, user: { id: 42 } });
    assert.ok(/reset-mfa-button/.test(render(true, false)), 'platform admin has no button');
    assert.ok(/reset-mfa-button/.test(render(false, true)), 'tenant admin has no button');
    assert.strictEqual(render(false, false), '', 'regular user must not see the button');
});
