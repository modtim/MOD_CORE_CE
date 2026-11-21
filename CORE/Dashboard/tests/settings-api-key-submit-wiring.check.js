// DIAG-E7-SUBMIT — the E7 witness in dash-stack-deploy-tokens.spec.ts depends on
// `handleCreateApiKey` running when the Create Key button is clicked, so the
// `submit` listener on #create-api-key-form must be bound at PAGE INIT
// (initializeSettingsPage, which ui.js:932 calls once after settings.html is
// injected), NOT inside loadApiKeys() — a path the modal's reset flow does not
// always take. This static check pins that wiring so a future refactor cannot
// silently move the binding back into a per-panel loader and make the inline
// stack-scope hint unreachable.
//
//   node --test CORE/Dashboard/tests/settings-api-key-submit-wiring.check.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');

const dash = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(dash, 'js', 'settings.js'), 'utf8');

test('submit listener is bound at page init, not inside a panel loader', () => {
    const bind = js.indexOf("getElementById('create-api-key-form')?.addEventListener('submit'");
    assert.ok(bind > -1, 'the submit listener for #create-api-key-form is not bound at all');

    const initAt = js.indexOf('export function initializeSettingsPage()');
    assert.ok(initAt > -1, 'initializeSettingsPage not found');
    const loadAt = js.indexOf('async function loadApiKeys()');
    assert.ok(loadAt > -1, 'loadApiKeys not found');

    assert.ok(bind > initAt, 'submit listener bound before initializeSettingsPage (dead scope)');
    assert.ok(bind < loadAt, 'submit listener bound after loadApiKeys (inside a panel loader)');

    // The handler itself must exist and be the one bound.
    assert.ok(/async function handleCreateApiKey\(/.test(js), 'handleCreateApiKey missing');
    assert.ok(/addEventListener\('submit',\s*handleCreateApiKey\)/.test(js),
        'submit listener not bound to handleCreateApiKey');
});
