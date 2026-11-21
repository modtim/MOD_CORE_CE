// 2400LR-ceSignup-a2 — CE accept-invitation check (static, source-text asserts;
// no browser). Style of tests/admin-ingress-allowlist.check.js.
//
//   node CORE/Dashboard/tests/accept-invitation-ce.check.js
//
// Checks:
//   1) js/accept-invitation.js parses as a valid ES module.
//   2) pages/accept-invitation.html carries the CE form markup ids.
//   3) CE branch POSTs the contract route with the token in the BODY only.
//   4) No URL built in the CE branch carries the token (no /accept or ?token= POST).
//   5) Server text enters via textContent only — no innerHTML anywhere in the module.
//   6) Non-CE branch still redirects via getRegistrationUrl (unchanged shape).
//   7) ui.js hides signup-button on CE and only attaches the listener on non-CE.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const js = read('js/accept-invitation.js');
const html = read('pages/accept-invitation.html');
const ui = read('js/ui.js');

// 1) ES module syntax
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: js, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
assert.ok(!modErr, `accept-invitation.js not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// 2) CE form markup present + hidden by default
['ce-form-wrap', 'ce-accept-form', 'ce-first', 'ce-last', 'ce-password', 'ce-password-confirm', 'ce-submit', 'ce-form-error', 'ce-email', 'ce-success'].forEach((id) => {
    assert.ok(html.includes(`id="${id}"`), `pages/accept-invitation.html missing #${id}`);
});
assert.ok(/id="ce-form-wrap"[^>]*class="hidden/.test(html), 'CE form wrap must start hidden (JS unhides it only on CE)');
assert.ok(/id="ce-success"[^>]*class="hidden/.test(html), 'CE success block must start hidden');

// 3) contract route + token in body
// APP_CONFIG.API_URL already ends in /v1 (inject-config.sh), so the path is /onboarding/... (a literal /v1 here made /v1/v1/... = 404, D74).
assert.ok(/\$\{apiBase\}\/onboarding\/invitations\/accept/.test(js), 'CE branch must POST ${apiBase}/onboarding/invitations/accept');
assert.ok(!/\$\{apiBase\}\/v1\//.test(js), 'apiBase already ends in /v1: no second /v1 segment');
assert.ok(/method:\s*'POST'/.test(js), 'accept POST must be a POST');
assert.ok(/body:\s*JSON\.stringify\(\s*\{\s*token,/.test(js), 'token must be sent in the JSON body');

// 4) no token in any URL the CE branch builds
assert.ok(!/invitations\/accept\?/.test(js), 'token must never appear in the accept URL');
const postLines = js.split('\n').filter((l) => /fetch\(/.test(l));
assert.ok(postLines.every((l) => !/accept/.test(l) || !/\$\{token\}|\?token/.test(l)), 'no POST URL may interpolate the token');

// 5) textContent only — never innerHTML
assert.ok(!/\.innerHTML/.test(js), 'accept-invitation.js must not assign/read .innerHTML (server text via textContent only)');
assert.ok(/textContent/.test(js), 'server/user text enters via textContent');

// 6) non-CE branch unchanged
assert.ok(/await getRegistrationUrl\(\)/.test(js), 'non-CE branch still uses getRegistrationUrl');
assert.ok(/stateData = \{\s*invitation_token: token,\s*flow: 'invitation'\s*\}/.test(js), 'non-CE OAuth state unchanged');
assert.ok(/import \{ getRegistrationUrl, handleLogin \} from '\.\/auth\.js'/.test(js), 'auth.js imports present');

// 7) ui.js CE signup hide
const win = ui.split('\n').filter((l) => /signup-button/.test(l)).join('\n');
assert.ok(/function _registrationAllowed\(\)/.test(ui)
    && /_registrationAllowed\(\)[\s\S]{0,400}?signup-button/.test(ui),
    'signup-button gated on _registrationAllowed (R301-r2 fail-closed reveal)');
assert.ok(/style\.setProperty\('display', 'none'\)/.test(win), 'CE hides signup-button');
assert.ok(/addEventListener\("click", handleSignup\)/.test(win), 'non-CE still attaches handleSignup');

console.log('accept-invitation-ce.check.js: all asserts passed');
