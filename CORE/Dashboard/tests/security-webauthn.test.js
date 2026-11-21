// Node-runnable check for Dashboard/js/account.js security-key UI (GY.C3b).
//
// account.js is an ES module, so this script strips the import lines, runs
// the body in a vm sandbox with a tiny DOM and stubbed me/getKcActionUrl,
// invokes renderSecurityKeys() and asserts:
//   (a) registered keys are listed (label + added date), badge "on";
//   (b) "Add security key" opens the MOD-branded WEBAUTHN_REGISTER
//       application-initiated action (kc_action=WEBAUTHN_REGISTER, never a
//       Keycloak console /account URL);
//   (c) no keys -> badge "off" and the button is enabled.
//
//   node --test CORE/Dashboard/tests/security-webauthn.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'account.js'), 'utf8');

const body = src
    .replace(/^import .* from '\.\/api\.js';\s*$/m, '')
    .replace(/^import .* from '\.\/auth\.js';\s*$/m, '')
    .replace(/^import .* from '\.\/utils\.js';\s*$/m, '')
    .replace(/^export /gm, '');
assert.ok(!/^\s*(import|export) /m.test(body), 'unexpected ESM syntax left');

function makeEl(id) {
    return {
        id: id || '',
        dataset: {},
        value: '',
        innerHTML: '',
        textContent: '',
        disabled: true,
        className: '',
        classList: { add: () => {}, remove: () => {} },
        listeners: {},
        addEventListener(type, fn) { this.listeners[type] = fn; },
    };
}

const elements = {};
function getEl(id) {
    if (!elements[id]) elements[id] = makeEl(id);
    return elements[id];
}

const opened = [];
const sandbox = {
    console: { log: () => {} },
    document: { getElementById: getEl },
    window: { open: (url, _t, _o) => { opened.push(url); } },
    me: {
        getMfa: async () => ({
            enrolled: true,
            webauthn_registered: true,
            security_keys: [{
                id: 'cred-wa',
                type: 'WEBAUTHN',
                device_label: 'Office YubiKey',
                created_at: '2026-10-02T10:00:00Z',
            }],
            register_security_key_url:
                'http://keycloak.test/realms/modtex/protocol/openid-connect/auth?kc_action=WEBAUTHN_REGISTER',
        }),
    },
    getKcActionUrl: async (action, apiActionUrl) => {
        assert.strictEqual(action, 'WEBAUTHN_REGISTER');
        assert.ok(apiActionUrl.includes('kc_action=WEBAUTHN_REGISTER'));
        return apiActionUrl;
    },
    getMfaSetupUrl: async (u) => u,
    withButtonBusy: async (btn, fn) => fn(),
};
vm.createContext(sandbox);
vm.runInContext(body, sandbox, { filename: 'account.js' });

test('registered keys are listed and badge is on', async () => {
    const status = await sandbox.me.getMfa();
    await sandbox.renderSecurityKeys(status);
    assert.strictEqual(getEl('webauthn-status-badge').textContent, 'on');
    assert.ok(getEl('webauthn-status-detail').textContent.includes('1 security key'));
    assert.ok(getEl('webauthn-keys-list').innerHTML.includes('Office YubiKey'));
    assert.ok(getEl('webauthn-keys-list').innerHTML.includes('2026-10-02'));
    assert.strictEqual(getEl('webauthn-add-btn').disabled, false);
});

test('Add security key starts the MOD-branded WEBAUTHN_REGISTER action',
    async () => {
        const status = await sandbox.me.getMfa();
        await sandbox.renderSecurityKeys(status);
        opened.length = 0;
        // handler was attached on first render; re-attach is idempotent
        await getEl('webauthn-add-btn').listeners['click']();
        assert.strictEqual(opened.length, 1);
        assert.ok(opened[0].includes('kc_action=WEBAUTHN_REGISTER'));
        assert.ok(!opened[0].includes('/account'),
            'never a Keycloak console URL');
});

test('no keys -> badge off, button enabled', async () => {
    const status = {
        enrolled: false,
        webauthn_registered: false,
        security_keys: [],
        register_security_key_url: 'http://keycloak.test/x?kc_action=WEBAUTHN_REGISTER',
    };
    await sandbox.renderSecurityKeys(status);
    assert.strictEqual(getEl('webauthn-status-badge').textContent, 'off');
    assert.strictEqual(getEl('webauthn-add-btn').disabled, false);
});
