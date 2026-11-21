// Node-runnable check for Dashboard/js/burst-accounts.js (GT.D11).
//
// burst-accounts.js is an ES module, so this script strips the import lines,
// runs the body in a vm sandbox with a tiny DOM, a stubbed MODEntitlements
// recorder and stubbed burstAccounts/secrets clients, invokes
// renderBurstAccountsSection() and asserts the Connect button is
// locked per edition. Cloud ships in every edition (CE included, owner
// decision 11-05) so CE is never locked; PAID editions keep the original
// Connect lock (lockElement(ba-connect-submit, 'burst', 'Cloud burst'), owner
// decision 11-11), gated on api.js isPaidEdition(). Two sandboxes (PAID, CE)
// plus a rejecting-isPaidEdition sub-case (fail closed = CE = no lock).
// The server 402 plan gate stays authoritative.
//
//   node --test CORE/Dashboard/tests/burst-lock.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'burst-accounts.js'), 'utf8');

const body = src
    .replace(/^import .* from '\.\/entitlements\.js';\s*$/m, '')
    .replace(/^import .* from '\.\/api\.js';\s*$/m, '')
    .replace(/^export default .*$/m, '')
    .replace(/^export /m, '');
assert.ok(!/^\s*(import|export) /m.test(body), 'unexpected ESM syntax left');


function makeEl(id) {
    return {
        id: id || '',
        dataset: {},
        value: '',
        innerHTML: '',
        textContent: '',
        classList: { add: () => {}, remove: () => {} },
        addEventListener: () => {},
        appendChild: () => {},
        querySelector: () => null,
        querySelectorAll: () => [],
        closest: () => null,
    };
}

const resolved = () => Promise.resolve(null);
const burstAccounts = {
    list: () => Promise.resolve([]),
    providers: () => Promise.resolve([]),
    sessions: () => Promise.resolve([]),
    ledger: () => Promise.resolve([]),
    summary: () => Promise.resolve({}),
    probe: resolved,
    remove: resolved,
    create: resolved,
    update: resolved,
};
const secrets = { create: resolved };

// Build a fresh sandbox per edition case; returns the recorded lockElement calls.
async function run(isPaidEdition) {
    const calls = [];
    const MODEntitlements = {
        lockElement: (el, cap, label) => {
            calls.push({ id: el.id, cap, label });
            return Promise.resolve();
        },
    };
    const sandbox = {
        window: {},
        document: {
            getElementById: (id) => makeEl(id),
            createElement: () => makeEl(''),
        },
        MODEntitlements,
        burstAccounts,
        secrets,
        isPaidEdition,
        console,
    };
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(body, sandbox, { filename: 'burst-accounts.js' });
    vm.runInContext('renderBurstAccountsSection()', sandbox);
    // isPaidEdition is async: let its promise (and the .then chain) settle.
    for (let n = 0; n < 5; n++) await new Promise((r) => setImmediate(r));
    return calls;
}

test('PAID: ba-connect-submit is locked with the burst capability', async () => {
    const calls = await run(() => Promise.resolve(true));
    assert.strictEqual(calls.length, 1, 'lockElement called exactly once');
    assert.strictEqual(calls[0].id, 'ba-connect-submit');
    assert.strictEqual(calls[0].cap, 'burst');
    assert.strictEqual(calls[0].label, 'Cloud burst');
});

test('CE: ba-connect-submit is NOT locked: Cloud ships in every edition', async () => {
    const calls = await run(() => Promise.resolve(false));
    assert.strictEqual(calls.length, 0, 'lockElement must not be called on CE');
});

test('CE (isPaidEdition rejects): fail closed, no lock', async () => {
    const calls = await run(() => Promise.reject(new Error('features unavailable')));
    assert.strictEqual(calls.length, 0, 'lockElement must not be called when edition is unknown');
});
